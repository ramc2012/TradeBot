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
import math
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from loguru import logger
from sqlalchemy import text

import bisect

from candidate_capture.labelling import (
    ALL_HORIZONS_SECONDS,
    SESSION_HORIZON_TOLERANCE_SECONDS,
    BARRIER_SIGMA_MULTIPLE,
    DEFAULT_HORIZONS_SECONDS,
    SESSION_HORIZONS,
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
    session_target_instant,
    tolerance_window,
)
from db.database import AsyncSessionLocal

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# NSE cash + F&O session, in IST. Used only to build literal UTC query bounds.
SESSION_START_IST = dt_time(9, 15)
SESSION_END_IST = dt_time(15, 30)


def forward_bounds_utc(
    session_date: date, *, forward_days: int = 0
) -> tuple[datetime, datetime]:
    """[session start, end of the window a multi-day horizon needs).

    A session horizon looks at LATER sessions, so the forward readers must span
    them. `forward_days` is padded generously in CALENDAR days because the
    target lands N TRADING sessions out, and weekends plus holidays can put a
    3-session horizon more than a week away.
    """
    start, end = session_bounds_utc(session_date)
    if forward_days <= 0:
        return start, end
    return start, end + timedelta(days=forward_days)


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
                           ltp, bid, ask, spread_pct, spread_pct_estimated,
                           volume, oi, spot, lot_size,
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
    underlying: str, session_date: date, *, forward_days: int = 0
) -> list[tuple[datetime, float]]:
    """The whole session's index tick path, ascending.

    One bounded read per underlying per session rather than one per anchor:
    there are ~90,000 ticks in a session and potentially thousands of anchors,
    so re-querying per anchor would be thousands of scans of the same chunk.
    """
    symbol = INDEX_TICK_SYMBOL.get(str(underlying).upper())
    if not symbol:
        return []
    start, end = forward_bounds_utc(session_date, forward_days=forward_days)
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


def _merge_windows(
    windows: Sequence[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Collapse overlapping [start, end) windows so the query stays small."""
    ordered = sorted(windows)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


# The broker-history sources. `live_tick` and the tick-derived aggregates carry
# the documented cross-symbol contamination (measured here: 2x the true 1-minute
# range), so a path built from them would report excursions that never happened.
SPOT_CANDLE_SOURCES = ("upstox_spot_index", "fyers_spot_index", "upstox_spot", "upstox")


async def load_spot_bars(
    underlying: str, session_date: date, forward_days: int
) -> list[tuple[datetime, float]]:
    """Multi-day spot path from 1-minute candles, expanded to capture extremes.

    Ticks are the right source INTRADAY, but a 3-session horizon needs ~450k of
    them per underlying-session and that does not finish. One-minute bars over
    the same span are ~1,900 rows and lose nothing that matters at a day scale.

    Each bar contributes its LOW and HIGH as separate points so MFE/MAE still
    see the true extremes; within-bar ordering is arbitrary, which only affects
    first-touch resolution inside a single minute.
    """
    start, end = forward_bounds_utc(session_date, forward_days=forward_days)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT time, low, high, close
                      FROM underlying_spot_candles
                     WHERE time >= :start AND time < :end
                       AND underlying = :underlying
                       AND interval = '1minute'
                       AND source = ANY(:sources)
                       AND low IS NOT NULL AND high IS NOT NULL AND close IS NOT NULL
                     ORDER BY time
                    """
                ),
                {
                    "start": start, "end": end, "underlying": underlying,
                    "sources": list(SPOT_CANDLE_SOURCES),
                },
            )
        ).fetchall()
    path: list[tuple[datetime, float]] = []
    for stamp, low, high, close in rows:
        path.append((stamp, float(low)))
        path.append((stamp, float(high)))
        path.append((stamp, float(close)))
    return path


DAILY_VOL_LOOKBACK_SESSIONS = 20
MIN_DAILY_VOL_SESSIONS = 10


