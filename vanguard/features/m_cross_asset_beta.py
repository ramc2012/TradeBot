"""M-cross-asset — 60-day rolling beta of three sector_group indices against
their spec-mapped external drivers.

    Metals & Mining -> LME proxy = equal-weight(COPPER, ALUMINI, ZINCMINI)
    Oil & Gas       -> Brent proxy = CRUDEOIL
    IT Services     -> USDINR

All three proxy legs read data that already exists rather than being
re-fetched:
  - COPPER/ALUMINI/ZINCMINI/CRUDEOIL come straight from
    `underlying_spot_candles` -- verified live 2026-08-26 to already hold
    MCX continuous-contract history back to 2021 (sources
    commodity_broker_history / fyers_mcx_cont / live_tick). These ARE
    Vanguard's Brent-proxy and LME Al/Zn/Cu proxies already, per the spec's
    own framing; re-fetching them would duplicate a source of truth.
  - USDINR is the one leg that was genuinely missing from the schema (see
    ingest/m_usdinr_fx.py's docstring for the inventory) and is read here
    from the `usdinr_daily` table that collector populates.

The spec names a fourth driver -- a "RateSens" sector_group vs IN10Y (a
bond-yield series). Neither exists: `sector_taxonomy`'s 25 sector_group
buckets (loaded from config/fno_universe_aug2026_series.csv, matching
features/m4_sector.py's own taxonomy) has no "RateSens" bucket, and IN10Y
itself could not be sourced (see README's note on this run). Both are
skipped rather than invented.

Sector_group equal-weight index construction is a byte-for-byte copy of
`features/m4_sector.py`'s own `equal_weight_index()` -- same daily-return
mean, same cumulative-product-from-100 construction -- so a reviewer
diffing the two functions should see no divergence in method, only in what
they're built for (a sector_group index here vs both sector_group and
sector20 there).

Beta/corr are computed as measured, not sign-adjusted to match the spec's
"OMC<->Brent inverse" assumption -- the whole point of running this is to
report whether that assumption holds against Oil & Gas's actual, mixed
membership (upstream ONGC/OIL/GAIL alongside downstream-refiner OMCs
BPCL/HINDPETRO/IOC, plus RELIANCE), not to force the sign doctrine expects.

    python vanguard/features/m_cross_asset_beta.py --lookback-days 60
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
LOOKBACK_DAYS = 60

# sector_group -> (driver name, member symbols read from underlying_spot_candles,
# or the special "usdinr_daily" source).
DRIVER_SPEC = {
    "Metals & Mining": ("lme_metals", ["COPPER", "ALUMINI", "ZINCMINI"], "underlying_spot_candles"),
    "Oil & Gas": ("crude_oil", ["CRUDEOIL"], "underlying_spot_candles"),
    "IT Services": ("usdinr", [], "usdinr_daily"),
}


def load_taxonomy(connection) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT symbol, sector_group FROM sector_taxonomy WHERE instrument_type = 'Equity'",
        connection,
    )


def load_equity_daily_closes(connection, symbols: list[str], start: date) -> pd.DataFrame:
    """Daily close per equity symbol -- identical query shape to
    features/m4_sector.py's load_daily_closes() (last 30-minute print of
    each session; underlying_spot_candles has no native '1day' interval)."""
    if not symbols:
        return pd.DataFrame()
    query = """
        SELECT DISTINCT ON (underlying, date(time AT TIME ZONE 'Asia/Kolkata'))
               underlying AS symbol,
               date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
               close
        FROM underlying_spot_candles
        WHERE interval = '30minute' AND underlying = ANY(%(symbols)s)
          AND time >= %(start)s
        ORDER BY underlying, date(time AT TIME ZONE 'Asia/Kolkata'), time DESC
    """
    frame = pd.read_sql(query, connection, params={"symbols": symbols, "start": start})
    return frame.pivot(index="dt", columns="symbol", values="close").astype(float)


def load_commodity_daily_closes(connection, symbols: list[str], start: date) -> pd.DataFrame:
    """Same shape as load_equity_daily_closes -- MCX names, same table,
    same 30-minute-interval-derived daily close, kept as its own function
    only because the docstring/callsite context differs (proxy legs, not
    the sector_group universe)."""
    return load_equity_daily_closes(connection, symbols, start)


def load_usdinr_daily_closes(connection, start: date) -> pd.Series:
    frame = pd.read_sql(
        "SELECT dt, close FROM usdinr_daily WHERE dt >= %(start)s ORDER BY dt",
        connection, params={"start": start},
    )
    return frame.set_index("dt")["close"].astype(float)


def equal_weight_index(closes: pd.DataFrame, members: list[str]) -> pd.Series:
    """Byte-identical construction to features/m4_sector.py's function of
    the same name: equal-weight daily returns, cumulative product from 100.
    """
    present = [symbol for symbol in members if symbol in closes.columns]
    if not present:
        return pd.Series(dtype=float)
    returns = closes[present].pct_change(fill_method=None)
    portfolio_return = returns.mean(axis=1, skipna=True)
    index = (1 + portfolio_return.fillna(0)).cumprod() * 100
    index.iloc[0] = 100.0
    return index


def rolling_beta_corr(sector_returns: pd.Series, driver_returns: pd.Series,
                       lookback: int) -> pd.DataFrame:
    aligned = pd.concat([sector_returns, driver_returns], axis=1,
                        keys=["sector", "driver"]).dropna()
    if len(aligned) < 2:
        return pd.DataFrame(columns=["beta", "corr"])
    # min_periods can never exceed the window itself (pandas raises otherwise) --
    # matters for short lookbacks (tests, or a future small --lookback-days).
    min_periods = min(lookback, max(10, lookback // 3))
    cov = aligned["sector"].rolling(lookback, min_periods=min_periods).cov(aligned["driver"])
    var = aligned["driver"].rolling(lookback, min_periods=min_periods).var()
    beta = cov / var
    corr = aligned["sector"].rolling(lookback, min_periods=min_periods).corr(aligned["driver"])
    return pd.DataFrame({"beta": beta, "corr": corr}).dropna(how="all")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--history-days", type=int, default=150,
                        help="calendar days of price history to pull before computing "
                             "the rolling window (must exceed --lookback-days with margin)")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    connection.autocommit = True
    start = date.today() - timedelta(days=args.history_days)

    taxonomy = load_taxonomy(connection)

    all_commodity_symbols = sorted({s for _, syms, src in DRIVER_SPEC.values()
                                    if src == "underlying_spot_candles" for s in syms})
    commodity_closes = load_commodity_daily_closes(connection, all_commodity_symbols, start)
    print(f"commodity daily closes: {commodity_closes.shape[0]} sessions x "
          f"{list(commodity_closes.columns)}")

    usdinr_closes = load_usdinr_daily_closes(connection, start)
    print(f"usdinr daily closes: {len(usdinr_closes)} sessions, "
          f"{usdinr_closes.index.min()} to {usdinr_closes.index.max()}")

    rows = []
    summary_lines = []
    for sector_group, (driver_name, members, source) in DRIVER_SPEC.items():
        equity_symbols = taxonomy.loc[taxonomy["sector_group"] == sector_group, "symbol"].tolist()
        if not equity_symbols:
            print(f"  {sector_group}: no equities in sector_taxonomy -- skipped")
            continue
        equity_closes = load_equity_daily_closes(connection, equity_symbols, start)
        sector_index = equal_weight_index(equity_closes, equity_symbols)
        sector_returns = sector_index.pct_change(fill_method=None)

        if source == "usdinr_daily":
            driver_series = usdinr_closes
        else:
            driver_series = equal_weight_index(commodity_closes, members)
        driver_returns = driver_series.pct_change(fill_method=None)

        table = rolling_beta_corr(sector_returns, driver_returns, args.lookback_days)
        if table.empty:
            print(f"  {sector_group} vs {driver_name}: insufficient overlapping history -- skipped")
            continue

        for dt, row in table.iterrows():
            if pd.isna(row["beta"]):
                continue
            rows.append((
                pd.Timestamp(dt).date(), sector_group, driver_name,
                float(row["beta"]), None if pd.isna(row["corr"]) else float(row["corr"]),
                args.lookback_days,
            ))

        latest = table.dropna().iloc[-1] if not table.dropna().empty else None
        if latest is not None:
            latest_dt = table.dropna().index[-1]
            n_members = len([s for s in equity_symbols if s in equity_closes.columns])
            line = (f"  {sector_group} ({n_members} equities) vs {driver_name}: "
                   f"as of {pd.Timestamp(latest_dt).date()}  "
                   f"beta={latest['beta']:+.3f}  corr={latest['corr']:+.3f}")
            summary_lines.append((sector_group, driver_name, latest['beta'], latest['corr'], line))
            print(line)

    if rows:
        with connection.cursor() as cursor:
            psycopg2.extras.execute_values(
                cursor,
                """INSERT INTO cross_asset_beta (dt, sector_group, driver, beta, corr, lookback_days)
                   VALUES %s
                   ON CONFLICT (dt, sector_group, driver) DO UPDATE SET
                     beta = EXCLUDED.beta, corr = EXCLUDED.corr,
                     lookback_days = EXCLUDED.lookback_days, computed_at = now()""",
                rows,
            )
        print(f"\ncross_asset_beta: {len(rows)} rows written")
    else:
        print("\ncross_asset_beta: 0 rows written (no driver had enough overlapping history)")

    oil_gas = next((s for s in summary_lines if s[0] == "Oil & Gas"), None)
    if oil_gas is not None:
        _, _, beta, corr, _ = oil_gas
        matches_inverse = beta < 0
        print(f"\nOil & Gas vs crude_oil sign check: spec assumes OMC<->Brent INVERSE "
              f"(sector should fall as crude rises, i.e. beta < 0). "
              f"Observed beta={beta:+.3f}, corr={corr:+.3f} -> "
              f"{'MATCHES' if matches_inverse else 'DOES NOT MATCH'} the spec's inverse assumption.")

    connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
