"""SQLAlchemy ORM models for Nomad Curie."""
from __future__ import annotations
import uuid
from datetime import date, datetime
from typing import Any, Optional
from sqlalchemy import (
    String, Integer, Float, Boolean, Date, DateTime, Text, JSON,
    Enum as SAEnum, ForeignKey, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from db.database import Base
import enum


# ─── Enums ──────────────────────────────────────────────────────────────────

class BrokerEnum(str, enum.Enum):
    fyers = "fyers"
    upstox = "upstox"
    fivepaisa = "fivepaisa"
    icici_breeze = "icici_breeze"

class TradingModeEnum(str, enum.Enum):
    paper = "paper"
    live = "live"

class OrderTypeEnum(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL_M"

class ActionEnum(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderStatusEnum(str, enum.Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

class InstrumentTypeEnum(str, enum.Enum):
    CE = "CE"
    PE = "PE"
    FUT = "FUT"
    EQ = "EQ"

class ConfidenceEnum(str, enum.Enum):
    HIGH = "HIGH"
    MED = "MED"
    LOW = "LOW"

class ProposalStatusEnum(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    EXPIRED = "EXPIRED"


# ─── Tables ─────────────────────────────────────────────────────────────────

class BrokerSession(Base):
    __tablename__ = "broker_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    broker: Mapped[str] = mapped_column(SAEnum(BrokerEnum), nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(String(100))
    access_token_enc: Mapped[Optional[str]] = mapped_column(Text)
    refresh_token_enc: Mapped[Optional[str]] = mapped_column(Text)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PaperSession(Base):
    __tablename__ = "paper_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    broker: Mapped[str] = mapped_column(SAEnum(BrokerEnum), nullable=False)
    initial_capital: Mapped[float] = mapped_column(Float, default=1_000_000.0)
    current_capital: Mapped[float] = mapped_column(Float, default=1_000_000.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    orders: Mapped[list["Order"]] = relationship("Order", back_populates="paper_session")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("paper_sessions.id"), nullable=True)
    mode: Mapped[str] = mapped_column(SAEnum(TradingModeEnum), nullable=False)
    broker: Mapped[str] = mapped_column(SAEnum(BrokerEnum), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    exchange: Mapped[str] = mapped_column(String(20), default="NSE")
    instrument_type: Mapped[str] = mapped_column(SAEnum(InstrumentTypeEnum), nullable=False)
    strike: Mapped[Optional[float]] = mapped_column(Float)
    expiry: Mapped[Optional[str]] = mapped_column(String(20))
    option_type: Mapped[Optional[str]] = mapped_column(String(5))
    action: Mapped[str] = mapped_column(SAEnum(ActionEnum), nullable=False)
    order_type: Mapped[str] = mapped_column(SAEnum(OrderTypeEnum), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Optional[float]] = mapped_column(Float)
    sl: Mapped[Optional[float]] = mapped_column(Float)
    target: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[str] = mapped_column(SAEnum(OrderStatusEnum), default=OrderStatusEnum.PENDING)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(100))
    fill_price: Mapped[Optional[float]] = mapped_column(Float)
    fill_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    paper_session: Mapped[Optional["PaperSession"]] = relationship("PaperSession", back_populates="orders")


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    mode: Mapped[str] = mapped_column(SAEnum(TradingModeEnum), nullable=False)
    broker: Mapped[str] = mapped_column(SAEnum(BrokerEnum), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    strike: Mapped[Optional[float]] = mapped_column(Float)
    expiry: Mapped[Optional[str]] = mapped_column(String(20))
    option_type: Mapped[Optional[str]] = mapped_column(String(5))
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_price: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentProposal(Base):
    __tablename__ = "agent_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    strategy: Mapped[str] = mapped_column(String(100), nullable=False)
    entry: Mapped[float] = mapped_column(Float, nullable=False)
    sl: Mapped[float] = mapped_column(Float, nullable=False)
    target: Mapped[float] = mapped_column(Float, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    rationale: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(SAEnum(ConfidenceEnum), nullable=False)
    status: Mapped[str] = mapped_column(SAEnum(ProposalStatusEnum), default=ProposalStatusEnum.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    acted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class AgentLog(Base):
    __tablename__ = "agent_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 or 2
    input_context: Mapped[Optional[dict]] = mapped_column(JSON)
    reasoning: Mapped[Optional[str]] = mapped_column(Text)
    output: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ValidationRun(Base):
    __tablename__ = "validation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gate: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(150), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    symbol: Mapped[Optional[str]] = mapped_column(String(50))
    mode: Mapped[Optional[str]] = mapped_column(String(30))
    scenario: Mapped[Optional[str]] = mapped_column(String(50))
    source: Mapped[Optional[str]] = mapped_column(String(50))
    context: Mapped[Optional[Any]] = mapped_column(JSON)
    report: Mapped[Optional[Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    metrics: Mapped[list["ValidationMetric"]] = relationship(
        "ValidationMetric",
        back_populates="run",
        cascade="all, delete-orphan",
    )
    artifacts: Mapped[list["ValidationArtifact"]] = relationship(
        "ValidationArtifact",
        back_populates="run",
        cascade="all, delete-orphan",
    )


class ValidationMetric(Base):
    __tablename__ = "validation_metrics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("validation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    metric_name: Mapped[str] = mapped_column(String(120), nullable=False)
    metric_value: Mapped[Optional[Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped["ValidationRun"] = relationship("ValidationRun", back_populates="metrics")


class ValidationArtifact(Base):
    __tablename__ = "validation_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("validation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    artifact_key: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[Optional[Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped["ValidationRun"] = relationship("ValidationRun", back_populates="artifacts")


class ShadowObservation(Base):
    __tablename__ = "shadow_observations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source: Mapped[Optional[str]] = mapped_column(String(50))
    snapshot_mode: Mapped[Optional[str]] = mapped_column(String(30))
    agent_name: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    regime_label: Mapped[Optional[str]] = mapped_column(String(50))
    setup_name: Mapped[Optional[str]] = mapped_column(String(100))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entry_price: Mapped[Optional[float]] = mapped_column(Float)
    stop_price: Mapped[Optional[float]] = mapped_column(Float)
    target_price: Mapped[Optional[float]] = mapped_column(Float)
    tick_size: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    risk_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    simulated_fill_price: Mapped[Optional[float]] = mapped_column(Float)
    observed_touch_price: Mapped[Optional[float]] = mapped_column(Float)
    observed_fill_price: Mapped[Optional[float]] = mapped_column(Float)
    fill_drift_ticks: Mapped[Optional[float]] = mapped_column(Float)
    stale_signal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reconciliation_status: Mapped[str] = mapped_column(String(30), nullable=False, default="matched")
    mismatch_duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    kill_switch_tested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    kill_switch_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dashboard_checked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    alerts_checked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manual_override_tested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    details: Mapped[Optional[Any]] = mapped_column("metadata", JSON)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class RLPolicyVersion(Base):
    __tablename__ = "rl_policy_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="candidate", index=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="manual")
    symbol: Mapped[Optional[str]] = mapped_column(String(50))
    trained_on: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    average_reward: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    metrics: Mapped[Optional[Any]] = mapped_column(JSON)
    qtable_snapshot: Mapped[Any] = mapped_column(JSON, nullable=False)
    promotion_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    promoted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