async def load_daily_closes(
    underlying: str, session_date: date, lookback: int = DAILY_VOL_LOOKBACK_SESSIONS
) -> list[float]:
    """Trailing session closes, oldest first — the basis for a MULTI-DAY barrier.

    A session horizon must not take its barrier from intraday volatility scaled
    by sqrt(t). Measured on this data, that extrapolation overstates the barrier
    so badly that the confirmed-direction rate collapses from ~10% intraday to
    ~0% at three days: mean |sigma| falls 0.45 -> 0.09 purely because the
    denominator grows faster than any real move.

    Correcting calendar seconds to TRADING seconds only halves the error, which
    is itself the finding: intraday variance does not scale to multi-day
    variance, because index moves mean-revert within the day. So multi-day
    volatility is measured from actual close-to-close returns instead of
    extrapolated.
    """
    end = datetime.combine(session_date, SESSION_START_IST, tzinfo=IST).astimezone(UTC)
    start = end - timedelta(days=lookback * 2 + 10)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT (time AT TIME ZONE 'Asia/Kolkata')::date AS sess,
                           (ARRAY_AGG(close ORDER BY time DESC))[1] AS close
                      FROM underlying_spot_candles
                     WHERE time >= :start AND time < :end
                       AND underlying = :underlying
                       AND interval IN ('30minute', '1minute')
                       AND source = ANY(:sources)
                       AND close IS NOT NULL
                     GROUP BY 1 ORDER BY 1
                    """
                ),
                {
                    "start": start, "end": end, "underlying": underlying,
                    "sources": list(SPOT_CANDLE_SOURCES),
                },
            )
        ).fetchall()
    return [float(r[1]) for r in rows if r[1] is not None][-lookback:]


def daily_sigma(closes: Sequence[float]) -> Optional[float]:
    """Std-dev of close-to-close log returns; None below a usable sample."""
    usable = [c for c in closes if c and c > 0]
    if len(usable) < MIN_DAILY_VOL_SESSIONS:
        return None
    rets = [
        math.log(cur / prev) for prev, cur in zip(usable, usable[1:]) if prev > 0 and cur > 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) if var > 0 else None


async def load_forward_option_samples(
    *,
    underlying: str,
    session_date: date,
    forward_days: int = 0,
    extra_windows: Optional[Sequence[tuple[datetime, datetime]]] = None,
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
    # TARGETED WINDOWS, NOT A BULK MULTI-DAY LOAD.
    #
    # A session horizon points at a handful of specific instants days away, so
    # the forward data it needs is a few hours around each — not every row in
    # between. Loading the full span was ~10 days x 75k rows per underlying-
    # session and did not finish; the union of narrow windows is a small
    # fraction of that and answers exactly the same question.
    windows = [session_bounds_utc(session_date)]
    if extra_windows:
        windows.extend(extra_windows)
    windows = _merge_windows(windows)
    # Bound by the UNION of windows, not their envelope. An earlier version
    # merged the windows and then queried min(start)..max(end), which for a
    # 3-session horizon spans five days — the whole cost the windows exist to
    # avoid. One session took 70 minutes that way.
    where_time = " OR ".join(
        f"(time >= :w{i}s AND time < :w{i}e)" for i in range(len(windows))
    )
    window_params: dict[str, Any] = {}
    for i, (w_start, w_end) in enumerate(windows):
        window_params[f"w{i}s"] = w_start
        window_params[f"w{i}e"] = w_end
    out: dict[tuple[Any, float, str], list[dict[str, Any]]] = {}

    async with AsyncSessionLocal() as session:
        own = (
            await session.execute(
                text(
                    """
                    SELECT time, expiry, strike, option_type,
                           ltp, bid, ask, volume, oi
                      FROM candidate_snapshots
                     WHERE (""" + where_time + """)
                       AND underlying = :underlying
                       AND option_type IN ('CE','PE')
                     ORDER BY time
                    """
                ),
                {**window_params, "underlying": underlying},
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
                         WHERE (""" + where_time + """)
                           AND symbol = :symbol
                         ORDER BY time
                        """
                    ),
                    {**window_params, "symbol": chain_symbol},
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
    exact = {
        (r["expiry"], float(r["strike"]), str(r["option_type"])): int(r["lot_size"])
        for r in rows
    }
    # MODAL FALLBACK for contracts the catalog no longer lists.
    #
    # fo_contract_catalog holds only FORWARD contracts, so a backfilled row on
    # an expiry that has already passed resolves nothing — measured at ~20% of
    # reconstructed rows. Without a lot size the quantity is 0, the trade is
    # uncostable, and the row silently drops out of training.
    #
    # The modal lot for the underlying is an approximation and can be an
    # anachronism if the exchange revised the lot mid-history, but it is bounded
    # and visible, whereas dropping a fifth of the data is neither. Exact
    # matches always win; this only fills the gaps.
    counts: dict[int, int] = {}
    for value in exact.values():
        counts[value] = counts.get(value, 0) + 1
    modal = max(counts, key=lambda k: counts[k]) if counts else None
    return {"__modal__": modal, **exact} if modal is not None else exact


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


