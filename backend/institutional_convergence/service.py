from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from auction_intelligence.live import build_live_analysis
from cbe_scanner.repository import load_latest_scan_payload
from core.config import settings
from core.trading_calendar import trading_calendar
from db.database import AsyncSessionLocal
from market_data import data_router as market_data_router
from paper_engine.base_strategy_agent import _now_ist
from sqlalchemy import text

from .engine import evaluate_rules, tick_clock_drift_ms
from .paper import convergence_paper_book


DEFAULT_INDICES = ("NIFTY", "BANKNIFTY")
INDEX_ROOTS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX"}
STATE_FILE = Path(__file__).resolve().parents[1] / "runtime" / "institutional_convergence" / "state.json"
RTH_START = time(9, 15)
RTH_END = time(15, 30)
MIN_COMPLETE_SESSION_BARS = 80
_VIX_CACHE: tuple[datetime, float | None] | None = None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _filter_session_ohlc(
    rows: list[dict[str, Any]],
    *,
    relative_tolerance: float,
) -> list[dict[str, Any]]:
    """Drop contaminated candles against each session's median close."""
    sessions: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        timestamp = row.get("time")
        session_day = timestamp.date() if isinstance(timestamp, datetime) else None
        sessions.setdefault(session_day, []).append(row)

    clean: list[dict[str, Any]] = []
    for session_rows in sessions.values():
        closes = sorted(
            _number(row.get("close"))
            for row in session_rows
            if _number(row.get("close")) > 0
        )
        if len(closes) < 3:
            clean.extend(session_rows)
            continue
        reference = closes[len(closes) // 2]
        lower = reference * (1.0 - relative_tolerance)
        upper = reference * (1.0 + relative_tolerance)
        for row in session_rows:
            values = [_number(row.get(key)) for key in ("open", "high", "low", "close")]
            if (
                all(lower <= value <= upper for value in values)
                and values[1] >= values[2]
            ):
                clean.append(row)
    clean.sort(key=lambda row: row["time"])
    return clean


def configured_indices() -> list[str]:
    raw = str(getattr(settings, "INSTITUTIONAL_CONVERGENCE_INDEX_SYMBOLS", "NIFTY,BANKNIFTY"))
    values = [item.strip().upper() for item in raw.split(",") if item.strip()]
    return list(dict.fromkeys(values or DEFAULT_INDICES))


def select_diversified_stocks(payload: dict[str, Any] | None, limit: int = 10) -> list[dict[str, Any]]:
    """Select the strongest actionable name per sector, never duplicating sectors."""
    watchlist = list((payload or {}).get("watchlist") or [])
    results = list((payload or {}).get("results") or [])
    watchlist_symbols = {str(row.get("instrument") or "").strip().upper() for row in watchlist}
    rows = [*watchlist, *(row for row in results if str(row.get("instrument") or "").strip().upper() not in watchlist_symbols)]
    ranked = sorted(
        rows,
        key=lambda row: _number(row.get("composite_alpha_score"), _number(row.get("composite_score"))),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    sectors: set[str] = set()
    symbols: set[str] = set()
    for row in ranked:
        symbol = str(row.get("instrument") or "").strip().upper()
        sector = str(row.get("sector_code") or "UNCLASSIFIED").strip().upper()
        bias = str(row.get("directional_bias") or "neutral").lower()
        if (
            not symbol
            or symbol in INDEX_ROOTS
            or symbol in symbols
            or sector == "UNCLASSIFIED"
            or sector in sectors
            or bias not in {"bullish", "bearish"}
        ):
            continue
        selected.append(
            {
                "symbol": symbol,
                "sector": sector,
                "directional_bias": bias,
                "alpha_score": round(
                    _number(row.get("composite_alpha_score"), _number(row.get("composite_score"))), 4
                ),
                "latest_close": row.get("latest_close"),
                "stock_quadrant": row.get("stock_quadrant"),
                "sector_quadrant": row.get("sector_quadrant"),
                "source": "cbe_alpha_engine",
            }
        )
        symbols.add(symbol)
        sectors.add(sector)
        if len(selected) >= max(1, int(limit)):
            break
    return selected


def evaluate_index_snapshot(symbol: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    analysis = dict(snapshot.get("analysis") or {})
    profile = dict(analysis.get("market_profile") or {})
    order_flow = dict(analysis.get("order_flow") or {})
    ntm = dict(analysis.get("ntm_volx") or {})
    metadata = dict((snapshot.get("request") or {}).get("metadata") or {})
    spot = _number((snapshot.get("request") or {}).get("session", {}).get("last_price"), _number(profile.get("close_price")))
    val = _number(profile.get("val"))
    ib_range = max(_number(profile.get("initial_balance_range")), 0.0)
    tolerance = max(spot * 0.0015, ib_range * 0.25)
    flow_source = str(metadata.get("order_flow_source") or "unavailable")
    structural = spot > 0 and val > 0 and spot <= val + tolerance
    options = str(ntm.get("directional_bias") or "FLAT").upper() == "LONG" or _number(ntm.get("net_pressure")) > 0.05
    flow = _number(order_flow.get("cumulative_delta")) > 0 and _number(order_flow.get("book_pressure")) > 0
    volume = _number(order_flow.get("volatility_burst")) >= 1.5
    real_book = flow_source == "tick_reconstruction_book" and bool(metadata.get("order_flow_book_active"))
    gates = {
        "structural_arrival": structural,
        "options_confluence": options,
        "positive_orderflow": flow,
        "volume_trigger": volume,
        "real_book_data": real_book,
    }
    passed = all(gates.values())
    return {
        "kind": "index",
        "symbol": symbol,
        "sector": "INDEX",
        "status": "actionable_shadow" if passed else "blocked",
        "action": "LONG" if passed else "FLAT",
        "score": round(sum(1 for value in gates.values() if value) / len(gates) * 100.0, 2),
        "spot": spot,
        "profile": {"val": val, "vah": profile.get("vah"), "poc": profile.get("poc"), "ibh": profile.get("initial_balance_high")},
        "order_flow": {
            "source": flow_source,
            "book_symbol": metadata.get("order_flow_book_symbol"),
            "cumulative_delta": order_flow.get("cumulative_delta"),
            "book_pressure": order_flow.get("book_pressure"),
            "volatility_burst": order_flow.get("volatility_burst"),
        },
        "options": {
            "expiry": ntm.get("expiry"),
            "bias": ntm.get("directional_bias"),
            "net_pressure": ntm.get("net_pressure"),
            "call_wall": ntm.get("call_wall_strike"),
            "put_wall": ntm.get("put_wall_strike"),
        },
        "gates": gates,
        "blocked_reasons": [key for key, value in gates.items() if not value],
        "snapshot_time": metadata.get("snapshot_time"),
    }


def evaluate_stock_context(row: dict[str, Any]) -> dict[str, Any]:
    gates = {
        "cbe_ranked_context": _number(row.get("alpha_score")) >= 50.0,
        "sector_diversified": True,
        "intraday_profile_ready": False,
        "real_book_data": False,
        "footprint_trigger": False,
    }
    return {
        "kind": "stock",
        **row,
        "status": "collecting_orderflow",
        "action": "FLAT",
        "score": round(sum(1 for value in gates.values() if value) / len(gates) * 100.0, 2),
        "gates": gates,
        "blocked_reasons": [key for key, value in gates.items() if not value],
    }


@dataclass
class InstitutionalConvergenceService:
    state_file: Path = STATE_FILE

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_state(self, payload: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        temp.replace(self.state_file)

    async def build_universe(self) -> dict[str, Any]:
        cbe = await load_latest_scan_payload(source="alpha_engine_v4_direction_aware")
        if cbe is None:
            cbe = await load_latest_scan_payload()
        stock_limit = int(getattr(settings, "INSTITUTIONAL_CONVERGENCE_STOCK_COUNT", 10))
        stocks = select_diversified_stocks(cbe, limit=stock_limit)
        return {
            "indices": configured_indices(),
            "stocks": stocks,
            "stock_count": len(stocks),
            "sector_count": len({row["sector"] for row in stocks}),
            "cbe_scan_date": (cbe or {}).get("signal_session_date") or (cbe or {}).get("scan_date"),
        }

    async def run_cycle(self) -> dict[str, Any]:
        now = _now_ist()
        has_session = trading_calendar.has_exchange_session("NSE", now.date())
        premarket = has_session and time(8, 45) <= now.time() < time(9, 15)
        market_open = trading_calendar.is_exchange_open("NSE", now)
        if not market_open and not premarket:
            return {
                "status": "market_closed",
                "next_run_at": trading_calendar.next_exchange_open("NSE", now).isoformat(),
                "latest": self._load_state(),
            }
        universe = await self.build_universe()
        from core.config import auction_front_month_book_symbols
        from data.index_futures_backfill import fyers_front_month_symbol, month_code_for_front_contract

        book_map = auction_front_month_book_symbols(now.date())
        symbols = [*universe["indices"], *[row["symbol"] for row in universe["stocks"]]]
        futures_map = {}
        for symbol in symbols:
            if symbol in INDEX_ROOTS:
                futures_map[symbol] = book_map.get(_index_app_symbol(symbol)) or fyers_front_month_symbol(symbol, now.date())
            else:
                futures_map[symbol] = f"NSE:{symbol}{month_code_for_front_contract(now.date(), symbol)}FUT"
        spot_symbols = [
            _index_app_symbol(symbol) if symbol in INDEX_ROOTS else f"NSE:{symbol}-EQ"
            for symbol in symbols
        ]
        await market_data_router.add_subscriptions([*futures_map.values(), *spot_symbols])

        vix = await _load_india_vix()
        if premarket:
            prepared = []
            for symbol in symbols:
                inputs = await _load_rule_inputs(symbol, futures_map[symbol], now)
                prepared.append({"symbol": symbol, "profile": inputs.get("prior_profile"), "options": inputs["options"], "futures_contract": futures_map[symbol], "data_ready": bool(inputs["prior_bars"])})
            payload = {"status": "prepared", "mode": "paper", "paper_execution_enabled": True, "generated_at": datetime.now(timezone.utc).isoformat(), "session_date": now.date().isoformat(), "universe": universe, "pre_market": {"window": "08:45-09:15", "india_vix": vix, "instruments": prepared}, "results": [], "result_count": 0, "actionable_count": 0, "failure_count": 0, "gate_breakdown": {}}
            await asyncio.to_thread(self._save_state, payload)
            return payload

        results: list[dict[str, Any]] = []
        failures: dict[str, str] = {}
        stock_meta = {row["symbol"]: row for row in universe["stocks"]}
        for symbol in symbols:
            try:
                inputs = await _load_rule_inputs(symbol, futures_map[symbol], now)
                result = await asyncio.to_thread(
                    evaluate_rules,
                    symbol=symbol,
                    current_bars=inputs["current_bars"],
                    prior_bars=inputs["prior_bars"],
                    history_bars=inputs["history_bars"],
                    ticks=inputs["ticks"],
                    options=inputs["options"],
                    vix=vix,
                    lot_size=inputs["lot_size"],
                    tick_size=inputs["tick_size"],
                    clock_drift_ms=inputs["clock_drift_ms"],
                    now=now,
                    directional_bias=(stock_meta.get(symbol) or {}).get("directional_bias"),
                    setup_window_bars=int(getattr(settings, "INSTITUTIONAL_CONVERGENCE_SETUP_WINDOW_BARS", 5)),
                    min_confirmations=int(getattr(settings, "INSTITUTIONAL_CONVERGENCE_MIN_CONFIRMATIONS", 2)),
                    max_chase_atr=float(getattr(settings, "INSTITUTIONAL_CONVERGENCE_MAX_CHASE_ATR", 0.5)),
                    min_reward_risk=float(getattr(settings, "INSTITUTIONAL_CONVERGENCE_MIN_REWARD_RISK", 1.5)),
                )
                result.update({"sector": (stock_meta.get(symbol) or {}).get("sector", "INDEX"), "futures_contract": futures_map[symbol], "alpha_context": stock_meta.get(symbol)})
                results.append(result)
            except Exception as exc:
                failures[symbol] = str(exc)
                results.append({"kind": "index", "symbol": symbol, "status": "error", "action": "FLAT", "blocked_reasons": ["analysis_failed"], "detail": str(exc)})
        paper = convergence_paper_book.sync(results, now)
        payload = {
            "status": "ok" if not failures else "degraded",
            "mode": "paper",
            "paper_execution_enabled": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "session_date": now.date().isoformat(),
            "universe": universe,
            "results": results,
            "result_count": len(results),
            "actionable_count": sum(1 for row in results if row.get("status") == "actionable_paper"),
            "failure_count": len(failures),
            "failures": failures,
            "gate_breakdown": _gate_breakdown(results),
            "paper": paper,
            "india_vix": vix,
        }
        await asyncio.to_thread(self._save_state, payload)
        return payload

    async def status(self) -> dict[str, Any]:
        state = self._load_state()
        universe = await self.build_universe()
        return {
            "key": "institutional_convergence",
            "enabled": bool(getattr(settings, "INSTITUTIONAL_CONVERGENCE_AUTO_ENABLED", True)),
            "mode": "paper",
            "paper_execution_enabled": True,
            "market_open": trading_calendar.is_exchange_open("NSE", _now_ist()),
            "universe": universe,
            "latest": state,
            "paper": convergence_paper_book.summary(),
            "paper_statistics": convergence_paper_book.statistics(),
        }


def _index_app_symbol(symbol: str) -> str:
    return {"NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:BANKNIFTY-INDEX", "SENSEX": "BSE:SENSEX-INDEX"}.get(symbol, f"NSE:{symbol}-INDEX")


async def _load_india_vix() -> float | None:
    global _VIX_CACHE
    cache_now = datetime.now(timezone.utc)
    if _VIX_CACHE and cache_now - _VIX_CACHE[0] < timedelta(minutes=5):
        return _VIX_CACHE[1]
    try:
        from analytics.sector import SectorRotationTracker
        payload = await SectorRotationTracker()._get_india_vix()
        value = _number(payload.get("price"))
        result = value if value > 0 else None
    except Exception:
        result = None
    _VIX_CACHE = (cache_now, result)
    return result


def _select_rule_sessions(
    bars: list[dict[str, Any]], now: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return today's live bars, the prior complete session, and ten-session history."""
    sessions: dict[Any, list[dict[str, Any]]] = {}
    for row in bars:
        timestamp = row["time"]
        session_day = timestamp.date()
        if (
            not trading_calendar.has_exchange_session("NSE", session_day)
            or not RTH_START <= timestamp.time().replace(tzinfo=None) <= RTH_END
        ):
            continue
        sessions.setdefault(session_day, []).append(row)

    complete_dates = sorted(
        day
        for day, rows in sessions.items()
        if day < now.date() and len(rows) >= MIN_COMPLETE_SESSION_BARS
    )
    current_bars = sessions.get(now.date(), [])
    if len(current_bars) < 4:
        current_bars = []
    prior_bars = sessions[complete_dates[-1]] if complete_dates else []
    history = [row for day in complete_dates[-10:] for row in sessions[day]]
    return current_bars, prior_bars, history


async def _load_rule_inputs(symbol: str, futures_symbol: str, now: datetime) -> dict[str, Any]:
    spot_tick_symbol = _index_app_symbol(symbol) if symbol in INDEX_ROOTS else f"NSE:{symbol}-EQ"
    async with AsyncSessionLocal() as session:
        bars_result = await session.execute(text("""
            SELECT time, open, high, low, close, volume
            FROM underlying_spot_candles
            WHERE underlying=:symbol AND interval='3minute' AND time >= :since
            ORDER BY time
        """), {"symbol": symbol, "since": now - timedelta(days=18)})
        bars = [{**dict(row), "time": row["time"].astimezone(now.tzinfo)} for row in bars_result.mappings().all()]
        bars = _filter_session_ohlc(
            bars,
            relative_tolerance=0.08 if symbol in INDEX_ROOTS else 0.20,
        )
        # DESC + reverse: the cap must keep the LATEST ticks. Ascending LIMIT
        # kept the oldest 5000 of the 2h window, so on any busy session the
        # footprint/CVD trigger was computed on stale early-window ticks.
        tick_result = await session.execute(text("""
            WITH recent AS (
              SELECT time, ltp, bid, ask, bid_qty, ask_qty, volume
              FROM market_ticks
              WHERE symbol=:symbol AND time >= :since AND ltp > 0
            ), reference AS (
              SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY ltp) AS median_ltp
              FROM recent
            )
            SELECT r.time, r.ltp, r.bid, r.ask, r.bid_qty, r.ask_qty, r.volume
            FROM recent r CROSS JOIN reference p
            WHERE p.median_ltp IS NOT NULL
              AND r.ltp BETWEEN p.median_ltp * 0.97 AND p.median_ltp * 1.03
            ORDER BY r.time DESC
            LIMIT 5000
        """), {"symbol": futures_symbol, "since": now - timedelta(minutes=120)})
        ticks = [dict(row) for row in reversed(tick_result.mappings().all())]
        # Pipeline reference clock for the tick_fresh drift (single index
        # probe); see engine.tick_clock_drift_ms for why wall-clock is wrong.
        pipeline_result = await session.execute(text("""
            SELECT time FROM market_ticks WHERE time >= :since ORDER BY time DESC LIMIT 1
        """), {"since": now - timedelta(minutes=120)})
        pipeline_last = pipeline_result.scalar()
        current_result = await session.execute(text("""
            WITH recent AS (
              SELECT time, ltp, volume
              FROM market_ticks
              WHERE symbol=:symbol AND time >= :session_start AND time <= :now
                AND ltp > 0
            ), reference AS (
              SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY ltp) AS median_ltp
              FROM recent
            ), clean AS (
              SELECT r.*
              FROM recent r CROSS JOIN reference p
              WHERE p.median_ltp IS NOT NULL
                AND r.ltp BETWEEN p.median_ltp * 0.97 AND p.median_ltp * 1.03
            )
            SELECT time_bucket(INTERVAL '3 minutes', time) AS time,
                   first(ltp, time) AS open, max(ltp) AS high, min(ltp) AS low,
                   last(ltp, time) AS close,
                   GREATEST(max(volume) - min(volume), 0) AS volume
            FROM clean
            GROUP BY 1 ORDER BY 1
        """), {
            "symbol": spot_tick_symbol,
            "session_start": now.replace(hour=9, minute=15, second=0, microsecond=0),
            "now": now,
        })
        current_tick_bars = [
            {**dict(row), "time": row["time"].astimezone(now.tzinfo)}
            for row in current_result.mappings().all()
        ]
        option_result = await session.execute(text("""
            WITH latest AS (
              SELECT DISTINCT ON (expiry,strike,option_type) expiry,strike,option_type,oi
              FROM option_premium_candles
              WHERE underlying=:symbol AND expiry >= CURRENT_DATE AND time >= NOW()-INTERVAL '5 days'
              ORDER BY expiry,strike,option_type,time DESC
            ), nearest AS (SELECT min(expiry) expiry FROM latest)
            SELECT l.expiry,l.strike,l.option_type,l.oi FROM latest l JOIN nearest n ON n.expiry=l.expiry
        """), {"symbol": symbol})
        option_rows = [dict(row) for row in option_result.mappings().all()]
        contract_result = await session.execute(text("""
            SELECT COALESCE(max(lot_size),1) lot_size, COALESCE(max(tick_size),0.05) tick_size
            FROM fo_contract_catalog WHERE underlying=:symbol AND expiry >= CURRENT_DATE
        """), {"symbol": symbol})
        contract = dict(contract_result.mappings().first() or {})

    current_bars, prior_bars, history = _select_rule_sessions(bars, now)
    if trading_calendar.has_exchange_session("NSE", now.date()) and len(current_tick_bars) >= 4:
        current_bars = current_tick_bars
    calls = sorted((row for row in option_rows if row["option_type"] == "CE"), key=lambda row: _number(row.get("oi")), reverse=True)
    puts = sorted((row for row in option_rows if row["option_type"] == "PE"), key=lambda row: _number(row.get("oi")), reverse=True)
    options = {"expiry": str(option_rows[0]["expiry"]) if option_rows else None, "call_wall": _number(calls[0]["strike"]) if calls else None, "put_wall": _number(puts[0]["strike"]) if puts else None, "top_call_walls": [{"strike": _number(row["strike"]), "oi": _number(row["oi"])} for row in calls[:2]], "top_put_walls": [{"strike": _number(row["strike"]), "oi": _number(row["oi"])} for row in puts[:2]]}
    last_tick = ticks[-1]["time"] if ticks else None
    drift = tick_clock_drift_ms(last_tick, pipeline_last, datetime.now(timezone.utc))
    prior_profile = None
    if prior_bars:
        from auction_intelligence.market_profile import MarketProfileEngine
        from auction_intelligence.schemas import MarketBar
        engine = MarketProfileEngine({"period_minutes": 30, "tick_size": _number(contract.get("tick_size"), .05), "initial_balance_periods": 2})
        profile = engine.build_profile(symbol, [MarketBar(timestamp=row["time"], open=_number(row["open"]), high=_number(row["high"]), low=_number(row["low"]), close=_number(row["close"]), volume=_number(row["volume"])) for row in prior_bars])
        prior_profile = {"vah": profile.vah, "val": profile.val, "poc": profile.poc}
    return {"current_bars": current_bars, "prior_bars": prior_bars, "history_bars": history, "ticks": ticks, "options": options, "lot_size": int(contract.get("lot_size") or 1), "tick_size": _number(contract.get("tick_size"), .05), "clock_drift_ms": drift, "prior_profile": prior_profile}


def _gate_breakdown(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in results:
        for reason in row.get("blocked_reasons") or []:
            counts[str(reason)] = counts.get(str(reason), 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


institutional_convergence_service = InstitutionalConvergenceService()
