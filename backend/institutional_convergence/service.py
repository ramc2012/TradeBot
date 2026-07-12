from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auction_intelligence.live import build_live_analysis
from cbe_scanner.repository import load_latest_scan_payload
from core.config import settings
from core.trading_calendar import trading_calendar
from market_data import data_router as market_data_router
from paper_engine.base_strategy_agent import _now_ist


DEFAULT_INDICES = ("NIFTY", "BANKNIFTY")
INDEX_ROOTS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX"}
STATE_FILE = Path(__file__).resolve().parents[1] / "runtime" / "institutional_convergence" / "state.json"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
        if not trading_calendar.is_exchange_open("NSE", now):
            return {
                "status": "market_closed",
                "next_run_at": trading_calendar.next_exchange_open("NSE", now).isoformat(),
                "latest": self._load_state(),
            }
        universe = await self.build_universe()
        stock_symbols = [f"NSE:{row['symbol']}-EQ" for row in universe["stocks"]]
        if stock_symbols:
            await market_data_router.add_subscriptions(stock_symbols)

        results: list[dict[str, Any]] = []
        failures: dict[str, str] = {}
        for symbol in universe["indices"]:
            try:
                snapshot = await asyncio.wait_for(build_live_analysis(symbol_code=symbol), timeout=240.0)
                results.append(evaluate_index_snapshot(symbol, snapshot))
            except Exception as exc:
                failures[symbol] = str(exc)
                results.append({"kind": "index", "symbol": symbol, "status": "error", "action": "FLAT", "blocked_reasons": ["analysis_failed"], "detail": str(exc)})
        results.extend(evaluate_stock_context(row) for row in universe["stocks"])
        payload = {
            "status": "ok" if not failures else "degraded",
            "mode": "shadow",
            "paper_execution_enabled": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "session_date": now.date().isoformat(),
            "universe": universe,
            "results": results,
            "result_count": len(results),
            "actionable_count": sum(1 for row in results if row.get("status") == "actionable_shadow"),
            "failure_count": len(failures),
            "failures": failures,
            "gate_breakdown": _gate_breakdown(results),
        }
        await asyncio.to_thread(self._save_state, payload)
        return payload

    async def status(self) -> dict[str, Any]:
        state = self._load_state()
        universe = await self.build_universe()
        return {
            "key": "institutional_convergence",
            "enabled": bool(getattr(settings, "INSTITUTIONAL_CONVERGENCE_AUTO_ENABLED", True)),
            "mode": "shadow",
            "paper_execution_enabled": False,
            "market_open": trading_calendar.is_exchange_open("NSE", _now_ist()),
            "universe": universe,
            "latest": state,
        }


def _gate_breakdown(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in results:
        for reason in row.get("blocked_reasons") or []:
            counts[str(reason)] = counts.get(str(reason), 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


institutional_convergence_service = InstitutionalConvergenceService()
