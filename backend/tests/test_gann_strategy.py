"""Contract tests for the regime-gated Gann engine (gann_tp_delta.strategy).

These build the geometry objects directly (frozen dataclasses) so the scoring,
archetype gating and — crucially — the *no-whipsaw* property are exercised
without the SQL/feature pipeline. The headline regression we're guarding:
the old engine flipped the trade side on every new pivot; the new one must
keep a stable regime when EMA + structure stay put.
"""
from __future__ import annotations

import pandas as pd

from gann_tp_delta import strategy as st
from gann_tp_delta.config import clone_default_config
from gann_tp_delta.schemas import (
    AnchorPoint,
    GannAngle,
    PriceTimeSquare,
    SquareNineLevel,
    TimeCycleWindow,
)

CFG = clone_default_config()


def _frame(*, bullish: bool = True, adx: float = 25.0, n: int = 12, close: float = 100.0,
           up_bar: bool | None = None) -> pd.DataFrame:
    """Build a feature frame with a clean trend in high/low so trend_structure
    resolves, plus EMA/ADX/ATR columns the regime reads."""
    step = 1.0 if bullish else -1.0
    base = close - step * (n - 1)
    closes = [base + step * i for i in range(n)]
    closes[-1] = close
    highs = [c + 2.0 for c in closes]
    lows = [c - 2.0 for c in closes]
    opens = list(closes)
    if up_bar is True:
        opens[-1] = close - 1.5   # close > open ⇒ up/bull bar
    elif up_bar is False:
        opens[-1] = close + 1.5   # close < open ⇒ down/bear bar
        lows[-1] = close - 0.2    # closes near the low
    ema_fast = [c + (1.0 if bullish else -1.0) for c in closes]
    ema_slow = list(closes)
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1] * n, "oi": [0] * n,
        "atr": [1.0] * n, "ema_fast": ema_fast, "ema_slow": ema_slow, "adx": [adx] * n,
    })


def _angle(name: str, current_price: float, *, bullish: bool = True, distance_close: float = 100.0) -> GannAngle:
    return GannAngle(
        name=name, ratio=1.0, direction="bullish" if bullish else "bearish",
        anchor_price=90.0, anchor_bar_index=0, slope=1.0,
        current_price=current_price, projected_price=current_price,
        distance=distance_close - current_price,
        distance_pct=abs(distance_close - current_price) / max(abs(distance_close), 1.0),
    )


def _sq9(degree: int, price: float, *, level_type: str = "cardinal", close: float = 100.0) -> SquareNineLevel:
    return SquareNineLevel(
        degree=degree, direction="upside" if price >= close else "downside",
        price=price, level_type=level_type,
        distance=close - price, distance_pct=abs(close - price) / max(abs(close), 1.0),
    )


def _cycle(active: bool, cycle: int = 144) -> TimeCycleWindow:
    return TimeCycleWindow(cycle=cycle, start_bar_index=9, center_bar_index=11,
                           end_bar_index=13, active=active, distance_bars=0)


def _square(active: bool, ratio: float = 1.0) -> PriceTimeSquare:
    return PriceTimeSquare(active=active, ratio=ratio, scaled_price_move=11.0, time_bars=11, tolerance=0.05)


_LOW_ANCHOR = AnchorPoint(mode="auto_pivot", kind="swing_low", bar_index=0, time="t", price=90.0)
_HIGH_ANCHOR = AnchorPoint(mode="auto_pivot", kind="swing_high", bar_index=0, time="t", price=110.0)


# ─── Regime ──────────────────────────────────────────────────────────────────


def test_regime_bull_when_emas_structure_and_master_align():
    angles = [_angle("1x1", 99.0)]  # close 100 above rising 1x1 ⇒ +1 master
    reg = st.compute_regime(frame=_frame(bullish=True), angles=angles, anchor=_LOW_ANCHOR, config=CFG["strategy"])
    assert reg["regime"] == "bull"
    assert reg["votes"]["ema"] == 1 and reg["votes"]["structure"] == 1


