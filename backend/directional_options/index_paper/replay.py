"""Bar-by-bar historical replay of the index directional swing lane.

Three uses:

  1. It exercises the whole lifecycle — entry, mark, stop, target, max-hold,
     expiry settlement, attribution — against real bars, so those paths are
     proven rather than merely written.

  2. It measures the COST FLOOR: what the lane loses per trade purely to
     spread, slippage, charges and decay. Every candidate signal has to beat
     that floor before it is interesting, and a strategy evaluated without
     knowing its own floor is being graded against zero.

  3. It fills the FACTOR JOURNAL, which is the only route by which a factor
     currently carrying a small shadow weight can ever earn a real one.

Direction is no longer injected. It comes from the factor panel, which is the
whole point of the rebuild: the lane decides from everything it can see, and
the record shows what each input contributed.

    python -m directional_options.index_paper.replay --bars 60 --reset
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.index_paper import book, journal
from directional_options.index_paper.engine import (
    IndexPaperConfig,
    PassResult,
    run_underlying,
)
from directional_options.index_paper.store import ensure_tables
from directional_options.vol.builder import usable_bars
from directional_options.vol.realized import load_daily_series
from directional_options.vol.surface import INDEX_UNIVERSE

@dataclass
class ReplayReport:
    bars: int = 0
    passes: int = 0
    entries: int = 0
    exits: int = 0
    marks: int = 0
    errors: int = 0
    per_underlying: dict[str, dict[str, int]] = field(default_factory=dict)

    def absorb(self, result: PassResult) -> None:
        self.passes += 1
        self.entries += result.entries
        self.exits += result.exits
        self.marks += result.marks
        if result.status == "error":
            self.errors += 1
        bucket = self.per_underlying.setdefault(
            result.underlying, {"entries": 0, "exits": 0, "marks": 0, "errors": 0}
        )
        bucket["entries"] += result.entries
        bucket["exits"] += result.exits
        bucket["marks"] += result.marks
        bucket["errors"] += int(result.status == "error")

    def as_dict(self) -> dict[str, Any]:
        return {
            "bars": self.bars,
            "passes": self.passes,
            "entries": self.entries,
            "exits": self.exits,
            "marks": self.marks,
            "errors": self.errors,
            "per_underlying": self.per_underlying,
        }


async def replay(
    *,
    underlyings: Sequence[str] = INDEX_UNIVERSE,
    bars: int = 40,
    lookback_days: int = 45,
    config: IndexPaperConfig | None = None,
    reset: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    cfg = config or IndexPaperConfig()
    await ensure_tables()
    # Every decision this replay writes is stamped, so its own tallies scope to
    # this run.  The decision journal is append-only and survives a book reset;
    # an unstamped tally silently blends this replay with every earlier one.
    run_id = run_id or f"replay-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"

    if reset:
        await book.reset(actor="replay", confirm_token="RESET-INDEX-PAPER-BOOK")

    # Bars are replayed in strict chronological order ACROSS underlyings, so
    # the shared exposure cap behaves the way it would live rather than letting
    # whichever index happens to be iterated first take every slot.
    timeline: list[tuple[datetime, str]] = []
    series_cache: dict[str, Any] = {}
    for symbol in underlyings:
        for ts in await usable_bars(symbol, limit=bars, lookback_days=lookback_days):
            timeline.append((ts, symbol))
        # Loaded once per symbol; run_underlying slices it to the sessions that
        # closed before each bar, so caching cannot leak future bars into a
        # factor.
        series_cache[symbol] = await load_daily_series(symbol)
    timeline.sort()

    report = ReplayReport(bars=len({ts for ts, _ in timeline}))
    for ts, symbol in timeline:
        result = await run_underlying(
            symbol, config=cfg, bar_ts=ts, run_id=run_id, daily_series=series_cache.get(symbol)
        )
        report.absorb(result)

    summary = await book.summary(cfg.initial_capital)
    lower = min((ts for ts, _ in timeline), default=datetime.now(timezone.utc))
    upper = max((ts for ts, _ in timeline), default=datetime.now(timezone.utc))
    # The settlement path stamps its decision at the EXPIRY timestamp, which can
    # sit outside the replayed bar window, so the tally window is widened to
    # catch it rather than silently dropping those rows.
    tally_lower = lower - timedelta(days=45)
    tally_upper = upper + timedelta(days=45)
    return {
        "run_id": run_id,
        "replay": report.as_dict(),
        "book": summary,
        "rejections": await journal.rejection_tally(tally_lower, tally_upper, run_id),
        "gates": await journal.gate_tally(tally_lower, tally_upper, run_id),
        "cost_floor": await cost_floor(),
        "factor_ic": await journal.factor_ic(min_n=20),
        "factor_coverage": await factor_coverage(run_id),
        "window": {"from": lower.isoformat(), "to": upper.isoformat()},
    }


_COST_FLOOR_SQL = text(
    """
    SELECT count(*)                                   AS trades,
           coalesce(avg(realized_pnl), 0)             AS avg_realized,
           coalesce(sum(realized_pnl), 0)             AS total_realized,
           coalesce(avg(entry_cost + coalesce(exit_cost, 0)), 0) AS avg_round_trip_cost,
           coalesce(sum(entry_cost + coalesce(exit_cost, 0)), 0) AS total_costs,
           coalesce(avg((exit_premium - entry_premium) * quantity), 0) AS avg_gross
    FROM index_paper_positions
    WHERE status = 'closed'
    """
)


async def cost_floor() -> dict[str, Any]:
    """Average round-trip cost per closed trade — the bar a signal must clear."""
    async with AsyncSessionLocal() as session:
        row = (await session.execute(_COST_FLOOR_SQL)).mappings().first()
    data = dict(row) if row else {}
    trades = int(data.get("trades") or 0)
    return {
        "closed_trades": trades,
        "avg_gross_pnl": float(data.get("avg_gross") or 0.0),
        "avg_round_trip_cost": float(data.get("avg_round_trip_cost") or 0.0),
        "avg_realized_pnl": float(data.get("avg_realized") or 0.0),
        "total_costs": float(data.get("total_costs") or 0.0),
        "total_realized": float(data.get("total_realized") or 0.0),
    }


# Joined to the book on purpose.  The attribution table is append-only history
# and survives a book reset, so an unjoined rollup silently blends the current
# run with every previous one.
_ATTRIBUTION_ROLLUP = text(
    """
    SELECT count(*) AS n,
           coalesce(avg(a.delta_pnl), 0)    AS delta_pnl,
           coalesce(avg(a.gamma_pnl), 0)    AS gamma_pnl,
           coalesce(avg(a.vega_pnl), 0)     AS vega_pnl,
           coalesce(avg(a.theta_pnl), 0)    AS theta_pnl,
           coalesce(avg(a.residual_pnl), 0) AS residual_pnl,
           coalesce(avg(a.costs), 0)        AS costs,
           coalesce(avg(a.net_pnl), 0)      AS net_pnl,
           coalesce(sum(abs(a.residual_pnl)), 0) AS abs_residual,
           coalesce(sum(abs(a.gross_pnl)), 0)    AS abs_gross
    FROM index_paper_attribution a
    JOIN index_paper_positions p ON p.position_id = a.position_id
    """
)


async def attribution_rollup() -> dict[str, Any]:
    """Which greek actually paid, over the trades currently in the book.

    `explained_fraction` is computed on SUMMED ABSOLUTE terms rather than as a
    mean of per-trade ratios.  A trade whose gross P&L lands near zero produces
    an unbounded ratio, and averaging those made the aggregate meaningless.
    """
    async with AsyncSessionLocal() as session:
        row = (await session.execute(_ATTRIBUTION_ROLLUP)).mappings().first()
    data = {k: (float(v) if k != "n" else int(v)) for k, v in dict(row or {}).items()}
    abs_gross = data.pop("abs_gross", 0.0)
    abs_residual = data.pop("abs_residual", 0.0)
    data["explained_fraction"] = (
        1.0 - abs_residual / abs_gross if abs_gross > 1e-9 else None
    )
    return data


_FACTOR_COVERAGE = text(
    """
    SELECT factor, role, confidence,
           count(*) AS decisions,
           count(*) FILTER (WHERE acting) AS acting,
           count(*) FILTER (WHERE status <> 'ok') AS unavailable,
           avg(weight) AS avg_weight
    FROM index_paper_factors
    WHERE (CAST(:run_id AS text) IS NULL OR run_id = CAST(:run_id AS text))
    GROUP BY factor, role, confidence
    ORDER BY acting DESC, factor
    """
)


async def factor_coverage(run_id: str | None) -> list[dict[str, Any]]:
    """How often each factor was actually available to act.

    A factor that is unavailable on most decisions is not contributing,
    whatever its weight says — and that is invisible unless it is counted.
    """
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(_FACTOR_COVERAGE, {"run_id": run_id})).mappings().all()
    return [dict(r) for r in rows]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay the index paper lane over historical bars.")
    parser.add_argument("--underlying", action="append")
    parser.add_argument("--bars", type=int, default=40)
    parser.add_argument("--lookback-days", type=int, default=45)
    parser.add_argument("--reset", action="store_true", help="Archive and clear the book first.")
    parser.add_argument("--spread-multiplier", type=float, default=1.0)
    parser.add_argument("--horizon-sessions", type=int, default=3)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    from dataclasses import replace as dc_replace

    args = _parse_args(argv)
    cfg = IndexPaperConfig(horizon_sessions=args.horizon_sessions)
    if args.spread_multiplier != 1.0:
        cfg.cost_model = dc_replace(cfg.cost_model, spread_multiplier=args.spread_multiplier)

    async def _go() -> dict[str, Any]:
        # One event loop for the whole run: the async engine's connection pool
        # is bound to the loop that created it, so a second asyncio.run() here
        # hands back connections attached to a dead loop.
        out = await replay(
            underlyings=tuple(s.upper() for s in (args.underlying or INDEX_UNIVERSE)),
            bars=args.bars,
            lookback_days=args.lookback_days,
            config=cfg,
            reset=args.reset,
        )
        out["attribution"] = await attribution_rollup()
        return out

    print(json.dumps(asyncio.run(_go()), indent=2, default=str))


if __name__ == "__main__":
    main()