async def load_session_calendar() -> list[date]:
    """Sessions that actually have captured rows — the calendar a session
    horizon steps along. Read from the data rather than a holiday table so a
    day with no capture cannot be counted as a trading day."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT DISTINCT session_date FROM candidate_snapshots ORDER BY 1")
            )
        ).fetchall()
    return [r[0] for r in rows]


# Calendar days of forward data to load when a session horizon is requested.
# 3 trading sessions can be 5 calendar days across a weekend, more with a
# holiday; padded so the target instant is inside the loaded window.
FORWARD_DAYS_FOR_SESSION_HORIZONS = 10


async def label_session(
    session_date: date,
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS_SECONDS,
    persist: bool = True,
    calendar: Optional[Sequence[date]] = None,
) -> dict[str, Any]:
    """Label every candidate captured in one session, at every horizon."""
    started = datetime.now(UTC)
    needs_sessions = any(h in SESSION_HORIZONS for h in horizons)
    forward_days = FORWARD_DAYS_FOR_SESSION_HORIZONS if needs_sessions else 0
    sessions_calendar = list(calendar) if calendar is not None else (
        await load_session_calendar() if needs_sessions else []
    )
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
        # Spot for a multi-day horizon comes from the tick tape too, but only
        # out to the furthest target instant rather than a fixed pad.
        spot_forward_days = 0
        if needs_sessions and rows:
            latest = max(a["time"] for a in rows)
            furthest = max(
                (
                    session_target_instant(latest, h, sessions_calendar)
                    for h in horizons
                    if h in SESSION_HORIZONS
                ),
                default=None,
            )
            if furthest is not None:
                spot_forward_days = max(0, (furthest.date() - session_date).days + 1)
        # Intraday horizons use the tick tape; multi-day uses 1-minute bars.
        ticks = await load_spot_ticks(underlying, session_date)
        bars = (
            await load_spot_bars(underlying, session_date, spot_forward_days)
            if spot_forward_days > 0
            else []
        )
        bar_stamps = [t for t, _ in bars]
        # Multi-day barriers come from close-to-close volatility, not from an
        # intraday estimate stretched by sqrt(t).
        sigma_daily = (
            daily_sigma(await load_daily_closes(underlying, session_date))
            if needs_sessions else None
        )
        # Precomputed once so every slice below is a binary search.
        stamps = [t for t, _ in ticks]
        dark = await option_source_dark(underlying, session_date)
        # The instants a session horizon points at, computed up front so the
        # forward reader can fetch only those windows.
        target_windows: list[tuple[datetime, datetime]] = []
        if needs_sessions:
            grid = sorted({a["time"] for a in rows})
            for horizon in (h for h in horizons if h in SESSION_HORIZONS):
                for anchor_time in grid:
                    target = session_target_instant(
                        anchor_time, horizon, sessions_calendar
                    )
                    if target is None:
                        continue
                    pad = timedelta(seconds=SESSION_HORIZON_TOLERANCE_SECONDS)
                    target_windows.append((target - pad, target + pad))
        forward = await load_forward_option_samples(
            underlying=underlying,
            session_date=session_date,
            extra_windows=target_windows,
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
            lot_size = (lots.get(key) or lots.get("__modal__")) if key[1] is not None else None
            anchor_spot = anchor.get("spot") or _spot_at(ticks, anchor["time"], stamps)
            sigma = _anchor_sigma(ticks, anchor["time"], stamps)

            # Give the event loop a chance between anchors: the per-anchor work
            # is synchronous, and without a yield the whole underlying is one
            # uninterruptible stretch that the supervisor's timeout cannot cut.
            if built and built % 200 == 0:
                await asyncio.sleep(0)

            for horizon in horizons:
                is_session_horizon = horizon in SESSION_HORIZONS

                # A session horizon points at the same time-of-day N TRADING
                # sessions later, so a Friday anchor resolves to Monday rather
                # than to a Saturday when nothing trades.
                target_lag: Optional[float] = None
                if is_session_horizon:
                    target = session_target_instant(
                        anchor["time"], horizon, sessions_calendar
                    )
                    if target is None:
                        continue  # not enough later sessions exist yet
                    target_lag = (target - anchor["time"]).total_seconds()

                effective = target_lag if target_lag is not None else float(horizon)

                # Multi-day barriers come from close-to-close volatility.
                # Extrapolating a 30-minute estimate by sqrt(t) overstates them
                # so badly that the confirmed-direction rate collapsed to ~0%:
                # mean |sigma| fell 0.45 -> 0.09 purely because the denominator
                # grew faster than any real move could.
                if is_session_horizon:
                    steps = SESSION_HORIZONS[horizon]
                    width = (
                        round(sigma_daily * math.sqrt(steps) * BARRIER_SIGMA_MULTIPLE, 8)
                        if sigma_daily
                        else None
                    )
                else:
                    width = barrier_width_for_horizon(sigma, horizon)

                # Ticks intraday; 1-minute bars for the multi-day path, which is
                # ~240x cheaper and loses nothing at a day scale.
                path_src, path_stamps = (
                    (bars, bar_stamps) if is_session_horizon else (ticks, stamps)
                )
                spot_path = build_spot_path(
                    anchor_price=anchor_spot,
                    forward_ticks=_slice_forward(
                        path_src, anchor["time"], int(effective), path_stamps
                    ),
                    anchor_time=anchor["time"],
                    horizon_seconds=horizon,
                    barrier_width_pct=width,
                    target_lag_seconds=target_lag,
                )
                window_end = anchor["time"] + timedelta(
                    seconds=tolerance_window(horizon, target_lag_seconds=target_lag)[1]
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
                    target_lag_seconds=target_lag,
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
