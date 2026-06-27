from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))


def _resolve_calendar_file() -> Path:
    env_path = os.environ.get("TRADING_CALENDAR_FILE", "").strip()
    if env_path:
        return Path(env_path)
    docker_path = Path("/app/trading_calendar.json")
    if docker_path.parent.is_dir():
        return docker_path
    return Path(__file__).resolve().parents[1] / "runtime" / "trading_calendar.json"


_CALENDAR_FILE = _resolve_calendar_file()

_DEFAULT_SESSIONS: dict[str, list[dict[str, str]]] = {
    "NSE": [{"key": "regular", "label": "Regular", "open": "09:15", "close": "15:30"}],
    "MCX": [
        {"key": "morning", "label": "Morning", "open": "09:00", "close": "17:00"},
        {"key": "evening", "label": "Evening", "open": "17:00", "close": "23:30"},
    ],
}

_NSE_2026_CLOSED = [
    ("2026-01-26", "Republic Day"),
    ("2026-03-03", "Holi"),
    ("2026-03-26", "Shri Ram Navami"),
    ("2026-03-31", "Shri Mahavir Jayanti"),
    ("2026-04-03", "Good Friday"),
    ("2026-04-14", "Dr. Baba Saheb Ambedkar Jayanti"),
    ("2026-05-01", "Maharashtra Day"),
    ("2026-05-28", "Bakri Id"),
    ("2026-06-26", "Muharram"),
    ("2026-09-14", "Ganesh Chaturthi"),
    ("2026-10-02", "Mahatma Gandhi Jayanti"),
    ("2026-10-20", "Dussehra"),
    ("2026-11-10", "Diwali-Balipratipada"),
    ("2026-11-24", "Prakash Gurpurb Sri Guru Nanak Dev"),
    ("2026-12-25", "Christmas"),
]

_MCX_2026_CLOSED = [
    ("2026-01-01", "New Year Day"),
    ("2026-01-26", "Republic Day"),
    ("2026-03-03", "Holi"),
    ("2026-03-26", "Shri Ram Navami"),
    ("2026-03-31", "Shri Mahavir Jayanti"),
    ("2026-04-03", "Good Friday"),
    ("2026-04-14", "Dr. Baba Saheb Ambedkar Jayanti"),
    ("2026-05-01", "Maharashtra Day"),
    ("2026-10-02", "Mahatma Gandhi Jayanti"),
    ("2026-12-25", "Christmas"),
]

_MCX_2026_EVENING_ONLY = [
    ("2026-05-28", "Bakri Id"),
    ("2026-06-26", "Muharram"),
    ("2026-09-14", "Ganesh Chaturthi"),
    ("2026-10-20", "Dussehra"),
    ("2026-11-10", "Diwali-Balipratipada"),
    ("2026-11-24", "Prakash Gurpurb Sri Guru Nanak Dev"),
]


def _default_exceptions(exchange: str) -> list[dict[str, Any]]:
    if exchange == "NSE":
        return [
            {"date": day, "name": name, "status": "closed", "sessions": []}
            for day, name in _NSE_2026_CLOSED
        ]
    if exchange == "MCX":
        rows = [
            {"date": day, "name": name, "status": "closed", "sessions": []}
            for day, name in _MCX_2026_CLOSED
        ]
        rows.extend(
            {"date": day, "name": name, "status": "partial", "sessions": ["evening"]}
            for day, name in _MCX_2026_EVENING_ONLY
        )
        return sorted(rows, key=lambda item: str(item["date"]))
    return []


def _default_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "timezone": "Asia/Kolkata",
        "exchanges": {
            exchange: {
                "enabled": True,
                "sessions": deepcopy(sessions),
                "exceptions": _default_exceptions(exchange),
            }
            for exchange, sessions in _DEFAULT_SESSIONS.items()
        },
    }


def _parse_time(value: Any) -> time:
    hour, minute = str(value or "00:00").split(":", 1)
    return time(int(hour), int(minute[:2]))


