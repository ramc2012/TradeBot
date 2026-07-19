"""Regime-gated Gann confluence engine (v2).

The legacy :func:`gann_tp_delta.signals.confluence_signal` set the trade bias
purely from the most-recent pivot type (swing-high ⇒ bearish, swing-low ⇒
bullish). Because confirmed pivots alternate high/low/high/low, the bias — and
therefore the paper position — flipped every few bars. On the live book that
produced 11 self-reversal exits out of 12 closes and a realised −₹9.7k.

This engine fixes that by separating three things the old code conflated:

1. **Regime** — a STABLE directional context from EMA(fast/slow) + swing
   structure + the 1×1 master angle, gated by ADX. It does not flip on a
   single new pivot.
2. **Archetype** — two explicit, auditable trade models:
     • *continuation*  — with-regime entry on a pullback to a rising/falling
       Gann support/resistance that holds.
     • *reversal*      — counter-regime (or range) entry only at a STRONG,
       near-exact confluence (cardinal SQ9 + major time cycle + price-time
       square) with a confirmation bar, traded at reduced size.
3. **Conviction** — a continuous score weighted by how *exactly* price sits on
   each Gann element and how *important* that element is, instead of the old
   binary +1-per-touch that handed out points for any 0.3 % near-miss.

The output is the existing :class:`ConfluenceSignal` (so the service / agent /
frontend keep working) enriched with regime/archetype/side/conviction/stop and
underlying-based stop & targets the agent uses for Gann-native exits.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from gann_tp_delta.geometry import angle_by_name
from gann_tp_delta.schemas import (
    AnchorPoint,
    ConfluenceSignal,
    GannAngle,
    PriceTimeSquare,
    SquareNineLevel,
    TimeCycleWindow,
)
from gann_tp_delta.signals import trend_structure


def _f(value: Any, default: float = float("nan")) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _prox(distance_pct: float, tolerance: float, weight: float) -> float:
    """Proximity-weighted contribution: `weight` when exact, 0 at the tolerance
    edge, 0 beyond. Linear in how close price sits to the level."""
    if tolerance <= 0 or distance_pct > tolerance:
        return 0.0
    return float(weight) * (1.0 - distance_pct / tolerance)


def _angle_weight(name: str, weights: dict[str, Any], major_angles: list[str]) -> float:
    if name == "1x1":
        return float(weights.get("angle_1x1", 2.0))
    if name in major_angles:
        return float(weights.get("angle_major", 1.0))
    return float(weights.get("angle_minor", 0.5))


# ─── Regime ────────────────────────────────────────────────────────────────


def compute_regime(
    *,
    frame: pd.DataFrame,
    angles: list[GannAngle],
    anchor: AnchorPoint,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Stable directional context. Returns regime ∈ {bull, bear, neutral},
    a 0..1 strength proxy, and the component votes for audit."""
    current = frame.iloc[-1]
    close = _f(current.get("close"))
    ema_fast = _f(current.get("ema_fast"))
    ema_slow = _f(current.get("ema_slow"))
    adx = _f(current.get("adx"))
    lookback = int(config.get("structure_lookback", 8))
    structure = trend_structure(frame, lookback)

    ema_vote = 0
    if math.isfinite(ema_fast) and math.isfinite(ema_slow):
        ema_vote = 1 if ema_fast > ema_slow else -1 if ema_fast < ema_slow else 0
    struct_vote = 1 if structure == "bullish" else -1 if structure == "bearish" else 0

    # 1×1 master angle: price holding above a rising 1×1 (bullish fan from a
    # swing low) is a bull vote; below a falling 1×1 (bearish fan) a bear vote.
    master = angle_by_name(angles, "1x1")
    master_vote = 0
    if master is not None and math.isfinite(close):
        bullish_fan = master.direction != "bearish"
        if bullish_fan and close >= master.current_price:
            master_vote = 1
        elif (not bullish_fan) and close <= master.current_price:
            master_vote = -1

    regime_score = ema_vote + struct_vote + master_vote
    min_score = int(config.get("regime_min_score", 2))
    adx_min = float(config.get("adx_trend_min", 18.0))
    adx_trending = math.isfinite(adx) and adx >= adx_min
    # If ADX is unavailable (early frame) fall back to a stricter vote count.
    if not math.isfinite(adx):
        adx_trending = abs(regime_score) >= max(min_score + 1, 3)

    regime = "neutral"
    if adx_trending and regime_score >= min_score:
        regime = "bull"
    elif adx_trending and regime_score <= -min_score:
        regime = "bear"

    strength = 0.0
    if regime != "neutral":
        strength = max(0.0, min(1.0, (adx / 40.0) if math.isfinite(adx) else 0.5))
    return {
        "regime": regime,
        "strength": round(strength, 3),
        "votes": {"ema": ema_vote, "structure": struct_vote, "master_1x1": master_vote},
        "regime_score": regime_score,
        "adx": round(adx, 2) if math.isfinite(adx) else None,
        "adx_trending": bool(adx_trending),
        "structure": structure,
    }


