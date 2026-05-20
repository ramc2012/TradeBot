"""Institutional order-flow visualization API.

This router intentionally composes the existing broker-backed Auction IQ live
snapshot rather than creating a second market-data fetch path.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import text

from auction_intelligence.live import (
    SYMBOL_MAP,
    _aggregate_rows,
    _fetch_recent_minute_rows,
    _group_rows_by_session,
    _session_bounds,
    build_live_analysis,
)
from core.config import settings
from db.database import AsyncSessionLocal
from market_data.commodity_runtime_history import load_commodity_history_rows
from market_data.upstox_commodity import load_upstox_mcx_quote_snapshots


router = APIRouter(prefix="/api/orderflow", tags=["orderflow"])

SUPPORTED_SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL")
SUPPORTED_INTERVALS = (3, 5, 15, 30)
IST = ZoneInfo("Asia/Kolkata")
_CRUDE_TICK_SEED_LOCK = asyncio.Lock()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: Any) -> float | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _time_label(value: Any) -> str:
    parsed = _parse_dt(value)
    if parsed is None:
        return ""
    return parsed.astimezone(IST).strftime("%H:%M")


def _round_price(value: float, tick_size: float) -> float:
    tick = max(float(tick_size or 0.5), 0.01)
    return round(round(value / tick) * tick, 2)


async def _seed_crude_tick_orderflow() -> dict[str, Any]:
    """Bridge live MCX quote/history snapshots into the shared tick table.

    The app's websocket router currently subscribes to index symbols only.
    Crude quotes arrive through the commodity broker path, so we seed recent
    1-minute commodity rows as tick-like observations for the existing
    Auction IQ tick reconstruction engine.
    """
    async with _CRUDE_TICK_SEED_LOCK:
        symbol = str(SYMBOL_MAP["CRUDEOIL"]["app_symbol"])
        tick_size = _num(SYMBOL_MAP["CRUDEOIL"].get("tick_size"), 1.0)
        rows, history_symbol = await load_commodity_history_rows(
            "CRUDEOIL",
            interval="1minute",
            lookback_days=2,
            persist=True,
        )
        usable_rows = [row for row in rows[-80:] if _num(row.get("close")) > 0]
        if not usable_rows:
            return {"seeded": 0, "symbol": symbol, "history_symbol": history_symbol, "reason": "no_history_rows"}

        quote_snapshots = await load_upstox_mcx_quote_snapshots([symbol])
        live_quote = quote_snapshots.get(symbol) or {}
        live_price = _num(live_quote.get("price"))
        if live_price > 0:
            latest = dict(usable_rows[-1])
            now = datetime.now(timezone.utc)
            latest.update(
                {
                    "time": now,
                    "open": _num(latest.get("open"), live_price),
                    "high": max(_num(latest.get("high"), live_price), live_price),
                    "low": min(_num(latest.get("low"), live_price), live_price),
                    "close": live_price,
                    "volume": max(_num(latest.get("volume")), _num(usable_rows[-1].get("volume"))),
                    "oi": _num(latest.get("oi")),
                }
            )
            usable_rows.append(latest)

        payload: list[dict[str, Any]] = []
        cumulative_volume = 0.0
        for row in usable_rows[-60:]:
            ts = _parse_dt(row.get("time") or row.get("timestamp"))
            close = _num(row.get("close"))
            if ts is None or close <= 0:
                continue
            volume = max(_num(row.get("volume")), 1.0)
            cumulative_volume += volume
            spread = max(tick_size, close * 0.00008)
            bid = _round_price(close - spread / 2, tick_size)
            ask = _round_price(close + spread / 2, tick_size)
            payload.append(
                {
                    "time": ts,
                    "symbol": symbol,
                    "ltp": close,
                    "open": _num(row.get("open"), close),
                    "high": _num(row.get("high"), close),
                    "low": _num(row.get("low"), close),
                    "close": close,
                    "volume": int(cumulative_volume),
                    "oi": int(_num(row.get("oi"))),
                    "bid": bid,
                    "ask": ask,
                    "bid_qty": int(max(volume * 0.52, 1.0)),
                    "ask_qty": int(max(volume * 0.48, 1.0)),
                }
            )

        if not payload:
            return {"seeded": 0, "symbol": symbol, "history_symbol": history_symbol, "reason": "empty_payload"}

        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO market_ticks (
                        time, symbol, ltp, open, high, low, close, volume,
                        oi, bid, ask, bid_qty, ask_qty
                    ) VALUES (
                        :time, :symbol, :ltp, :open, :high, :low, :close, :volume,
                        :oi, :bid, :ask, :bid_qty, :ask_qty
                    )
                    """
                ),
                payload,
            )
            await session.commit()

        return {
            "seeded": len(payload),
            "symbol": symbol,
            "history_symbol": history_symbol,
            "latest_time": payload[-1]["time"].isoformat(),
            "latest_ltp": payload[-1]["ltp"],
            "quote_source": live_quote.get("source") or "commodity_runtime_history",
        }


