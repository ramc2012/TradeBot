"""Reconstruct historical candidates from the option-chain tape — liquid band only.

WHY A BAND, AND WHY THIS ONE
────────────────────────────
No historical table in this schema carries an option bid/ask, so a backfilled
row's spread must be estimated. That is only defensible where the spread is
actually predictable, and measured on the live captures it is predictable in
exactly one place:

    liquidity   moneyness      p50 spread   p90 spread   max
    TOP         |steps| <= 5      0.31%        1.2%       5.0%
    HIGH        |steps| <= 5      0.38%       10.3%      79.3%
    MID         |steps| <= 5     11.9%        29.8%     104.8%

TOP is tight and stable across every moneyness band. HIGH has a usable median
but a fat tail an order of magnitude worse. MID is not a market anyone crosses —
a 12% median spread means the quote carries no information about a fill.

So the band is TOP liquidity within 5 ladder steps of the money. Outside it the
estimate would be doing the work the data cannot support, and a model trained
there would be learning the assumption rather than the market.

THE ESTIMATE IS CONSERVATIVE AND FLAGGED
────────────────────────────────────────
`ASSUMED_SPREAD_PCT` is the band's ~p90, not its median. An edge that survives a
p90 spread is real; one that needs the median to work is an artifact of the
assumption. Every backfilled row sets `spread_pct_estimated = TRUE` and leaves
bid/ask NULL, so the cost model still reports `entry_half_spread_measured=False`
and the two populations never merge silently.

The calibration's provenance is recorded below because it is THIN — one session.
It should be recomputed as live captures accumulate; `measure_spread_calibration`
does that from whatever real quotes exist.

WHAT IS RECONSTRUCTED VS WHAT IS REAL
─────────────────────────────────────
Real, from the tape:  ltp, oi, volume, iv, greeks, spot, and every derived
                      taxonomy field (moneyness, liquidity rank, expiry class)
Estimated:            spread only
Absent:               bid, ask — left NULL, never invented
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from loguru import logger
from sqlalchemy import text

from analysis.instruments import ALL_FO_INDICES, get_fo_market
from candidate_capture.labelling import INDEX_TICK_SYMBOL
from candidate_capture.service import NO_TRADE, SOURCE_CHAIN_CACHE
from candidate_capture.taxonomy import (
    DEFINITION_VERSION,
    chain_liquidity_percentiles,
    classify_contract,
)
from db.database import AsyncSessionLocal
from market_data.strike_ladder import ladder_step

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

BACKFILL_VERSION = "backfill_v1"
SOURCE_BACKFILL = "option_chain_backfill"

SESSION_START_IST = dt_time(9, 15)
SESSION_END_IST = dt_time(15, 30)

# ── the band ───────────────────────────────────────────────────────────────
# Percentile rank within the contract's OWN chain, so it adapts to each chain's
# depth rather than needing a per-underlying absolute threshold.
BAND_MIN_LIQUIDITY_PCTILE = 0.80          # "TOP"
BAND_MAX_MONEYNESS_STEPS = 5.0
BAND_MAX_DTE = 45

# ── spread calibration (see the module docstring) ──────────────────────────
# Full quoted spread as a fraction of mid, at roughly the band's p90.
ASSUMED_SPREAD_PCT = 0.015
CALIBRATION = {
    "assumed_spread_pct": ASSUMED_SPREAD_PCT,
    "basis": "p90 of measured spread_pct within TOP liquidity, |moneyness_steps| <= 5",
    "measured_on": "2026-08-27 live captures",
    "sample_rows": 381,
    "caveat": (
        "ONE session. Conservative by construction (p90, not median) so an edge "
        "that survives it is not an artifact of the assumption. Recompute with "
        "measure_spread_calibration() as live captures accumulate."
    ),
}

# Anchor grid. Matches the live capture cadence so backfilled and captured rows
# are the same kind of observation.
ANCHOR_INTERVAL_SECONDS = 300
# A chain snapshot further than this from the anchor is not that anchor's chain.
MAX_CHAIN_STALENESS_SECONDS = 180


def session_bounds_utc(session_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(session_date, SESSION_START_IST, tzinfo=IST).astimezone(UTC)
    end = datetime.combine(session_date, SESSION_END_IST, tzinfo=IST).astimezone(UTC)
    return start, end


def anchor_grid(session_date: date, interval: int = ANCHOR_INTERVAL_SECONDS) -> list[datetime]:
    start, end = session_bounds_utc(session_date)
    out, cursor = [], start
    while cursor < end:
        out.append(cursor)
        cursor += timedelta(seconds=interval)
    return out


async def measure_spread_calibration() -> dict[str, Any]:
    """Recompute the band's spread quantiles from REAL captured quotes.

    The constant above is only as good as the session it came from. This reads
    whatever measured spreads exist now and reports the band's distribution, so
    the assumption can be refreshed rather than inherited indefinitely.
    """
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS rows,
                           percentile_cont(0.50) WITHIN GROUP (ORDER BY spread_pct) AS p50,
                           percentile_cont(0.90) WITHIN GROUP (ORDER BY spread_pct) AS p90,
                           percentile_cont(0.99) WITHIN GROUP (ORDER BY spread_pct) AS p99
                      FROM candidate_snapshots
                     WHERE spread_pct IS NOT NULL
                       AND spread_pct_estimated IS FALSE
                       AND liquidity_bucket = 'TOP'
                       AND abs(moneyness_steps) <= :max_steps
                       AND option_type <> 'NO_TRADE'
                    """
                ),
                {"max_steps": BAND_MAX_MONEYNESS_STEPS},
            )
        ).mappings().first()
    return dict(row or {})


