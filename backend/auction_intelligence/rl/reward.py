"""Reward computation for MP trade outcomes.

Rewards are normalized to roughly [-2.0, +3.0] range:
  - Win (target reached):  R:R ratio (e.g. 2.0 for a 2:1 trade)
  - Loss (stop hit):      -1.0 (flat regardless of stop size — risk is risk)
  - Confidence bonus:     +0.2 for high confidence entries (≥0.70)
  - Confidence penalty:   -0.1 for low confidence entries (<0.60)
"""
from __future__ import annotations

from typing import Optional


def compute_reward(
    *,
    action: str,
    entry_price: Optional[float],
    stop_price: Optional[float],
    target_price: Optional[float],
    outcome: str,  # "win" | "loss" | "timeout"
    exit_price: Optional[float] = None,
    confidence: float = 0.62,
) -> float:
    """Compute a scalar reward from a completed paper trade.

    Args:
        action:       "LONG" or "SHORT"
        entry_price:  fill price
        stop_price:   hard stop price
        target_price: profit target price
        outcome:      "win" if target hit, "loss" if stop hit, "timeout" if neither
        exit_price:   actual exit price (for timeout calculation)
        confidence:   agent confidence at entry

    Returns:
        Scalar reward for Q-learning update.
    """
    if not entry_price or entry_price <= 0:
        return 0.0

    if action == "LONG":
        risk = entry_price - (stop_price or entry_price)
        reward_pts = (target_price or entry_price) - entry_price
    elif action == "SHORT":
        risk = (stop_price or entry_price) - entry_price
        reward_pts = entry_price - (target_price or entry_price)
    else:
        return 0.0

    risk = max(risk, 0.01)  # avoid divide-by-zero

    if outcome == "win":
        rr = reward_pts / risk
        base_reward = min(rr, 4.0)  # cap at 4× R:R
    elif outcome == "loss":
        base_reward = -1.0
    else:
        # Timeout: partial reward based on exit vs entry
        if exit_price is not None:
            if action == "LONG":
                pts = exit_price - entry_price
            else:
                pts = entry_price - exit_price
            base_reward = pts / risk
            base_reward = max(-1.0, min(base_reward, 2.0))
        else:
            base_reward = 0.0

    # Confidence adjustment: reward high-confidence wins, penalize low-confidence losses
    if outcome == "win" and confidence >= 0.70:
        base_reward += 0.20
    elif outcome == "loss" and confidence < 0.60:
        base_reward -= 0.10

    return round(base_reward, 4)


def compute_proxy_reward(
    *,
    action: str,
    entry_price: Optional[float],
    stop_price: Optional[float],
    target_price: Optional[float],
    confidence: float = 0.62,
) -> float:
    """Compute a proxy reward from entry/stop/target alone (no outcome data).

    Used when training from shadow observations that don't have actual outcomes.
    Formula: E[reward] = p_win * R:R - p_loss * 1.0
    where p_win ≈ confidence (rough approximation).

    This teaches the RL agent to prefer:
    - High confidence entries
    - Good R:R ratios
    Without needing real outcome data.
    """
    if not entry_price or entry_price <= 0 or not stop_price or not target_price:
        return 0.0

    if action == "LONG":
        risk = entry_price - stop_price
        reward_pts = target_price - entry_price
    elif action == "SHORT":
        risk = stop_price - entry_price
        reward_pts = entry_price - target_price
    else:
        return 0.0

    if risk <= 0:
        return 0.0

    rr = reward_pts / risk
    rr = max(0.0, min(rr, 4.0))

    # Expected value approximation
    p_win = max(0.0, min(confidence, 1.0))
    p_loss = 1.0 - p_win
    expected = p_win * rr - p_loss * 1.0

    return round(expected, 4)
