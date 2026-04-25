from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentic_rag.schemas import ContextGateRequest, RAGDocument, RAGSearchRequest, TradeCaseRecord
from agentic_rag.service import AgenticRAGService
from agentic_rag.store import RAGFileStore


def _service(tmp_path: Path) -> AgenticRAGService:
    return AgenticRAGService(RAGFileStore(tmp_path / "rag"))


def test_rag_search_uses_metadata_filters_and_seed_playbooks(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.add_document(
        RAGDocument(
            collection="playbooks",
            title="NIFTY failed auction playbook",
            text="NIFTY failed auction below value with seller failure supports CE only after VWAP reclaim.",
            source="test",
            metadata={"strategy_key": "auction_intelligence", "underlying": "NIFTY", "tags": ["failed_auction", "CE"]},
        )
    )

    hits = service.search(
        RAGSearchRequest(
            query="NIFTY failed auction seller failure CE VWAP",
            filters={"underlying": "NIFTY", "collection": "playbooks"},
            include_runtime_cases=False,
        )
    )

    assert hits
    assert hits[0].title == "NIFTY failed auction playbook"
    assert hits[0].score > 0


def test_context_gate_blocks_when_hard_risk_fails(tmp_path: Path) -> None:
    service = _service(tmp_path)

    result = service.context_gate(
        ContextGateRequest(
            strategy_key="auction_intelligence",
            underlying="NIFTY",
            signal_direction="CE",
            setup_name="TREND_UP",
            hard_risk_passed=False,
        )
    )

    assert result.decision == "block"
    assert "hard_risk_failed" in result.reason_codes
    assert result.audit_bundle["hard_risk_passed"] is False


def test_context_gate_warns_on_negative_similar_case_expectancy(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for index, pnl in enumerate([-120.0, -60.0, -30.0, 40.0]):
        service.add_trade_case(
            TradeCaseRecord(
                id=f"case-{index}",
                strategy_key="auction_intelligence",
                underlying="NIFTY",
                setup_name="FAILED_AUCTION",
                regime="FAILED_AUCTION",
                direction="CE",
                pnl=pnl,
                result="win" if pnl > 0 else "loss",
                tags=["failed_auction", "seller_failure", "CE"],
                lesson="Similar failed auction case.",
            )
        )

    result = service.context_gate(
        ContextGateRequest(
            strategy_key="auction_intelligence",
            underlying="NIFTY",
            signal_direction="CE",
            setup_name="FAILED_AUCTION",
            regime="FAILED_AUCTION",
            event_tags=["seller_failure"],
        )
    )

    assert result.case_stats["resolved_cases"] >= 3
    assert result.case_stats["expectancy"] < 0
    assert result.decision in {"warn", "block"}
    assert "negative_retrieval_conditional_expectancy" in result.reason_codes
