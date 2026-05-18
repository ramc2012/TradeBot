from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from cbe_scanner import CBEConfig, SyntheticDataProvider, generate_watchlist, scan_universe
from cbe_scanner.repository import _coerce_scan_date
from cbe_scanner.service import run_synthetic_scan


PLANTED_BULLISH = {"RELIANCE", "TCS", "ICICIBANK"}
PLANTED_BEARISH = {"TATAMOTORS", "BAJFINANCE"}
PLANTED = PLANTED_BULLISH | PLANTED_BEARISH


def test_cbe_scanner_identifies_planted_synthetic_signals() -> None:
    universe = [
        "RELIANCE",
        "TCS",
        "ICICIBANK",
        "TATAMOTORS",
        "BAJFINANCE",
        "INFY",
        "HDFCBANK",
        "SBIN",
        "BHARTIARTL",
        "KOTAKBANK",
        "HCLTECH",
        "WIPRO",
        "MARUTI",
        "ASIANPAINT",
        "AXISBANK",
        "LT",
        "ITC",
        "NTPC",
        "POWERGRID",
        "ULTRACEMCO",
    ]
    cfg = CBEConfig()
    provider = SyntheticDataProvider(seed=42, today=pd.Timestamp("2024-12-27"))

    scan_df = scan_universe(universe, provider, pd.Timestamp("2024-12-27"), cfg)
    top_8 = set(scan_df.head(8)["instrument"])

    assert PLANTED <= top_8
    for _, row in scan_df[scan_df["instrument"].isin(PLANTED)].iterrows():
        expected = "bullish" if row["instrument"] in PLANTED_BULLISH else "bearish"
        assert row["directional_bias"] == expected

    watchlist = generate_watchlist(scan_df, cfg)
    assert set(watchlist["instrument"]) & PLANTED


def test_cbe_service_returns_json_ready_payload() -> None:
    payload = run_synthetic_scan(scan_date=pd.Timestamp("2024-12-27"))

    assert payload["source"] == "synthetic"
    assert payload["scored_count"] == 20
    assert payload["watchlist_count"] >= 1
    assert "details" in payload["results"][0]
    assert isinstance(payload["watchlist"][0]["composite_score"], float)


def test_cbe_repository_coerces_scan_date_for_postgres() -> None:
    assert _coerce_scan_date("2026-05-18") == date(2026, 5, 18)
    assert _coerce_scan_date(datetime(2026, 5, 18, 9, 15)) == date(2026, 5, 18)
    assert _coerce_scan_date(date(2026, 5, 18)) == date(2026, 5, 18)
    assert _coerce_scan_date(None) is None
