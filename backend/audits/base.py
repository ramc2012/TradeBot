from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Protocol


@dataclass
class InvariantResult:
    """One invariant's outcome.

    `status` is the source of truth:
        "pass"  — had evidence and passed
        "fail"  — had evidence and failed
        "na"    — no evidence; cannot judge (do NOT count as pass)

    `passed` is a convenience for callers who only care about the green
    light; it is True iff status == "pass".
    """

    name: str
    status: str  # "pass" | "fail" | "na"
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def is_na(self) -> bool:
        return self.status == "na"


@dataclass
class AuditResult:
    """Outcome of running one lane's audit over a date window."""

    lane: str
    audit_date: date
    window_start: datetime
    window_end: datetime

    data_integrity: InvariantResult
    replay_parity: InvariantResult
    gate_attribution: InvariantResult
    backtest_parity: InvariantResult
    trade_reconciliation: InvariantResult
    edge_persistence: InvariantResult

    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def invariants(self) -> list[InvariantResult]:
        return [
            self.data_integrity,
            self.replay_parity,
            self.gate_attribution,
            self.backtest_parity,
            self.trade_reconciliation,
            self.edge_persistence,
        ]

    @property
    def overall_status(self) -> str:
        # An N/A invariant blocks green — we cannot bless a lane we cannot test.
        # Any fail demotes to red. Yellow is "everything we can check passes
        # but at least one invariant is N/A".
        if any(inv.status == "fail" for inv in self.invariants):
            return "red"
        if any(inv.status == "na" for inv in self.invariants):
            return "yellow"
        return "green"

    def to_db_row(self) -> dict[str, Any]:
        di = self.data_integrity.detail
        rp = self.replay_parity.detail
        ga = self.gate_attribution.detail
        bp = self.backtest_parity.detail
        tr = self.trade_reconciliation.detail
        ep = self.edge_persistence.detail
        # Stash per-invariant tri-state status in metadata so the UI can
        # render pass / fail / na without a schema change.
        metadata = {
            **self.metadata,
            "invariant_status": {
                "data_integrity": self.data_integrity.status,
                "replay_parity": self.replay_parity.status,
                "gate_attribution": self.gate_attribution.status,
                "backtest_parity": self.backtest_parity.status,
                "trade_reconciliation": self.trade_reconciliation.status,
                "edge_persistence": self.edge_persistence.status,
            },
        }
        return {
            "lane": self.lane,
            "audit_date": self.audit_date,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "data_gaps": di.get("gaps", []),
            "freshness_violations": di.get("freshness_violations", 0),
            "data_integrity_pass": self.data_integrity.passed,
            "replay_signals": rp.get("replay_signals", 0),
            "live_signals": rp.get("live_signals", 0),
            "replay_match_count": rp.get("match_count", 0),
            "replay_mismatches": rp.get("mismatches", []),
            "replay_parity_pass": self.replay_parity.passed,
            "signals_emitted": ga.get("emitted", 0),
            "signals_blocked_total": ga.get("blocked_total", 0),
            "gate_block_breakdown": ga.get("breakdown", {}),
            "gate_attribution_pass": self.gate_attribution.passed,
            "backtest_live_diff": bp.get("diff", {}),
            "backtest_parity_pass": self.backtest_parity.passed,
            "trades_booked": tr.get("trades_booked", 0),
            "trade_recon_pass_count": tr.get("pass_count", 0),
            "trade_recon_failures": tr.get("failures", []),
            "trade_recon_pass": self.trade_reconciliation.passed,
            "expectancy_60d": ep.get("expectancy_60d"),
            "expectancy_baseline": ep.get("expectancy_baseline"),
            "drift_pct": ep.get("drift_pct"),
            "edge_persistence_pass": self.edge_persistence.passed,
            "overall_status": self.overall_status,
            "metadata": metadata,
        }


class LaneAuditor(Protocol):
    """Each lane provides a class implementing this protocol."""

    lane: str

    async def run(self, audit_date: date, lookback_days: int) -> AuditResult: ...
