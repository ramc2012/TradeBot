"""Measure the strike-resolution rate at a given expiry WITHOUT any broker call.

Definition (stated explicitly so the number is reproducible):
  For every (underlying, side) in the F&O universe we take
    * the strike GRID from fo_contract_catalog at --grid-expiry (the monthly
      grid is identical month to month; the August grid is not in the catalog),
    * the SPOT anchor = latest non-live_tick close in underlying_spot_candles,
    * the 3-strike spot-spanning window used by the real selector
      (atm_watchlist._spot_spanning_window),
    * the REAL production prior-volume loader
      (macd_watchlist.load_prior_volume) at --expiry.
  A side RESOLVES when at least one strike in that window has measurable
  prior-session median volume >= the production min_flow floor
  (MACD_STRIKE_MIN_PRIOR_VOLUME_STOCK / _INDEX).

This isolates exactly the component the backfill changes. It deliberately does
NOT model the live-chain oi / bid-ask-spread floors, which come from a live
option chain and no backfill can supply.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from db.database import AsyncSessionLocal  # noqa: E402
from market_data.atm_watchlist import _spot_spanning_window, strike_liquidity_floors  # noqa: E402
from market_data.macd_watchlist import load_prior_volume  # noqa: E402


async def _universe() -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "SELECT symbol, kind FROM fo_underlying_catalog ORDER BY symbol"
        ))).fetchall()
    return [(r.symbol, r.kind) for r in rows]


async def _grid(grid_expiry: date) -> dict[str, list[float]]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "SELECT underlying, array_agg(DISTINCT strike::float8) AS ks "
            "FROM fo_contract_catalog WHERE expiry = :e GROUP BY underlying"
        ), {"e": grid_expiry})).fetchall()
    return {r.underlying: sorted(r.ks) for r in rows}


async def _spots() -> dict[str, float]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            """
            SELECT DISTINCT ON (underlying) underlying, close::float8 AS px
            FROM underlying_spot_candles
            WHERE time >= TIMESTAMPTZ '2026-07-17 00:00:00+00'
              AND time <  TIMESTAMPTZ '2026-07-22 00:00:00+00'
              AND close > 0 AND source <> 'live_tick'
            ORDER BY underlying, time DESC
            """
        ))).fetchall()
    return {r.underlying: float(r.px) for r in rows}


async def _dedup_prior_volume(sym: str, expiry: date, today) -> dict[str, dict[float, float]]:
    """Same statistic as load_prior_volume, but immune to the cross-broker
    duplicate-row defect: option_premium_candles' primary key is
    (instrument_key, interval, time), so the SAME contract-bar persisted under
    a Fyers key AND an Upstox key becomes two rows, and production's
    SUM(volume) counts it twice. Here we collapse to one row per
    (strike, side, bar) with MAX(volume) before summing per session."""
    import statistics as _stats

    from market_data.macd_watchlist import _session_bounds_utc, prior_trading_sessions

    days = prior_trading_sessions(today=today)
    if not days:
        return {"CE": {}, "PE": {}}
    start_utc, _ = _session_bounds_utc(days[0])
    _, end_utc = _session_bounds_utc(days[-1])
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            """
            WITH one_row_per_bar AS (
              SELECT option_type, strike, time, MAX(volume) AS volume
                FROM option_premium_candles
               WHERE time >= :start_utc AND time < :end_utc
                 AND underlying = :symbol AND expiry = :expiry
                 AND interval = '30minute'
               GROUP BY option_type, strike, time
            )
            SELECT option_type, strike,
                   date_trunc('day', time AT TIME ZONE 'Asia/Kolkata') AS session_key,
                   SUM(volume) AS session_volume
              FROM one_row_per_bar
             GROUP BY option_type, strike, session_key
            """
        ), {"start_utc": start_utc, "end_utc": end_utc,
            "symbol": sym, "expiry": expiry})).fetchall()
    buckets: dict[str, dict[float, list[float]]] = {"CE": {}, "PE": {}}
    for r in rows:
        side = str(r.option_type or "").upper()
        if side in buckets:
            buckets[side].setdefault(float(r.strike), []).append(float(r.session_volume or 0.0))
    return {side: {k: float(_stats.median(v)) for k, v in per.items()}
            for side, per in buckets.items()}


async def _amain(a) -> int:
    universe = await _universe()
    grid = await _grid(a.grid_expiry)
    spots = await _spots()

    total = resolved = 0
    no_grid = no_spot = 0
    per_name: list[tuple[str, str, int, int]] = []
    for sym, kind in universe:
        ks = grid.get(sym)
        px = spots.get(sym)
        if not ks:
            no_grid += 1
            total += 2
            continue
        if not px:
            no_spot += 1
            total += 2
            continue
        window = _spot_spanning_window(ks, px)
        _min_oi, min_flow, _sp = strike_liquidity_floors(kind)
        if a.dedup and kind.upper() != "INDEX":
            prior = await _dedup_prior_volume(sym, a.expiry, a.today)
        else:
            prior = await load_prior_volume(
                underlying=sym, kind=kind, expiry=a.expiry, today=a.today
            )
        ok = 0
        for side in ("CE", "PE"):
            side_map = prior.get(side) or {}
            total += 1
            if any(float(side_map.get(k, -1.0)) >= min_flow for k in window):
                resolved += 1
                ok += 1
        per_name.append((sym, kind, ok, len(window)))

    print(f"expiry={a.expiry} grid_expiry={a.grid_expiry} today={a.today}")
    print(f"universe={len(universe)} no_grid={no_grid} no_spot={no_spot}")
    print(f"sides_total={total} sides_resolved={resolved} "
          f"RATE={100.0 * resolved / total if total else 0.0:.1f}%")
    both = sum(1 for _s, _k, ok, _w in per_name if ok == 2)
    one = sum(1 for _s, _k, ok, _w in per_name if ok == 1)
    none = sum(1 for _s, _k, ok, _w in per_name if ok == 0)
    print(f"names both_sides={both} one_side={one} neither={none}")
    if a.list_missing:
        print("NEITHER:", ",".join(s for s, _k, ok, _w in per_name if ok == 0))
    return 0


def _d(v: str) -> date:
    return datetime.strptime(v, "%Y-%m-%d").date()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--expiry", type=_d, required=True)
    p.add_argument("--grid-expiry", type=_d, default=_d("2026-07-28"), dest="grid_expiry")
    p.add_argument("--today", type=_d, default=None)
    p.add_argument("--list-missing", action="store_true", dest="list_missing")
    p.add_argument("--dedup", action="store_true",
                   help="collapse cross-broker duplicate bars before summing volume")
    return asyncio.run(_amain(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
