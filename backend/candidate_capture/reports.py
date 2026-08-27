"""Data-quality reporting over the captured/labelled set.

Two questions this answers, both of which must be answered from the DATA before
any model is trained on it:

1. IS THE CAPTURE HONEST? How much of what we claim to have captured is
   actually present — coverage gaps, unlabellable rows, assumed-vs-measured
   spreads, contracts that never traded.

2. IS THE HORIZON DECIDABLE? For a given horizon and contract class, can the
   typical contract's move actually clear its own round-trip cost? This is
   sniper-phase0's `m_breakeven`, described there as the number that "gates
   every label". A horizon that fails it produces a training set whose labels
   are almost all losses by construction, and a ranker trained on that learns
   to abstain rather than to rank — which reads as a modelling failure when it
   is really a horizon-selection failure.

Measured on real 2026-08-25 expiry-day NIFTY contracts, decidability at a
5-minute horizon was ZERO — breakeven 7.4% of premium against a typical option
move well under 2%. Measured on 33-DTE ATM monthly contracts with a real quoted
spread, breakeven was 0.76%. The same horizon is therefore decidable for one
contract class and useless for another, which is exactly why the report
stratifies rather than producing one headline number.

Read-only: this module runs SELECTs and returns dicts. It writes nothing.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import text

from db.database import AsyncSessionLocal

IST = timezone(timedelta(hours=5, minutes=30))

# A stratum needs at least this many rows before its decidability rate is
# reported as a rate rather than as a count. A percentage over three rows is
# not a percentage, and this repo has been burned before by statistics quoted
# from samples too small to carry them.
MIN_STRATUM_ROWS = 30


async def coverage_report(
    *, since: Optional[date] = None, limit_sessions: int = 30
) -> dict[str, Any]:
    """Per-session capture health: rows, gaps, and what was assumed vs measured."""
    params: dict[str, Any] = {"limit": int(limit_sessions)}
    where = ""
    if since is not None:
        where = "WHERE session_date >= :since"
        params["since"] = since

    async with AsyncSessionLocal() as session:
        snaps = (
            await session.execute(
                text(
                    f"""
                    SELECT session_date,
                           count(*)                                        AS rows,
                           count(DISTINCT decision_id)                     AS decision_sets,
                           count(DISTINCT underlying)                      AS underlyings,
                           count(*) FILTER (WHERE option_type = 'NO_TRADE') AS no_trade_rows,
                           count(*) FILTER (WHERE eligibility_status = 'eligible') AS eligible,
                           count(*) FILTER (WHERE chain_is_stale)          AS stale,
                           count(*) FILTER (WHERE bid IS NULL OR ask IS NULL) AS no_quote,
                           count(*) FILTER (WHERE lot_size IS NULL)        AS missing_lot_size,
                           count(*) FILTER (WHERE expiry_class = 'UNKNOWN') AS unknown_expiry_class
                      FROM candidate_snapshots
                      {where}
                     GROUP BY session_date
                     ORDER BY session_date DESC
                     LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().all()

        labels = (
            await session.execute(
                text(
                    f"""
                    SELECT session_date, horizon_seconds, label_status,
                           count(*) AS rows,
                           count(*) FILTER (WHERE entry_half_spread_measured) AS entry_spread_measured,
                           count(*) FILTER (WHERE exit_half_spread_measured)  AS exit_spread_measured,
                           count(*) FILTER (WHERE trade_arrived IS FALSE)     AS never_traded,
                           round(avg(forward_lag_seconds)::numeric, 1)        AS avg_forward_lag_s,
                           round(avg(forward_sample_count)::numeric, 2)       AS avg_forward_samples,
                           round(avg(spot_tick_count)::numeric, 0)            AS avg_spot_ticks
                      FROM candidate_outcomes
                      {where}
                     GROUP BY session_date, horizon_seconds, label_status
                     ORDER BY session_date DESC, horizon_seconds
                    """
                ),
                params if since is not None else {},
            )
        ).mappings().all()

    return {
        "capture_by_session": [dict(r) for r in snaps],
        "labels_by_session_horizon_status": [dict(r) for r in labels],
        "notes": [
            "no_quote counts rows with no two-sided quote — their cost is ASSUMED, not measured.",
            "exit_spread_measured is expected to be 0 whenever the forward mark came from "
            "option_chain_snapshots, which carries no bid/ask. That is a property of the "
            "source, not a defect.",
            "never_traded counts forward marks with zero volume delta: the price did not "
            "hold, no trade arrived. A zero return on those rows is not a market observation.",
        ],
    }


