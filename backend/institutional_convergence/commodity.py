"""Institutional-convergence lane for the MCX commodity universe.

Same rules engine and paper mechanics as the NSE lane (market-profile
structural gates + real-tick CVD divergence + 3:1 footprint trigger + ATR
stops with 50% booking at 1R and break-even runner), adapted to the MCX
evening session:

  * Universe = configured commodity roots, resolved to the ACTIVE front-month
    futures contract via the Upstox instrument master (same resolver as the
    commodity strategy agent, so both lanes always trade the same contract).
  * Bars = the unified 1-minute commodity store (the commodity agent pushes
    per-root 1-minute bars into underlying_spot_candles every scan),
    aggregated to 3-minute here.
  * Ticks = market_ticks for the active futures symbol; this service
    subscribes the contracts on the shared data router so the tape fills.
  * No India-VIX gate and no NSE noon quarantine (session runs 09:00-23:30);
    intraday square-off is 23:15 exchange-local.
  * MCX option-OI walls are read where present (market='MCX'); most roots
    have no options data, so target2 simply stays unset and exits rely on
    stop / 1R / CVD-reversal / square-off.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from core.config import settings
from core.trading_calendar import trading_calendar
from db.database import AsyncSessionLocal
from market_data import data_router as market_data_router
from market_data.commodity_contract_specs import (
    canonicalize_commodity_root,
    extract_commodity_root,
    get_commodity_contract_spec,
)
from market_data.upstox_commodity import resolve_active_upstox_mcx_future
from paper_engine.base_strategy_agent import _now_ist
from sqlalchemy import text

from .engine import evaluate_rules, tick_clock_drift_ms
from .paper import ConvergencePaperBook


DEFAULT_COMMODITY_ROOTS = (
    "GOLD", "SILVERM", "CRUDEOIL", "NATURALGAS",
    "COPPER", "ALUMINI", "ZINCMINI", "NICKEL",
)
STATE_FILE = Path(__file__).resolve().parents[1] / "runtime" / "institutional_convergence" / "commodity_state.json"
PAPER_FILE = Path(__file__).resolve().parents[1] / "runtime" / "institutional_convergence" / "commodity_paper.json"
# MCX regular session. The winter 23:55 close is deliberately covered by the
# wider bound so late bars are never filtered out of profiles.
RTH_START = time(9, 0)
RTH_END = time(23, 55)
SQUAREOFF = time(23, 15)
# A complete 3-minute MCX session is ~290 bars; 200 tolerates thin stretches
# while still rejecting half-captured sessions from profile construction.
MIN_COMPLETE_SESSION_BARS = 200


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def configured_roots() -> list[str]:
    raw = str(
        getattr(
            settings,
            "INSTITUTIONAL_CONVERGENCE_COMMODITY_SYMBOLS",
            ",".join(DEFAULT_COMMODITY_ROOTS),
        )
    )
    # extract_commodity_root tolerates full contract symbols (MCX:GOLD26AUGFUT)
    # as well as bare roots; canonicalize maps aliases (GOLDM -> GOLD class).
    values = [
        canonicalize_commodity_root(extract_commodity_root(item) or item)
        for item in raw.split(",")
        if item.strip()
    ]
    return list(dict.fromkeys(value for value in values if value))


def aggregate_bars(rows: list[dict[str, Any]], minutes: int = 3) -> list[dict[str, Any]]:
    """Aggregate 1-minute rows (dicts with tz-aware `time`) to N-minute bars."""
    buckets: dict[datetime, dict[str, Any]] = {}
    for row in rows:
        timestamp = row["time"]
        bucket_time = timestamp.replace(
            minute=(timestamp.minute // minutes) * minutes, second=0, microsecond=0
        )
        bucket = buckets.get(bucket_time)
        if bucket is None:
            buckets[bucket_time] = {
                "time": bucket_time,
                "open": _number(row.get("open")),
                "high": _number(row.get("high")),
                "low": _number(row.get("low")),
                "close": _number(row.get("close")),
                "volume": _number(row.get("volume")),
            }
            continue
        bucket["high"] = max(bucket["high"], _number(row.get("high")))
        bucket["low"] = min(bucket["low"], _number(row.get("low")))
        bucket["close"] = _number(row.get("close"))
        bucket["volume"] += _number(row.get("volume"))
    return [buckets[key] for key in sorted(buckets)]


def _select_rule_sessions(
    bars: list[dict[str, Any]], now: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Today's live bars, the prior complete MCX session, ten-session history."""
    sessions: dict[Any, list[dict[str, Any]]] = {}
    for row in bars:
        timestamp = row["time"]
        session_day = timestamp.date()
        if (
            not trading_calendar.has_exchange_session("MCX", session_day)
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


async def _load_rule_inputs(root: str, futures_symbol: str, now: datetime) -> dict[str, Any]:
    spec = get_commodity_contract_spec(root)
    async with AsyncSessionLocal() as session:
        bars_result = await session.execute(
            text(
                """
                SELECT time, open, high, low, close, volume
                FROM underlying_spot_candles
                WHERE underlying = :symbol AND interval = '1minute' AND time >= :since
                ORDER BY time
                """
            ),
            {"symbol": root, "since": now - timedelta(days=18)},
        )
        minute_bars = [
            {**dict(row), "time": row["time"].astimezone(now.tzinfo)}
            for row in bars_result.mappings().all()
        ]
        # DESC + reverse: keep the LATEST ticks when the window is busy.
        tick_result = await session.execute(
            text(
                """
                SELECT time, ltp, bid, ask, bid_qty, ask_qty, volume
                FROM market_ticks WHERE symbol = :symbol AND time >= :since
                ORDER BY time DESC LIMIT 5000
                """
            ),
            {"symbol": futures_symbol, "since": now - timedelta(minutes=120)},
        )
        ticks = [dict(row) for row in reversed(tick_result.mappings().all())]
        # Pipeline reference clock for the tick_fresh drift: the newest tick
        # ANY symbol got through the shared batched flush (single index probe).
        # See engine.tick_clock_drift_ms for why wall-clock drift is wrong here.
        pipeline_result = await session.execute(
            text(
                """
                SELECT time FROM market_ticks WHERE time >= :since
                ORDER BY time DESC LIMIT 1
                """
            ),
            {"since": now - timedelta(minutes=120)},
        )
        pipeline_last = pipeline_result.scalar()
        option_result = await session.execute(
            text(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (expiry,strike,option_type) expiry,strike,option_type,oi
                  FROM option_premium_candles
                  WHERE underlying=:symbol AND market='MCX'
                    AND expiry >= CURRENT_DATE AND time >= NOW()-INTERVAL '5 days'
                  ORDER BY expiry,strike,option_type,time DESC
                ), nearest AS (SELECT min(expiry) expiry FROM latest)
                SELECT l.expiry,l.strike,l.option_type,l.oi FROM latest l JOIN nearest n ON n.expiry=l.expiry
                """
            ),
            {"symbol": root},
        )
        option_rows = [dict(row) for row in option_result.mappings().all()]

    bars = aggregate_bars(minute_bars, minutes=3)
    current_bars, prior_bars, history = _select_rule_sessions(bars, now)
    calls = sorted((row for row in option_rows if row["option_type"] == "CE"), key=lambda row: _number(row.get("oi")), reverse=True)
    puts = sorted((row for row in option_rows if row["option_type"] == "PE"), key=lambda row: _number(row.get("oi")), reverse=True)
    options = {
        "expiry": str(option_rows[0]["expiry"]) if option_rows else None,
        "call_wall": _number(calls[0]["strike"]) if calls else None,
        "put_wall": _number(puts[0]["strike"]) if puts else None,
        "top_call_walls": [{"strike": _number(row["strike"]), "oi": _number(row["oi"])} for row in calls[:2]],
        "top_put_walls": [{"strike": _number(row["strike"]), "oi": _number(row["oi"])} for row in puts[:2]],
    }
    last_tick = ticks[-1]["time"] if ticks else None
    drift = tick_clock_drift_ms(last_tick, pipeline_last, datetime.now(timezone.utc))
    return {
        "current_bars": current_bars,
        "prior_bars": prior_bars,
        "history_bars": history,
        "ticks": ticks,
        "options": options,
        "lot_size": int(getattr(spec, "futures_lot_size", 1) or 1),
        "tick_size": _number(getattr(spec, "mp_tick_size", 0.05), 0.05),
        "clock_drift_ms": drift,
    }


commodity_convergence_paper_book = ConvergencePaperBook(
    PAPER_FILE, squareoff=SQUAREOFF, entry_quarantine=None
)


@dataclass
class CommodityConvergenceService:
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
        now = _now_ist()
        session_date = trading_calendar.next_exchange_open("MCX", now).date()
        roots = configured_roots()
        contracts: dict[str, dict[str, Any]] = {}
        for root in roots:
            resolved = await resolve_active_upstox_mcx_future(root, session_date=session_date)
            if resolved and resolved.get("symbol"):
                contracts[root] = resolved
        return {
            "roots": roots,
            "contracts": contracts,
            "resolved_count": len(contracts),
            "unresolved": [root for root in roots if root not in contracts],
            "session_date": session_date.isoformat(),
        }

    async def run_cycle(self) -> dict[str, Any]:
        now = _now_ist()
        has_session = trading_calendar.has_exchange_session("MCX", now.date())
        premarket = has_session and time(8, 45) <= now.time() < RTH_START
        market_open = trading_calendar.is_exchange_open("MCX", now)
        if not market_open and not premarket:
            return {
                "status": "market_closed",
                "next_run_at": trading_calendar.next_exchange_open("MCX", now).isoformat(),
                "latest": self._load_state(),
            }

        universe = await self.build_universe()
        futures_map = {
            root: str(contract.get("symbol"))
            for root, contract in universe["contracts"].items()
        }
        if futures_map:
            # Same shared WS router as the NSE lane — this is what fills
            # market_ticks for the footprint/CVD gates.
            await market_data_router.add_subscriptions(list(futures_map.values()))

        if premarket:
            prepared = []
            for root, symbol in futures_map.items():
                inputs = await _load_rule_inputs(root, symbol, now)
                prepared.append(
                    {
                        "symbol": root,
                        "futures_contract": symbol,
                        "options": inputs["options"],
                        "data_ready": bool(inputs["prior_bars"]),
                    }
                )
            payload = {
                "status": "prepared",
                "mode": "paper",
                "market": "MCX",
                "paper_execution_enabled": True,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "session_date": now.date().isoformat(),
                "universe": universe,
                "pre_market": {"window": "08:45-09:00", "instruments": prepared},
                "results": [],
                "result_count": 0,
                "actionable_count": 0,
                "failure_count": 0,
                "gate_breakdown": {},
            }
            await asyncio.to_thread(self._save_state, payload)
            return payload

        results: list[dict[str, Any]] = []
        failures: dict[str, str] = {}
        for root, symbol in futures_map.items():
            try:
                inputs = await _load_rule_inputs(root, symbol, now)
                # to_thread — evaluate_rules is pure CPU (profiles, footprint,
                # HVN density). Run synchronously it seized the event loop for
                # the whole universe every cycle (2026-07-13: health checks
                # >5s, audit writes >3s, every runner blowing its watchdog).
                result = await asyncio.to_thread(
                    evaluate_rules,
                    symbol=root,
                    current_bars=inputs["current_bars"],
                    prior_bars=inputs["prior_bars"],
                    history_bars=inputs["history_bars"],
                    ticks=inputs["ticks"],
                    options=inputs["options"],
                    vix=None,
                    lot_size=inputs["lot_size"],
                    tick_size=inputs["tick_size"],
                    clock_drift_ms=inputs["clock_drift_ms"],
                    now=now,
                    noon_quarantine=False,
                    require_vix=False,
                    kind="commodity",
                    setup_window_bars=int(getattr(settings, "INSTITUTIONAL_CONVERGENCE_SETUP_WINDOW_BARS", 5)),
                    min_confirmations=int(getattr(settings, "INSTITUTIONAL_CONVERGENCE_MIN_CONFIRMATIONS", 2)),
                    max_chase_atr=float(getattr(settings, "INSTITUTIONAL_CONVERGENCE_MAX_CHASE_ATR", 0.5)),
                    min_reward_risk=float(getattr(settings, "INSTITUTIONAL_CONVERGENCE_MIN_REWARD_RISK", 1.5)),
                )
                result.update({"sector": "COMMODITY", "futures_contract": symbol})
                results.append(result)
            except Exception as exc:  # noqa: BLE001 — one root must not kill the cycle
                failures[root] = str(exc)
                results.append(
                    {
                        "kind": "commodity",
                        "symbol": root,
                        "status": "error",
                        "action": "FLAT",
                        "blocked_reasons": ["analysis_failed"],
                        "detail": str(exc),
                    }
                )
        for root in universe["unresolved"]:
            results.append(
                {
                    "kind": "commodity",
                    "symbol": root,
                    "status": "blocked",
                    "action": "FLAT",
                    "blocked_reasons": ["active_contract_unresolved"],
                }
            )

        paper = commodity_convergence_paper_book.sync(results, now)
        payload = {
            "status": "ok" if not failures else "degraded",
            "mode": "paper",
            "market": "MCX",
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
        }
        await asyncio.to_thread(self._save_state, payload)
        return payload

    async def status(self) -> dict[str, Any]:
        state = self._load_state()
        universe = await self.build_universe()
        return {
            "key": "institutional_convergence_commodity",
            "enabled": bool(getattr(settings, "INSTITUTIONAL_CONVERGENCE_COMMODITY_ENABLED", True)),
            "mode": "paper",
            "market": "MCX",
            "paper_execution_enabled": True,
            "market_open": trading_calendar.is_exchange_open("MCX", _now_ist()),
            "universe": universe,
            "latest": state,
            "paper": commodity_convergence_paper_book.summary(),
            "paper_statistics": commodity_convergence_paper_book.statistics(),
        }


def _gate_breakdown(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in results:
        for reason in row.get("blocked_reasons") or []:
            counts[str(reason)] = counts.get(str(reason), 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


commodity_convergence_service = CommodityConvergenceService()