def _reversal_confirmation(frame: pd.DataFrame, side: str) -> bool:
    """A simple reversal candle in the trade direction on the last closed bar.

    short ⇒ a down/rejection bar (close < open, closes in lower half).
    long  ⇒ an up/rejection bar (close > open, closes in upper half).
    """
    if len(frame.index) < 2:
        return False
    cur = frame.iloc[-1]
    o, h, l, c = _f(cur.get("open")), _f(cur.get("high")), _f(cur.get("low")), _f(cur.get("close"))
    if not all(math.isfinite(x) for x in (o, h, l, c)) or h <= l:
        return False
    pos = (c - l) / (h - l)  # 0 = closed on low, 1 = closed on high
    if side == "short":
        return c < o and pos <= 0.45
    return c > o and pos >= 0.55


def _continuation_confirmation(frame: pd.DataFrame, side: str) -> bool:
    """Require the pullback bar to resume in the regime direction.

    A continuation is not actionable just because price is near a support or
    resistance line. The last closed bar must have a directional body and must
    not close behind the prior close.
    """
    if len(frame.index) < 2:
        return False
    cur, prev = frame.iloc[-1], frame.iloc[-2]
    o, c, prev_c = _f(cur.get("open")), _f(cur.get("close")), _f(prev.get("close"))
    if not all(math.isfinite(x) for x in (o, c, prev_c)):
        return False
    if side == "long":
        return c > o and c >= prev_c
    return c < o and c <= prev_c


# ─── Element scoring shared by both archetypes ───────────────────────────────


def _timing_score(
    cycles: list[TimeCycleWindow],
    square: PriceTimeSquare,
    weights: dict[str, Any],
    major_cycles: list[int],
) -> tuple[float, list[str], TimeCycleWindow | None]:
    score = 0.0
    reasons: list[str] = []
    active_cycles = [c for c in cycles if c.active]
    # Overlapping windows are common. Select the most important, closest cycle
    # rather than whichever happens to appear first in BAR_CYCLES.
    active = max(
        active_cycles,
        key=lambda c: (
            int(c.cycle) in major_cycles,
            -abs(int(c.distance_bars)),
            int(c.cycle),
        ),
        default=None,
    )
    if active is not None:
        is_major = int(active.cycle) in major_cycles
        w = float(weights.get("cycle_major" if is_major else "cycle_minor", 0.75))
        # Sharper when dead-centre of the window.
        span = max(active.end_bar_index - active.center_bar_index, 1)
        closeness = 1.0 - min(abs(active.distance_bars) / span, 1.0)
        score += w * (0.5 + 0.5 * closeness)
        reasons.append(f"cycle {active.cycle}{'*' if is_major else ''} active (Δ{active.distance_bars}b)")
    if square.active:
        w = float(weights.get("price_time_square", 2.0))
        sharp = max(0.0, 1.0 - abs(square.ratio - 1.0) / max(square.tolerance, 1e-9))
        score += w * (0.5 + 0.5 * sharp)
        reasons.append(f"price-time square (r={round(square.ratio, 3)})")
    return score, reasons, active