async def load_spot_path(underlying: str, session_date: date) -> list[tuple[datetime, float]]:
    """The session's index tick path — the anchor spot, exactly as it was."""
    symbol = INDEX_TICK_SYMBOL.get(str(underlying).upper())
    if not symbol:
        return []
    start, end = session_bounds_utc(session_date)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT time, ltp FROM market_ticks
                     WHERE time >= :start AND time < :end AND symbol = :symbol
                       AND ltp IS NOT NULL AND ltp > 0
                     ORDER BY time
                    """
                ),
                {"start": start, "end": end, "symbol": symbol},
            )
        ).fetchall()
    return [(r[0], float(r[1])) for r in rows]


async def load_chain_rows(underlying: str, session_date: date) -> list[dict[str, Any]]:
    """Every chain snapshot for one underlying-session, ascending."""
    symbol = INDEX_TICK_SYMBOL.get(str(underlying).upper())
    if not symbol:
        return []
    start, end = session_bounds_utc(session_date)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT time, expiry, strike, option_type, ltp, oi, volume,
                           iv, delta, gamma, theta, vega
                      FROM option_chain_snapshots
                     WHERE time >= :start AND time < :end AND symbol = :symbol
                       AND ltp IS NOT NULL AND ltp > 0
                     ORDER BY time
                    """
                ),
                {"start": start, "end": end, "symbol": symbol},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def load_listed_expiries(underlying: str) -> list[date]:
    from candidate_capture.service import load_listed_expiries as _listed

    return await _listed(underlying)


def _spot_at(path: Sequence[tuple[datetime, float]], anchor: datetime) -> Optional[float]:
    best: Optional[float] = None
    for stamp, price in path:
        if stamp <= anchor:
            best = price
        else:
            break
    return best


