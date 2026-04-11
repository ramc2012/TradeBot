from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from fastapi.encoders import jsonable_encoder

from auction_intelligence.service import AuctionIntelligenceService
from auction_intelligence.schemas import (
    DepthLevel,
    DepthSnapshot,
    MarketBar,
    PortfolioSnapshot,
    QuoteSnapshot,
    SessionContext,
    TradePrint,
)


_SCENARIO_LABELS = {
    "acceptance_up": "Breakout acceptance above prior value",
    "failed_auction": "Failed downside auction with re-entry",
    "balance": "Rotational balance session",
}

_SYMBOL_BASES = {
    "NIFTY": 22480.0,
    "BANKNIFTY": 48650.0,
}


def available_symbols() -> list[str]:
    return list(_SYMBOL_BASES.keys())


def available_scenarios() -> list[dict[str, str]]:
    return [{"id": key, "label": value} for key, value in _SCENARIO_LABELS.items()]


def build_demo_validation_series(
    symbol_code: str = "BANKNIFTY",
    scenario: str = "acceptance_up",
    session_count: int = 6,
) -> dict[str, Any]:
    normalized_symbol = symbol_code.upper()
    if normalized_symbol not in _SYMBOL_BASES:
        raise ValueError(f"Unsupported demo symbol: {symbol_code}")
    if scenario not in _SCENARIO_LABELS:
        raise ValueError(f"Unsupported demo scenario: {scenario}")

    scenario_cycle = [scenario, "balance", "failed_auction", "acceptance_up", "balance", scenario]
    start_date = date(2026, 3, 24)
    sessions: list[dict[str, Any]] = []
    for index in range(max(session_count, len(scenario_cycle))):
        scenario_id = scenario_cycle[index % len(scenario_cycle)]
        payload = build_demo_payload(normalized_symbol, scenario_id)
        session_date = start_date + timedelta(days=index)
        shifted_bars: list[dict[str, Any]] = []
        for bar in payload["bars"]:
            timestamp = datetime.combine(session_date, datetime.fromisoformat(bar["timestamp"]).time())
            shifted_bars.append({**bar, "timestamp": timestamp.isoformat()})
        sessions.append(
            {
                "session_date": session_date.isoformat(),
                "scenario": scenario_id,
                "bars": shifted_bars,
            }
        )

    return {
        "symbol_code": normalized_symbol,
        "source": "demo_series",
        "sessions": sessions,
    }


def build_demo_payload(symbol_code: str = "NIFTY", scenario: str = "acceptance_up") -> dict[str, Any]:
    normalized_symbol = symbol_code.upper()
    if normalized_symbol not in _SYMBOL_BASES:
        raise ValueError(f"Unsupported demo symbol: {symbol_code}")
    if scenario not in _SCENARIO_LABELS:
        raise ValueError(f"Unsupported demo scenario: {scenario}")

    base = _SYMBOL_BASES[normalized_symbol]
    symbol = f"{normalized_symbol} FUT"
    start = datetime(2026, 4, 1, 9, 15)
    lot_size = 25 if normalized_symbol == "NIFTY" else 15

    if scenario == "acceptance_up":
        prior_rows = [
            (base - 75, base - 20, base - 110, base - 40),
            (base - 40, base + 5, base - 55, base - 10),
            (base - 10, base + 20, base - 18, base + 12),
            (base + 12, base + 35, base + 5, base + 18),
            (base + 18, base + 22, base - 8, base + 6),
            (base + 6, base + 10, base - 18, base - 4),
        ]
        current_rows = [
            (base + 30, base + 55, base + 20, base + 48),
            (base + 48, base + 86, base + 42, base + 78),
            (base + 78, base + 118, base + 75, base + 112),
            (base + 112, base + 155, base + 108, base + 148),
            (base + 148, base + 196, base + 142, base + 190),
            (base + 190, base + 228, base + 182, base + 220),
        ]
        trade_rows = [
            (base + 216, 42, "buy"),
            (base + 220, 55, "buy"),
            (base + 224, 38, "buy"),
            (base + 218, 14, "sell"),
            (base + 226, 30, "buy"),
        ]
        quote = {
            "bid": base + 224,
            "ask": base + 226,
            "bid_size": 580,
            "ask_size": 310,
        }
        depth = {
            "bids": [(base + 224, 580), (base + 223, 440), (base + 222, 360)],
            "asks": [(base + 226, 310), (base + 227, 260), (base + 228, 230)],
        }
    elif scenario == "failed_auction":
        prior_rows = [
            (base - 40, base + 8, base - 72, base - 18),
            (base - 18, base + 12, base - 32, base + 4),
            (base + 4, base + 28, base - 6, base + 16),
            (base + 16, base + 24, base - 4, base + 8),
            (base + 8, base + 18, base - 10, base),
            (base, base + 6, base - 14, base - 6),
        ]
        current_rows = [
            (base - 12, base - 4, base - 48, base - 38),
            (base - 38, base - 22, base - 92, base - 84),
            (base - 84, base - 28, base - 122, base - 36),
            (base - 36, base + 6, base - 44, base - 8),
            (base - 8, base + 14, base - 12, base + 6),
            (base + 6, base + 18, base - 2, base + 10),
        ]
        trade_rows = [
            (base + 2, 18, "sell"),
            (base - 6, 22, "sell"),
            (base + 4, 40, "buy"),
            (base + 8, 52, "buy"),
            (base + 12, 34, "buy"),
        ]
        quote = {
            "bid": base + 8,
            "ask": base + 10,
            "bid_size": 640,
            "ask_size": 270,
        }
        depth = {
            "bids": [(base + 8, 640), (base + 7, 520), (base + 6, 400)],
            "asks": [(base + 10, 270), (base + 11, 220), (base + 12, 210)],
        }
    else:
        prior_rows = [
            (base - 28, base + 8, base - 46, base - 10),
            (base - 10, base + 18, base - 22, base + 6),
            (base + 6, base + 22, base - 4, base + 12),
            (base + 12, base + 20, base - 6, base + 2),
            (base + 2, base + 14, base - 8, base + 4),
            (base + 4, base + 10, base - 12, base - 2),
        ]
        current_rows = [
            (base - 2, base + 10, base - 18, base + 2),
            (base + 2, base + 14, base - 8, base + 6),
            (base + 6, base + 18, base - 6, base + 4),
            (base + 4, base + 16, base - 10, base + 8),
            (base + 8, base + 20, base - 4, base + 10),
            (base + 10, base + 18, base - 2, base + 6),
        ]
        trade_rows = [
            (base + 6, 16, "buy"),
            (base + 5, 20, "sell"),
            (base + 7, 18, "buy"),
            (base + 6, 18, "sell"),
            (base + 8, 12, "buy"),
        ]
        quote = {
            "bid": base + 6,
            "ask": base + 8,
            "bid_size": 360,
            "ask_size": 355,
        }
        depth = {
            "bids": [(base + 6, 360), (base + 5, 330), (base + 4, 310)],
            "asks": [(base + 8, 355), (base + 9, 340), (base + 10, 315)],
        }

    prior_bars = _make_bars(start - timedelta(days=1), prior_rows)
    bars = _make_bars(start, current_rows)
    trades = _make_trades(start + timedelta(hours=3), trade_rows)

    request = {
        "session": {
            "symbol": symbol,
            "session_date": date(2026, 4, 1).isoformat(),
            "last_price": round(bars[-1]["close"], 2),
            "stale_data_seconds": 0.0,
            "minutes_to_close": 110,
            "broker_connected": True,
        },
        "portfolio": {
            "net_liquidation": 2_400_000.0,
            "daily_realized_pnl": 0.0,
            "open_positions": 0,
            "symbol_exposure": {symbol: 0.0},
            "agent_drawdowns": {"positional": 0.01, "swing": 0.0, "scalp": 0.0},
            "correlated_exposure": 0.1,
        },
        "quote": {
            "timestamp": (start + timedelta(hours=3, minutes=5)).isoformat(),
            **quote,
        },
        "depth": {
            "timestamp": (start + timedelta(hours=3, minutes=5)).isoformat(),
            "bids": [{"price": price, "quantity": qty} for price, qty in depth["bids"]],
            "asks": [{"price": price, "quantity": qty} for price, qty in depth["asks"]],
        },
        "bars": bars,
        "prior_bars": prior_bars,
        "trades": trades,
        "metadata": {
            "symbol_code": normalized_symbol,
            "scenario": scenario,
            "scenario_label": _SCENARIO_LABELS[scenario],
            "lot_size": lot_size,
        },
    }
    return request


