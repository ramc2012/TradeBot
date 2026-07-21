"""Universe-wide option-premium backfill for ONE expiry, reusing existing machinery.

Per underlying it takes the REAL broker strike ladder for the target expiry out
of ``fo_contract_catalog`` (freshly synced by sync_expiry_catalog.py), picks
the strikes nearest the spot anchor, and re-drives the SAME primitives the live
loader uses — ``OptionHistoryService._fetch_broker_candles`` /
``._persist_broker_candles`` via ``backfill_underlying_option_ladder._one`` —
under CLASS_BULK.

Why the catalog rather than a derived step: a newly-listed monthly expiry has a
COARSER ladder than the front month (AXISBANK: 10 in Jul-28, 20 in Aug-25), so a
step derived from the front month asks the broker for contracts that do not
exist and burns half the calls on 'Invalid symbol'.

Uses the Upstox instrument key when the catalog has one, so the persisted rows
carry OPEN INTEREST (the Fyers history payload has no oi field at all).
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

import api.routers.auth as auth  # noqa: E402


async def _no_ws() -> None:
    """Never open a second market-data WebSocket from a backfill process."""
    return None


auth._sync_market_data_feed = _no_ws  # noqa: SLF001

from db.database import AsyncSessionLocal  # noqa: E402
from market_data.atm_watchlist import _spot_spanning_window  # noqa: E402
from market_data.option_history import OptionHistoryService  # noqa: E402
from scripts.backfill_underlying_option_ladder import _one  # noqa: E402


def _d(v: str) -> date:
    return datetime.strptime(v, "%Y-%m-%d").date()


async def _ladders(expiry: date) -> dict[str, dict[tuple[float, str], str]]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "SELECT underlying, strike::float8 AS strike, option_type, instrument_key "
            "FROM fo_contract_catalog WHERE expiry = :e AND option_type IN ('CE','PE')"
        ), {"e": expiry})).fetchall()
    out: dict[str, dict[tuple[float, str], str]] = defaultdict(dict)
    for r in rows:
        out[r.underlying][(float(r.strike), r.option_type)] = r.instrument_key
    return out


async def _spots() -> dict[str, float]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            """
            SELECT DISTINCT ON (underlying) underlying, close::float8 AS px
            FROM underlying_spot_candles
            WHERE time >= TIMESTAMPTZ '2026-07-16 00:00:00+00'
              AND time <  TIMESTAMPTZ '2026-07-22 00:00:00+00'
              AND close > 0 AND source <> 'live_tick'
            ORDER BY underlying, time DESC
            """
        ))).fetchall()
    return {r.underlying: float(r.px) for r in rows}


async def _universe(kinds: set[str]) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "SELECT symbol, kind FROM fo_underlying_catalog ORDER BY symbol"
        ))).fetchall()
    return [(r.symbol, r.kind) for r in rows if r.kind.upper() in kinds]


async def _count(underlying: str, expiry: date, interval: str,
                 d0: date, d1: date) -> tuple[int, int]:
    async with AsyncSessionLocal() as s:
        row = (await s.execute(text(
            """
            SELECT count(*) AS n, count(DISTINCT strike) AS k
            FROM option_premium_candles
            WHERE underlying = :u AND expiry = :e AND interval = :i
              AND time >= :d0 AND time < :d1
            """
        ), {"u": underlying, "e": expiry, "i": interval, "d0": d0,
            "d1": d1 + timedelta(days=1)})).one()
    return int(row.n), int(row.k)


async def _amain(a) -> int:
    if auth.get_active_adapter("upstox") is None:
        await auth.ensure_upstox_session(force_validate=True)

    ladders = await _ladders(a.expiry)
    spots = await _spots()
    universe = await _universe({k.strip().upper() for k in a.kinds.split(",")})
    if a.only:
        want = {x.strip().upper() for x in a.only.split(",")}
        universe = [(s, k) for s, k in universe if s.upper() in want]
    universe = universe[a.skip: a.skip + a.limit if a.limit else None]

    svc = OptionHistoryService()
    sem = asyncio.Semaphore(a.concurrency)
    failures: list[str] = []
    totals: dict[str, int] = defaultdict(int)
    t0 = time.monotonic()

    for n, (sym, kind) in enumerate(universe, 1):
        ladder = ladders.get(sym) or {}
        px = spots.get(sym)
        if not ladder or not px:
            failures.append(f"{sym}: no {'ladder' if not ladder else 'spot anchor'}")
            totals["skipped"] += 1
            continue
        strikes = sorted({k for k, _ot in ladder})
        window = _spot_spanning_window(strikes, px)
        if a.extra:
            lo = strikes.index(window[0])
            hi = strikes.index(window[-1])
            window = strikes[max(0, lo - a.extra): min(len(strikes), hi + a.extra + 1)]
        legs = [(k, ot, ladder[(k, ot)])
                for k in window for ot in ("CE", "PE") if (k, ot) in ladder]
        if not legs:
            failures.append(f"{sym}: window {window} has no catalog legs")
            totals["skipped"] += 1
            continue

        before_n, before_k = await _count(sym, a.expiry, a.interval, a.from_date, a.to_date)
        stats: dict[str, int] = defaultdict(int)

        async def run(strike: float, ot: str, key: str, _sym: str = "") -> None:
            async with sem:
                try:
                    status, rows = await _one(
                        svc, underlying=_sym, expiry=a.expiry, strike=strike,
                        option_type=ot, key=key, interval=a.interval,
                        # NOT to_date+1. Upstox's historical range is INCLUSIVE,
                        # and a range that reaches the current IST day forks
                        # _fetch_broker_candles into historical + intraday, i.e.
                        # TWO broker calls per leg instead of one. Ending on a
                        # completed session halves the whole run's budget.
                        from_date=a.from_date, to_date=a.to_date,
                    )
                    stats[status] += 1
                    stats["broker_rows"] += rows
                except Exception as exc:  # noqa: BLE001 — never abort the run
                    stats["error"] += 1
                    failures.append(f"{_sym} {key}: {type(exc).__name__}: {exc}")

        await asyncio.gather(*(run(s, ot, k, sym) for s, ot, k in legs))
        after_n, after_k = await _count(sym, a.expiry, a.interval, a.from_date, a.to_date)
        totals["rows_added"] += after_n - before_n
        totals["names"] += 1
        totals["calls"] += len(legs)
        for key in ("ok", "empty", "error"):
            totals[key] += stats[key]
        print(f"[{n}/{len(universe)}] {sym} {kind} spot={px:.1f} win={window} "
              f"legs={len(legs)} ok={stats['ok']} empty={stats['empty']} err={stats['error']} "
              f"rows {before_n}->{after_n} (+{after_n - before_n}) strikes {before_k}->{after_k} "
              f"t={time.monotonic() - t0:.0f}s", flush=True)

    from brokers.rate_limiter import FYERS_DATA_LIMITER, UPSTOX_DATA_LIMITER
    print("TOTALS", dict(totals), f"wall={time.monotonic() - t0:.0f}s")
    print("FYERS_LIMITER", FYERS_DATA_LIMITER.snapshot())
    print("UPSTOX_LIMITER", UPSTOX_DATA_LIMITER.snapshot())
    print(f"FAILURES={len(failures)}")
    for f in failures[:80]:
        print("  FAIL", f)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--expiry", type=_d, required=True)
    p.add_argument("--from-date", type=_d, required=True, dest="from_date")
    p.add_argument("--to-date", type=_d, required=True, dest="to_date")
    p.add_argument("--interval", default="30minute")
    p.add_argument("--extra", type=int, default=0,
                   help="strikes to add each side of the 3-strike spanning window")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--kinds", default="STOCK")
    p.add_argument("--only", default=None)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--skip", type=int, default=0)
    return asyncio.run(_amain(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
