"""Single source of truth for directional long-option exit rules.

The live paper book (`paper.py`) and the bounded backtest (`backtest.py`) both
call `evaluate_exit` so the exit regime they enforce is *identical*. Before this
existed, the full ladder (premium stop, underlying invalidation, profit target,
trailing take-profit, expiry guard, time/horizon stop) lived only in the
backtest, and the live book held positions until the signal merely went flat or
flipped — a long option could bleed to zero with no protective stop. Keeping one
definition here prevents the live book and the backtest from drifting apart.

The function is pure: callers compute the inputs (premiums, spot, days-to-expiry,
held bars) from their own position representation and pass them in.
"""
from __future__ import annotations

from typing import Optional


# Conventional thresholds for the secondary time-based stops. The premium / target
# / trail / expiry thresholds are supplied by the caller from the risk config so
# they stay tunable in one place.
TIME_STOP_RETURN_CEILING = 0.12  # a stalled trade past its horizon with <12% gain is cut
HORIZON_EXPIRY_MULTIPLE = 1.5    # hard time-out at 1.5× the expected horizon


def evaluate_exit(
    *,
    option_type: str,
    current_premium: float,
    entry_basis_premium: float,
    return_basis_premium: float,
    peak_premium: float,
    current_spot: float,
    stop_underlying: Optional[float],
    expiry_days_left: Optional[int],
    held_bars: int,
    max_horizon_bars: int,
    planned_stop_pct: float,
    profit_target_pct: float,
    trail_giveback_pct: float,
    expiry_guard_days: float,
) -> Optional[str]:
    """Return an exit reason string, or None to hold.

    Order matters: loss protection (premium stop, underlying invalidation) is
    checked before profit-taking so a violent reversal exits at the stop, not the
    target. `entry_basis_premium` anchors the stop/target/trail levels (the entry
    mark); `return_basis_premium` anchors the realised-return used by the time
    stop (the entry fill). For the live book the two are the same recorded entry.
    """
    entry_basis = max(float(entry_basis_premium), 1e-9)
    return_basis = max(float(return_basis_premium), 1e-9)
    stop_price = entry_basis * (1.0 - float(planned_stop_pct))
    target_price = entry_basis * (1.0 + float(profit_target_pct))
    current_return = (float(current_premium) - return_basis) / return_basis
    peak_return = (float(peak_premium) - entry_basis) / entry_basis

    # 1. Hard premium stop — the long option has lost `planned_stop_pct` of value.
    if float(current_premium) <= stop_price:
        return "premium_stop"
    # 2. Underlying invalidation — spot moved against the directional thesis.
    if stop_underlying is not None and float(current_spot) > 0.0:
        if option_type == "CE" and float(current_spot) <= float(stop_underlying):
            return "underlying_invalidation"
        if option_type == "PE" and float(current_spot) >= float(stop_underlying):
            return "underlying_invalidation"
    # 3. Profit target.
    if float(current_premium) >= target_price:
        return "target_hit"
    # 4. Trailing take-profit — gave back `trail_giveback_pct` from a peak that
    #    had already cleared the profit target.
    if peak_return >= float(profit_target_pct) and float(current_premium) <= float(peak_premium) * (
        1.0 - float(trail_giveback_pct)
    ):
        return "trail_take_profit"
    # 5. Expiry guard — never carry a long option into the last fraction of a day
    #    where theta/pin risk dominates.
    if expiry_days_left is not None and expiry_days_left <= float(expiry_guard_days):
        return "expiry_guard"
    # 6. Time/horizon stops — a trade that hasn't worked within its expected
    #    horizon is dead money bleeding theta.
    if max_horizon_bars > 0:
        if held_bars >= max_horizon_bars and current_return <= TIME_STOP_RETURN_CEILING:
            return "time_stop"
        if held_bars >= int(max_horizon_bars * HORIZON_EXPIRY_MULTIPLE):
            return "horizon_expired"
    return None
