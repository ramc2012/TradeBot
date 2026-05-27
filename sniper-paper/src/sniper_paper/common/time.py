from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from sniper_paper.common.settings import Instrument

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> pd.Timestamp:
    return pd.Timestamp.now(tz=IST)


def to_ist(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        return ts.tz_localize(IST)
    return ts.tz_convert(IST)


def parse_hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_in_trading_hours(ts: pd.Timestamp, instrument: Instrument) -> bool:
    ts = to_ist(ts)
    o = parse_hm(instrument.trading_hours_ist.open)
    c = parse_hm(instrument.trading_hours_ist.close)
    return o <= ts.time() <= c


def minutes_into_session(ts: pd.Timestamp, instrument: Instrument) -> int:
    ts = to_ist(ts)
    o = parse_hm(instrument.trading_hours_ist.open)
    open_dt = datetime.combine(ts.date(), o, tzinfo=IST)
    return max(0, int((ts - open_dt).total_seconds() // 60))


def next_session_open(ts: pd.Timestamp, instrument: Instrument) -> pd.Timestamp:
    ts = to_ist(ts)
    o = parse_hm(instrument.trading_hours_ist.open)
    today_open = datetime.combine(ts.date(), o, tzinfo=IST)
    if ts < today_open:
        return pd.Timestamp(today_open)
    next_day = ts.date() + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day = next_day + timedelta(days=1)
    return pd.Timestamp(datetime.combine(next_day, o, tzinfo=IST))
