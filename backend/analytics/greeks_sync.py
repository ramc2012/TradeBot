"""Greeks confluence scoring for option-premium expansion research."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class GreeksSyncConfig:
    """Config for the Greeks confluence signal."""

    delta_lookback_bars: int = 3
    iv_lookback_minutes: int = 30
    gamma_threshold: float = 0.005
    delta_momentum_target: float = 0.12
    theta_ratio_threshold: float = 2.0
    score_threshold: float = 70.0
    strong_score_threshold: float = 85.0
    macd_confirmation_window: int = 3

    delta_weight: float = 30.0
    gamma_weight: float = 20.0
    vega_weight: float = 25.0
    theta_weight: float = 15.0


def infer_bar_minutes(df: pd.DataFrame, default: int = 30) -> int:
    """Infer the dominant bar size from the time column."""
    if "time" not in df.columns or df["time"].empty:
        return default

    times = pd.to_datetime(df["time"], utc=True, errors="coerce").dropna().sort_values()
    if len(times) < 2:
        return default

    diffs = times.diff().dt.total_seconds().dropna() / 60.0
    diffs = diffs[diffs > 0]
    if diffs.empty:
        return default

    return max(1, int(round(float(diffs.median()))))


def label_sync_score_bucket(
    score: float,
    *,
    threshold: float = 70.0,
    strong_threshold: float = 85.0,
) -> str:
    """Bucketize a Greeks Sync score for reporting."""
    if pd.isna(score):
        return "score_unavailable"
    if score >= strong_threshold:
        return "score_85_plus"
    if score >= threshold:
        return "score_70_84"
    if score >= 50.0:
        return "score_50_69"
    return "score_below_50"


def label_theta_ratio_bucket(theta_ratio: float) -> str:
    """Bucketize the theta overwhelm ratio for breakdown tables."""
    if pd.isna(theta_ratio):
        return "theta_unknown"
    if theta_ratio >= 3.0:
        return "theta_overwhelmed"
    if theta_ratio >= 1.5:
        return "theta_competing"
    return "theta_dominant"


def compute_greeks_sync_frame(
    df: pd.DataFrame,
    option_type: str,
    *,
    config: Optional[GreeksSyncConfig] = None,
    bar_minutes: Optional[int] = None,
) -> pd.DataFrame:
    """
    Score each bar for multi-Greek premium-expansion confluence.

    Required columns:
      time, iv, delta, gamma, theta, vega, underlying_price
    """
    cfg = config or GreeksSyncConfig()
    required = {"iv", "delta", "gamma", "theta", "vega", "underlying_price"}
    missing = required.difference(df.columns)
    if missing:
        missing_cols = ", ".join(sorted(missing))
        raise ValueError(f"Missing required Greeks Sync columns: {missing_cols}")

    out = df.copy()
    direction = 1.0 if option_type.upper() == "CE" else -1.0
    resolved_bar_minutes = bar_minutes or infer_bar_minutes(out)
    iv_lookback_bars = max(
        1,
        int(round(cfg.iv_lookback_minutes / max(resolved_bar_minutes, 1))),
    )
    bar_days = resolved_bar_minutes / (60.0 * 24.0)

    out["aligned_delta"] = direction * pd.to_numeric(out["delta"], errors="coerce")
    out["aligned_underlying_move"] = (
        direction * pd.to_numeric(out["underlying_price"], errors="coerce").diff()
    )
    out["delta_momentum"] = out["aligned_delta"] - out["aligned_delta"].shift(
        cfg.delta_lookback_bars
    )
    out["gamma_level"] = pd.to_numeric(out["gamma"], errors="coerce").abs()
    out["iv_change_pct_points"] = (
        pd.to_numeric(out["iv"], errors="coerce")
        - pd.to_numeric(out["iv"], errors="coerce").shift(iv_lookback_bars)
    ) * 100.0

    favorable_move = out["aligned_underlying_move"].clip(lower=0.0).fillna(0.0)
    aligned_delta_prev = out["aligned_delta"].shift(1).clip(lower=0.0).fillna(0.0)

    out["directional_contribution"] = aligned_delta_prev * favorable_move
    out["convexity_contribution"] = (
        0.5 * out["gamma_level"].fillna(0.0) * favorable_move.pow(2)
    )
    out["vega_iv_contribution"] = (
        pd.to_numeric(out["vega"], errors="coerce").clip(lower=0.0).fillna(0.0)
        * out["iv_change_pct_points"].clip(lower=0.0).fillna(0.0)
    )
    out["theta_bar_drag"] = (
        pd.to_numeric(out["theta"], errors="coerce").abs().fillna(0.0) * bar_days
    )

    theta_drag = out["theta_bar_drag"].replace(0.0, np.nan)
    out["directional_overwhelm_ratio"] = (
        (out["directional_contribution"] + out["convexity_contribution"]) / theta_drag
    )
    out["vega_overwhelm_ratio"] = out["vega_iv_contribution"] / theta_drag
    out["theta_overwhelm_ratio"] = (
        (
            out["directional_contribution"]
            + out["convexity_contribution"]
            + out["vega_iv_contribution"]
        )
        / theta_drag
    )

    delta_signal_strength = (
        out["delta_momentum"].clip(lower=0.0).fillna(0.0) / cfg.delta_momentum_target
    ).clip(upper=1.0)
    directional_support = (
        out["directional_overwhelm_ratio"].fillna(0.0) / cfg.theta_ratio_threshold
    ).clip(lower=0.0, upper=1.0)

    out["delta_score"] = (
        cfg.delta_weight * delta_signal_strength * directional_support
    )
    out["gamma_score"] = cfg.gamma_weight * (
        out["gamma_level"].fillna(0.0) / cfg.gamma_threshold
    ).clip(lower=0.0, upper=1.0)
    out["vega_score"] = cfg.vega_weight * (
        out["vega_overwhelm_ratio"].fillna(0.0) / cfg.theta_ratio_threshold
    ).clip(lower=0.0, upper=1.0)
    out["theta_score"] = cfg.theta_weight * (
        out["theta_overwhelm_ratio"].fillna(0.0) / cfg.theta_ratio_threshold
    ).clip(lower=0.0, upper=1.0)

    required_mask = out[list(required)].notna().all(axis=1)
    out["greeks_sync_score"] = np.where(
        required_mask,
        out[["delta_score", "gamma_score", "vega_score", "theta_score"]]
        .sum(axis=1)
        .round(4),
        0.0,
    )
    out["greeks_sync_ready"] = out["greeks_sync_score"] >= cfg.score_threshold
    out["greeks_sync_strong"] = out["greeks_sync_score"] >= cfg.strong_score_threshold
    out["greeks_sync_signal"] = out["greeks_sync_ready"] & (
        out["greeks_sync_score"].shift(1).fillna(0.0) < cfg.score_threshold
    )
    out["greeks_sync_strength"] = np.where(
        out["greeks_sync_strong"],
        "strong",
        np.where(out["greeks_sync_ready"], "ready", "none"),
    )
    out["greeks_sync_score_bucket"] = out["greeks_sync_score"].apply(
        lambda score: label_sync_score_bucket(
            float(score),
            threshold=cfg.score_threshold,
            strong_threshold=cfg.strong_score_threshold,
        )
    )
    out["theta_ratio_bucket"] = out["theta_overwhelm_ratio"].apply(
        lambda value: label_theta_ratio_bucket(float(value))
        if not pd.isna(value)
        else "theta_unknown"
    )

    numeric_columns = [
        "aligned_delta",
        "aligned_underlying_move",
        "delta_momentum",
        "gamma_level",
        "iv_change_pct_points",
        "directional_contribution",
        "convexity_contribution",
        "vega_iv_contribution",
        "theta_bar_drag",
        "directional_overwhelm_ratio",
        "vega_overwhelm_ratio",
        "theta_overwhelm_ratio",
        "delta_score",
        "gamma_score",
        "vega_score",
        "theta_score",
        "greeks_sync_score",
    ]
    out[numeric_columns] = (
        out[numeric_columns]
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0.0)
        .round(6)
    )
    return out
