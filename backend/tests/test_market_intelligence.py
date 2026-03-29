from __future__ import annotations

from analytics.sector import SectorRotationTracker
from brokers.base import OptionChain, OptionChainEntry
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
