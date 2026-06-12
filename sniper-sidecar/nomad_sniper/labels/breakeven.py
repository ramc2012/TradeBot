"""Calibrate `m_breakeven` — the ATR-multiple an ATM long must travel to clear theta + cost
over the holding horizon H (contract §8). This is the single number that gates every label,
so it is computed on arithmetic, not guessed.

The model:
  - You buy the ATM option (call for an up call, symmetric for down) at entry.
  - Over H minutes the underlying moves `m · ATR` points in your favour AND time decays by H.
  - `m_breakeven` is the smallest `m` for which the repriced option covers entry premium + cost.

Solved by bisection on `m` using one option-chain snapshot. Two input modes:
  - spot + IV + days-to-expiry           → BS directly
  - ATM straddle price + spot + DTE       → invert straddle to IV, then BS
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from nomad_sniper.utils.black_scholes import bs_price, implied_vol_from_straddle


@dataclass
class BreakevenResult:
    m_breakeven: float
    atr_points: float
    breakeven_move_points: float       # m_breakeven · ATR
    iv_used: float
    entry_premium: float
    exit_premium_at_breakeven: float
    cost_inr_per_unit: float
    horizon_minutes: int
    days_to_expiry: float
    spot: float
    opt_type: str
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def calibrate_m_breakeven(
    *,
    spot: float,
    atr_points: float,
    horizon_minutes: int,
    days_to_expiry: float,
    iv: float | None = None,
    straddle_price: float | None = None,
    cost_inr_per_unit: float = 4.0,
    risk_free: float = 0.065,
    opt_type: str = "call",
    m_hi: float = 5.0,
) -> BreakevenResult:
    """Return the ATR-multiple an ATM long needs to clear theta + cost over H.

    Provide either `iv` (annualized, e.g. 0.14) OR `straddle_price` (ATM straddle in points).
    """
    if atr_points <= 0:
        raise ValueError("atr_points must be positive.")
    if iv is None and straddle_price is None:
        raise ValueError("Provide either iv or straddle_price.")

    K = spot  # ATM
    T_entry = max(days_to_expiry, 1e-6) / 365.0
    T_exit = max(days_to_expiry - horizon_minutes / (60.0 * 24.0), 1e-6) / 365.0

    note = ""
    if iv is None:
        iv = implied_vol_from_straddle(straddle_price, spot, K, T_entry, risk_free)
        if iv is None:
            raise ValueError(
                "Could not invert straddle to IV (price out of [1%,200%] vol bracket). "
                "Check the straddle/spot/DTE inputs."
            )
        note = f"IV inferred from straddle={straddle_price:.2f} → {iv:.4f}"

    entry_premium = bs_price(spot, K, T_entry, risk_free, iv, opt_type)
    target = entry_premium + cost_inr_per_unit

    def exit_premium(m: float) -> float:
        # favourable move: up → spot rises (call); down handled symmetrically via put.
        moved_spot = spot + m * atr_points if opt_type == "call" else spot - m * atr_points
        return bs_price(moved_spot, K, T_exit, risk_free, iv, opt_type)

    # If even a huge move can't cover cost (deep theta vs tiny horizon), report m_hi capped.
    if exit_premium(m_hi) < target:
        note = (note + " | " if note else "") + (
            f"capped: even {m_hi} ATR does not clear cost over H "
            f"(theta dominates — shorten H or pick a longer-DTE expiry)"
        )
        m_be = m_hi
    else:
        lo, hi = 0.0, m_hi
        for _ in range(100):
            mid = 0.5 * (lo + hi)
            if exit_premium(mid) - target >= 0:
                hi = mid
            else:
                lo = mid
            if hi - lo < 1e-4:
                break
        m_be = 0.5 * (lo + hi)

    return BreakevenResult(
        m_breakeven=round(m_be, 4),
        atr_points=atr_points,
        breakeven_move_points=round(m_be * atr_points, 2),
        iv_used=round(iv, 4),
        entry_premium=round(entry_premium, 2),
        exit_premium_at_breakeven=round(exit_premium(m_be), 2),
        cost_inr_per_unit=cost_inr_per_unit,
        horizon_minutes=horizon_minutes,
        days_to_expiry=days_to_expiry,
        spot=spot,
        opt_type=opt_type,
        note=note,
    )
