from __future__ import annotations

import importlib
import json
from datetime import datetime

import pytest

from brokers.base import OptionChain, OptionChainEntry
from market_data.market_intelligence_runtime import APP_SYMBOLS, IST, MarketIntelligenceRuntime


market_intelligence_module = importlib.import_module("market_data.market_intelligence_runtime")


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value


class _FakeUpstoxAdapter:
    broker_name = "upstox"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        self.calls.append((symbol, expiry))
        if symbol == "BSE_INDEX|SENSEX":
            raise RuntimeError("upstox missing sensex chain")
        return OptionChain(
            symbol=symbol,
            expiry=expiry,
            spot_price=22500.0,
            entries=[
                OptionChainEntry(
                    strike=22500.0,
                    option_type="CE",
                    ltp=150.0,
                    oi=1000,
                    volume=500,
                    bid=149.5,
                    ask=150.5,
                    prev_oi=900,
                    prev_close=140.0,
                    instrument_key="UPSTOX-CE",
                ),
                OptionChainEntry(
                    strike=22500.0,
                    option_type="PE",
                    ltp=130.0,
                    oi=1100,
                    volume=450,
                    bid=129.5,
                    ask=130.5,
                    prev_oi=1000,
                    prev_close=135.0,
                    instrument_key="UPSTOX-PE",
                ),
            ],
        )


class _FakeFyersAdapter:
    broker_name = "fyers"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        self.calls.append((symbol, expiry))
        return OptionChain(
            symbol=symbol,
            expiry=expiry,
            spot_price=78500.0,
            entries=[
                OptionChainEntry(
                    strike=78500.0,
                    option_type="CE",
                    ltp=220.0,
                    oi=800,
                    volume=300,
                    bid=219.5,
                    ask=220.5,
                    prev_oi=750,
                    prev_close=210.0,
                    instrument_key="FYERS-CE",
                ),
                OptionChainEntry(
                    strike=78500.0,
                    option_type="PE",
                    ltp=205.0,
                    oi=780,
                    volume=290,
                    bid=204.5,
                    ask=205.5,
                    prev_oi=760,
                    prev_close=215.0,
                    instrument_key="FYERS-PE",
                ),
            ],
        )


async def _fake_get_redis(redis: _FakeRedis) -> _FakeRedis:
    return redis


@pytest.mark.asyncio
async def test_refresh_index_option_chains_prefers_upstox_and_falls_back_to_fyers(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MarketIntelligenceRuntime()
    redis = _FakeRedis()
    upstox = _FakeUpstoxAdapter()
    fyers = _FakeFyersAdapter()

    async def fake_candidates() -> list[tuple[str, str]]:
        return [
            ("NIFTY", "2026-04-28"),
            ("SENSEX", "2026-04-30"),
        ]

    async def fake_ensure_session(*, force_validate: bool = False) -> bool:
        return False

    monkeypatch.setattr(runtime, "_load_chain_refresh_candidates", fake_candidates)
    monkeypatch.setattr(market_intelligence_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(
        market_intelligence_module,
        "get_active_adapter",
        lambda broker: {"upstox": upstox, "fyers": fyers}.get(broker),
    )
    monkeypatch.setattr(market_intelligence_module, "ensure_upstox_session", fake_ensure_session)
    monkeypatch.setattr(market_intelligence_module, "ensure_fyers_session", fake_ensure_session)

    payload = await runtime.refresh_index_option_chains()

    assert payload["status"] == "ok"
    assert payload["requests"] == [
        {"symbol_code": "NIFTY", "expiry": "2026-04-28", "status": "refreshed", "source": "upstox"},
        {"symbol_code": "SENSEX", "expiry": "2026-04-30", "status": "refreshed", "source": "fyers"},
    ]
    assert upstox.calls == [
        ("NSE_INDEX|Nifty 50", "2026-04-28"),
        ("BSE_INDEX|SENSEX", "2026-04-30"),
    ]
    assert fyers.calls == [("BSE:SENSEX-INDEX", "2026-04-30")]

    cached_nifty = json.loads(redis.values[f"oc:{APP_SYMBOLS['NIFTY']}:2026-04-28"])
    cached_sensex = json.loads(redis.values[f"oc:{APP_SYMBOLS['SENSEX']}:2026-04-30"])
    assert cached_nifty["source"] == "upstox"
    assert cached_sensex["source"] == "fyers"
    assert cached_nifty["entries"][0]["instrument_key"] == "UPSTOX-CE"
    assert cached_sensex["entries"][0]["instrument_key"] == "FYERS-CE"


@pytest.mark.asyncio
async def test_refresh_index_option_chains_respects_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MarketIntelligenceRuntime()
    runtime._last_chain_refresh_at = datetime.now(IST)
    observed: list[str] = []

    def fake_get_active_adapter(broker: str):
        observed.append(broker)
        return None

    monkeypatch.setattr(market_intelligence_module, "get_active_adapter", fake_get_active_adapter)

    payload = await runtime.refresh_index_option_chains()

    assert payload["status"] == "cooldown"
    assert payload["source"] == "cached"
    assert payload["requests"] == []
    assert observed == []
