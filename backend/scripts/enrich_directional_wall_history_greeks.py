"""Add spot prices, implied volatility, and greeks to prepared option history."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from analysis.backtest import MACDBacktester  # noqa: E402
from data.run_upstox_research_sync import _load_upstox_token  # noqa: E402
from data.upstox_research_sync import (  # noqa: E402
    SECONDS_PER_YEAR,
    _implied_volatility,
    _option_greeks,
)


UTC = timezone.utc
DEFAULT_UNDERLYINGS_PATH = REPO_ROOT / "data" / "catalogs" / "underlyings.parquet"


def _as_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


async def _fetch_spot_series(
    *,
    client: MACDBacktester,
    symbols: list[str],
    start: date,
    end: date,
) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        key = await client._get_spot_instrument_key(symbol)
        if not key:
            print(f"spot key missing for {symbol}", flush=True)
            continue
        candles = await client._fetch_candles_from_upstox(key, start, end)
        if not candles:
            print(f"spot candles empty for {symbol}", flush=True)
            continue
        df = pd.DataFrame(candles)
        df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df[df["time"].notna() & df["close"].notna()]
        out[symbol] = df[["time", "close"]].drop_duplicates("time").sort_values("time")
        print(f"spot {symbol}: {len(out[symbol])} rows", flush=True)
    return out


def _prime_underlying_meta(client: MACDBacktester, symbols: list[str], path: Path) -> int:
    if not path.exists():
        return 0
    df = pd.read_parquet(path)
    if not {"symbol", "spot_instrument_key", "underlying_key"}.issubset(df.columns):
        return 0
    df["symbol"] = df["symbol"].astype(str).str.upper()
    primed = 0
    for row in df[df["symbol"].isin(set(symbols))].itertuples(index=False):
        symbol = str(row.symbol)
        spot_key = str(row.spot_instrument_key or "")
        underlying_key = str(row.underlying_key or spot_key)
        if not spot_key:
            continue
        client._underlying_meta_cache[symbol] = {
            "spot_instrument_key": spot_key,
            "underlying_key": underlying_key,
            "segment": "NSE_INDEX" if symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"} else "NSE_EQ",
            "display_name": symbol,
        }
        primed += 1
    return primed


def _merge_spot(options: pd.DataFrame, spot: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for symbol, group in options.groupby("underlying", sort=False):
        group = group.sort_values("time").copy()
        spot_df = spot.get(str(symbol))
        if spot_df is None or spot_df.empty:
            group["underlying_price"] = pd.NA
            frames.append(group)
            continue
        merged = pd.merge_asof(
            group,
            spot_df.rename(columns={"close": "spot_close"}).sort_values("time"),
            on="time",
            direction="nearest",
            tolerance=pd.Timedelta("45min"),
        )
        if "underlying_price" in merged.columns:
            existing = pd.to_numeric(merged["underlying_price"], errors="coerce")
        else:
            existing = pd.Series(pd.NA, index=merged.index)
        merged["underlying_price"] = existing.fillna(merged["spot_close"])
        merged = merged.drop(columns=["spot_close"])
        frames.append(merged)
    return pd.concat(frames, ignore_index=True)


def _compute_greeks(df: pd.DataFrame, risk_free_rate: float) -> pd.DataFrame:
    rows: list[dict] = []
    total = len(df)
    for idx, row in enumerate(df.to_dict(orient="records"), start=1):
        spot = _as_float(row.get("underlying_price"))
        strike = _as_float(row.get("strike"))
        premium = _as_float(row.get("close"))
        option_type = str(row.get("option_type") or "").upper()
        if spot is not None and strike is not None and premium is not None and spot > 0 and strike > 0 and option_type in {"CE", "PE"}:
            ts = pd.Timestamp(row["time"]).to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            expiry_date = pd.to_datetime(row["expiry"], errors="coerce")
            if pd.notna(expiry_date):
                expiry_dt = datetime.combine(expiry_date.date(), datetime.max.time(), tzinfo=UTC)
                tte = max(
                    (expiry_dt - ts).total_seconds() / SECONDS_PER_YEAR,
                    1.0 / (365.25 * 24 * 60),
                )
                iv = _implied_volatility(option_type, premium, spot, strike, tte, risk_free_rate)
                if iv is not None:
                    try:
                        delta, gamma, theta, vega = _option_greeks(
                            option_type,
                            spot,
                            strike,
                            tte,
                            risk_free_rate,
                            iv,
                        )
                        row.update(
                            {
                                "iv": iv,
                                "delta": delta,
                                "gamma": gamma,
                                "theta": theta,
                                "vega": vega,
                                "time_to_expiry_years": tte,
                            }
                        )
                    except Exception:
                        pass
        rows.append(row)
        if idx % 50000 == 0 or idx == total:
            print(f"greeks {idx}/{total}", flush=True)
    return pd.DataFrame(rows)


async def _amain(args: argparse.Namespace) -> int:
    token = _load_upstox_token()
    if not token:
        raise SystemExit("No Upstox token available. Connect Upstox first.")
    client = MACDBacktester(token)
    client.rate_limit_delay = args.gap_seconds

    options = pd.read_parquet(args.input) if args.input.suffix.lower() != ".csv" else pd.read_csv(args.input)
    options["time"] = pd.to_datetime(options["time"], errors="coerce", utc=True)
    options = options[options["time"].notna()].copy()
    options["underlying"] = options["underlying"].astype(str).str.upper()
    symbols = sorted(options["underlying"].dropna().unique())
    primed = _prime_underlying_meta(client, symbols, args.underlyings_path)
    print(f"primed underlying metadata: {primed}/{len(symbols)}", flush=True)

    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)
    spot = await _fetch_spot_series(client=client, symbols=symbols, start=start, end=end)
    merged = _merge_spot(options, spot)
    enriched = _compute_greeks(merged, args.risk_free_rate)
    enriched["time"] = pd.to_datetime(enriched["time"], errors="coerce", utc=True)
    enriched = enriched.sort_values(["underlying", "expiry", "strike", "option_type", "time"])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.suffix.lower() == ".csv":
        enriched.to_csv(args.output, index=False)
    else:
        enriched.to_parquet(args.output, index=False)
    summary = {
        "input": str(args.input),
        "output": str(args.output),
        "rows": int(len(enriched)),
        "spot_symbols": sorted(spot),
        "gamma_nonzero_rows": int((pd.to_numeric(enriched.get("gamma"), errors="coerce").fillna(0).abs() > 0).sum()),
        "api_calls": dict(client.api_call_counts),
    }
    summary_path = args.output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--risk-free-rate", type=float, default=0.06)
    parser.add_argument("--gap-seconds", type=float, default=0.5)
    parser.add_argument("--underlyings-path", type=Path, default=DEFAULT_UNDERLYINGS_PATH)
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
