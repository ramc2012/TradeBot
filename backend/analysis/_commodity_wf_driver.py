"""Causal walk-forward edge audit for the live commodity MP+OF lane.

This runner deliberately reuses the production signal evaluator and position
lifecycle.  It differs from :mod:`analysis.commodity_walkforward` in one
important way: its input is the durable local MCX store, selected one coherent
contract/source per session, rather than the broker's shallow live-history
window.

The replay is causal:

* a session only sees the immediately-prior completed profile;
* the adaptive volume baseline contains prior sessions only;
* weekly/monthly value-area votes contain no future profile;
* no parameter is selected on the held-out segment;
* results are shown gross and after 2/5 bps per side.

By default only sessions with an available durable profile JSON are tested.
Set ``CWF_PROFILE_ONLY=0`` to use every selected DB session, or
``CWF_SESSIONS=<n>`` to cap the number of most-recent sessions per root.
"""
from __future__ import annotations

import csv
import json
import os
import random
import sys
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

import psycopg2

from analysis import commodity_walkforward as cwf
from core.config import settings
from market_data.commodity_contract_specs import get_commodity_contract_spec
from paper_engine import commodity_mp_signal as mp_signal
from paper_engine import commodity_profile_store as profile_store
from paper_engine.commodity_strategy_agent import (
    COMMODITY_SCALP_ENTRY_STYLES,
    COMMODITY_THESIS_FAILURE_EXIT_REASONS,
    FUTURES_ATR_STOP_MULT,
    FUTURES_MIN_STOP_PCT,
    FUTURES_MIN_STOP_PCT_WIDE,
)
from paper_engine.commodity_volume_baseline import (
    aggregate_mp_volumes,
    compute_baseline,
)


