from __future__ import annotations

import asyncio
from datetime import date

import market_data.atm_watchlist as atm_watchlist_module
from brokers.base import OptionChain, OptionChainEntry
from market_data.atm_watchlist import ATMWatchlistService, UnderlyingMeta


class _FakeRedis:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    async def get(self, key: str):
        return self._values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._values[key] = value

    async def delete(self, key: str) -> None:
        self._values.pop(key, None)


class _FakeFyersAdapter:
    async def get_option_contracts(self, symbol: str) -> list[dict]:
        return [
            {"expiry": "2026-04-23"},
            {"expiry": "2026-05-28"},
        ]

    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        return OptionChain(
            symbol=symbol,
            expiry=expiry,
            spot_price=55912.75,
            entries=[
                OptionChainEntry(
                    strike=55900.0,
                    option_type="CE",
                    ltp=212.0,
                    oi=100,
                    volume=120,
                    bid=211.5,
                    ask=212.5,
                    prev_close=198.0,
                    prev_oi=90,
                    instrument_key="FYERS-CE",
                ),
                OptionChainEntry(
                    strike=55900.0,
                    option_type="PE",
                    ltp=188.0,
                    oi=110,
                    volume=100,
                    bid=187.5,
                    ask=188.5,
                    prev_close=176.0,
                    prev_oi=95,
                    instrument_key="FYERS-PE",
                ),
            ],
        )


async def _fake_get_redis(redis: _FakeRedis) -> _FakeRedis:
    return redis


