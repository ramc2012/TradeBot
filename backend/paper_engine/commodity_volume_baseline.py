"""Per-instrument volume baselines for ADAPTIVE order-flow reads.

CVD / volume must be judged RELATIVE to each instrument's own normal — 5 lots is
"large" for NICKEL but trivial for CRUDEOIL. This module learns each instrument's
volume distribution (median + p90/p95) from its MP-period bar volumes and
PERSISTS it, so "normal volume", "pressure" and "large volume" are defined
per-instrument instead of by one global threshold (which is exactly why the
old R0 blanket-demote was the wrong call).

Volumes are aggregated to the MP period (default 15 min) because 1-min volume is
too sparse on illiquid names (NICKEL ~5% / GOLD ~25% non-zero 1-min bars; at the
15-min MP bar NICKEL ~47% / GOLD ~60% / NATGAS+SILVERM ~98%).

Bar-OHLCV-inferred: normalization fixes the SCALE problem, not the missing
tick-aggressor feed (MCX has no public trade prints).

Persisted at ``runtime/commodity_vol_baselines/<ROOT>.json`` (mirrors the daily
profile store). Consumed only when ``settings.COMMODITY_VOL_BASELINE_ENABLED``.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from loguru import logger

IST = timezone(timedelta(hours=5, minutes=30))

_STORE_DIR = Path(__file__).resolve().parent.parent / "runtime" / "commodity_vol_baselines"
DEFAULT_MP_PERIOD_MINUTES = 15
# Need at least this many MP-period observations before the baseline is trusted;
# below it, callers should treat the instrument as "not yet learned" and fall
# back to raw reads rather than a half-learned threshold.
MIN_SAMPLES = 20


@dataclass
class VolumeBaseline:
    root: str
    mp_period_minutes: int = DEFAULT_MP_PERIOD_MINUTES
    median: float = 0.0
    p50: float = 0.0
    p90: float = 0.0
    p95: float = 0.0
    mean: float = 0.0
    std: float = 0.0
    sample_count: int = 0
    updated_at: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self.sample_count >= MIN_SAMPLES and self.median > 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "VolumeBaseline":
        return cls(
            root=str(payload.get("root") or "").strip().upper(),
            mp_period_minutes=int(payload.get("mp_period_minutes") or DEFAULT_MP_PERIOD_MINUTES),
            median=float(payload.get("median") or 0.0),
            p50=float(payload.get("p50") or 0.0),
            p90=float(payload.get("p90") or 0.0),
            p95=float(payload.get("p95") or 0.0),
            mean=float(payload.get("mean") or 0.0),
            std=float(payload.get("std") or 0.0),
            sample_count=int(payload.get("sample_count") or 0),
            updated_at=payload.get("updated_at"),
        )


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (q in [0,1]) of an ascending-sorted list."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


def aggregate_mp_volumes(
    bars_1m: Sequence[dict[str, Any]],
    *,
    period_bars: int = DEFAULT_MP_PERIOD_MINUTES,
) -> list[float]:
    """Sum 1-min bar volume into fixed MP-period buckets (``period_bars`` bars each).

    Zero-volume buckets are kept (they are real "quiet" periods and part of the
    instrument's distribution). Partial trailing bucket is dropped.
    """
    out: list[float] = []
    step = max(int(period_bars), 1)
    n = len(bars_1m)
    for i in range(0, n - step + 1, step):
        total = 0.0
        for b in bars_1m[i:i + step]:
            try:
                total += max(float(b.get("volume") or 0.0), 0.0)
            except (TypeError, ValueError):
                continue
        out.append(total)
    return out


def compute_baseline(
    root: str,
    mp_volumes: Sequence[float],
    *,
    mp_period_minutes: int = DEFAULT_MP_PERIOD_MINUTES,
) -> VolumeBaseline:
    """Build a VolumeBaseline from a list of MP-period bucket volumes.

    Only positive-volume buckets define the "what is normal / large" distribution
    (a market that wasn't trading isn't informative about size); ``sample_count``
    reflects those positive observations.
    """
    vals = sorted(float(v) for v in mp_volumes if v is not None and float(v) > 0.0)
    base = VolumeBaseline(root=str(root or "").strip().upper(), mp_period_minutes=int(mp_period_minutes))
    if not vals:
        return base
    base.median = float(statistics.median(vals))
    base.p50 = _percentile(vals, 0.50)
    base.p90 = _percentile(vals, 0.90)
    base.p95 = _percentile(vals, 0.95)
    base.mean = float(statistics.fmean(vals))
    base.std = float(statistics.pstdev(vals)) if len(vals) > 1 else 0.0
    base.sample_count = len(vals)
    base.updated_at = datetime.now(IST).isoformat()
    return base


# ─── Persistence (JSON per root, mirrors commodity_profile_store) ───────────

def _file_for(root: str) -> Path:
    return _STORE_DIR / f"{str(root or '').strip().upper()}.json"


def save_baseline(baseline: VolumeBaseline) -> bool:
    try:
        _STORE_DIR.mkdir(parents=True, exist_ok=True)
        _file_for(baseline.root).write_text(json.dumps(baseline.to_dict(), indent=2), encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[commodity_volume_baseline] failed to persist {baseline.root}: {exc}")
        return False


def load_baseline(root: str) -> Optional[VolumeBaseline]:
    try:
        path = _file_for(root)
        if not path.exists():
            return None
        return VolumeBaseline.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[commodity_volume_baseline] load failed for {root}: {exc}")
        return None


# ─── Adaptive classification helpers (the point of all this) ────────────────

def is_large_volume(volume: float, baseline: Optional[VolumeBaseline], *, level: str = "p90") -> bool:
    """True if `volume` is large FOR THIS INSTRUMENT (>= its p90/p95). Returns
    False when the baseline isn't learned yet (caller falls back to raw reads)."""
    if baseline is None or not baseline.ready:
        return False
    thresh = baseline.p95 if level == "p95" else baseline.p90
    return thresh > 0.0 and float(volume) >= thresh


def pressure_ratio(signed_volume: float, baseline: Optional[VolumeBaseline]) -> Optional[float]:
    """Per-instrument pressure: signed flow in units of the instrument's typical
    (median) MP-bar volume. +2.0 means ~2× normal buying, −1.0 ~1× normal selling.
    None when the baseline isn't learned."""
    if baseline is None or not baseline.ready or baseline.median <= 0.0:
        return None
    return float(signed_volume) / baseline.median


def volume_z(volume: float, baseline: Optional[VolumeBaseline]) -> Optional[float]:
    """Z-score of `volume` vs the instrument's own distribution (None if unlearned)."""
    if baseline is None or not baseline.ready or baseline.std <= 0.0:
        return None
    return (float(volume) - baseline.mean) / baseline.std


# ─── Backfill (write the durable per-instrument baselines) ──────────────────
#
# Reuses the MP-history session loader (per-IST-session 1-min bars WITH volume
# from underlying_spot_candles), pools every session's MP-period bucket volumes,
# and computes one baseline per root. Local + idempotent — re-running just
# refreshes the distribution as more sessions accrue. Run on startup + via a
# periodic runner so the baselines stay current, like the MP profiles.

async def backfill_baseline_for_root(
    root: str,
    *,
    lookback_sessions: int = 90,
    period_bars: int = DEFAULT_MP_PERIOD_MINUTES,
    reason: str = "scheduled",
) -> dict[str, Any]:
    """Build + persist one root's volume baseline from its durable 1-min history."""
    from paper_engine.commodity_mp_history import _load_session_bars

    try:
        from market_data.commodity_contract_specs import extract_commodity_root

        normalized = extract_commodity_root(root) if ":" in str(root) else str(root or "").strip().upper()
    except Exception:
        normalized = str(root or "").strip().upper()

    sessions = await _load_session_bars(normalized, limit=lookback_sessions)
    mp_volumes: list[float] = []
    for _session_date, bars in sessions.items():
        bar_dicts = [{"volume": getattr(b, "volume", 0.0)} for b in bars]
        mp_volumes.extend(aggregate_mp_volumes(bar_dicts, period_bars=period_bars))

    baseline = compute_baseline(normalized, mp_volumes, mp_period_minutes=period_bars)
    saved = save_baseline(baseline) if baseline.sample_count > 0 else False
    if saved:
        logger.info(
            f"[commodity_volume_baseline] {normalized}: {baseline.sample_count} MP-bucket samples "
            f"from {len(sessions)} sessions — median={baseline.median:.0f} p90={baseline.p90:.0f} "
            f"p95={baseline.p95:.0f} ready={baseline.ready} (reason={reason})"
        )
    return {
        "root": normalized,
        "reason": reason,
        "sessions": len(sessions),
        "mp_buckets": len(mp_volumes),
        "sample_count": baseline.sample_count,
        "median": baseline.median,
        "p90": baseline.p90,
        "p95": baseline.p95,
        "ready": baseline.ready,
        "saved": saved,
    }


async def backfill_all_baselines(
    roots: Sequence[str],
    *,
    lookback_sessions: int = 90,
    reason: str = "scheduled",
) -> dict[str, Any]:
    """Backfill baselines for every root; per-root failures are isolated."""
    out: dict[str, Any] = {}
    for r in roots:
        key = str(r or "").strip().upper()
        try:
            out[key] = await backfill_baseline_for_root(
                r, lookback_sessions=lookback_sessions, reason=reason
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[commodity_volume_baseline] backfill failed for {key}: {exc}")
            out[key] = {"root": key, "error": str(exc)}
    return out
