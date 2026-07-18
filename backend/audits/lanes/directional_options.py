"""Directional Options — long-premium directional sleeve (3 indices + rotating
NIFTY-50 stock batch).

Execution-capable paper lane persisted to the ``directional_paper_positions`` /
``directional_paper_journal`` DB tables. This auditor covers the data-quality /
signal-correctness dimensions checkable without a replay engine (see
``audits/lanes/_common.py``): provenance precedence, completed-bar completeness,
input freshness, greeks/IV completeness (this lane DOES consume greeks), gate
funnel / rejection attribution, and book/journal reconciliation. Per-lane
replay-parity is the deeper follow-up (S1-only today).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import ClassVar

from sqlalchemy import text

from audits.lanes._common import (
    BaseLaneAuditor,
    LaneAuditProfile,
    LaneAuditState,
    _safe_execute,
)

PROFILE = LaneAuditProfile(
    key="directional_options",
    label="Directional Options Paper Cycle",
    strategy_key="directional_long_options",
    expected_decision_source="fyers",  # fast lane: Fyers WS marks + fast decision feed
    cadence_seconds=300.0,
    greeks_applicable=True,
    notes="DB-backed (directional_paper_positions / _journal); greeks-consuming.",
)


@dataclass
class DirectionalOptionsAuditor(BaseLaneAuditor):
    profile: ClassVar[LaneAuditProfile] = PROFILE

    async def collect_state(self, ws, we, audit_date, lookback_days) -> LaneAuditState:
        st = LaneAuditState.empty()
        s = self.session

        # ── gate funnel / rejection attribution (journal: approved=false) ─────
        gate = await _safe_execute(
            s,
            text(
                """
                SELECT COALESCE(payload->>'gate', payload->>'reason', 'unknown') AS gate,
                       COUNT(*) AS n
                FROM directional_paper_journal
                WHERE approved = false AND recorded_at >= :ws AND recorded_at < :we
                GROUP BY 1
                """
            ),
            {"ws": ws, "we": we},
        )
        if gate is None:
            st.errors["gate"] = "directional_paper_journal query failed"
        else:
            rows = gate.mappings().all()
            st.gate_breakdown = {r["gate"]: int(r["n"]) for r in rows}
            st.gate_unknown = st.gate_breakdown.get("unknown", 0)

        emitted = await _safe_execute(
            s,
            text(
                """
                SELECT COUNT(*) FROM directional_paper_journal
                WHERE approved = true AND recorded_at >= :ws AND recorded_at < :we
                """
            ),
            {"ws": ws, "we": we},
        )
        if emitted is not None:
            st.signals_emitted = int(emitted.scalar() or 0)

        # ── greeks / IV completeness on approved decisions ───────────────────
        greeks = await _safe_execute(
            s,
            text(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE payload->>'iv' IS NOT NULL AND payload->>'delta' IS NOT NULL
                    ) AS complete
                FROM directional_paper_journal
                WHERE approved = true AND recorded_at >= :ws AND recorded_at < :we
                """
            ),
            {"ws": ws, "we": we},
        )
        if greeks is None:
            st.errors["greeks"] = "directional_paper_journal greeks query failed"
        else:
            row = greeks.mappings().first()
            if row:
                st.greeks_rows_total = int(row["total"] or 0)
                st.greeks_rows_complete = int(row["complete"] or 0)
            if st.greeks_rows_total == 0:
                st.notes["greeks"] = "no approved decisions in window to check greeks on"

        # ── provenance precedence (best-effort; journal may not carry source) ─
        prov = await _safe_execute(
            s,
            text(
                """
                SELECT COALESCE(payload->>'decision_source', payload->>'source') AS src,
                       COUNT(*) AS n
                FROM directional_paper_journal
                WHERE recorded_at >= :ws AND recorded_at < :we
                  AND (payload ? 'decision_source' OR payload ? 'source')
                GROUP BY 1
                """
            ),
            {"ws": ws, "we": we},
        )
        if prov is None:
            st.errors["provenance"] = "directional_paper_journal provenance query failed"
        else:
            prov_rows = prov.mappings().all()
            total = sum(int(r["n"]) for r in prov_rows)
            st.provenance_rows = total
            if total:
                exp = self.profile.expected_decision_source or ""
                st.provenance_observed = ",".join(str(r["src"]) for r in prov_rows if r["src"])
                st.provenance_mismatches = sum(
                    int(r["n"]) for r in prov_rows
                    if not r["src"] or exp.lower() not in str(r["src"]).lower()
                )
            else:
                st.notes["provenance"] = (
                    "journal payload does not record a decision_source yet — wire the "
                    "broker/source tag onto the decision journal to enable this check"
                )

        # completed-bar completeness: not yet recorded on the journal payload.
        st.notes["completed_bar"] = (
            "decision bar closed-vs-forming flag is not persisted on the journal yet; "
            "record it to enable completed-bar completeness"
        )

        # ── freshness: age of the newest journal decision ────────────────────
        latest = await _safe_execute(
            s, text("SELECT MAX(recorded_at) FROM directional_paper_journal"),
        )
        if latest is not None:
            ts = latest.scalar()
            if ts is not None:
                now = datetime.now(timezone.utc) if ts.tzinfo else datetime.utcnow()
                st.input_age_seconds = (now - ts).total_seconds()

        # ── book / journal reconciliation ────────────────────────────────────
        openres = await _safe_execute(
            s, text("SELECT COUNT(*) FROM directional_paper_positions WHERE status = 'open'"),
        )
        if openres is None:
            st.errors["recon"] = "directional_paper_positions query failed"
        else:
            st.open_positions = int(openres.scalar() or 0)
            nullpay = await _safe_execute(
                s,
                text(
                    "SELECT COUNT(*) FROM directional_paper_positions "
                    "WHERE status = 'open' AND payload IS NULL"
                ),
            )
            if nullpay is not None and int(nullpay.scalar() or 0) > 0:
                st.recon_failures.append(
                    {"reason": "open_position_null_payload", "count": int(nullpay.scalar() or 0)}
                )

        # ── edge persistence (closed positions, trailing 365d) ───────────────
        closed = await _safe_execute(
            s,
            text(
                """
                SELECT payload, closed_at
                FROM directional_paper_positions
                WHERE status = 'closed' AND closed_at >= :start
                """
            ),
            {"start": audit_date - timedelta(days=365)},
        )
        if closed is not None:
            for r in closed.mappings().all():
                pnl_pct = _extract_ret_pct(r["payload"])
                if pnl_pct is not None:
                    st.closed_trades.append({"ret_pct": pnl_pct, "closed_at": r["closed_at"]})

        return st


def _extract_ret_pct(payload) -> float | None:
    """Best-effort return-% from a directional position payload (JSONB → dict)."""
    if payload is None:
        return None
    d = payload
    if isinstance(payload, str):
        import json
        try:
            d = json.loads(payload)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(d, dict):
        return None
    for k in ("return_pct", "pnl_pct", "realized_return_pct"):
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    pnl = d.get("realized_pnl")
    entry = d.get("entry_price")
    qty = d.get("qty") or d.get("quantity")
    if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (pnl, entry, qty)):
        denom = float(entry) * float(qty)
        if denom:
            return float(pnl) / denom * 100.0
    return None
