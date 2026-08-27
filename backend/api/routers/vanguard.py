"""Read-only API surface for the Vanguard paper trade-selection lane.

Vanguard (M1-M10) is a research/paper system that lives OUTSIDE this backend --
its code sits in its own git worktree and runs as its own process against the
same Postgres. This router therefore projects Vanguard's TABLES and imports
none of its Python. That is deliberate:

  - the backend must not depend on a worktree path that may not exist in the
    container, and must not import research code into the API process;
  - Vanguard writes on its own cadence (`make daily-cycle`), so the API is a
    reader of whatever that last wrote, never a trigger for it. There is no
    POST here at all -- doctrine #4 forbids an execution layer, and a "run it
    now" button from a browser is exactly the execution layer it forbids.

THE ONE PIECE OF DUPLICATED KNOWLEDGE, and how it is kept honest: `/funnel`
re-expresses M6's own candidate filter in SQL so the UI can show WHERE
candidates die, not just that none survived. That mirrors constants defined in
`vanguard/fusion/m6_select.py`. Mirrored constants drift silently, so:
  (a) they are returned in the response under `thresholds`, so the UI always
      renders the numbers actually applied rather than hardcoding its own; and
  (b) `backend/tests/test_vanguard_router.py` reads m6_select.py and asserts
      these values still match, failing loudly if Vanguard retunes them.
This is the same technique nav-model.test.ts uses against ViewNav.tsx.

WHY A FUNNEL AT ALL: on today's real data Vanguard emits ZERO tickets -- every
candidate dies at one filter or another, and `tickets` is empty. An empty table
rendered as an empty panel is indistinguishable from a broken feed, which is
precisely the failure nav-model.ts calls out ("an em-dash indistinguishable
from a flat book"). The funnel turns "nothing here" into "247 symbols had a
bar, 12 reached IGNITION, 4 had prior-session flow, 0 of those cleared
|flow|>=60" -- a designed no-trade, evidenced. Doctrine #2 says the default
answer is NO TRADE; this endpoint is how the UI proves the no was reasoned.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from db.database import AsyncSessionLocal

router = APIRouter(prefix="/api/vanguard", tags=["vanguard"])

# ── Mirrored from vanguard/fusion/m6_select.py — see module docstring ────────
FLOW_MIN_ABS = 60.0
SECTOR_RS_MIN_ABS_Z = 1.0
TIMING_MIN_SCORE = 70.0
REGIME_PERMITS = ["STRONG_NEG", "NEG", "NEUTRAL"]
CONVICTION_MIN = 85.0
TOP_N_PER_BAR = 3

# The feature tables M6 consumes, and the cadence each is written at. Used by
# /pipeline to report coverage per input rather than one blended "is it fresh".
FEATURE_TABLES = [
    ("features_flow", "ts", "M2 options informed-flow", "session"),
    ("regime", "ts", "M3 GEX regime", "bar"),
    ("sector_rs", "ts", "M4 sector relative strength", "session"),
    ("timing", "ts", "M5 microstructure timing", "bar"),
]


async def _fetch_all(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return [dict(row) for row in result.mappings().all()]


async def _fetch_one(sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rows = await _fetch_all(sql, params)
    return rows[0] if rows else None


async def _latest_timing_bar() -> datetime | None:
    row = await _fetch_one("SELECT max(ts) AS ts FROM timing")
    return row["ts"] if row else None


@router.get("/summary")
async def summary() -> dict[str, Any]:
    """Headline state of the whole lane: what it last evaluated, what it
    selected, what the book is doing, and how much simulated capital exists."""
    tickets = await _fetch_one(
        """SELECT count(*) AS total,
                  count(*) FILTER (WHERE emitted) AS emitted,
                  count(*) FILTER (WHERE NOT emitted) AS gated,
                  max(ts) AS last_ticket_ts
           FROM tickets"""
    )
    book = await _fetch_one(
        """SELECT count(*) FILTER (WHERE NOT o.closed) AS open_positions,
                  count(*) FILTER (WHERE o.closed) AS closed_positions,
                  count(*) FILTER (WHERE o.closed AND o.r_multiple > 0) AS winners,
                  avg(o.r_multiple) FILTER (WHERE o.closed) AS avg_r,
                  sum(o.pnl_rupees) FILTER (WHERE o.closed) AS realized_pnl
           FROM outcomes o"""
    )
    capital = await _fetch_one(
        """SELECT dt, starting_equity, ending_equity, realized_pnl
           FROM paper_capital_daily ORDER BY dt DESC LIMIT 1"""
    )
    latest_bar = await _latest_timing_bar()

    closed = (book or {}).get("closed_positions") or 0
    winners = (book or {}).get("winners") or 0
    return {
        "latest_timing_bar": latest_bar,
        "tickets": tickets or {},
        "book": {
            **(book or {}),
            "hit_rate": (winners / closed) if closed else None,
            # Carried INTO `book` (not left only under `capital`) so the shared
            # landing-card reader in nav-model.normalizeBook can pick it up from
            # a single path; its alias list already recognises `total_equity`.
            # Unrealized P&L is deliberately NOT here: nothing marks open paper
            # positions to market, and inventing a 0 would read as a flat book
            # rather than an unmeasured one. The desk declares it absent.
            "total_equity": (capital or {}).get("ending_equity"),
        },
        "capital": capital,
        "thresholds": {
            "flow_min_abs": FLOW_MIN_ABS,
            "sector_rs_min_abs_z": SECTOR_RS_MIN_ABS_Z,
            "timing_min_score": TIMING_MIN_SCORE,
            "regime_permits": REGIME_PERMITS,
            "conviction_min": CONVICTION_MIN,
            "top_n_per_bar": TOP_N_PER_BAR,
        },
    }


@router.get("/funnel")
async def funnel(ts: datetime | None = Query(None, description="Bar to explain; default = latest timing bar")) -> dict[str, Any]:
    """Where candidates die at one 30-minute bar.

    Mirrors `m6_select.load_candidates_at`'s joins and filters EXACTLY --
    including its `::date` comparison for the session-cadence tables, so the
    funnel reports what M6 actually did rather than what it ideally should do.
    If M6's join is ever corrected, correct it here too (the router test
    guards the thresholds, not the join shape).
    """
    bar_ts = ts or await _latest_timing_bar()
    if bar_ts is None:
        raise HTTPException(status_code=404, detail="no timing bars exist yet")

    row = await _fetch_one(
        """
        WITH bar AS (
            SELECT tm.symbol, tm.ts, tm.timing_state, tm.timing_score
            FROM timing tm WHERE tm.ts = :ts
        ),
        joined AS (
            SELECT b.symbol, b.ts, b.timing_state, b.timing_score,
                   st.sector20, fl.flow_score, sr.rs_z20, rg.regime
            FROM bar b
            LEFT JOIN sector_taxonomy st ON st.symbol = b.symbol
            LEFT JOIN LATERAL (
                SELECT flow_score FROM features_flow f
                WHERE f.symbol = b.symbol AND f.ts::date < :day
                ORDER BY f.ts DESC LIMIT 1
            ) fl ON true
            LEFT JOIN LATERAL (
                SELECT rs_z20 FROM sector_rs s
                WHERE s.sector20 = st.sector20 AND s.ts::date < :day
                ORDER BY s.ts DESC LIMIT 1
            ) sr ON true
            LEFT JOIN LATERAL (
                SELECT regime FROM regime r
                WHERE r.symbol = b.symbol AND r.ts <= b.ts
                ORDER BY r.ts DESC LIMIT 1
            ) rg ON true
        )
        SELECT
          count(*) AS symbols_at_bar,
          count(*) FILTER (WHERE flow_score IS NOT NULL) AS has_prior_flow,
          count(*) FILTER (WHERE flow_score IS NOT NULL
                             AND abs(flow_score) >= :flow_min) AS flow_passes,
          count(*) FILTER (WHERE flow_score IS NOT NULL
                             AND abs(flow_score) >= :flow_min
                             AND rs_z20 IS NOT NULL
                             AND abs(rs_z20) >= :rs_min
                             AND (rs_z20 > 0) = (flow_score > 0)) AS sector_confirms,
          count(*) FILTER (WHERE flow_score IS NOT NULL
                             AND abs(flow_score) >= :flow_min
                             AND rs_z20 IS NOT NULL
                             AND abs(rs_z20) >= :rs_min
                             AND (rs_z20 > 0) = (flow_score > 0)
                             AND regime = ANY(:permits)) AS regime_permits_count,
          count(*) FILTER (WHERE flow_score IS NOT NULL
                             AND abs(flow_score) >= :flow_min
                             AND rs_z20 IS NOT NULL
                             AND abs(rs_z20) >= :rs_min
                             AND (rs_z20 > 0) = (flow_score > 0)
                             AND regime = ANY(:permits)
                             AND timing_state = 'IGNITION'
                             AND timing_score >= :timing_min) AS survives_all
        FROM joined
        """,
        {
            "ts": bar_ts,
            "day": bar_ts.date(),
            "flow_min": FLOW_MIN_ABS,
            "rs_min": SECTOR_RS_MIN_ABS_Z,
            "timing_min": TIMING_MIN_SCORE,
            "permits": REGIME_PERMITS,
        },
    )
    row = row or {}

    stages = [
        {"stage": "symbols with a timing bar", "surviving": row.get("symbols_at_bar") or 0,
         "gate": "M5 wrote a row for this bar"},
        {"stage": "has prior-session flow", "surviving": row.get("has_prior_flow") or 0,
         "gate": "M2 features_flow exists for an earlier session"},
        {"stage": "flow conviction", "surviving": row.get("flow_passes") or 0,
         "gate": f"|flow_score| >= {FLOW_MIN_ABS}"},
        {"stage": "sector RS confirms", "surviving": row.get("sector_confirms") or 0,
         "gate": f"|rs_z20| >= {SECTOR_RS_MIN_ABS_Z} and same direction as flow"},
        {"stage": "regime permits", "surviving": row.get("regime_permits_count") or 0,
         "gate": f"M3 regime in {'/'.join(REGIME_PERMITS)}"},
        {"stage": "timing fires", "surviving": row.get("survives_all") or 0,
         "gate": f"timing_state = IGNITION and score >= {TIMING_MIN_SCORE}"},
    ]
    binding = None
    for index in range(1, len(stages)):
        if stages[index]["surviving"] == 0 and stages[index - 1]["surviving"] > 0:
            binding = stages[index]["stage"]
            break

    return {
        "ts": bar_ts,
        "stages": stages,
        "binding_constraint": binding,
        "survivors": row.get("survives_all") or 0,
    }


@router.get("/selection")
async def selection(
    ts: datetime | None = Query(None, description="Bar to show; default = the most recent bar that produced any ticket"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Tickets at one bar -- BOTH emitted and gated-out.

    Gated rows are the point, not noise: doctrine #5 keeps every near-miss with
    the gate that stopped it, so the UI can show a reasoned no-trade rather
    than a blank panel.
    """
    bar_ts = ts
    if bar_ts is None:
        row = await _fetch_one("SELECT max(ts) AS ts FROM tickets")
        bar_ts = row["ts"] if row else None
    if bar_ts is None:
        return {"ts": None, "tickets": [], "note": "no tickets have ever been generated"}

    tickets = await _fetch_all(
        """SELECT id, ts, symbol, instrument, direction, conviction, rank_in_session,
                  regime_at_ts, evidence, emitted, gated_reason,
                  entry_zone_low, entry_zone_high, stop, target1, target2,
                  sizing_lots, sizing_notional, sizing_risk_rupees, sizing_method,
                  strike, option_type, expiry, lot_size
           FROM tickets WHERE ts = :ts
           ORDER BY emitted DESC, conviction DESC
           LIMIT :limit""",
        {"ts": bar_ts, "limit": limit},
    )
    return {"ts": bar_ts, "tickets": tickets, "conviction_min": CONVICTION_MIN}


