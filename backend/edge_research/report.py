"""Descriptive net results and session-cluster uncertainty, never auto-promotion."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import random
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from edge_research.engine import IST, ts


def summarize(trades: list[dict], observed_sessions: int, cfg: dict) -> dict:
    pnls = [t["net_pnl"] for t in trades]
    stress = [t["stressed_net_pnl"] for t in trades]
    n = len(pnls)
    wins = sum(p > 0 for p in pnls)
    profit = sum(p for p in pnls if p > 0)
    loss = -sum(p for p in pnls if p < 0)
    daily = defaultdict(list)
    for t in trades:
        daily[t["session"]].append(t["net_pnl"])
    clustered = [(sum(p), len(p)) for p in daily.values()]
    ci = None
    if len(clustered) >= 2:
        rng = random.Random(20261020)
        samples = []
        for _ in range(2000):
            chosen = rng.choices(clustered, k=len(clustered))
            samples.append(sum(p for p, _ in chosen) / sum(c for _, c in chosen))
        samples.sort()
        ci = [round(samples[49], 2), round(samples[1949], 2)]
    running = peak = max_dd = 0.0
    for p in pnls:
        running += p
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    enough = (
        n >= cfg["minimum_trades_for_evidence"]
        and len(daily) >= cfg["minimum_sessions_for_evidence"]
    )
    return {
        "closed_trades": n,
        "observed_sessions": observed_sessions,
        "sessions_with_closed_trades": len(daily),
        "win_rate": wins / n if n else None,
        "net_pnl": round(sum(pnls), 2) if n else None,
        "net_expectancy": round(sum(pnls) / n, 2) if n else None,
        "profit_factor": (
            round(profit / loss, 4) if loss else "infinity" if profit else None
        ),
        "avg_win": profit / wins if wins else None,
        "avg_loss": -loss / sum(p < 0 for p in pnls) if loss else None,
        "stressed_net_pnl": round(sum(stress), 2) if n else None,
        "closed_trade_max_drawdown": round(max_dd, 2) if n else None,
        "expectancy_session_bootstrap_95pct": ci,
        "best_session_share_of_positive_pnl": (
            max((sum(v) for v in daily.values()), default=0) / profit
            if profit
            else None
        ),
        "worst_leave_one_session_out_net": (
            round(min((sum(pnls) - sum(v) for v in daily.values()), default=0), 2)
            if daily
            else None
        ),
        "evidence_status": (
            "insufficient_sample"
            if not enough
            else "requires_untouched_holdout_and_multiple_trial_review"
        ),
        "positive_lower_interval": bool(ci and ci[0] > 0) if enough else None,
        "promotion": "not_authorized",
    }


def read_report(path: Path, *, unseal_holdout: bool = False) -> dict:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        manifest = json.loads(
            db.execute("SELECT payload FROM manifest WHERE id=1").fetchone()[0]
        )
        trades = [
            json.loads(r[0])
            for r in db.execute("SELECT payload FROM trades ORDER BY id")
        ]
        decisions = [
            json.loads(r[0]) for r in db.execute("SELECT payload FROM decisions")
        ]
        states = [json.loads(r[0]) for r in db.execute("SELECT payload FROM states")]
        groups = {}
        for lane, spec in manifest["config"]["lanes"].items():
            for variant in ("control", "challenger"):
                subset = [
                    t for t in trades if t["lane"] == lane and t["variant"] == variant
                ]
                d = [d for d in decisions if d["key"].startswith(f"{lane}:{variant}:")]
                # A valid observed session needs a fully evaluated setup, not
                # merely a closed-market poll or a source-error heartbeat.
                days = sorted(
                    {
                        ts(x["available_at"]).astimezone(IST).date().isoformat()
                        for x in d
                        if x.get("features")
                    }
                )
                protocol = manifest["config"]["evaluation"]
                dev_days = days[: protocol["development_sessions"]]
                test_days = days[
                    protocol["development_sessions"] : protocol["development_sessions"]
                    + protocol["holdout_sessions"]
                ]

                def inside(t, window):
                    return (
                        t["session"] in window
                        and ts(t["exit_at"]).astimezone(IST).date().isoformat()
                        in window
                    )

                dev = [t for t in subset if inside(t, dev_days)]
                holdout = {
                    "status": "sealed",
                    "observed_sessions": len(test_days),
                    "required_sessions": protocol["holdout_sessions"],
                }
                if unseal_holdout:
                    complete = (
                        len(test_days) == protocol["holdout_sessions"]
                        and test_days[-1]
                        < datetime.now(timezone.utc).astimezone(IST).date().isoformat()
                    )
                    if complete:
                        holdout = {
                            "status": "unsealed",
                            **summarize(
                                [t for t in subset if inside(t, test_days)],
                                len(test_days),
                                manifest["config"],
                            ),
                        }
                    else:
                        holdout["status"] = "incomplete_results_withheld"
                step = protocol["diagnostic_block_sessions"]
                folds = []
                for start in range(0, len(dev_days), step):
                    window = dev_days[start : start + step]
                    folds.append(
                        {
                            "sessions": window,
                            "complete": len(window) == step,
                            **summarize(
                                [t for t in dev if inside(t, window)],
                                len(window),
                                manifest["config"],
                            ),
                        }
                    )
                groups[f"{lane}:{variant}"] = {
                    **summarize(dev, len(days), manifest["config"]),
                    "performance_scope": "development_only",
                    "development_sessions": dev_days,
                    "diagnostic_folds": folds,
                    "holdout": holdout,
                    "by_symbol": {
                        symbol: summarize(
                            [t for t in dev if t["symbol"] == symbol],
                            len(dev_days),
                            manifest["config"],
                        )
                        for symbol, market in manifest["config"]["symbols"].items()
                        if market in spec["markets"]
                    },
                    "decision_reasons": dict(
                        Counter(
                            x["reason"]
                            for x in d
                            if ts(x["available_at"]).astimezone(IST).date().isoformat()
                            not in test_days
                        )
                    ),
                    "open_or_pending": sum(
                        s["lane"] == lane and s["variant"] == variant for s in states
                    ),
                }
        return {
            "manifest": manifest,
            "books": groups,
            "integrity": db.execute("PRAGMA quick_check(1)").fetchone()[0],
            "limitations": [
                "Prospective one-lot quote simulation, not actual fills or production performance.",
                "Stops and targets use completed 5-minute closes; intrabar protection is not simulated.",
                "Auction v1 tests failed rolling-range auctions, not archived aggressor flow or the production MP agent.",
                "No automatic promotion. Session bootstrap is descriptive and does not correct for strategy-selection trials.",
                "Different contract prices and lot sizes make raw rupee comparisons across markets inappropriate.",
                "Greeks have receipt-time availability; exchange timestamps may be unavailable.",
                "Closed-trade drawdown excludes intratrade excursions and stale open marks.",
            ],
        }
    finally:
        db.close()
