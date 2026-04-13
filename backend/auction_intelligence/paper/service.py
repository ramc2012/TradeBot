from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from auction_intelligence.paper.book import PaperPositionBook
from auction_intelligence.paper.journal import JournalWriter
from auction_intelligence.schemas import AnalysisBundle, PaperTradeRecord


class PaperTradingService:
    def __init__(self, journal_root: str):
        self.writer = JournalWriter(journal_root)
        self.book = PaperPositionBook(journal_root)

    def record_analysis(self, bundle: AnalysisBundle) -> list[str]:
        written: list[str] = []
        for execution in bundle.execution_plan:
            if execution.action == "FLAT":
                continue
            matching = next(
                (decision for decision in bundle.agent_decisions if decision.agent_name == execution.agent_name),
                None,
            )
            if matching is None:
                continue
            record = PaperTradeRecord(
                recorded_at=datetime.now(timezone.utc).isoformat(),
                symbol=execution.symbol,
                regime=bundle.regime.label,
                agent_name=execution.agent_name,
                action=execution.action,
                broker_action=execution.broker_action,
                confidence=matching.confidence,
                quantity=int(execution.quantity or matching.quantity or 0),
                entry_price=matching.entry_price,
                stop_price=matching.stop_price,
                target_price=matching.target_price,
                execution_style=execution.style,
                underlying_symbol=execution.underlying_symbol,
                instrument_type=execution.instrument_type,
                expiry=execution.expiry,
                strike=execution.strike,
                option_type=execution.option_type,
                instrument_key=execution.instrument_key,
                trading_symbol=execution.trading_symbol,
                lot_size=execution.lot_size,
                premium=execution.premium,
                spot_price=execution.spot_price,
                moneyness=execution.moneyness,
                expiry_kind=execution.expiry_kind,
                days_to_expiry=execution.days_to_expiry,
                selection_reason=execution.selection_reason,
                notes=[*matching.rationale, *execution.rationale],
            )
            path = self.writer.append(bundle.market_profile.symbol.lower().replace(" ", "_"), asdict(record))
            written.append(str(path))
        return written

    async def sync_positions(self, bundle: AnalysisBundle) -> dict:
        return await self.book.sync_analysis(bundle)
