from __future__ import annotations

import asyncio
import functools
from dataclasses import replace

from loguru import logger

from auction_intelligence.agents import PositionalAgent, ScalpAgent, SwingAgent
from auction_intelligence.config import clone_default_config
from auction_intelligence.execution import ExecutionPlanner
from auction_intelligence.market_profile import MarketProfileEngine
from auction_intelligence.mp_ticks import mp_tick_for
from auction_intelligence.meta_controller import MetaController
from auction_intelligence.options import OptionStrategyMapper
from auction_intelligence.order_flow import OrderFlowEngine
from auction_intelligence.paper import PaperTradingService
from auction_intelligence.regime import RegimeEngine
from auction_intelligence.risk import RiskGovernor
from auction_intelligence.schemas import (
    AgentContext,
    AnalysisBundle,
    DepthSnapshot,
    MarketBar,
    NTMVolXSnapshot,
    PortfolioSnapshot,
    QuoteSnapshot,
    SessionContext,
    TradePrint,
)
from auction_intelligence.validation.engine import GateAValidator
from auction_intelligence.validation.schemas import ValidationReport


class AuctionIntelligenceService:
    def __init__(self, config: dict | None = None, *, paper_mode: bool = False):
        self.config = config or clone_default_config()
        self.paper_mode = paper_mode
        self.market_profile = MarketProfileEngine(self.config["market_profile"])
        # Per-symbol bar-TPO engines: a global 0.5 tick over-fragments
        # BANKNIFTY/SENSEX profiles (poor-high/low ~1-2%, tails ~96-98% = no
        # info). Built lazily per resolved value tick and cached.
        self._mp_engines: dict[float, MarketProfileEngine] = {}
        self.order_flow = OrderFlowEngine(self.config["order_flow"])
        self.regime = RegimeEngine(self.config["regime"])
        self.swing_agent = SwingAgent(self.config["agents"]["swing"])
        self.positional_agent = PositionalAgent(self.config["agents"]["positional"])
        self.scalp_agent = ScalpAgent(self.config["agents"]["scalp"])
        self.meta_controller = MetaController(self.config["meta_controller"])
        self.risk = RiskGovernor(
            {
                **self.config["risk"],
                "contract_specs": self.config.get("contract_specs", {}),
                "mvp_scope": self.config.get("mvp_scope", {}),
                "paper_mode": paper_mode,
            }
        )
        self.execution = ExecutionPlanner()
        self.options = OptionStrategyMapper(
            self.config.get("options_mapping", {}),
            self.config.get("contract_specs", {}),
        )
        self.paper = PaperTradingService(self.config["paper_trading"]["journal_root"])
        self.validation = GateAValidator(self.config)

    def _profile_engine_for(self, symbol: str) -> MarketProfileEngine:
        """Bar-TPO engine for one symbol at its per-symbol value tick (cached).

        Falls back to the global engine's tick for unmapped symbols.
        """
        base_tick = float(self.config["market_profile"].get("tick_size", 0.5))
        tick = mp_tick_for(symbol, base_tick)
        engine = self._mp_engines.get(tick)
        if engine is None:
            engine = MarketProfileEngine({**self.config["market_profile"], "tick_size": tick})
            self._mp_engines[tick] = engine
        return engine

    def analyze(
        self,
        session: SessionContext,
        bars: list[MarketBar],
        quote: QuoteSnapshot,
        trades: list[TradePrint],
        *,
        prior_bars: list[MarketBar] | None = None,
        depth: DepthSnapshot | None = None,
        portfolio: PortfolioSnapshot | None = None,
        quote_history: list[QuoteSnapshot] | None = None,
        ntm_volx: NTMVolXSnapshot | None = None,
    ) -> AnalysisBundle:
        portfolio = portfolio or PortfolioSnapshot()
        mp_engine = self._profile_engine_for(session.symbol)
        prior_profile = None
        if prior_bars:
            prior_profile = mp_engine.build_profile(session.symbol, prior_bars)

        current_profile = mp_engine.build_profile(
            session.symbol,
            bars,
            prior_profile=prior_profile,
        )
        order_flow = self.order_flow.compute(
            quote=quote,
            trades=trades,
            depth=depth,
            tick_size=current_profile.tick_size,
            quote_history=quote_history,
        )
        regime = self.regime.classify(current=current_profile, prior=prior_profile, order_flow=order_flow)
        context = AgentContext(
            session=session,
            portfolio=portfolio,
            current_profile=current_profile,
            prior_profile=prior_profile,
            order_flow=order_flow,
            regime=regime,
            config=self.config,
            ntm_volx=ntm_volx,
        )
        decisions = [
            self.positional_agent.evaluate(context),
            self.swing_agent.evaluate(context),
            self.scalp_agent.evaluate(context),
        ]
        coordinated = self.meta_controller.coordinate(decisions, regime)
        coordinated = self._apply_ntm_volx_overlay(coordinated, ntm_volx)
        coordinated = self._apply_sniper_overlay(coordinated, session)
        risk = self.risk.evaluate(session=session, portfolio=portfolio, decisions=coordinated)
        execution_plan = []
        if risk.allowed:
            execution_plan = [
                self.execution.plan(session=session, decision=decision, order_flow=order_flow)
                for decision in coordinated
                if decision.action != "FLAT"
            ]

        return AnalysisBundle(
            config_scope=self.config["mvp_scope"],
            market_profile=current_profile,
            prior_market_profile=prior_profile,
            order_flow=order_flow,
            regime=regime,
            agent_decisions=coordinated,
            risk=risk,
            execution_plan=execution_plan,
            ntm_volx=ntm_volx,
        )

    def analyze_and_record_paper(
        self,
        session: SessionContext,
        bars: list[MarketBar],
        quote: QuoteSnapshot,
        trades: list[TradePrint],
        *,
        prior_bars: list[MarketBar] | None = None,
        depth: DepthSnapshot | None = None,
        portfolio: PortfolioSnapshot | None = None,
        quote_history: list[QuoteSnapshot] | None = None,
    ) -> tuple[AnalysisBundle, list[str]]:
        bundle = self.analyze(
            session=session,
            bars=bars,
            quote=quote,
            trades=trades,
            prior_bars=prior_bars,
            depth=depth,
            portfolio=portfolio,
            quote_history=quote_history,
        )
        paths = self.paper.record_analysis(bundle)
        return bundle, paths

    async def analyze_with_options(
        self,
        session: SessionContext,
        bars: list[MarketBar],
        quote: QuoteSnapshot,
        trades: list[TradePrint],
        *,
        prior_bars: list[MarketBar] | None = None,
        depth: DepthSnapshot | None = None,
        portfolio: PortfolioSnapshot | None = None,
        quote_history: list[QuoteSnapshot] | None = None,
    ) -> AnalysisBundle:
        ntm_volx = await self.options.build_ntm_volx(session=session)
        # CPU-bulkhead: analyze() is a pure synchronous CPU block (no awaits) —
        # market-profile/order-flow/regime/agents/meta/risk/execution loops over
        # the passed-in bars/quote/trades. Offload it to a worker thread so the
        # 200-320s compute does not block the event loop during the live cycle.
        # ntm_volx is already awaited above and passed in, so the thread does no IO.
        bundle = await asyncio.to_thread(
            functools.partial(
                self.analyze,
                session=session,
                bars=bars,
                quote=quote,
                trades=trades,
                prior_bars=prior_bars,
                depth=depth,
                portfolio=portfolio,
                quote_history=quote_history,
                ntm_volx=ntm_volx,
            )
        )
        bundle.execution_plan = await self.options.map_execution_plan(
            session=session,
            decisions=bundle.agent_decisions,
            execution_plan=bundle.execution_plan,
            ntm_volx=bundle.ntm_volx,
        )
        return bundle

    async def analyze_and_record_option_paper(
        self,
        session: SessionContext,
        bars: list[MarketBar],
        quote: QuoteSnapshot,
        trades: list[TradePrint],
        *,
        prior_bars: list[MarketBar] | None = None,
        depth: DepthSnapshot | None = None,
        portfolio: PortfolioSnapshot | None = None,
        quote_history: list[QuoteSnapshot] | None = None,
    ) -> tuple[AnalysisBundle, list[str], dict]:
        bundle = await self.analyze_with_options(
            session=session,
            bars=bars,
            quote=quote,
            trades=trades,
            prior_bars=prior_bars,
            depth=depth,
            portfolio=portfolio,
            quote_history=quote_history,
        )
        paths = self.paper.record_analysis(bundle)
        paper_positions = await self.paper.sync_positions(bundle)
        return bundle, paths, paper_positions

    def validate_gate_a(
        self,
        *,
        session: SessionContext,
        bars: list[MarketBar],
        prior_bars: list[MarketBar] | None = None,
        ) -> ValidationReport:
        return self.validation.validate(
            session=session,
            bars=bars,
            prior_bars=prior_bars,
        )

    def _apply_ntm_volx_overlay(
        self,
        decisions: list,
        ntm_volx: NTMVolXSnapshot | None,
    ) -> list:
        if ntm_volx is None or ntm_volx.directional_bias == "FLAT":
            return decisions

        overlay_config = self.config.get("options_mapping", {}).get("ntm_volx", {})
        aligned_confidence_boost = float(overlay_config.get("aligned_confidence_boost", 0.06))
        conflict_confidence_penalty = float(overlay_config.get("conflict_confidence_penalty", 0.1))
        conflict_flatten_ratio = float(overlay_config.get("conflict_flatten_ratio", 2.25))
        conflict_flatten_confidence_ceiling = float(
            overlay_config.get("conflict_flatten_confidence_ceiling", 0.72)
        )
        min_alignment_pressure = float(overlay_config.get("min_alignment_pressure", 0.12))

        adjusted = []
        for decision in decisions:
            if decision.action == "FLAT":
                adjusted.append(decision)
                continue

            metadata = {
                **decision.metadata,
                "ntm_volx_bias": ntm_volx.directional_bias,
                "ntm_volx_regime": ntm_volx.regime,
                "ntm_volx_vxr": round(float(ntm_volx.vxr), 4),
                "ntm_volx_net_pressure": round(float(ntm_volx.net_pressure), 4),
            }
            rationale = list(decision.rationale)
            is_aligned = decision.action == ntm_volx.directional_bias

            if is_aligned and abs(float(ntm_volx.net_pressure)) >= min_alignment_pressure:
                confidence_boost = min(
                    aligned_confidence_boost,
                    abs(float(ntm_volx.net_pressure)) * 0.12,
                )
                rationale.append(
                    f"NTM VolX confirms {decision.action.lower()} bias with {ntm_volx.dominant_side.lower()} pressure at {ntm_volx.vxr:.2f}x."
                )
                adjusted.append(
                    replace(
                        decision,
                        confidence=round(min(1.0, float(decision.confidence) + confidence_boost), 4),
                        rationale=rationale,
                        metadata={**metadata, "ntm_alignment": "aligned"},
                    )
                )
                continue

            if (
                not is_aligned
                and float(ntm_volx.vxr) >= conflict_flatten_ratio
                and float(decision.confidence) <= conflict_flatten_confidence_ceiling
            ):
                rationale.append(
                    f"NTM VolX blocked this counter-bias setup: {ntm_volx.dominant_side.lower()} pressure is running at {ntm_volx.vxr:.2f}x."
                )
                adjusted.append(
                    replace(
                        decision,
                        action="FLAT",
                        confidence=0.0,
                        entry_price=None,
                        stop_price=None,
                        target_price=None,
                        quantity=0,
                        rationale=rationale,
                        metadata={**metadata, "ntm_alignment": "blocked", "flat_reason": "ntm_volx_conflict"},
                    )
                )
                continue

            if not is_aligned:
                confidence_penalty = min(
                    conflict_confidence_penalty,
                    abs(float(ntm_volx.net_pressure)) * 0.15,
                )
                rationale.append(
                    f"NTM VolX is leaning the other way ({ntm_volx.directional_bias.lower()}) at {ntm_volx.vxr:.2f}x, so confidence is discounted."
                )
                adjusted.append(
                    replace(
                        decision,
                        confidence=round(max(0.0, float(decision.confidence) - confidence_penalty), 4),
                        rationale=rationale,
                        metadata={**metadata, "ntm_alignment": "conflicted"},
                    )
                )
                continue

            adjusted.append(decision)

        return adjusted

    def _apply_sniper_overlay(self, decisions: list, session: SessionContext) -> list:
        """Overlay the Sniper excursion-estimator alpha onto agent decisions.

        The sidecar POSTs a per-underlying directional prediction (direction +
        ATR-magnitude + confidence) which we read from the in-process
        ``sniper_signal_store``. We annotate EVERY decision with the sniper's
        read (so the journal/scorer can learn whether the overlay helps) and
        apply a bounded confidence nudge to actionable, non-FLAT decisions of
        the configured agents — aligned -> small boost, conflicted -> small
        penalty, mirroring the proven NTM VolX overlay. An optional (default
        OFF) hard-flatten vetoes a low-conviction setup that the sniper strongly
        opposes. Fully feature-flagged and wrapped so a sniper hiccup can never
        break the live cycle.
        """
        cfg = self.config.get("sniper_overlay", {}) or {}
        if not cfg.get("enabled", False):
            return decisions
        try:
            from auction_intelligence.sniper_signal import sniper_signal_store

            max_staleness = float(cfg.get("max_staleness_seconds", 2700.0))
            signal = sniper_signal_store.get(
                session.symbol, max_staleness_seconds=max_staleness
            )
            if signal is None:
                return decisions

            apply_to = set(cfg.get("apply_to_agents", ["positional", "swing"]))
            aligned_boost = float(cfg.get("aligned_confidence_boost", 0.08))
            conflict_penalty = float(cfg.get("conflict_confidence_penalty", 0.10))
            min_signal_conf = float(cfg.get("min_signal_confidence", 0.5))
            min_magnitude = float(cfg.get("min_magnitude_atr", 0.25))
            flatten_enabled = bool(cfg.get("conflict_flatten_enabled", False))
            flatten_signal_conf = float(cfg.get("conflict_flatten_signal_confidence", 0.78))
            flatten_decision_ceiling = float(cfg.get("conflict_flatten_decision_ceiling", 0.6))

            direction = str(signal.direction or "FLAT").upper()
            confidence = max(0.0, min(1.0, float(signal.confidence or 0.0)))
            magnitude = float(signal.magnitude_atr or 0.0)
            actionable = (
                direction in ("LONG", "SHORT")
                and confidence >= min_signal_conf
                and magnitude >= min_magnitude
            )

            base_meta_extra = {
                "sniper_direction": direction,
                "sniper_magnitude_atr": round(magnitude, 4),
                "sniper_confidence": round(confidence, 4),
                "sniper_horizon": signal.horizon,
                "sniper_age_sec": round(signal.age_seconds(), 1),
                "sniper_model": signal.model,
            }

            adjusted = []
            for decision in decisions:
                meta = {**decision.metadata, **base_meta_extra}
                if (
                    decision.action == "FLAT"
                    or decision.agent_name not in apply_to
                    or not actionable
                ):
                    tag = "no_signal" if not actionable else "skipped"
                    adjusted.append(
                        replace(decision, metadata={**meta, "sniper_alignment": tag})
                    )
                    continue

                rationale = list(decision.rationale)
                is_aligned = decision.action == direction

                if is_aligned:
                    boost = round(min(aligned_boost, aligned_boost * confidence), 4)
                    rationale.append(
                        f"Sniper alpha confirms {decision.action.lower()} "
                        f"(~{magnitude:.2f} ATR @ {confidence:.0%} conf, {signal.horizon}); confidence boosted."
                    )
                    adjusted.append(
                        replace(
                            decision,
                            confidence=round(min(1.0, float(decision.confidence) + boost), 4),
                            rationale=rationale,
                            metadata={**meta, "sniper_alignment": "aligned"},
                        )
                    )
                    continue

                # Conflicted.
                if (
                    flatten_enabled
                    and confidence >= flatten_signal_conf
                    and float(decision.confidence) <= flatten_decision_ceiling
                ):
                    rationale.append(
                        f"Sniper alpha strongly opposes this {decision.action.lower()} "
                        f"setup (~{magnitude:.2f} ATR {direction.lower()} @ {confidence:.0%}); vetoed."
                    )
                    adjusted.append(
                        replace(
                            decision,
                            action="FLAT",
                            confidence=0.0,
                            entry_price=None,
                            stop_price=None,
                            target_price=None,
                            quantity=0,
                            rationale=rationale,
                            metadata={
                                **meta,
                                "sniper_alignment": "vetoed",
                                "flat_reason": "sniper_conflict",
                            },
                        )
                    )
                    continue

                penalty = round(min(conflict_penalty, conflict_penalty * confidence), 4)
                rationale.append(
                    f"Sniper alpha leans {direction.lower()} (~{magnitude:.2f} ATR @ {confidence:.0%}); "
                    f"confidence discounted on this {decision.action.lower()} setup."
                )
                adjusted.append(
                    replace(
                        decision,
                        confidence=round(max(0.0, float(decision.confidence) - penalty), 4),
                        rationale=rationale,
                        metadata={**meta, "sniper_alignment": "conflicted"},
                    )
                )

            return adjusted
        except Exception as exc:  # never let the sniper overlay break the cycle
            logger.warning(f"sniper overlay skipped: {exc}")
            return decisions
