from __future__ import annotations

from datetime import datetime, timezone
from statistics import fmean
from typing import Any

from agentic_rag.schemas import (
    ContextGateRequest,
    ContextGateResult,
    RAGDocument,
    RAGSearchHit,
    RAGSearchRequest,
    TradeCaseRecord,
)
from agentic_rag.sources import collect_runtime_trade_cases
from agentic_rag.store import RAGFileStore
from agentic_rag.text import (
    cosine,
    lexical_score,
    normalized_upper,
    recency_score,
    sparse_vector,
    tokenize,
)


class AgenticRAGService:
    def __init__(self, store: RAGFileStore | None = None):
        self.store = store or RAGFileStore()

    def health(self) -> dict[str, Any]:
        stats = self.store.stats()
        runtime_cases = collect_runtime_trade_cases(limit=1_000)
        stats["runtime_cases"] = len(runtime_cases)
        stats["collections"] = ["playbooks", "policies", "context", "trade_cases", "audit"]
        stats["retrieval_mode"] = "hybrid_lexical_hash_vector_recency"
        return stats

    def add_document(self, document: RAGDocument) -> RAGDocument:
        self.store.append_documents([document])
        return document

    def add_trade_case(self, trade_case: TradeCaseRecord) -> TradeCaseRecord:
        self.store.append_trade_cases([trade_case])
        return trade_case

    def search(self, request: RAGSearchRequest) -> list[RAGSearchHit]:
        query_tokens = tokenize(request.query)
        query_vector = sparse_vector(query_tokens)
        docs = self._candidate_documents(include_runtime_cases=request.include_runtime_cases)
        scored: list[RAGSearchHit] = []
        for doc in docs:
            if not self._matches_filters(doc, request.filters):
                continue
            doc_tokens = tokenize(f"{doc.title} {doc.text} {doc.metadata}")
            if not doc_tokens:
                continue
            lex = lexical_score(query_tokens, doc_tokens)
            vec = cosine(query_vector, sparse_vector(doc_tokens))
            meta = self._metadata_boost(doc, request)
            recency = self._document_recency(doc)
            score = (0.48 * lex) + (0.34 * vec) + (0.12 * meta) + (request.recency_bias * recency)
            if score <= 0 and query_tokens:
                continue
            scored.append(
                RAGSearchHit(
                    id=doc.id,
                    collection=doc.collection,
                    title=doc.title,
                    text=doc.text[:1_200],
                    source=doc.source,
                    metadata=doc.metadata,
                    score=round(score, 6),
                    score_parts={
                        "lexical": round(lex, 6),
                        "vector": round(vec, 6),
                        "metadata": round(meta, 6),
                        "recency": round(recency, 6),
                    },
                )
            )
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[: request.top_k]

    def context_gate(self, request: ContextGateRequest) -> ContextGateResult:
        query = request.query or self._build_context_query(request)
        base_filters = {
            "underlying": request.underlying,
        }
        case_hits = self.search(
            RAGSearchRequest(
                query=query,
                top_k=request.top_k_cases,
                filters={**base_filters, "collection": "trade_cases"},
                include_runtime_cases=True,
                recency_bias=0.22,
            )
        )
        doc_hits = self.search(
            RAGSearchRequest(
                query=query,
                top_k=request.top_k_docs,
                filters={},
                include_runtime_cases=False,
                recency_bias=0.08,
            )
        )
        retrievals = case_hits + doc_hits
        case_stats = self._case_stats(case_hits)
        decision, reason_codes = self._context_decision(request, case_stats, doc_hits)
        confidence = self._decision_confidence(case_stats, retrievals)
        summary = self._summary(decision, case_stats, reason_codes)
        audit_bundle = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "strategy_key": request.strategy_key,
            "underlying": normalized_upper(request.underlying),
            "symbol": request.symbol,
            "signal_direction": request.signal_direction,
            "setup_name": request.setup_name,
            "regime": request.regime,
            "numeric_context": request.numeric_context,
            "hard_risk_passed": request.hard_risk_passed,
            "query": query,
            "decision": decision,
            "reason_codes": reason_codes,
            "retrieval_ids": [hit.id for hit in retrievals],
            "case_stats": case_stats,
        }
        self.store.append_audit_bundle(audit_bundle)
        return ContextGateResult(
            decision=decision,
            confidence=confidence,
            summary=summary,
            reason_codes=reason_codes,
            case_stats=case_stats,
            retrievals=retrievals,
            audit_bundle=audit_bundle,
        )

    def _candidate_documents(self, *, include_runtime_cases: bool) -> list[RAGDocument]:
        documents = self.store.load_documents()
        documents.extend(case.to_document() for case in self.store.load_trade_cases())
        if include_runtime_cases:
            documents.extend(case.to_document() for case in collect_runtime_trade_cases())
        return documents

    def _matches_filters(self, doc: RAGDocument, filters: dict[str, Any]) -> bool:
        if not filters:
            return True
        for key, expected in filters.items():
            if expected is None or expected == "":
                continue
            actual = doc.collection if key == "collection" else doc.metadata.get(key)
            if key in {"underlying", "symbol"}:
                expected_symbol = normalized_upper(expected)
                actual_symbol = normalized_upper(actual or doc.metadata.get("symbol") or doc.metadata.get("underlying"))
                if expected_symbol and expected_symbol not in actual_symbol and actual_symbol not in expected_symbol:
                    return False
                continue
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif str(actual or "").lower() != str(expected).lower():
                return False
        return True

    def _metadata_boost(self, doc: RAGDocument, request: RAGSearchRequest) -> float:
        boost = 0.0
        filters = request.filters or {}
        if filters.get("underlying") and normalized_upper(filters["underlying"]) == normalized_upper(doc.metadata.get("underlying")):
            boost += 0.7
        if filters.get("strategy_key") and str(filters["strategy_key"]).lower() == str(doc.metadata.get("strategy_key") or "").lower():
            boost += 0.5
        if doc.collection in {"policies", "playbooks"}:
            boost += 0.25
        return min(boost, 1.0)

    def _document_recency(self, doc: RAGDocument) -> float:
        metadata = doc.metadata
        if doc.collection in {"playbooks", "policies"}:
            return 0.05
        return recency_score(
            metadata.get("entry_time")
            or metadata.get("exit_time")
            or metadata.get("date")
            or doc.updated_at,
            half_life_days=55.0,
        )

    def _build_context_query(self, request: ContextGateRequest) -> str:
        parts = [
            request.strategy_key,
            request.underlying,
            request.symbol or "",
            request.signal_direction or "",
            request.setup_name or "",
            request.regime or "",
            " ".join(request.event_tags),
            " ".join(f"{key} {value}" for key, value in request.numeric_context.items() if value is not None),
        ]
        return " ".join(part for part in parts if part)

    def _case_stats(self, hits: list[RAGSearchHit]) -> dict[str, Any]:
        pnls: list[float] = []
        wins = 0
        losses = 0
        neutral = 0
        for hit in hits:
            metadata = hit.metadata
            pnl = metadata.get("pnl")
            try:
                if pnl is not None:
                    pnl_float = float(pnl)
                    pnls.append(pnl_float)
                    if pnl_float > 0:
                        wins += 1
                    elif pnl_float < 0:
                        losses += 1
                    else:
                        neutral += 1
                    continue
            except (TypeError, ValueError):
                pass
            result = str(metadata.get("result") or "").lower()
            if result == "win":
                wins += 1
            elif result == "loss":
                losses += 1
            else:
                neutral += 1
        total = len(hits)
        resolved = wins + losses
        return {
            "matched_cases": total,
            "resolved_cases": resolved,
            "wins": wins,
            "losses": losses,
            "neutral_or_open": neutral,
            "win_rate": round(wins / resolved, 4) if resolved else None,
            "expectancy": round(fmean(pnls), 2) if pnls else None,
            "best_pnl": round(max(pnls), 2) if pnls else None,
            "worst_pnl": round(min(pnls), 2) if pnls else None,
        }

    def _context_decision(
        self,
        request: ContextGateRequest,
        case_stats: dict[str, Any],
        doc_hits: list[RAGSearchHit],
    ) -> tuple[str, list[str]]:
        """RAG is an evidence accumulator, not a pre-trade veto.

        The risk governor already owns the hard pre-trade veto. RAG's job is
        to attach case-base context (similar trades, expectancy, policies)
        so the system can *learn* — and only veto when there is strong
        evidence the setup loses repeatedly. Until that evidence exists,
        every trade goes through and gets recorded for future retrieval.

        Thresholds:
          * resolved_cases ≥ 8  before negative-expectancy alone can block
          * resolved_cases ≥ 10 before low-win-rate alone can block
          * Both signals together require resolved_cases ≥ 6 to block
        Below those thresholds, reasons are surfaced as `warn` so they show
        in the audit trail but never gate the trade.
        """
        reasons: list[str] = []
        expectancy = case_stats.get("expectancy")
        resolved_cases = int(case_stats.get("resolved_cases") or 0)
        win_rate = case_stats.get("win_rate")

        if not request.hard_risk_passed:
            return "block", ["hard_risk_failed"]

        negative_expectancy = (
            resolved_cases >= 1 and expectancy is not None and expectancy < 0
        )
        low_win_rate = (
            resolved_cases >= 1 and win_rate is not None and win_rate < 0.35
        )
        if negative_expectancy:
            reasons.append("negative_retrieval_conditional_expectancy")
        if low_win_rate:
            reasons.append("low_similar_case_win_rate")

        severe_policy = [
            hit for hit in doc_hits
            if hit.collection == "policies" and str(hit.metadata.get("severity") or "").lower() == "critical"
        ]
        if severe_policy:
            reasons.append("policy_context_attached")

        # Only block when there is *enough* evidence the setup is broken.
        both_signals_block = (
            negative_expectancy and low_win_rate and resolved_cases >= 6
        )
        negative_only_block = negative_expectancy and resolved_cases >= 8
        low_winrate_only_block = low_win_rate and resolved_cases >= 10
        if both_signals_block or negative_only_block or low_winrate_only_block:
            return "block", reasons

        if reasons:
            return "warn", reasons
        if resolved_cases == 0:
            return "warn", ["insufficient_case_memory"]
        return "allow", ["case_memory_supportive"]

    def _decision_confidence(self, case_stats: dict[str, Any], retrievals: list[RAGSearchHit]) -> float:
        matched = min(int(case_stats.get("matched_cases") or 0), 8)
        retrieval_quality = sum(hit.score for hit in retrievals[:6]) / max(min(len(retrievals), 6), 1)
        confidence = 0.42 + (matched * 0.045) + min(retrieval_quality, 0.35)
        return round(min(confidence, 0.92), 4)

    def _summary(self, decision: str, case_stats: dict[str, Any], reason_codes: list[str]) -> str:
        expectancy = case_stats.get("expectancy")
        win_rate = case_stats.get("win_rate")
        pieces = [f"RAG context gate: {decision.upper()}"]
        if case_stats.get("matched_cases"):
            pieces.append(
                f"{case_stats['matched_cases']} similar cases"
                + (f", win rate {win_rate:.0%}" if isinstance(win_rate, float) else "")
                + (f", expectancy {expectancy}" if expectancy is not None else "")
            )
        else:
            pieces.append("no similar closed cases yet")
        if reason_codes:
            pieces.append("reasons: " + ", ".join(reason_codes))
        return "; ".join(pieces)


rag_service = AgenticRAGService()
