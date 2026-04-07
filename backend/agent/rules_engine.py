"""Tier 1 Rules Engine — fast condition checks (<200ms)."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from loguru import logger


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
    ) -> List[RulesFlag]:
        flags = []

        # Rule 1: IV Rank > 80 → premium selling opportunity
        if self._enabled_rules.get("iv_rank_high") and iv_rank:
            rank = iv_rank.get("iv_rank", 0)
            if rank > 80:
                flags.append(RulesFlag(
                    rule_name="iv_rank_high",
                    symbol=symbol,
                    details={"iv_rank": rank, "action": "sell_premium"},
                    priority="HIGH",
                ))

        # Rule 2: PCR extreme → contrarian signal
        if self._enabled_rules.get("pcr_extreme") and option_chain:
            pcr = option_chain.get("pcr_oi", 1.0)
            if pcr < 0.6:   # Extreme bearish: contrarian BUY
                flags.append(RulesFlag(
                    rule_name="pcr_extreme_bearish",
                    symbol=symbol,
                    details={"pcr_oi": pcr, "signal": "contrarian_buy"},
                    priority="HIGH",
                ))
            elif pcr > 1.5:  # Extreme bullish: contrarian SELL
                flags.append(RulesFlag(
                    rule_name="pcr_extreme_bullish",
                    symbol=symbol,
                    details={"pcr_oi": pcr, "signal": "contrarian_sell"},
                    priority="NORMAL",
                ))

        # Rule 3: Market Profile breakout above VAH or below VAL
        if self._enabled_rules.get("market_profile_breakout") and market_profile and ltp:
            vah = market_profile.get("vah", 0)
            val = market_profile.get("val", 0)
            poc = market_profile.get("poc", 0)
            if vah and ltp > vah * 1.001:   # 0.1% above VAH
                flags.append(RulesFlag(
                    rule_name="mp_breakout_above_vah",
                    symbol=symbol,
                    details={"ltp": ltp, "vah": vah, "poc": poc},
                    priority="HIGH",
                ))
            elif val and ltp < val * 0.999:  # 0.1% below VAL
                flags.append(RulesFlag(
                    rule_name="mp_breakdown_below_val",
                    symbol=symbol,
                    details={"ltp": ltp, "val": val, "poc": poc},
                    priority="HIGH",
                ))

        # Rule 4: POC reclaim after rejection
        if self._enabled_rules.get("poc_reclaim") and market_profile and ltp:
            poc = market_profile.get("poc", 0)
            if poc and abs(ltp - poc) / poc < 0.002:  # within 0.2% of POC
                flags.append(RulesFlag(
                    rule_name="poc_reclaim",
                    symbol=symbol,
                    details={"ltp": ltp, "poc": poc},
                    priority="NORMAL",
                ))

        # Rule 5: High OI buildup at ATM strike
        if self._enabled_rules.get("oi_buildup") and option_chain:
            atm_strike = option_chain.get("atm_strike", 0)
            entries = option_chain.get("entries", [])
            for e in entries:
                if e.get("strike") == atm_strike:
                    if e.get("oi", 0) > 1_000_000:  # large OI buildup
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

    def set_rule(self, rule_name: str, enabled: bool):
        self._enabled_rules[rule_name] = enabled

    def get_status(self) -> dict:
        return {"enabled_rules": self._enabled_rules}
