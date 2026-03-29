"""CURIE — Two-tier AI trading agent powered by Claude."""
from __future__ import annotations
import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, List, Optional

import anthropic
from loguru import logger

from agent.rules_engine import RulesEngine, RulesFlag
from agent.tools import AGENT_TOOLS, execute_tool
from core.config import settings
from db.redis_client import get_redis


SYSTEM_PROMPT = """You are CURIE, an expert NSE F&O algorithmic trader with deep expertise in:
- Market Profile theory (TPO, POC, VAH, VAL, Initial Balance, single prints)
- Options flow analysis (PCR, OI analysis, IV rank, gamma exposure)
- Macro context (India VIX, FII/DII flows, sector rotation)
- Volatility strategies (straddles, strangles, spreads, iron condor)

When analyzing opportunities:
1. Always check the broader market context (Nifty/BankNifty trend, VIX level)
2. Evaluate options mispricing through IV rank and PCR
3. Use Market Profile to identify key support/resistance levels
4. Size positions appropriately with defined risk
5. Consider theta decay and gamma risk for options strategies

Always return structured TradeProposals with clear rationale, confidence level, and risk parameters.
Never trade without defined SL. Prefer high probability, positive theta strategies when IV is high."""


@dataclass
class TradeProposal:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str = ""
    strategy: str = ""
    entry: float = 0.0
    sl: float = 0.0
    target: float = 0.0
    qty: int = 0
    rationale: str = ""
    confidence: str = "MED"   # HIGH / MED / LOW
    holding_period: str = "intraday"
    created_at: datetime = field(default_factory=datetime.utcnow)
    status: str = "PENDING"