def _rule(key: str, label: str, passed: bool, detail: str, *, required: bool = True) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "passed": bool(passed),
        "required": bool(required),
        "detail": detail,
    }


def _candidate_blockers(checks: list[dict[str, Any]]) -> list[str]:
    return [str(item["label"]) for item in checks if item["required"] and not item["passed"]]


def _instrument_floor(scfg: dict[str, Any], underlying: str | None, base: float) -> float:
    symbol = str(underlying or "").upper()
    floor = float(base)
    per_symbol = scfg.get("per_underlying_min_conviction") or {}
    floor = max(floor, float(per_symbol.get(symbol, 0.0) or 0.0))
    commodities = {str(x).upper() for x in scfg.get("commodity_underlyings") or []}
    if symbol in commodities:
        floor = max(floor, float(scfg.get("commodity_min_conviction", 0.0) or 0.0))
    return floor


def _level_support_resistance(
    *,
    side: str,
    close: float,
    angles: list[GannAngle],
    sq9_levels: list[SquareNineLevel],
    tol_angle: float,
    tol_sq9: float,
    weights: dict[str, Any],
    major_angles: list[str],
) -> tuple[float, list[str], float | None, str | None]:
    """Score how exactly price sits on a with-direction level.

    For a long we want a SUPPORT at/just-below price (level <= close); for a
    short a RESISTANCE at/just-above price (level >= close). Returns the score,
    reasons, the nearest qualifying level price (for the stop), and a label.
    """
    score = 0.0
    reasons: list[str] = []
    best_level: float | None = None
    best_label: str | None = None
    best_gap = float("inf")

    want_support = side == "long"
    for a in angles:
        lvl = a.current_price
        if want_support and lvl > close:
            continue
        if (not want_support) and lvl < close:
            continue
        if a.distance_pct > tol_angle:
            continue
        w = _angle_weight(a.name, weights, major_angles)
        contrib = _prox(a.distance_pct, tol_angle, w)
        if contrib <= 0:
            continue
        score += contrib
        reasons.append(f"on {a.direction} {a.name} ({round(a.distance_pct * 100, 3)}%)")
        gap = abs(close - lvl)
        if gap < best_gap:
            best_gap, best_level, best_label = gap, lvl, f"angle {a.name}"

    for s in sq9_levels:
        lvl = s.price
        if want_support and lvl > close:
            continue
        if (not want_support) and lvl < close:
            continue
        if s.distance_pct > tol_sq9:
            continue
        w = float(weights.get("sq9_cardinal" if s.level_type == "cardinal" else "sq9_ordinal", 0.75))
        contrib = _prox(s.distance_pct, tol_sq9, w)
        if contrib <= 0:
            continue
        score += contrib
        reasons.append(f"on SQ9 {s.degree}° {s.level_type} ({round(s.distance_pct * 100, 3)}%)")
        gap = abs(close - lvl)
        if gap < best_gap:
            best_gap, best_level, best_label = gap, lvl, f"SQ9 {s.degree}"
    return score, reasons, best_level, best_label


def _targets(side: str, close: float, angles: list[GannAngle], sq9_levels: list[SquareNineLevel]) -> list[float]:
    vals: list[float] = []
    if side == "long":
        vals += [a.current_price for a in angles if a.current_price > close]
        vals += [s.price for s in sq9_levels if s.price > close]
        return sorted({round(v, 2) for v in vals})[:3]
    vals += [a.current_price for a in angles if a.current_price < close]
    vals += [s.price for s in sq9_levels if s.price < close]
    return sorted({round(v, 2) for v in vals}, reverse=True)[:3]


# ─── Public entrypoint ───────────────────────────────────────────────────────


