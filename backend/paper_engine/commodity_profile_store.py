"""Persistent store for daily MP profile snapshots.

Each closed MCX session, the commodity strategy agent writes the day's
MarketProfileSnapshot to a JSON file under
``backend/runtime/commodity_profiles/<root>/<YYYY-MM-DD>.json``. On boot,
the most recent ``N`` files per root are loaded into an in-memory cache
keyed by ``(root, date)`` so the dashboard can look up history without
re-deriving from raw candles.

The store also computes rolling **week** and **month** aggregates from
the daily snapshots so the detail-modal timeline can show Y / W / M
references that grow with each new session.

The on-disk shape is intentionally a plain dict that mirrors the
fields exposed in the strategy agent's row payload so future tooling
(notebooks, audit jobs) can consume it without a Python import:

.. code-block:: json

    {
      "root": "GOLD",
      "session_date": "2026-05-30",
      "tick_size": 1.0,
      "high": 161320.0,
      "low": 159580.0,
      "poc": 160620.0,
      "vah": 161080.0,
      "val": 160280.0,
      "ib_high": 160940.0,
      "ib_low": 160340.0,
      "tpo_counts": {"160280.0": 3, "160320.0": 4, ...},
      "tpo_letters": {"160280.0": "ABC", "160320.0": "ABCD", ...},
      "single_prints": [161320.0, 159580.0],
      "poor_high": false,
      "poor_low": true,
      "buying_tail": [...],
      "selling_tail": [...],
      "period_count": 36,
      "saved_at": "2026-05-31T01:32:14+05:30"
    }

This module is intentionally small and side-effect free at import time so
the strategy agent can call it directly without managing lifecycle.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from loguru import logger

IST = timezone(timedelta(hours=5, minutes=30))

# Persistence root — sits inside the existing runtime/ tree so the backup/
# restore step in the deploy workflow carries these snapshots forward.
PROFILE_STORE_DIR = (
    Path(__file__).resolve().parent.parent / "runtime" / "commodity_profiles"
)
PROFILE_STORE_DIR.mkdir(parents=True, exist_ok=True)

# Rolling-window memory limit per root — pulling more than 90 days into RAM is
# wasteful when the dashboard only renders day / week / month references.
DEFAULT_HISTORY_DAYS = 90


@dataclass
class DailyProfile:
    """Strongly-typed view over the JSON shape stored on disk."""

    root: str
    session_date: date
    poc: Optional[float]
    vah: Optional[float]
    val: Optional[float]
    ib_high: Optional[float]
    ib_low: Optional[float]
    high: Optional[float]
    low: Optional[float]
    tick_size: Optional[float]
    tpo_counts: dict[float, int]
    tpo_letters: dict[float, str]
    single_prints: list[float]
    poor_high: bool
    poor_low: bool
    period_count: int
    saved_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "session_date": self.session_date.isoformat(),
            "poc": self.poc,
            "vah": self.vah,
            "val": self.val,
            "ib_high": self.ib_high,
            "ib_low": self.ib_low,
            "high": self.high,
            "low": self.low,
            "tick_size": self.tick_size,
            # JSON keys must be strings — preserve the original float in the value
            # via the implicit ordering of the keys list (callers don't depend
            # on numeric key order; the dashboard rebuilds the float on read).
            "tpo_counts": {str(k): v for k, v in self.tpo_counts.items()},
            "tpo_letters": {str(k): v for k, v in self.tpo_letters.items()},
            "single_prints": list(self.single_prints),
            "poor_high": self.poor_high,
            "poor_low": self.poor_low,
            "period_count": self.period_count,
            "saved_at": self.saved_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DailyProfile":
        return cls(
            root=str(payload.get("root") or "").upper(),
            session_date=date.fromisoformat(str(payload.get("session_date"))),
            poc=_opt_float(payload.get("poc")),
            vah=_opt_float(payload.get("vah")),
            val=_opt_float(payload.get("val")),
            ib_high=_opt_float(payload.get("ib_high")),
            ib_low=_opt_float(payload.get("ib_low")),
            high=_opt_float(payload.get("high")),
            low=_opt_float(payload.get("low")),
            tick_size=_opt_float(payload.get("tick_size")),
            tpo_counts={
                float(k): int(v) for k, v in dict(payload.get("tpo_counts") or {}).items()
            },
            tpo_letters={
                float(k): str(v) for k, v in dict(payload.get("tpo_letters") or {}).items()
            },
            single_prints=[float(x) for x in list(payload.get("single_prints") or [])],
            poor_high=bool(payload.get("poor_high", False)),
            poor_low=bool(payload.get("poor_low", False)),
            period_count=int(payload.get("period_count") or 0),
            saved_at=str(payload.get("saved_at") or ""),
        )


# ─── Cache ────────────────────────────────────────────────────────────────


# In-memory cache: (root, session_date) → DailyProfile.
_CACHE: dict[tuple[str, date], DailyProfile] = {}
# Track when we last touched disk per root so repeated reads don't rescan.
_SCAN_AT: dict[str, datetime] = {}


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _root_dir(root: str) -> Path:
    safe = str(root or "").strip().upper()
    path = PROFILE_STORE_DIR / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_for(root: str, session_date: date) -> Path:
    return _root_dir(root) / f"{session_date.isoformat()}.json"


def save_profile(profile: DailyProfile) -> bool:
    """Persist one daily snapshot. Returns True on success.

    Safe to call repeatedly — the latest call wins. The strategy agent
    calls this at most once per (root, session_date) pair.
    """
    try:
        target = _file_for(profile.root, profile.session_date)
        target.write_text(json.dumps(profile.to_dict(), indent=2))
        _CACHE[(profile.root, profile.session_date)] = profile
        return True
    except Exception as exc:
        logger.warning(
            f"[commodity_profile_store] failed to persist {profile.root}/{profile.session_date}: {exc}"
        )
        return False


def load_recent(
    root: str,
    *,
    days: int = DEFAULT_HISTORY_DAYS,
) -> list[DailyProfile]:
    """Return the most recent ``days`` profiles for ``root``, newest first.

    Reads from disk on the first call per root, then serves from cache.
    """
    root_clean = str(root or "").strip().upper()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    last = _SCAN_AT.get(root_clean)
    if last is None or last < cutoff:
        _rescan_root(root_clean)
    profiles = [p for (r, _), p in _CACHE.items() if r == root_clean]
    profiles.sort(key=lambda p: p.session_date, reverse=True)
    return profiles[:days]


def _rescan_root(root: str) -> None:
    """Walk the root dir once and refill the cache. Cheap — only metadata."""
    folder = _root_dir(root)
    for path in folder.glob("*.json"):
        try:
            payload = json.loads(path.read_text())
            profile = DailyProfile.from_dict(payload)
            _CACHE[(root, profile.session_date)] = profile
        except Exception as exc:
            logger.debug(f"[commodity_profile_store] skipping {path}: {exc}")
    _SCAN_AT[root] = datetime.now(timezone.utc)


def get_profile(root: str, session_date: date) -> Optional[DailyProfile]:
    key = (str(root or "").strip().upper(), session_date)
    if key in _CACHE:
        return _CACHE[key]
    path = _file_for(*key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        profile = DailyProfile.from_dict(payload)
        _CACHE[key] = profile
        return profile
    except Exception as exc:
        logger.debug(f"[commodity_profile_store] failed to read {path}: {exc}")
        return None


# ─── Aggregates ───────────────────────────────────────────────────────────


def _aggregate(profiles: Iterable[DailyProfile]) -> Optional[dict[str, Any]]:
    """Merge multiple daily TPO counts into a single composite profile.

    POC/VAH/VAL are recomputed from the summed TPO counts (70% value area),
    so the composite represents 'where price spent the most time across the
    period' rather than an average of individual daily levels.
    """
    profiles = [p for p in profiles if p.tpo_counts]
    if not profiles:
        return None
    summed: dict[float, int] = defaultdict(int)
    period_total = 0
    for p in profiles:
        for price, count in p.tpo_counts.items():
            summed[float(price)] += int(count)
        period_total += int(p.period_count)
    if not summed:
        return None
    # POC: price with the highest combined TPO count.
    prices = sorted(summed)
    counts = [summed[p] for p in prices]
    max_count = max(counts)
    poc_candidates = [p for p, c in summed.items() if c == max_count]
    midpoint = sum(prices) / len(prices)
    poc = min(poc_candidates, key=lambda p: abs(p - midpoint))
    # Value area: expand from POC outward until 70% of total TPO is covered.
    total = sum(counts)
    target = max(int(total * 0.7), 1)
    poc_idx = prices.index(poc)
    lo = hi = poc_idx
    covered = summed[poc]
    while covered < target and (lo > 0 or hi < len(prices) - 1):
        next_lo = summed[prices[lo - 1]] if lo > 0 else -1
        next_hi = summed[prices[hi + 1]] if hi < len(prices) - 1 else -1
        if next_hi >= next_lo and hi < len(prices) - 1:
            hi += 1
            covered += summed[prices[hi]]
        elif lo > 0:
            lo -= 1
            covered += summed[prices[lo]]
        else:
            break
    return {
        "poc": round(poc, 4),
        "vah": round(prices[hi], 4),
        "val": round(prices[lo], 4),
        "high": prices[-1],
        "low": prices[0],
        "tpo_counts": {str(p): summed[p] for p in prices},
        "period_count": period_total,
        "days_covered": len(profiles),
        "first_date": min(p.session_date for p in profiles).isoformat(),
        "last_date": max(p.session_date for p in profiles).isoformat(),
    }


def previous_day(root: str, today: date) -> Optional[dict[str, Any]]:
    """Return yesterday's profile if available — used as the 'Y' reference."""
    profile = get_profile(root, today - timedelta(days=1))
    if profile is None:
        # Walk back up to 7 days to find the most recent prior trading day
        # (handles weekends / holidays).
        for delta in range(2, 8):
            profile = get_profile(root, today - timedelta(days=delta))
            if profile is not None:
                break
    return profile.to_dict() if profile is not None else None