DSN = os.environ.get("NSE_WF_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
ROOTS = [
    item.strip().upper()
    for item in os.environ.get(
        "CWF_ROOTS",
        "ALUMINI,COPPER,CRUDEOIL,GOLD,NATURALGAS,NICKEL,SILVERM,ZINCMINI",
    ).split(",")
    if item.strip()
]
SESSION_LIMIT = max(int(os.environ.get("CWF_SESSIONS", "110")), 10)
PROFILE_ONLY = os.environ.get("CWF_PROFILE_ONLY", "1").strip().lower() not in {"0", "false", "no"}
OUT_DIR = Path(
    os.environ.get(
        "CWF_OUT_DIR",
        str(Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data" / "commodity_walkforward"),
    )
)


def _to_iso(value: datetime) -> str:
    return value.isoformat()


def _available_profile_dates(root: str) -> set[date]:
    path = profile_store.PROFILE_STORE_DIR / root
    dates: set[date] = set()
    if not path.exists():
        return dates
    for item in path.glob("*.json"):
        try:
            dates.add(date.fromisoformat(item.stem))
        except ValueError:
            continue
    return dates


def load_sessions(root: str, *, limit: int = SESSION_LIMIT) -> tuple[dict[date, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Load one coherent source/instrument per IST session.

    The table contains overlapping continuous and contract-specific histories.
    Mixing them creates duplicate timestamps with materially different prices.
    A candidate within 90% of the best session coverage is considered complete;
    among complete candidates the continuous series wins, otherwise the densest
    single contract wins.
    """
    available = _available_profile_dates(root)
    profile_start = min(available) if PROFILE_ONLY and available else None
    query = """
        WITH candidates AS (
            SELECT timezone('Asia/Kolkata', time)::date AS session_date,
                   source,
                   instrument_key,
                   COUNT(DISTINCT time) AS bar_count
            FROM underlying_spot_candles
            WHERE underlying = %s
              AND interval = '1minute'
              AND (CAST(%s AS date) IS NULL
                   OR timezone('Asia/Kolkata', time)::date >= CAST(%s AS date))
              AND timezone('Asia/Kolkata', time)::time BETWEEN time '09:00' AND time '23:59:59'
            GROUP BY 1, 2, 3
            HAVING COUNT(DISTINCT time) >= 20
        ), scored AS (
            SELECT *, MAX(bar_count) OVER (PARTITION BY session_date) AS max_bar_count
            FROM candidates
        ), ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY session_date
                ORDER BY
                    CASE WHEN bar_count >= max_bar_count * 0.90 THEN 0 ELSE 1 END,
                    CASE WHEN source = 'fyers_mcx_cont' THEN 0 ELSE 1 END,
                    bar_count DESC,
                    instrument_key
            ) AS source_rank
            FROM scored
        ), selected AS (
            SELECT session_date, source, instrument_key, bar_count
            FROM ranked
            WHERE source_rank = 1
            ORDER BY session_date DESC
            LIMIT %s
        )
        SELECT s.session_date, c.time, c.open, c.high, c.low, c.close,
               c.volume, c.oi, c.source, c.instrument_key, s.bar_count
        FROM selected s
        JOIN underlying_spot_candles c
          ON c.underlying = %s
         AND c.interval = '1minute'
         AND c.source = s.source
         AND c.instrument_key = s.instrument_key
         AND timezone('Asia/Kolkata', c.time)::date = s.session_date
        WHERE timezone('Asia/Kolkata', c.time)::time BETWEEN time '09:00' AND time '23:59:59'
        ORDER BY s.session_date, c.time
    """
    sessions: dict[date, list[dict[str, Any]]] = defaultdict(list)
    source_meta: dict[date, dict[str, Any]] = {}
    with psycopg2.connect(DSN) as conn:
        with conn.cursor() as cur:
            # Docker Desktop's PostgreSQL container has a small /dev/shm. Four
            # concurrent root loads can otherwise make PostgreSQL allocate
            # parallel-worker DSM segments and fail before replay starts.
            cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            cur.execute(query, (root, profile_start, profile_start, limit, root))
            for session_date, ts, opn, high, low, close, volume, oi, source, key, count in cur:
                if close is None:
                    continue
                sessions[session_date].append(
                    {
                        "time": _to_iso(ts),
                        "open": float(opn if opn is not None else close),
                        "high": float(high if high is not None else close),
                        "low": float(low if low is not None else close),
                        "close": float(close),
                        "volume": int(volume or 0),
                        "oi": int(oi or 0),
                        "_source": str(source),
                        "_instrument_key": str(key),
                    }
                )
                source_meta[session_date] = {
                    "session_date": session_date.isoformat(),
                    "source": source,
                    "instrument_key": key,
                    "bars": int(count),
                }

    if PROFILE_ONLY:
        sessions = defaultdict(list, {d: rows for d, rows in sessions.items() if d in available})
        source_meta = {d: row for d, row in source_meta.items() if d in sessions}
    return dict(sorted(sessions.items())), [source_meta[d] for d in sorted(source_meta)]


def _profile_bar_update(profile_bars: list[dict[str, Any]], row: dict[str, Any]) -> None:
    ts = cwf._parse_iso_timestamp(row.get("time"))
    if ts is None:
        return
    bucket = ts.replace(minute=(ts.minute // 15) * 15, second=0, microsecond=0)
    bucket_iso = bucket.isoformat()
    if not profile_bars or profile_bars[-1]["time"] != bucket_iso:
        profile_bars.append(
            {
                "time": bucket_iso,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume") or 0.0),
            }
        )
        return
    bar = profile_bars[-1]
    bar["high"] = max(float(bar["high"]), float(row["high"]))
    bar["low"] = min(float(bar["low"]), float(row["low"]))
    bar["close"] = float(row["close"])
    bar["volume"] = float(bar["volume"]) + float(row.get("volume") or 0.0)


def _current_aggregates(
    root: str,
    session_date: date,
    current_snapshot: Any,
    completed: list[profile_store.DailyProfile],
) -> tuple[str, dict[str, Any]]:
    """Causal equivalent of the live weekly/monthly value-area vote."""
    current = profile_store.build_daily_profile_from_snapshot(root, current_snapshot)
    usable = list(completed)
    if current is not None:
        usable.append(current)
    week_start = session_date - timedelta(days=session_date.weekday())
    week_profiles = [p for p in usable if week_start <= p.session_date <= session_date]
    week_segments = profile_store._segment_by_contract(  # noqa: SLF001 - same live aggregation rule
        list(reversed(week_profiles)), profile_store._roll_frac_for(root)  # noqa: SLF001
    )
    week = profile_store._aggregate(week_segments[0] if week_segments else [])  # noqa: SLF001
    all_segments = profile_store._segment_by_contract(  # noqa: SLF001
        list(reversed(usable)), profile_store._roll_frac_for(root)  # noqa: SLF001
    )
    month = profile_store._aggregate(all_segments[0] if all_segments else [])  # noqa: SLF001

    def vote(agg: Optional[dict[str, Any]]) -> Optional[int]:
        if not agg or agg.get("vah") is None or agg.get("val") is None:
            return None
        price = float(getattr(current_snapshot, "close_price", 0.0) or 0.0)
        if price > float(agg["vah"]):
            return 1
        if price < float(agg["val"]):
            return -1
        return 0

    wv, mv = vote(week), vote(month)
    votes = [v for v in (wv, mv) if v is not None]
    day = completed[-1].to_dict() if completed else None
    if not votes:
        dv = vote(day)
        if dv is not None:
            votes = [dv]
    score = sum(votes)
    bias = "strong" if score >= 1 else "weak" if score <= -1 else "neutral"
    return bias, {"week": week, "month": month, "previous_day": day}


def _apply_naked_poc_target(
    position: cwf.ReplayPosition,
    *,
    signal: str,
    aggregates: dict[str, Any],
) -> None:
    if not settings.COMMODITY_NAKED_POC_TARGET_ENABLED:
        return
    price = position.entry_price
    target = position.target_price
    full_dist = abs(target - price)
    if full_dist <= 0:
        return
    minimum = full_dist * float(settings.COMMODITY_NAKED_POC_MIN_R_FRACTION)
    pocs: list[float] = []
    for key in ("previous_day", "week", "month"):
        agg = aggregates.get(key)
        if agg and agg.get("poc") is not None:
            pocs.append(float(agg["poc"]))
    if signal == "BUY":
        valid = [p for p in pocs if price + minimum <= p <= target]
        if valid:
            position.target_price = round(min(valid), 2)
    else:
        valid = [p for p in pocs if target <= p <= price - minimum]
        if valid:
            position.target_price = round(max(valid), 2)


def _trade_cost_metrics(trade: dict[str, Any]) -> None:
    risk = abs(float(trade["entry_price"]) - float(trade["initial_stop_price"])) * int(trade["qty"])
    gross = float(trade["pnl"])
    turnover = (float(trade["entry_price"]) + float(trade["exit_price"])) * int(trade["qty"])
    trade["initial_risk"] = round(risk, 4)
    trade["gross_r"] = round(gross / risk, 6) if risk > 0 else None
    for bps in (2, 5):
        net = gross - turnover * bps / 10000.0
        trade[f"net_{bps}bps_pnl"] = round(net, 2)
        trade[f"net_{bps}bps_r"] = round(net / risk, 6) if risk > 0 else None


def _entry_atr_context(
    entry: dict[str, Any],
    analysis: dict[str, Any],
    *,
    bias: str,
    atr_15m: Optional[float],
) -> dict[str, Any]:
    atr = float(entry.get("atr") or 0.0)
    price = float(entry.get("price") or 0.0)
    ib_high = cwf._safe_float(analysis.get("mp_ib_high"))
    ib_low = cwf._safe_float(analysis.get("mp_ib_low"))
    poc = cwf._safe_float(analysis.get("mp_poc"))
    ts = cwf._parse_iso_timestamp(entry.get("bar_time"))
    ist = ts.astimezone(cwf.IST) if ts is not None else None
    return {
        "entry_atr": round(atr, 6),
        "entry_atr_1m": round(atr, 6),
        "entry_atr_15m": round(float(atr_15m), 6) if atr_15m and atr_15m > 0 else None,
        "entry_atr_pct": round(atr / price * 100.0, 6) if price > 0 else None,
        "entry_hour_ist": round(ist.hour + ist.minute / 60.0, 4) if ist is not None else None,
        "entry_minute_ist": ist.hour * 60 + ist.minute if ist is not None else None,
        "htf_bias": bias,
        "mp_periods": int(analysis.get("mp_periods") or 0),
        "confidence": cwf._round_or_none(analysis.get("confidence"), 4),
        "of_volume_coverage": cwf._round_or_none(analysis.get("of_volume_coverage"), 4),
        "cvd_pressure_ratio": cwf._round_or_none(analysis.get("cvd_pressure_ratio"), 4),
        "ib_extension_pct": cwf._round_or_none(analysis.get("ib_extension_pct"), 4),
        "ib_range_atr": (
            round(abs(ib_high - ib_low) / atr, 6)
            if atr > 0 and ib_high is not None and ib_low is not None
            else None
        ),
        "ib_range_atr_15m": (
            round(abs(ib_high - ib_low) / float(atr_15m), 6)
            if atr_15m and atr_15m > 0 and ib_high is not None and ib_low is not None
            else None
        ),
        "price_from_poc_atr": (
            round((price - poc) / atr, 6) if atr > 0 and poc is not None else None
        ),
        "price_from_poc_atr_15m": (
            round((price - poc) / float(atr_15m), 6)
            if atr_15m and atr_15m > 0 and poc is not None else None
        ),
    }


def _prime_excursion_metrics(
    position: cwf.ReplayPosition,
    context: dict[str, Any],
) -> None:
    position._atr_context = context  # type: ignore[attr-defined]
    position._mfe_points = 0.0  # type: ignore[attr-defined]
    position._mae_points = 0.0  # type: ignore[attr-defined]


def _update_excursion_metrics(position: cwf.ReplayPosition, row: dict[str, Any]) -> None:
    high = float(row.get("high") or row.get("close") or position.entry_price)
    low = float(row.get("low") or row.get("close") or position.entry_price)
    if position.action == "BUY":
        favorable = max(high - position.entry_price, 0.0)
        adverse = max(position.entry_price - low, 0.0)
    else:
        favorable = max(position.entry_price - low, 0.0)
        adverse = max(high - position.entry_price, 0.0)
    position._mfe_points = max(  # type: ignore[attr-defined]
        float(getattr(position, "_mfe_points", 0.0)), favorable
    )
    position._mae_points = max(  # type: ignore[attr-defined]
        float(getattr(position, "_mae_points", 0.0)), adverse
    )


def _annotate_atr_trade(trade: dict[str, Any], position: cwf.ReplayPosition) -> None:
    context = dict(getattr(position, "_atr_context", {}) or {})
    atr = float(context.get("entry_atr") or position.atr or 0.0)
    trade.update(context)
    stop_points = abs(position.entry_price - position.initial_stop_price)
    target_points = abs(position.target_price - position.entry_price)
    signed_points = (
        float(trade["exit_price"]) - position.entry_price
        if position.action == "BUY"
        else position.entry_price - float(trade["exit_price"])
    )
    trade["stop_distance_atr"] = round(stop_points / atr, 6) if atr > 0 else None
    trade["target_distance_atr"] = round(target_points / atr, 6) if atr > 0 else None
    trade["gross_atr"] = round(signed_points / atr, 6) if atr > 0 else None
    trade["mfe_atr"] = (
        round(float(getattr(position, "_mfe_points", 0.0)) / atr, 6) if atr > 0 else None
    )
    trade["mae_atr"] = (
        round(float(getattr(position, "_mae_points", 0.0)) / atr, 6) if atr > 0 else None
    )
    for bps in (2, 5):
        cost_points = (position.entry_price + float(trade["exit_price"])) * bps / 10000.0
        trade[f"net_{bps}bps_atr"] = (
            round((signed_points - cost_points) / atr, 6) if atr > 0 else None
        )
    mfe = float(trade.get("mfe_atr") or 0.0)
    trade["mfe_capture"] = round(float(trade.get("gross_atr") or 0.0) / mfe, 6) if mfe > 0 else None
    atr_15m = float(context.get("entry_atr_15m") or 0.0)
    if atr_15m > 0:
        trade["stop_distance_atr_15m"] = round(stop_points / atr_15m, 6)
        trade["target_distance_atr_15m"] = round(target_points / atr_15m, 6)
        trade["gross_atr_15m"] = round(signed_points / atr_15m, 6)
        trade["mfe_atr_15m"] = round(
            float(getattr(position, "_mfe_points", 0.0)) / atr_15m, 6
        )
        trade["mae_atr_15m"] = round(
            float(getattr(position, "_mae_points", 0.0)) / atr_15m, 6
        )
        for bps in (2, 5):
            cost_points = (
                position.entry_price + float(trade["exit_price"])
            ) * bps / 10000.0
            trade[f"net_{bps}bps_atr_15m"] = round(
                (signed_points - cost_points) / atr_15m, 6
            )


def replay_root(
    root: str,
    sessions: dict[date, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Replay production signal/position decisions in timestamp order."""
    agent = cwf.CommodityStrategyAgent.__new__(cwf.CommodityStrategyAgent)
    completed_profiles: list[profile_store.DailyProfile] = []
    baseline_volumes: deque[float] = deque()
    baseline_session_chunks: deque[list[float]] = deque()
    position: Optional[cwf.ReplayPosition] = None
    trades: list[dict[str, Any]] = []
    entry_horizons: list[str] = []
    stopped_setups: set[tuple[date, str, str, str]] = set()
    last_exit_time: Optional[datetime] = None
    global_index = 0
    counters: defaultdict[str, int] = defaultdict(int)
    previous_profile: Any = None
    previous_15m_bars: list[dict[str, Any]] = []
    previous_last_row: Optional[dict[str, Any]] = None
    previous_session_date: Optional[date] = None
    original_loader = mp_signal._vb_load_baseline

    try:
        for session_date, rows in sessions.items():
            if len(rows) < 40:
                counters["thin_sessions"] += 1
                continue
            # Never carry a synthetic continuous-series position through a
            # contract roll.  The live desk would still own the old contract;
            # a continuous price jump is not executable P&L.  Close at the
            # prior session's last tradable price when either the selected
            # contract changes or the root's roll-gap threshold is crossed.
            session_roll = False
            if previous_last_row is not None:
                previous_key = str(previous_last_row.get("_instrument_key") or "")
                current_key = str(rows[0].get("_instrument_key") or "")
                prior_close = float(previous_last_row.get("close") or 0.0)
                next_open = float(rows[0].get("open") or rows[0].get("close") or 0.0)
                roll_gap = (
                    prior_close > 0
                    and abs(next_open - prior_close) / prior_close
                    > get_commodity_contract_spec(root).roll_gap_threshold()
                )
                session_roll = bool(
                    (previous_key and current_key and previous_key != current_key) or roll_gap
                )
                if session_roll:
                    # A non-back-adjusted roll gap is contract basis, not
                    # volatility. Do not pollute the new contract's ATR(14).
                    previous_15m_bars = []
                if session_roll and position is not None:
                    trade = cwf._close_trade(
                        position,
                        row=previous_last_row,
                        index=max(global_index - 1, position.entry_index),
                        reason="contract_roll",
                    )
                    _annotate_atr_trade(trade, position)
                    _trade_cost_metrics(trade)
                    trade["session_date"] = (
                        previous_session_date.isoformat() if previous_session_date else session_date.isoformat()
                    )
                    trades.append(trade)
                    last_exit_time = cwf._parse_iso_timestamp(previous_last_row.get("time"))
                    position = None
                    counters["contract_roll_exits"] += 1
            profile_bars: list[dict[str, Any]] = []
            session_prefix: list[dict[str, Any]] = []
            baseline = compute_baseline(root, list(baseline_volumes))
            mp_signal._vb_load_baseline = lambda _root, value=baseline: value  # type: ignore[assignment]

            for row in rows:
                session_prefix.append(row)
                _profile_bar_update(profile_bars, row)
                current_profile = agent._build_market_profile(root, profile_bars)  # noqa: SLF001
                if current_profile is None:
                    global_index += 1
                    continue
                atr = cwf._compute_atr_series(session_prefix, period=14)
                analysis = mp_signal.evaluate_commodity_mp_signal(
                    session_prefix,
                    symbol=root,
                    today_profile=current_profile,
                    prior_profile=previous_profile,
                    cvd_anchor_index=0,
                    atr_1m=atr,
                )

                if position is not None:
                    _update_excursion_metrics(position, row)
                    reason = cwf._exit_reason(
                        position,
                        row=row,
                        raw_signal=analysis.get("signal"),
                        value_migration_signal=(
                            analysis.get("value_migration_signal")
                            if analysis.get("value_migration_state") == "confirmed"
                            else None
                        ),
                        holding_bars=global_index - position.entry_index,
                    )
                    if reason:
                        trade = cwf._close_trade(position, row=row, index=global_index, reason=reason)
                        _annotate_atr_trade(trade, position)
                        _trade_cost_metrics(trade)
                        trade["session_date"] = session_date.isoformat()
                        trades.append(trade)
                        if reason in COMMODITY_THESIS_FAILURE_EXIT_REASONS:
                            stopped_setups.add(
                                (session_date, root, position.action, position.entry_reason)
                            )
                        last_exit_time = cwf._parse_iso_timestamp(row.get("time"))
                        position = None
                    global_index += 1
                    continue

                if analysis.get("signal") not in {"BUY", "SELL"}:
                    global_index += 1
                    continue
                counters["raw_signals"] += 1
                entry = cwf._entry_row(
                    agent,
                    symbol=root,
                    rows=session_prefix,
                    index=len(session_prefix) - 1,
                    prior_cache={},
                    analysis=analysis,
                )
                if not entry:
                    global_index += 1
                    continue
                lock = (session_date, root, str(entry["signal"]), str(entry["reason"]))
                if lock in stopped_setups:
                    counters["setup_stop_lock"] += 1
                    global_index += 1
                    continue
                now = cwf._parse_iso_timestamp(row.get("time"))
                if last_exit_time is not None and now is not None:
                    elapsed = (now - last_exit_time).total_seconds() / 60.0
                    if elapsed < float(settings.COMMODITY_REENTRY_COOLDOWN_MINUTES):
                        counters["reentry_cooldown"] += 1
                        global_index += 1
                        continue

                entry_style = str(entry.get("entry_style") or "")
                horizon = "scalp" if entry_style in COMMODITY_SCALP_ENTRY_STYLES else "positional"
                if horizon == "scalp":
                    lookback = max(5, int(settings.COMMODITY_SCALP_MIX_LOOKBACK))
                    history = entry_horizons[-max(lookback - 1, 0):]
                    share = (history.count("scalp") + 1) / (len(history) + 1)
                    if share > float(settings.COMMODITY_SCALP_MAX_TRADE_SHARE) + 1e-12:
                        counters["scalp_mix_cap"] += 1
                        global_index += 1
                        continue

                bias, aggregates = _current_aggregates(
                    root, session_date, current_profile, completed_profiles
                )
                opposed = (
                    (entry["signal"] == "BUY" and bias == "weak")
                    or (entry["signal"] == "SELL" and bias == "strong")
                )
                if settings.COMMODITY_HTF_GATE_ENABLED and opposed and settings.COMMODITY_HTF_REQUIRE_ALIGNMENT:
                    counters["htf_counter_bias"] += 1
                    global_index += 1
                    continue

                entry["trade_horizon"] = horizon
                candidate = cwf._open_position(entry, index=global_index, lots=1)
                if candidate is None:
                    counters["invalid_position"] += 1
                    global_index += 1
                    continue

                # Match the live one-lot structural-risk gate. R-multiple edge is
                # invariant to the final equal-notional lot count.
                min_distance = (
                    max(candidate.atr * FUTURES_ATR_STOP_MULT, candidate.entry_price * FUTURES_MIN_STOP_PCT_WIDE)
                    if settings.COMMODITY_STOP_WIDENING_ENABLED
                    else max(candidate.atr, candidate.entry_price * FUTURES_MIN_STOP_PCT)
                )
                structural_distance = abs(candidate.entry_price - candidate.initial_stop_price)
                risk_distance = max(min_distance, structural_distance)
                risk_per_lot = risk_distance * candidate.lot_size
                risk_budget = 5_000_000.0 * float(settings.COMMODITY_RISK_PER_TRADE_PCT)
                if risk_per_lot > risk_budget:
                    counters["structural_risk_too_large"] += 1
                    global_index += 1
                    continue
                if entry.get("target_hint") is None:
                    _apply_naked_poc_target(candidate, signal=str(entry["signal"]), aggregates=aggregates)

                position = candidate
                position.entry_index = global_index
                # A directional MP position lives on a much slower horizon than
                # the 1-minute trigger ATR. Use a fixed causal ATR(14) over
                # completed 15-minute buckets. Prior-session buckets provide
                # warm-up for early entries and retain the overnight true range;
                # the currently-forming bucket is excluded.
                completed_15m = profile_bars[:-1]
                atr_input = (previous_15m_bars + completed_15m)[-14:]
                atr_15m = (
                    cwf._compute_atr_series(atr_input, period=14)
                    if len(atr_input) >= 14 else None
                )
                _prime_excursion_metrics(
                    position,
                    _entry_atr_context(entry, analysis, bias=bias, atr_15m=atr_15m),
                )
                entry_horizons.append(horizon)
                counters["entries"] += 1
                global_index += 1

            # Completed session becomes causal input for the next session.
            full_profile = agent._build_market_profile(root, profile_bars)  # noqa: SLF001
            if full_profile is not None:
                daily = profile_store.build_daily_profile_from_snapshot(root, full_profile)
                if daily is not None:
                    completed_profiles.append(daily)
                previous_profile = full_profile
            chunk = aggregate_mp_volumes(rows)
            baseline_session_chunks.append(chunk)
            baseline_volumes.extend(chunk)
            while len(baseline_session_chunks) > 90:
                old = baseline_session_chunks.popleft()
                for _ in old:
                    baseline_volumes.popleft()
            previous_last_row = rows[-1]
            previous_session_date = session_date
            previous_15m_bars = [dict(bar) for bar in profile_bars]

        if position is not None:
            last_date = next(reversed(sessions))
            last_row = sessions[last_date][-1]
            trade = cwf._close_trade(position, row=last_row, index=global_index, reason="hold_to_end")
            _annotate_atr_trade(trade, position)
            _trade_cost_metrics(trade)
            trade["session_date"] = last_date.isoformat()
            trades.append(trade)
    finally:
        mp_signal._vb_load_baseline = original_loader
    return trades, dict(counters)


def _max_drawdown(values: Iterable[float]) -> float:
    equity = peak = worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def summarize(trades: list[dict[str, Any]], field: str) -> dict[str, Any]:
    vals = [float(t[field]) for t in trades if t.get(field) is not None]
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    result = {
        "trades": len(vals),
        "total_r": round(sum(vals), 4),
        "expectancy_r": round(sum(vals) / len(vals), 6) if vals else None,
        "win_rate": round(len(wins) / len(vals), 4) if vals else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else None,
        "max_drawdown_r": round(_max_drawdown(vals), 4),
    }
    if len(vals) >= 2:
        rng = random.Random(20260704)
        means = []
        for _ in range(5000):
            sample = [vals[rng.randrange(len(vals))] for _ in vals]
            means.append(sum(sample) / len(sample))
        means.sort()
        result["bootstrap_mean_r_95ci"] = [
            round(means[int(0.025 * len(means))], 6),
            round(means[int(0.975 * len(means))], 6),
        ]
        result["bootstrap_p_mean_le_zero"] = round(
            sum(1 for value in means if value <= 0) / len(means), 4
        )
    return result


def _bucket_summary(trades: list[dict[str, Any]], key: str) -> dict[str, Any]:
    buckets: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        buckets[str(trade.get(key) or "unknown")].append(trade)
    return {
        name: {
            "gross": summarize(items, "gross_r"),
            "net_5bps": summarize(items, "net_5bps_r"),
        }
        for name, items in sorted(buckets.items())
    }


def _walkforward_folds(trades: list[dict[str, Any]], session_dates: list[date]) -> list[dict[str, Any]]:
    if len(session_dates) < 20:
        return []
    train = max(20, int(len(session_dates) * 0.50))
    test = max(5, int(len(session_dates) * 0.10))
    folds: list[dict[str, Any]] = []
    cursor = train
    while cursor < len(session_dates):
        test_dates = session_dates[cursor : cursor + test]
        if not test_dates:
            break
        start, end = test_dates[0], test_dates[-1]
        fold_trades = [
            t for t in trades if start <= date.fromisoformat(str(t["session_date"])) <= end
        ]
        folds.append(
            {
                "train_sessions": cursor,
                "test_start": start.isoformat(),
                "test_end": end.isoformat(),
                "net_5bps": summarize(fold_trades, "net_5bps_r"),
            }
        )
        cursor += test
    return folds


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not fields:
            return
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def combine_existing_results() -> None:
    """Combine independently replayed roots without running the DB replay again."""
    roots: dict[str, Any] = {}
    all_trades: list[dict[str, Any]] = []
    all_dates: set[date] = set()
    for root in ROOTS:
        root_dir = OUT_DIR / "by_root" / root
        summary_path = root_dir / "edge_summary.json"
        trades_path = root_dir / "edge_trades.csv"
        if not summary_path.exists() or not trades_path.exists():
            continue
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        root_payload = payload.get("roots", {}).get(root)
        if root_payload:
            roots[root] = root_payload
            all_dates.update(
                date.fromisoformat(item["session_date"])
                for item in root_payload.get("sources", [])
            )
        with trades_path.open(newline="", encoding="utf-8") as handle:
            all_trades.extend(dict(row) for row in csv.DictReader(handle))

    dates = sorted(all_dates)
    split_date = dates[int(len(dates) * 0.70)] if dates else None
    oos = [
        trade
        for trade in all_trades
        if split_date and date.fromisoformat(str(trade["session_date"])) >= split_date
    ]
    combined = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "method": "causal production-rule replay; combined independent roots",
        "profile_only": PROFILE_ONLY,
        "session_limit": SESSION_LIMIT,
        "cost_note": "2bps and 5bps are charged per side on futures turnover",
        "roots": roots,
        "overall": {
            "coverage": {
                "roots": len(roots),
                "sessions_by_root": sum(int(item.get("sessions") or 0) for item in roots.values()),
                "bars": sum(int(item.get("bars") or 0) for item in roots.values()),
                "first": min((item.get("first") for item in roots.values() if item.get("first")), default=None),
                "last": max((item.get("last") for item in roots.values() if item.get("last")), default=None),
            },
            "gross": summarize(all_trades, "gross_r"),
            "net_2bps": summarize(all_trades, "net_2bps_r"),
            "net_5bps": summarize(all_trades, "net_5bps_r"),
            "oos_split_date": split_date.isoformat() if split_date else None,
            "oos_net_5bps": summarize(oos, "net_5bps_r"),
            "by_root": _bucket_summary(all_trades, "underlying"),
            "by_setup": _bucket_summary(all_trades, "entry_style"),
            "by_horizon": _bucket_summary(all_trades, "trade_horizon"),
            "by_exit": _bucket_summary(all_trades, "exit_reason"),
        },
    }
    (OUT_DIR / "edge_summary.json").write_text(
        json.dumps(combined, indent=2, default=str), encoding="utf-8"
    )
    _write_csv(OUT_DIR / "edge_trades.csv", all_trades)
    print(json.dumps(combined["overall"], indent=2, default=str))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_trades: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "method": "causal production-rule replay",
        "profile_only": PROFILE_ONLY,
        "session_limit": SESSION_LIMIT,
        "cost_note": "2bps and 5bps are charged per side on futures turnover",
        "roots": {},
    }
    all_dates: set[date] = set()
    for root in ROOTS:
        sessions, source_meta = load_sessions(root)
        dates = list(sessions)
        all_dates.update(dates)
        sys.stderr.write(f"[replay] {root}: {len(sessions)} sessions / {sum(map(len, sessions.values()))} bars\n")
        sys.stderr.flush()
        trades, counters = replay_root(root, sessions)
        all_trades.extend(trades)
        split_date = dates[int(len(dates) * 0.70)] if dates else None
        in_sample = [t for t in trades if split_date and date.fromisoformat(t["session_date"]) < split_date]
        out_sample = [t for t in trades if split_date and date.fromisoformat(t["session_date"]) >= split_date]
        result["roots"][root] = {
            "sessions": len(sessions),
            "bars": sum(map(len, sessions.values())),
            "first": dates[0].isoformat() if dates else None,
            "last": dates[-1].isoformat() if dates else None,
            "profile_dates_available": len(_available_profile_dates(root)),
            "sources": source_meta,
            "signal_funnel": counters,
            "gross": summarize(trades, "gross_r"),
            "net_2bps": summarize(trades, "net_2bps_r"),
            "net_5bps": summarize(trades, "net_5bps_r"),
            "chrono_70_30": {
                "split_date": split_date.isoformat() if split_date else None,
                "in_sample_net_5bps": summarize(in_sample, "net_5bps_r"),
                "out_of_sample_net_5bps": summarize(out_sample, "net_5bps_r"),
            },
            "walkforward_folds": _walkforward_folds(trades, dates),
            "by_setup": _bucket_summary(trades, "entry_style"),
            "by_horizon": _bucket_summary(trades, "trade_horizon"),
            "by_exit": _bucket_summary(trades, "exit_reason"),
        }
        sys.stderr.write(
            f"[done] {root}: {len(trades)} trades; net5 {result['roots'][root]['net_5bps']['total_r']}R\n"
        )
        sys.stderr.flush()

    dates = sorted(all_dates)
    split_date = dates[int(len(dates) * 0.70)] if dates else None
    oos = [t for t in all_trades if split_date and date.fromisoformat(t["session_date"]) >= split_date]
    result["overall"] = {
        "gross": summarize(all_trades, "gross_r"),
        "net_2bps": summarize(all_trades, "net_2bps_r"),
        "net_5bps": summarize(all_trades, "net_5bps_r"),
        "oos_split_date": split_date.isoformat() if split_date else None,
        "oos_net_5bps": summarize(oos, "net_5bps_r"),
        "by_root": _bucket_summary(all_trades, "underlying"),
        "by_setup": _bucket_summary(all_trades, "entry_style"),
        "by_horizon": _bucket_summary(all_trades, "trade_horizon"),
        "by_exit": _bucket_summary(all_trades, "exit_reason"),
    }
    (OUT_DIR / "edge_summary.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    _write_csv(OUT_DIR / "edge_trades.csv", all_trades)
    print(json.dumps(result["overall"], indent=2, default=str))


if __name__ == "__main__":
    if os.environ.get("CWF_COMBINE_ONLY", "").strip().lower() in {"1", "true", "yes"}:
        combine_existing_results()
    else:
        main()