class TradingAgent:
    """
    Two-tier trading agent:
    Tier 1: Fast rules engine → flags opportunities
    Tier 2: Claude LLM → deep analysis → TradeProposal
    """

    SCAN_SYMBOLS = ["NSE:NIFTY50-INDEX", "NSE:BANKNIFTY-INDEX"]
    SCAN_EXPIRIES = []  # populated at runtime

    def __init__(self, mode: str = "paper"):
        self.mode = mode  # paper | live
        self.rules_engine = RulesEngine()
        self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._portfolio = None
        self._proposals: List[TradeProposal] = []
        self._agent_logs: List[dict] = []
        self._scan_task: Optional[asyncio.Task] = None

    def set_portfolio(self, portfolio):
        self._portfolio = portfolio

    async def start_scheduled_scan(self, interval_minutes: int = 15):
        """Start periodic market scan during market hours."""
        async def _loop():
            while True:
                try:
                    now = datetime.now()
                    # NSE market hours: 9:15 AM to 3:30 PM IST
                    if 9 <= now.hour < 16:
                        await self.run_scan()
                except Exception as e:
                    logger.error(f"[Agent] Scan error: {e}")
                await asyncio.sleep(interval_minutes * 60)

        self._scan_task = asyncio.create_task(_loop())
        logger.info(f"[Agent] Scheduled scan every {interval_minutes} minutes")

    async def stop_scan(self):
        if self._scan_task:
            self._scan_task.cancel()

    async def run_scan(self, symbols: Optional[List[str]] = None) -> List[TradeProposal]:
        """Full two-tier scan: rules → LLM analysis."""
        symbols = symbols or self.SCAN_SYMBOLS
        all_proposals = []

        for symbol in symbols:
            try:
                proposals = await self._scan_symbol(symbol)
                all_proposals.extend(proposals)
            except Exception as e:
                logger.error(f"[Agent] Scan failed for {symbol}: {e}")

        return all_proposals

    async def _scan_symbol(self, symbol: str) -> List[TradeProposal]:
        """Tier 1 + Tier 2 analysis for one symbol."""
        from market_data import option_chain_service, market_profile_builder
        from analytics.sector import sector_tracker

        # Gather context
        oc = await option_chain_service.get_cached(symbol, self._next_expiry())
        mp = await market_profile_builder.get_cached_profile(symbol, "daily")
        iv_rank = await sector_tracker.get_iv_rank(symbol)
        ltp = 0.0  # Would get from data_router

        # Tier 1: Rules engine
        flags = self.rules_engine.evaluate(
            symbol=symbol,
            option_chain=oc,
            market_profile=mp,
            iv_rank=iv_rank,
            ltp=ltp,
        )

        if not flags:
            return []

        logger.info(f"[Agent] Tier 1: {len(flags)} flags for {symbol}")

        # Tier 2: LLM analysis for HIGH priority flags
        proposals = []
        high_flags = [f for f in flags if f.priority == "HIGH"]
        if high_flags:
            proposal = await self._llm_analyze(symbol, high_flags, oc, mp, iv_rank, ltp)
            if proposal:
                proposals.append(proposal)
                await self._store_proposal(proposal)

        return proposals

    async def _llm_analyze(
        self,
        symbol: str,
        flags: List[RulesFlag],
        option_chain: Optional[dict],
        market_profile: Optional[dict],
        iv_rank: Optional[dict],
        ltp: float,
    ) -> Optional[TradeProposal]:
        """Tier 2: Ask Claude to analyze and generate a trade proposal."""

        context = {
            "symbol": symbol,
            "ltp": ltp,
            "flags": [{"rule": f.rule_name, "details": f.details} for f in flags],
            "option_chain_summary": {
                "pcr_oi": option_chain.get("pcr_oi") if option_chain else None,
                "atm_strike": option_chain.get("atm_strike") if option_chain else None,
                "atm_iv": option_chain.get("atm_iv") if option_chain else None,
                "max_pain": option_chain.get("max_pain") if option_chain else None,
            } if option_chain else {},
            "market_profile": {
                "poc": market_profile.get("poc") if market_profile else None,
                "vah": market_profile.get("vah") if market_profile else None,
                "val": market_profile.get("val") if market_profile else None,
                "ib_high": market_profile.get("ib_high") if market_profile else None,
                "ib_low": market_profile.get("ib_low") if market_profile else None,
            } if market_profile else {},
            "iv_rank": iv_rank or {},
            "timestamp": datetime.utcnow().isoformat(),
        }

        user_message = f"""Analyze this trading opportunity and generate a specific trade proposal.

Market Context:
{json.dumps(context, indent=2)}

Use the available tools to gather additional data if needed, then provide:
1. A specific trade strategy with entry, SL, and target
2. Clear rationale based on market structure
3. Confidence level (HIGH/MED/LOW)
4. Expected holding period

Return a TradeProposal with these exact fields:
- symbol, strategy, entry, sl, target, qty, rationale, confidence, holding_period"""

        messages = [{"role": "user", "content": user_message}]
        reasoning = ""
        proposal = None

        try:
            # Agentic loop
            while True:
                response = await self._client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=2048,
                    system=SYSTEM_PROMPT,
                    tools=AGENT_TOOLS,
                    messages=messages,
                )

                reasoning += "\n".join(
                    block.text for block in response.content if hasattr(block, "text")
                )

                if response.stop_reason == "end_turn":
                    # Parse proposal from text response
                    proposal = self._parse_proposal_from_text(
                        reasoning, symbol, context
                    )
                    break

                if response.stop_reason == "tool_use":
                    # Execute tool calls
                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            result = await execute_tool(
                                block.name, block.input, self._portfolio
                            )
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result),
                            })

                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": tool_results})
                else:
                    break

        except Exception as e:
            logger.error(f"[Agent] LLM analysis failed: {e}")
            return None

        # Log reasoning
        self._agent_logs.append({
            "tier": 2,
            "symbol": symbol,
            "input": context,
            "reasoning": reasoning,
            "proposal": proposal.__dict__ if proposal else None,
            "timestamp": datetime.utcnow().isoformat(),
        })

        return proposal

    def _parse_proposal_from_text(
        self, text: str, symbol: str, context: dict
    ) -> Optional[TradeProposal]:
        """Extract structured proposal from Claude's response."""
        try:
            import re
            # Try to find JSON block in response
            json_match = re.search(r'\{[^{}]*"entry"[^{}]*\}', text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return TradeProposal(
                    symbol=data.get("symbol", symbol),
                    strategy=data.get("strategy", ""),
                    entry=float(data.get("entry", 0)),
                    sl=float(data.get("sl", 0)),
                    target=float(data.get("target", 0)),
                    qty=int(data.get("qty", 1)),
                    rationale=data.get("rationale", text[:500]),
                    confidence=data.get("confidence", "MED"),
                    holding_period=data.get("holding_period", "intraday"),
                )
        except Exception:
            pass

        # Fallback: create LOW confidence proposal from context
        return TradeProposal(
            symbol=symbol,
            strategy="ANALYSIS",
            rationale=text[:1000],
            confidence="LOW",
        )

    async def _store_proposal(self, proposal: TradeProposal):
        """Store proposal in Redis with 5-minute TTL and in-memory list."""
        self._proposals.append(proposal)
        redis = await get_redis()
        await redis.set(
            f"proposal:{proposal.id}",
            json.dumps({
                "id": proposal.id,
                "symbol": proposal.symbol,
                "strategy": proposal.strategy,
                "entry": proposal.entry,
                "sl": proposal.sl,
                "target": proposal.target,
                "qty": proposal.qty,
                "rationale": proposal.rationale,
                "confidence": proposal.confidence,
                "holding_period": proposal.holding_period,
                "status": proposal.status,
                "created_at": proposal.created_at.isoformat(),
            }),
            ex=300,  # 5 minute TTL
        )

        # Publish to WebSocket channel
        await redis.publish("proposals", json.dumps({
            "event": "new_proposal",
            "proposal_id": proposal.id,
            "symbol": proposal.symbol,
            "confidence": proposal.confidence,
        }))

        # Auto-execute in paper mode for HIGH confidence
        if self.mode == "paper" and proposal.confidence == "HIGH":
            logger.info(f"[Agent] Auto-executing HIGH confidence proposal: {proposal.id[:8]}")
            # Trigger execution via portfolio

    async def approve_proposal(self, proposal_id: str) -> bool:
        proposal = self._find_proposal(proposal_id)
        if not proposal or proposal.status != "PENDING":
            return False
        proposal.status = "APPROVED"
        redis = await get_redis()
        await redis.delete(f"proposal:{proposal_id}")
        return True

    async def reject_proposal(self, proposal_id: str) -> bool:
        proposal = self._find_proposal(proposal_id)
        if not proposal:
            return False
        proposal.status = "REJECTED"
        redis = await get_redis()
        await redis.delete(f"proposal:{proposal_id}")
        return True

    def get_pending_proposals(self) -> List[dict]:
        return [
            p.__dict__ | {"created_at": p.created_at.isoformat()}
            for p in self._proposals
            if p.status == "PENDING"
        ]

    def get_agent_logs(self, limit: int = 50) -> List[dict]:
        return self._agent_logs[-limit:]

    def _find_proposal(self, proposal_id: str) -> Optional[TradeProposal]:
        for p in self._proposals:
            if p.id == proposal_id:
                return p
        return None

    @staticmethod
    def _next_expiry() -> str:
        """Get next Thursday (NSE weekly expiry)."""
        from datetime import date
        today = date.today()
        days_until_thursday = (3 - today.weekday()) % 7
        if days_until_thursday == 0:
            days_until_thursday = 7
        next_thu = today + timedelta(days=days_until_thursday)
        return next_thu.strftime("%Y-%m-%d")

    async def chat(self, message: str) -> str:
        """Direct chat interface for CURIE."""
        try:
            response = await self._client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=AGENT_TOOLS,
                messages=[{"role": "user", "content": message}],
            )
            return "\n".join(
                block.text for block in response.content if hasattr(block, "text")
            )
        except Exception as e:
            logger.error(f"[Agent] Chat error: {e}")
            return f"Error: {str(e)}"


# ── Singleton ────────────────────────────────────────────────────────────────
trading_agent = TradingAgent(mode="paper")