def _pressure_split(open_price: float, close_price: float, high: float, low: float, flow_bias: float) -> float:
    span = max(high - low, 0.01)
    close_location = ((close_price - low) / span) - 0.5
    direction = 0.35 if close_price > open_price else -0.35 if close_price < open_price else 0.0
    pressure = 0.5 + direction + (close_location * 0.35) + (max(min(flow_bias, 1.0), -1.0) * 0.15)
    return max(0.08, min(0.92, pressure))


def _build_price_levels(
    *,
    bar: dict[str, Any],
    tick_size: float,
    buy_volume: float,
    sell_volume: float,
) -> list[dict[str, Any]]:
    high = _num(bar.get("high"))
    low = _num(bar.get("low"))
    close_price = _num(bar.get("close"))
    span = max(high - low, tick_size * 8)
    raw_steps = max(5, min(11, int(span / max(tick_size, 0.01)) + 1))
    step = max(tick_size, span / (raw_steps - 1))
    levels: list[dict[str, Any]] = []
    weights: list[float] = []

    for index in range(raw_steps):
        price = _round_price(low + (index * step), tick_size)
        distance = abs(price - close_price) / max(span, 0.01)
        weight = max(0.08, 1.0 - distance * 1.65)
        weights.append(weight)
        levels.append({"price": price})

    total_weight = sum(weights) or 1.0
    for level, weight in zip(levels, weights):
        bid_volume = sell_volume * weight / total_weight
        ask_volume = buy_volume * weight / total_weight
        delta = ask_volume - bid_volume
        total = ask_volume + bid_volume
        imbalance = delta / max(total, 1.0)
        level.update(
            {
                "bid_volume": round(bid_volume, 2),
                "ask_volume": round(ask_volume, 2),
                "delta": round(delta, 2),
                "imbalance": round(imbalance, 4),
                "intensity": round(min(1.0, total / max((buy_volume + sell_volume) * 0.22, 1.0)), 4),
            }
        )
    return levels


