"""Read-only API for the index directional long-options swing lane.

WHY THIS DESK LEADS WITH REFUSALS. The lane holds 1-5 trading sessions, trades
long premium only on NIFTY/BANKNIFTY/SENSEX, and on most sessions it will
decide to do nothing. That is the designed behaviour -- it has four sessions of
wide chain history and no measured direction factor, so standing aside is the
honest output. But an empty positions table renders identically whether the
lane reasoned its way to no-trade or whether its data feed died, and those two
have OPPOSITE meanings. So `/funnel` and `/factors` are the primary surfaces
here and `/positions` is secondary, which is the reverse of a normal book desk.

Everything is a projection of tables. There is no POST: the runners are
scheduled by the supervisor and a "run it now" button from a browser would be
an execution layer this lane deliberately does not have.

Four things the desk must be able to answer, and the endpoint that answers each:

  "Did it look at all today?"          /funnel      -- decisions by reason code
  "Why did it not trade?"              /factors     -- every factor's value and
                                                      whether it acted
  "Is the vol substrate alive?"        /surface     -- fit quality, staleness,
                                                      what the solver refused
  "What did the trades actually pay?"  /attribution -- greek decomposition
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.index_paper.engine import IndexPaperConfig
from directional_options.vol.surface import INDEX_UNIVERSE

router = APIRouter(prefix="/api/index-swing", tags=["index-swing"])

IST = timezone(timedelta(hours=5, minutes=30))

# Reason codes the lane emits, grouped so the funnel reads as a pipeline rather
# than an unordered tally. Order is the order a candidate meets them.
FUNNEL_ORDER: tuple[tuple[str, str], ...] = (
    ("surface_unusable", "No fittable volatility surface at this bar"),
    ("already_positioned", "Already holding this index"),
    ("max_exposure", "Book already at its concurrent-position cap"),
    ("no_direction_factor", "No direction factor was available to act"),
    ("direction_inconclusive", "Composite direction below the conviction floor"),
    ("direction_disagreement", "Acting factors did not agree on a side"),
    ("size_regime_hostile", "Panel judged this a poor regime to buy premium"),
    ("no_candidate_expiries_holdable", "No expiry has room for the intended hold"),
    ("no_candidate_quotes_on_side", "No quotes on the chosen side"),
    ("no_candidate_in_delta_band", "No strike inside the delta band"),
    ("no_candidate_above_oi_floor", "No strike above the open-interest floor"),
    ("no_candidate_geometry_computable", "Breakeven not computable for any strike"),
    ("no_candidate_within_breakeven_ceiling", "Every strike must travel too far to break even"),
    ("cost_too_high", "Round trip costs too much of the premium"),
    ("no_lagged_fill", "Chosen contract did not print at the fill bar"),
    ("below_one_lot", "Risk budget does not buy one lot"),
    ("degenerate_risk", "Modelled adverse move was zero"),
    ("entered", "Entered"),
)
FUNNEL_LABELS = dict(FUNNEL_ORDER)


async def _fetch_all(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        return [dict(row) for row in result.mappings().all()]


async def _fetch_one(sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rows = await _fetch_all(sql, params)
    return rows[0] if rows else None


def _session_bounds(session_date: date) -> dict[str, datetime]:
    start = datetime.combine(session_date, datetime.min.time(), tzinfo=IST).astimezone(timezone.utc)
    return {"lower": start, "upper": start + timedelta(days=1)}


async def _latest_session() -> date | None:
    row = await _fetch_one(
        "SELECT max(session_date) AS d FROM index_paper_decisions"
    )
    return row["d"] if row and row["d"] else None


@router.get("/summary")
async def summary() -> dict[str, Any]:
    """Book, configuration and today's activity in one call.

    `book` is shaped for the nav card. `unrealizedPnl` is genuinely marked --
    every open position is re-marked each pass off the chain or, when the
    contract has not printed, off the fitted surface -- so unlike some sibling
    lanes it is NOT declared absent.
    """
    cfg = IndexPaperConfig()
    book = await _fetch_one(
        """SELECT
             count(*) FILTER (WHERE status='open')                              AS open_positions,
             count(*) FILTER (WHERE status='closed')                            AS closed_positions,
             coalesce(sum(unrealized_pnl) FILTER (WHERE status='open'), 0)      AS unrealized_pnl,
             coalesce(sum(realized_pnl)   FILTER (WHERE status='closed'), 0)    AS realized_pnl,
             coalesce(sum(entry_cost) + sum(coalesce(exit_cost,0)), 0)          AS total_costs,
             count(*) FILTER (WHERE status='closed' AND realized_pnl > 0)       AS wins,
             count(*) FILTER (WHERE status='closed' AND realized_pnl <= 0)      AS losses
           FROM index_paper_positions"""
    ) or {}
    wins, losses = int(book.get("wins") or 0), int(book.get("losses") or 0)
    closed = wins + losses
    realized = float(book.get("realized_pnl") or 0.0)
    unrealized = float(book.get("unrealized_pnl") or 0.0)

    session_date = await _latest_session()
    today = datetime.now(IST).date()

    return {
        "universe": list(INDEX_UNIVERSE),
        "horizon_sessions": cfg.horizon_sessions,
        "horizon_label": f"{cfg.min_horizon_sessions}-{cfg.max_horizon_sessions} trading sessions",
        "book": {
            "initialCapital": cfg.initial_capital,
            "equity": cfg.initial_capital + realized + unrealized,
            "realizedPnl": realized,
            "unrealizedPnl": unrealized,
            "totalCosts": float(book.get("total_costs") or 0.0),
            "openPositions": int(book.get("open_positions") or 0),
            "closedPositions": int(book.get("closed_positions") or 0),
            "wins": wins,
            "losses": losses,
            "winRate": (wins / closed) if closed else None,
        },
        "latest_decision_session": session_date.isoformat() if session_date else None,
        "decided_today": bool(session_date and session_date == today),
        "gates": {
            "min_direction_score": cfg.min_direction_score,
            "min_direction_agreement": cfg.min_direction_agreement,
            "max_breakeven_ratio": cfg.max_breakeven_ratio,
            "min_contract_oi": cfg.min_contract_oi,
            "delta_band": [cfg.min_abs_delta, cfg.max_abs_delta],
            "max_concurrent_positions": cfg.max_concurrent_positions,
        },
    }


@router.get("/funnel")
async def funnel(
    session_date: date | None = Query(None, description="IST session; default = latest decided"),
    days: int = Query(1, ge=1, le=30, description="Sessions to aggregate, ending at session_date"),
    run_id: str | None = Query(None, description="Scope to one run; omit to include replays"),
) -> dict[str, Any]:
    """Where candidates die, per underlying.

    This is the endpoint that tells a reasoned no-trade apart from a dead feed.
    `evaluated = 0` means the lane never ran; a populated funnel with zero
    entries means it ran and declined, and names the gate that stopped it.
    """
    target = session_date or await _latest_session()
    if target is None:
        return {"session_date": None, "evaluated": 0, "stages": [], "by_underlying": [],
                "note": "the lane has never recorded a decision"}

    rows = await _fetch_all(
        """SELECT underlying, action, reason_code, count(*) AS n,
                  max(decided_at) AS latest
           FROM index_paper_decisions
           WHERE session_date > :from_date AND session_date <= :to_date
             AND (CAST(:run_id AS text) IS NULL OR run_id = CAST(:run_id AS text))
           GROUP BY underlying, action, reason_code""",
        {"from_date": target - timedelta(days=days), "to_date": target, "run_id": run_id},
    )
    runs = await _fetch_all(
        """SELECT coalesce(run_id, 'unstamped') AS run_id, count(*) AS n,
                  min(decided_at) AS first_at, max(decided_at) AS last_at
           FROM index_paper_decisions
           WHERE session_date > :from_date AND session_date <= :to_date
           GROUP BY 1 ORDER BY max(decided_at) DESC""",
        {"from_date": target - timedelta(days=days), "to_date": target},
    )
    totals: dict[str, int] = {}
    per_symbol: dict[str, dict[str, Any]] = {}
    for r in rows:
        code, n = r["reason_code"], int(r["n"])
        totals[code] = totals.get(code, 0) + n
        sym = per_symbol.setdefault(
            r["underlying"], {"underlying": r["underlying"], "reasons": {}, "entered": 0, "evaluated": 0}
        )
        sym["reasons"][code] = sym["reasons"].get(code, 0) + n
        if r["action"] in ("enter", "skip"):
            sym["evaluated"] += n
        if code == "entered":
            sym["entered"] += n

    stages = [
        {"reason_code": code, "label": label, "count": totals.get(code, 0)}
        for code, label in FUNNEL_ORDER
        if totals.get(code)
    ]
    # Anything the lane emitted that this router does not know how to label is
    # surfaced rather than dropped — an unlabelled reason code is a code change
    # the desk has not caught up with, not an absence.
    for code, n in sorted(totals.items(), key=lambda kv: -kv[1]):
        if code not in FUNNEL_LABELS:
            stages.append({"reason_code": code, "label": code, "count": n, "unlabelled": True})

    evaluated = sum(v["evaluated"] for v in per_symbol.values())
    return {
        "session_date": target.isoformat(),
        "days": days,
        "evaluated": evaluated,
        "entered": totals.get("entered", 0),
        "stages": stages,
        "by_underlying": sorted(per_symbol.values(), key=lambda v: v["underlying"]),
        # The journal is append-only and survives a replay reset, so a session
        # can carry several runs. Counting them together would overstate
        # activity; listing them lets the desk scope or caveat it.
        "run_id": run_id,
        "runs": runs,
        "mixes_runs": run_id is None and len(runs) > 1,
    }


@router.get("/factors")
async def factors(
    session_date: date | None = Query(None),
    underlying: str | None = Query(None),
) -> dict[str, Any]:
    """Every factor's value at the last decision, acting or not.

    A factor with `acting=false` still has a value here. That is the point: the
    factor journal exists so a shadow factor can eventually be graded against
    this lane's own outcomes rather than re-fitted on the history that
    suggested it.
    """
    target = session_date or await _latest_session()
    if target is None:
        return {"session_date": None, "underlyings": []}

    rows = await _fetch_all(
        """SELECT DISTINCT ON (underlying, factor)
                  underlying, factor, family, role, value, score, sign, confidence,
                  weight, status, acting, direction_score, size_score, detail, bar_ts
           FROM index_paper_factors
           WHERE session_date = :d
             AND (CAST(:u AS text) IS NULL OR underlying = CAST(:u AS text))
           ORDER BY underlying, factor, bar_ts DESC""",
        {"d": target, "u": underlying},
    )
    by_symbol: dict[str, dict[str, Any]] = {}
    for r in rows:
        sym = by_symbol.setdefault(
            r["underlying"],
            {
                "underlying": r["underlying"],
                "bar_ts": r["bar_ts"].isoformat() if r["bar_ts"] else None,
                "direction_score": r["direction_score"],
                "size_score": r["size_score"],
                "direction": [],
                "size": [],
            },
        )
        entry = {
            "name": r["factor"], "family": r["family"], "value": r["value"],
            "score": r["score"], "sign": r["sign"], "confidence": r["confidence"],
            "weight": r["weight"], "status": r["status"], "acting": r["acting"],
            # The signed contribution is what actually entered the composite;
            # the raw score alone reads with the wrong sign on an inverted factor.
            "contribution": (
                (r["sign"] or 1) * r["score"] * (r["weight"] or 0.0)
                if r["score"] is not None else None
            ),
            "detail": r["detail"],
        }
        by_symbol[r["underlying"]]["direction" if r["role"] == "direction" else "size"].append(entry)
    return {"session_date": target.isoformat(), "underlyings": sorted(by_symbol.values(), key=lambda v: v["underlying"])}


@router.get("/surface")
async def surface(days: int = Query(10, ge=1, le=60)) -> dict[str, Any]:
    """Vol substrate health: fit quality, staleness, and what the solver refused.

    The quarantine count is not a footnote. A day where it jumps is a data
    incident, and having it on the desk is the difference between noticing and
    inferring it later from a missing feature.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    slices = await _fetch_all(
        """SELECT DISTINCT ON (underlying) underlying, ts, expiry, days_to_expiry,
                  forward, spot, atm_iv, fit_status, n_quotes, n_used, n_rejects,
                  rmse_vol, butterfly_ok, min_g, k_min, k_max, stale_seconds, reason
           FROM index_vol_surface_slices
           WHERE ts >= :since AND fit_status = 'ok'
           ORDER BY underlying, ts DESC""",
        {"since": since},
    )
    metrics = await _fetch_all(
        """SELECT DISTINCT ON (underlying) underlying, ts, tenor_days, atm_iv, rr_25,
                  bf_25, skew_slope, realized_vol, vrp_spread, vrp_ratio, rv_percentile
           FROM index_vol_tenor_metrics
           WHERE ts >= :since AND tenor_kind = 'front'
           ORDER BY underlying, ts DESC""",
        {"since": since},
    )
    quarantine = await _fetch_all(
        """SELECT underlying, stage, status, count(*) AS n
           FROM index_vol_quarantine WHERE ts >= :since
           GROUP BY underlying, stage, status ORDER BY n DESC""",
        {"since": since},
    )
    now = datetime.now(timezone.utc)
    for row in slices:
        row["age_minutes"] = round((now - row["ts"]).total_seconds() / 60.0, 1) if row["ts"] else None
    return {
        "window_days": days,
        "slices": slices,
        "tenor_metrics": metrics,
        "quarantine": quarantine,
        # An empty substrate and a stale one look identical in a table. Say
        # which it is rather than leaving the desk to guess.
        "status": (
            "no_data" if not slices
            else "stale" if min((r["age_minutes"] or 0) for r in slices) > 24 * 60
            else "ok"
        ),
    }


