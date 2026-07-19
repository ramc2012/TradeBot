"""Quarantine cross-symbol-contaminated STOCK spot bars.

Companion to ``quarantine_corrupt_index_bars.py``, which is index-only: it keys
off ``index_band_guard._UNDERLYING_TO_APP`` and a static per-index absolute band,
neither of which exists for the ~209 F&O stocks. This script is the stock
equivalent.

Why stocks needed their own quarantine
--------------------------------------
``fo_underlying_catalog`` mapped BOTH ``M&M`` and ``MARUTI`` to
``NSE_EQ|INE585B01010`` — MARUTI's ISIN (M&M's real ISIN is ``INE101A01026``).
``underlying_spot_candles`` is keyed ``(instrument_key, interval, time)`` and the
``underlying`` column is only a label, so the two names OVERWRITE each other
bar-for-bar: last writer wins the row and relabels it. Measured on 2026-07-17
under that key: MARUTI 1minute had 313 of 351 rows out of band (closes
139.65 … 13,824 against a ~13,800 underlying); M&M 1minute 60 of 66
(23.50 … 24,180). It is also why stock 30m coverage always read 210 of 211 —
exactly one of the colliding pair could own a clean grid on any given day.

Why delete, not clamp
---------------------
Same reasoning as the index script. Whole FOREIGN frames were written under this
key, so a poisoned row's open/high/low/close can ALL be foreign. Reconstructing
a leg (e.g. ``high = max(open, close)``) would fabricate a price that never
traded. A DELETE is honest and fully reversible via broker backfill; a clamp is
neither. Charts and aggregation already skip gaps.

The reference band is EXTERNAL
------------------------------
The band must not be derived from the table being cleaned. When 89% of a
symbol-day's rows are foreign, any self-referential statistic (mean, or even a
median) is dragged by the contamination and the guard INVERTS — it would keep
the poison and delete the ~38 good rows. So the per-symbol-per-day reference is
fetched fresh from the broker's DAILY candles (Fyers ``resolution=D``), which
are a different endpoint and were never touched by the collision. A row is a
candidate iff ANY of its O/H/L/C legs falls outside ``±--tolerance`` (default
20%) of that day's true daily range. 20% is generous — a stock beyond ±20%
intraday would have tripped its circuit filter — so a legitimate bar cannot be
false-flagged, while a foreign instrument's level (2x+ away, or 100x for the
23.50 / 139.65 prints) always is.

Days with no broker daily candle (holiday, or a symbol the broker cannot serve)
are SKIPPED, never guessed at.

Safety
------
* Market-closed operation; no writer races these rows.
* Every candidate row is logged (symbol/interval/time + O/H/L/C + the band) before deletion.
* ``--dry-run`` is the DEFAULT and deletes nothing. Pass ``--apply`` to delete.
* Bounded: explicit ``--symbols`` and an explicit ``--from``/``--to`` window are
  both REQUIRED. There is no "scan everything" mode.
* Idempotent — a second run finds nothing.
* Deletes by exact ``(instrument_key, interval, time)`` primary key.

Usage
-----
    # dry-run (default) — prints candidates, deletes nothing
    docker exec nomadcurie_backend python scripts/quarantine_corrupt_stock_bars.py \
        --symbols M\&M MARUTI --from 2026-07-15 --to 2026-07-17

    # actually delete
    docker exec nomadcurie_backend python scripts/quarantine_corrupt_stock_bars.py \
        --symbols M\&M MARUTI --from 2026-07-15 --to 2026-07-17 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from datetime import date, datetime, timedelta

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import text  # noqa: E402

from api.routers.auth import ensure_fyers_session, get_active_adapter  # noqa: E402
from brokers.rate_limiter import CLASS_BULK, broker_class  # noqa: E402
from db.database import AsyncSessionLocal  # noqa: E402

# Native timeframes the chart serves + the 1minute base they aggregate from.
_INTERVALS = ("1minute", "3minute", "5minute", "15minute", "30minute", "60minute")

DEFAULT_TOLERANCE = 0.20


def _parse_date(v: str) -> date:
    return datetime.strptime(v, "%Y-%m-%d").date()


async def _catalog_key(symbol: str) -> str | None:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            """
            SELECT spot_instrument_key
            FROM fo_underlying_catalog
            WHERE symbol = :symbol
            """
        ), {"symbol": symbol})).fetchone()
    if row is None:
        return None
    key = str(row.spot_instrument_key or "").strip()
    return key or None


def _fyers_symbol(symbol: str, key: str) -> str:
    return key if key.startswith(("NSE:", "BSE:")) else f"NSE:{symbol}-EQ"


async def _daily_reference(adapter, symbol: str, key: str, frm: date, to: date) -> dict[date, tuple[float, float]]:
    """Per-day (low, high) truth from the broker's DAILY candles — external anchor."""
    with broker_class(CLASS_BULK):
        raw = await adapter.get_historical_candles(
            symbol=_fyers_symbol(symbol, key),
            resolution="D",
            range_from=frm.isoformat(),
            range_to=to.isoformat(),
        )
    out: dict[date, tuple[float, float]] = {}
    for r in raw or []:
        try:
            day = datetime.fromisoformat(str(r["time"]).replace("Z", "+00:00")).date()
            out[day] = (float(r["low"]), float(r["high"]))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _out_of_band(legs, lo: float, hi: float) -> bool:
    for leg in legs:
        if leg is None:
            continue
        try:
            value = float(leg)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value < lo or value > hi:
            return True
    return False


