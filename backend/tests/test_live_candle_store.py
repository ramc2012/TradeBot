from __future__ import annotations

from datetime import datetime, timezone

import pytest

from market_data.live_candle_store import LiveCandleStore, _CandleBucket


class _FakeSession:
    def __init__(self, statements: list[tuple[str, object]]) -> None:
        self._statements = statements

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def execute(self, stmt, params=None):
        self._statements.append((str(stmt), params))
        return None

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_live_candle_store_persists_spot_and_option_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    store = LiveCandleStore()
    statements: list[tuple[str, object]] = []

    monkeypatch.setattr(
        "market_data.live_candle_store.AsyncSessionLocal",
        lambda: _FakeSession(statements),
    )

    async def _resolve(symbol: str):
        if symbol == "NSE:NIFTY50-INDEX":
            return {
                "kind": "spot",
                "underlying": "NIFTY",
                "instrument_key": "NSE_INDEX|Nifty 50",
            }
        return {
            "kind": "option",
            "instrument_key": "NSE:TEST26APR24000CE",
            "trading_symbol": "TEST26APR24000CE",
            "underlying": "NIFTY",
            "expiry": "2026-04-30",
            "strike": 24000.0,
            "option_type": "CE",
            "market": "NSE",
        }

    monkeypatch.setattr(store, "_resolve_symbol_metadata", _resolve)

    now = datetime(2026, 4, 11, 9, 15, tzinfo=timezone.utc)
    store._latest_spot["NIFTY"] = 24050.0
    store._buckets[("NSE:NIFTY50-INDEX", "1minute")] = _CandleBucket(
        symbol="NSE:NIFTY50-INDEX",
        interval="1minute",
        bucket_start=now,
        open=24010.0,
        high=24060.0,
        low=24000.0,
        close=24050.0,
        volume=100,
        oi=0,
        updated_at=now,
    )
    store._buckets[("NSE:TEST26APR24000CE", "5minute")] = _CandleBucket(
        symbol="NSE:TEST26APR24000CE",
        interval="5minute",
        bucket_start=now,
        open=100.0,
        high=105.0,
        low=99.0,
        close=103.0,
        volume=50,
        oi=10,
        updated_at=now,
    )

    await store._persist_candles()

    assert any("underlying_spot_candles" in sql for sql, _ in statements)
    assert any("option_premium_candles" in sql for sql, _ in statements)

    spot_payload = next(params for sql, params in statements if "underlying_spot_candles" in sql)
    option_payload = next(params for sql, params in statements if "option_premium_candles" in sql)

    assert isinstance(spot_payload, list)
    assert isinstance(option_payload, list)
    assert spot_payload[0]["interval"] == "1minute"
    assert option_payload[0]["interval"] == "5minute"
    assert option_payload[0]["underlying_price"] == 24050.0
