"""Canonical validation and provenance for signal-generation market inputs.

The registry is deliberately payload-free: strategies keep consuming their normal
objects while operators get one bounded, comparable view of freshness, rejection
reasons and source lineage.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from threading import RLock
from typing import Any, Iterable, Mapping


UTC = timezone.utc


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class ValidationResult:
    rows: list[dict[str, Any]]
    accepted_indices: list[int]
    quality: dict[str, Any]


class ValidatedSnapshotStore:
    """Thread-safe, bounded latest-quality registry keyed by feed identity."""

    def __init__(self, max_entries: int = 256) -> None:
        self._max_entries = max(int(max_entries), 1)
        self._latest: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def publish(self, quality: Mapping[str, Any]) -> None:
        item = dict(quality)
        key = str(item.get("key") or "")
        if not key:
            return
        with self._lock:
            self._latest[key] = item
            while len(self._latest) > self._max_entries:
                oldest = min(
                    self._latest,
                    key=lambda candidate: str(self._latest[candidate].get("received_at") or ""),
                )
                self._latest.pop(oldest, None)

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or _utc_now()).astimezone(UTC)
        with self._lock:
            items = [dict(item) for item in self._latest.values()]
        ready = degraded = rejected = 0
        for item in items:
            observed = _as_utc(item.get("observed_at"))
            age = max((current - observed).total_seconds(), 0.0) if observed else None
            budget = float(item.get("freshness_budget_seconds") or 0.0)
            fresh = age is not None and (budget <= 0 or age <= budget)
            item["age_seconds"] = round(age, 3) if age is not None else None
            item["fresh"] = fresh
            structural_ready = bool(item.pop("_structural_ready", item.get("execution_ready")))
            requires_fresh = bool(item.pop("_requires_fresh", True))
            item["execution_ready"] = structural_ready and (fresh or not requires_fresh)
            if not structural_ready:
                item["validation_status"] = "rejected"
                rejected += 1
            elif item.get("rejected_count") or (requires_fresh and not fresh):
                item["validation_status"] = "degraded"
                degraded += 1
            else:
                item["validation_status"] = "ready"
                ready += 1
        items.sort(key=lambda item: str(item.get("received_at") or ""), reverse=True)
        return {
            "generated_at": current.isoformat(),
            "feed_count": len(items),
            "ready_count": ready,
            "degraded_count": degraded,
            "rejected_count": rejected,
            "feeds": items,
        }

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()


validated_snapshot_store = ValidatedSnapshotStore()


def _quality(
    *,
    kind: str,
    symbol: str,
    scope: str,
    source: str,
    observed_at: datetime | None,
    received_at: datetime,
    freshness_budget_seconds: float,
    input_count: int,
    accepted_count: int,
    rejections: Counter[str],
    structural_ready: bool,
    requires_fresh: bool,
) -> dict[str, Any]:
    rejected_count = input_count - accepted_count
    quality = {
        "key": f"{kind}:{symbol.upper()}:{scope}",
        "kind": kind,
        "symbol": symbol.upper(),
        "scope": scope,
        "source": source,
        "observed_at": observed_at.isoformat() if observed_at else None,
        "received_at": received_at.isoformat(),
        "freshness_budget_seconds": float(freshness_budget_seconds),
        "input_count": input_count,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "rejection_counts": dict(rejections),
        "_structural_ready": structural_ready,
        "_requires_fresh": requires_fresh,
    }
    validated_snapshot_store.publish(quality)
    age = max((received_at - observed_at).total_seconds(), 0.0) if observed_at else None
    fresh = age is not None and (freshness_budget_seconds <= 0 or age <= freshness_budget_seconds)
    public_quality = {key: value for key, value in quality.items() if not key.startswith("_")}
    public_quality.update(
        age_seconds=round(age, 3) if age is not None else None,
        fresh=fresh,
        execution_ready=structural_ready and (fresh or not requires_fresh),
        validation_status=(
            "rejected"
            if not structural_ready
            else "degraded"
            if rejected_count or (requires_fresh and not fresh)
            else "ready"
        ),
    )
    return public_quality


def validate_candle_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    symbol: str,
    source: str,
    interval: str = "1minute",
    freshness_budget_seconds: float = 180.0,
    min_rows: int = 1,
    require_fresh: bool = True,
    now: datetime | None = None,
) -> ValidationResult:
    received_at = (now or _utc_now()).astimezone(UTC)
    raw_rows = list(rows)
    rejections: Counter[str] = Counter()
    deduped: dict[str, tuple[int, dict[str, Any], datetime]] = {}
    for index, row in enumerate(raw_rows):
        timestamp = _as_utc(row.get("time") or row.get("timestamp"))
        if timestamp is None:
            rejections["invalid_timestamp"] += 1
            continue
        if timestamp > received_at.replace(microsecond=0) and (timestamp - received_at).total_seconds() > 60:
            rejections["future_timestamp"] += 1
            continue
        open_, high, low, close = (_finite(row.get(field)) for field in ("open", "high", "low", "close"))
        if any(value is None or value <= 0 for value in (open_, high, low, close)):
            rejections["invalid_ohlc"] += 1
            continue
        assert open_ is not None and high is not None and low is not None and close is not None
        if low > min(open_, close) or high < max(open_, close) or low > high:
            rejections["inconsistent_ohlc"] += 1
            continue
        volume = _finite(row.get("volume") or 0.0)
        if volume is None or volume < 0:
            rejections["invalid_volume"] += 1
            continue
        normalized = dict(row)
        normalized.update(
            time=timestamp.isoformat(), open=open_, high=high, low=low, close=close, volume=volume
        )
        key = f"{normalized.get('instrument_key') or symbol}:{interval}:{timestamp.isoformat()}"
        if key in deduped:
            rejections["duplicate_bar"] += 1
        deduped[key] = (index, normalized, timestamp)
    ordered = sorted(deduped.values(), key=lambda item: item[2])
    accepted = [item[1] for item in ordered]
    indices = [item[0] for item in ordered]
    observed_at = ordered[-1][2] if ordered else None
    reject_ratio = (len(raw_rows) - len(accepted)) / max(len(raw_rows), 1)
    structural_ready = len(accepted) >= max(int(min_rows), 1) and reject_ratio <= 0.10
    quality = _quality(
        kind="candles", symbol=symbol, scope=interval, source=source,
        observed_at=observed_at, received_at=received_at,
        freshness_budget_seconds=freshness_budget_seconds,
        input_count=len(raw_rows), accepted_count=len(accepted), rejections=rejections,
        structural_ready=structural_ready, requires_fresh=require_fresh,
    )
    return ValidationResult(accepted, indices, quality)


def validate_option_chain_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    symbol: str,
    expiry: str,
    spot_price: Any,
    source: str,
    observed_at: datetime | None = None,
    freshness_budget_seconds: float = 60.0,
    now: datetime | None = None,
) -> ValidationResult:
    received_at = (now or _utc_now()).astimezone(UTC)
    observed = (observed_at or received_at).astimezone(UTC)
    raw_rows = list(rows)
    rejections: Counter[str] = Counter()
    spot = _finite(spot_price)
    accepted: list[dict[str, Any]] = []
    indices: list[int] = []
    seen: set[tuple[float, str]] = set()
    if spot is None or spot <= 0:
        rejections["invalid_spot"] += len(raw_rows) or 1
    else:
        for index, row in enumerate(raw_rows):
            strike = _finite(row.get("strike"))
            option_type = str(row.get("option_type") or "").upper()
            ltp, oi, volume = (_finite(row.get(field)) for field in ("ltp", "oi", "volume"))
            bid, ask = (_finite(row.get(field) or 0.0) for field in ("bid", "ask"))
            if strike is None or strike <= 0 or option_type not in {"CE", "PE"}:
                rejections["invalid_contract"] += 1
                continue
            if any(value is None or value < 0 for value in (ltp, oi, volume, bid, ask)):
                rejections["invalid_market_values"] += 1
                continue
            assert ltp is not None and bid is not None and ask is not None
            if bid > 0 and ask > 0 and bid > ask:
                rejections["crossed_market"] += 1
                continue
            if (option_type == "PE" and ltp > strike * 1.02) or (option_type == "CE" and ltp > spot * 1.05):
                rejections["no_arbitrage_violation"] += 1
                continue
            contract = (strike, option_type)
            if contract in seen:
                rejections["duplicate_contract"] += 1
                continue
            seen.add(contract)
            accepted.append(dict(row))
            indices.append(index)
    reject_ratio = (len(raw_rows) - len(accepted)) / max(len(raw_rows), 1)
    structural_ready = bool(accepted) and reject_ratio <= 0.10
    quality = _quality(
        kind="option_chain", symbol=symbol, scope=str(expiry), source=source,
        observed_at=observed, received_at=received_at,
        freshness_budget_seconds=freshness_budget_seconds,
        input_count=len(raw_rows), accepted_count=len(accepted), rejections=rejections,
        structural_ready=structural_ready, requires_fresh=True,
    )
    return ValidationResult(accepted, indices, quality)
