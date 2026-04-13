from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from fastapi.encoders import jsonable_encoder

from auction_intelligence.config import clone_default_config
from auction_intelligence.options.ntm_volx import NTMVolXAnalyzer
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
from brokers.base import OptionChain, OptionChainEntry


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
    quote_history = _make_quote_history(bars)

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
        "quote_history": quote_history,
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
    ntm_volx = _build_demo_ntm_volx(payload)
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
        ntm_volx=ntm_volx,
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


def _make_quote_history(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    quotes: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
        close = float(bar["close"])
        spread = 2.0 if close >= 10_000 else 1.0
        quotes.append(
            {
                "timestamp": bar["timestamp"],
                "bid": round(close - (spread / 2), 2),
                "ask": round(close + (spread / 2), 2),
                "bid_size": 280 + (index * 18),
                "ask_size": 250 + max(0, (len(bars) - index - 1) * 16),
            }
        )
    return quotes


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


def _build_demo_ntm_volx(payload: dict[str, Any]):
    symbol_code = str(payload.get("metadata", {}).get("symbol_code") or "").upper()
    scenario = str(payload.get("metadata", {}).get("scenario") or "")
    spot_price = float(payload.get("session", {}).get("last_price") or 0.0)
    if not symbol_code or spot_price <= 0:
        return None

    strike_step = 50.0 if symbol_code == "NIFTY" else 100.0
    atm_strike = round(spot_price / strike_step) * strike_step
    strikes = [atm_strike + (offset * strike_step) for offset in (-2, -1, 0, 1, 2)]
    bias = {
        "acceptance_up": "CALLS",
        "failed_auction": "CALLS",
        "balance": "BALANCED",
    }.get(scenario, "BALANCED")

    entries: list[OptionChainEntry] = []
    for index, strike in enumerate(strikes):
        distance = abs(strike - spot_price) / max(strike_step, 1.0)
        base_extrinsic = max((strike_step * 1.45) - (distance * strike_step * 0.28), strike_step * 0.45)
        call_intrinsic = max(spot_price - strike, 0.0)
        put_intrinsic = max(strike - spot_price, 0.0)
        base_volume = max(4_500.0, 18_000.0 - (index * 2_200.0))
        base_oi = max(10_000.0, 26_000.0 - (index * 2_000.0))

        if bias == "CALLS":
            call_volume = base_volume * (1.75 if distance <= 1 else 1.35)
            put_volume = base_volume * (0.72 if distance <= 1 else 0.82)
            call_oi_change = base_oi * 0.08
            put_oi_change = base_oi * 0.02
        elif bias == "PUTS":
            call_volume = base_volume * (0.72 if distance <= 1 else 0.82)
            put_volume = base_volume * (1.75 if distance <= 1 else 1.35)
            call_oi_change = base_oi * 0.02
            put_oi_change = base_oi * 0.08
        else:
            call_volume = base_volume * (1.05 if distance <= 1 else 0.95)
            put_volume = base_volume * (0.98 if distance <= 1 else 1.02)
            call_oi_change = base_oi * 0.03
            put_oi_change = base_oi * 0.03

        call_ltp = round(call_intrinsic + base_extrinsic, 2)
        put_ltp = round(put_intrinsic + base_extrinsic, 2)
        entries.append(
            OptionChainEntry(
                strike=strike,
                option_type="CE",
                ltp=call_ltp,
                oi=int(base_oi + call_oi_change),
                volume=int(call_volume),
                bid=round(call_ltp - 0.5, 2),
                ask=round(call_ltp + 0.5, 2),
                delta=0.55 if strike <= atm_strike else 0.42,
                prev_oi=int(base_oi),
                prev_close=round(max(call_ltp - (1.8 if bias == "CALLS" else 0.8), 1.0), 2),
            )
        )
        entries.append(
            OptionChainEntry(
                strike=strike,
                option_type="PE",
                ltp=put_ltp,
                oi=int(base_oi + put_oi_change),
                volume=int(put_volume),
                bid=round(put_ltp - 0.5, 2),
                ask=round(put_ltp + 0.5, 2),
                delta=-0.55 if strike >= atm_strike else -0.42,
                prev_oi=int(base_oi),
                prev_close=round(max(put_ltp - (1.8 if bias == "PUTS" else 0.8), 1.0), 2),
            )
        )

    analyzer = NTMVolXAnalyzer(clone_default_config().get("options_mapping", {}).get("ntm_volx"))
    chain = OptionChain(
        symbol=f"{symbol_code} DEMO",
        expiry=(date.fromisoformat(payload["session"]["session_date"]) + timedelta(days=2)).isoformat(),
        spot_price=spot_price,
        entries=entries,
    )
    return analyzer.analyze_chain(
        underlying=symbol_code,
        expiry=chain.expiry,
        chain=chain,
    )
