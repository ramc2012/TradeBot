"""Tier 1 Rules Engine — fast condition checks (<200ms).

Evaluates MACD quadrant regime, IV filter, spot MA context, and legacy
market-structure rules.  Called by strategy_agent before opening trades.

Per STRATEGY_DOCUMENT.md §6, each rule returns RulesFlags which the
strategy agent aggregates to decide entry eligibility and sizing mode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from loguru import logger

from agent.strategy_config import (
    MAX_ENTRY_IV_PCT,
    HARD_MAX_IV_PCT,
    REGIME_BULLISH,
    REGIME_BEARISH,
    REGIME_DEAD,
    REGIME_IV_SPIKE,
    SETUP_BREAKOUT,
    SETUP_TREND,
    SETUP_REVERSAL,
    SETUP_PREMIUM,
    CIRCUIT,
)


@dataclass
class RulesFlag:
    rule_name: str
    symbol: str
    details: dict
    triggered_at: datetime = field(default_factory=datetime.utcnow)
    priority: str = "NORMAL"   # HIGH / NORMAL / LOW


class RulesEngine:
    """
    Evaluates pre-defined conditions on market data.
    Returns a list of RulesFlag for any conditions triggered.
    """

    def __init__(self):
        self._enabled_rules = {
            # Strategy-core rules
            "macd_quadrant": True,
            "iv_filter": True,
            "spot_ma_context": True,
            # Legacy market-structure rules
            "iv_rank_high": True,
            "pcr_extreme": True,
            "market_profile_breakout": True,
            "poc_reclaim": True,
            "vix_spike": True,
            "oi_buildup": True,
        }

    def evaluate(
        self,
        symbol: str,
        option_chain: Optional[dict] = None,
        market_profile: Optional[dict] = None,
        iv_rank: Optional[dict] = None,
        ltp: float = 0.0,
        *,
        regime: Optional[str] = None,
        ce_macd_value: Optional[float] = None,
        pe_macd_value: Optional[float] = None,
        iv_pct: Optional[float] = None,
        spot_setup: Optional[str] = None,
        vix: Optional[float] = None,
    ) -> List[RulesFlag]:
        flags = []

        # ── Strategy-core rules ────────────────────────────────────────

        # Rule: MACD Quadrant regime
        if self._enabled_rules.get("macd_quadrant") and regime:
            if regime == REGIME_DEAD:
                flags.append(RulesFlag(
                    rule_name="regime_dead_zone",
                    symbol=symbol,
                    details={
                        "regime": regime,
                        "ce_macd": ce_macd_value,
                        "pe_macd": pe_macd_value,
                        "action": "no_entry",
                    },
                    priority="HIGH",
                ))
            elif regime == REGIME_IV_SPIKE:
                flags.append(RulesFlag(
                    rule_name="regime_iv_spike",
                    symbol=symbol,
                    details={
                        "regime": regime,
                        "ce_macd": ce_macd_value,
                        "pe_macd": pe_macd_value,
                        "action": "evaluate_individually",
                    },
                    priority="NORMAL",
                ))
            elif regime in (REGIME_BULLISH, REGIME_BEARISH):
                flags.append(RulesFlag(
                    rule_name="regime_directional",
                    symbol=symbol,
                    details={
                        "regime": regime,
                        "ce_macd": ce_macd_value,
                        "pe_macd": pe_macd_value,
                        "direction": "CE" if regime == REGIME_BULLISH else "PE",
                    },
                    priority="HIGH",
                ))

        # Rule: IV filter — reject high-IV entries (vol-adjusted return degrades)
        if self._enabled_rules.get("iv_filter") and iv_pct is not None:
            if iv_pct > HARD_MAX_IV_PCT:
                flags.append(RulesFlag(
                    rule_name="iv_reject",
                    symbol=symbol,
                    details={
                        "iv_pct": iv_pct,
                        "threshold": HARD_MAX_IV_PCT,
                        "action": "no_entry",
                    },
                    priority="HIGH",
                ))
            elif iv_pct > MAX_ENTRY_IV_PCT:
                flags.append(RulesFlag(
                    rule_name="iv_caution",
                    symbol=symbol,
                    details={
                        "iv_pct": iv_pct,
                        "threshold": MAX_ENTRY_IV_PCT,
                        "action": "cautious_sizing",
                    },
                    priority="NORMAL",
                ))
            else:
                flags.append(RulesFlag(
                    rule_name="iv_preferred",
                    symbol=symbol,
                    details={
                        "iv_pct": iv_pct,
                        "action": "preferred_sizing",
                    },
                    priority="LOW",
                ))

        # Rule: Spot MA context — classify setup quality for sizing
        if self._enabled_rules.get("spot_ma_context") and spot_setup:
            priority_map = {
                SETUP_BREAKOUT: "HIGH",
                SETUP_PREMIUM: "HIGH",
                SETUP_TREND: "NORMAL",
                SETUP_REVERSAL: "LOW",
            }
            flags.append(RulesFlag(
                rule_name="spot_ma_setup",
                symbol=symbol,
                details={
                    "setup": spot_setup,
                    "action": "premium_sizing" if spot_setup in (SETUP_BREAKOUT, SETUP_PREMIUM) else "normal_sizing",
                },
                priority=priority_map.get(spot_setup, "NORMAL"),
            ))

        # Rule: VIX spike — high-VIX environment limits sizing
        if self._enabled_rules.get("vix_spike") and vix is not None:
            if vix >= CIRCUIT.vix_high_threshold:
                flags.append(RulesFlag(
                    rule_name="vix_high",
                    symbol=symbol,
                    details={
                        "vix": vix,
                        "threshold": CIRCUIT.vix_high_threshold,
                        "action": "itm_only_half_size",
                    },
                    priority="HIGH",
                ))

        # ── Legacy market-structure rules ──────────────────────────────

        # Rule: IV Rank > 80 → premium selling opportunity
        if self._enabled_rules.get("iv_rank_high") and iv_rank:
            rank = iv_rank.get("iv_rank", 0)
            if rank > 80:
                flags.append(RulesFlag(
                    rule_name="iv_rank_high",
                    symbol=symbol,
                    details={"iv_rank": rank, "action": "sell_premium"},
                    priority="HIGH",
                ))

        # Rule: PCR extreme → contrarian signal
        if self._enabled_rules.get("pcr_extreme") and option_chain:
            pcr = option_chain.get("pcr_oi", 1.0)
            if pcr < 0.6:
                flags.append(RulesFlag(
                    rule_name="pcr_extreme_bearish",
                    symbol=symbol,
                    details={"pcr_oi": pcr, "signal": "contrarian_buy"},
                    priority="HIGH",
                ))
            elif pcr > 1.5:
                flags.append(RulesFlag(
                    rule_name="pcr_extreme_bullish",
                    symbol=symbol,
                    details={"pcr_oi": pcr, "signal": "contrarian_sell"},
                    priority="NORMAL",
                ))

        # Rule: Market Profile breakout above VAH or below VAL
        if self._enabled_rules.get("market_profile_breakout") and market_profile and ltp:
            vah = market_profile.get("vah", 0)
            val = market_profile.get("val", 0)
            poc = market_profile.get("poc", 0)
            if vah and ltp > vah * 1.001:
                flags.append(RulesFlag(
                    rule_name="mp_breakout_above_vah",
                    symbol=symbol,
                    details={"ltp": ltp, "vah": vah, "poc": poc},
                    priority="HIGH",
                ))
            elif val and ltp < val * 0.999:
                flags.append(RulesFlag(
                    rule_name="mp_breakdown_below_val",
                    symbol=symbol,
                    details={"ltp": ltp, "val": val, "poc": poc},
                    priority="HIGH",
                ))

        # Rule: POC reclaim after rejection
        if self._enabled_rules.get("poc_reclaim") and market_profile and ltp:
            poc = market_profile.get("poc", 0)
            if poc and abs(ltp - poc) / poc < 0.002:
                flags.append(RulesFlag(
                    rule_name="poc_reclaim",
                    symbol=symbol,
                    details={"ltp": ltp, "poc": poc},
                    priority="NORMAL",
                ))

        # Rule: High OI buildup at ATM strike
        if self._enabled_rules.get("oi_buildup") and option_chain:
            atm_strike = option_chain.get("atm_strike", 0)
            entries = option_chain.get("entries", [])
            for e in entries:
                if e.get("strike") == atm_strike:
                    if e.get("oi", 0) > 1_000_000:
                        flags.append(RulesFlag(
                            rule_name="oi_buildup_atm",
                            symbol=symbol,
                            details={
                                "strike": atm_strike,
                                "option_type": e.get("option_type"),
                                "oi": e.get("oi"),
                            },
                            priority="NORMAL",
                        ))

        if flags:
            logger.info(f"[Rules] {len(flags)} flags for {symbol}: {[f.rule_name for f in flags]}")

        return flags

    def has_blocking_flag(self, flags: List[RulesFlag]) -> bool:
        """Check if any flag blocks entry (dead zone, IV reject)."""
        blocking = {"regime_dead_zone", "iv_reject"}
        return any(f.rule_name in blocking for f in flags)

    def get_sizing_hint(self, flags: List[RulesFlag]) -> str:
        """Derive sizing mode from flags: premium > normal > cautious."""
        names = {f.rule_name for f in flags}
        if "iv_reject" in names or "regime_dead_zone" in names:
            return "no_entry"
        if "vix_high" in names or "iv_caution" in names:
            return "cautious"
        if "iv_preferred" in names:
            setups = [f for f in flags if f.rule_name == "spot_ma_setup"]
            if setups and setups[0].details.get("action") == "premium_sizing":
                return "premium"
        return "normal"

    def set_rule(self, rule_name: str, enabled: bool):
        self._enabled_rules[rule_name] = enabled

    def get_status(self) -> dict:
        return {"enabled_rules": self._enabled_rules}
