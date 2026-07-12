from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from math import floor
from typing import Any

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


def _footprint_trigger(footprint: dict[str, Any], direction: str) -> tuple[bool, float]:
    rows = footprint.get("bars") or []
    if not rows:
        return False, 0.0
    levels = rows[-1].get("levels") or []
    if not levels:
        return False, 0.0
    span = max(1, len(levels) // 3)
    tail = levels[:span] if direction == "LONG" else levels[-span:]
    key = "buy_ratio" if direction == "LONG" else "sell_ratio"
    ratio = max((_f(row.get(key)) for row in tail), default=0.0)
    return ratio >= 3.0, ratio


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
    engine = MarketProfileEngine({"period_minutes": 30, "tick_size": tick_size, "initial_balance_periods": 2})
    objects = [MarketBar(timestamp=row["time"], open=_f(row["open"]), high=_f(row["high"]), low=_f(row["low"]), close=_f(row["close"]), volume=_f(row.get("volume"))) for row in bars]
    return engine.build_profile(symbol, objects, prior_profile=prior)


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
) -> dict[str, Any]:
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
    near_hvn = any(abs(spot - level) <= tolerance for level in hvn_prices)
    long_structure = spot <= _f(prior.val) + tolerance or near_hvn
    short_structure = spot >= _f(prior.vah) - tolerance or abs(spot - _f(prior.poc)) <= tolerance
    long_div = divergence is not None and divergence.kind == "bullish" and cvd_source == "market_ticks"
    short_div = divergence is not None and divergence.kind == "bearish" and cvd_source == "market_ticks"
    long_fp, long_ratio = _footprint_trigger(footprint, "LONG")
    short_fp, short_ratio = _footprint_trigger(footprint, "SHORT")
    clock_ok = clock_drift_ms is not None and abs(clock_drift_ms) <= 1000.0
    noon = now.hour == 12 or (now.hour == 11 and now.minute >= 45) or (now.hour == 13 and now.minute <= 15)
    data_gates = {"clock_sync": clock_ok, "real_tick_cvd": cvd_source == "market_ticks", "outside_noon_quarantine": not noon, "vix_available": vix is not None}
    long_gates = {**data_gates, "structural_trap": long_structure, "bullish_cvd_divergence": long_div, "buying_footprint_3x": long_fp}
    short_gates = {**data_gates, "structural_ceiling": short_structure, "bearish_cvd_divergence": short_div, "selling_footprint_3x": short_fp}
    direction = "LONG" if all(long_gates.values()) else "SHORT" if all(short_gates.values()) else "FLAT"
    trigger = current_bars[-1]
    atr_value = atr(current_bars)
    stop_mult = 3.0 if vix is not None and vix > 22 else 2.0
    stop = _f(trigger["low"]) - stop_mult * atr_value if direction == "LONG" else _f(trigger["high"]) + stop_mult * atr_value if direction == "SHORT" else None
    entry = spot
    risk_points = abs(entry - stop) if stop is not None else 0.0
    target1 = entry + risk_points if direction == "LONG" else entry - risk_points if direction == "SHORT" else None
    ib_mid = (_f(current.initial_balance_high) + _f(current.initial_balance_low)) / 2
    return {
        "kind": "index" if symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"} else "stock",
        "symbol": symbol,
        "status": "actionable_paper" if direction != "FLAT" else "blocked",
        "action": direction,
        "spot": spot,
        "score": round(max(sum(long_gates.values()), sum(short_gates.values())) / len(long_gates) * 100, 2),
        "gates": long_gates if direction != "SHORT" else short_gates,
        "long_gates": long_gates,
        "short_gates": short_gates,
        "blocked_reasons": [key for key, value in (long_gates if direction != "SHORT" else short_gates).items() if not value],
        "profile": {**asdict(current), "prior": {"vah": prior.vah, "val": prior.val, "poc": prior.poc}, "hvn_prices": [round(value, 2) for value in hvn_prices[:8]]},
        "cvd": {"source": cvd_source, "series": cvd_series, "divergence": asdict(divergence) if divergence else None},
        "footprint": {**footprint, "long_ratio": long_ratio, "short_ratio": short_ratio},
        "options": options,
        "risk": {"entry": entry, "atr_3m": atr_value, "stop_multiplier": stop_mult, "stop": stop, "target1": target1, "ib_midpoint": ib_mid, "target2_long": options.get("call_wall"), "target2_short": options.get("put_wall"), "lot_size": lot_size, "risk_fraction": 0.005 if vix is not None and vix < 11 else 0.01},
        "vix": {"value": vix, "size_multiplier": 0.5 if vix is not None and vix < 11 else 1.0, "stop_multiplier": stop_mult},
        "clock_drift_ms": clock_drift_ms,
        "bars": bar_series,
    }


def lots_for_risk(capital: float, risk_fraction: float, stop_points: float, lot_size: int) -> int:
    if capital <= 0 or risk_fraction <= 0 or stop_points <= 0 or lot_size <= 0:
        return 0
    return max(0, floor((capital * risk_fraction) / (stop_points * lot_size)))
