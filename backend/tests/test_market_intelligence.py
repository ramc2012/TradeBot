from __future__ import annotations

from datetime import date

from analytics.sector import SectorRotationTracker
from brokers.base import OptionChain, OptionChainEntry
from data.upstox_research_sync import UpstoxResearchSync
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
    tracker._baseline_price["NSE:NIFTYIT-INDEX"] = 29671.3
    tracker._baseline_price["NSE:NIFTY50-INDEX"] = 23306.45

    series = tracker._build_rrg_series(
        "NSE:NIFTYIT-INDEX",
        "NSE:NIFTY50-INDEX",
        [29541.65],
        [22819.6],
    )

    assert len(series) == 2
    assert series[-1]["ratio"] > 100.0
    assert series[-1]["momentum"] > 100.0


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
