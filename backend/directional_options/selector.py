"""Contract discovery and scoring for long call/put trades."""
from __future__ import annotations

import math
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd

from analysis.instruments import normalize_index_contract_expiry
from directional_options.data import DirectionalOptionsDataStore
from directional_options.features import timeframe_minutes
from directional_options.schemas import ContractCandidate, ContractMeta, DirectionalSignal, RegimeSnapshot


def _normalize_iv(value: Any) -> float:
    """Broker IV → decimal. Some feeds (Upstox) report IV in PERCENT (e.g. 11.2)
    and some (Fyers) as a decimal (0.112); a value > 3.0 is implausible as a
    decimal vol (300%), so treat it as a percent and divide by 100. Prevents a
    percent IV from being clamped to the sigma ceiling and corrupting greeks."""
    try:
        v = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0.0:
        return 0.0
    return v / 100.0 if v > 3.0 else v


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _norm_pdf(value: float) -> float:
    return math.exp(-(value * value) / 2.0) / math.sqrt(2.0 * math.pi)


def _black_scholes_greeks(
    *,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    sigma: float,
    risk_free_rate: float,
    option_type: str,
) -> tuple[float, float, float, float]:
    if (
        spot <= 0.0
        or strike <= 0.0
        or time_to_expiry_years <= 0.0
        or sigma <= 0.0
    ):
        return 0.0, 0.0, 0.0, 0.0

    sqrt_t = math.sqrt(time_to_expiry_years)
    sigma_sqrt_t = sigma * sqrt_t
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * sigma * sigma) * time_to_expiry_years) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    pdf = _norm_pdf(d1)

    if option_type == "CE":
        delta = _norm_cdf(d1)
        theta = (
            -(spot * pdf * sigma) / (2.0 * sqrt_t)
            - risk_free_rate * strike * math.exp(-risk_free_rate * time_to_expiry_years) * _norm_cdf(d2)
        )
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (
            -(spot * pdf * sigma) / (2.0 * sqrt_t)
            + risk_free_rate * strike * math.exp(-risk_free_rate * time_to_expiry_years) * _norm_cdf(-d2)
        )
    gamma = pdf / (spot * sigma_sqrt_t)
    vega = spot * pdf * sqrt_t
    return delta, gamma, theta, vega


def _black_scholes_price(
    *,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    sigma: float,
    risk_free_rate: float,
    option_type: str,
) -> float:
    if spot <= 0.0 or strike <= 0.0 or time_to_expiry_years <= 0.0 or sigma <= 0.0:
        return max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)

    sqrt_t = math.sqrt(time_to_expiry_years)
    sigma_sqrt_t = sigma * sqrt_t
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * sigma * sigma) * time_to_expiry_years) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    discount = math.exp(-risk_free_rate * time_to_expiry_years)
    if option_type == "CE":
        return (spot * _norm_cdf(d1)) - (strike * discount * _norm_cdf(d2))
    return (strike * discount * _norm_cdf(-d2)) - (spot * _norm_cdf(-d1))


def _risk_neutral_tail(
    *,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    sigma: float,
    risk_free_rate: float,
    option_type: str,
) -> float:
    if spot <= 0.0 or strike <= 0.0 or time_to_expiry_years <= 0.0 or sigma <= 0.0:
        return 0.0
    sigma_sqrt_t = sigma * math.sqrt(time_to_expiry_years)
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * sigma * sigma) * time_to_expiry_years) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    return _norm_cdf(d2) if option_type == "CE" else _norm_cdf(-d2)


def _normal_tail(mean: float, stdev: float, threshold: float, option_type: str) -> float:
    if stdev <= 0.0:
        if option_type == "CE":
            return 1.0 if mean > threshold else 0.0
        return 1.0 if mean < threshold else 0.0
    z = (threshold - mean) / stdev
    return 1.0 - _norm_cdf(z) if option_type == "CE" else _norm_cdf(z)


def _normal_expected_payoff(mean: float, stdev: float, strike: float, option_type: str) -> float:
    if stdev <= 0.0:
        return max(mean - strike, 0.0) if option_type == "CE" else max(strike - mean, 0.0)
    d = (mean - strike) / stdev
    if option_type == "CE":
        return max(0.0, (mean - strike) * _norm_cdf(d) + stdev * _norm_pdf(d))
    return max(0.0, (strike - mean) * _norm_cdf(-d) + stdev * _norm_pdf(d))


def _delta_bucket(delta_abs: float) -> str:
    if delta_abs < 0.25:
        return "lottery"
    if delta_abs < 0.35:
        return "convex"
    if delta_abs < 0.55:
        return "core"
    if delta_abs < 0.70:
        return "linear"
    return "deep"


