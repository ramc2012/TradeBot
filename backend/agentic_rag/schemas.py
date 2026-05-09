from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


RAGCollection = Literal["playbooks", "policies", "context", "trade_cases", "audit"]


class RAGDocument(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    collection: RAGCollection = "context"
    title: str
    text: str
    source: str = "manual"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class TradeCaseRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    strategy_key: str
    underlying: str
    symbol: str | None = None
    setup_name: str | None = None
    regime: str | None = None
    direction: str | None = None
    entry_time: str | None = None
    exit_time: str | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    pnl: float | None = None
    r_multiple: float | None = None
    result: str | None = None
    tags: list[str] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)
    lesson: str | None = None
    source: str = "manual"
    created_at: str = Field(default_factory=utc_now_iso)

    def to_document(self) -> RAGDocument:
        fields = [
            f"strategy {self.strategy_key}",
            f"underlying {self.underlying}",
            f"symbol {self.symbol}" if self.symbol else "",
            f"setup {self.setup_name}" if self.setup_name else "",
            f"regime {self.regime}" if self.regime else "",
            f"direction {self.direction}" if self.direction else "",
            f"result {self.result}" if self.result else "",
            f"pnl {self.pnl}" if self.pnl is not None else "",
            f"r multiple {self.r_multiple}" if self.r_multiple is not None else "",
            " ".join(self.tags),
            " ".join(f"{key} {value}" for key, value in self.features.items() if value is not None),
            self.lesson or "",
        ]
        text = ". ".join(item for item in fields if item)
        return RAGDocument(
            id=self.id,
            collection="trade_cases",
            title=f"{self.strategy_key} {self.underlying} {self.setup_name or 'case'} {self.entry_time or ''}".strip(),
            text=text,
            source=self.source,
            metadata={
                "strategy_key": self.strategy_key,
                "underlying": self.underlying,
                "symbol": self.symbol,
                "setup_name": self.setup_name,
                "regime": self.regime,
                "direction": self.direction,
                "entry_time": self.entry_time,
                "exit_time": self.exit_time,
                "pnl": self.pnl,
                "r_multiple": self.r_multiple,
                "result": self.result,
                "tags": self.tags,
                "features": self.features,
                "case_id": self.id,
            },
            created_at=self.created_at,
            updated_at=self.created_at,
        )


class RAGSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=8, ge=1, le=50)
    filters: dict[str, Any] = Field(default_factory=dict)
    include_runtime_cases: bool = True
    recency_bias: float = Field(default=0.18, ge=0.0, le=1.0)


class ContextGateRequest(BaseModel):
    strategy_key: str
    underlying: str
    symbol: str | None = None
    signal_direction: str | None = None
    setup_name: str | None = None
    regime: str | None = None
    event_tags: list[str] = Field(default_factory=list)
    numeric_context: dict[str, Any] = Field(default_factory=dict)
    hard_risk_passed: bool = True
    query: str | None = None
    top_k_cases: int = Field(default=6, ge=1, le=20)
    top_k_docs: int = Field(default=6, ge=1, le=20)


class RAGSearchHit(BaseModel):
    id: str
    collection: str
    title: str
    text: str
    source: str
    metadata: dict[str, Any]
    score: float
    score_parts: dict[str, float]


class ContextGateResult(BaseModel):
    decision: Literal["allow", "warn", "block"]
    confidence: float
    summary: str
    reason_codes: list[str]
    case_stats: dict[str, Any]
    retrievals: list[RAGSearchHit]
    audit_bundle: dict[str, Any]
