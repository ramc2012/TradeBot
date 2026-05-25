"""S1 — NSE 30-min ATM-option MACD zero-cross.

Backtested edge: 725 signals, 86.1% win rate, +114.6% median exit return
(STRATEGY_DOCUMENT.md). This auditor verifies the live recorder still matches
that pure-function logic and that no invariant has drifted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audits.base import AuditResult, InvariantResult
from audits.replay import detect_zero_cross_signals, diff_signal_sets


async def _safe_execute(session: AsyncSession, sql, params: dict | None = None):
    """Execute inside a SAVEPOINT so a SQL error doesn't poison the parent tx.

    Returns the result on success or None on failure (caller must handle None).
    """
    sp = await session.begin_nested()
    try:
        result = await session.execute(sql, params or {})
        await sp.commit()
        return result
    except Exception:  # noqa: BLE001
        await sp.rollback()
        return None


LANE_KEY = "s1"
LANE_LABEL = "NSE 30m ATM MACD"
STRATEGY_KEY_FILTER = "macd_strategy"  # actual strategy_key as observed in agent_signals
INTERVAL_LITERAL = "30minute"  # actual interval value in option_premium_candles
INTERVAL_MINUTES = 30
EXPECTED_BARS_PER_DAY = 13  # 09:15→15:30 IST, 30-min bars
MAX_GAP_BARS = 1
# Off-hours, the most-recent candle can be many hours old; only flag if > 24h.
# A proper market-hours check belongs here once we wire the IST session calendar.
FRESHNESS_TOLERANCE_SECONDS = 24 * 60 * 60


@dataclass
class S1Auditor:
    session: AsyncSession
    lane: str = LANE_KEY

    async def run(self, audit_date: date, lookback_days: int = 30) -> AuditResult:
        window_start = datetime.combine(audit_date - timedelta(days=lookback_days), time(0, 0))
        window_end = datetime.combine(audit_date + timedelta(days=1), time(0, 0))

        di = await self._invariant_data_integrity(window_start, window_end)
        rp = await self._invariant_replay_parity(window_start, window_end)
        ga = await self._invariant_gate_attribution(window_start, window_end)
        bp = await self._invariant_backtest_parity(window_start, window_end)
        tr = await self._invariant_trade_reconciliation(window_start, window_end)
        ep = await self._invariant_edge_persistence(audit_date)

        return AuditResult(
            lane=self.lane,
            audit_date=audit_date,
            window_start=window_start,
            window_end=window_end,
            data_integrity=di,
            replay_parity=rp,
            gate_attribution=ga,
            backtest_parity=bp,
            trade_reconciliation=tr,
            edge_persistence=ep,
            metadata={"label": LANE_LABEL, "lookback_days": lookback_days},
        )

    # ── Invariant 1: data integrity ─────────────────────────────────────────
    async def _invariant_data_integrity(self, ws: datetime, we: datetime) -> InvariantResult:
        # Count 30m candles per session-day. Days below the threshold are gaps.
        sql = text(
            """
            SELECT DATE(time AT TIME ZONE 'Asia/Kolkata') AS d, COUNT(*) AS n
            FROM option_premium_candles
            WHERE time >= :ws AND time < :we
              AND interval = '30minute'
            GROUP BY 1
            ORDER BY 1
            """
        )
        rows = (await self.session.execute(sql, {"ws": ws, "we": we})).mappings().all()
        gaps = [
            {"date": str(r["d"]), "bars": int(r["n"])}
            for r in rows
            if int(r["n"]) < EXPECTED_BARS_PER_DAY - MAX_GAP_BARS
        ]

        # Freshness: most recent bar should be within tolerance during market hours.
        freshness_violations = 0
        latest = (
            await self.session.execute(
                text("SELECT MAX(time) FROM option_premium_candles WHERE interval = '30minute'")
            )
        ).scalar()
        if latest is not None:
            now = datetime.utcnow().replace(tzinfo=latest.tzinfo) if latest.tzinfo else datetime.utcnow()
            if (now - latest).total_seconds() > FRESHNESS_TOLERANCE_SECONDS:
                freshness_violations = 1

        # If there are zero candles in the entire window, we have no evidence.
        total_bars = sum(int(r["n"]) for r in rows)
        if total_bars == 0:
            return InvariantResult(
                name="data_integrity",
                status="na",
                detail={"gaps": [], "freshness_violations": 0, "note": "no candles in window"},
            )
        status = "pass" if (not gaps and freshness_violations == 0) else "fail"
        return InvariantResult(
            name="data_integrity",
            status=status,
            detail={"gaps": gaps, "freshness_violations": freshness_violations, "total_bars": total_bars},
        )

    # ── Invariant 2: replay parity ──────────────────────────────────────────
    async def _invariant_replay_parity(self, ws: datetime, we: datetime) -> InvariantResult:
        # Filter to FIRED signals only — `watching` rows are watchlist candidates,
        # not signals the lane actually emitted. (See agent_signals.status values:
        # watching, candidate, observed. Only the last two represent a real fire.)
        # NOTE: this audit only replays contracts that had a live signal. A
        # contract the live recorder skipped entirely will NOT show up as a
        # mismatch here — that's a known one-sided gap; a future revision should
        # walk the universe of ATM contracts in the window.
        contract_rows = (
            await self.session.execute(
                text(
                    """
                    SELECT DISTINCT underlying, expiry, strike, option_type
                    FROM agent_signals
                    WHERE strategy_key = :sk
                      AND status IN ('candidate', 'observed', 'fired', 'CLOSED')
                      AND signal_bar_time >= :ws AND signal_bar_time < :we
                    """
                ),
                {"sk": STRATEGY_KEY_FILTER, "ws": ws, "we": we},
            )
        ).mappings().all()

        replay_signals = []
        for c in contract_rows:
            candles = await self._load_candles(
                c["underlying"], c["expiry"], c["strike"], c["option_type"], ws, we
            )
            replay_signals.extend(
                detect_zero_cross_signals(candles, underlying=c["underlying"])
            )

        live_rows = (
            await self.session.execute(
                text(
                    """
                    SELECT signal_bar_time, underlying, expiry, strike, option_type, signal_reason
                    FROM agent_signals
                    WHERE strategy_key = :sk
                      AND status IN ('candidate', 'observed', 'fired', 'CLOSED')
                      AND signal_bar_time >= :ws AND signal_bar_time < :we
                    """
                ),
                {"sk": STRATEGY_KEY_FILTER, "ws": ws, "we": we},
            )
        ).mappings().all()

        live_keys = [
            (
                r["signal_bar_time"].replace(microsecond=0).isoformat(),
                r["underlying"],
                str(r["expiry"]),
                round(float(r["strike"]), 2),
                r["option_type"],
                _normalize_reason(r["signal_reason"]),
            )
            for r in live_rows
        ]

        diff = diff_signal_sets(replay_signals, live_keys)
        # Semantics:
        #   live ⊆ replay  → every live signal must have a precursor zero-cross.
        #     If a live signal has no zero-cross precursor, the live recorder is
        #     emitting on a different basis than the documented S1 logic; that
        #     is a real bug.
        #   replay ⊃ live  → expected. Live applies further filter gates
        #     (window, expiry buffer, IV cap, spot-MA, regime, etc.) that pure
        #     replay does not. The DIFF (replay − live) should be explained by
        #     the gate_attribution invariant (count of signal_blocked events).
        live_not_in_replay = diff["missing_from_replay"]  # live with no zero-cross
        replay_not_in_live = diff["missing_from_live"]    # zero-crosses live filtered
        subset_ok = len(live_not_in_replay) == 0
        detail = {
            "replay_signals": len(replay_signals),
            "live_signals": len(live_rows),
            "match_count": diff["match_count"],
            "live_without_zero_cross_precursor": live_not_in_replay[:50],
            "zero_crosses_live_filtered_out_count": len(replay_not_in_live),
            "zero_crosses_live_filtered_out_sample": replay_not_in_live[:25],
            "note": (
                "subset check: live ⊆ replay. Excess in replay is expected "
                "(live applies extra gates; cross-check via gate_attribution)."
            ),
        }
        if len(replay_signals) == 0 and len(live_rows) == 0:
            return InvariantResult(name="replay_parity", status="na", detail=detail)
        status = "pass" if subset_ok else "fail"
        return InvariantResult(name="replay_parity", status=status, detail=detail)

    async def _load_candles(self, underlying, expiry, strike, opt, ws, we) -> pd.DataFrame:
        sql = text(
            """
            SELECT time, close, expiry, strike, option_type
            FROM option_premium_candles
            WHERE underlying = :u AND expiry = :e AND strike = :s
              AND option_type = :o AND interval = '30minute'
              AND time >= :ws AND time < :we
            ORDER BY time
            """
        )
        rows = (
            await self.session.execute(
                sql,
                {"u": underlying, "e": expiry, "s": strike, "o": opt, "ws": ws, "we": we},
            )
        ).mappings().all()
        return pd.DataFrame([dict(r) for r in rows])

    # ── Invariant 3: gate attribution ───────────────────────────────────────
    async def _invariant_gate_attribution(self, ws: datetime, we: datetime) -> InvariantResult:
        # Requires `agent_audit_events` to log gate decisions. Lane is non-compliant
        # if more than 5% of candidates have no gate attribution.
        result = await _safe_execute(
            self.session,
            text(
                """
                SELECT
                    COALESCE(payload->>'gate', 'unknown') AS gate,
                    COUNT(*) AS n
                FROM agent_audit_events
                WHERE event_type = 'signal_blocked'
                  AND strategy_key = :sk
                  AND created_at >= :ws AND created_at < :we
                GROUP BY 1
                """
            ),
            {"sk": STRATEGY_KEY_FILTER, "ws": ws, "we": we},
        )
        if result is None:
            return InvariantResult(
                name="gate_attribution",
                status="fail",
                detail={"error": "query failed", "emitted": 0, "blocked_total": 0, "breakdown": {}},
            )
        rows = result.mappings().all()

        breakdown = {r["gate"]: int(r["n"]) for r in rows}
        blocked_total = sum(breakdown.values())
        unknown = breakdown.get("unknown", 0)
        emitted = (
            await self.session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM agent_signals
                    WHERE strategy_key = :sk
                      AND signal_bar_time >= :ws AND signal_bar_time < :we
                    """
                ),
                {"sk": STRATEGY_KEY_FILTER, "ws": ws, "we": we},
            )
        ).scalar() or 0

        # No evidence: lane has never logged a gate event. Cannot grant pass.
        if blocked_total == 0:
            return InvariantResult(
                name="gate_attribution",
                status="na",
                detail={
                    "emitted": int(emitted),
                    "blocked_total": 0,
                    "breakdown": {},
                    "note": "no signal_blocked events recorded — instrument the live recorder to log gate decisions",
                },
            )
        unknown_pct = unknown / blocked_total * 100.0
        status = "pass" if unknown_pct <= 5.0 else "fail"
        return InvariantResult(
            name="gate_attribution",
            status=status,
            detail={
                "emitted": int(emitted),
                "blocked_total": blocked_total,
                "breakdown": breakdown,
                "unknown_pct": round(unknown_pct, 2),
            },
        )

    # ── Invariant 4: backtest⇄live parity ───────────────────────────────────
    async def _invariant_backtest_parity(self, ws: datetime, we: datetime) -> InvariantResult:
        # Compare counts and reason distribution between backtest_trades and agent_signals
        # over the same window. Full byte-diff is too slow for the daily run.
        live_n = (
            await self.session.execute(
                text(
                    """
                    SELECT COUNT(*) FROM agent_signals
                    WHERE strategy_key = :sk
                      AND signal_bar_time >= :ws AND signal_bar_time < :we
                    """
                ),
                {"sk": STRATEGY_KEY_FILTER, "ws": ws, "we": we},
            )
        ).scalar() or 0
        # backtest_trades has no strategy_key column (see migration 002); the
        # closest filter we have today is the run_id naming convention. Until a
        # run_id↔lane mapping table exists, treat any trade in the window as a
        # candidate count and rely on the user to keep run scopes clean.
        bt_result = await _safe_execute(
            self.session,
            text(
                """
                SELECT COUNT(*) FROM backtest_trades
                WHERE entry_time >= :ws AND entry_time < :we
                """
            ),
            {"ws": ws, "we": we},
        )
        bt_n = (bt_result.scalar() if bt_result is not None else None) or None

        diff = {"live": int(live_n), "backtest": bt_n}
        # backtest_trades has no lane filter today, so this comparison is
        # tagged N/A until a run_id↔lane mapping table exists. Returning
        # `na` blocks green status — that's deliberate.
        return InvariantResult(
            name="backtest_parity",
            status="na",
            detail={
                "diff": diff,
                "note": "backtest_trades lacks strategy_key column; needs run_id↔lane mapping before comparison",
            },
        )

    # ── Invariant 5: trade reconciliation ───────────────────────────────────
    async def _invariant_trade_reconciliation(self, ws: datetime, we: datetime) -> InvariantResult:
        # Every paper trade entry/exit price must exist as a tick within ±2s and qty
        # must equal lot_size * configured_lots. This stub returns counts;
        # full tick-level recon is wired once the tick store path is finalized.
        tr_result = await _safe_execute(
            self.session,
            text(
                """
                SELECT id, symbol, qty, entry_price, current_price AS exit_price, entered_at, closed_at
                FROM agent_positions
                WHERE strategy_key = :sk
                  AND entered_at >= :ws AND entered_at < :we
                """
            ),
            {"sk": STRATEGY_KEY_FILTER, "ws": ws, "we": we},
        )
        if tr_result is None:
            return InvariantResult(name="trade_reconciliation", status="fail", detail={"error": "query failed"})
        trade_rows = tr_result.mappings().all()

        if len(trade_rows) == 0:
            return InvariantResult(
                name="trade_reconciliation",
                status="na",
                detail={"trades_booked": 0, "note": "no positions opened by this lane in the window"},
            )

        failures = []
        pass_count = 0
        for t in trade_rows:
            if t["entry_price"] is None or t["qty"] is None:
                failures.append({"id": str(t["id"]), "reason": "null entry/qty"})
                continue
            pass_count += 1

        status = "pass" if len(failures) == 0 else "fail"
        return InvariantResult(
            name="trade_reconciliation",
            status=status,
            detail={
                "trades_booked": len(trade_rows),
                "pass_count": pass_count,
                "failures": failures[:50],
            },
        )

    # ── Invariant 6: edge persistence ───────────────────────────────────────
    async def _invariant_edge_persistence(self, audit_date: date) -> InvariantResult:
        # Rolling 60-day expectancy vs 1-year baseline. Drift > 30% trips this gate.
        # We compute expectancy across two surfaces:
        #   1. closed positions in agent_positions  (status='closed')
        #   2. closed signals in agent_signals      (status='CLOSED' if that exists)
        # whichever yields a positive sample size.
        rows = await _safe_execute(
            self.session,
            text(
                """
                SELECT realized_pnl, entry_price, qty, closed_at
                FROM agent_positions
                WHERE strategy_key = :sk
                  AND status = 'closed'
                  AND closed_at >= :start
                """
            ),
            {"sk": STRATEGY_KEY_FILTER, "start": audit_date - timedelta(days=365)},
        )
        if rows is None:
            return InvariantResult(name="edge_persistence", status="fail", detail={"error": "query failed"})
        all_closed = rows.mappings().all()
        if len(all_closed) == 0:
            return InvariantResult(
                name="edge_persistence",
                status="na",
                detail={
                    "expectancy_60d": None,
                    "expectancy_baseline": None,
                    "drift_pct": None,
                    "note": "no closed positions in trailing 365d — cannot compute expectancy yet",
                },
            )

        def _ret_pct(r) -> float | None:
            if not r["entry_price"] or not r["qty"]:
                return None
            denom = float(r["entry_price"]) * float(r["qty"])
            if denom == 0:
                return None
            return float(r["realized_pnl"] or 0) / denom * 100.0

        cutoff60 = datetime.combine(audit_date - timedelta(days=60), time(0, 0))
        rets_60 = [v for r in all_closed if r["closed_at"] and r["closed_at"].replace(tzinfo=None) >= cutoff60 for v in [_ret_pct(r)] if v is not None]
        rets_365 = [v for r in all_closed for v in [_ret_pct(r)] if v is not None]
        exp60 = sum(rets_60) / len(rets_60) if rets_60 else None
        baseline = sum(rets_365) / len(rets_365) if rets_365 else None
        drift_pct: float | None = None
        if exp60 is not None and baseline is not None and baseline != 0:
            drift_pct = (exp60 - baseline) / abs(baseline) * 100.0

        if exp60 is None or baseline is None:
            return InvariantResult(
                name="edge_persistence",
                status="na",
                detail={
                    "expectancy_60d": exp60,
                    "expectancy_baseline": baseline,
                    "drift_pct": drift_pct,
                    "n_60d": len(rets_60),
                    "n_365d": len(rets_365),
                    "note": "insufficient closed-trade samples for 60d or 365d window",
                },
            )
        status = "pass" if exp60 > 0 and (drift_pct is None or drift_pct > -30.0) else "fail"
        return InvariantResult(
            name="edge_persistence",
            status=status,
            detail={
                "expectancy_60d": exp60,
                "expectancy_baseline": baseline,
                "drift_pct": drift_pct,
                "n_60d": len(rets_60),
                "n_365d": len(rets_365),
            },
        )


def _normalize_reason(reason: str | None) -> str:
    """Map signal_reason strings to canonical signal_type keys used by replay."""
    if not reason:
        return ""
    r = reason.upper()
    if "ZERO" in r and ("UP" in r or "BUY" in r):
        return "ZERO_CROSS_UP"
    if "ZERO" in r and ("DOWN" in r or "SELL" in r):
        return "ZERO_CROSS_DOWN"
    return r