def this_week(root: str, today: date) -> Optional[dict[str, Any]]:
    """Aggregate days since Monday (inclusive) up to and including today."""
    week_start = today - timedelta(days=today.weekday())
    week = [p for p in load_recent(root) if week_start <= p.session_date <= today]
    return _aggregate(week)


def last_week(root: str, today: date) -> Optional[dict[str, Any]]:
    """Aggregate the seven days preceding the start of this week."""
    this_week_start = today - timedelta(days=today.weekday())
    last_start = this_week_start - timedelta(days=7)
    last_end = this_week_start - timedelta(days=1)
    period = [p for p in load_recent(root) if last_start <= p.session_date <= last_end]
    return _aggregate(period)


def this_month(root: str, today: date) -> Optional[dict[str, Any]]:
    month_start = today.replace(day=1)
    period = [p for p in load_recent(root) if month_start <= p.session_date <= today]
    return _aggregate(period)


def last_month(root: str, today: date) -> Optional[dict[str, Any]]:
    this_month_start = today.replace(day=1)
    last_end = this_month_start - timedelta(days=1)
    last_start = last_end.replace(day=1)
    period = [p for p in load_recent(root) if last_start <= p.session_date <= last_end]
    return _aggregate(period)


def historical_timeline(root: str, today: Optional[date] = None) -> dict[str, Any]:
    """One-shot payload for the dashboard: today's date plus four aggregates.

    Today's profile itself is provided live by the agent's analyze loop, so
    this only returns prior references.
    """
    today = today or datetime.now(IST).date()
    return {
        "root": root,
        "today": today.isoformat(),
        "yesterday": previous_day(root, today),
        "this_week": this_week(root, today),
        "last_week": last_week(root, today),
        "this_month": this_month(root, today),
        "last_month": last_month(root, today),
    }