async def _quarantine(a: argparse.Namespace) -> int:
    adapter = get_active_adapter("fyers")
    if adapter is None:
        if not await ensure_fyers_session(force_validate=False):
            raise SystemExit("No live Fyers session — cannot build an external reference band")
        adapter = get_active_adapter("fyers")
    if adapter is None:
        raise SystemExit("Fyers adapter unavailable")

    mode = "APPLY (deleting)" if a.apply else "DRY-RUN (no deletes)"
    print(f"[quarantine-stock] {mode} | symbols={a.symbols} | "
          f"{a.from_date}..{a.to_date} | tolerance=±{a.tolerance:.0%}")

    total_candidates = 0
    total_deleted = 0

    for symbol in a.symbols:
        key = await _catalog_key(symbol)
        if not key:
            print(f"[quarantine-stock] SKIP {symbol}: no catalog spot_instrument_key")
            continue

        reference = await _daily_reference(adapter, symbol, key, a.from_date, a.to_date)
        if not reference:
            print(f"[quarantine-stock] SKIP {symbol}: broker returned no daily reference candles")
            continue
        print(f"[quarantine-stock] {symbol} key={key} reference days={len(reference)}")
        for day, (lo, hi) in sorted(reference.items()):
            print(f"    ref {day}: daily low={lo} high={hi} "
                  f"=> band [{lo * (1 - a.tolerance):.2f}, {hi * (1 + a.tolerance):.2f}]")

        for interval in _INTERVALS:
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(text(
                    """
                    SELECT time, underlying, source, open, high, low, close
                    FROM underlying_spot_candles
                    WHERE instrument_key = :key
                      AND interval = :interval
                      AND time >= :frm
                      AND time < :to
                    ORDER BY time ASC
                    """
                ), {
                    "key": key,
                    "interval": interval,
                    "frm": a.from_date,
                    "to": a.to_date + timedelta(days=1),
                })).fetchall()

                bad_times = []
                for row in rows:
                    r = dict(row._mapping)
                    band = reference.get(r["time"].date())
                    if band is None:
                        continue  # no external truth for this day — never guess
                    lo = band[0] * (1 - a.tolerance)
                    hi = band[1] * (1 + a.tolerance)
                    legs = (r.get("open"), r.get("high"), r.get("low"), r.get("close"))
                    if _out_of_band(legs, lo, hi):
                        bad_times.append(r["time"])
                        print(
                            f"[quarantine-stock] CANDIDATE {symbol}/{interval} "
                            f"t={r['time']} label={r['underlying']} src={r['source']} "
                            f"o={r.get('open')} h={r.get('high')} l={r.get('low')} "
                            f"c={r.get('close')} | band=[{lo:.2f},{hi:.2f}]"
                        )

                if not bad_times:
                    continue
                total_candidates += len(bad_times)
                print(f"[quarantine-stock] {symbol}/{interval}: "
                      f"{len(bad_times)} of {len(rows)} rows out of band")

                if a.apply:
                    result = await session.execute(text(
                        """
                        DELETE FROM underlying_spot_candles
                        WHERE instrument_key = :key
                          AND interval = :interval
                          AND time = ANY(:times)
                        """
                    ), {"key": key, "interval": interval, "times": bad_times})
                    await session.commit()
                    total_deleted += int(result.rowcount or 0)

    print(f"[quarantine-stock] candidates={total_candidates} deleted={total_deleted}")
    if not a.apply and total_candidates:
        print("[quarantine-stock] DRY-RUN: nothing deleted. Re-run with --apply.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", nargs="+", required=True,
                   help="Catalog symbols to scan (REQUIRED — there is no scan-all mode)")
    p.add_argument("--from", dest="from_date", type=_parse_date, required=True)
    p.add_argument("--to", dest="to_date", type=_parse_date, required=True)
    p.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                   help="Fractional slack around the broker daily low/high (default 0.20)")
    p.add_argument("--apply", action="store_true",
                   help="Perform the DELETE. Omit for a dry-run (the default).")
    a = p.parse_args()
    if a.to_date < a.from_date:
        raise SystemExit("--to must not precede --from")
    return asyncio.run(_quarantine(a))


if __name__ == "__main__":
    raise SystemExit(main())
