"""Per-symbol open interest, positioning state, and price performance.

THE FINDING THIS MODULE EXISTS BECAUSE OF
--------------------------------------------------------------------------
m2_flow.py declares, as a verified result, that no stock-level OI source
exists in this schema, and hardcodes its fourth ingredient -- the delta-OI
conjunction, 15% of the flow composite -- to NULL for every row ever
written. `classify_oi_state()` sits there implemented and unit-tested and
has never once been called with real data.

Two live sources exist. Both were fresh the day this was written:

  fo_mwpl_snapshot.open_interest   Aggregate F&O OI per symbol, daily, 211
                                   symbols, back to 2026-07-09. This is NSE's
                                   own MWPL publication -- the same file the
                                   ban list is read from. The lane already
                                   ingests it and looks at nothing but the
                                   ban flag.
  option_premium_candles.oi        Per-contract chain OI, 213 underlyings,
                                   latest print 2026-08-27 09:57 UTC. Only
                                   the GREEKS on that table stopped in July;
                                   close/oi/volume never did.

The second also revives an aggregate everyone treats as dead:
`fo_option_chain_metrics` (ce_oi/pe_oi/oi_pcr) tops out at 2026-08-03, but
its inputs are alive, so the CE/PE split is recomputed here instead of read.

WHY TWO SOURCES AND NOT ONE
--------------------------------------------------------------------------
They measure different things and must never be silently interchanged. MWPL
OI is exchange-published across every series in the name. `chain_sum` is
this lane's own sum over whichever contracts it collected, which is a subset
that varies with collection health. Every row records its `oi_source`, and a
delta is only ever taken between two rows of the SAME source -- otherwise a
collection gap would masquerade as a position unwind, which is precisely the
kind of artefact that has cost this project real money before.

THE CONJUNCTION
--------------------------------------------------------------------------
  price up,   OI up    long buildup      new longs, conviction behind it
  price down, OI up    short buildup     new shorts, conviction behind it
  price up,   OI down  short covering    shorts closing -- a weaker rally
  price down, OI down  long unwinding    longs closing -- a weaker decline

Computed by m2_flow.classify_oi_state(), imported rather than restated: it
is already the tested definition, and a second copy would be a second thing
to keep in step.

    python vanguard/features/m_oi_positioning.py --lookback-days 90 --write
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from features.m2_flow import classify_oi_state  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

# NSE bans fresh F&O positions in a name once market-wide OI crosses this share
# of the position limit. Surfaced, not enforced -- M7 still has no ban veto.
MWPL_BAN_PCT = 95.0
RETURN_HORIZONS = (5, 20, 60)


def load_universe(connection) -> list[str]:
    frame = pd.read_sql(
        "SELECT symbol FROM sector_taxonomy WHERE instrument_type = 'Equity'", connection)
    return sorted(frame["symbol"].unique())


def load_mwpl(connection, symbols: list[str], start: date, end: date) -> pd.DataFrame:
    """NSE's own aggregate F&O open interest, plus the position limit it is
    measured against."""
    query = """
        SELECT snapshot_date AS dt, symbol,
               open_interest AS total_oi,
               market_wide_position_limit AS mwpl
        FROM fo_mwpl_snapshot
        WHERE symbol = ANY(%(symbols)s)
          AND snapshot_date >= %(start)s AND snapshot_date <= %(end)s
    """
    frame = pd.read_sql(query, connection, params={
        "symbols": symbols, "start": start, "end": end})
    for col in ("total_oi", "mwpl"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def load_chain_oi(connection, symbols: list[str], start: date, end: date) -> pd.DataFrame:
    """Front-expiry CE/PE open interest per session, from live contract OI.

    Same end-of-session dedup discipline as m2_flow.load_chain_eod: contract
    rows genuinely duplicate at identical timestamps, so the last print per
    (contract, day) is taken with an explicit non-NULL tiebreak rather than
    whatever order Postgres returns.

    Front expiry is the SMALLEST expiry still strictly after the session --
    not `min(expiry)` outright, which is what m2_flow does and which on an
    expiry day reads the contract that is expiring that afternoon.
    """
    query = """
        WITH ranked AS (
            SELECT underlying, expiry, strike, option_type, oi,
                   date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
                   ROW_NUMBER() OVER (
                       PARTITION BY underlying, expiry, strike, option_type,
                                    date(time AT TIME ZONE 'Asia/Kolkata')
                       ORDER BY time DESC, (oi IS NULL) ASC) AS rn
            FROM option_premium_candles
            WHERE underlying = ANY(%(symbols)s) AND interval = '30minute'
              AND time >= %(start)s AND time < %(end)s
              AND oi IS NOT NULL
        ),
        eod AS (
            SELECT underlying, expiry, option_type, oi, dt FROM ranked WHERE rn = 1
        ),
        front AS (
            SELECT underlying, dt, min(expiry) AS front_expiry
            FROM eod WHERE expiry > dt GROUP BY underlying, dt
        )
        SELECT e.underlying AS symbol, e.dt,
               sum(e.oi) FILTER (WHERE e.option_type = 'CE') AS ce_oi,
               sum(e.oi) FILTER (WHERE e.option_type = 'PE') AS pe_oi
        FROM eod e
        JOIN front f ON f.underlying = e.underlying AND f.dt = e.dt
                    AND e.expiry = f.front_expiry
        GROUP BY e.underlying, e.dt
    """
    frame = pd.read_sql(query, connection, params={
        "symbols": symbols, "start": start, "end": end + timedelta(days=1)})
    for col in ("ce_oi", "pe_oi"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def load_daily_closes(connection, symbols: list[str], start: date, end: date) -> pd.DataFrame:
    """Daily close per symbol, on NSE's own :15/:45 session grid.

    The grid filter matters for the same reason it matters in m5_timing: the
    table carries a second, 15-minute-offset grid, and an unfiltered "last
    print of the day" can land on a bar from the wrong session shape.
    """
    query = """
        SELECT DISTINCT ON (underlying, date(time AT TIME ZONE 'Asia/Kolkata'))
               underlying AS symbol,
               date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
               close
        FROM underlying_spot_candles
        WHERE interval = '30minute' AND underlying = ANY(%(symbols)s)
          AND time >= %(start)s AND time < %(end)s
          AND EXTRACT(minute FROM time AT TIME ZONE 'Asia/Kolkata') IN (15, 45)
          AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN TIME '09:15' AND TIME '15:15'
        ORDER BY underlying, date(time AT TIME ZONE 'Asia/Kolkata'), time DESC
    """
    frame = pd.read_sql(query, connection, params={
        "symbols": symbols, "start": start, "end": end + timedelta(days=1)})
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame


# --------------------------------------------------------------------------
# Pure assembly (offline-testable)
# --------------------------------------------------------------------------
def build(mwpl: pd.DataFrame, chain: pd.DataFrame, closes: pd.DataFrame) -> pd.DataFrame:
    """One row per (symbol, session). Deltas are per symbol, in session order.

    A symbol's `d_oi` is only computed between consecutive rows OF THE SAME
    `oi_source`; where the source changes, the delta is NULL rather than a
    difference between two different measurements of the world.
    """
    frames = []
    if not closes.empty:
        frames.append(closes[["symbol", "dt"]])
    if not mwpl.empty:
        frames.append(mwpl[["symbol", "dt"]])
    if not chain.empty:
        frames.append(chain[["symbol", "dt"]])
    if not frames:
        return pd.DataFrame()

    grid = pd.concat(frames, ignore_index=True).drop_duplicates()
    out = grid
    for frame in (closes, mwpl, chain):
        if not frame.empty:
            out = out.merge(frame, on=["symbol", "dt"], how="left")
    for col in ("total_oi", "mwpl", "ce_oi", "pe_oi", "close"):
        if col not in out.columns:
            out[col] = np.nan

    # Prefer the exchange's own aggregate; fall back to this lane's chain sum.
    chain_sum = out["ce_oi"].fillna(0) + out["pe_oi"].fillna(0)
    chain_sum = chain_sum.where(out[["ce_oi", "pe_oi"]].notna().any(axis=1))
    out["oi_source"] = np.where(out["total_oi"].notna(), "mwpl",
                                np.where(chain_sum.notna(), "chain_sum", None))
    out["total_oi"] = out["total_oi"].fillna(chain_sum)

    out["oi_pcr"] = np.where(
        (out["ce_oi"].notna()) & (out["pe_oi"].notna()) & (out["ce_oi"] > 0),
        out["pe_oi"] / out["ce_oi"].replace(0, np.nan), np.nan)
    out["mwpl_pct"] = np.where(
        (out["mwpl"].notna()) & (out["mwpl"] > 0),
        100.0 * out["total_oi"] / out["mwpl"].replace(0, np.nan), np.nan)

    rows = []
    for symbol, group in out.sort_values("dt").groupby("symbol"):
        group = group.copy()
        # The OI delta must span the SAME interval the price delta does, or the
        # conjunction pairs a two-day OI move with a one-day price move. Both
        # are therefore taken across trading sessions only.
        group["_traded"] = group["close"].notna()
        prev_oi = group["total_oi"].where(group["_traded"]).shift(1).ffill()
        prev_source = group["oi_source"].where(group["_traded"]).shift(1).ffill()
        same_source = group["oi_source"].eq(prev_source) & group["oi_source"].notna()
        prev_oi = prev_oi.where(same_source)

        group["prev_total_oi"] = prev_oi
        group["d_oi"] = group["total_oi"] - prev_oi
        group["d_oi_pct"] = np.where(
            prev_oi.notna() & (prev_oi != 0), 100.0 * group["d_oi"] / prev_oi, np.nan)

        # PRICE SHIFTS ONLY OVER TRADING SESSIONS.
        # fo_mwpl_snapshot publishes on days the equity market did not trade
        # (2026-08-23 was a Sunday and has 207 rows). Those rows join into the
        # grid with a real total_oi and no close, so shifting `close` over the
        # RAW grid put a NaN in front of the next real session and silently
        # blanked its d_price_pct -- and with it its oi_state. Confirmed live:
        # 24-Aug had 207 OI rows and zero positioning reads for exactly this
        # reason. The price series is therefore built over sessions that
        # actually have a close, then joined back.
        traded = group[group["close"].notna()].copy()
        if not traded.empty:
            traded["prev_close"] = traded["close"].shift(1)
            traded["d_price_pct"] = np.where(
                traded["prev_close"].notna() & (traded["prev_close"] != 0),
                100.0 * (traded["close"] - traded["prev_close"]) / traded["prev_close"], np.nan)
            for horizon in RETURN_HORIZONS:
                base = traded["close"].shift(horizon)
                traded[f"ret_{horizon}d"] = np.where(
                    base.notna() & (base != 0), 100.0 * (traded["close"] - base) / base, np.nan)
            price_cols = ["prev_close", "d_price_pct"] + [f"ret_{h}d" for h in RETURN_HORIZONS]
            group = group.drop(columns=[c for c in price_cols if c in group.columns])
            group = group.merge(traded[["dt"] + price_cols], on="dt", how="left")
        else:
            group["prev_close"] = np.nan
            group["d_price_pct"] = np.nan
            for horizon in RETURN_HORIZONS:
                group[f"ret_{horizon}d"] = np.nan

        group["d_oi_pcr"] = group["oi_pcr"] - group["oi_pcr"].shift(1)

        group["oi_state"] = [
            classify_oi_state(
                None if pd.isna(d_oi) else float(d_oi),
                None if pd.isna(d_px) else float(d_px),
            )
            for d_oi, d_px in zip(group["d_oi"], group["d_price_pct"])
        ]
        group["oi_state_strength"] = np.where(
            group["oi_state"].notna(),
            group["d_oi_pct"].abs() * group["d_price_pct"].abs(), np.nan)
        rows.append(group.drop(columns=["_traded"], errors="ignore"))

    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


COLUMNS = ("dt", "symbol", "total_oi", "prev_total_oi", "d_oi", "d_oi_pct", "oi_source",
           "mwpl", "mwpl_pct", "ce_oi", "pe_oi", "oi_pcr", "d_oi_pcr",
           "close", "prev_close", "d_price_pct", "ret_5d", "ret_20d", "ret_60d",
           "oi_state", "oi_state_strength")
INT_COLUMNS = {"total_oi", "prev_total_oi", "d_oi", "mwpl", "ce_oi", "pe_oi"}


def _cell(value, column):
    if value is None or (isinstance(value, float) and (np.isnan(value) or np.isinf(value))):
        return None
    if pd.isna(value):
        return None
    if column in INT_COLUMNS:
        return int(value)
    if column in ("dt", "symbol", "oi_source", "oi_state"):
        return value
    return float(value)


def upsert(connection, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    rows = [tuple(_cell(row[c], c) for c in COLUMNS) for _, row in frame.iterrows()]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c not in ("dt", "symbol"))
    with connection.cursor() as cursor:
        psycopg2.extras.execute_values(
            cursor,
            f"""INSERT INTO oi_positioning ({", ".join(COLUMNS)}) VALUES %s
                ON CONFLICT (dt, symbol) DO UPDATE SET {updates}, computed_at = now()""",
            rows, page_size=500,
        )
    return len(rows)


def run(dsn: str, lookback_days: int, write: bool) -> dict:
    connection = psycopg2.connect(dsn)
    connection.autocommit = True
    try:
        symbols = load_universe(connection)
        end = date.today()
        # The return horizons need history BEFORE the reporting window, or the
        # first sessions come back NULL for no reason other than the window.
        start = end - timedelta(days=lookback_days)
        fetch_start = start - timedelta(days=max(RETURN_HORIZONS) * 2 + 20)

        mwpl = load_mwpl(connection, symbols, fetch_start, end)
        chain = load_chain_oi(connection, symbols, fetch_start, end)
        closes = load_daily_closes(connection, symbols, fetch_start, end)
        frame = build(mwpl, chain, closes)
        if not frame.empty:
            frame = frame[frame["dt"] >= start]

        written = upsert(connection, frame) if write else 0
        return {"frame": frame, "written": written, "symbols": symbols,
                "mwpl_rows": len(mwpl), "chain_rows": len(chain), "close_rows": len(closes)}
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    result = run(args.dsn, args.lookback_days, args.write)
    frame = result["frame"]
    print(f"universe {len(result['symbols'])} · mwpl rows {result['mwpl_rows']:,} · "
          f"chain rows {result['chain_rows']:,} · close rows {result['close_rows']:,}")
    if frame.empty:
        print("no rows assembled — check that the source tables cover this window")
        return 1

    print(f"assembled {len(frame):,} symbol-sessions, {frame['dt'].min()} .. {frame['dt'].max()}")
    print(f"\noi_source: {frame['oi_source'].value_counts(dropna=False).to_dict()}")
    print("\noi_state distribution (NULL = a leg was missing or flat, never a guess):")
    print(frame["oi_state"].value_counts(dropna=False).to_string())

    latest = frame[frame["dt"] == frame["dt"].max()]
    banned = latest[latest["mwpl_pct"] >= MWPL_BAN_PCT]
    print(f"\nlatest session {frame['dt'].max()}: {len(latest)} symbols, "
          f"{latest['oi_state'].notna().sum()} with a positioning read")
    if not banned.empty:
        print(f"  MWPL >= {MWPL_BAN_PCT}% (NSE bans fresh F&O): "
              f"{sorted(banned['symbol'].tolist())}")
    top = latest.reindex(latest["oi_state_strength"].sort_values(ascending=False).index).head(8)
    print("\nmost emphatic positioning at the latest session:")
    with pd.option_context("display.width", 170, "display.max_columns", 20):
        print(top[["symbol", "oi_state", "d_oi_pct", "d_price_pct", "mwpl_pct",
                   "oi_pcr", "ret_5d", "ret_20d"]].to_string(index=False))
    if args.write:
        print(f"\nwrote {result['written']:,} rows to oi_positioning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
