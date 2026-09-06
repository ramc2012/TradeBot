"""NSE 30-minute candles are OPEN-stamped; their close is known later."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def available_at(stamp: datetime) -> datetime:
    if stamp.tzinfo is None:
        raise ValueError("candle timestamp must be timezone-aware")
    local = stamp.astimezone(IST)
    if (local.weekday() >= 5 or local.minute not in (15, 45)
            or local.second or local.microsecond
            or not time(9, 15) <= local.time() <= time(15, 15)):
        raise ValueError("not an NSE 30-minute candle start")
    return min(local + timedelta(minutes=30), local.replace(hour=15, minute=30))


def completed(stamp: datetime, as_of: datetime) -> bool:
    return available_at(stamp) <= as_of


def timely_decision(stamp: datetime, decision_at: datetime) -> bool:
    """A historical replay made after the next close is not prospective."""
    close = available_at(stamp)
    return close <= decision_at < close + timedelta(minutes=30)