def test_get_broker_expiries_reuses_persisted_ladder_when_brokers_are_unavailable(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()
    meta = UnderlyingMeta(
        symbol="BANKNIFTY",
        kind="INDEX",
        spot_instrument_key="NSE_INDEX|Nifty Bank",
        underlying_key="NSE_INDEX|Nifty Bank",
    )

    async def fake_load_persisted(symbol: str) -> list[str]:
        assert symbol == "BANKNIFTY"
        return ["2026-04-28", "2026-05-26"]

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", fake_load_persisted)

    expiries = asyncio.run(service._get_broker_expiries_for_symbol(meta, None, None))

    assert expiries == ["2026-04-28", "2026-05-26"]
    assert redis._values["atm_watchlist:sym_expiries:v1:BANKNIFTY"]


def test_get_broker_expiries_prefers_saved_ladder_over_fyers_when_upstox_is_offline(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()
    meta = UnderlyingMeta(
        symbol="NIFTY",
        kind="INDEX",
        spot_instrument_key="NSE_INDEX|Nifty 50",
        underlying_key="NSE_INDEX|Nifty 50",
    )

    async def fake_load_persisted(symbol: str) -> list[str]:
        assert symbol == "NIFTY"
        return ["2026-04-28", "2026-05-26"]

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", fake_load_persisted)

    expiries = asyncio.run(service._get_broker_expiries_for_symbol(meta, None, _FakeFyersAdapter()))

    assert expiries == ["2026-04-28", "2026-05-26"]
    assert "2026-04-23" not in expiries


def test_build_row_uses_saved_contract_metadata_when_upstox_is_offline(monkeypatch) -> None:
    service = ATMWatchlistService()
    meta = UnderlyingMeta(
        symbol="BANKNIFTY",
        kind="INDEX",
        spot_instrument_key="NSE_INDEX|Nifty Bank",
        underlying_key="NSE_INDEX|Nifty Bank",
    )
    expiry = "2026-04-28"

    async def fake_get_expiries(meta_arg: UnderlyingMeta, upstox_adapter, fyers_adapter) -> list[str]:
        assert meta_arg.symbol == "BANKNIFTY"
        return [expiry, "2026-05-26"]

    async def fake_get_contracts(meta_arg: UnderlyingMeta, expiry_arg: str, upstox_adapter) -> list[dict]:
        assert meta_arg.symbol == "BANKNIFTY"
        assert expiry_arg == expiry
        assert upstox_adapter is None
        return [
            {
                "instrument_key": "UPSTOX-CE",
                "trading_symbol": "BANKNIFTY26APR55900CE",
                "strike_price": 55900.0,
                "instrument_type": "CE",
                "expiry": expiry,
                "lot_size": 30,
            },
            {
                "instrument_key": "UPSTOX-PE",
                "trading_symbol": "BANKNIFTY26APR55900PE",
                "strike_price": 55900.0,
                "instrument_type": "PE",
                "expiry": expiry,
                "lot_size": 30,
            },
        ]

    async def fake_load_technicals(**kwargs) -> dict:
        return {"macd": None, "macd_signal": None, "macd_histogram": None, "rsi": None}

    async def fake_persist_snapshot(**kwargs) -> None:
        return None

    persisted_lot_sizes: list[tuple[str, int]] = []

    async def fake_persist_lot_size(symbol: str, lot_size: int) -> None:
        persisted_lot_sizes.append((symbol, lot_size))

    monkeypatch.setattr(service, "_get_broker_expiries_for_symbol", fake_get_expiries)
    monkeypatch.setattr(service, "_get_contracts_for_expiry", fake_get_contracts)
    monkeypatch.setattr(service, "_load_technicals", fake_load_technicals)
    monkeypatch.setattr(service, "_persist_snapshot", fake_persist_snapshot)
    monkeypatch.setattr(service, "_persist_lot_size", fake_persist_lot_size)

    row = asyncio.run(
        service._build_row(
            meta,
            expiry,
            date.fromisoformat(expiry),
            None,
            _FakeFyersAdapter(),
        )
    )

    assert row is not None
    assert row["expiry"] == expiry
    assert row["live_source"] == "fyers"
    assert row["lot_size"] == 30
    assert row["ce"]["instrument_key"] == "UPSTOX-CE"
    assert row["ce"]["trading_symbol"] == "BANKNIFTY26APR55900CE"
    assert row["pe"]["instrument_key"] == "UPSTOX-PE"
    assert persisted_lot_sizes == [("BANKNIFTY", 30)]


def test_get_expiries_reports_catalog_fallback_when_saved_ladder_is_used(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()

    async def fake_load_underlyings() -> list[UnderlyingMeta]:
        return [
            UnderlyingMeta("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
            UnderlyingMeta("BANKNIFTY", "INDEX", "NSE_INDEX|Nifty Bank", "NSE_INDEX|Nifty Bank"),
            UnderlyingMeta("FINNIFTY", "INDEX", "NSE_INDEX|Nifty Fin Service", "NSE_INDEX|Nifty Fin Service"),
            UnderlyingMeta("MIDCPNIFTY", "INDEX", "NSE_INDEX|Nifty Midcap Select", "NSE_INDEX|Nifty Midcap Select"),
            UnderlyingMeta("SENSEX", "INDEX", "BSE_INDEX|SENSEX", "BSE_INDEX|SENSEX"),
        ]

    async def fake_get_upstox_adapter():
        return None

    async def fake_ensure_fyers_session(*args, **kwargs) -> bool:
        return False

    async def fake_broker_expiries(meta: UnderlyingMeta, *args, **kwargs) -> list[str]:
        ladders = {
            "NIFTY": ["2026-04-30", "2026-05-28"],
            "BANKNIFTY": ["2026-04-28", "2026-05-26"],
            "FINNIFTY": ["2026-04-28", "2026-05-26"],
            "MIDCPNIFTY": ["2026-04-28", "2026-05-25"],
            "SENSEX": ["2026-04-24", "2026-05-29"],
        }
        return ladders[meta.symbol]

    async def fake_load_persisted(symbol: str) -> list[str]:
        ladders = {
            "NIFTY": ["2026-04-30", "2026-05-28"],
            "BANKNIFTY": ["2026-04-28", "2026-05-26"],
            "FINNIFTY": ["2026-04-28", "2026-05-26"],
            "MIDCPNIFTY": ["2026-04-28", "2026-05-25"],
            "SENSEX": ["2026-04-24", "2026-05-29"],
        }
        return ladders[symbol]

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: None)
    monkeypatch.setattr(atm_watchlist_module, "ensure_fyers_session", fake_ensure_fyers_session)
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_load_persisted_expiries_for_symbol", fake_load_persisted)
    monkeypatch.setattr(service, "_get_upstox_adapter", fake_get_upstox_adapter)
    monkeypatch.setattr(service, "_get_broker_expiries_for_symbol", fake_broker_expiries)

    payload = asyncio.run(service.get_expiries())

    assert payload["source"] == "catalog"
    assert payload["default_expiry"] == "2026-04-30"
    assert "saved expiry catalog" in str(payload["detail"])
    assert "2026-04-28" in payload["expiries"]
    assert "2026-04-30" in payload["expiries"]
    assert "BNKN 2026-04-28" in payload["expiry_scope_note"]


def test_get_watchlist_returns_building_payload_on_first_load(monkeypatch) -> None:
    service = ATMWatchlistService()
    redis = _FakeRedis()
    scheduled = []

    async def fake_get_expiries() -> dict[str, str]:
        return {"default_expiry": "2026-04-28"}

    async def fake_load_underlyings() -> list[UnderlyingMeta]:
        return [
            UnderlyingMeta("NIFTY", "INDEX", "NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"),
            UnderlyingMeta("BANKNIFTY", "INDEX", "NSE_INDEX|Nifty Bank", "NSE_INDEX|Nifty Bank"),
            UnderlyingMeta("RELIANCE", "STOCK", "NSE_EQ|RELIANCE", "NSE_EQ|RELIANCE"),
        ]

    async def fake_get_upstox_adapter():
        return None

    async def fake_build_row(meta: UnderlyingMeta, expiry: str, expiry_date: date, upstox_adapter, fyers_adapter):
        return {
            "underlying": meta.symbol,
            "kind": meta.kind,
            "spot_price": 100.0,
            "expiry": expiry,
            "atm_strike": 100.0,
            "live_source": "fyers",
            "ce": {"instrument_key": f"{meta.symbol}-CE", "option_type": "CE", "strike": 100.0, "ltp": 10.0, "oi": 1, "volume": 1},
            "pe": {"instrument_key": f"{meta.symbol}-PE", "option_type": "PE", "strike": 100.0, "ltp": 10.0, "oi": 1, "volume": 1},
        }

    def fake_ensure_future(coro):
        scheduled.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(atm_watchlist_module, "get_redis", lambda: _fake_get_redis(redis))
    monkeypatch.setattr(atm_watchlist_module, "get_active_adapter", lambda broker: _FakeFyersAdapter() if broker == "fyers" else None)
    monkeypatch.setattr(atm_watchlist_module.asyncio, "ensure_future", fake_ensure_future)
    monkeypatch.setattr(service, "get_expiries", fake_get_expiries)
    monkeypatch.setattr(service, "_load_underlyings", fake_load_underlyings)
    monkeypatch.setattr(service, "_get_upstox_adapter", fake_get_upstox_adapter)
    monkeypatch.setattr(service, "_build_row", fake_build_row)

    payload = asyncio.run(service.get_watchlist("2026-04-28"))

    assert payload["build_status"] == "building"
    assert payload["summary"]["total_rows"] == 2
    assert payload["rows"][0]["underlying"] in {"BANKNIFTY", "NIFTY"}
    assert "Building 1 remaining symbols in background." in str(payload["detail"])
    assert scheduled
