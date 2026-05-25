"""CLI entry point — `python -m audits.lane_audit --lane s1 --days 30`.

Runs the chosen lane's auditor, persists the result to `lane_audit`, and
emits a markdown report under audits/reports/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text

from audits.base import AuditResult
from audits.lanes import REGISTRY
from audits.report import render_markdown
from db.database import AsyncSessionLocal


REPORTS_DIR = Path(__file__).parent / "reports"


async def run_one(lane: str, audit_date: date, lookback_days: int) -> AuditResult:
    if lane not in REGISTRY:
        raise SystemExit(f"unknown lane: {lane} (known: {list(REGISTRY)})")
    auditor_cls = REGISTRY[lane]
    async with AsyncSessionLocal() as session:
        # Begin an explicit tx so per-invariant SAVEPOINTs (begin_nested) work.
        async with session.begin():
            auditor = auditor_cls(session=session)
            result = await auditor.run(audit_date=audit_date, lookback_days=lookback_days)
            await _persist(session, result)
    return result


async def _persist(session, result: AuditResult) -> None:
    row = result.to_db_row()
    # asyncpg + jsonb: bind the JSON text and CAST in SQL. (Using CAST(:name AS jsonb) in
    # text() trips asyncpg's positional-param rewrite.)
    for k in (
        "data_gaps",
        "replay_mismatches",
        "gate_block_breakdown",
        "backtest_live_diff",
        "trade_recon_failures",
        "metadata",
    ):
        row[k] = json.dumps(row[k], default=str)
    await session.execute(
        text(
            """
            INSERT INTO lane_audit (
                lane, audit_date, window_start, window_end,
                data_gaps, freshness_violations, data_integrity_pass,
                replay_signals, live_signals, replay_match_count, replay_mismatches, replay_parity_pass,
                signals_emitted, signals_blocked_total, gate_block_breakdown, gate_attribution_pass,
                backtest_live_diff, backtest_parity_pass,
                trades_booked, trade_recon_pass_count, trade_recon_failures, trade_recon_pass,
                expectancy_60d, expectancy_baseline, drift_pct, edge_persistence_pass,
                overall_status, report_path, metadata
            ) VALUES (
                :lane, :audit_date, :window_start, :window_end,
                CAST(:data_gaps AS jsonb), :freshness_violations, :data_integrity_pass,
                :replay_signals, :live_signals, :replay_match_count, CAST(:replay_mismatches AS jsonb), :replay_parity_pass,
                :signals_emitted, :signals_blocked_total, CAST(:gate_block_breakdown AS jsonb), :gate_attribution_pass,
                CAST(:backtest_live_diff AS jsonb), :backtest_parity_pass,
                :trades_booked, :trade_recon_pass_count, CAST(:trade_recon_failures AS jsonb), :trade_recon_pass,
                :expectancy_60d, :expectancy_baseline, :drift_pct, :edge_persistence_pass,
                :overall_status, :report_path, CAST(:metadata AS jsonb)
            )
            ON CONFLICT (lane, audit_date) DO UPDATE SET
                window_start = EXCLUDED.window_start,
                window_end = EXCLUDED.window_end,
                data_gaps = EXCLUDED.data_gaps,
                freshness_violations = EXCLUDED.freshness_violations,
                data_integrity_pass = EXCLUDED.data_integrity_pass,
                replay_signals = EXCLUDED.replay_signals,
                live_signals = EXCLUDED.live_signals,
                replay_match_count = EXCLUDED.replay_match_count,
                replay_mismatches = EXCLUDED.replay_mismatches,
                replay_parity_pass = EXCLUDED.replay_parity_pass,
                signals_emitted = EXCLUDED.signals_emitted,
                signals_blocked_total = EXCLUDED.signals_blocked_total,
                gate_block_breakdown = EXCLUDED.gate_block_breakdown,
                gate_attribution_pass = EXCLUDED.gate_attribution_pass,
                backtest_live_diff = EXCLUDED.backtest_live_diff,
                backtest_parity_pass = EXCLUDED.backtest_parity_pass,
                trades_booked = EXCLUDED.trades_booked,
                trade_recon_pass_count = EXCLUDED.trade_recon_pass_count,
                trade_recon_failures = EXCLUDED.trade_recon_failures,
                trade_recon_pass = EXCLUDED.trade_recon_pass,
                expectancy_60d = EXCLUDED.expectancy_60d,
                expectancy_baseline = EXCLUDED.expectancy_baseline,
                drift_pct = EXCLUDED.drift_pct,
                edge_persistence_pass = EXCLUDED.edge_persistence_pass,
                overall_status = EXCLUDED.overall_status,
                report_path = EXCLUDED.report_path,
                metadata = EXCLUDED.metadata
            """
        ),
        {**row, "report_path": _report_path(result.lane, result.audit_date)},
    )


def _report_path(lane: str, audit_date: date) -> str:
    return f"audits/reports/{lane}_{audit_date.isoformat()}.md"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--lane", required=True, help=f"one of {list(REGISTRY)}")
    p.add_argument("--days", type=int, default=30, help="lookback window in days")
    p.add_argument("--date", default=date.today().isoformat(), help="audit-as-of date (YYYY-MM-DD)")
    args = p.parse_args()
    audit_date = date.fromisoformat(args.date)

    result = asyncio.run(run_one(args.lane, audit_date, args.days))
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"{result.lane}_{result.audit_date.isoformat()}.md"
    out.write_text(render_markdown(result))
    print(f"audit complete: {result.overall_status.upper()} — {out}")


if __name__ == "__main__":
    main()
