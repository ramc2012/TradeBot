from __future__ import annotations

import asyncio
from datetime import date

import httpx

from analytics.technicals import latest_macd_rsi
from analytics.sector import SectorRotationTracker
from brokers.base import OptionChain, OptionChainEntry
from brokers.fyers import FyersAdapter
import market_data.option_history as option_history_module
from data.upstox_research_sync import UpstoxResearchSync
from market_data.option_history import OptionHistoryService
from market_data.option_chain import OptionChainService


def test_option_chain_analytics_include_previous_day_deltas() -> None:
    service = OptionChainService()
    chain = OptionChain(
        symbol="NSE_INDEX|Nifty 50",
        expiry="2026-03-30",
        spot_price=22820.0,
        entries=[
            OptionChainEntry(
                strike=22800.0,
                option_type="CE",
                ltp=205.0,
                oi=150000,
                volume=32000,
                bid=204.0,
                ask=206.0,
                iv=18.2,
                delta=0.52,
                gamma=0.006,
                theta=-12.4,
                vega=8.6,
                prev_oi=120000,
                prev_close=180.0,
            ),
            OptionChainEntry(
                strike=22800.0,
                option_type="PE",
                ltp=188.0,
                oi=175000,
                volume=29800,
                bid=187.0,
                ask=189.0,
                iv=17.9,
                delta=-0.48,
                gamma=0.006,
                theta=-11.8,
                vega=8.2,
                prev_oi=190000,
                prev_close=210.0,
            ),
        ],
    )

    analytics = service._calculate_analytics(chain)

    assert analytics["total_ce_oi_change"] == 30000.0
    assert analytics["total_pe_oi_change"] == -15000.0
    assert analytics["pcr_prev_oi"] == round(190000 / 120000, 4)
    assert analytics["atm_call_ltp_change"] == 25.0
    assert analytics["atm_put_ltp_change"] == -22.0


def test_sector_rrg_uses_seeded_baseline_when_only_one_live_sample_exists() -> None:
    tracker = SectorRotationTracker()
    series = tracker._build_rrg_series(
        [29671.3, 29541.65, 29980.0],
        [23306.45, 22819.6, 22980.0],
    )

    assert len(series) == 3
    assert series[-1]["ratio"] > 100.0
    assert series[-1]["momentum"] != 100.0


def test_sector_rotation_row_carries_source_metadata() -> None:
    tracker = SectorRotationTracker()
    row = tracker._build_rotation_row(
        code="IT",
        name="IT",
        symbol="NSE:NIFTYIT-INDEX",
        closes=[100.0, 101.5, 103.0, 105.0],
        benchmark_closes=[100.0, 100.5, 101.0, 101.2],
        trail_limit=4,
        sample_count=4,
        series_source="fyers",
        member_count=12,
    )

    assert row["series_source"] == "fyers"
    assert row["member_count"] == 12
    assert row["quadrant"] in {"leading", "improving", "weakening", "lagging"}


def test_fyers_expiry_helpers_convert_dates() -> None:
    expiry_rows = [
        {"date": "30-03-2026", "expiry": "1774864800"},
        {"date": "07-04-2026", "expiry": "1775556000"},
    ]

    assert FyersAdapter._expiry_date_to_epoch("2026-03-30", expiry_rows) == "1774864800"
    assert FyersAdapter._epoch_to_iso_date("1774864800") == "2026-03-30"


def test_contract_priority_focuses_on_near_atm_common_strikes() -> None:
    sync = UpstoxResearchSync(
        access_token="test-token",
        from_date=date(2025, 3, 1),
        to_date=date(2026, 3, 1),
    )
    contracts = []
    for strike in (90, 95, 100, 105, 110, 115):
        contracts.append({
            "instrument_key": f"CE-{strike}",
            "instrument_type": "CE",
            "strike_price": strike,
        })
        contracts.append({
            "instrument_key": f"PE-{strike}",
            "instrument_type": "PE",
            "strike_price": strike,
        })

    priority_keys = sync._prioritized_contract_keys(contracts, selection_spot_price=101.0)

    assert "CE-100" in priority_keys
    assert "PE-100" in priority_keys
    assert "CE-105" in priority_keys
    assert "PE-105" in priority_keys
    assert len(priority_keys) == 4
    assert "CE-95" not in priority_keys
    assert "PE-95" not in priority_keys
    assert "CE-110" not in priority_keys
    assert "PE-110" not in priority_keys
    assert "CE-90" not in priority_keys
    assert "PE-115" not in priority_keys


def test_contract_reprioritization_preserves_synced_priority_and_skips_noise() -> None:
    sync = UpstoxResearchSync(
        access_token="test-token",
        from_date=date(2025, 3, 1),
        to_date=date(2026, 3, 1),
    )

    assert sync._desired_contract_state(
        current_status="complete",
        current_last_error=None,
        prioritized=True,
    ) == ("complete", None)
    assert sync._desired_contract_state(
        current_status="empty",
        current_last_error="No candles returned",
        prioritized=True,
    ) == ("empty", "No candles returned")
    assert sync._desired_contract_state(
        current_status="pending",
        current_last_error="Old error",
        prioritized=True,
    ) == ("pending", None)
    assert sync._desired_contract_state(
        current_status="complete",
        current_last_error=None,
        prioritized=False,
    ) == ("skipped", sync.PRIORITY_SKIP_REASON)


