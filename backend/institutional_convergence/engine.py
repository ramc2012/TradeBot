from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from math import floor
from statistics import median
from typing import Any, Optional

from analytics.orderflow import bar_cvd, cvd_divergence, hvn_lvn, volume_node_density
from auction_intelligence.market_profile import MarketProfileEngine
from auction_intelligence.schemas import MarketBar


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def atr(bars: list[dict[str, Any]], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    values: list[float] = []
    for index, row in enumerate(bars):
        high, low = _f(row.get("high")), _f(row.get("low"))
        prev = _f(bars[index - 1].get("close")) if index else _f(row.get("open"))
        values.append(max(high - low, abs(high - prev), abs(low - prev)))
    sample = values[-max(1, period):]
    return sum(sample) / len(sample)


def build_footprint(ticks: list[dict[str, Any]], tick_size: float) -> dict[str, Any]:
    buckets: dict[datetime, dict[str, Any]] = {}
    cumulative = 0.0
    previous: dict[str, Any] | None = None
    for tick in ticks:
        timestamp = tick["time"]
        bucket_time = timestamp.replace(minute=(timestamp.minute // 3) * 3, second=0, microsecond=0)
        price = _f(tick.get("ltp"))
        if price <= 0:
            continue
        volume_delta = max(_f(tick.get("volume")) - _f((previous or {}).get("volume")), 0.0)
        if volume_delta <= 0:
            previous = tick
            continue
        bid, ask = _f(tick.get("bid")), _f(tick.get("ask"))
        prev_price = _f((previous or {}).get("ltp"), price)
        side = "buy" if ask > 0 and price >= ask else "sell" if bid > 0 and price <= bid else "buy" if price >= prev_price else "sell"
        level = round(round(price / max(tick_size, 0.01)) * max(tick_size, 0.01), 4)
        bucket = buckets.setdefault(bucket_time, {"time": bucket_time.isoformat(), "levels": {}, "delta": 0.0, "volume": 0.0})
        cell = bucket["levels"].setdefault(level, {"price": level, "buy": 0.0, "sell": 0.0})
        cell[side] += volume_delta
        bucket["delta"] += volume_delta if side == "buy" else -volume_delta
        bucket["volume"] += volume_delta
        previous = tick

    rows: list[dict[str, Any]] = []
    for _, bucket in sorted(buckets.items()):
        cumulative += bucket["delta"]
        levels = sorted(bucket["levels"].values(), key=lambda row: row["price"])
        for cell in levels:
            cell["buy_ratio"] = round(cell["buy"] / max(cell["sell"], 1.0), 2)
            cell["sell_ratio"] = round(cell["sell"] / max(cell["buy"], 1.0), 2)
        rows.append({**bucket, "levels": levels, "cumulative_delta": cumulative})
    return {"bars": rows, "tick_count": len(ticks), "source": "market_ticks" if len(rows) >= 4 else "insufficient_ticks"}


def _footprint_trigger(
    footprint: dict[str, Any],
    direction: str,
    *,
    now: datetime | None = None,
    lookback_bars: int = 2,
) -> tuple[bool, float]:
    rows = list(footprint.get("bars") or [])
    if now is not None:
        completed: list[dict[str, Any]] = []
        for row in rows:
            try:
                timestamp = datetime.fromisoformat(str(row["time"]))
                if timestamp + timedelta(minutes=3) <= now:
                    completed.append(row)
            except (KeyError, TypeError, ValueError):
                continue
        rows = completed
    if not rows:
        return False, 0.0
    key = "buy_ratio" if direction == "LONG" else "sell_ratio"
    ratio = 0.0
    for bar in rows[-max(1, lookback_bars):]:
        levels = bar.get("levels") or []
        if not levels:
            continue
        span = max(1, len(levels) // 3)
        tail = levels[:span] if direction == "LONG" else levels[-span:]
        ratio = max(ratio, max((_f(row.get(key)) for row in tail), default=0.0))
    return ratio >= 3.0, ratio


def _freshness_limit_ms(ticks: list[dict[str, Any]]) -> float:
    """Adaptive tape freshness for a three-minute strategy.

    A fixed one-second limit made a 60-second runner reject healthy tapes. Use
    three times the observed inter-tick cadence, bounded to 45-90 seconds.
    """
    gaps: list[float] = []
    for previous, current in zip(ticks[-51:-1], ticks[-50:]):
        try:
            gap = (current["time"] - previous["time"]).total_seconds() * 1000.0
        except (KeyError, TypeError, AttributeError):
            continue
        if gap > 0:
            gaps.append(gap)
    adaptive = median(gaps) * 3.0 if gaps else 45_000.0
    return min(90_000.0, max(45_000.0, adaptive))


# If the newest tick across the WHOLE store is older than this vs the wall
# clock, the shared flush pipeline itself is considered stalled and drift falls
# back to wall-clock measurement (so a dead feed still blocks every symbol).
PIPELINE_STALL_GUARD_SECONDS = 180.0


def tick_clock_drift_ms(
    last_tick_at: datetime | None,
    pipeline_last_at: datetime | None,
    wall_now: datetime,
    *,
    stall_guard_seconds: float = PIPELINE_STALL_GUARD_SECONDS,
) -> float | None:
    """Per-symbol tape staleness for the tick_fresh gate.

    Measured against the newest tick across the ENTIRE tick store (the shared
    batched flush pipeline), not the wall clock. market_ticks is written by
    live_candle_store's queue+flush worker (5s cadence, 30s failure backoff);
    under evening load its visibility lag reached a uniform ~19-22s on
    subsecond-fresh tapes and >45s around 21:18 IST 2026-07-15 — wall-clock
    drift then failed tick_fresh on 5/8 MCX roots whose tape was seconds
    fresh (the sparser roots first, since their inter-tick gap stacks on top
    of the shared lag). Comparing each symbol's newest tick to the newest
    tick ANY symbol got through the same pipeline cancels the shared write
    lag and leaves only genuine per-symbol tape staleness.

    Fail-safe: if the pipeline reference is itself older than the stall
    guard (or absent), the whole store is quiet — fall back to wall-clock so
    a dead feed still blocks instead of every symbol passing with ~0 drift.
    """
    if last_tick_at is None:
        return None
    last_utc = last_tick_at.astimezone(timezone.utc)
    reference = (
        pipeline_last_at.astimezone(timezone.utc) if pipeline_last_at is not None else wall_now
    )
    if (wall_now - reference).total_seconds() > stall_guard_seconds:
        reference = wall_now
    return max(0.0, (reference - last_utc).total_seconds() * 1000.0)


def _structural_setup(
    bars: list[dict[str, Any]],
    levels: list[float],
    direction: str,
    tolerance: float,
    *,
    active_window_bars: int = 5,
    search_window_bars: int = 10,
) -> dict[str, Any]:
    """Find the latest sweep/rejection and keep it armed for several bars."""
    latest: dict[str, Any] | None = None
    start = max(0, len(bars) - max(active_window_bars, search_window_bars))
    for index in range(start, len(bars)):
        bar = bars[index]
        open_, high, low, close = (_f(bar.get(key)) for key in ("open", "high", "low", "close"))
        candidates: list[tuple[float, str]] = []
        for level in levels:
            if level <= 0:
                continue
            if direction == "LONG":
                sweep = low < level and close >= level
                rejection = abs(low - level) <= tolerance * 0.25 and close >= level and close > open_
            else:
                sweep = high > level and close <= level
                rejection = abs(high - level) <= tolerance * 0.25 and close <= level and close < open_
            if sweep or rejection:
                candidates.append((level, "sweep_reclaim" if sweep else "rejection"))
        if candidates:
            level, event = min(candidates, key=lambda item: abs(close - item[0]))
            latest = {
                "direction": direction,
                "event": event,
                "level": level,
                "extreme": low if direction == "LONG" else high,
                "bar_time": bar["time"].isoformat() if hasattr(bar.get("time"), "isoformat") else str(bar.get("time")),
                "bar_index": index,
            }
    if latest is None:
        return {"direction": direction, "state": "WATCHING", "active": False, "age_bars": None}
    age = len(bars) - 1 - int(latest["bar_index"])
    active = age < max(1, active_window_bars)
    latest.update({"age_bars": age, "active": active, "state": "ARMED" if active else "EXPIRED"})
    latest.pop("bar_index", None)
    return latest


def _cvd_impulse(cvd: list[float], direction: str) -> bool:
    if len(cvd) < 3:
        return False
    recent = cvd[-7:]
    changes = [current - previous for previous, current in zip(recent, recent[1:])]
    if not changes:
        return False
    latest = changes[-1]
    baseline = median(abs(change) for change in changes)
    return latest >= baseline and latest > 0 if direction == "LONG" else latest <= -baseline and latest < 0


def _price_confirmation(bars: list[dict[str, Any]], setup: dict[str, Any], direction: str) -> bool:
    if not setup.get("active") or not bars:
        return False
    latest = bars[-1]
    open_, close = _f(latest.get("open")), _f(latest.get("close"))
    level = _f(setup.get("level"))
    if direction == "LONG":
        return close >= level and (close > open_ or (len(bars) >= 2 and close > _f(bars[-2].get("high"))))
    return close <= level and (close < open_ or (len(bars) >= 2 and close < _f(bars[-2].get("low"))))


def _risk_plan(
    *,
    direction: str,
    entry: float,
    setup: dict[str, Any],
    atr_value: float,
    targets: list[float],
    max_chase_atr: float,
    min_reward_risk: float,
) -> dict[str, Any]:
    if not setup.get("active") or entry <= 0 or atr_value <= 0:
        return {"not_chasing": False, "reward_risk_valid": False, "reward_risk": 0.0, "stop": None, "target1": None, "target2": None}
    level = _f(setup.get("level"))
    extreme = _f(setup.get("extreme"), entry)
    stop = extreme - 0.25 * atr_value if direction == "LONG" else extreme + 0.25 * atr_value
    risk_points = abs(entry - stop)
    not_chasing = abs(entry - level) <= max_chase_atr * atr_value
    favorable = sorted(
        {target for target in targets if target > entry} if direction == "LONG" else {target for target in targets if 0 < target < entry},
        reverse=direction == "SHORT",
    )
    target2 = favorable[0] if favorable else None
    reward_risk = abs(target2 - entry) / risk_points if target2 is not None and risk_points > 0 else 0.0
    return {
        "not_chasing": not_chasing,
        "reward_risk_valid": reward_risk >= min_reward_risk,
        "reward_risk": reward_risk,
        "stop": stop,
        "target1": entry + risk_points if direction == "LONG" else entry - risk_points,
        "target2": target2,
    }


def _aligned_tick_cvd(
    current_bars: list[dict[str, Any]], footprint_bars: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[float], list[dict[str, Any]]]:
    candles_by_time = {
        row["time"].astimezone(timezone.utc).replace(second=0, microsecond=0): row
        for row in current_bars
    }
    aligned_bars: list[dict[str, Any]] = []
    cvd: list[float] = []
    series: list[dict[str, Any]] = []
    for row in footprint_bars:
        timestamp = datetime.fromisoformat(str(row["time"])).astimezone(timezone.utc)
        candle = candles_by_time.get(timestamp)
        if candle is None:
            continue
        value = _f(row.get("cumulative_delta"))
        aligned_bars.append(candle)
        cvd.append(value)
        series.append({"time": row["time"], "cvd": value, "close": candle["close"]})
    return aligned_bars, cvd, series


def _profile(symbol: str, bars: list[dict[str, Any]], tick_size: float, prior=None):
    # Routed through mp_core's content-addressed cache (2026-08-29): this
    # function used to rebuild the full TPO ladder for the current AND prior
    # session on every evaluation cycle. The prior session's bars never change,
    # so its profile now computes once per process; the developing session
    # recomputes only when a new bar arrives. Same engine, same output.
    from mp_core.service import build_cached_profile

    objects = [MarketBar(timestamp=row["time"], open=_f(row["open"]), high=_f(row["high"]), low=_f(row["low"]), close=_f(row["close"]), volume=_f(row.get("volume"))) for row in bars]
    return build_cached_profile(symbol, objects, tick_size=tick_size,
                                period_minutes=30, initial_balance_periods=2,
                                prior_profile=prior)


def evaluate_rules(
    *,
    symbol: str,
    current_bars: list[dict[str, Any]],
    prior_bars: list[dict[str, Any]],
    history_bars: list[dict[str, Any]],
    ticks: list[dict[str, Any]],
    options: dict[str, Any],
    vix: float | None,
    lot_size: int,
    tick_size: float,
    clock_drift_ms: float | None,
    now: datetime,
    noon_quarantine: bool = True,
    require_vix: bool = True,
    kind: str | None = None,
    directional_bias: str | None = None,
    setup_window_bars: int = 5,
    min_confirmations: int = 2,
    max_chase_atr: float = 0.5,
    min_reward_risk: float = 1.5,
) -> dict[str, Any]:
    # noon_quarantine / require_vix are NSE-session concepts: the 11:45-13:15
    # lunch-liquidity hole and India VIX conditioning don't apply to the MCX
    # evening session, so the commodity lane disables them (the gate keys stay
    # in the payload as True so dashboards render one uniform gate set).
    if len(current_bars) < 4 or len(prior_bars) < 4:
        return {"symbol": symbol, "status": "collecting_data", "action": "FLAT", "blocked_reasons": ["insufficient_3minute_bars"]}
    prior = _profile(symbol, prior_bars, tick_size)
    current = _profile(symbol, current_bars, tick_size, prior=prior)
    footprint = build_footprint(ticks, tick_size)
    bar_series = [{**row, "time": row["time"].isoformat()} for row in current_bars]
    proxy_cvd = bar_cvd(current_bars)
    fp_bars = footprint.get("bars") or []
    aligned_bars, cvd, cvd_series = _aligned_tick_cvd(current_bars, fp_bars)
    divergence = cvd_divergence(aligned_bars, cvd, lookback=min(20, len(cvd))) if len(cvd) >= 4 else cvd_divergence(current_bars, proxy_cvd)
    cvd_source = "market_ticks" if len(cvd) >= 4 else "bar_proxy"
    histogram = volume_node_density(history_bars, bins=40)
    hvns = hvn_lvn(histogram)["hvn"]
    hvn_prices = [(_f(row["price_low"]) + _f(row["price_high"])) / 2 for row in hvns]
    spot = _f(current_bars[-1]["close"])
    tolerance = max(spot * 0.0015, _f(current.initial_balance_range) * 0.25)
    long_setup = _structural_setup(current_bars, [_f(prior.val), *hvn_prices], "LONG", tolerance, active_window_bars=setup_window_bars)
    short_setup = _structural_setup(current_bars, [_f(prior.vah), *hvn_prices], "SHORT", tolerance, active_window_bars=setup_window_bars)
    long_div = divergence is not None and divergence.kind == "bullish" and cvd_source == "market_ticks"
    short_div = divergence is not None and divergence.kind == "bearish" and cvd_source == "market_ticks"
    long_cvd = long_div or (cvd_source == "market_ticks" and _cvd_impulse(cvd, "LONG"))
    short_cvd = short_div or (cvd_source == "market_ticks" and _cvd_impulse(cvd, "SHORT"))
    long_fp, long_ratio = _footprint_trigger(footprint, "LONG", now=now, lookback_bars=2)
    short_fp, short_ratio = _footprint_trigger(footprint, "SHORT", now=now, lookback_bars=2)
    long_price = _price_confirmation(current_bars, long_setup, "LONG")
    short_price = _price_confirmation(current_bars, short_setup, "SHORT")
    long_confirmations = {"cvd_confirmation": long_cvd, "buying_footprint_3x_recent": long_fp, "price_reclaim": long_price}
    short_confirmations = {"cvd_confirmation": short_cvd, "selling_footprint_3x_recent": short_fp, "price_reclaim": short_price}
    long_confirmation_count = sum(long_confirmations.values())
    short_confirmation_count = sum(short_confirmations.values())
    freshness_limit_ms = _freshness_limit_ms(ticks)
    # abs(): tick timestamps come from DB writes whose clock can sit a few ms
    # AHEAD of the app clock — requiring drift >= 0 made this gate fail on all
    # 8 MCX roots while ticks were second-fresh (observed live 2026-07-15
    # 16:18 IST, gate_breakdown tick_fresh:8). Magnitude is what matters.
    tick_fresh = clock_drift_ms is not None and abs(clock_drift_ms) <= freshness_limit_ms
    noon = noon_quarantine and (now.hour == 12 or (now.hour == 11 and now.minute >= 45) or (now.hour == 13 and now.minute <= 15))
    readiness_gates = {
        "tick_fresh": tick_fresh,
        # `real_tick_cvd` is a HISTORICAL KEY NAME (kept: the UI, tests and the
        # lane registry all key off it). What it actually asserts is that CVD
        # came from the observed QUOTE-tick stream rather than from bar shape.
        # It is NOT a claim of trade-print provenance: market_ticks carries no
        # trade id, no per-trade size and no aggressor flag, so `build_footprint`
        # infers every side. See analytics/orderflow.py. Condition unchanged.
        "real_tick_cvd": cvd_source == "market_ticks",
        "outside_noon_quarantine": not noon,
        "vix_available": vix is not None if require_vix else True,
    }
    atr_value = atr(current_bars)
    ib_mid = (_f(current.initial_balance_high) + _f(current.initial_balance_low)) / 2
    long_targets = [_f(current.poc), _f(current.vah), ib_mid, _f(prior.poc), _f(prior.vah), _f(options.get("call_wall"))]
    short_targets = [_f(current.poc), _f(current.val), ib_mid, _f(prior.poc), _f(prior.val), _f(options.get("put_wall"))]
    long_risk = _risk_plan(direction="LONG", entry=spot, setup=long_setup, atr_value=atr_value, targets=long_targets, max_chase_atr=max_chase_atr, min_reward_risk=min_reward_risk)
    short_risk = _risk_plan(direction="SHORT", entry=spot, setup=short_setup, atr_value=atr_value, targets=short_targets, max_chase_atr=max_chase_atr, min_reward_risk=min_reward_risk)
    bias = str(directional_bias or "neutral").lower()
    long_bias_ok = bias in {"neutral", "bullish", "long", "none", ""}
    short_bias_ok = bias in {"neutral", "bearish", "short", "none", ""}
    long_gates = {
        **readiness_gates,
        "directional_bias_aligned": long_bias_ok,
        "structural_setup_armed": bool(long_setup.get("active")),
        "confirmation_2_of_3": long_confirmation_count >= min_confirmations,
        "not_chasing": bool(long_risk["not_chasing"]),
        "reward_risk_1_5": bool(long_risk["reward_risk_valid"]),
    }
    short_gates = {
        **readiness_gates,
        "directional_bias_aligned": short_bias_ok,
        "structural_setup_armed": bool(short_setup.get("active")),
        "confirmation_2_of_3": short_confirmation_count >= min_confirmations,
        "not_chasing": bool(short_risk["not_chasing"]),
        "reward_risk_1_5": bool(short_risk["reward_risk_valid"]),
    }
    long_actionable = all(long_gates.values())
    short_actionable = all(short_gates.values())
    long_score = 35 * int(bool(long_setup.get("active"))) + 15 * long_confirmation_count + 10 * int(bool(long_risk["not_chasing"])) + 10 * int(bool(long_risk["reward_risk_valid"]))
    short_score = 35 * int(bool(short_setup.get("active"))) + 15 * short_confirmation_count + 10 * int(bool(short_risk["not_chasing"])) + 10 * int(bool(short_risk["reward_risk_valid"]))
    direction_conflict = long_actionable and short_actionable and long_score == short_score
    if direction_conflict:
        long_gates["unambiguous_direction"] = False
        short_gates["unambiguous_direction"] = False
        direction = "FLAT"
    elif long_actionable and short_actionable:
        direction = "LONG" if long_score > short_score else "SHORT" if short_score > long_score else "FLAT"
    else:
        direction = "LONG" if long_actionable else "SHORT" if short_actionable else "FLAT"
    preferred = direction if direction != "FLAT" else ("LONG" if long_score >= short_score else "SHORT")
    preferred_setup = long_setup if preferred == "LONG" else short_setup
    preferred_gates = long_gates if preferred == "LONG" else short_gates
    preferred_count = long_confirmation_count if preferred == "LONG" else short_confirmation_count
    preferred_risk = long_risk if preferred == "LONG" else short_risk
    if direction_conflict:
        setup_state = "CONFLICT"
    elif direction != "FLAT":
        setup_state = "CONFIRMED"
    elif preferred_setup.get("state") == "EXPIRED":
        setup_state = "EXPIRED"
    elif not preferred_setup.get("active"):
        setup_state = "WATCHING"
    elif preferred_count < min_confirmations:
        setup_state = "ARMED"
    elif not preferred_risk["not_chasing"]:
        setup_state = "MISSED_NO_CHASE"
    else:
        setup_state = "CONFIRMED_BLOCKED"
    chosen_risk = long_risk if direction == "LONG" else short_risk if direction == "SHORT" else preferred_risk
    confirmation_count = long_confirmation_count if direction == "LONG" else short_confirmation_count if direction == "SHORT" else preferred_count
    base_risk_fraction = 0.01 if confirmation_count == 3 else 0.005
    if vix is not None and vix < 11:
        base_risk_fraction *= 0.5
    quality = "A+" if confirmation_count == 3 and direction != "FLAT" else "VALID" if direction != "FLAT" else setup_state
    return {
        "kind": kind or ("index" if symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"} else "stock"),
        "symbol": symbol,
        "status": "actionable_paper" if direction != "FLAT" else setup_state.lower(),
        "action": direction,
        "preferred_direction": preferred,
        "setup_state": setup_state,
        "quality": quality,
        "spot": spot,
        "score": round(max(long_score, short_score), 2),
        "readiness_gates": readiness_gates,
        "gates": preferred_gates,
        "long_gates": long_gates,
        "short_gates": short_gates,
        "long_confirmations": long_confirmations,
        "short_confirmations": short_confirmations,
        "confirmation_count": confirmation_count,
        "confirmation_required": min_confirmations,
        "long_setup": long_setup,
        "short_setup": short_setup,
        "blocked_reasons": [key for key, value in preferred_gates.items() if not value],
        "profile": {**asdict(current), "prior": {"vah": prior.vah, "val": prior.val, "poc": prior.poc}, "hvn_prices": [round(value, 2) for value in hvn_prices[:8]]},
        "cvd": {"source": cvd_source, "series": cvd_series, "divergence": asdict(divergence) if divergence else None},
        "footprint": {**footprint, "long_ratio": long_ratio, "short_ratio": short_ratio},
        "options": options,
        "risk": {"entry": spot, "atr_3m": atr_value, "stop": chosen_risk["stop"] if direction != "FLAT" else None, "target1": chosen_risk["target1"] if direction != "FLAT" else None, "target2_long": long_risk["target2"], "target2_short": short_risk["target2"], "reward_risk": chosen_risk["reward_risk"], "ib_midpoint": ib_mid, "lot_size": lot_size, "risk_fraction": base_risk_fraction},
        "vix": {"value": vix, "size_multiplier": 0.5 if vix is not None and vix < 11 else 1.0},
        "clock_drift_ms": clock_drift_ms,
        "tick_age_ms": clock_drift_ms,
        "tick_freshness_limit_ms": freshness_limit_ms,
        "bars": bar_series,
    }


# Volatility floor on the RISK DISTANCE used for sizing, as a multiple of the
# instrument's own 3-minute ATR (owner directive 2026-08-17: "sizing as per IV",
# not a fixed price fraction).
#
# A fixed bps floor was the wrong instrument: it is blind to both the instrument
# and the regime — a Rs 319 stock and a 24,000 index share no noise scale, and the
# same name in calm vs stressed vol needs a different floor. ATR is per-instrument
# and per-regime by construction.
#
# 0.5x a 3-minute ATR is the "inside noise" line: a stop nearer than half a 3m bar's
# average true range is not structure, it is tick noise, and sizing 1/stop against
# it is what produced 2308x leverage on DIVISLAB.
#
# NOTE this widens only the SIZING denominator; the working stop stays where
# structure put it. Sizing for volatility while stopping at structure means the
# realised loss lands at or under the risk budget, never over.
MIN_STOP_ATR_MULTIPLE = 0.5

# Hard ceiling on ONE position's notional as a multiple of capital. F&O notional
# legitimately exceeds capital (margin ~10-15%), so this must not clip normal
# sizing: every trade with a stop >=2 bps sat at <=23.4x, so 25x passes the whole
# sane cluster while making 57x / 222x / 2308x arithmetically impossible.
MAX_NOTIONAL_MULTIPLE = 25.0


def lots_for_risk(
    capital: float,
    risk_fraction: float,
    stop_points: float,
    lot_size: int,
    *,
    entry_price: float = 0.0,
    atr: float = 0.0,
    size_multiplier: float = 1.0,
    stop_price: float | None = None,
    direction: str = "",
) -> int:
    """Lots that keep a stop-out within ``capital * risk_fraction``.

    Size is ``1/stop``, so a stop approaching zero approaches infinite leverage —
    ``stop_points > 0`` is nowhere near a sufficient guard.

    **DIVISLAB, 2026-07-28** (the lane's entire lifetime loss in one trade):
    LONG at 7391.0 with ``initial_stop`` **7391.0179 — 1.8 paise ABOVE entry, the
    WRONG SIDE for a long**. The caller passed ``abs(entry - stop)``, which hides
    the sign error, so this sized 3,123 lots x 100 = **Rs 2,308 CRORE notional,
    2308x the Rs 10L account**, and because ``stop_hit = price <= stop`` was
    already true at entry it stopped out instantly (0.86 min) for
    **-Rs 24,98,400** (r = -448). BPCL had the same inverted shape (-Rs 31,501).

    Split cleanly: the **2 wrong-side trades lost Rs 25.3L; the other 24 made
    +Rs 1.59L**. The strategy was fine — the stop side was not.

    Three independent guards, none redundant (each catches what the others miss):

    1. ``direction`` / ``stop_price`` — a stop on the wrong side of entry is an
       upstream bug. Refuse it; never trade a stop that is already breached.
    2. ``atr`` — size against VOLATILITY, not a raw structural stop. The engine
       already computes ``atr_3m`` per instrument and a VIX-derived
       ``size_multiplier``, and until now **read neither**; sizing was purely
       1/structural-stop, which is what let a noise-width stop buy 2308x leverage.
    3. ``MAX_NOTIONAL_MULTIPLE`` — a leverage backstop for when side and stop both
       look sane (HAVELLS at 0.88 bps still reached 57x).

    ``size_multiplier`` is the implied-vol scalar (VIX band today): it shrinks the
    risk budget when volatility is unusual, so exposure falls as conditions widen.
    """
    if capital <= 0 or risk_fraction <= 0 or stop_points <= 0 or lot_size <= 0:
        return 0
    # (1) Wrong-side stop → upstream bug. A long's stop must sit BELOW entry and
    # a short's ABOVE; otherwise the position is stopped out on its first mark.
    if stop_price is not None and entry_price > 0 and direction:
        side = str(direction).strip().upper()
        if side in {"LONG", "BUY"} and stop_price >= entry_price:
            return 0
        if side in {"SHORT", "SELL"} and stop_price <= entry_price:
            return 0
    # (2) Volatility floor. Size off max(structural stop, 0.5 x ATR) so a stop
    # inside the noise band cannot inflate the position. When ATR is unavailable
    # (0), fall through rather than fail closed — a missing volatility read must
    # not silently mute the lane; the side check and notional cap still apply.
    if atr > 0:
        stop_points = max(stop_points, MIN_STOP_ATR_MULTIPLE * atr)
    # IV-scaled risk budget (VIX band). Clamped so a bad feed cannot scale UP.
    risk_fraction *= max(0.0, min(1.0, float(size_multiplier)))
    if risk_fraction <= 0:
        return 0
    lots = max(0, floor((capital * risk_fraction) / (stop_points * lot_size)))
    # (3) Hard notional backstop, independent of the risk maths above.
    if lots > 0 and entry_price > 0:
        max_notional = capital * MAX_NOTIONAL_MULTIPLE
        per_lot_notional = entry_price * lot_size
        if per_lot_notional > 0:
            lots = min(lots, int(floor(max_notional / per_lot_notional)))
    return max(0, lots)


# ── Status-payload compaction ────────────────────────────────────────────────
# The full evaluator result carries the complete 3-min bar series, per-bar
# footprint levels, the whole TPO profile and the CVD series — up to ~100 KB per
# instrument. The multi-instrument /status summary returned ALL of them for
# EVERY instrument (~600+ KB per poll). The desk overview matrix + gate tabs
# only need the small keys; the heavy detail is fetched for one selected
# instrument via the detail endpoint. These helpers slim the summary while the
# persisted state.json (attribution/paper) keeps full detail.

# Heavy keys dropped wholesale from the compact summary.
_HEAVY_RESULT_KEYS = ("profile", "bars")


def compact_result(result: dict) -> dict:
    """Return a compact copy of one evaluator result.

    Keeps every small key the overview matrix, tiles and gate tabs read
    (symbol, kind, status, action, setup_state, gates, confirmations, options,
    risk, vix, tick freshness, blocked_reasons, etc.) and replaces the two
    heavy structured keys (``cvd``, ``footprint``) with compact freshness stubs.
    Drops ``profile`` and ``bars`` entirely.
    """
    if not isinstance(result, dict):
        return result
    compact = {key: value for key, value in result.items() if key not in _HEAVY_RESULT_KEYS}

    footprint = result.get("footprint") or {}
    fp_bars = footprint.get("bars") or []
    compact["footprint"] = {
        "source": footprint.get("source"),
        "long_ratio": footprint.get("long_ratio"),
        "short_ratio": footprint.get("short_ratio"),
        "tick_count": footprint.get("tick_count"),
        "last_bar_time": (fp_bars[-1].get("time") if fp_bars else None),
    }

    cvd = result.get("cvd") or {}
    cvd_series = cvd.get("series") or []
    compact["cvd"] = {
        "source": cvd.get("source"),
        "divergence": cvd.get("divergence"),
        "last_bar_time": (cvd_series[-1].get("time") if cvd_series else None),
    }
    return compact


def compact_state(state: dict) -> dict:
    """Return a copy of a persisted run-cycle state with every result compacted.

    Non-``results`` keys (universe, paper, gate_breakdown, …) pass through
    untouched. A state with no results is returned unchanged.
    """
    if not isinstance(state, dict) or not state.get("results"):
        return state
    slim = dict(state)
    slim["results"] = [compact_result(row) for row in state.get("results") or []]
    return slim


def detail_result(state: dict, symbol: str) -> Optional[dict]:
    """Return the single FULL evaluator result for ``symbol`` from a state.

    Case-insensitive symbol match; ``None`` when absent.
    """
    if not isinstance(state, dict):
        return None
    target = str(symbol or "").strip().upper()
    for row in state.get("results") or []:
        if str(row.get("symbol") or "").strip().upper() == target:
            return row
    return None