def evaluate_gann_signal(
    *,
    frame: pd.DataFrame,
    anchor: AnchorPoint,
    angles: list[GannAngle],
    sq9_levels: list[SquareNineLevel],
    cycles: list[TimeCycleWindow],
    square: PriceTimeSquare,
    h: float,
    config: dict[str, Any],
    underlying: str | None = None,
) -> ConfluenceSignal:
    """Regime-gated, exactness-weighted Gann decision. `config` is the
    top-level module config (reads its `strategy` section)."""
    scfg = config.get("strategy", {})
    weights = scfg.get("weights", {})
    major_angles = list(scfg.get("major_angles", ["1x2", "2x1"]))
    major_cycles = [int(x) for x in scfg.get("major_cycles", [90, 144, 180, 270, 360])]

    current = frame.iloc[-1]
    close = _f(current.get("close"))
    atr = _f(current.get("atr"), 0.0)
    if not math.isfinite(atr) or atr <= 0:
        atr = abs(close) * 0.002

    reg = compute_regime(frame=frame, angles=angles, anchor=anchor, config=scfg)
    regime = reg["regime"]

    cont_min = _instrument_floor(
        scfg, underlying, float(scfg.get("continuation_min_conviction", 4.0))
    )
    rev_min = _instrument_floor(
        scfg, underlying, float(scfg.get("reversal_min_conviction", 6.5))
    )
    tol_angle = float(scfg.get("angle_tolerance_pct", 0.0025))
    tol_sq9 = float(scfg.get("sq9_tolerance_pct", 0.0025))
    tol_pull = float(scfg.get("pullback_tolerance_pct", 0.005))
    rev_factor = float(scfg.get("reversal_size_factor", 0.5))
    rev_edge = float(scfg.get("reversal_edge_over_continuation", 1.0))
    buffer = float(scfg.get("stop_atr_buffer", 0.5))
    min_stop_pct = float(scfg.get("min_stop_pct", 0.0015))

    timing_score, timing_reasons, selected_cycle = _timing_score(cycles, square, weights, major_cycles)
    has_major_cycle = bool(selected_cycle and int(selected_cycle.cycle) in major_cycles)

    candidates: list[dict[str, Any]] = []

    # ── Continuation (with-regime, pullback to support/resistance) ──────────
    if regime in ("bull", "bear"):
        side = "long" if regime == "bull" else "short"
        lvl_score, lvl_reasons, lvl_price, lvl_label = _level_support_resistance(
            side=side, close=close, angles=angles, sq9_levels=sq9_levels,
            tol_angle=tol_pull, tol_sq9=tol_pull, weights=weights, major_angles=major_angles,
        )
        if lvl_price is not None:
            # Pullback-resumption proxy: last bar pushing back in trend direction.
            resuming = _continuation_confirmation(frame, side)
            conv = lvl_score + timing_score
            conv += float(weights.get("regime_align", 1.5)) * (0.5 + 0.5 * reg["strength"])
            reasons = [f"{regime} regime"] + lvl_reasons + timing_reasons
            if reg["structure"] == ("bullish" if side == "long" else "bearish"):
                conv += float(weights.get("structure_align", 1.0))
                reasons.append("structure aligned")
            if resuming:
                conv += 0.5
                reasons.append("bar resuming trend")
            sign = -1.0 if side == "long" else 1.0
            stop_u = lvl_price + sign * buffer * atr
            # keep stop on the correct side and not too tight
            min_dist = abs(close) * min_stop_pct
            if side == "long":
                stop_u = min(stop_u, close - min_dist)
            else:
                stop_u = max(stop_u, close + min_dist)
            checks = [
                _rule("regime", "Directional regime", True, f"{regime}; votes {reg['regime_score']:+d}"),
                _rule("level", "Gann pullback level", True, str(lvl_label or "qualified level")),
                _rule(
                    "resumption",
                    "Trend-resumption close",
                    resuming,
                    "directional body; close holds beyond prior close",
                    required=bool(scfg.get("continuation_require_resumption", True)),
                ),
            ]
            checks.append(_rule(
                "conviction",
                "Conviction floor",
                conv >= cont_min,
                f"{conv:.2f} / {cont_min:.2f}",
            ))
            candidates.append({
                "archetype": "continuation", "side": side, "conviction": conv,
                "reasons": reasons, "stop_u": stop_u, "size_factor": 1.0,
                "min_conviction": cont_min, "confirmation": resuming,
                "level_label": lvl_label, "rule_checks": checks,
                "blockers": _candidate_blockers(checks),
            })

    # ── Reversal (counter-regime or range, strong exact confluence) ─────────
    rev_sides = []
    if regime == "bull":
        rev_sides = ["short"]
    elif regime == "bear":
        rev_sides = ["long"]
    else:  # neutral range — allow whichever side has a confluence the price reached
        rev_sides = ["long", "short"]
    for side in rev_sides:
        # For a reversal the price has REACHED a resistance (short) / support (long).
        lvl_score, lvl_reasons, lvl_price, lvl_label = _level_support_resistance(
            side=side, close=close, angles=angles, sq9_levels=sq9_levels,
            tol_angle=tol_angle, tol_sq9=tol_sq9, weights=weights, major_angles=major_angles,
        )
        if lvl_price is None:
            continue
        confirm = _reversal_confirmation(frame, side)
        want_support = side == "long"
        has_cardinal_sq9 = any(
            s.level_type == "cardinal"
            and s.distance_pct <= tol_sq9
            and ((want_support and s.price <= close) or ((not want_support) and s.price >= close))
            for s in sq9_levels
        )
        conv = lvl_score + timing_score
        reasons = [f"{regime} regime", "reversal@confluence"] + lvl_reasons + timing_reasons
        if confirm:
            conv += float(weights.get("confirmation_bar", 1.5))
            reasons.append("confirmation bar")
        sign = -1.0 if side == "long" else 1.0
        stop_u = lvl_price + sign * buffer * atr
        min_dist = abs(close) * min_stop_pct
        if side == "long":
            stop_u = min(stop_u, close - min_dist)
        else:
            stop_u = max(stop_u, close + min_dist)
        checks = [
            _rule("level", "Reversal level", True, str(lvl_label or "qualified level")),
            _rule(
                "cardinal_sq9",
                "Cardinal SQ9 touch",
                has_cardinal_sq9,
                "90°, 180°, 270° or 360° on the trade side",
                required=bool(scfg.get("reversal_require_cardinal_sq9", True)),
            ),
            _rule(
                "major_cycle",
                "Major time-cycle window",
                has_major_cycle,
                f"cycle {selected_cycle.cycle}" if selected_cycle else "no active major cycle",
                required=bool(scfg.get("reversal_require_major_cycle", True)),
            ),
            _rule(
                "price_time_square",
                "Price-time square",
                bool(square.active),
                f"ratio {square.ratio:.3f}; tolerance {square.tolerance:.3f}",
                required=bool(scfg.get("reversal_require_price_time_square", True)),
            ),
            _rule("confirmation", "Rejection confirmation bar", confirm, "directional close in rejection half"),
        ]
        checks.append(_rule(
            "conviction",
            "Conviction floor",
            conv >= rev_min,
            f"{conv:.2f} / {rev_min:.2f}",
        ))
        candidates.append({
            "archetype": "reversal", "side": side, "conviction": conv,
            "reasons": reasons, "stop_u": stop_u, "size_factor": rev_factor,
            "min_conviction": rev_min, "confirmation": confirm,
            "level_label": lvl_label, "rule_checks": checks,
            "blockers": _candidate_blockers(checks),
        })

    # ── Pick the winner ─────────────────────────────────────────────────────
    fired = [c for c in candidates if not c["blockers"]]

    chosen: dict[str, Any] | None = None
    if fired:
        cont = next((c for c in fired if c["archetype"] == "continuation"), None)
        rev = next((c for c in fired if c["archetype"] == "reversal"), None)
        if cont and rev:
            # Counter-trend must clearly beat the in-trend trade to override it.
            chosen = rev if rev["conviction"] >= cont["conviction"] + rev_edge else cont
        else:
            chosen = cont or rev

    breakdown = {
        "timing": round(timing_score, 3),
        "regime_score": float(reg["regime_score"]),
        "regime_strength": reg["strength"],
    }
    timing_labels = list(timing_reasons)

    if chosen is None:
        # Nothing actionable — report the best near-miss for the UI.
        best = max(candidates, key=lambda c: c["conviction"], default=None)
        state = "ignore"
        setup_state = "SEARCHING"
        if best is not None:
            non_score_blockers = [
                item for item in best["blockers"] if item != "Conviction floor"
            ]
            if best["conviction"] >= 0.5 * best["min_conviction"]:
                state = "watch"
                setup_state = "BLOCKED" if non_score_blockers else "ARMED"
        return ConfluenceSignal(
            score=int(round(best["conviction"])) if best else 0,
            threshold=int(round(best["min_conviction"])) if best else int(round(cont_min)),
            bias=regime,
            state=state,
            reasons=(best["reasons"][:6] if best else [f"{regime} regime, no confluence"]),
            regime=regime,
            regime_strength=reg["strength"],
            archetype=None,
            side=None,
            conviction=round(best["conviction"], 3) if best else 0.0,
            score_breakdown=breakdown,
            setup_state=setup_state,
            candidate_archetype=best["archetype"] if best else None,
            minimum_conviction=round(best["min_conviction"], 3) if best else cont_min,
            conviction_gap=round((best["conviction"] - best["min_conviction"]), 3) if best else -cont_min,
            selected_level=best.get("level_label") if best else None,
            rule_checks=best.get("rule_checks", []) if best else [
                _rule("geometry", "Trade-side Gann level", False, "no qualifying support or resistance")
            ],
            blockers=best.get("blockers", []) if best else ["Trade-side Gann level"],
            regime_votes=dict(reg["votes"]),
            adx=reg["adx"],
            active_timing=timing_labels,
        )

    side = chosen["side"]
    stop_u = round(float(chosen["stop_u"]), 2)
    risk_per_unit = round(abs(close - stop_u), 2)
    targets_u = _targets(side, close, angles, sq9_levels)
    # Keep only targets that are a meaningful multiple of risk away. A target
    # closer than min_target_r·R caps winners below the -1R stop and bleeds
    # expectancy even at a high hit-rate. With none left, the position rides
    # the break-even + trailing stop instead of a fixed objective.
    min_target_r = float(scfg.get("min_target_r", 1.5))
    if risk_per_unit > 0:
        min_dist = min_target_r * risk_per_unit
        targets_u = [t for t in targets_u if abs(t - close) >= min_dist]
    state = "bullish_setup" if side == "long" else "bearish_setup"
    return ConfluenceSignal(
        score=int(round(chosen["conviction"])),
        threshold=int(round(chosen["min_conviction"])),
        bias=("bullish" if side == "long" else "bearish"),
        state=state,
        reasons=chosen["reasons"][:8],
        trigger=round(close, 2),
        stop=stop_u,
        targets=targets_u,
        regime=regime,
        regime_strength=reg["strength"],
        archetype=chosen["archetype"],
        side=side,
        conviction=round(chosen["conviction"], 3),
        size_factor=float(chosen["size_factor"]),
        confirmation=bool(chosen["confirmation"]),
        stop_underlying=stop_u,
        targets_underlying=targets_u,
        risk_per_unit=risk_per_unit,
        score_breakdown=breakdown,
        setup_state="ACTIONABLE",
        candidate_archetype=chosen["archetype"],
        minimum_conviction=round(chosen["min_conviction"], 3),
        conviction_gap=round(chosen["conviction"] - chosen["min_conviction"], 3),
        selected_level=chosen.get("level_label"),
        rule_checks=chosen.get("rule_checks", []),
        blockers=[],
        regime_votes=dict(reg["votes"]),
        adx=reg["adx"],
        active_timing=timing_labels,
    )