def build_backfill_rows(
    *,
    underlying: str,
    anchor: datetime,
    chain: Sequence[Mapping[str, Any]],
    spot: Optional[float],
    listed_expiries: Sequence[date],
    assumed_spread_pct: float = ASSUMED_SPREAD_PCT,
) -> list[dict[str, Any]]:
    """One anchor's chain -> band-restricted candidate rows. Pure, testable.

    `chain` must already be the snapshot nearest this anchor, for ONE expiry set.
    """
    if not chain:
        return []
    decision_id = str(uuid.uuid4())
    session_date = anchor.astimezone(IST).date()
    exchange = get_fo_market(underlying)

    by_expiry: dict[Any, list[Mapping[str, Any]]] = {}
    for row in chain:
        by_expiry.setdefault(row["expiry"], []).append(row)

    out: list[dict[str, Any]] = []
    for expiry_raw, series in by_expiry.items():
        try:
            expiry = (
                expiry_raw if isinstance(expiry_raw, date)
                else date.fromisoformat(str(expiry_raw)[:10])
            )
        except ValueError:
            continue
        dte = (expiry - session_date).days
        if dte < 0 or dte > BAND_MAX_DTE:
            continue

        step = ladder_step([float(r["strike"]) for r in series])
        percentiles = chain_liquidity_percentiles(
            [{"oi": r.get("oi"), "volume": r.get("volume")} for r in series]
        )

        for row, percentile in zip(series, percentiles):
            taxonomy = classify_contract(
                exchange=exchange,
                underlying=underlying,
                index_symbols=ALL_FO_INDICES,
                expiry=expiry,
                listed_expiries=listed_expiries,
                option_type=str(row.get("option_type") or ""),
                strike=row.get("strike"),
                spot=spot,
                ladder_step=step,
                liquidity_percentile=percentile,
                now=anchor,
            )
            # ── THE BAND ──────────────────────────────────────────────────
            if percentile is None or percentile < BAND_MIN_LIQUIDITY_PCTILE:
                continue
            if (
                taxonomy.moneyness_steps is None
                or abs(taxonomy.moneyness_steps) > BAND_MAX_MONEYNESS_STEPS
            ):
                continue

            ltp = row.get("ltp")
            out.append(
                {
                    "time": anchor,
                    "decision_id": decision_id,
                    "session_date": session_date,
                    "exchange": taxonomy.exchange,
                    "underlying": taxonomy.underlying,
                    "underlying_class": taxonomy.underlying_class,
                    "expiry": taxonomy.expiry,
                    "expiry_class": taxonomy.expiry_class,
                    "expiry_class_reason": taxonomy.expiry_class_reason,
                    "days_to_expiry": taxonomy.days_to_expiry,
                    "hours_to_expiry": taxonomy.hours_to_expiry,
                    "expiry_day_flag": taxonomy.expiry_day_flag,
                    "monthly_expiry_week_flag": taxonomy.monthly_expiry_week_flag,
                    "option_type": taxonomy.option_type,
                    "strike": taxonomy.strike,
                    "moneyness": taxonomy.moneyness,
                    "moneyness_steps": taxonomy.moneyness_steps,
                    "liquidity_bucket": taxonomy.liquidity_bucket,
                    "liquidity_percentile": taxonomy.liquidity_percentile,
                    "lot_size": None,
                    "spot": spot,
                    "ltp": float(ltp) if ltp is not None else None,
                    # NEVER invented. The chain tape has no quote.
                    "bid": None,
                    "ask": None,
                    "spread": None,
                    "spread_pct": assumed_spread_pct,
                    "spread_pct_estimated": True,
                    "volume": int(row["volume"]) if row.get("volume") is not None else None,
                    "oi": int(row["oi"]) if row.get("oi") is not None else None,
                    "oi_change": None,
                    "iv": row.get("iv"),
                    "delta": row.get("delta"),
                    "gamma": row.get("gamma"),
                    "theta": row.get("theta"),
                    "vega": row.get("vega"),
                    "features": {"backfill": BACKFILL_VERSION},
                    "missing_fields": ["bid", "ask", "india_vix", "spread_pct_is_estimated"],
                    "chain_is_stale": False,
                    "chain_quote_age_seconds": None,
                    "eligibility_status": "eligible",
                    "eligibility_reason": None,
                    "is_selected": False,
                    "source": SOURCE_BACKFILL,
                    "capture_version": BACKFILL_VERSION,
                    "definition_version": DEFINITION_VERSION,
                }
            )

    if out:
        # The abstain candidate, so a backfilled decision set has the same shape
        # as a captured one and the label space is identical.
        out.append(
            {
                **{k: None for k in out[0]},
                "time": anchor,
                "decision_id": decision_id,
                "session_date": session_date,
                "exchange": exchange,
                "underlying": str(underlying).upper(),
                "underlying_class": "INDEX",
                "expiry_class": "UNKNOWN",
                "expiry_class_reason": "no_trade_candidate_has_no_contract",
                "option_type": NO_TRADE,
                "moneyness": "UNKNOWN",
                "liquidity_bucket": "UNKNOWN",
                "expiry_day_flag": False,
                "monthly_expiry_week_flag": False,
                "spread_pct_estimated": False,
                "chain_is_stale": False,
                "eligibility_status": "eligible",
                "is_selected": False,
                "spot": spot,
                "features": {"backfill": BACKFILL_VERSION},
                "missing_fields": ["india_vix"],
                "source": SOURCE_BACKFILL,
                "capture_version": BACKFILL_VERSION,
                "definition_version": DEFINITION_VERSION,
            }
        )
    return out


_INSERT = text(
    """
    INSERT INTO candidate_snapshots (
        time, decision_id, session_date, exchange, underlying, underlying_class,
        expiry, expiry_class, expiry_class_reason, days_to_expiry, hours_to_expiry,
        expiry_day_flag, monthly_expiry_week_flag, option_type, strike, moneyness,
        moneyness_steps, liquidity_bucket, liquidity_percentile, lot_size,
        spot, ltp, bid, ask, spread, spread_pct, spread_pct_estimated,
        volume, oi, oi_change, iv, delta, gamma, theta, vega,
        features, missing_fields, chain_is_stale, chain_quote_age_seconds,
        eligibility_status, eligibility_reason, is_selected,
        source, capture_version, definition_version
    ) VALUES (
        :time, CAST(:decision_id AS uuid), :session_date, :exchange, :underlying,
        :underlying_class, :expiry, :expiry_class, :expiry_class_reason,
        :days_to_expiry, :hours_to_expiry, :expiry_day_flag,
        :monthly_expiry_week_flag, :option_type, :strike, :moneyness,
        :moneyness_steps, :liquidity_bucket, :liquidity_percentile, :lot_size,
        :spot, :ltp, :bid, :ask, :spread, :spread_pct, :spread_pct_estimated,
        :volume, :oi, :oi_change, :iv, :delta, :gamma, :theta, :vega,
        CAST(:features AS jsonb), CAST(:missing_fields AS jsonb),
        :chain_is_stale, :chain_quote_age_seconds,
        :eligibility_status, :eligibility_reason, :is_selected,
        :source, :capture_version, :definition_version
    )
    """
)