def _parse_date(value: Any) -> date:
    return date.fromisoformat(str(value or "").strip()[:10])


def _normalize_exception(raw: dict[str, Any]) -> dict[str, Any] | None:
    try:
        day = _parse_date(raw.get("date")).isoformat()
    except Exception:
        return None
    status = str(raw.get("status") or "closed").strip().lower()
    if status not in {"closed", "partial", "open"}:
        status = "closed"
    sessions = [
        str(item).strip().lower()
        for item in (raw.get("sessions") or [])
        if str(item or "").strip()
    ]
    return {
        "date": day,
        "name": str(raw.get("name") or raw.get("description") or "").strip(),
        "status": status,
        "sessions": sessions,
    }


def _normalize_exchange_config(exchange: str, raw: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        "enabled": True,
        "sessions": deepcopy(_DEFAULT_SESSIONS.get(exchange, [])),
        "exceptions": _default_exceptions(exchange),
    }
    if not isinstance(raw, dict):
        return base
    base["enabled"] = bool(raw.get("enabled", base["enabled"]))
    if isinstance(raw.get("sessions"), list) and raw["sessions"]:
        sessions: list[dict[str, str]] = []
        for session in raw["sessions"]:
            if not isinstance(session, dict):
                continue
            key = str(session.get("key") or "").strip().lower()
            open_at = str(session.get("open") or "").strip()
            close_at = str(session.get("close") or "").strip()
            if not key or not open_at or not close_at:
                continue
            sessions.append(
                {
                    "key": key,
                    "label": str(session.get("label") or key.title()).strip(),
                    "open": open_at,
                    "close": close_at,
                }
            )
        if sessions:
            base["sessions"] = sessions
    if isinstance(raw.get("exceptions"), list):
        normalized = [
            item
            for item in (_normalize_exception(row) for row in raw["exceptions"] if isinstance(row, dict))
            if item is not None
        ]
        base["exceptions"] = sorted(normalized, key=lambda item: str(item["date"]))
    return base


def _normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    defaults = _default_config()
    if not isinstance(raw, dict):
        return defaults
    return {
        "enabled": bool(raw.get("enabled", defaults["enabled"])),
        "timezone": "Asia/Kolkata",
        "exchanges": {
            "NSE": _normalize_exchange_config("NSE", (raw.get("exchanges") or {}).get("NSE")),
            "MCX": _normalize_exchange_config("MCX", (raw.get("exchanges") or {}).get("MCX")),
        },
    }


