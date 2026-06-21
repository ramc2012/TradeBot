"""Populate ACTIVE option-contract history from Fyers.

Upstox's expired-instruments backfill only covers EXPIRED contracts, leaving the
current month (e.g. the June 2026-06-30 expiry) — which is still active — without
intraday history. Fyers /history serves active option contracts, so this pulls them.

Approach (no symbol guessing): take the verified Fyers option symbols already
captured live in atm_option_watchlist_snapshots for the target expiry, derive each
index's exact Fyers prefix (e.g. "NSE:NIFTY26JUN"), widen to the ATM±BAND strike
grid, fetch 30m + 1m candles from Fyers, and store to option_premium_candles with
greeks (computed from the DB spot — Fyers doesn't stream option greeks).

Usage (inside the backend container):
    python -m tools.populate_active_options_fyers 2026-06-30
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import date, timedelta

from sqlalchemy import text

from analysis.instruments import STRIKE_STEPS, get_atm_strike
from api.routers.auth import (ensure_fyers_session, get_active_adapter,
                              refresh_persistent_credentials_async)
from data.historical_backfill import (_db_spot_near, fetch_fyers_candles,
                                      store_option_rows)
from db.database import AsyncSessionLocal

BAND = 10
INTERVALS = ("30minute", "1minute")
LIFE_DAYS = 60  # how far back to pull each contract (its active life)


async def _captured_prefixes(expiry: date) -> dict[str, tuple[str, str]]:
    """{underlying: (fyers_prefix, market)} from verified captured Fyers symbols."""
    async with AsyncSessionLocal() as s:
        res = await s.execute(text("""
            SELECT DISTINCT underlying, instrument_key, strike, option_type
            FROM atm_option_watchlist_snapshots
            WHERE expiry = :e AND source_broker = 'fyers'
              AND (instrument_key LIKE 'NSE:%' OR instrument_key LIKE 'BSE:%')
        """), {"e": expiry})
        rows = res.fetchall()
    out: dict[str, tuple[str, str]] = {}
    for r in rows:
        m = re.match(r"^(?P<prefix>.+?)(?P<strike>\d+)(?P<ot>CE|PE)$", r.instrument_key)
        if not m:
            continue
        if abs(float(m.group("strike")) - float(r.strike)) > 0.5:
            continue  # the trailing digits really are the strike — safe prefix
        market = "BSE" if r.instrument_key.startswith("BSE:") else "NSE"
        out.setdefault(r.underlying, (m.group("prefix"), market))
    return out


async def _populate_via_chain(adapter, underlying: str, lookup_symbol: str,
                              market: str, expiry: date, strikecount: int = 12) -> int:
    """Discover the EXACT Fyers option symbols from the live chain (per-option
    `symbol`, auto-centered on ATM) and store their history. No symbol construction —
    works for any index incl. BSE SENSEX. Used when no captured prefix exists."""
    try:
        raw = await adapter._get_data_json(
            "/options-chain-v3", {"symbol": lookup_symbol, "strikecount": str(strikecount)})
    except Exception as exc:
        print(f"  {underlying}: chain fetch failed: {exc}")
        return 0
    chain = (raw.get("data", {}) or {}).get("optionsChain", []) or []
    contracts = [
        (o["symbol"], float(o.get("strike_price") or 0), str(o.get("option_type")).upper())
        for o in chain
        if o.get("symbol") and str(o.get("option_type", "")).upper() in ("CE", "PE")
    ]
    if not contracts:
        print(f"  {underlying}: chain returned no option symbols")
        return 0
    today = date.today()
    start = max(expiry - timedelta(days=LIFE_DAYS), date(2022, 1, 1))
    end = min(today, expiry)
    total = 0
    for sym, strike, otype in contracts:
        meta = {"instrument_key": sym, "trading_symbol": sym, "underlying": underlying,
                "market": market, "expiry": expiry, "strike": strike, "option_type": otype}
        for interval in INTERVALS:
            rows = await fetch_fyers_candles(adapter, sym, interval, start, end, cont_flag=0)
            total += await store_option_rows(meta, interval, rows, source="fyers")
    print(f"  {underlying}: chain-discovered {len(contracts)} contracts → stored {total} rows")
    return total


# Underlyings that need chain-discovery (no captured Fyers prefix), with their
# Fyers index lookup symbol + exchange.
_CHAIN_DISCOVER = {
    "SENSEX": ("BSE:SENSEX-INDEX", "BSE"),
    "BANKEX": ("BSE:BANKEX-INDEX", "BSE"),
}


async def main(expiry: date, underlying: str | None = None) -> None:
    await refresh_persistent_credentials_async(force=True)
    adapter = get_active_adapter("fyers")
    if adapter is None and await ensure_fyers_session(force_validate=True):
        adapter = get_active_adapter("fyers")
    if adapter is None:
        print("No Fyers session — cannot populate.")
        return

    # Targeted single-underlying via chain-discovery (e.g. SENSEX/BANKEX on BSE,
    # which have no captured Fyers symbol to seed a prefix from).
    if underlying:
        und = underlying.upper()
        lookup, market = _CHAIN_DISCOVER.get(und, (None, "NSE"))
        if lookup is None:
            print(f"No chain-discovery lookup configured for {und}.")
            return
        n = await _populate_via_chain(adapter, und, lookup, market, expiry)
        print(f"TOTAL stored ({und}): {n}")
        return

    prefixes = await _captured_prefixes(expiry)
    if not prefixes:
        print(f"No captured Fyers symbols for {expiry} — nothing to seed from.")
        return
    print("Index prefixes:", prefixes)

    today = date.today()
    start = max(expiry - timedelta(days=LIFE_DAYS), date(2022, 1, 1))
    end = min(today, expiry)
    grand = 0
    for und, (prefix, market) in prefixes.items():
        spot = (await _db_spot_near(und, today - timedelta(days=3))
                or await _db_spot_near(und, expiry - timedelta(days=20)))
        if not spot:
            print(f"  {und}: no DB spot — skip")
            continue
        step = STRIKE_STEPS.get(und, 50)
        atm = get_atm_strike(float(spot), step)
        strikes = [int(atm + i * step) for i in range(-BAND, BAND + 1)]
        und_total = 0
        for strike in strikes:
            for otype in ("CE", "PE"):
                sym = f"{prefix}{strike}{otype}"
                meta = {
                    "instrument_key": sym, "trading_symbol": sym, "underlying": und,
                    "market": market, "expiry": expiry, "strike": float(strike),
                    "option_type": otype,
                }
                for interval in INTERVALS:
                    rows = await fetch_fyers_candles(adapter, sym, interval, start, end,
                                                     cont_flag=0)
                    und_total += await store_option_rows(meta, interval, rows, source="fyers")
        grand += und_total
        print(f"  {und}: ATM={atm} band=±{BAND} → stored {und_total} rows")
    print(f"TOTAL stored: {grand}")


if __name__ == "__main__":
    exp = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 6, 30)
    asyncio.run(main(exp))
