"""Drive the existing spot gap-fill paths off-line (indices + MCX roots).

Wraps the two runtime reconciliation entry points that already know how to
resolve symbols and persist into underlying_spot_candles:

  * MarketIntelligenceRuntime.gap_fill_spot_history(force=True)  — NSE indices
  * commodity_runtime_history.load_commodity_history_rows(persist=True) — MCX

Both already run under CLASS_BULK internally. Used to repair the 2026-07-16
NIFTY/MIDCPNIFTY/FINNIFTY 1-minute swiss-cheese and the 2026-07-17 restart-window
holes on the MCX roots.

    docker exec nomadcurie_backend python scripts/backfill_spot_gaps.py \
        --indices --commodities --lookback-days 6
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from db.database import AsyncSessionLocal  # noqa: E402

INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")
# Canonical 1-minute index keyspace (source='timescaledb_spot_1minute') — these
# ARE Fyers history symbols, so a direct /history pull upserts onto the same
# (instrument_key, interval, time) rows the live lane writes.
INDEX_FYERS = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:BANKNIFTY-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}
# Fyers /history rejects "NSE:BANKNIFTY-INDEX" (422 Invalid symbol) even though
# that IS the key the live 1-minute lane persists under. Fetch with the symbol
# Fyers accepts, store under the canonical key so the rows merge.
INDEX_FETCH_OVERRIDE = {"BANKNIFTY": "NSE:NIFTYBANK-INDEX"}
MCX_ROOTS = ("GOLD", "SILVER", "CRUDEOIL", "NATURALGAS", "COPPER", "ZINC", "NICKEL", "ALUMINIUM")


async def _count(underlyings: tuple[str, ...], interval: str) -> int:
    async with AsyncSessionLocal() as s:
        row = (await s.execute(text(
            """
            SELECT count(*) AS n FROM underlying_spot_candles
            WHERE interval = :i AND time >= '2026-07-15' AND time < '2026-07-18'
              AND underlying = ANY(:u)
            """
        ), {"i": interval, "u": list(underlyings)})).one()
    return int(row.n)


async def _amain(a: argparse.Namespace) -> int:
    if a.indices:
        from market_data.market_intelligence_runtime import MarketIntelligenceRuntime

        before = await _count(INDICES, "1minute")
        rt = MarketIntelligenceRuntime()
        res = await rt.gap_fill_spot_history(
            symbols=list(INDICES), lookback_days=a.lookback_days, force=True
        )
        after = await _count(INDICES, "1minute")
        print(f"[indices 1minute] stored_total={res.get('stored_total')} "
              f"db 07-15..07-17 {before} -> {after} (+{after - before})")
        for r in res.get("results", []):
            print(f"   {r.get('symbol_code'):<12} source={r.get('source')} "
                  f"seen={r.get('rows_seen')} stored={r.get('rows_stored')} "
                  f"{('ERROR: ' + str(r.get('error'))) if r.get('error') else ''}")

    if a.index_history:
        from api.routers.auth import ensure_fyers_session, get_active_adapter
        from brokers.rate_limiter import CLASS_BULK, broker_class
        from datetime import datetime as _dt

        adapter = get_active_adapter("fyers")
        if adapter is None:
            await ensure_fyers_session(force_validate=False)
            adapter = get_active_adapter("fyers")
        if adapter is None:
            print("[index history] no Fyers adapter — skipped")
        else:
            before = await _count(INDICES, "1minute")
            for sym, fy in INDEX_FYERS.items():
                try:
                    with broker_class(CLASS_BULK):
                        raw = await adapter.get_historical_candles(
                            symbol=INDEX_FETCH_OVERRIDE.get(sym, fy), resolution="1",
                            range_from=a.from_date, range_to=a.to_date,
                        )
                    payload = []
                    for r in raw or []:
                        try:
                            payload.append({
                                "time": _dt.fromisoformat(str(r["time"]).replace("Z", "+00:00")),
                                "open": float(r["open"]), "high": float(r["high"]),
                                "low": float(r["low"]), "close": float(r["close"]),
                                "volume": int(r.get("volume") or 0),
                            })
                        except (TypeError, ValueError, KeyError):
                            continue
                    async with AsyncSessionLocal() as s2:
                        if payload:
                            await s2.execute(text(
                                """
                                INSERT INTO underlying_spot_candles
                                    (time, instrument_key, underlying, interval,
                                     open, high, low, close, volume, oi, source)
                                VALUES (:time, :ik, :u, '1minute',
                                        :open, :high, :low, :close, :volume, 0, 'fyers')
                                ON CONFLICT (instrument_key, interval, "time") DO NOTHING
                                """
                            ), [{**r, "ik": fy, "u": sym} for r in payload])
                            await s2.commit()
                    print(f"   {sym:<12} {fy:<24} broker_rows={len(payload)}")
                except Exception as exc:
                    print(f"   {sym:<12} ERROR {type(exc).__name__}: {exc}")
            after = await _count(INDICES, "1minute")
            print(f"[index 1minute history] db 07-15..07-17 {before} -> {after} (+{after - before})")

    if a.mcx_history:
        # load_commodity_history_rows persists INCREMENTALLY (only rows newer than
        # the highest already-stored timestamp), so it can extend a tape but can
        # never repair an intraday hole. Pull the same Fyers contract symbols the
        # commodity lane resolved and upsert hole-first.
        from api.routers.auth import ensure_fyers_session, get_active_adapter
        from brokers.rate_limiter import CLASS_BULK, broker_class
        from datetime import datetime as _dt

        adapter = get_active_adapter("fyers")
        if adapter is None:
            await ensure_fyers_session(force_validate=False)
            adapter = get_active_adapter("fyers")
        if adapter is None:
            print("[mcx history] no Fyers adapter — skipped")
        else:
            async with AsyncSessionLocal() as s0:
                contracts = (await s0.execute(text(
                    """
                    SELECT DISTINCT underlying, instrument_key
                    FROM underlying_spot_candles
                    WHERE interval = '1minute' AND source = 'commodity_broker_history'
                      AND time >= :frm AND time < :to
                    ORDER BY underlying
                    """
                ), {"frm": _dt.strptime(a.from_date, "%Y-%m-%d").date(),
     "to": _dt.strptime(a.to_date, "%Y-%m-%d").date() + timedelta(days=1)})).fetchall()
            before = await _count(MCX_ROOTS, "1minute")
            for underlying, key in contracts:
                try:
                    with broker_class(CLASS_BULK):
                        raw = await adapter.get_historical_candles(
                            symbol=key, resolution="1",
                            range_from=a.from_date, range_to=a.to_date,
                        )
                    payload = []
                    for r in raw or []:
                        try:
                            payload.append({
                                "time": _dt.fromisoformat(str(r["time"]).replace("Z", "+00:00")),
                                "open": float(r["open"]), "high": float(r["high"]),
                                "low": float(r["low"]), "close": float(r["close"]),
                                "volume": int(r.get("volume") or 0),
                            })
                        except (TypeError, ValueError, KeyError):
                            continue
                    if payload:
                        async with AsyncSessionLocal() as s2:
                            await s2.execute(text(
                                """
                                INSERT INTO underlying_spot_candles
                                    (time, instrument_key, underlying, interval,
                                     open, high, low, close, volume, oi, source)
                                VALUES (:time, :ik, :u, '1minute',
                                        :open, :high, :low, :close, :volume, 0,
                                        'commodity_broker_history')
                                ON CONFLICT (instrument_key, interval, "time") DO NOTHING
                                """
                            ), [{**r, "ik": key, "u": underlying} for r in payload])
                            await s2.commit()
                    print(f"   {underlying:<12} {key:<28} broker_rows={len(payload)}")
                except Exception as exc:
                    print(f"   {underlying:<12} {key:<28} ERROR {type(exc).__name__}: {exc}")
            after = await _count(MCX_ROOTS, "1minute")
            print(f"[mcx 1minute history] db 07-15..07-17 {before} -> {after} (+{after - before})")

    if a.commodities:
        from market_data.commodity_runtime_history import load_commodity_history_rows

        before = await _count(MCX_ROOTS, "1minute")
        for root in MCX_ROOTS:
            try:
                rows, symbol = await load_commodity_history_rows(
                    root, interval="1minute", lookback_days=a.lookback_days, persist=True
                )
                print(f"   {root:<12} symbol={symbol} rows={len(rows)}")
            except Exception as exc:  # one root never aborts the run
                print(f"   {root:<12} ERROR {type(exc).__name__}: {exc}")
        await asyncio.sleep(a.persist_wait)  # persistence is fire-and-forget
        after = await _count(MCX_ROOTS, "1minute")
        print(f"[mcx 1minute] db 07-15..07-17 {before} -> {after} (+{after - before})")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--indices", action="store_true")
    p.add_argument("--commodities", action="store_true")
    p.add_argument("--mcx-history", action="store_true", dest="mcx_history",
                   help="direct Fyers /history pull onto the resolved MCX contract keyspace")
    p.add_argument("--index-history", action="store_true", dest="index_history",
                   help="direct Fyers /history pull onto the canonical index 1m keyspace")
    p.add_argument("--from", dest="from_date", default="2026-07-14")
    p.add_argument("--to", dest="to_date", default="2026-07-17")
    p.add_argument("--lookback-days", type=int, default=6, dest="lookback_days")
    p.add_argument("--persist-wait", type=float, default=45.0, dest="persist_wait")
    return asyncio.run(_amain(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
