from __future__ import annotations

from auction_intelligence.agents import PositionalAgent, ScalpAgent, SwingAgent
from auction_intelligence.config import clone_default_config
from auction_intelligence.execution import ExecutionPlanner
from auction_intelligence.market_profile import MarketProfileEngine
from auction_intelligence.meta_controller import MetaController
from auction_intelligence.order_flow import OrderFlowEngine
from auction_intelligence.paper import PaperTradingService
from auction_intelligence.regime import RegimeEngine
from auction_intelligence.risk import RiskGovernor
from auction_intelligence.schemas import (
    AgentContext,
    AnalysisBundle,
    DepthSnapshot,
    MarketBar,
    PortfolioSnapshot,
    QuoteSnapshot,
    SessionContext,
    TradePrint,
)


class AuctionIntelligenceService:
    def __init__(self, config: dict | None = None):
        self.config = config or clone_default_config()
        self.market_profile = MarketProfileEngine(self.config["market_profile"])
        self.order_flow = OrderFlowEngine(self.config["order_flow"])
        self.regime = RegimeEngine(self.config["regime"])
        self.swing_agent = SwingAgent(self.config["agents"]["swing"])
        self.positional_agent = PositionalAgent(self.config["agents"]["positional"])
        self.scalp_agent = ScalpAgent(self.config["agents"]["scalp"])
        self.meta_controller = MetaController(self.config["meta_controller"])
        self.risk = RiskGovernor(self.config["risk"])
        self.execution = ExecutionPlanner()
        self.paper = PaperTradingService(self.config["paper_trading"]["journal_root"])

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
    ) -> AnalysisBundle:
        portfolio = portfolio or PortfolioSnapshot()
        prior_profile = None
        if prior_bars:
            prior_profile = self.market_profile.build_profile(session.symbol, prior_bars)

        current_profile = self.market_profile.build_profile(
            session.symbol,
            bars,
            prior_profile=prior_profile,
        )
        order_flow = self.order_flow.compute(
            quote=quote,
            trades=trades,
            depth=depth,
            tick_size=current_profile.tick_size,
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
        )
        decisions = [
            self.positional_agent.evaluate(context),
            self.swing_agent.evaluate(context),
            self.scalp_agent.evaluate(context),
        ]
        coordinated = self.meta_controller.coordinate(decisions, regime)
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
    ) -> tuple[AnalysisBundle, list[str]]:
        bundle = self.analyze(
            session=session,
            bars=bars,
            quote=quote,
            trades=trades,
            prior_bars=prior_bars,
            depth=depth,
            portfolio=portfolio,
        )
        paths = self.paper.record_analysis(bundle)
        return bundle, paths
