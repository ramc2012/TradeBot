"""Bounded reads + the post-close labelling pass.

Split from `labelling.py` so every decision rule in that module stays pure and
unit-testable with no database. This file is only queries and orchestration.

EVERY QUERY HERE BOUNDS `time` DIRECTLY WITH LITERAL UTC INSTANTS.
Wrapping the partitioning column of a hypertable in a function or cast defeats
chunk exclusion and has previously SIGKILLed the live Postgres mid-session; the
60s statement timeout does not protect against it. There is also no fan-out:
the pass is serialized on one session at a time, because a concurrent backfill
against this database has previously produced a "too many clients" storm that
wiped a lane's symbol list.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from loguru import logger
from sqlalchemy import text

import bisect

from candidate_capture.labelling import (
    BARRIER_SIGMA_MULTIPLE,
    DEFAULT_HORIZONS_SECONDS,
    INDEX_TICK_SYMBOL,
    LABEL_VERSION,
    OK,
    SOURCE_CANDIDATE_SNAPSHOTS,
    SOURCE_OPTION_CHAIN_SNAPSHOTS,
    TABLE,
    UNLABELLABLE_NO_SPOT,
    VOL_LOOKBACK_SECONDS,
    ForwardMark,
    SpotPath,
    barrier_width_for_horizon,
    build_outcome_row,
    build_spot_path,
    realized_vol_per_sqrt_second,
    select_forward_mark,
    tolerance_window,
)
from db.database import AsyncSessionLocal

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# NSE cash + F&O session, in IST. Used only to build literal UTC query bounds.
SESSION_START_IST = dt_time(9, 15)
SESSION_END_IST = dt_time(15, 30)


def session_bounds_utc(
    session_date: date, *, pad_after_seconds: int = 0
) -> tuple[datetime, datetime]:
    """[start, end) of one NSE session in UTC.

    `pad_after_seconds` DEFAULTS TO ZERO and every forward reader relies on
    that. An earlier version padded the forward windows by an hour so that a
    60-minute horizon on a late anchor would still "find" a mark — but the
    exchange is shut after 15:30, so what it found was the last print of the
    session repeated under a post-close timestamp. That is a fabricated
    observation: it reads as "the price held for an hour" when in fact nothing
    traded. Windows now stop at the close, and a horizon that runs past it is
    reported as a truncated window (see SpotPath.window_complete and
    `forward_lag_seconds`) rather than silently completed.
    """
    start = datetime.combine(session_date, SESSION_START_IST, tzinfo=IST).astimezone(UTC)
    end = datetime.combine(session_date, SESSION_END_IST, tzinfo=IST).astimezone(UTC)
    return start, end + timedelta(seconds=pad_after_seconds)


async def load_anchors(session_date: date) -> list[dict[str, Any]]:
    """Every candidate row captured in one session — the rows to be labelled."""
    start, end = session_bounds_utc(session_date)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT time, decision_id, underlying, underlying_class,
                           expiry, strike, option_type,
                           ltp, bid, ask, volume, oi, spot, lot_size,
                           eligibility_status
                      FROM candidate_snapshots
                     WHERE time >= :start AND time < :end
                     ORDER BY underlying, time
                    """
                ),
                {"start": start, "end": end},
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def load_spot_ticks(
    underlying: str, session_date: date
) -> list[tuple[datetime, float]]:
    """The whole session's index tick path, ascending.

    One bounded read per underlying per session rather than one per anchor:
    there are ~90,000 ticks in a session and potentially thousands of anchors,
    so re-querying per anchor would be thousands of scans of the same chunk.
    """
    symbol = INDEX_TICK_SYMBOL.get(str(underlying).upper())
    if not symbol:
        return []
    start, end = session_bounds_utc(session_date)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT time, ltp
                      FROM market_ticks
                     WHERE time >= :start AND time < :end
                       AND symbol = :symbol
                       AND ltp IS NOT NULL
                       AND ltp > 0
                     ORDER BY time
                    """
                ),
                {"start": start, "end": end, "symbol": symbol},
            )
        ).fetchall()
    return [(r[0], float(r[1])) for r in rows]


async def load_forward_option_samples(
    *, underlying: str, session_date: date
) -> dict[tuple[Any, float, str], list[dict[str, Any]]]:
    """Forward marks for every contract of one underlying, keyed by contract.

    TWO SOURCES, PREFERRING THE ONE THAT CARRIES A QUOTE:

    `candidate_snapshots` is the better forward source when it has the contract,
    because it stores bid/ask — which is the ONLY way the exit half-spread is
    ever a measurement rather than an assumption. Its cadence is the capture
    interval (300s).

    `option_chain_snapshots` is the fallback: denser in principle (~140s median)
    but LTP-only, one expiry per underlying, and prone to whole-session silent
    outages.

    Both are keyed on the full logical contract — (underlying, expiry, strike,
    option_type) — never on a broker instrument key.
    """
    start, end = session_bounds_utc(session_date)
    out: dict[tuple[Any, float, str], list[dict[str, Any]]] = {}

    async with AsyncSessionLocal() as session:
        own = (
            await session.execute(
                text(
                    """
                    SELECT time, expiry, strike, option_type,
                           ltp, bid, ask, volume, oi
                      FROM candidate_snapshots
                     WHERE time >= :start AND time < :end
                       AND underlying = :underlying
                       AND option_type IN ('CE','PE')
                     ORDER BY time
                    """
                ),
                {"start": start, "end": end, "underlying": underlying},
            )
        ).mappings().all()

        chain_symbol = INDEX_TICK_SYMBOL.get(str(underlying).upper())
        chain = (
            (
                await session.execute(
                    text(
                        """
                        SELECT time, expiry, strike, option_type, ltp, oi, volume
                          FROM option_chain_snapshots
                         WHERE time >= :start AND time < :end
                           AND symbol = :symbol
                         ORDER BY time
                        """
                    ),
                    {"start": start, "end": end, "symbol": chain_symbol},
                )
            ).mappings().all()
            if chain_symbol
            else []
        )

    for row in own:
        price = row["ltp"]
        bid, ask = row["bid"], row["ask"]
        if bid and ask and ask >= bid:
            price = (float(bid) + float(ask)) / 2.0
        if price is None:
            continue
        key = (row["expiry"], float(row["strike"]), str(row["option_type"]))
        out.setdefault(key, []).append(
            {
                "time": row["time"],
                "price": float(price),
                "bid": bid,
                "ask": ask,
                "volume": row["volume"],
                "oi": row["oi"],
                "source": SOURCE_CANDIDATE_SNAPSHOTS,
            }
        )

    for row in chain:
        if row["ltp"] is None:
            continue
        try:
            expiry = date.fromisoformat(str(row["expiry"])[:10])
        except ValueError:
            continue
        key = (expiry, float(row["strike"]), str(row["option_type"]))
        out.setdefault(key, []).append(
            {
                "time": row["time"],
                "price": float(row["ltp"]),
                "bid": None,
                "ask": None,
                "volume": row["volume"],
                "oi": row["oi"],
                "source": SOURCE_OPTION_CHAIN_SNAPSHOTS,
            }
        )

    for samples in out.values():
        samples.sort(key=lambda s: s["time"])
    return out


async def option_source_dark(underlying: str, session_date: date) -> bool:
    """Did the forward option source produce NOTHING for this whole session?

    A real and silent failure mode: on 2026-08-19 there were 103,440 index ticks
    and 48,205 option candles but ZERO option_chain_snapshots rows. Without this
    precondition every contract that session labels as "no forward mark", which
    reads as contracts that stopped trading rather than a dead source.
    """
    symbol = INDEX_TICK_SYMBOL.get(str(underlying).upper())
    start, end = session_bounds_utc(session_date)
    async with AsyncSessionLocal() as session:
        # An underlying with no INDEX_TICK_SYMBOL entry has no chain-snapshot
        # source, but `load_forward_option_samples` still reads OUR OWN
        # snapshots for it unconditionally. Early-returning True here would
        # therefore mark every such row source_dark while real forward marks
        # were sitting in the loader — the two must agree, so the chain probe is
        # skipped and the own-snapshot probe still decides.
        chain = ((
            await session.execute(
                text(
                    """
                    SELECT 1 FROM option_chain_snapshots
                     WHERE time >= :start AND time < :end AND symbol = :symbol
                     LIMIT 1
                    """
                ),
                {"start": start, "end": end, "symbol": symbol},
            )
        ).first() if symbol else None)
        _unused_chain = (
            None
        )
        own = (
            await session.execute(
                text(
                    """
                    SELECT 1 FROM candidate_snapshots
                     WHERE time >= :start AND time < :end
                       AND underlying = :underlying
                       AND option_type IN ('CE','PE')
                     LIMIT 1
                    """
                ),
                {"start": start, "end": end, "underlying": underlying},
            )
        ).first()
    return chain is None and own is None


async def load_lot_sizes(underlying: str) -> dict[tuple[Any, float, str], int]:
    """Catalog lot size per contract.

    Captured onto the label rather than joined at read time: cost as a fraction
    of premium is lot-size dependent (the flat per-order brokerage divides by
    lot x premium), and the catalog's lot size changes between expiries, so a
    later join would silently apply today's lot to an older row.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT expiry, strike::float8 AS strike, option_type, lot_size
                      FROM fo_contract_catalog
                     WHERE underlying = :underlying
                       AND lot_size IS NOT NULL
                    """
                ),
                {"underlying": underlying},
            )
        ).mappings().all()
    return {
        (r["expiry"], float(r["strike"]), str(r["option_type"])): int(r["lot_size"])
        for r in rows
    }


def _slice_forward(
    ticks: Sequence[tuple[datetime, float]],
    anchor_time: datetime,
    horizon_seconds: int,
    stamps: Optional[Sequence[datetime]] = None,
) -> list[tuple[datetime, float]]:
    """Ticks in (anchor, anchor + horizon].

    Binary search over a precomputed timestamp list rather than a scan. A
    session carries ~90,000 index ticks and a session's capture can carry
    thousands of anchors x four horizons; scanning the full list each time made
    the pass quadratic in the largest thing it holds.
    """
    end = anchor_time + timedelta(seconds=horizon_seconds)
    if stamps is None:
        return [(t, p) for t, p in ticks if anchor_time < t <= end]
    lo = bisect.bisect_right(stamps, anchor_time)
    hi = bisect.bisect_right(stamps, end)
    return list(ticks[lo:hi])


def _entry_mid(anchor: Mapping[str, Any]) -> Optional[float]:
    """Mid when the book is two-sided and uncrossed, else the last print."""
    bid, ask = anchor.get("bid"), anchor.get("ask")
    if bid and ask and float(ask) >= float(bid):
        return (float(bid) + float(ask)) / 2.0
    ltp = anchor.get("ltp")
    return float(ltp) if ltp is not None else None


def _spot_at(
    ticks: Sequence[tuple[datetime, float]],
    anchor_time: datetime,
    stamps: Optional[Sequence[datetime]] = None,
) -> Optional[float]:
    """The last tick at or before the anchor — the price that was actually live."""
    if stamps is not None:
        idx = bisect.bisect_right(stamps, anchor_time)
        return ticks[idx - 1][1] if idx > 0 else None
    best: Optional[float] = None
    for t, p in ticks:
        if t <= anchor_time:
            best = p
        else:
            break
    return best


def _anchor_sigma(
    ticks: Sequence[tuple[datetime, float]],
    anchor_time: datetime,
    stamps: Optional[Sequence[datetime]] = None,
) -> Optional[float]:
    """Per-sqrt-second volatility from ticks BEFORE the anchor only.

    Strictly backward-looking: a barrier whose width was set using the move it
    is meant to detect is the classic lookahead leak, and this repo has already
    shipped one lookahead-tinged backtest it now distrusts.

    Returns the VOLATILITY, not a width — the width depends on the horizon and
    is therefore computed per horizon by `barrier_width_for_horizon`.
    """
    window_start = anchor_time - timedelta(seconds=VOL_LOOKBACK_SECONDS)
    if stamps is not None:
        lo = bisect.bisect_left(stamps, window_start)
        hi = bisect.bisect_right(stamps, anchor_time)
        prior = list(ticks[lo:hi])
    else:
        prior = [(t, p) for t, p in ticks if window_start <= t <= anchor_time]
    return realized_vol_per_sqrt_second(prior)


_UPSERT = text(
    f"""
    INSERT INTO {TABLE} (
        time, decision_id, session_date, underlying, expiry, strike, option_type,
        horizon_seconds, label_status, label_reason,
        spot_entry, spot_forward, spot_return_pct, spot_mfe_pct, spot_mae_pct,
        spot_barrier_hit, spot_time_to_barrier_seconds, spot_barrier_width_pct,
        spot_tick_count, spot_forward_lag_seconds, spot_window_complete,
        option_entry_mid, option_forward_price, forward_lag_seconds,
        forward_sample_count, forward_source,
        option_gross_return_pct, option_net_return_pct,
        option_mfe_pct, option_mae_pct,
        trade_arrived, volume_delta, oi_delta,
        entry_half_spread_pct, entry_half_spread_measured,
        exit_half_spread_pct, exit_half_spread_measured,
        cost_spread_rupees, cost_statutory_rupees, cost_total_rupees,
        cost_pct_of_notional, breakeven_move_pct, economically_decidable,
        quantity, lot_size, label_version, computed_at
    ) VALUES (
        :time, CAST(:decision_id AS uuid), :session_date, :underlying, :expiry,
        :strike, :option_type,
        :horizon_seconds, :label_status, :label_reason,
        :spot_entry, :spot_forward, :spot_return_pct, :spot_mfe_pct, :spot_mae_pct,
        :spot_barrier_hit, :spot_time_to_barrier_seconds, :spot_barrier_width_pct,
        :spot_tick_count, :spot_forward_lag_seconds, :spot_window_complete,
        :option_entry_mid, :option_forward_price, :forward_lag_seconds,
        :forward_sample_count, :forward_source,
        :option_gross_return_pct, :option_net_return_pct,
        :option_mfe_pct, :option_mae_pct,
        :trade_arrived, :volume_delta, :oi_delta,
        :entry_half_spread_pct, :entry_half_spread_measured,
        :exit_half_spread_pct, :exit_half_spread_measured,
        :cost_spread_rupees, :cost_statutory_rupees, :cost_total_rupees,
        :cost_pct_of_notional, :breakeven_move_pct, :economically_decidable,
        :quantity, :lot_size, :label_version, now()
    )
    ON CONFLICT (time, decision_id, underlying, option_type,
                 COALESCE(strike, -1), COALESCE(expiry, DATE '1900-01-01'),
                 horizon_seconds)
    DO UPDATE SET
        label_status = EXCLUDED.label_status,
        label_reason = EXCLUDED.label_reason,
        spot_entry = EXCLUDED.spot_entry,
        spot_forward = EXCLUDED.spot_forward,
        spot_return_pct = EXCLUDED.spot_return_pct,
        spot_mfe_pct = EXCLUDED.spot_mfe_pct,
        spot_mae_pct = EXCLUDED.spot_mae_pct,
        spot_barrier_hit = EXCLUDED.spot_barrier_hit,
        spot_time_to_barrier_seconds = EXCLUDED.spot_time_to_barrier_seconds,
        spot_barrier_width_pct = EXCLUDED.spot_barrier_width_pct,
        spot_tick_count = EXCLUDED.spot_tick_count,
        spot_forward_lag_seconds = EXCLUDED.spot_forward_lag_seconds,
        spot_window_complete = EXCLUDED.spot_window_complete,
        option_entry_mid = EXCLUDED.option_entry_mid,
        option_forward_price = EXCLUDED.option_forward_price,
        forward_lag_seconds = EXCLUDED.forward_lag_seconds,
        forward_sample_count = EXCLUDED.forward_sample_count,
        forward_source = EXCLUDED.forward_source,
        option_gross_return_pct = EXCLUDED.option_gross_return_pct,
        option_net_return_pct = EXCLUDED.option_net_return_pct,
        option_mfe_pct = EXCLUDED.option_mfe_pct,
        option_mae_pct = EXCLUDED.option_mae_pct,
        trade_arrived = EXCLUDED.trade_arrived,
        volume_delta = EXCLUDED.volume_delta,
        oi_delta = EXCLUDED.oi_delta,
        entry_half_spread_pct = EXCLUDED.entry_half_spread_pct,
        entry_half_spread_measured = EXCLUDED.entry_half_spread_measured,
        exit_half_spread_pct = EXCLUDED.exit_half_spread_pct,
        exit_half_spread_measured = EXCLUDED.exit_half_spread_measured,
        cost_spread_rupees = EXCLUDED.cost_spread_rupees,
        cost_statutory_rupees = EXCLUDED.cost_statutory_rupees,
        cost_total_rupees = EXCLUDED.cost_total_rupees,
        cost_pct_of_notional = EXCLUDED.cost_pct_of_notional,
        breakeven_move_pct = EXCLUDED.breakeven_move_pct,
        economically_decidable = EXCLUDED.economically_decidable,
        quantity = EXCLUDED.quantity,
        lot_size = EXCLUDED.lot_size,
        label_version = EXCLUDED.label_version,
        computed_at = now()
    """
)


async def persist_outcomes(rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    async with AsyncSessionLocal() as session:
        await session.execute(_UPSERT, [dict(r) for r in rows])
        await session.commit()
    return len(rows)


async def label_session(
    session_date: date,
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS_SECONDS,
    persist: bool = True,
) -> dict[str, Any]:
    """Label every candidate captured in one session, at every horizon."""
    started = datetime.now(UTC)
    anchors = await load_anchors(session_date)
    if not anchors:
        return {
            "status": "no_anchors",
            "session_date": session_date.isoformat(),
            "result_count": 0,
        }

    by_underlying: dict[str, list[dict[str, Any]]] = {}
    for anchor in anchors:
        by_underlying.setdefault(str(anchor["underlying"]), []).append(anchor)

    all_rows: list[dict[str, Any]] = []
    per_underlying: dict[str, Any] = {}

    # Serialized per underlying on purpose — see the module docstring.
    for underlying, rows in by_underlying.items():
        ticks = await load_spot_ticks(underlying, session_date)
        # Precomputed once so every slice below is a binary search.
        stamps = [t for t, _ in ticks]
        dark = await option_source_dark(underlying, session_date)
        forward = await load_forward_option_samples(
            underlying=underlying, session_date=session_date
        )
        lots = await load_lot_sizes(underlying)

        built = 0
        for anchor in rows:
            key = (
                anchor.get("expiry"),
                float(anchor["strike"]) if anchor.get("strike") is not None else None,
                str(anchor["option_type"]),
            )
            samples = forward.get(key, []) if key[1] is not None else []
            lot_size = lots.get(key) if key[1] is not None else None
            anchor_spot = anchor.get("spot") or _spot_at(ticks, anchor["time"], stamps)
            sigma = _anchor_sigma(ticks, anchor["time"], stamps)

            # Give the event loop a chance between anchors: the per-anchor work
            # is synchronous, and without a yield the whole underlying is one
            # uninterruptible stretch that the supervisor's timeout cannot cut.
            if built and built % 200 == 0:
                await asyncio.sleep(0)

            for horizon in horizons:
                width = barrier_width_for_horizon(sigma, horizon)
                spot_path = build_spot_path(
                    anchor_price=anchor_spot,
                    forward_ticks=_slice_forward(ticks, anchor["time"], horizon, stamps),
                    anchor_time=anchor["time"],
                    horizon_seconds=horizon,
                    barrier_width_pct=width,
                )
                window_end = anchor["time"] + timedelta(
                    seconds=tolerance_window(horizon)[1]
                )
                mark = select_forward_mark(
                    samples=[s for s in samples if s["time"] <= window_end],
                    anchor_time=anchor["time"],
                    # Same crossed-book guard build_outcome_row uses. Without
                    # ask >= bid here, a crossed quote measured option_mfe/mae
                    # against a crossed mid while gross/net/cost were measured
                    # against ltp — two different entry prices on one row.
                    anchor_price=_entry_mid(anchor),
                    horizon_seconds=horizon,
                )
                all_rows.append(
                    build_outcome_row(
                        anchor=anchor,
                        horizon_seconds=horizon,
                        spot=spot_path,
                        mark=mark,
                        lot_size=lot_size,
                        source_dark=dark,
                    )
                )
                built += 1

        per_underlying[underlying] = {
            "anchors": len(rows),
            "rows": built,
            "spot_ticks": len(ticks),
            "option_source_dark": dark,
            "contracts_with_forward": len(forward),
        }

    written = await persist_outcomes(all_rows) if persist else 0

    by_status: dict[str, int] = {}
    for row in all_rows:
        by_status[row["label_status"]] = by_status.get(row["label_status"], 0) + 1

    summary = {
        "status": "ok",
        "session_date": session_date.isoformat(),
        "result_count": written,
        "rows_built": len(all_rows),
        "anchors": len(anchors),
        "horizons": list(horizons),
        "by_label_status": by_status,
        "by_underlying": per_underlying,
        "label_version": LABEL_VERSION,
        "elapsed_seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
    }
    logger.info(
        "[candidate-label] {} anchors={} rows={} written={} statuses={}",
        session_date.isoformat(), len(anchors), len(all_rows), written, by_status,
    )
    return summary


async def run_candidate_labelling(now: Optional[datetime] = None) -> dict[str, Any]:
    """Runner entry point. Flag-gated OFF by default.

    Fires after the session close so the forward windows it needs are already
    complete. Labels TODAY only; a re-run repairs rows in place rather than
    duplicating them.
    """
    from core.config import settings

    if not bool(getattr(settings, "CANDIDATE_LABELLING_ENABLED", False)):
        return {"status": "disabled", "flag": "CANDIDATE_LABELLING_ENABLED"}
    now = (now or datetime.now(IST)).astimezone(IST)
    return await label_session(now.date())