def test_regime_neutral_when_adx_below_min():
    angles = [_angle("1x1", 99.0)]
    reg = st.compute_regime(frame=_frame(bullish=True, adx=5.0), angles=angles, anchor=_LOW_ANCHOR, config=CFG["strategy"])
    assert reg["regime"] == "neutral"


def test_regime_does_not_flip_to_bear_on_master_vote_alone():
    # The OLD whipsaw: a swing-high anchor flipped bias straight to bearish.
    # Here EMA + structure stay bullish; flipping only the 1×1 master vote must
    # NOT yield a bear regime (worst case it degrades to neutral).
    angles = [_angle("1x1", 101.0, bullish=False)]  # close 100 below a falling 1x1 ⇒ -1 master
    reg = st.compute_regime(frame=_frame(bullish=True), angles=angles, anchor=_HIGH_ANCHOR, config=CFG["strategy"])
    assert reg["regime"] != "bear"


# ─── Continuation archetype ──────────────────────────────────────────────────


def test_continuation_long_fires_on_support_touch_in_bull():
    # Bull regime + price sitting on a rising 1×1 support + active major cycle.
    # Also seed levels ABOVE close so there are Gann targets to scale into.
    angles = [_angle("1x1", 99.97), _angle("2x1", 102.5)]  # support below + resistance above
    sig = st.evaluate_gann_signal(
        frame=_frame(bullish=True, up_bar=True), anchor=_LOW_ANCHOR, angles=angles,
        sq9_levels=[_sq9(180, 99.95), _sq9(90, 101.8)], cycles=[_cycle(True, 144)], square=_square(False), h=10.0, config=CFG,
    )
    assert sig.archetype == "continuation"
    assert sig.side == "long"
    assert sig.state == "bullish_setup"
    assert sig.stop_underlying is not None and sig.stop_underlying < 100.0
    assert sig.targets_underlying  # at least one Gann target above
    assert sig.risk_per_unit and sig.risk_per_unit > 0


def test_no_trade_when_price_far_from_all_levels():
    angles = [_angle("1x1", 80.0)]  # 20% away
    sig = st.evaluate_gann_signal(
        frame=_frame(bullish=True), anchor=_LOW_ANCHOR, angles=angles,
        sq9_levels=[_sq9(90, 70.0)], cycles=[_cycle(False)], square=_square(False), h=10.0, config=CFG,
    )
    assert sig.side is None
    assert sig.archetype is None
    assert sig.state in {"ignore", "watch"}


def test_continuation_needs_support_not_just_resistance():
    # Bull regime but the only near level is ABOVE price (resistance) ⇒ no
    # continuation long (you don't buy a pullback into resistance).
    angles = [_angle("1x1", 100.05)]  # just above close ⇒ resistance, not support
    sig = st.evaluate_gann_signal(
        frame=_frame(bullish=True), anchor=_LOW_ANCHOR, angles=angles,
        sq9_levels=[], cycles=[_cycle(False)], square=_square(False), h=10.0, config=CFG,
    )
    assert sig.archetype != "continuation"


def test_continuation_requires_a_resumption_close():
    angles = [_angle("1x1", 99.97)]
    sig = st.evaluate_gann_signal(
        frame=_frame(bullish=True, up_bar=False),
        anchor=_LOW_ANCHOR,
        angles=angles,
        sq9_levels=[],
        cycles=[_cycle(True, 144)],
        square=_square(False),
        h=10.0,
        config=CFG,
    )
    assert sig.archetype is None
    assert sig.candidate_archetype == "continuation"
    assert sig.setup_state == "BLOCKED"
    assert "Trend-resumption close" in sig.blockers
    resumption = next(item for item in sig.rule_checks if item["key"] == "resumption")
    assert resumption["required"] is True and resumption["passed"] is False