@router.get("/book")
async def book(limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    """The paper book: open positions, closed trades, and the equity curve.

    Every row here originates from a SIMULATED fill against real historical
    bars. `fills.fill_method` is carried through verbatim so the UI can render
    the simulation basis rather than implying a broker fill.
    """
    open_positions = await _fetch_all(
        """SELECT t.id, t.symbol, t.instrument, t.direction, t.conviction,
                  t.stop, t.target1, t.target2, t.sizing_lots, t.lot_size,
                  t.sizing_risk_rupees,
                  f.fill_price, f.fill_ts, f.fill_method
           FROM outcomes o
           JOIN tickets t ON t.id = o.ticket_id
           JOIN fills f ON f.ticket_id = o.ticket_id
           WHERE NOT o.closed
           ORDER BY f.fill_ts DESC
           LIMIT :limit""",
        {"limit": limit},
    )
    closed = await _fetch_all(
        """SELECT t.id, t.symbol, t.instrument, t.direction, t.conviction,
                  t.sizing_lots, t.lot_size,
                  f.fill_price, f.fill_ts, f.fill_method,
                  o.exit_price, o.exit_ts, o.exit_reason, o.pnl_rupees,
                  o.r_multiple, o.holding_bars
           FROM outcomes o
           JOIN tickets t ON t.id = o.ticket_id
           JOIN fills f ON f.ticket_id = o.ticket_id
           WHERE o.closed
           ORDER BY o.exit_ts DESC
           LIMIT :limit""",
        {"limit": limit},
    )
    equity = await _fetch_all(
        """SELECT dt, starting_equity, ending_equity, realized_pnl
           FROM paper_capital_daily ORDER BY dt ASC LIMIT :limit""",
        {"limit": limit},
    )
    by_reason = await _fetch_all(
        """SELECT exit_reason, count(*) AS n, avg(r_multiple) AS avg_r
           FROM outcomes WHERE closed AND exit_reason IS NOT NULL
           GROUP BY exit_reason ORDER BY n DESC"""
    )
    return {
        "open_positions": open_positions,
        "closed": closed,
        "equity_curve": equity,
        "exit_reason_breakdown": by_reason,
    }


@router.get("/attribution")
async def attribution(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    """M10 rollups, newest first. `latest` is broken out so the UI does not
    have to know that runs are append-only."""
    runs = await _fetch_all(
        """SELECT id, run_at, as_of_date, n_tickets_closed, hit_rate, avg_r,
                  conviction_decile_monotonic, report
           FROM attribution_runs ORDER BY run_at DESC LIMIT :limit""",
        {"limit": limit},
    )
    return {"latest": runs[0] if runs else None, "runs": runs}


@router.get("/backtests")
async def backtests(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    """M8 replay results. Deliberately a DIFFERENT table from attribution:
    a backtest is not a track record and the UI must never blend them."""
    runs = await _fetch_all(
        """SELECT id, run_at, start_ts, end_ts, report
           FROM vanguard_backtest_runs ORDER BY run_at DESC LIMIT :limit""",
        {"limit": limit},
    )
    return {"latest": runs[0] if runs else None, "runs": runs}


@router.get("/pipeline")
async def pipeline() -> dict[str, Any]:
    """Feed health per M6 input, plus the overlap window where all four inputs
    actually coexist.

    The overlap is the number that explains the lane: M6 needs flow + sector RS
    (both prior-session) AND regime + timing (both same-bar) simultaneously. If
    those coverages barely intersect, the lane cannot emit no matter how the
    thresholds are tuned -- and that is a DATA problem the UI must show as such
    rather than letting it read as a quiet strategy decision.
    """
    feeds = []
    for table, ts_column, label, cadence in FEATURE_TABLES:
        stats = await _fetch_one(
            f"""SELECT count(*) AS rows,
                       min({ts_column}) AS first_ts,
                       max({ts_column}) AS last_ts,
                       count(DISTINCT {'symbol' if table != 'sector_rs' else 'sector20'}) AS entities
                FROM {table}"""  # noqa: S608 - table/column names are from a fixed local constant, never user input
        )
        feeds.append({"table": table, "label": label, "cadence": cadence, **(stats or {})})

    coverage = await _fetch_all(
        """SELECT d AS session,
                  (SELECT count(*) FROM features_flow f WHERE f.ts::date = d) AS flow_rows,
                  (SELECT count(*) FROM timing t WHERE date(t.ts AT TIME ZONE 'Asia/Kolkata') = d) AS timing_rows,
                  (SELECT count(*) FROM regime r WHERE date(r.ts AT TIME ZONE 'Asia/Kolkata') = d) AS regime_rows
           FROM (
               SELECT DISTINCT date(ts AT TIME ZONE 'Asia/Kolkata') AS d
               FROM timing
               ORDER BY 1 DESC LIMIT 30
           ) days
           ORDER BY d DESC"""
    )

    ingest = await _fetch_all(
        """SELECT collector, target_date, status, rows_written, run_at, detail
           FROM ingest_log ORDER BY run_at DESC LIMIT 40"""
    )

    sessions_all_present = sum(
        1 for row in coverage
        if (row.get("flow_rows") or 0) > 0 and (row.get("timing_rows") or 0) > 0 and (row.get("regime_rows") or 0) > 0
    )
    return {
        "feeds": feeds,
        "recent_sessions": coverage,
        "sessions_with_all_inputs": sessions_all_present,
        "sessions_examined": len(coverage),
        "ingest_log": ingest,
    }
