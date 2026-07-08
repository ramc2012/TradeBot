from __future__ import annotations

from pathlib import Path

import pytest

from macd_refined.config import clone_default_config
from macd_refined.paper import MacdRefinedPaperStore


def _store(tmp_path: Path) -> MacdRefinedPaperStore:
    config = clone_default_config()
    config["paper_trading"]["journal_root"] = str(tmp_path / "paper")
    return MacdRefinedPaperStore(config["paper_trading"]["journal_root"], config=config)


def _proposal() -> dict:
    return {
        "underlying": "BAJFINANCE",
        "option_type": "CE",
        "trading_symbol": "BAJFINANCE 1050 CE",
        "instrument_key": "NSE:BAJFINANCE26JUL1050CE",
        "expiry": "2026-07-28",
        "expiry_window_end": "2026-07-21",
        "strike": 1050.0,
        "spot": 1054.0,
        "lot_size": 750,
        "quantity_lots": 20,
        "quantity_units": 15_000,
        "entry_premium": 27.30,
        "iv": 0.2302,
        "selection_reason": "test",
    }


def test_macd_refined_open_pnl_matches_displayed_premiums(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.sync_cycle(proposals=[_proposal()], marks={}, now="2026-07-07T09:21:18+00:00")
    position_id = store.list_positions(status="open")["open_positions"][0]["position_id"]

    payload = store.sync_cycle(
        proposals=[],
        marks={position_id: {"premium": 22.50, "spot": 1043.40}},
        now="2026-07-07T09:51:22+00:00",
        allow_entries=False,
    )
    row = store.list_positions(status="open")["open_positions"][0]

    assert row["unrealized_pnl"] == pytest.approx((22.50 - 27.30) * 15_000)
    assert row["unrealized_pnl_gross"] == pytest.approx(-72_000.0)
    assert row["unrealized_pnl_net"] == pytest.approx(-90_675.0)
    assert payload["unrealized_pnl"] == pytest.approx(-72_000.0)
    assert payload["unrealized_pnl_net"] == pytest.approx(-90_675.0)


def test_macd_refined_closed_pnl_matches_displayed_entry_exit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.sync_cycle(proposals=[_proposal()], marks={}, now="2026-07-07T09:21:18+00:00")
    position_id = store.list_positions(status="open")["open_positions"][0]["position_id"]

    payload = store.sync_cycle(
        proposals=[],
        marks={position_id: {"premium": 18.00, "spot": 1030.00}},
        now="2026-07-07T10:01:22+00:00",
        allow_entries=False,
    )
    row = store.list_positions(status="closed")["closed_positions"][0]

    assert row["realized_pnl"] == pytest.approx((18.00 - 27.30) * 15_000)
    assert row["realized_pnl_gross"] == pytest.approx(-139_500.0)
    assert row["realized_pnl_net"] < row["realized_pnl"]
    assert payload["realized_pnl"] == pytest.approx(-139_500.0)
    assert payload["realized_pnl_net"] < payload["realized_pnl"]
