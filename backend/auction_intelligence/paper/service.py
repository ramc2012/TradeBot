from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from auction_intelligence.paper.journal import JournalWriter
from auction_intelligence.schemas import AnalysisBundle, PaperTradeRecord


class PaperTradingService:
    def __init__(self, journal_root: str):
        self.writer = JournalWriter(Path(journal_root))

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
                confidence=matching.confidence,
                entry_price=matching.entry_price,
                stop_price=matching.stop_price,
                target_price=matching.target_price,
                execution_style=execution.style,
                notes=matching.rationale,
            )
            path = self.writer.append(bundle.market_profile.symbol.lower().replace(" ", "_"), asdict(record))
            written.append(str(path))
        return written
