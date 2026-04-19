"""F&O instrument metadata: expiry calendars, strike steps, index keys."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import List


# ── NSE/BSE F&O Indices ───────────────────────────────────────────────────────

NSE_FO_INDICES: list[str] = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]
BSE_FO_INDICES: list[str] = ["SENSEX", "BANKEX"]
ALL_FO_INDICES: list[str] = NSE_FO_INDICES + BSE_FO_INDICES

# Per-index expiry weekday (Python weekday: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4)
# NSE equity derivatives currently use Tuesday expiry for index and stock
# contracts. BSE index contracts keep their native weekday schedules.
INDEX_EXPIRY_WEEKDAY: dict[str, int] = {
    "NIFTY":      1,   # Tuesday
    "NIFTYNXT50": 1,   # Tuesday
    "BANKNIFTY":  1,   # Tuesday
    "FINNIFTY":   1,   # Tuesday
    "MIDCPNIFTY": 1,   # Tuesday
    "SENSEX":     4,   # Friday  (BSE)
    "BANKEX":     0,   # Monday  (BSE)
}

# ── Strike step sizes ─────────────────────────────────────────────────────────
# Index options
STRIKE_STEPS: dict[str, int] = {
    # Indices
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 25,
    "NIFTYNXT50": 50,
    "SENSEX": 100,
    "BANKEX": 100,
    # Top F&O stocks (commonly traded)
    "RELIANCE": 20,
    "TCS": 50,
    "INFY": 20,
    "HDFCBANK": 10,
    "ICICIBANK": 10,
    "SBIN": 5,
    "AXISBANK": 10,
    "KOTAKBANK": 20,
    "HINDUNILVR": 50,
    "ITC": 5,
    "WIPRO": 5,
    "BAJFINANCE": 50,
    "BAJAJFINSV": 50,
    "LT": 20,
    "ADANIENT": 20,
    "ADANIPORTS": 10,
    "TATASTEEL": 5,
    "TATAMOTORS": 5,
    "MARUTI": 100,
    "M&M": 20,
    "SUNPHARMA": 10,
    "DRREDDY": 50,
    "CIPLA": 10,
    "POWERGRID": 5,
    "NTPC": 5,
    "ONGC": 5,
    "COALINDIA": 5,
    "BPCL": 5,
    "IOC": 2,
    "HCLTECH": 20,
    "TECHM": 10,
    "ULTRACEMCO": 50,
    "NESTLEIND": 100,
    "TITAN": 20,
    "GRASIM": 20,
    "BRITANNIA": 50,
    "EICHERMOT": 50,
    "HEROMOTOCO": 50,
    "DIVISLAB": 50,
    "APOLLOHOSP": 20,
    "INDUSINDBK": 20,
    "ASIANPAINT": 20,
}

# ── Upstox instrument key mapping for index underlyings ──────────────────────
# Format: "NSE_INDEX|<name as per Upstox>"
INDEX_INSTRUMENT_KEYS: dict[str, str] = {
    "NIFTY":      "NSE_INDEX|Nifty 50",
    "BANKNIFTY":  "NSE_INDEX|Nifty Bank",
    "FINNIFTY":   "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",   # confirmed via Upstox API
    "NIFTYNXT50": "NSE_INDEX|Nifty Next 50",
    "SENSEX":     "BSE_INDEX|SENSEX",
    "BANKEX":     "BSE_INDEX|BSE-BANKEX",
}


def get_fo_market(underlying: str | None) -> str:
    """Return the exchange market code used by the F&O catalogs."""
    symbol = str(underlying or "").upper()
    if symbol in BSE_FO_INDICES:
        return "BSE"
    return "NSE"

# ICICI Breeze stock_code / exchange_code mapping for F&O historical data
# Breeze uses different codes than Upstox trading symbols
BREEZE_INDEX_CODES: dict[str, dict] = {
    "NIFTY":      {"stock_code": "NIFTY",     "exchange_code": "NFO"},
    "BANKNIFTY":  {"stock_code": "BANKNIFTY", "exchange_code": "NFO"},
    "FINNIFTY":   {"stock_code": "FINNIFTY",  "exchange_code": "NFO"},
    "MIDCPNIFTY": {"stock_code": "MIDCPNIFTY","exchange_code": "NFO"},
    "NIFTYNXT50": {"stock_code": "NIFTYNXT50","exchange_code": "NFO"},
}

# For stocks, the Breeze stock_code is usually the same as the trading symbol
BREEZE_RIGHT_MAP = {"CE": "call", "PE": "put"}

# ── Known NSE market holidays (simplified list — expand as needed) ─────────────
# Add known market holidays here. If expiry Thursday falls on one of these,
# the expiry moves to Wednesday.
_KNOWN_HOLIDAYS: set[date] = {
    # 2024 holidays
    date(2024, 1, 22),   # Ram Navami (special)
    date(2024, 3, 25),   # Holi
    date(2024, 3, 29),   # Good Friday
    date(2024, 4, 14),   # Dr. Ambedkar Jayanti
    date(2024, 4, 17),   # Ram Navami
    date(2024, 4, 21),   # Eid-Ul-Fitr (Ramzan Eid)
    date(2024, 5, 23),   # Buddha Purnima
    date(2024, 6, 17),   # Eid Ul Adha (Bakri Eid)
    date(2024, 7, 17),   # Muharram
    date(2024, 8, 15),   # Independence Day
    date(2024, 10, 2),   # Gandhi Jayanti/Mahatma Gandhi Jayanti
    date(2024, 11, 1),   # Diwali Laxmi Pujan
    date(2024, 11, 15),  # Gurunanak Jayanti
    date(2024, 12, 25),  # Christmas
    # 2025 holidays
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr (Ramzan Eid)
    date(2025, 4, 14),   # Dr. Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 12),   # Buddha Purnima
    date(2025, 6, 7),    # Eid-Ul-Adha (Bakri Eid)
    date(2025, 7, 6),    # Muharram
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Gandhi Jayanti
    date(2025, 10, 20),  # Diwali (Laxmi Pujan)
    date(2025, 10, 21),  # Diwali (Balipratipada)
    date(2025, 11, 5),   # Gurunanak Jayanti
    date(2025, 12, 25),  # Christmas
    # 2026 holidays (add as confirmed)
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 20),   # Holi (estimated)
    date(2026, 4, 3),    # Good Friday (estimated)
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 12, 25),  # Christmas
}


def _is_trading_day(d: date) -> bool:
    """Return True if date is a weekday and not a known market holiday."""
    # Monday=0 ... Friday=4, Saturday=5, Sunday=6
    if d.weekday() >= 5:
        return False
    if d in _KNOWN_HOLIDAYS:
        return False
    return True


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of a given weekday (0=Mon … 6=Sun) in the month."""
    last_day = monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def get_monthly_expiry(year: int, month: int) -> date:
    """
    Return the actual NSE monthly expiry date, adjusted for holidays.

    NSE stock and NSE index derivatives currently share Tuesday monthly expiry.
    For BSE index contracts use get_index_monthly_expiry().
    """
    candidate = _last_weekday_of_month(year, month, 1)
    while not _is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def get_index_monthly_expiry(symbol: str, year: int, month: int) -> date:
    """
    Return the actual monthly expiry for any F&O index, based on its native expiry weekday.

    NSE indices currently share Tuesday monthly expiry:
      NIFTY/NIFTYNXT50/BANKNIFTY/FINNIFTY/MIDCPNIFTY → Tuesday (1)

    BSE indices keep their native weekday:
      SENSEX           → Friday (4) [BSE]
      BANKEX           → Monday (0) [BSE]

    The result is adjusted backward past any known market holidays.
    """
    weekday = INDEX_EXPIRY_WEEKDAY.get(symbol, 1)
    candidate = _last_weekday_of_month(year, month, weekday)
    while not _is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def get_monthly_expiries(from_date: date, to_date: date) -> list[date]:
    """
    Return all monthly NSE F&O expiry dates between from_date and to_date.

    Monthly expiry = last Tuesday of each month for NSE derivatives.
    If that Tuesday is a market holiday, expiry moves backward to the prior trading day.

    Parameters
    ----------
    from_date : date
        Start of range (inclusive)
    to_date : date
        End of range (inclusive)

    Returns
    -------
    list[date]
        Sorted list of expiry dates in the given range
    """
    expiries: list[date] = []

    year = from_date.year
    month = from_date.month

    while True:
        adjusted = get_monthly_expiry(year, month)

        if from_date <= adjusted <= to_date:
            expiries.append(adjusted)

        # Advance to next month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1

        # Stop if the start of this new month already exceeds to_date
        if date(year, month, 1) > to_date:
            break

    return sorted(expiries)


