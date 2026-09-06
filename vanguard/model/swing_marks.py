"""Causal, immutable-entry accounting for exact-contract paper observations."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from model.session_clock import available_at

IST = ZoneInfo("Asia/Kolkata")


def mark_available(row):
    return (available_at(row["time"]) if row["interval"] == "30minute"
            else row["time"] + timedelta(minutes=3))


def summarize_path(item, path, sessions, now):
    horizon = int(item["horizon_sessions"])
    if horizon not in (1, 2):
        raise ValueError("unsupported swing horizon")
    planned = datetime.combine(item["source_session"], time(14, 45), IST)
    entry_at = available_at(planned)
    decision_at = item.get("decision_at") or item.get("generated_at")
    if decision_at is None or (decision_at >= entry_at and not item.get("is_replay", False)):
        return None
    complete = [r for r in path if mark_available(r) <= now]
    entry_ts, entry_mark = item.get("entry_ts"), item.get("entry_mark")
    if entry_ts is None or entry_mark is None:
        entries = [r for r in complete if r["interval"] == "30minute" and r["time"] == planned]
        if not entries:
            return None  # never substitute a later candle's close as entry
        entry_ts, entry_mark = planned, float(entries[0]["close"])
    else:
        entry_mark = float(entry_mark)  # freeze the stored entry across archive corrections
    if entry_mark <= 0:
        return None
    entry_at = available_at(entry_ts)
    final_session = sessions[horizon-1] if len(sessions) >= horizon else None
    final_stamp = datetime.combine(final_session, time(14, 45), IST) if final_session else None
    final_at = available_at(final_stamp) if final_stamp else None
    after = [r for r in complete if r["time"] >= entry_at
             and (final_at is None or mark_available(r) <= final_at)]
    # In overlapping 30m/3m data the close available last is the newest mark.
    after.sort(key=lambda r: (mark_available(r), r["interval"] == "3minute"))
    latest = after[-1] if after else {"time": entry_ts, "close": entry_mark}
    day_marks = {}
    for number, day in enumerate(sessions[:horizon], 1):
        stamp = datetime.combine(day, time(14, 45), IST)
        matches = [r for r in after if r["interval"] == "30minute" and r["time"] == stamp]
        if matches:
            row = matches[-1]
            day_marks[number] = (stamp, float(row["close"]), float(row["close"])/entry_mark-1)
    closed = horizon in day_marks
    if closed:
        stamp, close, _ = day_marks[horizon]
        latest = {"time": stamp, "close": close}
    highs = [entry_mark] + [float(r["high"] or r["close"]) for r in after]
    lows = [entry_mark] + [float(r["low"] or r["close"]) for r in after]
    gross = float(latest["close"])/entry_mark-1
    return dict(entry_ts=entry_ts, entry_mark=entry_mark, latest_ts=latest["time"],
                latest_mark=float(latest["close"]), return_pct=gross,
                net_return_pct=gross-float(item.get("cost_pct") or 0),
                max_return_pct=max(highs)/entry_mark-1, min_return_pct=min(lows)/entry_mark-1,
                day_marks=day_marks, status="closed" if closed else "tracking")
