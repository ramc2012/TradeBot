from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from agentic_rag.schemas import RAGDocument, TradeCaseRecord


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = BACKEND_ROOT / "runtime" / "rag"


DEFAULT_DOCUMENTS: list[RAGDocument] = [
    RAGDocument(
        id="policy-hard-rules-code-enforced",
        collection="policies",
        title="Hard trading rules are code-enforced, not RAG-enforced",
        source="nomad-curie-rag-seed",
        metadata={
            "strategy_key": "all",
            "scope": "risk",
            "severity": "critical",
            "tags": ["risk", "audit", "sebi", "deterministic"],
        },
        text=(
            "RAG may retrieve policy text for explanation and audit context, but max premium, "
            "position limits, no-trade windows, kill switches, and broker throttles must be "
            "enforced by deterministic strategy code before an order can be placed."
        ),
    ),
    RAGDocument(
        id="playbook-routing-numeric-vs-context",
        collection="playbooks",
        title="Routing rule for strategy agents",
        source="nomad-curie-rag-seed",
        metadata={
            "strategy_key": "all",
            "scope": "agent-routing",
            "tags": ["sql", "timeseries", "rag", "decision"],
        },
        text=(
            "Numeric questions use SQL, TimescaleDB, pandas, and feature tools. Document, policy, "
            "playbook, and trade-memory questions use RAG. Trade decisions use both: numeric signal "
            "must be positive, retrieved context must not block, and hard risk checks must pass."
        ),
    ),
    RAGDocument(
        id="playbook-auction-iq-context-gate",
        collection="playbooks",
        title="Auction IQ context gate",
        source="nomad-curie-rag-seed",
        metadata={
            "strategy_key": "auction_intelligence",
            "scope": "market-profile",
            "tags": ["market-profile", "orderflow", "case-memory"],
        },
        text=(
            "For Auction IQ, retrieval is advisory. Use prior cases with similar day type, value "
            "relation, failed-auction state, CVD proxy, and options-flow pressure. If similar cases "
            "show negative expectancy or repeated fakeouts, warn or block the paper decision; never "
            "create a new trade from text alone."
        ),
    ),
    RAGDocument(
        id="playbook-directional-options-context-gate",
        collection="playbooks",
        title="Directional CE/PE context gate",
        source="nomad-curie-rag-seed",
        metadata={
            "strategy_key": "directional_long_options",
            "scope": "options",
            "tags": ["ce", "pe", "iv", "contract-selection"],
        },
        text=(
            "For directional long options, retrieve similar cases by regime, IV state, Greeks Sync, "
            "selected expiry type, strike moneyness, and expected move horizon. RAG can warn about "
            "historical IV crush or weak follow-through, but contract pricing and Greeks remain "
            "structured calculations."
        ),
    ),
    RAGDocument(
        id="playbook-options-whale-flow-proxy",
        collection="playbooks",
        title="Options-flow whale detection proxy",
        source="nomad-curie-rag-seed",
        metadata={
            "strategy_key": "auction_intelligence",
            "scope": "options-flow",
            "tags": ["whale", "unusual-flow", "volume-oi", "ntm-volx"],
        },
        text=(
            "Until raw sweep and NBBO-side data is available, options-flow whale detection should be "
            "a proxy: unusually high premium notional, volume relative to open-interest change, "
            "near-ATM pressure, same-side clustering, and confirmation from NTM VolX. Treat it as "
            "context, not proof of informed flow."
        ),
    ),
    RAGDocument(
        id="audit-context-bundle-requirement",
        collection="policies",
        title="Decision audit bundle requirement",
        source="nomad-curie-rag-seed",
        metadata={
            "strategy_key": "all",
            "scope": "audit",
            "severity": "high",
            "tags": ["audit", "algo-id", "traceability"],
        },
        text=(
            "Every automated or paper decision should store an audit bundle: numeric signal snapshot, "
            "hard-risk result, retrieved case IDs, retrieved policy/playbook IDs, context-gate decision, "
            "and final action. Retrieval traces explain what evidence was available, but decision logs "
            "explain how the system acted."
        ),
    ),
    RAGDocument(
        id="playbook-sector-interaction-var-granger",
        collection="playbooks",
        title="Sector interaction VAR and Granger model",
        source="nomad-curie-rag-seed",
        metadata={
            "strategy_key": "sector_interaction",
            "scope": "model",
            "tags": ["VAR", "Granger", "sectors", "lead-lag"],
        },
        text=(
            "Use aligned US or India sector returns to fit VAR models, select lag by AIC, "
            "convert significant pairwise Granger p-values into directed edge weights, "
            "and rank sectors by outgoing minus incoming influence. Correlation heatmaps "
            "show contemporaneous comovement; directed edges show lead-lag structure."
        ),
    ),
    RAGDocument(
        id="playbook-sector-alternative-data-pipeline",
        collection="playbooks",
        title="Sector alternative-data acquisition pipeline",
        source="nomad-curie-rag-seed",
        metadata={
            "strategy_key": "sector_interaction",
            "scope": "alternative-data",
            "tags": ["sources", "pipeline", "compliance", "signals"],
        },
        text=(
            "Collect permitted alternative data from search trends, transactions, hiring, patents, "
            "filings, policy disclosures, sentiment, geospatial sources, macro releases, commodities, "
            "and news. Normalize each source into timestamped sector or ticker metrics, map them to "
            "GICS or NSE sector definitions, validate predictive power with out-of-sample tests, and "
            "store source quality, legal constraints, and model outputs for auditability."
        ),
    ),
]


class RAGFileStore:
    def __init__(self, root: Path | str = DEFAULT_ROOT):
        self.root = Path(root)
        self.documents_path = self.root / "documents.jsonl"
        self.trade_cases_path = self.root / "trade_cases.jsonl"
        self.audit_path = self.root / "audit_bundles.jsonl"
        self._lock = RLock()

    def ensure_seeded(self) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            existing_ids = {doc.id for doc in self.load_documents(seed=False)}
            missing = [doc for doc in DEFAULT_DOCUMENTS if doc.id not in existing_ids]
            if missing:
                self.append_documents(missing)

    def load_documents(self, *, seed: bool = True) -> list[RAGDocument]:
        if seed:
            self.ensure_seeded()
        return [RAGDocument.model_validate(row) for row in self._read_jsonl(self.documents_path)]

    def append_documents(self, documents: Iterable[RAGDocument]) -> int:
        rows = [doc.model_dump() for doc in documents]
        self._append_jsonl(self.documents_path, rows)
        return len(rows)

    def load_trade_cases(self) -> list[TradeCaseRecord]:
        return [TradeCaseRecord.model_validate(row) for row in self._read_jsonl(self.trade_cases_path)]

    def append_trade_cases(self, cases: Iterable[TradeCaseRecord]) -> int:
        rows = [case.model_dump() for case in cases]
        self._append_jsonl(self.trade_cases_path, rows)
        return len(rows)

    def append_audit_bundle(self, payload: dict[str, Any]) -> Path:
        self._append_jsonl(self.audit_path, [payload])
        return self.audit_path

    def stats(self) -> dict[str, Any]:
        self.ensure_seeded()
        return {
            "root": str(self.root),
            "documents": len(self.load_documents(seed=False)),
            "trade_cases": len(self.load_trade_cases()),
            "audit_bundles": len(self._read_jsonl(self.audit_path)),
        }

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self._lock:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return rows

    def _append_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, default=str, sort_keys=True) + "\n")
