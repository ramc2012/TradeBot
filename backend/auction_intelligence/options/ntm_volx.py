from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from brokers.base import OptionChain, OptionChainEntry

from auction_intelligence.schemas import NTMVolXLevel, NTMVolXSnapshot


_DEFAULT_CONFIG = {
    "enabled": True,
    "max_pairs": 5,
    "max_spread_pct": 0.18,
    "min_distance_weight": 0.45,
    "distance_decay": 0.12,
    "oi_change_multiplier": 0.35,
    "balance_ratio": 1.15,
    "control_ratio": 1.5,
    "extreme_ratio": 2.25,
}


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


class NTMVolXAnalyzer:
    """Approximate Vtrender-style NTM call/put control from public option-chain fields.

    Vtrender's public material describes NTM VolX as a near-the-money volume/control
    lens focused on whether calls or puts are pressing harder. Their exact formula is
    not public, so this implementation uses a transparent proxy from:
    - near-the-money premium turnover
    - positive open-interest change
    - liquidity quality via spread
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = {
            **_DEFAULT_CONFIG,
            **(config or {}),
        }

    def analyze_chain(
        self,
        *,
        underlying: str,
        expiry: str,
        chain: OptionChain,
    ) -> Optional[NTMVolXSnapshot]:
        if not self.config.get("enabled", True):
            return None

        spot_price = float(chain.spot_price or 0.0)
        if spot_price <= 0:
            return None

        pairs = self._select_ntm_pairs(chain)
        if not pairs:
            return None

        snapshot_time = datetime.now(timezone.utc).isoformat()
        levels: list[NTMVolXLevel] = []
        call_pressure_total = 0.0
        put_pressure_total = 0.0
        call_volume_total = 0.0
        put_volume_total = 0.0
        call_notional_total = 0.0
        put_notional_total = 0.0
        call_oi_change_total = 0.0
        put_oi_change_total = 0.0

        for index, (strike, pair) in enumerate(pairs):
            distance_weight = max(
                float(self.config.get("min_distance_weight", 0.45)),
                1.0 - (index * float(self.config.get("distance_decay", 0.12))),
            )
            call_metrics = self._entry_metrics(pair["CE"], distance_weight=distance_weight)
            put_metrics = self._entry_metrics(pair["PE"], distance_weight=distance_weight)
            total_pressure = call_metrics["pressure"] + put_metrics["pressure"]
            net_pressure = (
                (call_metrics["pressure"] - put_metrics["pressure"]) / total_pressure
                if total_pressure > 0
                else 0.0
            )
            level = NTMVolXLevel(
                strike=round(float(strike), 4),
                distance_from_spot=round(abs(float(strike) - spot_price), 4),
                distance_from_spot_pct=round(abs(float(strike) - spot_price) / max(spot_price, 1.0), 6),
                call_volume=round(call_metrics["volume"], 4),
                put_volume=round(put_metrics["volume"], 4),
                call_notional=round(call_metrics["notional"], 4),
                put_notional=round(put_metrics["notional"], 4),
                call_oi_change=round(call_metrics["oi_change"], 4),
                put_oi_change=round(put_metrics["oi_change"], 4),
                call_pressure=round(call_metrics["pressure"], 4),
                put_pressure=round(put_metrics["pressure"], 4),
                net_pressure=round(net_pressure, 4),
                observed_at=snapshot_time,
                source="live_option_chain",
            )
            levels.append(level)
            call_pressure_total += call_metrics["pressure"]
            put_pressure_total += put_metrics["pressure"]
            call_volume_total += call_metrics["volume"]
            put_volume_total += put_metrics["volume"]
            call_notional_total += call_metrics["notional"]
            put_notional_total += put_metrics["notional"]
            call_oi_change_total += call_metrics["oi_change"]
            put_oi_change_total += put_metrics["oi_change"]

        levels.sort(key=lambda item: item.strike)
        call_wall_strike = max(levels, key=lambda item: item.call_pressure).strike if any(
            item.call_pressure > 0 for item in levels
        ) else None
        put_wall_strike = max(levels, key=lambda item: item.put_pressure).strike if any(
            item.put_pressure > 0 for item in levels
        ) else None

        dominant_side, directional_bias = self._dominant_side(call_pressure_total, put_pressure_total)
        vxr = self._vxr(call_pressure_total, put_pressure_total)
        total_pressure = call_pressure_total + put_pressure_total
        net_pressure = (
            (call_pressure_total - put_pressure_total) / total_pressure
            if total_pressure > 0
            else 0.0
        )
        regime = self._regime(dominant_side, vxr)
        atm_strike = min((item.strike for item in levels), key=lambda strike: abs(strike - spot_price))

        notes = [
            f"{dominant_side.title() if dominant_side != 'BALANCED' else 'Balanced'} control at {vxr:.2f}x across {len(levels)} NTM pairs.",
            f"Call wall {call_wall_strike:.0f}" if call_wall_strike is not None else "Call wall unavailable.",
            f"Put wall {put_wall_strike:.0f}" if put_wall_strike is not None else "Put wall unavailable.",
        ]

        return NTMVolXSnapshot(
            underlying=underlying,
            expiry=expiry,
            spot_price=round(spot_price, 4),
            atm_strike=round(float(atm_strike), 4),
            dominant_side=dominant_side,
            directional_bias=directional_bias,
            regime=regime,
            vxr=round(vxr, 4),
            call_pressure=round(call_pressure_total, 4),
            put_pressure=round(put_pressure_total, 4),
            net_pressure=round(net_pressure, 4),
            call_volume=round(call_volume_total, 4),
            put_volume=round(put_volume_total, 4),
            call_notional=round(call_notional_total, 4),
            put_notional=round(put_notional_total, 4),
            call_oi_change=round(call_oi_change_total, 4),
            put_oi_change=round(put_oi_change_total, 4),
            call_wall_strike=call_wall_strike,
            put_wall_strike=put_wall_strike,
            pair_count=len(levels),
            snapshot_time=snapshot_time,
            source="live_option_chain",
            notes=notes,
            pressure_ladder=levels,
        )

    def _select_ntm_pairs(self, chain: OptionChain) -> list[tuple[float, dict[str, OptionChainEntry]]]:
        grouped: dict[float, dict[str, OptionChainEntry]] = {}
        for entry in chain.entries:
            option_type = str(entry.option_type or "").upper()
            if option_type not in {"CE", "PE"}:
                continue
            if float(entry.ltp or 0.0) <= 0 and float(entry.volume or 0.0) <= 0:
                continue
            grouped.setdefault(float(entry.strike), {})[option_type] = entry

        spot_price = float(chain.spot_price or 0.0)
        candidates = [
            (strike, pair)
            for strike, pair in grouped.items()
            if "CE" in pair and "PE" in pair
        ]
        candidates.sort(key=lambda item: (abs(item[0] - spot_price), item[0]))
        return candidates[: max(int(self.config.get("max_pairs", 5)), 1)]

    def _entry_metrics(self, entry: OptionChainEntry, *, distance_weight: float) -> dict[str, float]:
        ltp = max(float(entry.ltp or 0.0), 0.0)
        volume = max(float(entry.volume or 0.0), 0.0)
        notional = ltp * volume
        oi = max(float(entry.oi or 0.0), 0.0)
        prev_oi = max(float(entry.prev_oi or 0.0), 0.0)
        oi_change = max(oi - prev_oi, 0.0) if prev_oi > 0 else 0.0
        prev_close = max(float(entry.prev_close or 0.0), 0.0)
        price_change_pct = ((ltp - prev_close) / prev_close) if prev_close > 0 else 0.0
        price_change_pct = max(price_change_pct, 0.0)
        spread_pct = self._spread_pct(entry, ltp)
        liquidity_weight = _clamp(
            1.0 - ((spread_pct / max(float(self.config.get("max_spread_pct", 0.18)), 0.001)) * 0.55),
            0.35,
            1.0,
        )
        premium_weight = 1.0 + min(price_change_pct, 1.5) * 0.75
        oi_notional = oi_change * max(ltp, 1.0) * float(self.config.get("oi_change_multiplier", 0.35))
        pressure = distance_weight * liquidity_weight * (notional + oi_notional) * premium_weight
        return {
            "volume": volume,
            "notional": notional,
            "oi_change": oi_change,
            "pressure": pressure,
        }

    def _spread_pct(self, entry: OptionChainEntry, premium: float) -> float:
        bid = float(entry.bid or 0.0)
        ask = float(entry.ask or 0.0)
        if bid <= 0 or ask <= 0 or premium <= 0:
            return 0.0
        spread = max(ask - bid, 0.0)
        return spread / max(premium, 0.01)

    def _vxr(self, call_pressure: float, put_pressure: float) -> float:
        dominant = max(call_pressure, put_pressure, 0.0)
        opposing = max(min(call_pressure, put_pressure), 0.0)
        if dominant <= 0:
            return 1.0
        if opposing <= 0:
            return 6.0
        return min(dominant / opposing, 6.0)

    def _dominant_side(self, call_pressure: float, put_pressure: float) -> tuple[str, str]:
        balance_ratio = float(self.config.get("balance_ratio", 1.15))
        vxr = self._vxr(call_pressure, put_pressure)
        if vxr < balance_ratio:
            return "BALANCED", "FLAT"
        if call_pressure > put_pressure:
            return "CALLS", "LONG"
        if put_pressure > call_pressure:
            return "PUTS", "SHORT"
        return "BALANCED", "FLAT"

    def _regime(self, dominant_side: str, vxr: float) -> str:
        if dominant_side == "BALANCED":
            return "balanced"
        control_ratio = float(self.config.get("control_ratio", 1.5))
        extreme_ratio = float(self.config.get("extreme_ratio", 2.25))
        prefix = "calls" if dominant_side == "CALLS" else "puts"
        if vxr >= extreme_ratio:
            return f"{prefix}_extreme"
        if vxr >= control_ratio:
            return f"{prefix}_control"
        return f"{prefix}_lean"
