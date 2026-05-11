"""Cross-strategy signal bucket classifier.

Every strategy agent in this codebase produces a per-instrument row that the
frontend renders. The user-visible workspace needs each row to be sorted into
one of three lists:

  * already met / traded   → bucket in {active, ready}
  * favourable, tracked    → bucket = favourable
  * drifting away          → bucket = drifting

This module is the single source of truth for that classification so the
commodity desk, NSE MACD desk, Index MP desk, Fractal MP desk and Directional
Options desk all bucket the same way. Strategy agents call
``classify_signal_bucket`` from their watchlist / signal-lane builders.

The classification is read-only — it never gates entries. The existing
per-strategy ``signal_validation`` pipelines retain responsibility for
deciding whether a row may execute.
"""
from __future__ import annotations

from typing import Any, Optional


def classify_signal_bucket(
    *,
    has_position: bool,
    signal_validation: Optional[str],
    macd: Optional[float],
    macd_histogram: Optional[float],
    prev_macd: Optional[float] = None,
    prev_macd_histogram: Optional[float] = None,
    recent_cross_signal: Optional[str] = None,
    recent_cross_bars_ago: Optional[int] = None,
) -> dict[str, Any]:
    """Return ``{bucket, trajectory, proximity_pct, bucket_rationale}``.

    Buckets:
      * ``active``     — position open on this underlying
      * ``ready``      — all validations passed; queued for entry
      * ``favourable`` — close to triggering or just past a fresh cross
      * ``drifting``   — moving further from the favourable zone
      * ``neutral``    — outside favourable zone, no immediate prospect
    """
    if has_position:
        return {
            "bucket": "active",
            "trajectory": None,
            "proximity_pct": 100.0,
            "bucket_rationale": "Position already open for this underlying.",
        }
    if signal_validation == "ready":
        return {
            "bucket": "ready",
            "trajectory": None,
            "proximity_pct": 100.0,
            "bucket_rationale": "All entry conditions met; queued for execution.",
        }
    if macd is None or macd_histogram is None:
        return {
            "bucket": "neutral",
            "trajectory": None,
            "proximity_pct": 0.0,
            "bucket_rationale": "Warming up or indicators unavailable.",
        }

    trajectory = "stalled"
    if prev_macd_histogram is not None:
        if macd_histogram > prev_macd_histogram + 1e-9:
            trajectory = "improving"
        elif macd_histogram < prev_macd_histogram - 1e-9:
            trajectory = "deteriorating"

    if recent_cross_signal in {"BUY", "SELL"} and (recent_cross_bars_ago or 0) <= 3:
        return {
            "bucket": "favourable",
            "trajectory": trajectory,
            "proximity_pct": 90.0,
            "bucket_rationale": (
                f"Fresh {recent_cross_signal} zero-cross "
                f"{recent_cross_bars_ago} bar(s) ago — tracking close for "
                "continuation breakout."
            ),
        }

    macd_abs = abs(macd)
    hist_abs = abs(macd_histogram)
    denom = max(macd_abs, hist_abs, 1e-9)
    proximity = max(0.0, min(100.0, 100.0 * (1.0 - macd_abs / (macd_abs + denom + 1e-9))))

    if macd < 0 and macd_histogram > 0:
        return {
            "bucket": "favourable",
            "trajectory": trajectory,
            "proximity_pct": round(proximity, 1),
            "bucket_rationale": (
                "MACD below zero with rising histogram — BUY zero-cross "
                f"approaching. Trajectory {trajectory}."
            ),
        }
    if macd > 0 and macd_histogram < 0:
        return {
            "bucket": "favourable",
            "trajectory": trajectory,
            "proximity_pct": round(proximity, 1),
            "bucket_rationale": (
                "MACD above zero with falling histogram — SELL zero-cross "
                f"approaching. Trajectory {trajectory}."
            ),
        }

    drift_growing = (
        prev_macd_histogram is not None
        and (
            (macd < 0 and macd_histogram < 0 and macd_histogram < prev_macd_histogram - 1e-9)
            or (macd > 0 and macd_histogram > 0 and macd_histogram > prev_macd_histogram + 1e-9)
        )
    )
    if drift_growing:
        return {
            "bucket": "drifting",
            "trajectory": "deteriorating",
            "proximity_pct": round(max(0.0, proximity - 25.0), 1),
            "bucket_rationale": (
                "MACD and histogram moving further from zero — favourable "
                "conditions fading."
            ),
        }

    return {
        "bucket": "neutral",
        "trajectory": trajectory,
        "proximity_pct": round(proximity, 1),
        "bucket_rationale": "Outside favourable zone; awaiting clearer momentum signal.",
    }


def classify_status_bucket(
    *,
    has_position: bool,
    status: Optional[str],
) -> dict[str, Any]:
    """Status-string mapper for agents that classify by labels not numerics.

    The NSE Strategy 2 (Index MP), Fractal MP and Directional Options stacks
    don't carry raw MACD/histogram on every lane row — they classify by a
    text status. This helper maps those strings into the same bucket
    vocabulary so the frontend can render the three lists uniformly.
    """
    if has_position:
        return {
            "bucket": "active",
            "trajectory": None,
            "proximity_pct": 100.0,
            "bucket_rationale": "Position already open for this underlying.",
        }
    label = (status or "").strip().lower()
    ready = {"entry-ready", "ready", "execute", "armed"}
    favourable = {"trend-aligned", "watching", "monitoring", "near-trigger", "tracking"}
    drifting = {"avoid", "rejected", "drifting", "fade", "exit-watch"}
    if label in ready:
        return {
            "bucket": "ready",
            "trajectory": None,
            "proximity_pct": 100.0,
            "bucket_rationale": "All entry conditions met; queued for execution.",
        }
    if label in favourable:
        return {
            "bucket": "favourable",
            "trajectory": "improving",
            "proximity_pct": 60.0,
            "bucket_rationale": f"Lane status `{status}` indicates a favourable setup is forming.",
        }
    if label in drifting:
        return {
            "bucket": "drifting",
            "trajectory": "deteriorating",
            "proximity_pct": 15.0,
            "bucket_rationale": f"Lane status `{status}` indicates the setup is fading.",
        }
    return {
        "bucket": "neutral",
        "trajectory": None,
        "proximity_pct": 0.0,
        "bucket_rationale": f"Lane status `{status}` does not match a tracked bucket.",
    }
