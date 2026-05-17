"""Trading window computation using the physical-delivery constraint.

Indian stock options trigger additional margins if held past expiry week.
Trading window = (previous_expiry − 7 days) to (current_expiry − 7 days).
This gives a ~4-week window per expiry cycle.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import text

from analysis.instruments import get_index_monthly_expiry, get_monthly_expiry
from agent.strategy_config import WINDOW_BUFFER_DAYS
from db.database import AsyncSessionLocal


async def _fetch_expiry_rows(query: str, params: dict[str, object]) -> list[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(query), params)
        return [dict(row._mapping) for row in result.fetchall()]


async def _fetch_underlying_rows(query: str, params: dict[str, object]) -> list[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(query), params)
        return [dict(row._mapping) for row in result.fetchall()]


def _shift_months(year: int, month: int, delta: int) -> tuple[int, int]:
    absolute = (year * 12) + (month - 1) + delta
    return absolute // 12, (absolute % 12) + 1


def _strategy1_monthly_expiry(underlying: str, kind: str, year: int, month: int) -> date:
    if kind == "INDEX":
        return get_index_monthly_expiry(underlying, year, month)
    return get_monthly_expiry(year, month)


def _generic_strategy1_scan_window(underlying: str, kind: str, today: date) -> dict:
    candidates: list[dict] = []
    for offset in range(0, 3):
        year, month = _shift_months(today.year, today.month, offset)
        prev_year, prev_month = _shift_months(year, month, -1)
        expiry = _strategy1_monthly_expiry(underlying, kind, year, month)
        prev_expiry = _strategy1_monthly_expiry(underlying, kind, prev_year, prev_month)
        window_start = prev_expiry - timedelta(days=WINDOW_BUFFER_DAYS)
        window_end = expiry - timedelta(days=WINDOW_BUFFER_DAYS)
        state = "future"
        if window_start <= today <= window_end:
            state = "active"
        elif today > window_end:
            state = "past"
        candidates.append(
            {
                "underlying": underlying,
                "expiry": expiry,
                "prev_expiry": prev_expiry,
                "window_start": window_start,
                "window_end": window_end,
                "window_state": state,
            }
        )

    for window in candidates:
        if window["window_state"] == "active":
            return window
    for window in candidates:
        if window["window_state"] == "future":
            return window
    return candidates[-1]


async def get_trading_windows(
    underlying: str,
    as_of: Optional[date] = None,
) -> list[dict]:
    """Return all valid trading windows for *underlying* from fo_expiry_catalog.

    Each window dict has keys:
        underlying, expiry, prev_expiry, window_start, window_end
    """
    today = as_of or date.today()
    rows = await _fetch_expiry_rows(
        """
        SELECT underlying, expiry, previous_monthly_expiry
        FROM fo_expiry_catalog
        WHERE underlying = :underlying
          AND expiry >= :min_expiry
          AND previous_monthly_expiry IS NOT NULL
        ORDER BY expiry
        """,
        {"underlying": underlying, "min_expiry": today - timedelta(days=60)},
    )

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


# Index window end buffer — indices are cash-settled so we only step out the
# day before expiry (so the agent does not enter on settlement day). Stocks
# use WINDOW_BUFFER_DAYS (7) because of compulsory physical delivery margins.
INDEX_WINDOW_END_BUFFER_DAYS = 1


def _stock_window_for_month(today: date, year: int, month: int) -> dict:
    """Compute the stock trading window arithmetically for one expiry cycle.

    No DB lookup. `prev_expiry` is the last Tuesday of the prior calendar
    month — a pure calendar fact, independent of whether prior contract
    data has been ingested.
    """
    expiry = get_monthly_expiry(year, month)
    prev_year, prev_month = _shift_months(year, month, -1)
    prev_expiry = get_monthly_expiry(prev_year, prev_month)
    return {
        "expiry": expiry,
        "prev_expiry": prev_expiry,
        "window_start": prev_expiry - timedelta(days=WINDOW_BUFFER_DAYS),
        "window_end": expiry - timedelta(days=WINDOW_BUFFER_DAYS),
    }


def _index_window_for_month(symbol: str, today: date, year: int, month: int) -> dict:
    """Compute the index trading window arithmetically for one expiry cycle.

    Indices are cash-settled — the whole month is tradable. Window starts
    the day after the prior month's expiry (when the new contract becomes
    the front month) and ends one day before expiry.
    """
    expiry = get_index_monthly_expiry(symbol, year, month)
    prev_year, prev_month = _shift_months(year, month, -1)
    prev_expiry = get_index_monthly_expiry(symbol, prev_year, prev_month)
    return {
        "expiry": expiry,
        "prev_expiry": prev_expiry,
        # Day AFTER the prior expiry — the front-month role rolls forward.
        "window_start": prev_expiry + timedelta(days=1),
        "window_end": expiry - timedelta(days=INDEX_WINDOW_END_BUFFER_DAYS),
    }


def _pick_active_window(underlying: str, kind: str, today: date) -> Optional[dict]:
    """Pick the active window for one underlying using arithmetic dates.

    Scans the current month and the next two months and returns the one
    that contains `today`. No DB round-trip required.
    """
    is_index = (kind or "").upper() == "INDEX"
    for offset in range(0, 3):
        year, month = _shift_months(today.year, today.month, offset)
        if is_index:
            w = _index_window_for_month(underlying, today, year, month)
        else:
            w = _stock_window_for_month(today, year, month)
        if w["window_start"] <= today <= w["window_end"]:
            return {
                "underlying": underlying,
                "kind": "INDEX" if is_index else "STOCK",
                **w,
            }
    return None


async def get_all_active_windows(
    as_of: Optional[date] = None,
) -> list[dict]:
    """Return active trading windows for ALL F&O underlyings.

    Windows are computed **arithmetically** from the calendar — they do
    not depend on prior contract data being in `fo_expiry_catalog`. Rules:

      * **Index** (cash-settled): window = (prev_monthly_expiry + 1) →
        (current_monthly_expiry − 1). Entire month is tradable.
      * **Stock** (compulsory physical delivery): window =
        (prev_monthly_expiry − 7) → (current_monthly_expiry − 7), per the
        delivery-margin constraint.

    Only `fo_underlying_catalog` is consulted (to learn each symbol's
    kind). The window math itself is calendar-driven so adding April 2026
    catalog rows is not required to unblock May 2026 trading.
    """
    today = as_of or date.today()
    rows = await _fetch_underlying_rows(
        """
        SELECT symbol, kind
        FROM fo_underlying_catalog
        WHERE spot_instrument_key IS NOT NULL
          AND underlying_key IS NOT NULL
        ORDER BY CASE WHEN kind = 'INDEX' THEN 0 ELSE 1 END, symbol
        """,
        {},
    )

    windows: list[dict] = []
    for row in rows:
        symbol = str(row["symbol"])
        kind = str(row["kind"] or "")
        picked = _pick_active_window(symbol, kind, today)
        if picked is not None:
            windows.append(picked)
    return windows


async def get_all_strategy1_scan_windows(
    as_of: Optional[date] = None,
) -> list[dict]:
    """Return one Strategy 1 window per underlying for live scanning.

    Selection rule:
    - keep the currently active window while it is still valid
    - otherwise roll to the next upcoming monthly window

    This preserves the agreed expiry behavior for Strategy 1: do not change
    the contract month early, but do not leave the lane idle once the current
    window is exhausted.
    """
    today = as_of or date.today()
    rows = await _fetch_underlying_rows(
        """
        SELECT symbol, kind
        FROM fo_underlying_catalog
        WHERE spot_instrument_key IS NOT NULL
          AND underlying_key IS NOT NULL
        ORDER BY CASE WHEN kind = 'INDEX' THEN 0 ELSE 1 END, symbol
        """,
        {},
    )

    return [
        _generic_strategy1_scan_window(
            underlying=str(row["symbol"]),
            kind=str(row["kind"]),
            today=today,
        )
        for row in rows
    ]


def days_remaining_in_window(window: dict, as_of: Optional[date] = None) -> int:
    """Calendar days left until window_end."""
    today = as_of or date.today()
    return max(0, (window["window_end"] - today).days)


def trading_days_remaining(window: dict, as_of: Optional[date] = None) -> int:
    """Mon-Fri trading days between today (inclusive) and window_end.

    Used by S1's TTE gate so "TTE ≤ 3 trading days" means three actual
    weekday sessions, not three calendar days. A Friday with expiry
    on the following Monday is therefore TTE=1 trading day, not 3.
    NSE/MCX holidays are not subtracted here — that refinement comes
    later if needed; the practical impact is at most one day per
    quarter, well within the noise of a 3-day threshold.
    """
    today = as_of or date.today()
    window_end = window["window_end"]
    if today >= window_end:
        return 0
    count = 0
    d = today
    while d < window_end:
        d += timedelta(days=1)
        if d.weekday() < 5:  # Monday=0 … Friday=4
            count += 1
    return count


def is_within_window(window: dict, as_of: Optional[date] = None) -> bool:
    today = as_of or date.today()
    return window["window_start"] <= today <= window["window_end"]