async def persist(rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    import json

    payload = []
    for row in rows:
        record = dict(row)
        record["features"] = json.dumps(record.get("features") or {})
        record["missing_fields"] = json.dumps(record.get("missing_fields") or [])
        payload.append(record)
    async with AsyncSessionLocal() as session:
        await session.execute(_INSERT, payload)
        await session.commit()
    return len(payload)


async def backfill_session(
    underlying: str, session_date: date, *, persist_rows: bool = True
) -> dict[str, Any]:
    """Reconstruct one underlying-session's band-restricted candidates."""
    chain = await load_chain_rows(underlying, session_date)
    if not chain:
        return {"underlying": underlying, "session": session_date.isoformat(),
                "status": "no_chain_data", "rows": 0}
    spot_path = await load_spot_path(underlying, session_date)
    if not spot_path:
        return {"underlying": underlying, "session": session_date.isoformat(),
                "status": "no_spot_ticks", "rows": 0}
    listed = await load_listed_expiries(underlying)

    by_time: dict[datetime, list[dict[str, Any]]] = {}
    for row in chain:
        by_time.setdefault(row["time"], []).append(row)
    stamps = sorted(by_time)

    all_rows: list[dict[str, Any]] = []
    used = 0
    for anchor in anchor_grid(session_date):
        # Nearest chain snapshot to this anchor, refusing a stale one rather
        # than stretching a distant snapshot across the gap.
        nearest = min(stamps, key=lambda s: abs((s - anchor).total_seconds()), default=None)
        if nearest is None:
            continue
        if abs((nearest - anchor).total_seconds()) > MAX_CHAIN_STALENESS_SECONDS:
            continue
        used += 1
        all_rows.extend(
            build_backfill_rows(
                underlying=underlying,
                anchor=anchor,
                chain=by_time[nearest],
                spot=_spot_at(spot_path, anchor),
                listed_expiries=listed,
            )
        )

    written = await persist(all_rows) if persist_rows else 0
    return {
        "underlying": underlying,
        "session": session_date.isoformat(),
        "status": "ok",
        "anchors_used": used,
        "rows": len(all_rows),
        "written": written,
    }


async def backfill_range(
    underlyings: Sequence[str],
    start: date,
    end: date,
    *,
    persist_rows: bool = True,
) -> dict[str, Any]:
    """Backfill a date range, one underlying-session at a time.

    Serialized deliberately: a concurrent backfill against this database has
    previously produced a "too many clients" storm that wiped a lane's symbol
    list. Throughput is not worth that.
    """
    started = datetime.now(UTC)
    results: list[dict[str, Any]] = []
    day = start
    while day <= end:
        for underlying in underlyings:
            try:
                results.append(
                    await backfill_session(underlying, day, persist_rows=persist_rows)
                )
            except Exception as exc:  # noqa: BLE001 — one bad session must not end the run
                results.append(
                    {"underlying": underlying, "session": day.isoformat(),
                     "status": "failed", "reason": f"{type(exc).__name__}: {exc}", "rows": 0}
                )
                logger.warning("[backfill] {} {} failed: {!r}", underlying, day, exc)
        day += timedelta(days=1)

    total = sum(r.get("rows", 0) for r in results)
    written = sum(r.get("written", 0) for r in results)
    ok = [r for r in results if r.get("status") == "ok"]
    logger.info(
        "[backfill] {}..{} sessions_ok={} rows={} written={}",
        start.isoformat(), end.isoformat(), len(ok), total, written,
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "underlyings": list(underlyings),
        "band": {
            "min_liquidity_percentile": BAND_MIN_LIQUIDITY_PCTILE,
            "max_moneyness_steps": BAND_MAX_MONEYNESS_STEPS,
            "max_dte": BAND_MAX_DTE,
        },
        "calibration": CALIBRATION,
        "sessions_ok": len(ok),
        "sessions_attempted": len(results),
        "rows": total,
        "written": written,
        "per_session": results,
        "elapsed_seconds": round((datetime.now(UTC) - started).total_seconds(), 1),
    }
