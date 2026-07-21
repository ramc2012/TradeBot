"""Sync fo_contract_catalog for ONE expiry across the F&O universe.

Reuses the production discovery path exactly:
  upstox_adapter.get_option_contracts(underlying_key, expiry)
  -> ATMWatchlistService._persist_contracts_for_expiry (which carries the
     filter_foreign_contracts guard that fixed the M&M/MARUTI collision).

One Upstox REST call per underlying, under CLASS_BULK. Never aborts on a
per-name failure. Does NOT start a second market-data WebSocket (see the
_sync_market_data_feed no-op below).
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import time
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

import api.routers.auth as auth  # noqa: E402


async def _no_ws() -> None:
    """A backfill process must NEVER open a second Fyers/Upstox WebSocket on
    the live token: it churns the primary connection and adds subscriptions
    (the 2026-07-20 cross-symbol contamination scales with subscription
    count). Session restore still runs; only the feed sync is neutered."""
    return None


auth._sync_market_data_feed = _no_ws  # noqa: SLF001


from brokers.rate_limiter import CLASS_BULK, broker_class  # noqa: E402
from api.routers.auth import get_active_adapter  # noqa: E402
from db.database import AsyncSessionLocal  # noqa: E402
from market_data.atm_watchlist import ATMWatchlistService  # noqa: E402


def _d(v: str) -> date:
    return datetime.strptime(v, "%Y-%m-%d").date()


async def _amain(a) -> int:
    if get_active_adapter("upstox") is None:
        await auth.ensure_upstox_session(force_validate=True)
    adapter = get_active_adapter("upstox")
    if adapter is None:
        print("NO UPSTOX SESSION — aborting (nothing written).")
        return 2

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "SELECT symbol, kind, underlying_key FROM fo_underlying_catalog ORDER BY symbol"
        ))).fetchall()
    universe = [(r.symbol, r.kind, r.underlying_key) for r in rows
                if r.underlying_key and r.kind.upper() in
                {k.strip().upper() for k in a.kinds.split(",")}]
    if a.only:
        want = {x.strip().upper() for x in a.only.split(",")}
        universe = [u for u in universe if u[0].upper() in want]
    universe = universe[a.skip: a.skip + a.limit if a.limit else None]

    svc = ATMWatchlistService()
    ok = empty = err = 0
    failures: list[str] = []
    t0 = time.monotonic()
    for n, (sym, _kind, key) in enumerate(universe, 1):
        try:
            with broker_class(CLASS_BULK):
                contracts = await adapter.get_option_contracts(key, a.expiry.isoformat())
            normalized = [
                {
                    "instrument_key": r.get("instrument_key"),
                    "trading_symbol": r.get("trading_symbol"),
                    "strike_price": float(r.get("strike_price", 0) or 0.0),
                    "instrument_type": r.get("instrument_type"),
                    "expiry": r.get("expiry"),
                    "lot_size": r.get("lot_size"),
                }
                for r in (contracts or [])
                if r.get("instrument_key") and r.get("instrument_type") in {"CE", "PE"}
            ]
            if normalized:
                await svc._persist_contracts_for_expiry(sym, normalized)  # noqa: SLF001
                ok += 1
            else:
                empty += 1
                failures.append(f"{sym}: no CE/PE contracts returned")
        except Exception as exc:  # noqa: BLE001 — record and continue
            err += 1
            failures.append(f"{sym}: {type(exc).__name__}: {exc}")
        if n % 25 == 0:
            print(f"  ... {n}/{len(universe)} ok={ok} empty={empty} err={err} "
                  f"elapsed={time.monotonic() - t0:.0f}s", flush=True)

    async with AsyncSessionLocal() as s:
        row = (await s.execute(text(
            "SELECT count(*) n, count(DISTINCT underlying) u FROM fo_contract_catalog "
            "WHERE expiry = :e"
        ), {"e": a.expiry})).one()
    from brokers.rate_limiter import UPSTOX_DATA_LIMITER
    print(f"DONE expiry={a.expiry} names={len(universe)} ok={ok} empty={empty} err={err} "
          f"wall={time.monotonic() - t0:.0f}s")
    print(f"CATALOG NOW: rows={row.n} underlyings={row.u}")
    print("UPSTOX_LIMITER", UPSTOX_DATA_LIMITER.snapshot())
    print(f"FAILURES={len(failures)}")
    for f in failures[:60]:
        print("  FAIL", f)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--expiry", type=_d, required=True)
    p.add_argument("--kinds", default="STOCK,INDEX")
    p.add_argument("--only", default=None)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--skip", type=int, default=0)
    return asyncio.run(_amain(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