@router.get("/positions")
async def positions(
    status: str = Query("all", pattern="^(all|open|closed)$"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    rows = await _fetch_all(
        """SELECT position_id, status, session_date, underlying, expiry, strike,
                  option_type, lots, lot_size, quantity, entry_ts, entry_premium,
                  entry_cost, entry_iv, entry_delta, entry_vega, entry_theta,
                  entry_mv_delta, stop_premium, target_premium, max_hold_bars AS hold_sessions,
                  latest_ts, latest_premium, unrealized_pnl, exit_ts, exit_premium,
                  exit_cost, exit_reason, realized_pnl
           FROM index_paper_positions
           WHERE (:status = 'all' OR status = :status)
           ORDER BY coalesce(exit_ts, entry_ts) DESC
           LIMIT :limit""",
        {"status": status, "limit": limit},
    )
    return {"status": status, "count": len(rows), "positions": rows}


@router.get("/attribution")
async def attribution(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """Which greek actually paid, per closed trade and in aggregate.

    Without this a correct directional call cannot be told apart from a wrong
    one rescued by a vol expansion, and factor selection runs on noise.
    """
    rows = await _fetch_all(
        """SELECT a.position_id, a.closed_at, a.underlying, a.gross_pnl, a.costs,
                  a.net_pnl, a.delta_pnl, a.gamma_pnl, a.vega_pnl, a.theta_pnl,
                  a.residual_pnl, a.d_spot, a.d_iv, a.d_sessions,
                  a.explained_fraction, p.option_type, p.strike, p.exit_reason
           FROM index_paper_attribution a
           JOIN index_paper_positions p USING (position_id)
           ORDER BY a.closed_at DESC LIMIT :limit""",
        {"limit": limit},
    )
    agg = await _fetch_one(
        """SELECT count(*) AS n,
                  coalesce(avg(a.delta_pnl),0)    AS delta_pnl,
                  coalesce(avg(a.gamma_pnl),0)    AS gamma_pnl,
                  coalesce(avg(a.vega_pnl),0)     AS vega_pnl,
                  coalesce(avg(a.theta_pnl),0)    AS theta_pnl,
                  coalesce(avg(a.residual_pnl),0) AS residual_pnl,
                  coalesce(avg(a.costs),0)        AS costs,
                  coalesce(avg(a.net_pnl),0)      AS net_pnl,
                  coalesce(sum(abs(a.residual_pnl)),0) AS abs_residual,
                  coalesce(sum(abs(a.gross_pnl)),0)    AS abs_gross
           FROM index_paper_attribution a
           JOIN index_paper_positions p USING (position_id)"""
    ) or {}
    abs_gross = float(agg.pop("abs_gross", 0.0) or 0.0)
    abs_residual = float(agg.pop("abs_residual", 0.0) or 0.0)
    agg["explained_fraction"] = (1.0 - abs_residual / abs_gross) if abs_gross > 1e-9 else None
    return {"per_trade_average": agg, "trades": rows}