async def decidability_report(
    *, since: Optional[date] = None
) -> dict[str, Any]:
    """Can each (horizon x contract class) actually clear its own cost?

    Stratified by the dimensions that genuinely change the answer — horizon,
    expiry class, moneyness and liquidity — because a single pooled number
    hides that the same horizon is fine for a liquid ATM monthly and hopeless
    for an expiry-day wing.
    """
    where = "WHERE label_status = 'ok'"
    params: dict[str, Any] = {}
    if since is not None:
        where += " AND o.session_date >= :since"
        params["since"] = since

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT o.horizon_seconds,
                           s.expiry_class,
                           s.moneyness,
                           s.liquidity_bucket,
                           count(*)                                             AS rows,
                           round((100*avg(o.breakeven_move_pct))::numeric, 3)    AS avg_breakeven_pct,
                           round((100*avg(abs(o.option_mfe_pct)))::numeric, 3)   AS avg_abs_mfe_pct,
                           count(*) FILTER (WHERE o.economically_decidable)      AS decidable_rows,
                           round((100*avg(o.option_gross_return_pct))::numeric,3) AS avg_gross_pct,
                           round((100*avg(o.option_net_return_pct))::numeric, 3) AS avg_net_pct,
                           count(*) FILTER (WHERE o.trade_arrived IS FALSE)      AS never_traded
                      FROM candidate_outcomes o
                      JOIN candidate_snapshots s
                        ON s.time = o.time
                       AND s.decision_id = o.decision_id
                       AND s.underlying = o.underlying
                       AND s.option_type = o.option_type
                       AND s.strike IS NOT DISTINCT FROM o.strike
                       AND s.expiry IS NOT DISTINCT FROM o.expiry
                      {where}
                     GROUP BY 1, 2, 3, 4
                     ORDER BY 1, 2, 3, 4
                    """
                ),
                params,
            )
        ).mappings().all()

    strata: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        n = int(record["rows"] or 0)
        decidable = int(record["decidable_rows"] or 0)
        if n >= MIN_STRATUM_ROWS:
            record["decidable_rate"] = round(decidable / n, 4)
        else:
            # Refused rather than reported: a rate over a handful of rows is not
            # a rate, and quoting one here is how a spurious "edge" gets adopted.
            record["decidable_rate"] = None
            record["decidable_rate_withheld"] = (
                f"n={n} < {MIN_STRATUM_ROWS}; too few rows to state a rate"
            )
        strata.append(record)

    usable = [s for s in strata if s.get("decidable_rate") is not None]
    recommended: list[dict[str, Any]] = sorted(
        usable, key=lambda s: (-(s["decidable_rate"] or 0.0), s["horizon_seconds"])
    )[:10]

    return {
        "strata": strata,
        "strata_count": len(strata),
        "strata_with_enough_rows": len(usable),
        "most_decidable": recommended,
        "min_stratum_rows": MIN_STRATUM_ROWS,
        "interpretation": (
            "decidable_rate is the fraction of rows whose own best excursion over the "
            "horizon could clear their own round-trip cost. A stratum near 0 means the "
            "horizon cannot decide anything for that contract class — do NOT train a "
            "ranker on it and conclude the signal is weak; the horizon is the problem. "
            "A NULL rate means the stratum is too small to quote."
        ),
    }


async def readiness_report(*, min_sessions: int = 10) -> dict[str, Any]:
    """Is there yet enough labelled data to justify training a first model?

    The plan's own sequence puts 10-20 sessions of collection BEFORE any
    training, and this is the gate that says whether that has happened. It
    reports readiness; it does not train anything.
    """
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT count(DISTINCT session_date)                    AS sessions,
                           count(*)                                        AS label_rows,
                           count(*) FILTER (WHERE label_status = 'ok')     AS ok_rows,
                           count(DISTINCT underlying)                      AS underlyings,
                           min(session_date)                               AS first_session,
                           max(session_date)                               AS last_session
                      FROM candidate_outcomes
                    """
                )
            )
        ).mappings().first()

    stats = dict(row or {})
    sessions = int(stats.get("sessions") or 0)
    ok_rows = int(stats.get("ok_rows") or 0)
    blockers: list[str] = []
    if sessions < min_sessions:
        blockers.append(
            f"only {sessions} labelled session(s); the plan calls for {min_sessions}-20 "
            "before a first baseline is trained"
        )
    if ok_rows == 0:
        blockers.append("no successfully labelled rows yet")

    stats["ready_to_train"] = not blockers
    stats["blockers"] = blockers
    stats["min_sessions"] = min_sessions
    return stats


# ══════════════════════════════════════════════════════════════════════════
# Explorer reads — one decision set, or a filtered slice of rows
# ══════════════════════════════════════════════════════════════════════════
# `time` is ALWAYS bounded directly with literal UTC instants derived from the
# session date, in addition to any session_date filter. The DATE column alone
# would filter correctly but scan every chunk; wrapping `time` in a cast to get
# there is the thing that previously SIGKILLed the live Postgres.