def get_atm_strike(spot: float, step: int) -> float:
    """
    Compute ATM strike by rounding spot to the nearest multiple of step.

    Parameters
    ----------
    spot : float
        Current spot price
    step : int
        Strike increment (e.g. 50 for NIFTY, 100 for BANKNIFTY)

    Returns
    -------
    float
        Nearest ATM strike
    """
    return float(round(spot / step) * step)


def get_expiry_month_start(expiry: date) -> date:
    """
    Return the first trading day of the month in which `expiry` falls.

    Skips weekends and known market holidays.

    Parameters
    ----------
    expiry : date
        The expiry date (typically last Thursday of month)

    Returns
    -------
    date
        First trading day of that month
    """
    d = date(expiry.year, expiry.month, 1)
    while not _is_trading_day(d):
        d += timedelta(days=1)
    return d


def get_previous_monthly_expiry(expiry: date) -> date:
    """
    Return the monthly expiry immediately preceding `expiry`.

    Parameters
    ----------
    expiry : date
        Any monthly expiry date.
    """
    if expiry.month == 1:
        return get_monthly_expiry(expiry.year - 1, 12)
    return get_monthly_expiry(expiry.year, expiry.month - 1)


def get_first_trading_day_after(d: date) -> date:
    """
    Return the first trading day strictly after the given date.
    """
    d = d + timedelta(days=1)
    while not _is_trading_day(d):
        d += timedelta(days=1)
    return d