def _build_footprint_from_bars(
    *,
    bars: list[dict[str, Any]],
    symbol: str,
    tick_size: float,
    flow_bias: float,
    trades: list[dict[str, Any]] | None = None,
    max_bars: int = 90,
) -> list[dict[str, Any]]:
    bars = list(bars or [])[-max_bars:]
    trades = list(trades or [])
    trade_totals_by_time: dict[str, dict[str, float]] = {}
    for trade in trades:
        label = _time_label(trade.get("timestamp"))
        if not label:
            continue
        side = str(trade.get("aggressor_side") or "").lower()
        bucket = trade_totals_by_time.setdefault(label, {"buy": 0.0, "sell": 0.0})
        if side == "buy":
            bucket["buy"] += _num(trade.get("quantity"))
        elif side == "sell":
            bucket["sell"] += _num(trade.get("quantity"))

    rows: list[dict[str, Any]] = []
    cumulative = 0.0
    for bar in bars:
        open_price = _num(bar.get("open"))
        high = _num(bar.get("high"), open_price)
        low = _num(bar.get("low"), open_price)
        close_price = _num(bar.get("close"), open_price)
        volume = max(_num(bar.get("volume")), 1.0)
        label = _time_label(bar.get("timestamp"))
        trade_bucket = trade_totals_by_time.get(label, {})
        buy_hint = _num(trade_bucket.get("buy"))
        sell_hint = _num(trade_bucket.get("sell"))
        if buy_hint + sell_hint > 0:
            scale = volume / max(buy_hint + sell_hint, 1.0)
            buy_volume = buy_hint * scale
            sell_volume = sell_hint * scale
        else:
            split = _pressure_split(open_price, close_price, high, low, flow_bias)
            buy_volume = volume * split
            sell_volume = volume - buy_volume
        delta = buy_volume - sell_volume
        cumulative += delta
        total = buy_volume + sell_volume
        rows.append(
            {
                "symbol": symbol,
                "timestamp": bar.get("timestamp"),
                "label": label,
                "open": round(open_price, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(close_price, 2),
                "total_volume": round(total, 2),
                "buy_volume": round(buy_volume, 2),
                "sell_volume": round(sell_volume, 2),
                "delta": round(delta, 2),
                "cumulative_delta": round(cumulative, 2),
                "imbalance": round(delta / max(total, 1.0), 4),
                "levels": _build_price_levels(
                    bar=bar,
                    tick_size=tick_size,
                    buy_volume=buy_volume,
                    sell_volume=sell_volume,
                ),
            }
        )
    return rows


def _build_footprint(snapshot: dict[str, Any], symbol: str, tick_size: float) -> list[dict[str, Any]]:
    request = snapshot.get("request") or {}
    analysis = snapshot.get("analysis") or {}
    order_flow = analysis.get("order_flow") or {}
    flow_bias = _num(order_flow.get("order_flow_imbalance") or order_flow.get("trade_imbalance"))
    return _build_footprint_from_bars(
        bars=list(request.get("bars") or []),
        symbol=symbol,
        tick_size=tick_size,
        flow_bias=flow_bias,
        trades=list(request.get("trades") or []),
        max_bars=16,
    )


def _build_historical_flow_markers(footprint: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not footprint:
        return []
    avg_volume = sum(_num(row.get("total_volume")) for row in footprint) / max(len(footprint), 1)
    max_abs_delta = max((abs(_num(row.get("delta"))) for row in footprint), default=1.0)
    markers: list[dict[str, Any]] = []
    for row in footprint:
        volume = _num(row.get("total_volume"))
        delta = _num(row.get("delta"))
        volume_score = volume / max(avg_volume, 1.0)
        delta_score = abs(delta) / max(max_abs_delta, 1.0)
        score = min(96, round(38 + volume_score * 18 + delta_score * 34))
        if score < 64:
            continue
        direction = "BULLISH" if delta >= 0 else "BEARISH"
        markers.append(
            {
                "id": f"HIST-{row.get('timestamp')}-{round(_num(row.get('close')), 2)}",
                "timestamp": row.get("timestamp"),
                "label": "HIST FLOW BLOCK",
                "side": "BUY" if delta >= 0 else "SELL",
                "direction": direction,
                "price": _num(row.get("close")),
                "strike": None,
                "notional": round(volume * _num(row.get("close")), 2),
                "volume": round(volume, 2),
                "oi_change": None,
                "score": int(score),
                "source": "historical_bar_footprint",
            }
        )
    return sorted(markers, key=lambda item: int(item["score"]), reverse=True)[:10]


async def _build_timeframe_history(
    symbol: str,
    *,
    tick_size: float,
    flow_bias: float,
    intervals: list[int],
    history_sessions: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    recent_rows, history_source, history_symbol = await _fetch_recent_minute_rows(
        symbol,
        lookback_days=max(7, history_sessions * 3),
        allow_live_broker_refresh=not (
            settings.MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY or settings.PAPER_TRADING_ONLY
        ),
    )
    session_open, _ = _session_bounds(symbol)
    sessions = _group_rows_by_session(
        recent_rows,
        allow_partial_live_session=True,
        symbol_code=symbol,
    )
    session_dates = sorted(sessions.keys())[-max(1, min(history_sessions, 8)):]
    timeframe_payload: dict[str, Any] = {}
    for interval in intervals:
        session_payloads: list[dict[str, Any]] = []
        for session_date in session_dates:
            bars = _aggregate_rows(
                sessions[session_date],
                interval_minutes=interval,
                session_open=session_open,
            )
            max_bars = 140 if interval <= 3 else 120 if interval <= 5 else 96
            footprint = _build_footprint_from_bars(
                bars=bars,
                symbol=symbol,
                tick_size=tick_size,
                flow_bias=flow_bias,
                max_bars=max_bars,
            )
            if not footprint:
                continue
            session_payloads.append(
                {
                    "session_date": session_date.isoformat(),
                    "interval_minutes": interval,
                    "bar_count": len(footprint),
                    "open": footprint[0]["open"],
                    "high": max(_num(row.get("high")) for row in footprint),
                    "low": min(_num(row.get("low")) for row in footprint),
                    "close": footprint[-1]["close"],
                    "delta": round(sum(_num(row.get("delta")) for row in footprint), 2),
                    "volume": round(sum(_num(row.get("total_volume")) for row in footprint), 2),
                    "footprint": footprint,
                    "whales": _build_historical_flow_markers(footprint),
                }
            )
        timeframe_payload[str(interval)] = {
            "interval_minutes": interval,
            "session_count": len(session_payloads),
            "sessions": session_payloads,
        }
    return timeframe_payload, {
        "history_source": history_source,
        "history_symbol": history_symbol,
        "session_dates": [item.isoformat() for item in session_dates],
    }


def _build_heatmap(snapshot: dict[str, Any], price: float) -> list[dict[str, Any]]:
    request = snapshot.get("request") or {}
    analysis = snapshot.get("analysis") or {}
    market_profile = analysis.get("market_profile") or {}
    depth = request.get("depth") or {}
    heatmap: list[dict[str, Any]] = []

    profile_levels = [
        ("POC", market_profile.get("poc"), "poc", 0.92),
        ("VAH", market_profile.get("vah"), "value_area", 0.62),
        ("VAL", market_profile.get("val"), "value_area", 0.62),
        ("IBH", market_profile.get("initial_balance_high"), "initial_balance", 0.46),
        ("IBL", market_profile.get("initial_balance_low"), "initial_balance", 0.46),
        ("VWAP", (analysis.get("order_flow") or {}).get("vwap"), "vwap", 0.54),
    ]
    for label, raw_level, kind, base_intensity in profile_levels:
        level = _num(raw_level)
        if level <= 0:
            continue
        proximity = 1.0 / (1.0 + abs(level - price) / max(price * 0.01, 1.0))
        heatmap.append(
            {
                "price": round(level, 2),
                "side": "reference",
                "label": label,
                "kind": kind,
                "quantity": None,
                "intensity": round(min(1.0, base_intensity + proximity * 0.18), 4),
            }
        )

    depth_levels = []
    for side, rows in (("bid", depth.get("bids") or []), ("ask", depth.get("asks") or [])):
        for row in rows:
            depth_levels.append((side, _num(row.get("price")), _num(row.get("quantity"))))
    max_depth = max((quantity for _, _, quantity in depth_levels), default=1.0)
    for side, level, quantity in depth_levels:
        if level <= 0:
            continue
        heatmap.append(
            {
                "price": round(level, 2),
                "side": side,
                "label": side.upper(),
                "kind": "book_depth",
                "quantity": round(quantity, 2),
                "intensity": round(min(1.0, quantity / max(max_depth, 1.0)), 4),
            }
        )
    return sorted(heatmap, key=lambda item: float(item["price"]))


def _build_whales(snapshot: dict[str, Any], tick_size: float) -> list[dict[str, Any]]:
    analysis = snapshot.get("analysis") or {}
    request = snapshot.get("request") or {}
    ntm = analysis.get("ntm_volx") or {}
    snapshot_time = ntm.get("snapshot_time") or (request.get("metadata") or {}).get("snapshot_time")
    source = ntm.get("source") or "broker_option_chain"
    candidates: list[dict[str, Any]] = []

    ladder = list(ntm.get("pressure_ladder") or [])
    side_rows: list[dict[str, Any]] = []
    for level in ladder:
        strike = _num(level.get("strike"))
        for side in ("CALL", "PUT"):
            prefix = "call" if side == "CALL" else "put"
            side_rows.append(
                {
                    "side": side,
                    "price": strike,
                    "strike": strike,
                    "notional": _num(level.get(f"{prefix}_notional")),
                    "volume": _num(level.get(f"{prefix}_volume")),
                    "oi_change": _num(level.get(f"{prefix}_oi_change")),
                    "pressure": _num(level.get(f"{prefix}_pressure")),
                    "net_pressure": _num(level.get("net_pressure")),
                    "distance_pct": _num(level.get("distance_from_spot_pct")),
                    "timestamp": level.get("observed_at") or snapshot_time,
                    "source": level.get("source") or source,
                }
            )

    max_notional = max((_num(row.get("notional")) for row in side_rows), default=1.0)
    max_pressure = max((_num(row.get("pressure")) for row in side_rows), default=1.0)
    for row in side_rows:
        if row["notional"] <= 0 and row["pressure"] <= 0:
            continue
        aligned = row["net_pressure"] > 0 if row["side"] == "CALL" else row["net_pressure"] < 0
        score = min(
            99,
            round(
                25
                + (row["notional"] / max(max_notional, 1.0)) * 36
                + (row["pressure"] / max(max_pressure, 1.0)) * 24
                + (8 if abs(row["distance_pct"]) <= 1.25 else 0)
                + (6 if aligned else 0),
            ),
        )
        if score < 55:
            continue
        kind = "WHALE OPENING" if row["oi_change"] > 0 and row["volume"] >= row["oi_change"] * 0.65 else "BLOCK PREMIUM"
        candidates.append(
            {
                "id": f"{row['side']}-{row['strike']:.0f}",
                "timestamp": row["timestamp"],
                "label": kind,
                "side": row["side"],
                "direction": "BULLISH" if row["side"] == "CALL" else "BEARISH",
                "price": round(row["price"], 2),
                "strike": round(row["strike"], 2),
                "notional": round(row["notional"], 2),
                "volume": round(row["volume"], 2),
                "oi_change": round(row["oi_change"], 2),
                "score": int(score),
                "source": row["source"],
            }
        )

    trades = list(request.get("trades") or [])
    quantities = [_num(item.get("quantity")) for item in trades if _num(item.get("quantity")) > 0]
    avg_quantity = sum(quantities) / max(len(quantities), 1)
    print_threshold = max(avg_quantity * 2.5, 1.0)
    for index, trade in enumerate(trades[-80:]):
        quantity = _num(trade.get("quantity"))
        price = _num(trade.get("price"))
        if quantity < print_threshold or price <= 0:
            continue
        side = str(trade.get("aggressor_side") or "unknown").upper()
        candidates.append(
            {
                "id": f"PRINT-{index}-{price}",
                "timestamp": trade.get("timestamp"),
                "label": "LARGE PRINT",
                "side": side,
                "direction": "BULLISH" if side == "BUY" else "BEARISH" if side == "SELL" else "NEUTRAL",
                "price": _round_price(price, tick_size),
                "strike": None,
                "notional": round(price * quantity, 2),
                "volume": round(quantity, 2),
                "oi_change": None,
                "score": min(99, round(50 + (quantity / max(print_threshold, 1.0)) * 12)),
                "source": "broker_tick_reconstruction",
            }
        )

    return sorted(candidates, key=lambda item: int(item["score"]), reverse=True)[:14]


async def _build_instrument_payload(
    symbol: str,
    snapshot: dict[str, Any],
    *,
    intervals: list[int],
    history_sessions: int,
) -> dict[str, Any]:
    request = snapshot.get("request") or {}
    metadata = request.get("metadata") or {}
    session = request.get("session") or {}
    quote = request.get("quote") or {}
    analysis = snapshot.get("analysis") or {}
    order_flow = analysis.get("order_flow") or {}
    market_profile = analysis.get("market_profile") or {}
    config = SYMBOL_MAP[symbol]
    tick_size = _num(config.get("tick_size"), 0.5)
    price = _num(session.get("last_price") or quote.get("last_price") or market_profile.get("close_price"))
    timestamp = quote.get("timestamp") or metadata.get("snapshot_time")
    age = _age_seconds(timestamp)
    footprint = _build_footprint(snapshot, symbol, tick_size)
    whales = _build_whales(snapshot, tick_size)
    flow_bias = _num(order_flow.get("order_flow_imbalance") or order_flow.get("trade_imbalance"))
    try:
        timeframes, history_meta = await _build_timeframe_history(
            symbol,
            tick_size=tick_size,
            flow_bias=flow_bias,
            intervals=intervals,
            history_sessions=history_sessions,
        )
    except Exception as exc:
        timeframes = {}
        history_meta = {"error": str(exc)}
    return {
        "symbol": symbol,
        "display": str(config.get("display") or symbol),
        "market": "MCX" if symbol == "CRUDEOIL" else "BSE" if symbol == "SENSEX" else "NSE",
        "instrument_proxy": metadata.get("instrument_proxy") or config.get("instrument_proxy"),
        "price": round(price, 2),
        "change": round(_num(market_profile.get("close_price")) - _num(market_profile.get("open_price")), 2),
        "change_pct": round(
            ((_num(market_profile.get("close_price")) - _num(market_profile.get("open_price"))) / max(_num(market_profile.get("open_price")), 1.0)) * 100,
            4,
        ),
        "timestamp": timestamp,
        "age_seconds": round(age, 3) if age is not None else None,
        "data_quality": metadata.get("data_status") or snapshot.get("data_status") or {},
        "source": {
            "history": metadata.get("history_source"),
            "history_symbol": metadata.get("history_symbol"),
            "quote": metadata.get("quote_source"),
            "order_flow": metadata.get("order_flow_source"),
            "common_fetch": "auction_intelligence.live.build_live_analysis",
        },
        "session": {
            "date": snapshot.get("session_date"),
            "mode": metadata.get("snapshot_mode"),
            "lot_size": metadata.get("lot_size") or config.get("lot_size"),
            "tick_size": tick_size,
        },
        "metrics": {
            "spread": _num(order_flow.get("spread")),
            "mid_price": _num(order_flow.get("mid_price")),
            "micro_price": _num(order_flow.get("micro_price")),
            "top_imbalance": _num(order_flow.get("top_imbalance")),
            "depth_imbalance": _num(order_flow.get("depth_imbalance")),
            "delta": _num(order_flow.get("delta")),
            "cumulative_delta": _num(order_flow.get("cumulative_delta")),
            "vwap": _num(order_flow.get("vwap")),
            "vwap_drift": _num(order_flow.get("vwap_drift")),
            "queue_pressure": _num(order_flow.get("queue_pressure")),
            "trade_imbalance": _num(order_flow.get("trade_imbalance")),
            "order_flow_imbalance": _num(order_flow.get("order_flow_imbalance")),
            "book_pressure": _num(order_flow.get("book_pressure")),
            "toxicity_score": _num(order_flow.get("toxicity_score") or order_flow.get("adverse_selection_risk")),
            "timing_confidence": _num(order_flow.get("timing_confidence")),
            "execution_aggression": order_flow.get("execution_aggression"),
        },
        "market_profile": {
            "poc": _num(market_profile.get("poc")),
            "vah": _num(market_profile.get("vah")),
            "val": _num(market_profile.get("val")),
            "initial_balance_high": _num(market_profile.get("initial_balance_high")),
            "initial_balance_low": _num(market_profile.get("initial_balance_low")),
            "day_type": market_profile.get("day_type"),
            "trend": market_profile.get("trend"),
        },
        "footprint": footprint,
        "timeframes": timeframes,
        "history": history_meta,
        "heatmap": _build_heatmap(snapshot, price),
        "whales": whales,
        "ntm_volx": analysis.get("ntm_volx"),
        "raw_bar_count": len(request.get("bars") or []),
        "raw_trade_count": len(request.get("trades") or []),
    }


async def _load_symbol(symbol: str, *, intervals: list[int], history_sessions: int) -> dict[str, Any]:
    crude_tick_seed: dict[str, Any] | None = None
    if symbol == "CRUDEOIL":
        try:
            crude_tick_seed = await _seed_crude_tick_orderflow()
        except Exception as exc:
            crude_tick_seed = {"seeded": 0, "error": str(exc)}
    try:
        snapshot = await build_live_analysis(symbol_code=symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        return {
            "symbol": symbol,
            "display": symbol,
            "error": str(exc),
            "source": {"common_fetch": "auction_intelligence.live.build_live_analysis"},
        }
    payload = await _build_instrument_payload(
        symbol,
        snapshot,
        intervals=intervals,
        history_sessions=history_sessions,
    )
    if crude_tick_seed is not None:
        payload["crude_tick_seed"] = crude_tick_seed
        payload.setdefault("source", {})["commodity_tick_bridge"] = "commodity_runtime_history+upstox_quote"
    return payload


def _parse_symbols(raw_symbols: str | None) -> list[str]:
    if not raw_symbols:
        return list(SUPPORTED_SYMBOLS)
    symbols = [item.strip().upper() for item in raw_symbols.split(",") if item.strip()]
    unsupported = [item for item in symbols if item not in SUPPORTED_SYMBOLS]
    if unsupported:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported symbols: {', '.join(unsupported)}. Supported: {', '.join(SUPPORTED_SYMBOLS)}",
        )
    return symbols or list(SUPPORTED_SYMBOLS)


def _parse_intervals(raw_intervals: str | None) -> list[int]:
    if not raw_intervals:
        return list(SUPPORTED_INTERVALS)
    intervals: list[int] = []
    for item in raw_intervals.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            interval = int(item)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid interval: {item}") from exc
        if interval not in SUPPORTED_INTERVALS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported interval: {interval}. Supported: {', '.join(map(str, SUPPORTED_INTERVALS))}",
            )
        intervals.append(interval)
    return intervals or list(SUPPORTED_INTERVALS)


@router.get("/snapshot")
async def orderflow_snapshot(
    symbols: str | None = Query(
        default=None,
        description="Comma separated symbols. Defaults to NIFTY,BANKNIFTY,SENSEX,CRUDEOIL.",
    ),
    intervals: str | None = Query(
        default="3,5,15,30",
        description="Comma separated chart intervals in minutes. Supported: 3,5,15,30.",
    ),
    history_sessions: int = Query(default=5, ge=1, le=8),
) -> dict[str, Any]:
    requested_symbols = _parse_symbols(symbols)
    requested_intervals = _parse_intervals(intervals)
    instruments = await asyncio.gather(
        *(
            _load_symbol(
                symbol,
                intervals=requested_intervals,
                history_sessions=history_sessions,
            )
            for symbol in requested_symbols
        )
    )
    return jsonable_encoder(
        {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "symbols": requested_symbols,
            "intervals": requested_intervals,
            "history_sessions": history_sessions,
            "instruments": instruments,
            "reference_model": {
                "heatmap": "Bookmap-style resting/reference liquidity bands from broker depth proxy plus MP levels.",
                "footprint": "ATAS/Sierra-style bid/ask delta footprint derived from broker ticks when available, else minute-bar inference.",
                "whales": "Option-chain pressure ladder plus large reconstructed prints; raw exchange MBO sweep data is not available.",
            },
        }
    )