class TradingCalendar:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _CALENDAR_FILE
        self._config = _normalize_config(self._load_file())

    def _load_file(self) -> dict[str, Any] | None:
        if not self._path.exists():
            return None
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def reload(self) -> dict[str, Any]:
        self._config = _normalize_config(self._load_file())
        return self.serialize()

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._config = _normalize_config(payload)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._config, indent=2, sort_keys=True), encoding="utf-8")
        return self.serialize()

    def serialize(self) -> dict[str, Any]:
        return deepcopy(self._config)

    def _exchange_config(self, exchange: str) -> dict[str, Any]:
        exchange_key = exchange.upper()
        return self._config.get("exchanges", {}).get(exchange_key) or _normalize_exchange_config(exchange_key, None)

    def _exception_for(self, exchange: str, target_date: date) -> dict[str, Any] | None:
        for row in self._exchange_config(exchange).get("exceptions") or []:
            if str(row.get("date")) == target_date.isoformat():
                return row
        return None

    def _sessions_for_date(self, exchange: str, target_date: date) -> list[dict[str, str]]:
        exchange_config = self._exchange_config(exchange)
        if target_date.weekday() >= 5:
            return []
        sessions = list(exchange_config.get("sessions") or [])
        if not bool(self._config.get("enabled", True)) or not bool(exchange_config.get("enabled", True)):
            return sessions
        exception = self._exception_for(exchange, target_date)
        if not exception:
            return sessions
        status = str(exception.get("status") or "closed").lower()
        if status == "closed":
            return []
        if status == "open":
            return sessions
        allowed = {str(item).lower() for item in exception.get("sessions") or []}
        return [session for session in sessions if str(session.get("key") or "").lower() in allowed]

    def trading_minutes_between(self, exchange: str, start: datetime, end: datetime) -> float:
        """Exchange-OPEN minutes between two instants.

        Sums only the wall-clock that falls inside a trading session for
        ``exchange``; the overnight gap, weekends, holidays and partial-session
        closures all contribute zero (honoured via ``_sessions_for_date``). This
        is the basis for counting a position's held-time in *trading-session
        bars* rather than raw wall-clock — without it, the ~17h overnight gap
        inflates an intraday horizon and force-closes a multi-day hold at the
        next open. Returns 0.0 when ``end <= start``.
        """
        s = (start or datetime.now(IST)).astimezone(IST)
        e = (end or datetime.now(IST)).astimezone(IST)
        if e <= s:
            return 0.0
        total = 0.0
        day = s.date()
        last = e.date()
        # A 1-2 day hold spans a handful of days; cap the walk so a stale/stuck
        # position can't iterate unbounded.
        guard = 0
        while day <= last and guard <= 400:
            guard += 1
            for session in self._sessions_for_date(exchange, day):
                open_dt = datetime.combine(day, _parse_time(session.get("open")), tzinfo=IST)
                close_dt = datetime.combine(day, _parse_time(session.get("close")), tzinfo=IST)
                lo = max(open_dt, s)
                hi = min(close_dt, e)
                if hi > lo:
                    total += (hi - lo).total_seconds() / 60.0
            day += timedelta(days=1)
        return total

    def is_exchange_open(self, exchange: str, now: datetime | None = None) -> bool:
        current = (now or datetime.now(IST)).astimezone(IST)
        for session in self._sessions_for_date(exchange, current.date()):
            if _parse_time(session.get("open")) <= current.time() <= _parse_time(session.get("close")):
                return True
        return False

    def has_exchange_session(self, exchange: str, target_date: date) -> bool:
        return bool(self._sessions_for_date(exchange, target_date))

    def next_exchange_open(self, exchange: str, now: datetime | None = None) -> datetime:
        current = (now or datetime.now(IST)).astimezone(IST)
        if self.is_exchange_open(exchange, current):
            return current
        for offset in range(0, 370):
            target_date = current.date() + timedelta(days=offset)
            for session in self._sessions_for_date(exchange, target_date):
                candidate = datetime.combine(target_date, _parse_time(session.get("open")), tzinfo=IST)
                if candidate > current:
                    return candidate
        return current + timedelta(days=1)

    def exchange_status(self, exchange: str, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(IST)).astimezone(IST)
        exception = self._exception_for(exchange, current.date())
        active_session = None
        for session in self._sessions_for_date(exchange, current.date()):
            if _parse_time(session.get("open")) <= current.time() <= _parse_time(session.get("close")):
                active_session = session
                break
        next_open = self.next_exchange_open(exchange, current)
        status = "open" if active_session else "closed"
        reason = None
        if exception and not active_session:
            reason = exception.get("name") or exception.get("status")
        elif current.weekday() >= 5:
            reason = "weekend"
        elif not active_session:
            reason = "outside_session"
        return {
            "exchange": exchange.upper(),
            "as_of": current.isoformat(),
            "is_open": bool(active_session),
            "status": status,
            "active_session": active_session,
            "today_exception": exception,
            "today_sessions": self._sessions_for_date(exchange, current.date()),
            "next_open_at": next_open.isoformat(),
            "reason": reason,
        }

    def status_payload(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(IST)).astimezone(IST)
        return {
            "config": self.serialize(),
            "status": {
                "as_of": current.isoformat(),
                "NSE": self.exchange_status("NSE", current),
                "MCX": self.exchange_status("MCX", current),
            },
        }


trading_calendar = TradingCalendar()