MAX_ROWS = 500


def _session_bounds(session_date: date) -> tuple[datetime, datetime]:
    from candidate_capture.labeller_io import session_bounds_utc

    return session_bounds_utc(session_date)


async def list_sessions(limit: int = 60) -> list[dict[str, Any]]:
    """Which sessions have captured data, newest first — the explorer's index."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT s.session_date,
                           count(*)                                         AS snapshots,
                           count(DISTINCT s.underlying)                     AS underlyings,
                           count(DISTINCT s.decision_id)                    AS decision_sets,
                           count(*) FILTER (WHERE s.eligibility_status = 'eligible') AS eligible,
                           (SELECT count(*) FROM candidate_outcomes o
                             WHERE o.session_date = s.session_date)         AS outcomes
                      FROM candidate_snapshots s
                     GROUP BY s.session_date
                     ORDER BY s.session_date DESC
                     LIMIT :limit
                    """
                ),
                {"limit": max(1, min(int(limit), 200))},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def list_snapshots(
    *,
    session_date: date,
    underlying: Optional[str] = None,
    expiry_class: Optional[str] = None,
    moneyness: Optional[str] = None,
    liquidity_bucket: Optional[str] = None,
    eligibility_status: Optional[str] = None,
    include_no_trade: bool = True,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """A filtered slice of captured candidates for one session."""
    start, end = _session_bounds(session_date)
    clauses = ["time >= :start", "time < :end", "session_date = :session_date"]
    params: dict[str, Any] = {
        "start": start,
        "end": end,
        "session_date": session_date,
        "limit": max(1, min(int(limit), MAX_ROWS)),
    }
    for column, value in (
        ("underlying", underlying),
        ("expiry_class", expiry_class),
        ("moneyness", moneyness),
        ("liquidity_bucket", liquidity_bucket),
        ("eligibility_status", eligibility_status),
    ):
        if value:
            clauses.append(f"{column} = :{column}")
            params[column] = value
    if not include_no_trade:
        clauses.append("option_type <> 'NO_TRADE'")

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT time, decision_id, underlying, underlying_class,
                           expiry, expiry_class, days_to_expiry,
                           option_type, strike, moneyness, moneyness_steps,
                           liquidity_bucket, liquidity_percentile,
                           spot, ltp, bid, ask, spread_pct, volume, oi, iv,
                           delta, gamma, theta, vega, lot_size,
                           eligibility_status, eligibility_reason,
                           chain_is_stale, missing_fields
                      FROM candidate_snapshots
                     WHERE {' AND '.join(clauses)}
                     ORDER BY time DESC, underlying, option_type, strike
                     LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def list_outcomes(
    *,
    session_date: date,
    underlying: Optional[str] = None,
    horizon_seconds: Optional[int] = None,
    label_status: Optional[str] = None,
    decidable_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """A filtered slice of labelled outcomes for one session."""
    start, end = _session_bounds(session_date)
    clauses = ["time >= :start", "time < :end", "session_date = :session_date"]
    params: dict[str, Any] = {
        "start": start,
        "end": end,
        "session_date": session_date,
        "limit": max(1, min(int(limit), MAX_ROWS)),
    }
    if underlying:
        clauses.append("underlying = :underlying")
        params["underlying"] = underlying
    if horizon_seconds:
        clauses.append("horizon_seconds = :horizon_seconds")
        params["horizon_seconds"] = int(horizon_seconds)
    if label_status:
        clauses.append("label_status = :label_status")
        params["label_status"] = label_status
    if decidable_only:
        clauses.append("economically_decidable IS TRUE")

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT time, decision_id, underlying, expiry, strike, option_type,
                           horizon_seconds, label_status, label_reason,
                           spot_return_pct, spot_mfe_pct, spot_mae_pct,
                           spot_barrier_hit, spot_barrier_width_pct,
                           spot_forward_lag_seconds, spot_window_complete, spot_tick_count,
                           option_entry_mid, option_forward_price,
                           forward_lag_seconds, forward_sample_count, forward_source,
                           option_gross_return_pct, option_net_return_pct,
                           option_mfe_pct, option_mae_pct,
                           trade_arrived, volume_delta,
                           entry_half_spread_pct, entry_half_spread_measured,
                           exit_half_spread_pct, exit_half_spread_measured,
                           cost_total_rupees, cost_pct_of_notional,
                           breakeven_move_pct, economically_decidable,
                           quantity, lot_size
                      FROM candidate_outcomes
                     WHERE {' AND '.join(clauses)}
                     ORDER BY time DESC, underlying, horizon_seconds, option_type, strike
                     LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def count_matching(
    *,
    table: str,
    session_date: date,
    filters: Optional[dict[str, Any]] = None,
) -> int:
    """How many rows MATCH, ignoring the page limit.

    Returned alongside every listing so the UI can say "showing 300 of 588"
    rather than presenting a truncated page as the whole result. A silent cap
    reads as "this is everything", which is the same class of quiet dishonesty
    the capture pipeline exists to avoid.
    """
    if table not in {"candidate_snapshots", "candidate_outcomes"}:
        raise ValueError(f"refusing to count an unexpected table: {table!r}")
    start, end = _session_bounds(session_date)
    clauses = ["time >= :start", "time < :end", "session_date = :session_date"]
    params: dict[str, Any] = {
        "start": start, "end": end, "session_date": session_date
    }
    for column, value in (filters or {}).items():
        if value is None or value == "":
            continue
        clauses.append(f"{column} = :{column}")
        params[column] = value

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    f"SELECT count(*) FROM {table} WHERE {' AND '.join(clauses)}"
                ),
                params,
            )
        ).first()
    return int(row[0]) if row else 0


