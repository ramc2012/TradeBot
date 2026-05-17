"""TP Delta harmonic speed calculation."""
from __future__ import annotations

import statistics
from typing import Any

import pandas as pd

from gann_tp_delta.anchors import pivot_vectors
from gann_tp_delta.schemas import HarmonicSpeed


def harmonic_speed(
    frame: pd.DataFrame,
    *,
    mode: str,
    anchor_config: dict[str, Any],
    scaling_config: dict[str, Any],
    manual_h: float | None = None,
) -> tuple[HarmonicSpeed, list[dict[str, Any]]]:
    vectors = pivot_vectors(frame, anchor_config)
    abs_tpds = [float(vector["abs_tpd"]) for vector in vectors if float(vector.get("abs_tpd") or 0.0) > 0]
    normalized = str(mode or scaling_config["default_h_mode"]).lower()
    min_h = float(scaling_config["min_h"])
    if normalized == "manual":
        value = max(float(manual_h if manual_h is not None else scaling_config["manual_h"]), min_h)
        return HarmonicSpeed("manual", value, "points/bar", 1, "manual input"), vectors
    if normalized == "average_tpd" and abs_tpds:
        value = max(float(statistics.fmean(abs_tpds)), min_h)
        return HarmonicSpeed("average_tpd", value, "points/bar", len(abs_tpds), "confirmed pivot vectors"), vectors
    if normalized == "atr":
        atr = float(frame.iloc[-1].get("atr", 0.0)) if not frame.empty else 0.0
        value = max(atr * float(scaling_config["atr_multiplier"]), min_h)
        return HarmonicSpeed("atr", value, "points/bar", 1 if atr else 0, "latest ATR"), vectors
    if abs_tpds:
        value = max(float(statistics.median(abs_tpds)), min_h)
        return HarmonicSpeed("median_tpd", value, "points/bar", len(abs_tpds), "confirmed pivot vectors"), vectors
    fallback = max(float(scaling_config["manual_h"]), min_h)
    return HarmonicSpeed("fallback_manual", fallback, "points/bar", 0, "no confirmed pivot vectors"), vectors