def test_latest_macd_rsi_returns_values_once_series_is_long_enough() -> None:
    closes = [100 + (index * 0.8) for index in range(40)]
    indicators = latest_macd_rsi(closes)

    assert indicators["macd"] is not None
    assert indicators["macd_signal"] is not None
    assert indicators["macd_histogram"] is not None
    assert indicators["rsi"] is not None


def test_option_history_interval_mappings_cover_five_minute_backfill() -> None:
    service = OptionHistoryService()

    assert service._upstox_interval("5minute") == "5minute"
    assert service._fyers_resolution("5minute") == "5"
    assert service._upstox_interval("30minute") == "30minute"
    assert service._needs_upstox_minute_fallback("NSE_FO|12345", "5minute") is True
    assert service._needs_upstox_minute_fallback("NSE_FO|12345", "15minute") is True
    assert service._needs_upstox_minute_fallback("NSE_FO|12345", "30minute") is False
    assert service._broker_lookback_days("1minute", limit=500) == 5
    assert service._broker_lookback_days("5minute", limit=96) == 5


def test_option_history_can_aggregate_one_minute_rows_to_five_minute() -> None:
    service = OptionHistoryService()
    rows = [
        {
            "time": f"2026-04-09T09:{15 + idx:02d}:00+05:30",
            "open": 100 + idx,
            "high": 101 + idx,
            "low": 99 + idx,
            "close": 100.5 + idx,
            "volume": 10,
        }
        for idx in range(5)
    ]

    aggregated = service._aggregate_rows(rows, 5)

    assert len(aggregated) == 1
    assert aggregated[0]["time"].startswith("2026-04-09T09:15:00")
    assert aggregated[0]["open"] == 100
    assert aggregated[0]["close"] == 104.5
    assert aggregated[0]["volume"] == 50


def test_option_history_upstox_ssl_errors_degrade_to_empty_rows(monkeypatch) -> None:
    service = OptionHistoryService()
    service.reset_health()

    class BrokenClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def get(self, *args, **kwargs):
            raise httpx.ConnectError("ssl failure")

    monkeypatch.setattr(option_history_module, "get_broker_token", lambda broker: "token")
    monkeypatch.setattr(option_history_module.httpx, "AsyncClient", BrokenClient)

    rows = asyncio.run(
        service._fetch_broker_candles(
            instrument_key="NSE_FO|12345",
            from_date=date(2026, 4, 1),
            to_date=date(2026, 4, 9),
            interval="30minute",
        )
    )

    assert rows == []
    health = service.get_health_snapshot()
    assert health["failure_count"] >= 1
    assert health["brokers"]["upstox"]["last_status"] == "failure"
    assert "ssl failure" in health["brokers"]["upstox"]["last_detail"]


def test_fyers_tick_parser_uses_name_field_when_symbol_missing() -> None:
    adapter = FyersAdapter()
    captured = []

    adapter._handle_tick(
        {
            "n": "NSE:NIFTY50-INDEX",
            "ltp": 22510.0,
            "open_price": 22400.0,
            "high_price": 22540.0,
            "low_price": 22380.0,
            "prev_close_price": 22395.0,
        },
        captured.append,
    )

    assert len(captured) == 1
    assert captured[0].symbol == "NSE:NIFTY50-INDEX"
    assert captured[0].ltp == 22510.0


def test_option_history_uses_upstox_intraday_for_current_day_requests(monkeypatch) -> None:
    service = OptionHistoryService()
    requested_urls: list[str] = []
    today = date(2026, 4, 10)

    class FakeResponse:
        def __init__(self, url: str) -> None:
            self.status_code = 200
            self._url = url

        def json(self) -> dict:
            if "/intraday/" in self._url:
                candles = [["2026-04-10T09:15:00+05:30", 110, 112, 109, 111, 25, 7]]
            else:
                candles = [["2026-04-09T15:25:00+05:30", 100, 101, 99, 100.5, 20, 5]]
            return {"data": {"candles": candles}}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def get(self, url: str, *args, **kwargs):
            requested_urls.append(url)
            return FakeResponse(url)

    monkeypatch.setattr(OptionHistoryService, "_today_ist_date", staticmethod(lambda: today))
    monkeypatch.setattr(option_history_module, "get_broker_token", lambda broker: "token")
    monkeypatch.setattr(option_history_module.httpx, "AsyncClient", FakeClient)

    rows = asyncio.run(
        service._fetch_broker_candles(
            instrument_key="NSE_INDEX|Nifty 50",
            from_date=date(2026, 4, 9),
            to_date=today,
            interval="1minute",
        )
    )

    assert len(rows) == 2
    assert rows[0]["time"].startswith("2026-04-09")
    assert rows[-1]["time"].startswith("2026-04-10")
    assert any("/historical-candle/intraday/" in url for url in requested_urls)
    assert any("/historical-candle/" in url and "/intraday/" not in url for url in requested_urls)


def test_option_history_marks_previous_session_intraday_rows_as_stale(monkeypatch) -> None:
    service = OptionHistoryService()
    monkeypatch.setattr(OptionHistoryService, "_today_ist_date", staticmethod(lambda: date(2026, 4, 10)))

    stale_rows = [{"time": "2026-04-09T15:29:00+05:30", "close": 101.0}]
    fresh_rows = [{"time": "2026-04-10T09:20:00+05:30", "close": 103.0}]

    assert service._latest_row_is_stale_for_today(stale_rows, "5minute") is True
    assert service._latest_row_is_stale_for_today(fresh_rows, "5minute") is False
    assert service._latest_row_is_stale_for_today(stale_rows, "1day") is False