def build_demo_analysis(symbol_code: str = "NIFTY", scenario: str = "acceptance_up") -> dict[str, Any]:
    payload = build_demo_payload(symbol_code, scenario)
    service = AuctionIntelligenceService()
    bundle = service.analyze(
        session=SessionContext(**payload["session"]),
        bars=[MarketBar(**_parse_bar(item)) for item in payload["bars"]],
        prior_bars=[MarketBar(**_parse_bar(item)) for item in payload["prior_bars"]],
        quote=QuoteSnapshot(**_parse_quote(payload["quote"])),
        trades=[TradePrint(**_parse_trade(item)) for item in payload["trades"]],
        depth=DepthSnapshot(
            timestamp=datetime.fromisoformat(payload["depth"]["timestamp"]),
            bids=[DepthLevel(**item) for item in payload["depth"]["bids"]],
            asks=[DepthLevel(**item) for item in payload["depth"]["asks"]],
        ),
        portfolio=PortfolioSnapshot(**payload["portfolio"]),
    )
    return {
        "scenario": scenario,
        "scenario_label": _SCENARIO_LABELS[scenario],
        "symbol_code": symbol_code.upper(),
        "available_symbols": available_symbols(),
        "available_scenarios": available_scenarios(),
        "request": payload,
        "analysis": jsonable_encoder(asdict(bundle)),
    }


def _make_bars(start: datetime, rows: list[tuple[float, float, float, float]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, (open_price, high_price, low_price, close_price) in enumerate(rows):
        result.append(
            {
                "timestamp": (start + timedelta(minutes=30 * index)).isoformat(),
                "open": round(open_price, 2),
                "high": round(high_price, 2),
                "low": round(low_price, 2),
                "close": round(close_price, 2),
                "volume": 900 + (index * 140),
            }
        )
    return result


def _make_trades(start: datetime, rows: list[tuple[float, float, str]]) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": (start + timedelta(seconds=index * 12)).isoformat(),
            "price": round(price, 2),
            "quantity": qty,
            "aggressor_side": side,
        }
        for index, (price, qty, side) in enumerate(rows)
    ]


def _parse_bar(item: dict[str, Any]) -> dict[str, Any]:
    return {
        **item,
        "timestamp": datetime.fromisoformat(item["timestamp"]),
    }


def _parse_quote(item: dict[str, Any]) -> dict[str, Any]:
    return {
        **item,
        "timestamp": datetime.fromisoformat(item["timestamp"]),
    }


def _parse_trade(item: dict[str, Any]) -> dict[str, Any]:
    return {
        **item,
        "timestamp": datetime.fromisoformat(item["timestamp"]),
    }
