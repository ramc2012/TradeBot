"""Hybrid rule model for Fractal Market Profile trades.

The FMP strategy already emits deterministic profile signals. This module
turns that packet into an explicit trade-quality score that can be audited
and passed into the online policy. The rules block only broken or clearly
unsafe packets; learned act/skip decisions live in ``policy.py``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from fractal_market_profile.config import MCX_SESSION_CLOSE, MCX_SESSION_OPEN, SESSION_CLOSE, SESSION_OPEN


def _minute_of_day(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value)
    timestamp = text.split("T", 1)[1] if "T" in text else text
    try:
        hh, mm = timestamp[:5].split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return None


def _time_to_minute(value: Any) -> int:
    return int(value.hour) * 60 + int(value.minute)


def _is_mcx_symbol(symbol_code: Any) -> bool:
    normalized = str(symbol_code or "").upper().strip()
    return normalized == "CRUDEOIL" or normalized.startswith("MCX:CRUDEOIL") or normalized.startswith("CRUDEOIL")


def _session_window_minutes(symbol_code: Any) -> tuple[int, int]:
    if _is_mcx_symbol(symbol_code):
        return _time_to_minute(MCX_SESSION_OPEN), _time_to_minute(MCX_SESSION_CLOSE)
    return _time_to_minute(SESSION_OPEN), _time_to_minute(SESSION_CLOSE)


def _get(source: Any, key: str, default: Any = None) -> Any:
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


def _direction_sign(action: str) -> float:
    return 1.0 if str(action).upper() == "LONG" else -1.0


@dataclass(frozen=True)
class FMPRuleEvaluation:
    allowed: bool
    score: float
    setup: str
    blockers: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    features: dict[str, float | str | bool] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "score": round(self.score, 2),
            "setup": self.setup,
            "blockers": list(self.blockers),
            "reasons": list(self.reasons),
            "components": {key: round(float(value), 4) for key, value in self.components.items()},
            "features": dict(self.features),
        }


class FMPHybridTradingModel:
    """Rule scorer for profile-driven option/futures trades."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    def evaluate(
        self,
        *,
        signal: dict[str, Any],
        analysis: dict[str, Any] | None = None,
        order_flow: dict[str, Any] | None = None,
    ) -> FMPRuleEvaluation:
        analysis = analysis or {}
        order_flow = order_flow or {}
        metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
        options = signal.get("options") if isinstance(signal.get("options"), dict) else {}
        data_status = analysis.get("data_status") if isinstance(analysis.get("data_status"), dict) else {}
        symbol_code = (
            analysis.get("symbol_code")
            or data_status.get("symbol_code")
            or metadata.get("symbol_code")
            or signal.get("symbol_code")
        )

        action = str(signal.get("action") or "FLAT").upper()
        filters = [str(item) for item in signal.get("filters") or [] if item]
        blockers: list[str] = []
        reasons: list[str] = []

        if action not in {"LONG", "SHORT"}:
            blockers.append("not_directional")
        if filters:
            blockers.append("base_signal_filtered")
        if data_status and not bool(data_status.get("execution_ready", True)):
            blockers.append("execution_data_not_ready")
        if _float(signal, "confidence") < float(self.config.get("min_rule_confidence", 0.50)):
            blockers.append("signal_confidence_below_rule_floor")

        instrument_type = str(options.get("instrument_type") or options.get("option_type") or "").upper()
        premium = _float(options, "premium")
        if action in {"LONG", "SHORT"}:
            if not options:
                blockers.append("instrument_context_missing")
            else:
                if instrument_type != "FUT":
                    expected_option = "CE" if action == "LONG" else "PE"
                    option_type = str(options.get("option_type") or "").upper()
                    if option_type and option_type != expected_option:
                        blockers.append("option_direction_mismatch")
                    dte = _float(options, "days_to_expiry")
                    if dte < float(self.config.get("min_dte_for_long_options", 1)):
                        blockers.append("option_expiry_too_close")
                if premium < float(self.config.get("min_option_premium", 1.0)):
                    blockers.append("instrument_premium_invalid")

        components = {
            "profile_alignment": self._profile_alignment(signal, metadata),
            "auction_structure": self._auction_structure(signal),
            "order_flow_confirmation": self._order_flow_confirmation(signal, metadata, order_flow),
            "instrument_quality": self._instrument_quality(signal, options),
            "volatility_risk": self._volatility_risk(signal, metadata),
            "execution_timing": self._execution_timing(signal, data_status, symbol_code),
            "data_quality": self._data_quality(data_status),
        }
        score = 100.0 * (
            0.22 * components["profile_alignment"]
            + 0.17 * components["auction_structure"]
            + 0.17 * components["order_flow_confirmation"]
            + 0.18 * components["instrument_quality"]
            + 0.10 * components["volatility_risk"]
            + 0.08 * components["execution_timing"]
            + 0.08 * components["data_quality"]
        )
        if score < float(self.config.get("min_rule_score", 46.0)):
            blockers.append("rule_score_below_min")

        if components["profile_alignment"] >= 0.64:
            reasons.append("profile direction and value migration are aligned")
        if components["auction_structure"] >= 0.62:
            reasons.append("auction structure supports the setup")
        if components["order_flow_confirmation"] >= 0.58:
            reasons.append("order flow confirms the profile thesis")
        if components["instrument_quality"] >= 0.60:
            reasons.append("mapped instrument is tradable for the horizon")
        if not reasons:
            reasons.append("mixed FMP context; learned policy must justify the trade")

        setup = self._setup_label(signal, components)
        return FMPRuleEvaluation(
            allowed=not blockers,
            score=score,
            setup=setup,
            blockers=blockers,
            reasons=reasons,
            components=components,
            features=self._features(signal, metadata, order_flow, options, data_status),
        )

    def _profile_alignment(self, signal: dict[str, Any], metadata: dict[str, Any]) -> float:
        action = str(signal.get("action") or "").upper()
        sign = _direction_sign(action)
        va_score = _float(signal, "value_migration_score")
        migration_score = _tanh_score(sign * va_score, 2.5)
        daily_direction = str(metadata.get("daily_direction") or "").lower()
        daily_match = 1.0 if (
            (action == "LONG" and daily_direction == "bullish")
            or (action == "SHORT" and daily_direction == "bearish")
        ) else 0.45 if daily_direction in {"bullish", "bearish"} else 0.55
        daily_context = str(signal.get("daily_context") or "").upper()
        trend_context = 1.0 if (
            (action == "LONG" and daily_context.startswith("TREND_UP"))
            or (action == "SHORT" and daily_context.startswith("TREND_DN"))
        ) else 0.58 if daily_context.startswith("NORMAL") else 0.50
        confidence = _clip(_float(signal, "confidence"))
        return _clip((0.36 * migration_score) + (0.24 * daily_match) + (0.20 * trend_context) + (0.20 * confidence))

    def _auction_structure(self, signal: dict[str, Any]) -> float:
        setup_name = str(signal.get("setup_name") or "")
        hourly_shape = str(signal.get("hourly_shape") or "")
        daily_shape = str(signal.get("daily_shape") or "")
        setup_score = 0.50
        if setup_name.startswith("hourly_ib_breakout"):
            setup_score = 0.78
        elif setup_name.startswith("trend_pullback"):
            setup_score = 0.68
        elif setup_name.startswith("daily_balance_mean_reversion"):
            setup_score = 0.62
        elif setup_name.startswith("daily_balance_extreme_reversion"):
            setup_score = 0.58
        elif setup_name.startswith("daily_balance_breakout"):
            setup_score = 0.56

        shape_score = 0.50
        if setup_name.startswith("hourly_ib_breakout") and hourly_shape == "Elongated":
            shape_score = 0.78
        elif setup_name.startswith("trend_pullback") and daily_shape == "Elongated":
            shape_score = 0.66
        elif setup_name.startswith("daily_balance") and daily_shape == "D-shape":
            shape_score = 0.64

        rr = self._risk_reward(signal)
        rr_score = _clip((rr - 0.75) / 2.25)
        return _clip((0.46 * setup_score) + (0.30 * shape_score) + (0.24 * rr_score))

    def _order_flow_confirmation(
        self,
        signal: dict[str, Any],
        metadata: dict[str, Any],
        order_flow: dict[str, Any],
    ) -> float:
        action = str(signal.get("action") or "").upper()
        sign = _direction_sign(action)
        flow_direction = str(metadata.get("order_flow_direction") or "").lower()
        direction_match = 1.0 if (
            (action == "LONG" and flow_direction == "bullish")
            or (action == "SHORT" and flow_direction == "bearish")
        ) else 0.35 if flow_direction in {"bullish", "bearish"} else 0.50
        alignment = _clip(_float(metadata, "order_flow_alignment"))
        delta_score = _tanh_score(sign * _float(order_flow, "delta"), 1500.0)
        trade_imbalance = _tanh_score(sign * _float(order_flow, "trade_imbalance"), 0.45)
        pressure = _tanh_score(sign * _float(order_flow, "book_pressure"), 0.55)
        toxicity = _clip(1.0 - _float(order_flow, "toxicity_score"))
        return _clip(
            (0.30 * direction_match)
            + (0.24 * alignment)
            + (0.18 * delta_score)
            + (0.12 * trade_imbalance)
            + (0.08 * pressure)
            + (0.08 * toxicity)
        )

    def _instrument_quality(self, signal: dict[str, Any], options: dict[str, Any]) -> float:
        if not options:
            return 0.0
        premium = _float(options, "premium")
        if premium <= 0.0:
            return 0.0
        instrument_type = str(options.get("instrument_type") or options.get("option_type") or "").upper()
        if instrument_type == "FUT":
            dte = _float(options, "days_to_expiry")
            dte_score = _clip(dte / 12.0) if dte > 0 else 0.75
            return _clip((0.72 * 1.0) + (0.28 * dte_score))

        oi_change = _float(options, "oi_change")
        volume = _float(options, "volume")
        pcr = _float(options, "pcr_oi", 1.0)
        iv_rank = _float(options, "iv_rank")
        dte = _float(options, "days_to_expiry")
        sign = _direction_sign(str(signal.get("action") or ""))
        oi_score = _tanh_score(oi_change, 50_000.0)
        volume_score = _tanh_score(volume, 250_000.0)
        pcr_edge = (pcr - 1.0) if sign > 0 else (1.0 - pcr)
        pcr_score = _tanh_score(pcr_edge, 0.50) if pcr > 0 else 0.50
        iv_score = _clip(1.0 - iv_rank / max(float(self.config.get("max_iv_rank_for_buying", 55.0)), 1.0)) if iv_rank > 0 else 0.55
        dte_score = _clip(dte / 8.0)
        return _clip((0.24 * oi_score) + (0.18 * volume_score) + (0.22 * pcr_score) + (0.18 * iv_score) + (0.18 * dte_score))

    def _volatility_risk(self, signal: dict[str, Any], metadata: dict[str, Any]) -> float:
        india_vix = _float(metadata, "india_vix")
        if india_vix <= 0:
            return 0.62
        high_vix = float(self.config.get("high_vix_threshold", 24.0))
        if india_vix <= high_vix:
            return _clip(1.0 - max(0.0, india_vix - 12.0) / 30.0)
        # Scalp balance trades can survive high VIX better than swing premium buys.
        horizon_bonus = 0.10 if str(signal.get("horizon") or "") == "scalp" else 0.0
        return _clip(0.42 + horizon_bonus - min((india_vix - high_vix) / 40.0, 0.25))

    def _execution_timing(self, signal: dict[str, Any], data_status: dict[str, Any], symbol_code: Any = None) -> float:
        latest_ist = data_status.get("latest_row_time_ist")
        progress = None
        minute = _minute_of_day(latest_ist)
        if minute is not None:
            session_open, session_close = _session_window_minutes(symbol_code)
            progress = (minute - session_open) / max(session_close - session_open, 1)
        if progress is None:
            hour_number = max(_float(signal, "hourly_number"), 1.0)
            progress = hour_number / 6.5
        progress = _clip(float(progress))
        late_penalty = max(0.0, progress - float(self.config.get("late_entry_progress", 0.88))) / 0.12
        open_penalty = max(0.0, 0.08 - progress) / 0.08
        return _clip(1.0 - (0.55 * late_penalty) - (0.20 * open_penalty))

    @staticmethod
    def _data_quality(data_status: dict[str, Any]) -> float:
        if not data_status:
            return 0.72
        if not bool(data_status.get("minute_history_ready", True)):
            return 0.20
        if not bool(data_status.get("order_flow_ready", True)):
            return 0.30
        if not bool(data_status.get("execution_ready", True)):
            return 0.35
        source = str(data_status.get("order_flow_source") or "")
        return 1.0 if source == "market_ticks" else 0.72 if source in {"bar_proxy", "bar_fallback"} else 0.62

    @staticmethod
    def _risk_reward(signal: dict[str, Any]) -> float:
        entry = _float(signal, "entry_trigger")
        stop = _float(signal, "stop_level")
        target = _float(signal, "target_level")
        action = str(signal.get("action") or "").upper()
        if action == "LONG":
            risk = max(entry - stop, 1e-9)
            reward = max(target - entry, 0.0)
        else:
            risk = max(stop - entry, 1e-9)
            reward = max(entry - target, 0.0)
        return float(reward / risk) if risk > 0 else 0.0

    def _setup_label(self, signal: dict[str, Any], components: dict[str, float]) -> str:
        setup_name = str(signal.get("setup_name") or "")
        if setup_name.startswith("hourly_ib_breakout") and components["order_flow_confirmation"] >= 0.55:
            return "profile_breakout_with_flow"
        if setup_name.startswith("trend_pullback") and components["profile_alignment"] >= 0.62:
            return "value_migration_pullback"
        if setup_name.startswith("daily_balance"):
            return "balance_rotation"
        if components["instrument_quality"] >= 0.70:
            return "instrument_led_continuation"
        return "exploratory_fmp_directional"

    def _features(
        self,
        signal: dict[str, Any],
        metadata: dict[str, Any],
        order_flow: dict[str, Any],
        options: dict[str, Any],
        data_status: dict[str, Any],
    ) -> dict[str, float | str | bool]:
        action = str(signal.get("action") or "FLAT").upper()
        sign = _direction_sign(action)
        flow_direction = str(metadata.get("order_flow_direction") or "")
        instrument_type = str(options.get("instrument_type") or options.get("option_type") or "")
        return {
            "action": action,
            "directional_value_migration": round(sign * _float(signal, "value_migration_score"), 4),
            "order_flow_matches": bool(
                (action == "LONG" and flow_direction == "bullish")
                or (action == "SHORT" and flow_direction == "bearish")
            ),
            "order_flow_alignment": round(_float(metadata, "order_flow_alignment"), 4),
            "risk_reward": round(self._risk_reward(signal), 4),
            "india_vix": round(_float(metadata, "india_vix"), 4),
            "instrument_type": instrument_type,
            "premium": round(_float(options, "premium"), 4),
            "days_to_expiry": round(_float(options, "days_to_expiry"), 4),
            "execution_ready": bool(data_status.get("execution_ready", True)) if data_status else True,
            "order_flow_source": str(data_status.get("order_flow_source") or ""),
        }
