"""M8 -- Backtest & Validation Harness.

Event-driven replay of M6+M7's own real logic (not a re-implementation of
it) against historical 30-minute timing bars, using the SAME exit rule M9
enforces live (backtest/exit_simulator.py) so a backtest result and a live
paper result can never silently diverge in what "the strategy" means.
Read-only throughout: never writes to tickets/decisions/fills/outcomes (M9's
own tables) -- mixing backtest-simulated rows into those would let M9's
live fill loop mistake a historical replay for a real pending trade. Results
persist to their own `vanguard_backtest_runs` table (db/migrations/004)
instead -- named with the vanguard_ prefix because this shared Postgres
instance already has an unrelated `backtest_runs` table belonging to MACD
mini (see that migration's own header for how this was caught).

WHY THIS EVALUATES THE FULL FILTERED CANDIDATE POOL, NOT JUST EMITTED
TICKETS: conviction-decile monotonicity (the spec's own P1-era acceptance
criterion) needs outcome data across the WHOLE conviction range to mean
anything. Restricting to build_tickets()'s emitted=True rows would be
tautological -- current live data shows conviction never yet reaches
CONVICTION_MIN=85 (see the empirical finding this module's own report
surfaces), so an emitted-only check would have zero decile-9-and-below data
points by construction. This harness instead calls load_candidates_at()
directly and resolves+simulates an exit for EVERY filter-passing candidate,
regardless of whether it would have cleared the conviction/rank/M7 gates --
answering "would a LOWER bar also have worked?", which is what the check is
actually for. build_tickets() is still called per bar too, purely to record
which candidates the REAL pipeline would have emitted (would_emit), for the
separate, smaller hit-rate/expectancy stats that only make sense on trades
that would genuinely have been taken.

COST MODEL: TRANSACTION_COST_PER_LOT is a documented approximation (flat
discount-broker brokerage + STT + exchange + GST + SEBI + stamp duty for a
typical retail F&O options round trip), not a live fee schedule pull --
override it if precise numbers matter for a real capital decision. Reported
gross AND net so the difference is visible, never silently netted away.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest.exit_simulator import load_same_session_bars, r_multiple, walk_exit  # noqa: E402
from fusion.m6_select import build_tickets, load_candidates_at, resolve_instrument  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
INITIAL_CAPITAL = 1_000_000.0
TRANSACTION_COST_PER_LOT = 60.0
N_DECILES = 10
MIN_CANDIDATES_FOR_DECILE_CHECK = 30   # below this, deciles are noise -- report but flag it


def walk_forward_windows(start: date, end: date, train_days: int, test_days: int, step_days: int):
    """(train_start, train_end, test_start, test_end) tuples covering
    [start, end]. A generator, not a DB call -- pure calendar arithmetic
    the caller uses to slice replay() into successive train/test splits."""
    windows = []
    train_start = start
    while True:
        train_end = train_start + timedelta(days=train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        windows.append((train_start, train_end, test_start, test_end))
        train_start = train_start + timedelta(days=step_days)
    return windows


def _simulate_candidate_exit(connection, candidate, ts):
    instrument = resolve_instrument(connection, candidate.symbol, candidate.direction, ts)
    if instrument is None:
        return None, None
    entry = instrument["premium"]
    with connection.cursor() as cursor:
        bars = load_same_session_bars(
            cursor, candidate.symbol, instrument["strike"], instrument["option_type"],
            instrument["expiry"], ts, ts + timedelta(hours=8),
        )
    result = walk_exit(entry, ts, bars)
    if result is None:
        return instrument, None
    return instrument, {
        "entry": entry, "exit_price": result.exit_price, "exit_reason": result.exit_reason,
        "holding_bars": result.holding_bars,
        # The SHARED R definition — see exit_simulator.r_multiple for why this
        # is not `entry * STOP_PCT` any more.
        "r_multiple": r_multiple(entry, result.exit_price),
        "exit_ts": result.exit_ts,
        "lot_size": instrument["lot_size"],
    }


def replay(connection, start_ts: datetime, end_ts: datetime, capital: float = INITIAL_CAPITAL) -> dict:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT ts FROM timing WHERE ts >= %(start)s AND ts <= %(end)s ORDER BY ts",
            {"start": start_ts, "end": end_ts},
        )
        bar_timestamps = [row[0] for row in cursor.fetchall()]

    all_candidates = []
    emitted_trades = []

    for ts in bar_timestamps:
        candidates = load_candidates_at(connection, ts)
        ticket_rows = build_tickets(connection, ts, capital)
        emitted_symbols = {r["symbol"] for r in ticket_rows if r["emitted"]}

        for candidate in candidates:
            instrument, exit_info = _simulate_candidate_exit(connection, candidate, ts)
            all_candidates.append({
                "ts": ts, "symbol": candidate.symbol, "direction": candidate.direction,
                "conviction": candidate.conviction, "would_emit": candidate.symbol in emitted_symbols,
                "resolved": instrument is not None, "exit": exit_info,
            })
        for row in ticket_rows:
            if row["emitted"]:
                emitted_trades.append(row)

    return {"bars_evaluated": len(bar_timestamps), "all_candidates": all_candidates,
            "emitted_trades": emitted_trades}


def bucketize_by_conviction(candidates_with_exit, n_buckets):
    """Sort ascending by conviction, split into n_buckets near-equal groups
    (early buckets absorb the remainder so no bucket is empty before the
    data runs out). Returns a list of (bucket_index, items) low-to-high."""
    ordered = sorted(candidates_with_exit, key=lambda c: c["conviction"])
    n = len(ordered)
    if n == 0:
        return []
    base, remainder = divmod(n, n_buckets)
    buckets = []
    i = 0
    for b in range(n_buckets):
        size = base + (1 if b < remainder else 0)
        if size == 0:
            continue
        buckets.append((b, ordered[i:i + size]))
        i += size
    return buckets


def compute_metrics(replay_result: dict) -> dict:
    all_candidates = replay_result["all_candidates"]
    emitted_trades = replay_result["emitted_trades"]

    resolved = [c for c in all_candidates if c["exit"] is not None]
    unresolved_count = len(all_candidates) - len(resolved)

    decile_report = []
    if resolved:
        n_buckets = min(N_DECILES, len(resolved))
        for b, items in bucketize_by_conviction(resolved, n_buckets):
            r_values = [c["exit"]["r_multiple"] for c in items]
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

    # Two DIFFERENT reasons an emitted ticket contributes no P&L, counted
    # separately rather than both vanishing into a bare `continue`. The
    # variable this loop used to iterate was named `closed_emitted` while
    # applying no filter at all -- a name that asserted a selection that never
    # happened, and no counter recorded what the loop then silently dropped.
    unresolved_emitted = 0
    unsized_emitted = 0
    priced = []  # (exit_ts, gross, net, r) — kept together so P&L can be time-ordered
    for t in emitted_trades:
        exit_match = next((c["exit"] for c in all_candidates
                           if c["symbol"] == t["symbol"] and c["ts"] == t["ts"] and c["exit"]), None)
        if exit_match is None:
            unresolved_emitted += 1
            continue
        if t.get("sizing_lots") is None:
            unsized_emitted += 1
            continue
        lots = t["sizing_lots"]
        gross = (exit_match["exit_price"] - exit_match["entry"]) * lots * exit_match["lot_size"]
        net = gross - lots * TRANSACTION_COST_PER_LOT
        priced.append((exit_match.get("exit_ts"), gross, net, exit_match["r_multiple"]))

    gross_pnls = [p[1] for p in priced]
    net_pnls = [p[2] for p in priced]
    r_multiples = [p[3] for p in priced if p[3] is not None]

    # Drawdown is a property of the EQUITY PATH, so the P&L sequence has to be
    # ordered by when each trade CLOSED. Ordering by entry bar (the order
    # `emitted_trades` happens to be built in) understates the trough whenever
    # a later-entered trade closes first.
    by_exit = [p[2] for p in sorted(priced, key=lambda p: (p[0] is None, p[0]))]

    hit_rate = round(sum(1 for r in r_multiples if r > 0) / len(r_multiples), 3) if r_multiples else None

    return {
        "candidates_evaluated": len(all_candidates),
        "candidates_unresolved_no_contract_or_no_exit": unresolved_count,
        "emitted_trades_closed": len(gross_pnls),
        "emitted_trades_unresolved": unresolved_emitted,
        "emitted_trades_without_sizing": unsized_emitted,
        "hit_rate": hit_rate,
        "avg_r": round(sum(r_multiples) / len(r_multiples), 4) if r_multiples else None,
        "expectancy_gross_rupees": round(sum(gross_pnls) / len(gross_pnls), 2) if gross_pnls else None,
        "expectancy_net_rupees": round(sum(net_pnls) / len(net_pnls), 2) if net_pnls else None,
        "max_drawdown_rupees": _max_drawdown(by_exit),
        "conviction_decile_report": decile_report,
        "conviction_decile_monotonic": monotonic,
        "decile_check_sample_size_adequate": len(resolved) >= MIN_CANDIDATES_FOR_DECILE_CHECK,
    }


def _max_drawdown(sequential_pnls: list[float]) -> float | None:
    if not sequential_pnls:
        return None
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in sequential_pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return round(max_dd, 2)


def persist_run(connection, start_ts, end_ts, metrics: dict) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO vanguard_backtest_runs (start_ts, end_ts, report)
               VALUES (%s, %s, %s) RETURNING id""",
            (start_ts, end_ts, psycopg2.extras.Json(metrics)),
        )
        (run_id,) = cursor.fetchone()
    return run_id


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=datetime.fromisoformat, required=True)
    parser.add_argument("--end", type=datetime.fromisoformat, required=True)
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    connection.autocommit = True
    try:
        result = replay(connection, args.start, args.end, args.capital)
        metrics = compute_metrics(result)
        print(f"bars evaluated: {result['bars_evaluated']}")
        for k, v in metrics.items():
            print(f"  {k}: {v}")
        if not metrics["decile_check_sample_size_adequate"]:
            print(f"  ** sample too small (<{MIN_CANDIDATES_FOR_DECILE_CHECK} resolved candidates) "
                  f"for the decile-monotonicity check to be meaningful -- reported anyway, not a verdict **")
        if args.write:
            run_id = persist_run(connection, args.start, args.end, metrics)
            print(f"wrote vanguard_backtest_runs id={run_id}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
