"""Live calibration and drift monitoring."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class DriftSnapshot:
    expected_r_error: float
    mfe_error: float
    mae_error: float
    confidence_error: float
    slippage_error: float
    feature_drift_score: float


@dataclass(frozen=True)
class DriftReport:
    drift_score: float
    action: str
    reasons: list[str]


def compute_drift_report(
    realized: pd.DataFrame,
    *,
    feature_drift_score: float = 0.0,
) -> DriftReport:
    """Compute a control action from prediction-vs-realized calibration data."""
    if realized.empty:
        return DriftReport(0.0, "normal", ["no realized data yet"])
    expected_err = _mae(realized, "pred_expected_r", "realized_r")
    mfe_err = _mae(realized, "pred_mfe_r", "realized_mfe_r")
    mae_err = _mae(realized, "pred_mae_r", "realized_mae_r")
    conf_err = _mae(realized, "confidence", "is_winner")
    slip_err = _mae(realized, "pred_slippage_atr", "realized_slippage_atr")
    score = float(np.clip(np.nanmean([expected_err, mfe_err, mae_err, conf_err, slip_err, feature_drift_score]), 0, 1))
    reasons = []
    if conf_err > 0.35:
        reasons.append("confidence calibration break")
    if slip_err > 0.25:
        reasons.append("slippage forecast error")
    if feature_drift_score > 0.5:
        reasons.append("feature distribution drift")
    if score >= 0.75:
        action = "disable_automation"
    elif score >= 0.5:
        action = "reduce_size"
    elif score >= 0.35:
        action = "paper_mode"
    else:
        action = "normal"
    return DriftReport(score, action, reasons or ["calibration within bounds"])


def _mae(df: pd.DataFrame, pred: str, actual: str) -> float:
    if pred not in df or actual not in df:
        return 0.0
    err = (pd.to_numeric(df[pred], errors="coerce") - pd.to_numeric(df[actual], errors="coerce")).abs()
    value = float(err.dropna().mean()) if err.notna().any() else 0.0
    return value if np.isfinite(value) else 0.0
