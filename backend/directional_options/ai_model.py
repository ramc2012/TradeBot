"""Hybrid rules + RL model support for directional long options.

The online RL policy owns exploration, strike ranking, act/skip, and size.
This module supplies the deterministic part of the model: invariant rules
that protect execution quality and a dense rule score that becomes another
policy feature. The rules are intentionally narrow: they block broken or
untradable candidates, while marginal strategy quality is left to the bandit
to learn from realized R-multiples.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


def _get(source: Any, key: str, default: Any = 0.0) -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _float(source: Any, key: str, default: float = 0.0) -> float:
    try:
        value = _get(source, key, default)
        if value is None:
            return default
        numeric = float(value)
        if not math.isfinite(numeric):
            return default
        return numeric
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _tanh_score(value: float, scale: float) -> float:
    return 0.5 + 0.5 * math.tanh(value / max(scale, 1e-9))


def _direction_sign(direction: str) -> float:
    return 1.0 if str(direction).upper() == "CE" else -1.0


@dataclass(frozen=True)
class RuleEvaluation:
    allowed: bool
    score: float
    setup: str
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    spot_features: dict[str, float] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "score": round(self.score, 2),
            "setup": self.setup,
            "reasons": list(self.reasons),
            "blockers": list(self.blockers),
            "components": {key: round(float(value), 4) for key, value in self.components.items()},
            "spot_features": {key: round(float(value), 5) for key, value in self.spot_features.items()},
        }


class HybridDirectionalOptionsModel:
    """Rule scorer that feeds the RL policy with option-aware context."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    def evaluate(
        self,
        *,
        row: Any,
        signal: Any,
        regime: Any,
        candidate: Any,
        chain: dict[str, Any] | None = None,
    ) -> RuleEvaluation:
        direction = str(_get(signal, "direction", "") or "").upper()
        option_type = str(_get(candidate, "option_type", "") or "").upper()
        blockers: list[str] = []
        reasons: list[str] = []

        option_price = _float(candidate, "option_price")
        spread_pct = _float(candidate, "spread_pct")
        liquidity = _float(candidate, "liquidity_score")
        delta_abs = abs(_float(candidate, "delta"))
        days_to_expiry = _float(candidate, "days_to_expiry")
        session_progress = _float(row, "session_progress")

        if direction not in {"CE", "PE"}:
            blockers.append("missing_direction")
        if option_type and direction and option_type != direction:
            blockers.append("direction_mismatch")
        if option_price <= 0.0:
            blockers.append("invalid_option_price")
        if liquidity < float(self.config.get("min_liquidity_score", 0.08)):
            blockers.append("liquidity_below_rule_floor")
        if spread_pct > float(self.config.get("max_spread_pct", 0.28)):
            blockers.append("spread_above_rule_ceiling")
        if delta_abs < float(self.config.get("min_delta_abs", 0.12)):
            blockers.append("delta_too_small")
        if delta_abs > float(self.config.get("max_delta_abs", 0.92)):
            blockers.append("delta_too_deep")
        if days_to_expiry < float(self.config.get("min_days_to_expiry", 0.20)):
            blockers.append("expiry_too_close")
        if (
            session_progress >= float(self.config.get("late_session_cutoff", 0.92))
            and days_to_expiry <= float(self.config.get("late_session_min_days_to_expiry", 1.0))
        ):
            blockers.append("late_session_expiry_risk")

        components = {
            "spot_trend": self._spot_trend(row, direction),
            "breakout_quality": self._breakout_quality(row, direction),
            "volatility_state": self._volatility_state(row),
            "option_quality": self._option_quality(candidate),
            "chain_confirmation": self._chain_confirmation(chain, direction),
            "execution_timing": self._execution_timing(row, candidate),
            "model_edge": self._model_edge(candidate),
        }
        score = 100.0 * (
            0.23 * components["spot_trend"]
            + 0.15 * components["breakout_quality"]
            + 0.12 * components["volatility_state"]
            + 0.22 * components["option_quality"]
            + 0.10 * components["chain_confirmation"]
            + 0.08 * components["execution_timing"]
            + 0.10 * components["model_edge"]
        )
        min_score = float(self.config.get("min_rule_score", 38.0))
        if score < min_score:
            blockers.append("rule_score_below_min")

        if components["breakout_quality"] >= 0.68:
            setup = "breakout_continuation"
        elif components["spot_trend"] >= 0.64 and components["volatility_state"] >= 0.50:
            setup = "trend_pullthrough"
        elif components["model_edge"] >= 0.60:
            setup = "edge_reversion"
        else:
            setup = "exploratory_directional"

        if components["spot_trend"] >= 0.62:
            reasons.append("spot trend confirms option direction")
        if components["breakout_quality"] >= 0.62:
            reasons.append("breakout and range expansion support timing")
        if components["option_quality"] >= 0.62:
            reasons.append("option liquidity, spread, theta, and delta are acceptable")
        if components["chain_confirmation"] >= 0.58:
            reasons.append("option-chain flow is aligned")
        if not reasons:
            reasons.append("mixed context; policy must earn the trade from learned value")

        return RuleEvaluation(
            allowed=not blockers,
            score=score,
            setup=setup,
            reasons=reasons,
            blockers=blockers,
            components=components,
            spot_features=self._spot_features(row, direction),
        )

    def _spot_trend(self, row: Any, direction: str) -> float:
        sign = _direction_sign(direction)
        ema = _tanh_score(sign * _float(row, "ema_spread_pct"), 0.0035)
        slope = _tanh_score(sign * _float(row, "ema_fast_slope_pct"), 0.0015)
        di = _tanh_score(sign * ((_float(row, "plus_di") - _float(row, "minus_di")) / 100.0), 0.18)
        momentum = _tanh_score(sign * ((_float(row, "momentum_3") * 0.6) + (_float(row, "momentum_8") * 0.4)), 0.006)
        macd = _tanh_score(sign * _float(row, "macd_hist_pct"), 0.0008)
        rsi = _tanh_score(sign * ((_float(row, "rsi_14", 50.0) - 50.0) / 50.0), 0.35)
        vwap = _tanh_score(sign * _float(row, "vwap_deviation_pct"), 0.0025)
        trend_quality = _clip(_float(row, "trend_quality"))
        raw = (0.22 * ema) + (0.12 * slope) + (0.18 * di) + (0.18 * momentum) + (0.12 * macd) + (0.08 * rsi) + (0.10 * vwap)
        return _clip((0.72 * raw) + (0.28 * trend_quality))

    def _breakout_quality(self, row: Any, direction: str) -> float:
        sign = _direction_sign(direction)
        directional_breakout = _float(row, "breakout_up") if sign > 0 else _float(row, "breakout_down")
        breakout_score = _tanh_score(max(directional_breakout, 0.0), 1.0)
        range_score = _clip((_float(row, "range_expansion", 1.0) - 0.75) / 1.25)
        close_location = _tanh_score(sign * _float(row, "close_location"), 0.65)
        opening_position = _float(row, "opening_range_position", 0.5)
        opening_edge = opening_position - 1.0 if sign > 0 else -opening_position
        opening_score = _tanh_score(opening_edge, 0.55)
        body_score = _tanh_score(sign * _float(row, "body_pct"), 0.0025)
        return _clip((0.32 * breakout_score) + (0.24 * range_score) + (0.18 * close_location) + (0.16 * opening_score) + (0.10 * body_score))

    def _volatility_state(self, row: Any) -> float:
        rv_pct = _clip(_float(row, "rv_percentile"))
        rv_band = _clip(1.0 - abs(rv_pct - 0.55) / 0.55)
        atr_pct = _float(row, "atr_pct")
        atr_alive = _clip(atr_pct / 0.006)
        range_expansion = _clip((_float(row, "range_expansion", 1.0) - 0.75) / 1.25)
        volume = _tanh_score(_float(row, "volume_zscore"), 1.5)
        return _clip((0.40 * rv_band) + (0.25 * atr_alive) + (0.20 * range_expansion) + (0.15 * volume))

    def _option_quality(self, candidate: Any) -> float:
        liquidity = _clip(_float(candidate, "liquidity_score"))
        spread_pct = _float(candidate, "spread_pct")
        spread_score = _clip(1.0 - spread_pct / max(float(self.config.get("max_spread_pct", 0.28)), 1e-9))
        option_price = max(_float(candidate, "option_price"), 1.0)
        theta_penalty = max(_float(candidate, "theta_penalty"), abs(_float(candidate, "theta")) / option_price * 0.01)
        theta_score = _clip(1.0 - theta_penalty / 0.18)
        delta_abs = abs(_float(candidate, "delta"))
        delta_score = _clip(1.0 - abs(delta_abs - 0.50) / 0.42)
        timing_fit = _clip(_float(candidate, "timing_fit"))
        probability = _clip(_float(candidate, "probability_of_profit"))
        return _clip((0.30 * liquidity) + (0.18 * spread_score) + (0.18 * theta_score) + (0.14 * delta_score) + (0.12 * timing_fit) + (0.08 * probability))

    def _chain_confirmation(self, chain: dict[str, Any] | None, direction: str) -> float:
        if not chain:
            return 0.50
        sign = _direction_sign(direction)
        call_ltp = _float(chain, "atm_call_ltp_change_pct")
        put_ltp = _float(chain, "atm_put_ltp_change_pct")
        ltp_edge = call_ltp - put_ltp if sign > 0 else put_ltp - call_ltp
        ltp_score = _tanh_score(ltp_edge, 4.0)

        call_oi = _float(chain, "atm_call_oi_change")
        put_oi = _float(chain, "atm_put_oi_change")
        oi_edge = call_oi - put_oi if sign > 0 else put_oi - call_oi
        oi_score = _tanh_score(oi_edge, 75_000.0)

        pcr = _float(chain, "pcr_oi")
        pcr_change = _float(chain, "pcr_oi_change")
        pcr_edge = (pcr - 1.0) if sign > 0 else (1.0 - pcr)
        pcr_score = _tanh_score(pcr_edge + (sign * -pcr_change * 0.5), 0.55)

        dex_calls = abs(_float(chain, "dex_calls"))
        dex_puts = abs(_float(chain, "dex_puts"))
        dex_net = _float(chain, "dex_net")
        dex_ratio = dex_net / max(dex_calls + dex_puts, 1.0)
        dex_score = _tanh_score(sign * dex_ratio, 0.35)
        return _clip((0.36 * ltp_score) + (0.22 * oi_score) + (0.24 * pcr_score) + (0.18 * dex_score))

    def _execution_timing(self, row: Any, candidate: Any) -> float:
        session_progress = _clip(_float(row, "session_progress"))
        days_to_expiry = _float(candidate, "days_to_expiry")
        open_penalty = max(0.0, 0.05 - session_progress) / 0.05
        close_penalty = max(0.0, session_progress - 0.88) / 0.12
        expiry_penalty = max(0.0, 1.0 - days_to_expiry) * 0.35
        return _clip(1.0 - (0.22 * open_penalty) - (0.45 * close_penalty) - expiry_penalty)

    def _model_edge(self, candidate: Any) -> float:
        option_price = max(_float(candidate, "option_price"), 1.0)
        trading_edge = _float(candidate, "p_trading_edge") / option_price
        terminal_edge = _float(candidate, "p_terminal_edge") / option_price
        tail_edge = _float(candidate, "p_minus_q_tail")
        return_on_premium = _float(candidate, "expected_return_on_premium")
        edge_score = _tanh_score((0.55 * trading_edge) + (0.20 * terminal_edge) + (0.25 * return_on_premium), 0.75)
        tail_score = _tanh_score(tail_edge, 0.18)
        return _clip((0.70 * edge_score) + (0.30 * tail_score))

    def _spot_features(self, row: Any, direction: str) -> dict[str, float]:
        sign = _direction_sign(direction)
        return {
            "directional_ema_spread": math.tanh(sign * _float(row, "ema_spread_pct") / 0.004),
            "directional_ema_slope": math.tanh(sign * _float(row, "ema_fast_slope_pct") / 0.0015),
            "directional_macd_hist": math.tanh(sign * _float(row, "macd_hist_pct") / 0.0008),
            "directional_vwap_deviation": math.tanh(sign * _float(row, "vwap_deviation_pct") / 0.0025),
            "directional_rsi": math.tanh(sign * ((_float(row, "rsi_14", 50.0) - 50.0) / 50.0) / 0.35),
            "trend_quality": _clip(_float(row, "trend_quality")),
            "volume_zscore": math.tanh(_float(row, "volume_zscore") / 2.0),
            "atr_pct": _float(row, "atr_pct"),
            "range_expansion": _float(row, "range_expansion", 1.0),
        }
