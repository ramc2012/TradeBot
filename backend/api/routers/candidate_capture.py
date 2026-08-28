"""Read-only surface over the candidate-capture research pipeline.

Answers the question that matters during collection: is this gathering data
worth training on, or is it quietly gathering nothing? The lanes board already
shows whether the two runners are alive; these endpoints show whether what they
produced is honest — coverage gaps, assumed-versus-measured spreads, marks where
no trade actually arrived, and whether any horizon is economically decidable for
any contract class.

Strictly read-only. Nothing here enables a flag, triggers a run, or writes a
row; the pipeline's own runners are the only writers.

Follows the house pattern for a read-only desk surface (see the /preopen routes
in api/routers/market.py): raw dicts rather than response models, heavy modules
imported lazily inside the handler, bounded Query params, and an explicit 400
on bad input rather than a silent empty result.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/candidate-capture", tags=["candidate-capture"])


def _parse_session_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"session_date must be an ISO date (YYYY-MM-DD), got {value!r}",
        ) from exc


@router.get("/readiness")
async def readiness(
    min_sessions: int = Query(10, ge=1, le=200),
) -> dict[str, Any]:
    """Is there yet enough labelled data to justify training a first model?

    Reports readiness and names its blockers; it never trains anything. The
    plan's own sequence puts 10-20 sessions of collection before a first
    baseline, and this is the gate that says whether that has happened.
    """
    from candidate_capture.reports import readiness_report

    return await readiness_report(min_sessions=min_sessions)


@router.get("/coverage")
async def coverage(
    since: Optional[str] = Query(None, description="ISO date; omit for all sessions"),
    limit_sessions: int = Query(30, ge=1, le=200),
) -> dict[str, Any]:
    """Per-session capture health — what was captured and what was missing."""
    from candidate_capture.reports import coverage_report

    return await coverage_report(
        since=_parse_session_date(since) if since else None,
        limit_sessions=limit_sessions,
    )


@router.get("/decidability")
async def decidability(
    since: Optional[str] = Query(None, description="ISO date; omit for all sessions"),
) -> dict[str, Any]:
    """Which (horizon x contract class) strata can clear their own cost.

    A stratum with too few rows reports a NULL rate and the reason, rather than
    a percentage computed over a handful of observations.
    """
    from candidate_capture.reports import decidability_report

    return await decidability_report(
        since=_parse_session_date(since) if since else None
    )


@router.get("/sessions")
async def sessions(limit: int = Query(60, ge=1, le=200)) -> dict[str, Any]:
    """Sessions that have captured data, newest first — the explorer's index."""
    from candidate_capture.reports import list_sessions

    rows = await list_sessions(limit=limit)
    return {"count": len(rows), "sessions": rows}


@router.get("/filters")
async def filters(
    session_date: Optional[str] = Query(None, description="ISO date; omit for all"),
) -> dict[str, Any]:
    """Distinct values actually present, so the UI only offers real filters."""
    from candidate_capture.reports import filter_options

    return await filter_options(
        _parse_session_date(session_date) if session_date else None
    )


@router.get("/models")
async def models(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    """Model versions and the gate results behind each verdict.

    A refusal is as informative as a promotion here — it names which gate the
    model failed and by how much — so refused versions are returned too.
    """
    from candidate_capture.reports import list_models

    rows = await list_models(limit=limit)
    return {"count": len(rows), "models": rows}


@router.get("/training-runs")
async def training_runs(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    """Training run history, including runs that promoted nothing."""
    from candidate_capture.reports import list_training_runs

    rows = await list_training_runs(limit=limit)
    return {"count": len(rows), "runs": rows}


@router.get("/method")
async def method() -> dict[str, Any]:
    """What the model is fed, what it is asked, and how inputs are coded.

    Read from the code that actually runs, so the card cannot drift from the
    implementation. A refusal is not reviewable without it.
    """
    from candidate_capture.reports import method_card

    return method_card()


@router.get("/direction")
async def direction(
    session_date: Optional[str] = Query(None, description="ISO date; omit for all"),
) -> dict[str, Any]:
    """Confirmed-direction outcomes by horizon — the Stage A label.

    Direction is confirmed only when the move clears BOTH a volatility bar and
    an efficiency bar, so chop that happens to close positive is not counted.
    """
    from candidate_capture.reports import direction_report

    return await direction_report(
        session_date=_parse_session_date(session_date) if session_date else None
    )


@router.get("/snapshots")
async def snapshots(
    session_date: str = Query(..., description="ISO date of the capture session"),
    underlying: Optional[str] = None,
    expiry_class: Optional[str] = None,
    moneyness: Optional[str] = None,
    liquidity_bucket: Optional[str] = None,
    eligibility_status: Optional[str] = None,
    include_no_trade: bool = True,
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    """Captured candidates for one session, filtered.

    Rejected contracts are present with their rejection reason — they were
    recorded rather than dropped, and the UI should show them as such.
    """
    from candidate_capture.reports import composition, count_matching, list_snapshots

    parsed = _parse_session_date(session_date)
    rows = await list_snapshots(
        session_date=parsed,
        underlying=underlying,
        expiry_class=expiry_class,
        moneyness=moneyness,
        liquidity_bucket=liquidity_bucket,
        eligibility_status=eligibility_status,
        include_no_trade=include_no_trade,
        limit=limit,
    )
    total = await count_matching(
        table="candidate_snapshots",
        session_date=parsed,
        filters={
            "underlying": underlying,
            "expiry_class": expiry_class,
            "moneyness": moneyness,
            "liquidity_bucket": liquidity_bucket,
            "eligibility_status": eligibility_status,
        },
    )
    return {
        "session_date": parsed.isoformat(),
        "count": len(rows),
        "total": total,
        "truncated": total > len(rows),
        "composition": await composition(session_date=parsed, underlying=underlying),
        "rows": rows,
    }


@router.get("/outcomes")
async def outcomes(
    session_date: str = Query(..., description="ISO date of the capture session"),
    underlying: Optional[str] = None,
    horizon_seconds: Optional[int] = Query(None, ge=1),
    label_status: Optional[str] = None,
    decidable_only: bool = False,
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    """Labelled outcomes for one session, filtered.

    Unlabellable rows are returned with their status and reason rather than
    filtered out — the count of what could NOT be labelled is as informative as
    the labels themselves.
    """
    from candidate_capture.reports import count_matching, list_outcomes

    parsed = _parse_session_date(session_date)
    rows = await list_outcomes(
        session_date=parsed,
        underlying=underlying,
        horizon_seconds=horizon_seconds,
        label_status=label_status,
        decidable_only=decidable_only,
        limit=limit,
    )
    total = await count_matching(
        table="candidate_outcomes",
        session_date=parsed,
        filters={
            "underlying": underlying,
            "horizon_seconds": horizon_seconds,
            "label_status": label_status,
        },
    )
    return {
        "session_date": parsed.isoformat(),
        "count": len(rows),
        "total": total,
        "truncated": total > len(rows),
        "rows": rows,
    }
