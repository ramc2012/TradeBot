from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from collections import Counter

from auction_intelligence.paper.book import PaperPositionBook
from auction_intelligence.paper.journal import JournalReader, JournalWriter
from auction_intelligence.schemas import AnalysisBundle, PaperTradeRecord


class PaperTradingService:
    def __init__(self, journal_root: str):
        self.writer = JournalWriter(journal_root)
        self.reader = JournalReader(journal_root)
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

    def journal(self, *, symbol: str | None = None, limit: int = 50) -> dict:
        normalized_symbol = self._normalize_symbol_filter(symbol)
        filtered = [
            record
            for record in self.reader.iter_records()
            if self._record_matches_symbol(record, normalized_symbol)
        ]
        filtered.sort(key=lambda item: str(item.get("recorded_at") or ""), reverse=True)
        records = filtered[: max(1, min(int(limit), 500))]

        action_breakdown = Counter(str(item.get("action") or "UNKNOWN") for item in filtered)
        style_breakdown = Counter(str(item.get("execution_style") or "unknown") for item in filtered)
        agent_breakdown = Counter(str(item.get("agent_name") or "unknown") for item in filtered)
        premiums = [
            float(item["premium"])
            for item in filtered
            if item.get("premium") is not None
        ]
        confidences = [
            float(item["confidence"])
            for item in filtered
            if item.get("confidence") is not None
        ]

        return {
            "symbol_filter": normalized_symbol,
            "count": len(records),
            "total_records": len(filtered),
            "summary": {
                "latest_recorded_at": records[0].get("recorded_at") if records else None,
                "avg_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
                "avg_premium": round(sum(premiums) / len(premiums), 4) if premiums else None,
                "action_breakdown": dict(action_breakdown),
                "style_breakdown": dict(style_breakdown),
                "agent_breakdown": dict(agent_breakdown),
            },
            "records": records,
        }

    async def positions(
        self,
        *,
        symbol: str | None = None,
        status: str = "all",
        limit: int = 50,
    ) -> dict:
        return await self.book.list_positions(
            symbol=symbol,
            status=status,
            limit=max(1, min(int(limit), 200)),
        )

    async def status(self) -> dict:
        positions = await self.positions(status="all", limit=100)
        journal = self.journal(limit=1)
        return {
            "mode": "paper",
            "journal_root": str(self.writer.root),
            "positions_path": str(self.book.path),
            "summary": positions.get("summary") or {},
            "open_positions": positions.get("open_positions") or [],
            "latest_journal_recorded_at": (journal.get("summary") or {}).get("latest_recorded_at"),
            "journal_record_count": journal.get("total_records", 0),
        }

    @staticmethod
    def _normalize_symbol_filter(symbol: str | None) -> str | None:
        raw = str(symbol or "").strip().upper()
        if not raw:
            return None
        return raw.replace(" FUT", "")

    @classmethod
    def _record_matches_symbol(cls, record: dict, normalized_symbol: str | None) -> bool:
        if not normalized_symbol:
            return True
        candidates = [
            record.get("underlying_symbol"),
            record.get("symbol"),
            record.get("trading_symbol"),
        ]
        for candidate in candidates:
            value = str(candidate or "").upper()
            if value.replace(" FUT", "") == normalized_symbol or value.startswith(normalized_symbol):
                return True
        return False