# ─── Helpers for the agent ────────────────────────────────────────────────


def build_daily_profile_from_snapshot(
    root: str,
    snapshot: Any,
) -> Optional[DailyProfile]:
    """Adapt a ``MarketProfileSnapshot`` to the stored ``DailyProfile`` shape.

    Used by the strategy agent when a session closes.
    """
    if snapshot is None:
        return None
    try:
        session_date_attr = getattr(snapshot, "session_date", None)
        if isinstance(session_date_attr, str):
            session_date = date.fromisoformat(session_date_attr)
        else:
            session_date = date.today()
        return DailyProfile(
            root=str(root or "").strip().upper(),
            session_date=session_date,
            poc=_opt_float(getattr(snapshot, "poc", None)),
            vah=_opt_float(getattr(snapshot, "vah", None)),
            val=_opt_float(getattr(snapshot, "val", None)),
            ib_high=_opt_float(getattr(snapshot, "initial_balance_high", None)),
            ib_low=_opt_float(getattr(snapshot, "initial_balance_low", None)),
            high=_opt_float(getattr(snapshot, "high_price", None)),
            low=_opt_float(getattr(snapshot, "low_price", None)),
            tick_size=_opt_float(getattr(snapshot, "tick_size", None)),
            tpo_counts={
                float(k): int(v)
                for k, v in dict(getattr(snapshot, "tpo_counts", {}) or {}).items()
            },
            tpo_letters={
                float(k): str(v)
                for k, v in dict(getattr(snapshot, "tpo_letters", {}) or {}).items()
            },
            single_prints=[
                float(x) for x in list(getattr(snapshot, "single_prints", []) or [])
            ],
            poor_high=bool(getattr(snapshot, "poor_high", False)),
            poor_low=bool(getattr(snapshot, "poor_low", False)),
            period_count=int(getattr(snapshot, "period_count", 0) or 0),
            saved_at=datetime.now(IST).isoformat(),
        )
    except Exception as exc:
        logger.debug(f"[commodity_profile_store] adapter failed for {root}: {exc}")
        return None
