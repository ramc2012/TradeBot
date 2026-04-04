"""MACD Quadrant System — simultaneous CE + PE regime detection.

For each underlying+expiry, compute MACD on BOTH the ATM CE and ATM PE
option premium 30-min candles.  The combination of their signs defines
the market regime:

    CE MACD ≥ 0 + PE MACD < 0  → BULLISH  (buy CE only)
    CE MACD < 0 + PE MACD ≥ 0  → BEARISH  (buy PE only)
    CE MACD < 0 + PE MACD < 0  → DEAD     (no directional trade)
    CE MACD ≥ 0 + PE MACD ≥ 0  → IV_SPIKE (both surging, rare 0.2%)

Validated on 1,806 CE+PE cycle pairs (§3 of STRATEGY_DOCUMENT.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from analysis.macd_engine import compute_macd
from agent.strategy_config import (
    MACD_FAST,
    MACD_SLOW,
    MACD_SIGNAL,
    MACD_MIN_BARS,
    REGIME_BULLISH,
    REGIME_BEARISH,
    REGIME_DEAD,
    REGIME_IV_SPIKE,
)


@dataclass
class QuadrantResult:
    """Result of a quadrant check for a single underlying+expiry."""

    underlying: str
    expiry: str
    regime: str                       # one of REGIME_* constants
    ce_macd_value: Optional[float]    # latest MACD line value for CE
    pe_macd_value: Optional[float]    # latest MACD line value for PE
    ce_has_zero_cross: bool           # CE MACD just crossed above zero
    pe_has_zero_cross: bool           # PE MACD just crossed below zero
    ce_macd_line: Optional[list]      # full MACD line for exit monitoring
    pe_macd_line: Optional[list]      # full MACD line for exit monitoring


def compute_quadrant(
    ce_closes: list[float],
    pe_closes: list[float],
    underlying: str = "",
    expiry: str = "",
) -> QuadrantResult:
    """Determine the MACD quadrant from ATM CE and PE close series.

    Parameters
    ----------
    ce_closes : list[float]
        30-min closes for the ATM CE option (oldest first).
    pe_closes : list[float]
        30-min closes for the ATM PE option (oldest first).

    Returns
    -------
    QuadrantResult with regime classification and signal flags.
    """
    ce_macd_val: Optional[float] = None
    pe_macd_val: Optional[float] = None
    ce_cross = False
    pe_cross = False
    ce_line = None
    pe_line = None

    # Compute CE MACD
    if len(ce_closes) >= MACD_MIN_BARS:
        macd_l, _, _ = compute_macd(ce_closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        ce_line = macd_l
        curr = macd_l[-1]
        prev = macd_l[-2] if len(macd_l) >= 2 else None
        if curr is not None:
            ce_macd_val = float(curr)
        if prev is not None and curr is not None:
            ce_cross = (prev <= 0 < curr)

    # Compute PE MACD
    if len(pe_closes) >= MACD_MIN_BARS:
        macd_l, _, _ = compute_macd(pe_closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        pe_line = macd_l
        curr = macd_l[-1]
        prev = macd_l[-2] if len(macd_l) >= 2 else None
        if curr is not None:
            pe_macd_val = float(curr)
        # PE signal: MACD crosses below zero (put premium starts rising)
        if prev is not None and curr is not None:
            pe_cross = (prev >= 0 > curr)

    # Classify regime
    regime = _classify_regime(ce_macd_val, pe_macd_val)

    return QuadrantResult(
        underlying=underlying,
        expiry=expiry,
        regime=regime,
        ce_macd_value=ce_macd_val,
        pe_macd_value=pe_macd_val,
        ce_has_zero_cross=ce_cross,
        pe_has_zero_cross=pe_cross,
        ce_macd_line=ce_line,
        pe_macd_line=pe_line,
    )


def _classify_regime(
    ce_macd: Optional[float],
    pe_macd: Optional[float],
) -> str:
    """Map CE/PE MACD sign pair to a regime label."""
    if ce_macd is None or pe_macd is None:
        return REGIME_DEAD  # insufficient data = treat as dead

    ce_positive = ce_macd >= 0
    pe_positive = pe_macd >= 0

    if ce_positive and not pe_positive:
        return REGIME_BULLISH
    if not ce_positive and pe_positive:
        return REGIME_BEARISH
    if not ce_positive and not pe_positive:
        return REGIME_DEAD
    return REGIME_IV_SPIKE  # both positive — rare


def check_macd_death_signal(
    macd_line: list[Optional[float]],
    option_type: str,
) -> bool:
    """Check if the held position's MACD has reversed (death signal).

    For CE: MACD crosses back below zero → exit
    For PE: MACD crosses back above zero → exit

    This should only trigger an exit if the position is already in profit
    (checked by the caller, typically ≥ +30%).
    """
    if len(macd_line) < 2:
        return False

    curr = macd_line[-1]
    prev = macd_line[-2]
    if curr is None or prev is None:
        return False

    if option_type == "CE":
        return prev >= 0 > curr   # crossed below zero
    else:  # PE
        return prev <= 0 < curr   # crossed above zero (put momentum died)