class OptionSelectionEngine:
    """Distributional strike-expiry optimizer for outright long CE/PE trades."""

    def __init__(self, store: DirectionalOptionsDataStore, config: dict[str, Any]):
        self.store = store
        self.config = config

    def select(
        self,
        *,
        underlying: str,
        timestamp: pd.Timestamp,
        spot_price: float,
        row,
        signal: DirectionalSignal,
        regime: RegimeSnapshot,
        timeframe: str,
    ) -> dict[str, Any]:
        selector_cfg = self.config
        contracts = self.store.list_contracts(
            underlying=underlying,
            option_type=signal.direction,
            max_days_to_expiry=float(selector_cfg["max_days_to_expiry"]),
            as_of=timestamp,
        )
        if not contracts:
            return {"best": None, "candidates": [], "reason": "No persisted option contracts were available for this timestamp."}

        expiry_preference = regime.preferred_expiry_kind
        contracts = self._front_expiry_contracts(
            contracts=contracts,
            timestamp=timestamp,
            preferred_expiry_kind=expiry_preference,
        )
        contracts = sorted(
            contracts,
            key=lambda meta: (
                self._contract_expiry_date(meta).isoformat(),
                abs(meta.strike - spot_price),
                0 if self._contract_expiry_kind(meta) == expiry_preference else 1,
            ),
        )[: int(selector_cfg["max_candidates"]) * 3]

        candidates: list[ContractCandidate] = []
        horizon_years = max(
            (signal.expected_horizon_bars * timeframe_minutes(timeframe)) / (252.0 * 375.0),
            1.0 / (252.0 * 375.0),
        )
        sigma = min(
            max(float(row.get("rv_annualized", 0.22)) * float(selector_cfg["sigma_multiplier"]), float(selector_cfg["sigma_floor"])),
            float(selector_cfg["sigma_ceiling"]),
        )
        risk_free_rate = float(selector_cfg["risk_free_rate"])
        delta_mid = (regime.delta_target_min + regime.delta_target_max) / 2.0
        atm_iv = sigma

        for meta in contracts:
            candidate = self._score_contract(
                meta=meta,
                timestamp=timestamp,
                spot_price=spot_price,
                sigma=sigma,
                row=row,
                signal=signal,
                regime=regime,
                horizon_years=horizon_years,
                risk_free_rate=risk_free_rate,
                delta_mid=delta_mid,
                atm_iv=atm_iv,
            )
            if candidate is None:
                continue
            candidates.append(candidate)

        if not candidates:
            return {"best": None, "candidates": [], "reason": "All available contracts failed liquidity or edge hurdles."}

        candidates = sorted(candidates, key=lambda item: item.contract_score, reverse=True)
        best = candidates[0]
        selected = ContractCandidate(**{**asdict(best), "selected": True})
        rest = [selected, *candidates[1: int(selector_cfg["max_candidates"])]]
        return {
            "best": selected,
            "candidates": rest,
            "reason": selected.selection_reason,
        }

    def select_from_live_snapshots(
        self,
        *,
        underlying: str,
        timestamp: pd.Timestamp,
        spot_price: float,
        row,
        signal: DirectionalSignal,
        regime: RegimeSnapshot,
        timeframe: str,
        snapshot_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not snapshot_rows:
            return {"best": None, "candidates": [], "reason": "No local watchlist contracts were available for this timestamp."}

        selector_cfg = self.config
        expiry_preference = regime.preferred_expiry_kind
        snapshot_rows = self._front_expiry_snapshots(
            snapshot_rows=snapshot_rows,
            timestamp=timestamp,
            preferred_expiry_kind=expiry_preference,
        )
        ordered = sorted(
            snapshot_rows,
            key=lambda item: (
                self._snapshot_expiry_date(item).isoformat(),
                abs(float(item.get("strike") or 0.0) - spot_price),
                0 if self._snapshot_expiry_kind(item) == expiry_preference else 1,
            ),
        )[: int(selector_cfg["max_candidates"]) * 3]

        candidates: list[ContractCandidate] = []
        horizon_years = max(
            (signal.expected_horizon_bars * timeframe_minutes(timeframe)) / (252.0 * 375.0),
            1.0 / (252.0 * 375.0),
        )
        sigma = min(
            max(
                float(row.get("rv_annualized", 0.22)) * float(selector_cfg["sigma_multiplier"]),
                float(selector_cfg["sigma_floor"]),
            ),
            float(selector_cfg["sigma_ceiling"]),
        )
        risk_free_rate = float(selector_cfg["risk_free_rate"])
        delta_mid = (regime.delta_target_min + regime.delta_target_max) / 2.0
        atm_iv = self._estimate_snapshot_atm_iv(snapshot_rows, spot_price, sigma)

        for snapshot in ordered:
            candidate = self._score_snapshot_contract(
                snapshot=snapshot,
                timestamp=timestamp,
                spot_price=spot_price,
                default_sigma=sigma,
                signal=signal,
                horizon_years=horizon_years,
                risk_free_rate=risk_free_rate,
                delta_mid=delta_mid,
                atm_iv=atm_iv,
            )
            if candidate is None:
                continue
            candidates.append(candidate)

        if not candidates:
            return {"best": None, "candidates": [], "reason": "All local watchlist contracts failed liquidity or edge hurdles."}

        candidates = sorted(candidates, key=lambda item: item.contract_score, reverse=True)
        best = candidates[0]
        selected = ContractCandidate(**{**asdict(best), "selected": True})
        rest = [selected, *candidates[1: int(selector_cfg["max_candidates"])]]
        return {
            "best": selected,
            "candidates": rest,
            "reason": selected.selection_reason,
        }

    def _contract_expiry_date(self, meta: ContractMeta) -> date:
        normalized = normalize_index_contract_expiry(meta.underlying, meta.expiry)
        return normalized or pd.Timestamp(meta.expiry).date()

    def _contract_expiry_kind(self, meta: ContractMeta) -> str:
        expiry_date = self._contract_expiry_date(meta)
        if expiry_date.isoformat() != str(meta.expiry or "")[:10]:
            return "monthly"
        return str(meta.expiry_kind or "weekly")

    def _snapshot_expiry_date(self, item: dict[str, Any]) -> date:
        normalized = normalize_index_contract_expiry(item.get("underlying"), item.get("expiry"))
        return normalized or pd.Timestamp(item.get("expiry")).date()

    def _snapshot_expiry_kind(self, item: dict[str, Any]) -> str:
        expiry_date = self._snapshot_expiry_date(item)
        if expiry_date.isoformat() != str(item.get("expiry") or "")[:10]:
            return "monthly"
        return str(item.get("expiry_kind") or "weekly")

    def _front_expiry_contracts(
        self,
        *,
        contracts: list[ContractMeta],
        timestamp: pd.Timestamp,
        preferred_expiry_kind: str,
    ) -> list[ContractMeta]:
        """For weekly intraday trades, keep selection on the front expiry.

        Month-end NSE contracts can be both the front tradable expiry and the
        monthly expiry. If we prioritize the "weekly" label before expiry date,
        a farther weekly contract can beat the real front expiry, which is how
        NIFTY drifted to 28-May instead of 26-May.
        """
        if preferred_expiry_kind != "weekly":
            return contracts
        as_of_date = timestamp.date()
        expiries = sorted({
            self._contract_expiry_date(item)
            for item in contracts
            if self._contract_expiry_date(item) >= as_of_date
        })
        if not expiries:
            return contracts
        front_expiry = expiries[0]
        if (front_expiry - as_of_date).days > float(self.config.get("preferred_weekly_days", 8)):
            return contracts
        return [item for item in contracts if self._contract_expiry_date(item) == front_expiry]

    def _front_expiry_snapshots(
        self,
        *,
        snapshot_rows: list[dict[str, Any]],
        timestamp: pd.Timestamp,
        preferred_expiry_kind: str,
    ) -> list[dict[str, Any]]:
        if preferred_expiry_kind != "weekly":
            return snapshot_rows
        as_of_date = timestamp.date()
        expiries: list[datetime.date] = []
        for item in snapshot_rows:
            expiry = item.get("expiry")
            if not expiry:
                continue
            expiry_date = self._snapshot_expiry_date(item)
            if expiry_date >= as_of_date:
                expiries.append(expiry_date)
        if not expiries:
            return snapshot_rows
        front_expiry = min(expiries)
        if (front_expiry - as_of_date).days > float(self.config.get("preferred_weekly_days", 8)):
            return snapshot_rows
        normalized_rows: list[dict[str, Any]] = []
        for item in snapshot_rows:
            if not item.get("expiry") or self._snapshot_expiry_date(item) != front_expiry:
                continue
            row = dict(item)
            normalized_expiry = self._snapshot_expiry_date(row)
            if normalized_expiry.isoformat() != str(row.get("expiry") or "")[:10]:
                row["raw_expiry"] = row.get("expiry")
                row["expiry"] = normalized_expiry.isoformat()
                row["expiry_kind"] = "monthly"
            normalized_rows.append(row)
        return normalized_rows

    def _score_contract(
        self,
        *,
        meta: ContractMeta,
        timestamp: pd.Timestamp,
        spot_price: float,
        sigma: float,
        row,
        signal: DirectionalSignal,
        regime: RegimeSnapshot,
        horizon_years: float,
        risk_free_rate: float,
        delta_mid: float,
        atm_iv: float,
    ) -> Optional[ContractCandidate]:
        bar = self.store.latest_contract_bar(meta, timestamp)
        if bar is None:
            return None

        option_price = float(bar.get("close", 0.0))
        volume = float(bar.get("volume", 0.0))
        oi = float(bar.get("oi", 0.0))
        if option_price <= 0.0:
            return None

        expiry_date = self._contract_expiry_date(meta)
        expiry_kind = self._contract_expiry_kind(meta)
        days_to_expiry = max((expiry_date - timestamp.date()).days + (1.0 - float(timestamp.hour / 24.0)), 0.25)
        time_to_expiry_years = max(days_to_expiry / 365.0, 1.0 / 3650.0)
        delta, gamma, theta, vega = _black_scholes_greeks(
            spot=spot_price,
            strike=meta.strike,
            time_to_expiry_years=time_to_expiry_years,
            sigma=sigma,
            risk_free_rate=risk_free_rate,
            option_type=meta.option_type,
        )
        delta_abs = abs(delta)
        moneyness_pct = abs(meta.strike - spot_price) / max(spot_price, 1.0)
        spread_pct = min(
            float(self.config["fallback_spread_pct"]),
            max(
                0.01,
                0.04 + (120.0 / max(volume, 120.0)) + (1_500.0 / max(oi, 1_500.0)) * 0.01 + moneyness_pct * 0.85,
            ),
        )
        slippage_pct = spread_pct * 0.28
        liquidity_score = max(
            0.0,
            min(
                1.0,
                0.45
                + min(volume / 2_000.0, 0.35)
                + min(oi / 20_000.0, 0.35)
                - min(spread_pct / 0.15, 0.4),
            ),
        )
        # Hard liquidity/spread filter — relaxed for commodities and for
        # learning sleeves (same logic as _score_snapshot_contract; this
        # path uses meta.trading_symbol because it's iterating local
        # ContractMeta records rather than live snapshot dicts).
        _trading_symbol = str(getattr(meta, "trading_symbol", "") or "").upper()
        _is_commodity = _trading_symbol.startswith("MCX:") or _trading_symbol.startswith("MCX_")
        _learning_sleeve = str(signal.sleeve or "").lower() in {
            "intraday_exploration", "intraday_micro_trend",
        }
        _relax = _is_commodity or _learning_sleeve
        _max_spread = float(
            self.config["fallback_spread_pct"] if _relax else self.config["max_spread_pct"]
        )
        _min_vol = 0.0 if _relax else float(self.config["min_volume"])
        _min_oi = 0.0 if _relax else float(self.config["min_oi"])
        if (
            volume < _min_vol
            or oi < _min_oi
            or spread_pct > _max_spread
        ):
            return None

        iv_value_score = max(0.0, min(1.0, 1.1 - sigma / 0.6 - moneyness_pct * 2.0))
        delta_fit = max(0.0, 1.0 - abs(delta_abs - delta_mid) / 0.35)
        theta_penalty = abs(theta) * horizon_years / max(option_price, 1.0)
        spread_cost = option_price * spread_pct
        slippage_cost = option_price * slippage_pct
        fees = 2.0 * 0.45
        signed_move = signal.expected_move if meta.option_type == "CE" else -signal.expected_move
        greek_expected_pnl = (
            (delta * signed_move)
            + (0.5 * gamma * (signal.expected_move ** 2))
            + (vega * signal.expected_iv_change)
            - (abs(theta) * horizon_years)
            - spread_cost
            - slippage_cost
            - fees
        )
        distributional = self._distributional_metrics(
            underlying=meta.underlying,
            option_type=meta.option_type,
            spot_price=spot_price,
            strike=meta.strike,
            option_price=option_price,
            sigma=sigma,
            atm_iv=atm_iv,
            time_to_expiry_years=time_to_expiry_years,
            horizon_years=horizon_years,
            risk_free_rate=risk_free_rate,
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            fees=fees,
            theta=theta,
            delta_abs=delta_abs,
            liquidity_score=liquidity_score,
            signal=signal,
        )
        expected_pnl = distributional["trading_edge"]

        weights = self.config["score_weights"]
        score = (
            (weights["direction"] * signal.confidence * delta_fit)
            + (weights["expected_pnl"] * max(-1.0, min(expected_pnl / max(option_price, 1.0), 2.0)))
            + (weights["liquidity"] * liquidity_score)
            + (weights["iv_value"] * iv_value_score)
            + (weights.get("tail_edge", 0.0) * distributional["tail_edge"])
            + (weights.get("timing_fit", 0.0) * distributional["timing_fit"])
            - (weights["theta_penalty"] * theta_penalty)
            - (weights["slippage_penalty"] * (spread_pct + slippage_pct))
            - (weights.get("skew_tax", 0.0) * distributional["skew_tax"])
            - (weights.get("model_uncertainty", 0.0) * distributional["model_uncertainty"])
        )
        score += max(-12.0, min(greek_expected_pnl / max(option_price, 1.0), 1.0) * 6.0)

        selection_reason = (
            f"{expiry_kind} {meta.option_type} with {delta_abs:.2f} delta, "
            f"{distributional['tail_edge']:+.2f} p-minus-q tail gap, "
            f"{distributional['timing_fit']:.0%} timing fit, and {expected_pnl:.2f} net trading edge."
        )

        return ContractCandidate(
            trading_symbol=meta.trading_symbol,
            file_path=meta.file_path,
            option_type=meta.option_type,
            expiry=expiry_date.isoformat(),
            expiry_kind=expiry_kind,
            strike=float(meta.strike),
            lot_size=int(meta.lot_size),
            tick_size=float(meta.tick_size),
            option_price=round(option_price, 2),
            volume=round(volume, 2),
            oi=round(oi, 2),
            days_to_expiry=round(days_to_expiry, 2),
            moneyness_pct=round(moneyness_pct, 4),
            implied_vol=round(sigma, 4),
            delta=round(delta, 4),
            gamma=round(gamma, 6),
            theta=round(theta, 4),
            vega=round(vega, 4),
            delta_bucket=_delta_bucket(delta_abs),
            liquidity_score=round(liquidity_score, 4),
            iv_value_score=round(iv_value_score, 4),
            theta_penalty=round(theta_penalty, 4),
            spread_pct=round(spread_pct, 4),
            slippage_pct=round(slippage_pct, 4),
            spread_cost=round(spread_cost, 2),
            slippage_cost=round(slippage_cost, 2),
            fees=round(fees, 2),
            expected_pnl=round(expected_pnl, 2),
            contract_score=round(score, 2),
            selection_reason=selection_reason,
            q_price=round(option_price, 2),
            p_terminal_edge=round(distributional["terminal_edge"], 2),
            p_trading_edge=round(distributional["trading_edge"], 2),
            p_tail=round(distributional["p_tail"], 4),
            q_tail=round(distributional["q_tail"], 4),
            p_minus_q_tail=round(distributional["tail_edge"], 4),
            expected_return_on_premium=round(distributional["return_on_premium"], 4),
            probability_of_profit=round(distributional["probability_of_profit"], 4),
            probability_of_50pct_loss=round(distributional["probability_of_50pct_loss"], 4),
            probability_of_total_loss=round(distributional["probability_of_total_loss"], 4),
            timing_fit=round(distributional["timing_fit"], 4),
            skew_tax=round(distributional["skew_tax"], 4),
            model_confidence=round(signal.confidence, 4),
            model_error_buffer=round(distributional["model_error_buffer"], 2),
            theta_cost=round(abs(theta) * horizon_years, 2),
            iv_tail_edge_bonus=round(distributional["iv_tail_edge_bonus"], 4),
            expiry_score=round(distributional["expiry_score"], 4),
            utility=round(score, 2),
            rejection_reasons=distributional["rejection_reasons"],
        )

    def _score_snapshot_contract(
        self,
        *,
        snapshot: dict[str, Any],
        timestamp: pd.Timestamp,
        spot_price: float,
        default_sigma: float,
        signal: DirectionalSignal,
        horizon_years: float,
        risk_free_rate: float,
        delta_mid: float,
        atm_iv: float,
    ) -> Optional[ContractCandidate]:
        option_price = float(snapshot.get("ltp") or 0.0)
        volume = float(snapshot.get("volume") or 0.0)
        oi = float(snapshot.get("oi") or 0.0)
        strike = float(snapshot.get("strike") or 0.0)
        if option_price <= 0.0 or strike <= 0.0:
            return None

        expiry_date = self._snapshot_expiry_date(snapshot)
        expiry_kind = self._snapshot_expiry_kind(snapshot)
        days_to_expiry = max((expiry_date - timestamp.date()).days + (1.0 - float(timestamp.hour / 24.0)), 0.25)
        time_to_expiry_years = max(days_to_expiry / 365.0, 1.0 / 3650.0)
        snap_iv = _normalize_iv(snapshot.get("iv"))
        sigma = min(
            max(snap_iv or float(default_sigma or 0.0), float(self.config["sigma_floor"])),
            float(self.config["sigma_ceiling"]),
        )
        delta, gamma, theta, vega = _black_scholes_greeks(
            spot=spot_price,
            strike=strike,
            time_to_expiry_years=time_to_expiry_years,
            sigma=sigma,
            risk_free_rate=risk_free_rate,
            option_type=str(snapshot.get("option_type") or signal.direction),
        )
        delta_abs = abs(delta)
        moneyness_pct = abs(strike - spot_price) / max(spot_price, 1.0)
        spread_pct = min(
            float(self.config["fallback_spread_pct"]),
            max(
                0.01,
                0.04 + (120.0 / max(volume, 120.0)) + (1_500.0 / max(oi, 1_500.0)) * 0.01 + moneyness_pct * 0.85,
            ),
        )
        slippage_pct = spread_pct * 0.28
        liquidity_score = max(
            0.0,
            min(
                1.0,
                0.45
                + min(volume / 2_000.0, 0.35)
                + min(oi / 20_000.0, 0.35)
                - min(spread_pct / 0.15, 0.4),
            ),
        )
        # Hard liquidity/spread filter — relaxed for commodities and for
        # learning sleeves so the agent can take small bets on thinly-traded
        # MCX option chains (typical commodity weeklies have low explicit
        # volume/OI in the cached watchlist row even when broker liquidity
        # is acceptable). Capital gates downstream still keep size honest.
        _trading_symbol = str(snapshot.get("trading_symbol") or "").upper()
        _is_commodity = _trading_symbol.startswith("MCX:") or _trading_symbol.startswith("MCX_")
        _learning_sleeve = str(signal.sleeve or "").lower() in {
            "intraday_exploration", "intraday_micro_trend",
        }
        _relax = _is_commodity or _learning_sleeve
        _max_spread = float(
            self.config["fallback_spread_pct"] if _relax else self.config["max_spread_pct"]
        )
        _min_vol = 0.0 if _relax else float(self.config["min_volume"])
        _min_oi = 0.0 if _relax else float(self.config["min_oi"])
        if (
            volume < _min_vol
            or oi < _min_oi
            or spread_pct > _max_spread
        ):
            return None

        iv_value_score = max(0.0, min(1.0, 1.1 - sigma / 0.6 - moneyness_pct * 2.0))
        delta_fit = max(0.0, 1.0 - abs(delta_abs - delta_mid) / 0.35)
        theta_penalty = abs(theta) * horizon_years / max(option_price, 1.0)
        spread_cost = option_price * spread_pct
        slippage_cost = option_price * slippage_pct
        fees = 2.0 * 0.45
        option_type = str(snapshot.get("option_type") or signal.direction)
        signed_move = signal.expected_move if option_type == "CE" else -signal.expected_move
        greek_expected_pnl = (
            (delta * signed_move)
            + (0.5 * gamma * (signal.expected_move ** 2))
            + (vega * signal.expected_iv_change)
            - (abs(theta) * horizon_years)
            - spread_cost
            - slippage_cost
            - fees
        )
        distributional = self._distributional_metrics(
            underlying=str(snapshot.get("underlying") or ""),
            option_type=option_type,
            spot_price=spot_price,
            strike=strike,
            option_price=option_price,
            sigma=sigma,
            atm_iv=atm_iv,
            time_to_expiry_years=time_to_expiry_years,
            horizon_years=horizon_years,
            risk_free_rate=risk_free_rate,
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            fees=fees,
            theta=theta,
            delta_abs=delta_abs,
            liquidity_score=liquidity_score,
            signal=signal,
        )
        expected_pnl = distributional["trading_edge"]

        weights = self.config["score_weights"]
        score = (
            (weights["direction"] * signal.confidence * delta_fit)
            + (weights["expected_pnl"] * max(-1.0, min(expected_pnl / max(option_price, 1.0), 2.0)))
            + (weights["liquidity"] * liquidity_score)
            + (weights["iv_value"] * iv_value_score)
            + (weights.get("tail_edge", 0.0) * distributional["tail_edge"])
            + (weights.get("timing_fit", 0.0) * distributional["timing_fit"])
            - (weights["theta_penalty"] * theta_penalty)
            - (weights["slippage_penalty"] * (spread_pct + slippage_pct))
            - (weights.get("skew_tax", 0.0) * distributional["skew_tax"])
            - (weights.get("model_uncertainty", 0.0) * distributional["model_uncertainty"])
        )
        score += max(-12.0, min(greek_expected_pnl / max(option_price, 1.0), 1.0) * 6.0)

        trading_symbol = str(snapshot.get("trading_symbol") or snapshot.get("instrument_key") or "")
        selection_reason = (
            f"Local {expiry_kind} {option_type} with {delta_abs:.2f} delta, "
            f"{distributional['tail_edge']:+.2f} p-minus-q tail gap, "
            f"{distributional['timing_fit']:.0%} timing fit, and {expected_pnl:.2f} net trading edge."
        )
        return ContractCandidate(
            trading_symbol=trading_symbol or f"{snapshot.get('underlying')} {strike:.0f} {option_type}",
            file_path=f"live:{snapshot.get('instrument_key') or trading_symbol or strike}",
            option_type=option_type,
            expiry=expiry_date.isoformat(),
            expiry_kind=expiry_kind,
            strike=strike,
            lot_size=int(snapshot.get("lot_size") or 1),
            tick_size=float(snapshot.get("tick_size") or 0.05),
            option_price=round(option_price, 2),
            volume=round(volume, 2),
            oi=round(oi, 2),
            days_to_expiry=round(days_to_expiry, 2),
            moneyness_pct=round(moneyness_pct, 4),
            implied_vol=round(sigma, 4),
            delta=round(delta, 4),
            gamma=round(gamma, 6),
            theta=round(theta, 4),
            vega=round(vega, 4),
            delta_bucket=_delta_bucket(delta_abs),
            liquidity_score=round(liquidity_score, 4),
            iv_value_score=round(iv_value_score, 4),
            theta_penalty=round(theta_penalty, 4),
            spread_pct=round(spread_pct, 4),
            slippage_pct=round(slippage_pct, 4),
            spread_cost=round(spread_cost, 2),
            slippage_cost=round(slippage_cost, 2),
            fees=round(fees, 2),
            expected_pnl=round(expected_pnl, 2),
            contract_score=round(score, 2),
            selection_reason=selection_reason,
            q_price=round(option_price, 2),
            p_terminal_edge=round(distributional["terminal_edge"], 2),
            p_trading_edge=round(distributional["trading_edge"], 2),
            p_tail=round(distributional["p_tail"], 4),
            q_tail=round(distributional["q_tail"], 4),
            p_minus_q_tail=round(distributional["tail_edge"], 4),
            expected_return_on_premium=round(distributional["return_on_premium"], 4),
            probability_of_profit=round(distributional["probability_of_profit"], 4),
            probability_of_50pct_loss=round(distributional["probability_of_50pct_loss"], 4),
            probability_of_total_loss=round(distributional["probability_of_total_loss"], 4),
            timing_fit=round(distributional["timing_fit"], 4),
            skew_tax=round(distributional["skew_tax"], 4),
            model_confidence=round(signal.confidence, 4),
            model_error_buffer=round(distributional["model_error_buffer"], 2),
            theta_cost=round(abs(theta) * horizon_years, 2),
            iv_tail_edge_bonus=round(distributional["iv_tail_edge_bonus"], 4),
            expiry_score=round(distributional["expiry_score"], 4),
            utility=round(score, 2),
            rejection_reasons=distributional["rejection_reasons"],
            instrument_key=str(snapshot.get("instrument_key") or "") or None,
            price_source="local_watchlist",
        )

    def _estimate_snapshot_atm_iv(self, snapshot_rows: list[dict[str, Any]], spot_price: float, fallback: float) -> float:
        valid = [
            (abs(float(row.get("strike") or 0.0) - spot_price), _normalize_iv(row.get("iv")))
            for row in snapshot_rows
            if _normalize_iv(row.get("iv")) > 0.0 and float(row.get("strike") or 0.0) > 0.0
        ]
        if not valid:
            return fallback
        valid.sort(key=lambda item: item[0])
        return valid[0][1]

    def _distributional_metrics(
        self,
        *,
        underlying: str,
        option_type: str,
        spot_price: float,
        strike: float,
        option_price: float,
        sigma: float,
        atm_iv: float,
        time_to_expiry_years: float,
        horizon_years: float,
        risk_free_rate: float,
        spread_cost: float,
        slippage_cost: float,
        fees: float,
        theta: float,
        delta_abs: float,
        liquidity_score: float,
        signal: DirectionalSignal,
    ) -> dict[str, Any]:
        optimizer_cfg = self.config.get("distributional_optimizer", {})
        signed_move = signal.expected_move if option_type == "CE" else -signal.expected_move
        horizon_stdev = max(
            spot_price * sigma * math.sqrt(max(horizon_years, 1.0 / (252.0 * 375.0))),
            signal.expected_move * (0.42 + signal.model_uncertainty),
            spot_price * 0.0015,
        )
        terminal_stdev = max(
            spot_price * sigma * math.sqrt(max(time_to_expiry_years, horizon_years)),
            horizon_stdev,
        )
        horizon_mean = spot_price + signed_move
        terminal_mean = spot_price + signed_move * min(1.35, max(0.65, math.sqrt(time_to_expiry_years / max(horizon_years, 1e-6)) * 0.55))
        discount = math.exp(-risk_free_rate * time_to_expiry_years)
        round_trip_costs = spread_cost + slippage_cost + fees
        model_error_buffer = option_price * (
            float(optimizer_cfg.get("model_error_base_pct", 0.035)) + signal.model_uncertainty * 0.18
        )

        terminal_payoff = _normal_expected_payoff(terminal_mean, terminal_stdev, strike, option_type)
        terminal_edge = (discount * terminal_payoff) - option_price - round_trip_costs - model_error_buffer
        remaining_years = max(time_to_expiry_years - horizon_years, 1.0 / 3650.0)
        future_iv = min(
            float(self.config["sigma_ceiling"]),
            max(float(self.config["sigma_floor"]), sigma + signal.expected_iv_change),
        )
        future_option_value = _black_scholes_price(
            spot=max(horizon_mean, 1.0),
            strike=strike,
            time_to_expiry_years=remaining_years,
            sigma=future_iv,
            risk_free_rate=risk_free_rate,
            option_type=option_type,
        )
        trading_edge = future_option_value - option_price - round_trip_costs - model_error_buffer
        p_tail = _normal_tail(terminal_mean, terminal_stdev, strike, option_type)
        q_tail = _risk_neutral_tail(
            spot=spot_price,
            strike=strike,
            time_to_expiry_years=time_to_expiry_years,
            sigma=sigma,
            risk_free_rate=risk_free_rate,
            option_type=option_type,
        )
        tail_edge = p_tail - q_tail
        profit_threshold = strike + option_price + round_trip_costs if option_type == "CE" else strike - option_price - round_trip_costs
        probability_of_profit = _normal_tail(terminal_mean, terminal_stdev, profit_threshold, option_type)
        half_loss_threshold = strike + (option_price * 0.5) if option_type == "CE" else strike - (option_price * 0.5)
        probability_of_50pct_loss = 1.0 - _normal_tail(terminal_mean, terminal_stdev, half_loss_threshold, option_type)
        probability_of_total_loss = 1.0 - p_tail

        horizon_days = max(horizon_years * 365.0, 0.05)
        dte_days = max(time_to_expiry_years * 365.0, 0.25)
        life_ratio = min(1.0, max(0.0, (dte_days - horizon_days) / max(dte_days, 1.0)))
        timing_fit = min(1.0, max(0.0, signal.timing_precision * 0.65 + life_ratio * 0.35))
        theta_cost = abs(theta) * horizon_years
        theta_penalty = theta_cost / max(option_price, 1.0)
        iv_richness = max(0.0, sigma - max(atm_iv, 0.01))
        index_put_tax = float(optimizer_cfg.get("index_put_skew_tax", 0.035)) if option_type == "PE" and underlying.upper() in {"NIFTY", "BANKNIFTY", "SENSEX"} else 0.0
        skew_tax = max(0.0, iv_richness * 0.75 + index_put_tax + max(0.0, 0.35 - delta_abs) * 0.18)
        weekly_tax = float(optimizer_cfg.get("event_variance_premium", 0.025)) if dte_days <= float(self.config.get("preferred_weekly_days", 8)) and timing_fit < 0.62 else 0.0
        skew_tax += weekly_tax
        iv_tail_edge_bonus = max(0.0, tail_edge) * max(0.0, 1.0 - skew_tax) + max(signal.expected_iv_change, 0.0) * 2.0
        expiry_score = max(0.0, min(1.0, timing_fit + min(delta_abs, 0.7) * 0.15 + liquidity_score * 0.15 - theta_penalty - weekly_tax))
        return_on_premium = trading_edge / max(option_price, 1.0)

        rejection_reasons: list[str] = []
        if trading_edge <= option_price * float(optimizer_cfg.get("min_net_edge_pct", 0.025)):
            rejection_reasons.append("net trading edge below hurdle")
        if probability_of_profit < float(optimizer_cfg.get("min_probability_of_profit", 0.38)):
            rejection_reasons.append("probability of profit below minimum")
        if liquidity_score < float(optimizer_cfg.get("min_liquidity_score", 0.35)):
            rejection_reasons.append("liquidity score below minimum")
        if timing_fit < float(optimizer_cfg.get("min_timing_fit", 0.28)):
            rejection_reasons.append("timing fit below minimum")
        if skew_tax > float(optimizer_cfg.get("max_skew_tax", 0.22)):
            rejection_reasons.append("skew tax too high")
        if delta_abs < 0.35 and (
            signal.jump_score < float(optimizer_cfg.get("otm_jump_threshold", 0.42))
            or signal.timing_precision < float(optimizer_cfg.get("otm_timing_threshold", 0.58))
        ):
            rejection_reasons.append("OTM option requires stronger jump score and timing precision")

        return {
            "terminal_edge": terminal_edge,
            "trading_edge": trading_edge,
            "p_tail": max(0.0, min(1.0, p_tail)),
            "q_tail": max(0.0, min(1.0, q_tail)),
            "tail_edge": tail_edge,
            "return_on_premium": return_on_premium,
            "probability_of_profit": max(0.0, min(1.0, probability_of_profit)),
            "probability_of_50pct_loss": max(0.0, min(1.0, probability_of_50pct_loss)),
            "probability_of_total_loss": max(0.0, min(1.0, probability_of_total_loss)),
            "timing_fit": timing_fit,
            "skew_tax": skew_tax,
            "model_uncertainty": signal.model_uncertainty,
            "model_error_buffer": model_error_buffer,
            "iv_tail_edge_bonus": iv_tail_edge_bonus,
            "expiry_score": expiry_score,
            "rejection_reasons": rejection_reasons,
        }
