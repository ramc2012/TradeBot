"""Contract discovery and scoring for long call/put trades."""
from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from directional_options.data import DirectionalOptionsDataStore
from directional_options.features import timeframe_minutes
from directional_options.schemas import ContractCandidate, ContractMeta, DirectionalSignal, RegimeSnapshot


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
    """Choose the contract whose expected convexity clears carry and friction."""

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
        contracts = sorted(
            contracts,
            key=lambda meta: (
                0 if meta.expiry_kind == expiry_preference else 1,
                abs(meta.strike - spot_price),
                meta.expiry,
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
    ) -> Optional[ContractCandidate]:
        bar = self.store.latest_contract_bar(meta, timestamp)
        if bar is None:
            return None

        option_price = float(bar.get("close", 0.0))
        volume = float(bar.get("volume", 0.0))
        oi = float(bar.get("oi", 0.0))
        if option_price <= 0.0:
            return None

        expiry_dt = pd.Timestamp(meta.expiry)
        days_to_expiry = max((expiry_dt.date() - timestamp.date()).days + (1.0 - float(timestamp.hour / 24.0)), 0.25)
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
        if (
            volume < float(self.config["min_volume"])
            or oi < float(self.config["min_oi"])
            or spread_pct > float(self.config["max_spread_pct"])
        ):
            return None

        iv_value_score = max(0.0, min(1.0, 1.1 - sigma / 0.6 - moneyness_pct * 2.0))
        delta_fit = max(0.0, 1.0 - abs(delta_abs - delta_mid) / 0.35)
        theta_penalty = abs(theta) * horizon_years / max(option_price, 1.0)
        spread_cost = option_price * spread_pct
        slippage_cost = option_price * slippage_pct
        fees = 2.0 * 0.45
        expected_pnl = (
            (delta * signal.expected_move)
            + (0.5 * gamma * (signal.expected_move ** 2))
            + (vega * signal.expected_iv_change)
            - (abs(theta) * horizon_years)
            - spread_cost
            - slippage_cost
            - fees
        )

        weights = self.config["score_weights"]
        score = (
            (weights["direction"] * signal.confidence * delta_fit)
            + (weights["expected_pnl"] * max(-1.0, min(expected_pnl / max(option_price, 1.0), 2.0)))
            + (weights["liquidity"] * liquidity_score)
            + (weights["iv_value"] * iv_value_score)
            - (weights["theta_penalty"] * theta_penalty)
            - (weights["slippage_penalty"] * (spread_pct + slippage_pct))
        )

        selection_reason = (
            f"{meta.expiry_kind} {meta.option_type} with {delta_abs:.2f} delta, "
            f"{liquidity_score:.0%} liquidity score, and {expected_pnl:.2f} expected PnL."
        )

        return ContractCandidate(
            trading_symbol=meta.trading_symbol,
            file_path=meta.file_path,
            option_type=meta.option_type,
            expiry=meta.expiry,
            expiry_kind=meta.expiry_kind,
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
        )
