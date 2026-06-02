"""Options intelligence layer: translate directional judgement into an option expression."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

import numpy as np

OptionAction = Literal["buy_atm", "buy_itm", "debit_spread", "avoid"]


@dataclass(frozen=True)
class OptionExpression:
    action: OptionAction
    side: Literal["call", "put", "none"]
    moneyness: Literal["ATM", "ITM", "OTM", "none"]
    expiry: Literal["weekly", "next_weekly", "none"]
    iv_risk: float
    liquidity_score: float
    reasons: list[str]


def select_option_expression(
    direction: str,
    features: Mapping[str, object],
    *,
    edge_score: float,
) -> OptionExpression:
    if direction not in {"up", "down"}:
        return OptionExpression("avoid", "none", "none", "none", 1.0, 0.0, ["no directional edge"])

    iv_level = _num(features, "o_iv_level", 0.18)
    iv_change = _num(features, "o_iv_change")
    dte = _num(features, "c_days_to_weekly_expiry", 3)
    ce_vol = _num(features, "o_ce_volume_z")
    pe_vol = _num(features, "o_pe_volume_z")
    volume_score = float(np.clip(0.5 + 0.1 * max(ce_vol, pe_vol), 0, 1))
    iv_risk = float(np.clip((iv_level - 0.18) * 2.5 + max(0, iv_change) * 5.0, 0, 1))
    side = "call" if direction == "up" else "put"
    reasons: list[str] = []
    if dte <= 0 and edge_score < 0.7:
        return OptionExpression("avoid", side, "none", "none", iv_risk, volume_score, ["expiry-day edge too weak"])
    if volume_score < 0.25:
        return OptionExpression("avoid", side, "none", "none", iv_risk, volume_score, ["option liquidity too weak"])
    if iv_risk >= 0.65 and edge_score < 0.8:
        reasons.append("high IV favors spread over naked buy")
        return OptionExpression("debit_spread", side, "ATM", "weekly", iv_risk, volume_score, reasons)
    if edge_score >= 0.75 and iv_risk < 0.55:
        return OptionExpression("buy_itm", side, "ITM", "weekly", iv_risk, volume_score, ["strong edge, control theta"])
    return OptionExpression("buy_atm", side, "ATM", "weekly" if dte >= 1 else "next_weekly", iv_risk, volume_score, ["standard directional expression"])


def _num(row: Mapping[str, object], key: str, default: float = 0.0) -> float:
    value = row.get(key, default)
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default
