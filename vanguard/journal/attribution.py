"""M10 -- Journal & Attribution.

Nightly rollup over M9's own real, closed paper-trading history (never a
backtest replay -- that lives separately in vanguard_backtest_runs, see
backtest/harness.py's own header for why the two must never mix). Reports
CUMULATIVE performance as of a given date (every closed outcome up to and
including that day), not just that single day's closes -- an attribution
report is a running track record, not a daily snapshot, and each nightly
run captures where that track record stood at the time.

PER-COMPONENT IC (information coefficient): correlates each of M6's five
raw component scores (flow/sector_rs/timing/regime/leadlag, pulled straight
from tickets.evidence.component_scores -- the exact numbers M6 actually
fused, not re-derived) against the eventual r_multiple outcome. This is the
signal-decay/attribution the spec calls for: which components are actually
predictive as real history accumulates, vs which are noise. Requires
MIN_IC_SAMPLE closed trades per component before reporting a correlation at
all -- doctrine says NULL over a fabricated/meaningless number from too few
points, not a coefficient dressed up as a finding.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

import numpy as np
import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest.harness import bucketize_by_conviction  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
COMPONENTS = ("flow", "sector_rs", "timing", "regime", "leadlag")
MIN_IC_SAMPLE = 20
MIN_DECILE_SAMPLE = 30


def load_closed_outcomes(connection, as_of_date: date) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT t.id, t.symbol, t.direction, t.conviction, t.evidence,
                      o.exit_reason, o.pnl_rupees, o.r_multiple, o.exit_ts
               FROM outcomes o
               JOIN tickets t ON t.id = o.ticket_id
               WHERE o.closed AND o.exit_ts::date <= %(as_of)s
               ORDER BY o.exit_ts ASC""",
            {"as_of": as_of_date},
        )
        rows = cursor.fetchall()

    return [
        {
            "ticket_id": r[0], "symbol": r[1], "direction": r[2], "conviction": float(r[3]),
            "evidence": r[4], "exit_reason": r[5],
            "pnl_rupees": float(r[6]) if r[6] is not None else None,
            "r_multiple": float(r[7]) if r[7] is not None else None,
            "exit_ts": r[8],
        }
        for r in rows
    ]


def compute_hit_rate_avg_r(rows: list[dict]) -> tuple[float | None, float | None]:
    r_values = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    if not r_values:
        return None, None
    hit_rate = sum(1 for r in r_values if r > 0) / len(r_values)
    avg_r = sum(r_values) / len(r_values)
    return round(hit_rate, 4), round(avg_r, 4)


def compute_decile_report(rows: list[dict]) -> tuple[list[dict], bool | None, bool]:
    eligible = [r for r in rows if r["r_multiple"] is not None]
    sample_adequate = len(eligible) >= MIN_DECILE_SAMPLE
    if not eligible:
        return [], None, sample_adequate

    n_buckets = min(10, len(eligible))
    decile_report = []
    for b, items in bucketize_by_conviction(eligible, n_buckets):
        r_values = [it["r_multiple"] for it in items]
        wins = sum(1 for r in r_values if r > 0)
        decile_report.append({
            "bucket": b, "n": len(items),
            "conviction_range": (round(items[0]["conviction"], 1), round(items[-1]["conviction"], 1)),
            "win_rate": round(wins / len(items), 3),
            "avg_r": round(sum(r_values) / len(r_values), 4),
        })
    monotonic = None
    if len(decile_report) >= 2:
        avg_rs = [d["avg_r"] for d in decile_report]
        monotonic = all(avg_rs[i] <= avg_rs[i + 1] for i in range(len(avg_rs) - 1))
    return decile_report, monotonic, sample_adequate


def compute_component_ic(rows: list[dict]) -> dict:
    """Pearson correlation between each raw component score (as fused by M6
    at ticket time) and the eventual r_multiple. NULL, not a fabricated
    coefficient, whenever a component has fewer than MIN_IC_SAMPLE paired
    observations -- a correlation from a handful of points is noise
    dressed up as a finding."""
    report = {}
    for component in COMPONENTS:
        pairs = []
        for r in rows:
            if r["r_multiple"] is None:
                continue
            scores = (r["evidence"] or {}).get("component_scores") or {}
            value = scores.get(component)
            if value is not None:
                pairs.append((float(value), r["r_multiple"]))
        if len(pairs) < MIN_IC_SAMPLE:
            report[component] = {"ic": None, "n": len(pairs),
                                 "reason": f"n={len(pairs)} < MIN_IC_SAMPLE={MIN_IC_SAMPLE}"}
            continue
        xs = np.array([p[0] for p in pairs])
        ys = np.array([p[1] for p in pairs])
        if np.std(xs) == 0 or np.std(ys) == 0:
            report[component] = {"ic": None, "n": len(pairs), "reason": "zero variance in sample"}
            continue
        ic = float(np.corrcoef(xs, ys)[0, 1])
        report[component] = {"ic": round(ic, 4), "n": len(pairs), "reason": None}
    return report


def per_symbol_sector_attribution(rows: list[dict]) -> dict:
    by_symbol = {}
    for r in rows:
        if r["pnl_rupees"] is None:
            continue
        entry = by_symbol.setdefault(r["symbol"], {"n": 0, "total_pnl_rupees": 0.0})
        entry["n"] += 1
        entry["total_pnl_rupees"] += r["pnl_rupees"]
    for entry in by_symbol.values():
        entry["total_pnl_rupees"] = round(entry["total_pnl_rupees"], 2)
    return by_symbol


def run_attribution(connection, as_of_date: date) -> dict:
    rows = load_closed_outcomes(connection, as_of_date)
    hit_rate, avg_r = compute_hit_rate_avg_r(rows)
    decile_report, decile_monotonic, decile_sample_adequate = compute_decile_report(rows)
    component_ic = compute_component_ic(rows)
    symbol_attribution = per_symbol_sector_attribution(rows)

    return {
        "as_of_date": as_of_date, "n_tickets_closed": len(rows),
        "hit_rate": hit_rate, "avg_r": avg_r,
        "conviction_decile_monotonic": decile_monotonic,
        "report": {
            "conviction_decile_report": decile_report,
            "decile_sample_size_adequate": decile_sample_adequate,
            "component_ic": component_ic,
            "per_symbol_attribution": symbol_attribution,
        },
    }


def persist_run(connection, result: dict) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO attribution_runs
               (as_of_date, n_tickets_closed, hit_rate, avg_r, conviction_decile_monotonic, report)
               VALUES (%(as_of_date)s, %(n_tickets_closed)s, %(hit_rate)s, %(avg_r)s,
                       %(conviction_decile_monotonic)s, %(report)s)
               RETURNING id""",
            {**result, "report": psycopg2.extras.Json(result["report"])},
        )
        (run_id,) = cursor.fetchone()
    return run_id


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="ISO date to roll up through; default = today")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    connection.autocommit = True
    try:
        as_of_date = args.as_of or datetime.now().date()
        result = run_attribution(connection, as_of_date)
        print(f"M10 attribution as of {as_of_date.isoformat()}")
        print(f"  n_tickets_closed: {result['n_tickets_closed']}")
        print(f"  hit_rate: {result['hit_rate']}")
        print(f"  avg_r: {result['avg_r']}")
        print(f"  conviction_decile_monotonic: {result['conviction_decile_monotonic']}")
        print(f"  component_ic: {result['report']['component_ic']}")
        print(f"  per_symbol_attribution: {result['report']['per_symbol_attribution']}")
        if args.write:
            run_id = persist_run(connection, result)
            print(f"wrote attribution_runs id={run_id}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
