"""Keep option_premium_candles flowing for HELD position legs.

Why this exists (2026-08-03)
----------------------------
Option premium candles were only ever maintained for legs the S1 scan
touches — i.e. the CURRENT ATM watchlist. A position is opened at the ATM
strike, spot then drifts, the watchlist rotates to a new strike, and the
held leg silently stops being collected: no new candles, and (because the
same scan path is what refreshes the stored mark) no new mark either.

Observed 2026-08-03: 16 of 21 open S1 positions were carrying marks up to
6 days old, every one of them at a strike that was no longer ATM
(ONGC held 250 PE while ATM had rotated to 240; TRENT held 2900 vs 3000).
`option_premium_candles` had ZERO rows for those legs' expiry — so the
premium history behind an open risk position simply did not exist, and
the session-gap backfiller could not even discover them (it derives its
universe FROM the candle table).

The owner's standing requirement is that held positions are maintained
for candle data ALONGSIDE the ATM options. This module is that guarantee:
independent of watchlist membership, every open leg gets its premium
series refreshed on a slow loop for as long as the position is open.

Design
------
* Reuses `OptionHistoryService.load_candles(allow_broker_refresh=True)` —
  the same broker-fetch + persist path the scan uses, so stored rows are
  byte-identical in shape and provenance to ATM-collected ones.
* CLASS_BULK: hard-capped at 25% of the broker budget and inadmissible
  while any CRITICAL waiter is queued, so it can never starve live
  decisions or held-position marks.
* Bar-aware skip: a leg whose newest stored bar is inside the current bar
  is left alone, so a steady state costs one cheap DB read per leg.
* Market-hours gated, sequential with a small pace delay, and every leg
  is independently failure-tolerant.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import text

from brokers.rate_limiter import CLASS_BULK, broker_class
from db.database import AsyncSessionLocal

IST = timezone(timedelta(hours=5, minutes=30))

# 30-minute is the S1 decision + exit timeframe; it is the series whose
# absence actually breaks position management.
DEFAULT_INTERVALS: tuple[str, ...] = ("30minute",)
_INTERVAL_MINUTES = {"1minute": 1, "3minute": 3, "5minute": 5, "15minute": 15, "30minute": 30}

# Pace between broker calls — CLASS_BULK already yields, this just keeps the
# pass from arriving as a burst.
PACE_SECONDS = 0.35


def _now_ist() -> datetime:
    return datetime.now(IST)


def _market_hours_now() -> bool:
    now = _now_ist()
    if now.weekday() >= 5:
        return False
    minute_of_day = now.hour * 60 + now.minute
    # 09:10–15:45 IST: slack each side of the NSE DERIVATIVES session, which
    # closes at 15:40 under the 2026-08-03 regime (was 15:30), so the closing
    # option bars are captured.
    return 9 * 60 + 10 <= minute_of_day <= 15 * 60 + 45


async def _newest_bar(underlying: str, expiry: date, strike: float, option_type: str, interval: str) -> datetime | None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT max(time) AS t
                    FROM option_premium_candles
                    WHERE underlying = :u AND expiry = :e AND strike = :s
                      AND option_type = :ot AND interval = :i
                    """
                ),
                {"u": underlying, "e": expiry, "s": strike, "ot": option_type, "i": interval},
            )
        ).first()
    t = row[0] if row is not None else None
    if not isinstance(t, datetime):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _needs_refresh(newest: datetime | None, interval: str) -> bool:
    """True when the stored series is missing or older than one full bar."""
    if newest is None:
        return True
    minutes = _INTERVAL_MINUTES.get(interval, 30)
    return (datetime.now(timezone.utc) - newest) > timedelta(minutes=minutes)


async def refresh_held_position_candles(
    *,
    intervals: tuple[str, ...] = DEFAULT_INTERVALS,
    limit: int = 80,
) -> dict[str, Any]:
    """One pass: refresh the premium series of every open NSE option leg."""
    from market_data.option_history import option_history_service
    from market_data.option_subscription_manager import _open_nse_option_positions

    positions = _open_nse_option_positions()
    checked = 0
    refreshed = 0
    skipped_fresh = 0
    failures: dict[str, str] = {}
    seen: set[tuple[str, str, float, str, str]] = set()

    for pos in positions:
        underlying = str(getattr(pos, "underlying", "") or "").upper()
        expiry_raw = str(getattr(pos, "expiry", "") or "").strip()
        option_type = str(getattr(pos, "option_type", "") or "").upper()
        strike = getattr(pos, "strike", None)
        instrument_key = getattr(pos, "instrument_key", None)
        if not (underlying and expiry_raw and option_type in ("CE", "PE") and strike):
            continue
        try:
            expiry = date.fromisoformat(expiry_raw[:10])
            strike_f = float(strike)
        except (TypeError, ValueError):
            continue

        for interval in intervals:
            key = (underlying, expiry.isoformat(), strike_f, option_type, interval)
            if key in seen:  # the same leg can be held by both runtimes
                continue
            seen.add(key)
            checked += 1
            label = f"{underlying} {strike_f:g} {option_type} {interval}"
            try:
                newest = await _newest_bar(underlying, expiry, strike_f, option_type, interval)
                if not _needs_refresh(newest, interval):
                    skipped_fresh += 1
                    continue
                # CLASS_BULK: reconciliation, never allowed to outrank a live
                # decision or a held-position mark.
                with broker_class(CLASS_BULK):
                    await option_history_service.load_candles(
                        underlying=underlying,
                        expiry=expiry,
                        strike=strike_f,
                        option_type=option_type,
                        instrument_key=instrument_key,
                        interval=interval,
                        limit=limit,
                        allow_broker_refresh=True,
                    )
                refreshed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad leg never aborts the pass
                failures[label] = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(PACE_SECONDS)

    summary = {
        "held_legs": len(seen),
        "checked": checked,
        "refreshed": refreshed,
        "skipped_fresh": skipped_fresh,
        "failure_count": len(failures),
        "failures": failures,
    }
    if refreshed or failures:
        logger.info(
            f"[HeldCandles] refreshed={refreshed} fresh={skipped_fresh} "
            f"legs={len(seen)} failures={len(failures)}"
        )
    if failures:
        logger.warning(f"[HeldCandles] failures: {list(failures.items())[:5]}")
    return summary


async def run_held_position_candle_loop(interval_seconds: float = 300.0) -> None:
    """Maintain held-leg premium candles through the session.

    5-minute cadence: 30m bars only close twice an hour, and the bar-aware
    skip makes a steady-state pass nearly free, so this is about closing the
    gap promptly after each bar rather than polling hard.
    """
    logger.info(f"[HeldCandles] daemon starting (poll={interval_seconds}s, intervals={DEFAULT_INTERVALS})")
    while True:
        try:
            if _market_hours_now():
                await refresh_held_position_candles()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[HeldCandles] pass failed: {exc}")
        await asyncio.sleep(max(60.0, interval_seconds))