async def composition(
    *, session_date: date, underlying: Optional[str] = None
) -> list[dict[str, Any]]:
    """Row counts per underlying for one session — "is everything here?".

    The listing is ordered by underlying, so the first screens are all one name
    and a reader cannot tell whether the others are present or missing. This
    answers that directly instead of asking them to scroll.
    """
    start, end = _session_bounds(session_date)
    clauses = ["time >= :start", "time < :end", "session_date = :session_date"]
    params: dict[str, Any] = {"start": start, "end": end, "session_date": session_date}
    if underlying:
        clauses.append("underlying = :underlying")
        params["underlying"] = underlying

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT underlying,
                           count(*) AS rows,
                           count(*) FILTER (WHERE eligibility_status = 'eligible') AS eligible,
                           count(DISTINCT expiry) AS expiries
                      FROM candidate_snapshots
                     WHERE {' AND '.join(clauses)}
                     GROUP BY underlying
                     ORDER BY underlying
                    """
                ),
                params,
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def filter_options(session_date: Optional[date] = None) -> dict[str, list[Any]]:
    """The distinct values actually present, so the UI offers only real filters."""
    where = ""
    params: dict[str, Any] = {}
    if session_date is not None:
        start, end = _session_bounds(session_date)
        where = "WHERE time >= :start AND time < :end AND session_date = :session_date"
        params = {"start": start, "end": end, "session_date": session_date}

    async with AsyncSessionLocal() as session:
        snap = (
            await session.execute(
                text(
                    f"""
                    SELECT DISTINCT underlying, expiry_class, moneyness,
                           liquidity_bucket, eligibility_status
                      FROM candidate_snapshots {where}
                    """
                ),
                params,
            )
        ).mappings().all()
        horizons = (
            await session.execute(
                text("SELECT DISTINCT horizon_seconds FROM candidate_outcomes ORDER BY 1")
            )
        ).fetchall()
        statuses = (
            await session.execute(
                text("SELECT DISTINCT label_status FROM candidate_outcomes ORDER BY 1")
            )
        ).fetchall()

    def _uniq(key: str) -> list[Any]:
        return sorted({r[key] for r in snap if r[key] is not None})

    return {
        "underlyings": _uniq("underlying"),
        "expiry_classes": _uniq("expiry_class"),
        "moneyness": _uniq("moneyness"),
        "liquidity_buckets": _uniq("liquidity_bucket"),
        "eligibility_statuses": _uniq("eligibility_status"),
        "horizons": [int(r[0]) for r in horizons],
        "label_statuses": [str(r[0]) for r in statuses],
    }


async def list_models(limit: int = 50) -> list[dict[str, Any]]:
    """Model versions newest first, with their full gate results.

    `promotion_gates` is returned whole rather than summarised: a model refused
    on one gate must stay re-judgeable from the stored row if that threshold
    ever moves, and a verdict alone would not allow it.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT version_name, status, model_family, horizon_seconds,
                           underlying_class, expiry_class, target,
                           train_rows, train_sessions, eval_rows, eval_sessions,
                           train_start, train_end, eval_start, eval_end,
                           metrics, promotion_gates, gates_passed,
                           promotion_reason, created_at, promoted_at
                      FROM candidate_model_versions
                     ORDER BY created_at DESC
                     LIMIT :limit
                    """
                ),
                {"limit": max(1, min(int(limit), 200))},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def list_training_runs(limit: int = 30) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, started_at, finished_at, status, reason,
                           requested, produced
                      FROM candidate_training_runs
                     ORDER BY started_at DESC
                     LIMIT :limit
                    """
                ),
                {"limit": max(1, min(int(limit), 200))},
            )
        ).mappings().all()
    return [dict(r) for r in rows]
