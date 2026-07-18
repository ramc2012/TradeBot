"""Shared scaffolding for execution-capable lane auditors.

The S1 auditor (``s1_atm_30m_macd.py``) is the reference implementation: a
``@dataclass`` holding an ``AsyncSession``, an async ``run(audit_date,
lookback_days)`` that returns an :class:`AuditResult` built from six
:class:`InvariantResult` slots, resilient DB access via a SAVEPOINT-wrapped
``_safe_execute`` (a failed query yields ``None`` → a *failed CHECK with
detail*, never a raised exception), and registration in
``audits.lanes.REGISTRY`` so ``core.lane_registry`` can flip
``audit_coverage=True``.

S1 additionally owns a *replay-parity* harness (``audits/replay.py`` — a pure
recompute of the MACD zero-cross logic diffed byte-for-byte against
``agent_signals``). That harness is **S1-only today**. The other
execution-capable lanes have no pure decision-function replica yet, so this
module deliberately does **not** attempt per-lane replay; ``replay_parity`` is
reported ``na`` with a note pointing at that deeper follow-up.

What this module DOES give every lane, without a replay engine, is the set of
data-quality / signal-correctness dimensions that are checkable from recorded
state:

  * **provenance precedence** — did the *expected* broker/source feed the
    decision data, and are there rows that came from the wrong source?
  * **completed-bar completeness** — was the decision bar a CLOSED bar rather
    than a still-forming one?
  * **input freshness** — is the newest decision input young relative to the
    lane cadence?
  * **greeks / IV completeness** — for lanes that consume option greeks
    (directional, macd_refined); reported ``na`` for lanes that do not.
  * **gate-funnel / rejection attribution** — are rejected candidates
    categorised and counted (vs. an "unknown" bucket)?
  * **book / journal reconciliation** — do open positions match the journal?

The design separates *collection* (best-effort, lane-specific, resilient) from
*evaluation* (a pure function of a :class:`LaneAuditState`). Tests drive
``evaluate_state`` / ``run_from_state`` with fixture state and never touch a DB;
the live daily run calls ``collect_state`` which degrades to ``na`` on missing
data and to a failed CHECK on a source error, but never raises.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from audits.base import AuditResult, InvariantResult


async def _safe_execute(session: AsyncSession, sql, params: dict | None = None):
    """Execute inside a SAVEPOINT so a SQL error doesn't poison the parent tx.

    Returns the result on success or ``None`` on failure (caller maps ``None``
    to a failed/na CHECK). Mirrors the helper in the S1 auditor.
    """
    sp = await session.begin_nested()
    try:
        result = await session.execute(sql, params or {})
        await sp.commit()
        return result
    except Exception:  # noqa: BLE001 — resilience is the contract
        await sp.rollback()
        return None


# ---------------------------------------------------------------------------
# Per-lane declarative profile
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LaneAuditProfile:
    key: str
    label: str
    strategy_key: str | None = None
    expected_decision_source: str | None = None  # e.g. "fyers_ws", "upstox_rest"
    cadence_seconds: float | None = None
    greeks_applicable: bool = False
    # Freshness: violation when age > max(cadence * multiple, floor). The 24h
    # floor keeps off-hours audits (market closed) from false-positiving, same
    # spirit as the S1 auditor's FRESHNESS_TOLERANCE_SECONDS.
    freshness_tolerance_multiple: float = 3.0
    freshness_floor_seconds: float = 24 * 60 * 60
    greeks_completeness_threshold: float = 0.95
    gate_unknown_max_pct: float = 5.0
    replay_followup_note: str = (
        "replay-parity harness is implemented for s1 only (audits/replay.py). "
        "Per-lane pure-recompute replay is the deeper follow-up: it needs a "
        "lane-specific decision function replica + a candle/feature store to "
        "diff against the live journal."
    )
    backtest_note: str = (
        "no backtest<->live run_id<->lane mapping for this lane yet; comparison "
        "deferred until a mapping table exists."
    )
    notes: str | None = None


# ---------------------------------------------------------------------------
# Collected evidence — a pure snapshot the evaluator judges
# ---------------------------------------------------------------------------
@dataclass
class LaneAuditState:
    # provenance precedence
    provenance_expected: str | None = None
    provenance_observed: str | None = None
    provenance_rows: int = 0
    provenance_mismatches: int = 0
    # completed-bar completeness
    decision_bars_total: int = 0
    decision_bars_forming: int = 0
    # input freshness
    input_age_seconds: float | None = None
    # greeks / IV completeness
    greeks_rows_total: int = 0
    greeks_rows_complete: int = 0
    # gate funnel / rejection attribution
    gate_breakdown: dict[str, int] = field(default_factory=dict)
    gate_unknown: int = 0
    signals_emitted: int = 0
    # book / journal reconciliation
    open_positions: int | None = None
    journal_open_positions: int | None = None
    recon_failures: list[dict[str, Any]] = field(default_factory=list)
    # edge persistence (best-effort; each item {"ret_pct": float, "closed_at": dt|None})
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    # error surface — per-dimension source error (→ failed CHECK, never raise)
    errors: dict[str, str] = field(default_factory=dict)
    # free-form per-dimension notes (why na, what is pending wiring)
    notes: dict[str, str] = field(default_factory=dict)
    # whole-collection failure (collect_state itself raised)
    collection_error: str | None = None

    @classmethod
    def empty(cls) -> "LaneAuditState":
        return cls()

    @classmethod
    def collection_failed(cls, err: str) -> "LaneAuditState":
        return cls(collection_error=str(err)[:300])


# ---------------------------------------------------------------------------
# Evaluation — pure, deterministic, testable
# ---------------------------------------------------------------------------
def window_bounds(audit_date: date, lookback_days: int) -> tuple[datetime, datetime]:
    ws = datetime.combine(audit_date - timedelta(days=lookback_days), time(0, 0))
    we = datetime.combine(audit_date + timedelta(days=1), time(0, 0))
    return ws, we


def _sub(name: str, status: str, **detail: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail}


def _di_provenance(profile: LaneAuditProfile, st: LaneAuditState) -> dict[str, Any]:
    if "provenance" in st.errors:
        return _sub("provenance", "fail", error=st.errors["provenance"])
    if profile.expected_decision_source is None:
        return _sub("provenance", "na", note="lane declares no expected decision source")
    if st.provenance_rows == 0:
        return _sub(
            "provenance", "na",
            note=st.notes.get("provenance", "no decision-source rows observed in window"),
            expected=profile.expected_decision_source,
        )
    status = "pass" if st.provenance_mismatches == 0 else "fail"
    return _sub(
        "provenance", status,
        expected=profile.expected_decision_source,
        observed=st.provenance_observed,
        rows=st.provenance_rows,
        mismatches=st.provenance_mismatches,
    )


def _di_completed_bar(st: LaneAuditState) -> dict[str, Any]:
    if "completed_bar" in st.errors:
        return _sub("completed_bar", "fail", error=st.errors["completed_bar"])
    if st.decision_bars_total == 0:
        return _sub("completed_bar", "na",
                    note=st.notes.get("completed_bar", "no decision bars observed in window"))
    status = "pass" if st.decision_bars_forming == 0 else "fail"
    return _sub("completed_bar", status,
                decision_bars=st.decision_bars_total, forming_bars=st.decision_bars_forming)


def _di_freshness(profile: LaneAuditProfile, st: LaneAuditState) -> dict[str, Any]:
    if "freshness" in st.errors:
        return _sub("freshness", "fail", error=st.errors["freshness"])
    if st.input_age_seconds is None or profile.cadence_seconds is None:
        return _sub("freshness", "na",
                    note=st.notes.get("freshness", "no fresh-input age or cadence available"))
    tolerance = max(
        profile.cadence_seconds * profile.freshness_tolerance_multiple,
        profile.freshness_floor_seconds,
    )
    status = "pass" if st.input_age_seconds <= tolerance else "fail"
    return _sub("freshness", status,
                input_age_seconds=round(float(st.input_age_seconds), 1),
                tolerance_seconds=round(float(tolerance), 1))


def _di_greeks(profile: LaneAuditProfile, st: LaneAuditState) -> dict[str, Any]:
    if not profile.greeks_applicable:
        return _sub("greeks_completeness", "na",
                    note="lane does not consume option greeks/IV")
    if "greeks" in st.errors:
        return _sub("greeks_completeness", "fail", error=st.errors["greeks"])
    if st.greeks_rows_total == 0:
        return _sub("greeks_completeness", "na",
                    note=st.notes.get("greeks", "no greeks-bearing decision rows observed"))
    pct = st.greeks_rows_complete / st.greeks_rows_total
    status = "pass" if pct >= profile.greeks_completeness_threshold else "fail"
    return _sub("greeks_completeness", status,
                rows=st.greeks_rows_total, complete=st.greeks_rows_complete,
                complete_pct=round(pct * 100.0, 2),
                threshold_pct=round(profile.greeks_completeness_threshold * 100.0, 2))


def _aggregate_status(subs: list[dict[str, Any]]) -> str:
    statuses = [s["status"] for s in subs]
    if any(s == "fail" for s in statuses):
        return "fail"
    if all(s == "na" for s in statuses):
        return "na"
    return "pass"


def _invariant_data_integrity(profile: LaneAuditProfile, st: LaneAuditState) -> InvariantResult:
    subs = [
        _di_provenance(profile, st),
        _di_completed_bar(st),
        _di_freshness(profile, st),
        _di_greeks(profile, st),
    ]
    status = _aggregate_status(subs)
    failing = [s["name"] for s in subs if s["status"] == "fail"]
    freshness_violations = 1 if any(
        s["name"] == "freshness" and s["status"] == "fail" for s in subs
    ) else 0
    return InvariantResult(
        name="data_integrity",
        status=status,
        detail={
            # to_db_row compatibility keys:
            "gaps": [{"check": n} for n in failing],
            "freshness_violations": freshness_violations,
            # rich per-sub-check breakdown for the report/metadata:
            "sub_checks": {s["name"]: {"status": s["status"], **s["detail"]} for s in subs},
        },
    )


def _invariant_gate_attribution(profile: LaneAuditProfile, st: LaneAuditState) -> InvariantResult:
    if "gate" in st.errors:
        return InvariantResult(name="gate_attribution", status="fail",
                               detail={"error": st.errors["gate"], "emitted": 0,
                                       "blocked_total": 0, "breakdown": {}})
    blocked_total = sum(st.gate_breakdown.values())
    if blocked_total == 0:
        return InvariantResult(
            name="gate_attribution", status="na",
            detail={"emitted": int(st.signals_emitted), "blocked_total": 0, "breakdown": {},
                    "note": st.notes.get("gate",
                                         "no rejection/gate events recorded — instrument the "
                                         "lane to log why candidates were blocked")},
        )
    unknown_pct = st.gate_unknown / blocked_total * 100.0
    status = "pass" if unknown_pct <= profile.gate_unknown_max_pct else "fail"
    return InvariantResult(
        name="gate_attribution", status=status,
        detail={"emitted": int(st.signals_emitted), "blocked_total": blocked_total,
                "breakdown": dict(st.gate_breakdown), "unknown_pct": round(unknown_pct, 2)},
    )


def _invariant_trade_reconciliation(st: LaneAuditState) -> InvariantResult:
    if "recon" in st.errors:
        return InvariantResult(name="trade_reconciliation", status="fail",
                               detail={"error": st.errors["recon"]})
    have_evidence = st.open_positions is not None or st.journal_open_positions is not None
    if not have_evidence:
        return InvariantResult(
            name="trade_reconciliation", status="na",
            detail={"trades_booked": 0, "pass_count": 0, "failures": [],
                    "note": st.notes.get("recon", "no book/journal evidence for this lane in the window")},
        )
    failures = list(st.recon_failures)
    if (st.open_positions is not None and st.journal_open_positions is not None
            and st.open_positions != st.journal_open_positions):
        failures.append({
            "reason": "open_position_count_mismatch",
            "book_open": st.open_positions,
            "journal_open": st.journal_open_positions,
        })
    booked = st.open_positions if st.open_positions is not None else st.journal_open_positions
    status = "pass" if not failures else "fail"
    return InvariantResult(
        name="trade_reconciliation", status=status,
        detail={"trades_booked": int(booked or 0),
                "journal_open_positions": st.journal_open_positions,
                "pass_count": 0 if failures else int(booked or 0),
                "failures": failures[:50]},
    )


def _invariant_edge_persistence(st: LaneAuditState, audit_date: date) -> InvariantResult:
    trades = st.closed_trades
    if not trades:
        return InvariantResult(
            name="edge_persistence", status="na",
            detail={"expectancy_60d": None, "expectancy_baseline": None, "drift_pct": None,
                    "note": st.notes.get("edge", "no closed trades in trailing window — cannot compute expectancy")},
        )
    cutoff60 = datetime.combine(audit_date - timedelta(days=60), time(0, 0))

    def _ret(t: dict[str, Any]) -> float | None:
        v = t.get("ret_pct")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def _closed_at(t: dict[str, Any]):
        ca = t.get("closed_at")
        if isinstance(ca, datetime):
            return ca.replace(tzinfo=None)
        return None

    rets_60 = [r for t in trades if (_closed_at(t) and _closed_at(t) >= cutoff60)
               for r in [_ret(t)] if r is not None]
    rets_all = [r for t in trades for r in [_ret(t)] if r is not None]
    exp60 = sum(rets_60) / len(rets_60) if rets_60 else None
    baseline = sum(rets_all) / len(rets_all) if rets_all else None
    drift_pct: float | None = None
    if exp60 is not None and baseline is not None and baseline != 0:
        drift_pct = (exp60 - baseline) / abs(baseline) * 100.0
    if exp60 is None or baseline is None:
        return InvariantResult(
            name="edge_persistence", status="na",
            detail={"expectancy_60d": exp60, "expectancy_baseline": baseline, "drift_pct": drift_pct,
                    "n_60d": len(rets_60), "n_baseline": len(rets_all),
                    "note": "insufficient closed-trade samples for the 60d or baseline window"},
        )
    status = "pass" if exp60 > 0 and (drift_pct is None or drift_pct > -30.0) else "fail"
    return InvariantResult(
        name="edge_persistence", status=status,
        detail={"expectancy_60d": exp60, "expectancy_baseline": baseline, "drift_pct": drift_pct,
                "n_60d": len(rets_60), "n_baseline": len(rets_all)},
    )


def evaluate_state(
    profile: LaneAuditProfile,
    st: LaneAuditState,
    audit_date: date,
    window_start: datetime,
    window_end: datetime,
    lookback_days: int,
) -> AuditResult:
    """Build the six-invariant :class:`AuditResult` purely from collected state."""
    replay = InvariantResult(name="replay_parity", status="na",
                             detail={"note": profile.replay_followup_note})
    backtest = InvariantResult(name="backtest_parity", status="na",
                               detail={"note": profile.backtest_note})

    if st.collection_error:
        err = {"error": st.collection_error}
        di = InvariantResult(name="data_integrity", status="fail",
                             detail={"gaps": [{"check": "collection"}], "freshness_violations": 0,
                                     "sub_checks": {}, **err})
        ga = InvariantResult(name="gate_attribution", status="fail",
                             detail={"emitted": 0, "blocked_total": 0, "breakdown": {}, **err})
        tr = InvariantResult(name="trade_reconciliation", status="fail",
                             detail={"trades_booked": 0, "pass_count": 0, "failures": [], **err})
        ep = InvariantResult(name="edge_persistence", status="na",
                             detail={"expectancy_60d": None, "expectancy_baseline": None,
                                     "drift_pct": None, "note": "not collected (collection error)"})
    else:
        di = _invariant_data_integrity(profile, st)
        ga = _invariant_gate_attribution(profile, st)
        tr = _invariant_trade_reconciliation(st)
        ep = _invariant_edge_persistence(st, audit_date)

    return AuditResult(
        lane=profile.key,
        audit_date=audit_date,
        window_start=window_start,
        window_end=window_end,
        data_integrity=di,
        replay_parity=replay,
        gate_attribution=ga,
        backtest_parity=backtest,
        trade_reconciliation=tr,
        edge_persistence=ep,
        metadata={"label": profile.label, "lookback_days": lookback_days,
                  "provenance_model": "data-quality/signal-correctness (no replay engine)",
                  "notes": profile.notes},
    )


# ---------------------------------------------------------------------------
# Base auditor — resilient run() wrapping a lane-specific collect_state()
# ---------------------------------------------------------------------------
@dataclass
class BaseLaneAuditor:
    session: AsyncSession
    # Subclasses set these as class attributes.
    profile: ClassVar[LaneAuditProfile]

    @property
    def lane(self) -> str:
        return self.profile.key

    async def collect_state(self, ws: datetime, we: datetime, audit_date: date,
                            lookback_days: int) -> LaneAuditState:
        """Override per lane. Default: no evidence (every dimension → na)."""
        return LaneAuditState.empty()

    async def _safe_collect(self, ws: datetime, we: datetime, audit_date: date,
                            lookback_days: int) -> LaneAuditState:
        try:
            return await self.collect_state(ws, we, audit_date, lookback_days)
        except Exception as exc:  # noqa: BLE001 — a collection error is a failed CHECK, not a crash
            return LaneAuditState.collection_failed(f"{type(exc).__name__}: {exc}")

    async def run(self, audit_date: date, lookback_days: int = 30) -> AuditResult:
        ws, we = window_bounds(audit_date, lookback_days)
        st = await self._safe_collect(ws, we, audit_date, lookback_days)
        return evaluate_state(self.profile, st, audit_date, ws, we, lookback_days)

    def run_from_state(self, st: LaneAuditState, audit_date: date,
                       lookback_days: int = 30) -> AuditResult:
        """Pure, DB-free entry point used by tests."""
        ws, we = window_bounds(audit_date, lookback_days)
        return evaluate_state(self.profile, st, audit_date, ws, we, lookback_days)


# ---------------------------------------------------------------------------
# Reusable best-effort collectors (shared plane: agent_* tables by strategy_key)
# ---------------------------------------------------------------------------
async def collect_gate_and_signals_by_strategy_key(
    session: AsyncSession, strategy_key: str, ws: datetime, we: datetime, st: LaneAuditState,
) -> None:
    """Populate gate_breakdown / gate_unknown / signals_emitted from the shared
    agent_audit_events + agent_signals tables. Best-effort: on query failure sets
    st.errors['gate']; on empty result leaves the na note in place."""
    from sqlalchemy import text

    res = await _safe_execute(
        session,
        text(
            """
            SELECT COALESCE(payload->>'gate', 'unknown') AS gate, COUNT(*) AS n
            FROM agent_audit_events
            WHERE event_type = 'signal_blocked' AND strategy_key = :sk
              AND created_at >= :ws AND created_at < :we
            GROUP BY 1
            """
        ),
        {"sk": strategy_key, "ws": ws, "we": we},
    )
    if res is None:
        st.errors["gate"] = "agent_audit_events query failed"
    else:
        rows = res.mappings().all()
        st.gate_breakdown = {r["gate"]: int(r["n"]) for r in rows}
        st.gate_unknown = st.gate_breakdown.get("unknown", 0)

    emitted = await _safe_execute(
        session,
        text(
            """
            SELECT COUNT(*) FROM agent_signals
            WHERE strategy_key = :sk AND signal_bar_time >= :ws AND signal_bar_time < :we
            """
        ),
        {"sk": strategy_key, "ws": ws, "we": we},
    )
    if emitted is not None:
        st.signals_emitted = int(emitted.scalar() or 0)


async def collect_positions_by_strategy_key(
    session: AsyncSession, strategy_key: str, ws: datetime, we: datetime,
    audit_date: date, st: LaneAuditState,
) -> None:
    """Populate open_positions + closed_trades from agent_positions. Best-effort."""
    from sqlalchemy import text

    openres = await _safe_execute(
        session,
        text("SELECT COUNT(*) FROM agent_positions WHERE strategy_key = :sk AND status = 'open'"),
        {"sk": strategy_key},
    )
    if openres is None:
        st.errors["recon"] = "agent_positions query failed"
    else:
        st.open_positions = int(openres.scalar() or 0)

    closed = await _safe_execute(
        session,
        text(
            """
            SELECT realized_pnl, entry_price, qty, closed_at
            FROM agent_positions
            WHERE strategy_key = :sk AND status = 'closed' AND closed_at >= :start
            """
        ),
        {"sk": strategy_key, "start": audit_date - timedelta(days=365)},
    )
    if closed is not None:
        for r in closed.mappings().all():
            ep, qty = r["entry_price"], r["qty"]
            if not ep or not qty:
                continue
            denom = float(ep) * float(qty)
            if denom == 0:
                continue
            st.closed_trades.append({
                "ret_pct": float(r["realized_pnl"] or 0) / denom * 100.0,
                "closed_at": r["closed_at"],
            })
