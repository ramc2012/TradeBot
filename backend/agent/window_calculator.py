"""Trading window computation using the physical-delivery constraint.

Indian stock options trigger additional margins if held past expiry week.
Trading window = (previous_expiry − 7 days) to (current_expiry − 7 days).
This gives a ~4-week window per expiry cycle.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import asyncpg
from loguru import logger

from agent.strategy_config import WINDOW_BUFFER_DAYS
from core.config import settings


async def _get_connection() -> asyncpg.Connection:
    return await asyncpg.connect(str(settings.DATABASE_URL).replace("+asyncpg", ""))


async def get_trading_windows(
    underlying: str,
    as_of: Optional[date] = None,
) -> list[dict]:
    """Return all valid trading windows for *underlying* from fo_expiry_catalog.

    Each window dict has keys:
        underlying, expiry, prev_expiry, window_start, window_end
    """
    today = as_of or date.today()
    conn = await _get_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT underlying, expiry, previous_monthly_expiry
            FROM fo_expiry_catalog
            WHERE underlying = $1
              AND expiry >= $2
              AND previous_monthly_expiry IS NOT NULL
            ORDER BY expiry
            """,
            underlying,
            today - timedelta(days=60),
        )
    finally:
        await conn.close()

    windows = []
    for r in rows:
        expiry: date = r["expiry"]
        prev_expiry: date = r["previous_monthly_expiry"]
        window_start = prev_expiry - timedelta(days=WINDOW_BUFFER_DAYS)
        window_end = expiry - timedelta(days=WINDOW_BUFFER_DAYS)
        windows.append(
            {
                "underlying": underlying,
                "expiry": expiry,
                "prev_expiry": prev_expiry,
                "window_start": window_start,
                "window_end": window_end,
            }
        )
    return windows


async def get_active_window(
    underlying: str,
    as_of: Optional[date] = None,
) -> Optional[dict]:
    """Return the currently active trading window for *underlying*.

    Active = window where ``window_start <= today <= window_end``.
    If between windows, returns the next upcoming window.
    """
    today = as_of or date.today()
    windows = await get_trading_windows(underlying, as_of=today)

    for w in windows:
        if w["window_start"] <= today <= w["window_end"]:
            return w

    # No active window — return the next upcoming one (if any)
    for w in windows:
        if w["window_start"] > today:
            return w

    return None


async def get_all_active_windows(
    as_of: Optional[date] = None,
) -> list[dict]:
    """Return active trading windows for ALL underlyings in the F&O universe.

    Used by the scanner to know which underlyings are currently tradeable.
    """
    today = as_of or date.today()
    conn = await _get_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT underlying, expiry, previous_monthly_expiry
            FROM fo_expiry_catalog
            WHERE previous_monthly_expiry IS NOT NULL
              AND (previous_monthly_expiry - $1) <= expiry
              AND expiry >= $2
            ORDER BY underlying, expiry
            """,
            WINDOW_BUFFER_DAYS,
            today,
        )
    finally:
        await conn.close()

    windows = []
    seen = set()
    for r in rows:
        expiry: date = r["expiry"]
        prev_expiry: date = r["previous_monthly_expiry"]
        und: str = r["underlying"]
        window_start = prev_expiry - timedelta(days=WINDOW_BUFFER_DAYS)
        window_end = expiry - timedelta(days=WINDOW_BUFFER_DAYS)

        if und in seen:
            continue

        if window_start <= today <= window_end:
            seen.add(und)
            windows.append(
                {
                    "underlying": und,
                    "expiry": expiry,
                    "prev_expiry": prev_expiry,
                    "window_start": window_start,
                    "window_end": window_end,
                }
            )

    return windows


def days_remaining_in_window(window: dict, as_of: Optional[date] = None) -> int:
    """Calendar days left until window_end."""
    today = as_of or date.today()
    return max(0, (window["window_end"] - today).days)


def is_within_window(window: dict, as_of: Optional[date] = None) -> bool:
    today = as_of or date.today()
    return window["window_start"] <= today <= window["window_end"]