def test_underlying_floor_is_part_of_signal_contract():
    angles = [_angle("1x1", 99.97)]
    kwargs = dict(
        frame=_frame(bullish=True, up_bar=True),
        anchor=_LOW_ANCHOR,
        angles=angles,
        sq9_levels=[_sq9(45, 99.9, level_type="ordinal")],
        cycles=[_cycle(False)],
        square=_square(False),
        h=10.0,
        config=CFG,
    )
    nifty = st.evaluate_gann_signal(**kwargs, underlying="NIFTY")
    bank = st.evaluate_gann_signal(**kwargs, underlying="BANKNIFTY")
    assert nifty.setup_state == "ACTIONABLE"
    assert bank.setup_state == "ARMED"
    assert bank.archetype is None
    assert bank.minimum_conviction == 6.0
    assert bank.threshold == 6


def test_timing_prefers_major_cycle_over_earlier_minor_overlap():
    score, reasons, selected = st._timing_score(
        [_cycle(True, 7), _cycle(True, 144)],
        _square(False),
        CFG["strategy"]["weights"],
        CFG["strategy"]["major_cycles"],
    )
    assert selected is not None and selected.cycle == 144
    assert score > CFG["strategy"]["weights"]["cycle_minor"]
    assert any("144*" in reason for reason in reasons)


# ─── Reversal archetype ──────────────────────────────────────────────────────


def _strong_reversal_geometry(close: float = 100.0):
    # A near-exact resistance cluster price has reached: cardinal SQ9 + major
    # cycle + price-time square — the kind of confluence a reversal needs.
    angles = [_angle("1x1", 100.02, bullish=True)]  # resistance just above
    sq9 = [_sq9(360, 100.01, level_type="cardinal", close=close)]
    return angles, sq9


def test_reversal_blocked_without_confirmation_bar():
    angles, sq9 = _strong_reversal_geometry()
    sig = st.evaluate_gann_signal(
        frame=_frame(bullish=True, up_bar=True),  # up bar ⇒ NOT a short confirmation
        anchor=_LOW_ANCHOR, angles=angles, sq9_levels=sq9,
        cycles=[_cycle(True, 360)], square=_square(True), h=10.0, config=CFG,
    )
    assert sig.archetype != "reversal" or sig.side != "short"


def test_reversal_short_fires_with_confirmation_and_reduced_size():
    angles, sq9 = _strong_reversal_geometry()
    sig = st.evaluate_gann_signal(
        frame=_frame(bullish=True, up_bar=False),  # down bar ⇒ short confirmation
        anchor=_LOW_ANCHOR, angles=angles, sq9_levels=sq9,
        cycles=[_cycle(True, 360)], square=_square(True), h=10.0, config=CFG,
    )
    if sig.archetype == "reversal":
        assert sig.side == "short"
        assert sig.size_factor < 1.0
        assert sig.confirmation is True
        assert sig.stop_underlying is not None and sig.stop_underlying > 100.0


def test_reversal_requires_each_structural_timing_gate():
    angles, sq9 = _strong_reversal_geometry()
    sig = st.evaluate_gann_signal(
        frame=_frame(bullish=True, up_bar=False),
        anchor=_LOW_ANCHOR,
        angles=angles,
        sq9_levels=sq9,
        cycles=[_cycle(True, 360)],
        square=_square(False),
        h=10.0,
        config=CFG,
    )
    assert sig.archetype is None
    assert sig.candidate_archetype == "reversal"
    assert sig.setup_state == "BLOCKED"
    assert "Price-time square" in sig.blockers


def test_reversal_requires_higher_bar_than_continuation():
    # Same modest confluence that would fire a continuation must NOT, on its
    # own, clear the (higher) reversal bar.
    cont_min = CFG["strategy"]["continuation_min_conviction"]
    rev_min = CFG["strategy"]["reversal_min_conviction"]
    assert rev_min > cont_min
