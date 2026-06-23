"""Unit tests for the CBE cash-equity paper book."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from cbe_scanner.paper import (
    CBEPaperBook,
    CBE_INITIAL_CAPITAL,
    HEDGE_MAX_GROSS_EXPOSURE_RATIO,
    HEDGE_MAX_NET_EXPOSURE_RATIO,
    HEDGE_MAX_SECTOR_EXPOSURE_RATIO,
    MIN_HOLD_TRADING_DAYS,
)


def _scan_payload(rows: list[dict[str, Any]], *, scan_date: str = "2026-05-29") -> dict[str, Any]:
    """Assemble a minimal scan payload shaped like run_scan's output."""
    watchlist = [r for r in rows if r.get("composite_score", 0.0) >= 5.0]
    return {
        "scan_date": scan_date,
        "scored_count": len(rows),
        "watchlist_count": len(watchlist),
        "results": rows,
        "watchlist": watchlist,
    }


def _row(
    symbol: str,
    bias: str,
    score: float,
    close: float,
    conviction: float = 0.6,
    sector: str | None = None,
) -> dict[str, Any]:
    return {
        "instrument": symbol,
        "composite_score": score,
        "composite_alpha_score": score * 10.0,
        "directional_bias": bias,
        "bias_conviction": conviction,
        "latest_close": close,
        "sector_code": sector,
    }


@pytest.fixture
def book() -> CBEPaperBook:
    tmp = tempfile.mkdtemp()
    return CBEPaperBook(tmp, initial_capital=1_000_000.0, position_notional_cap=100_000.0)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_empty_book_summary_is_baseline(book: CBEPaperBook):
    summary = _run(book.capital_status())
    assert summary["initial_capital"] == 1_000_000.0
    assert summary["available_capital"] == 1_000_000.0
    assert summary["reserved_margin"] == 0
    assert summary["total_equity"] == 1_000_000.0
    assert summary["open_positions"] == 0
    assert summary["closed_positions"] == 0
    assert summary["sharpe_ratio"] == 0.0
    assert summary["max_drawdown"] == 0.0


def test_bullish_signal_opens_long_position(book: CBEPaperBook):
    payload = _scan_payload([_row("RELIANCE", "bullish", 6.5, 2500.0)])
    summary = _run(book.sync_from_scan(payload))
    assert summary["open_positions"] == 1
    positions = _run(book.list_positions(status="open"))
    pos = positions["open_positions"][0]
    assert pos["instrument"] == "RELIANCE"
    assert pos["direction"] == "long"
    assert pos["entry_price"] == 2500.0
    # 100k cap / 2500 = 40 shares
    assert pos["quantity"] == 40
    assert pos["notional"] == 100_000.0
    # Reserved margin should reduce available_capital
    assert summary["available_capital"] == 900_000.0
    assert summary["reserved_margin"] == 100_000.0


def test_bearish_signal_opens_short_position(book: CBEPaperBook):
    payload = _scan_payload([_row("TCS", "bearish", 7.0, 4000.0)])
    _run(book.sync_from_scan(payload))
    positions = _run(book.list_positions(status="open"))
    pos = positions["open_positions"][0]
    assert pos["direction"] == "short"
    assert pos["quantity"] == 25  # 100k / 4000


def test_neutral_bias_does_not_open(book: CBEPaperBook):
    payload = _scan_payload([_row("INFY", "neutral", 6.0, 1500.0)])
    summary = _run(book.sync_from_scan(payload))
    assert summary["open_positions"] == 0


def test_below_watchlist_score_does_not_open(book: CBEPaperBook):
    # composite_score < 5.0 falls outside the watchlist
    payload = _scan_payload([_row("INFY", "bullish", 3.0, 1500.0)])
    summary = _run(book.sync_from_scan(payload))
    assert summary["open_positions"] == 0


def test_long_position_marks_to_market_on_repeat_scan(book: CBEPaperBook):
    _run(book.sync_from_scan(_scan_payload([_row("RELIANCE", "bullish", 6.5, 2500.0)])))
    # Price rises 2% — long should show unrealized gain
    _run(book.sync_from_scan(_scan_payload([_row("RELIANCE", "bullish", 6.5, 2550.0)])))
    positions = _run(book.list_positions(status="open"))
    pos = positions["open_positions"][0]
    assert pos["latest_close"] == 2550.0
    # 40 shares × (2550 - 2500) = +2000
    assert pos["unrealized_pnl"] == 2000.0


def test_short_position_gains_on_price_drop(book: CBEPaperBook):
    _run(book.sync_from_scan(_scan_payload([_row("TCS", "bearish", 7.0, 4000.0)])))
    _run(book.sync_from_scan(_scan_payload([_row("TCS", "bearish", 7.0, 3900.0)])))
    positions = _run(book.list_positions(status="open"))
    pos = positions["open_positions"][0]
    # 25 shares × (3900-4000) × -1 = +2500
    assert pos["unrealized_pnl"] == 2500.0


def test_bias_flip_held_during_min_hold_window(book: CBEPaperBook):
    """Per the weekly-rebalance rule, a bias flip on day 0 does NOT close
    the position — must wait MIN_HOLD_TRADING_DAYS first."""
    _run(book.sync_from_scan(_scan_payload([_row("RELIANCE", "bullish", 6.5, 2500.0)])))
    _run(book.sync_from_scan(_scan_payload([_row("RELIANCE", "bearish", 6.5, 2510.0)])))
    positions = _run(book.list_positions(status="all"))
    # Both signals arrived inside the min-hold window — original LONG stays open.
    assert len(positions["open_positions"]) == 1
    assert positions["open_positions"][0]["direction"] == "long"
    assert positions["open_positions"][0].get("pending_close_reason") == "bias_flip"
    assert not positions["closed_positions"]


def test_bias_flip_closes_after_min_hold_elapsed(book: CBEPaperBook):
    """Force-backdate opened_at and verify the next bias flip closes."""
    _run(book.sync_from_scan(_scan_payload([_row("RELIANCE", "bullish", 6.5, 2500.0)])))
    # Mutate the persisted state so the position looks old enough to exit.
    from datetime import datetime, timedelta, timezone
    import json
    state = json.loads(book.positions_path.read_text())
    state["open_positions"][0]["opened_at"] = (
        datetime.now(timezone.utc) - timedelta(days=MIN_HOLD_TRADING_DAYS * 7 // 5 + 1)
    ).isoformat()
    book.positions_path.write_text(json.dumps(state))
    _run(book.sync_from_scan(_scan_payload([_row("RELIANCE", "bearish", 6.5, 2510.0)])))
    positions = _run(book.list_positions(status="all"))
    assert len(positions["closed_positions"]) == 1
    closed = positions["closed_positions"][0]
    assert closed["close_reason"] == "bias_flip"
    assert closed["realized_pnl"] == 400.0
    # And the new short re-opened
    assert len(positions["open_positions"]) == 1
    assert positions["open_positions"][0]["direction"] == "short"


def test_drop_from_watchlist_held_during_min_hold(book: CBEPaperBook):
    _run(book.sync_from_scan(_scan_payload([_row("RELIANCE", "bullish", 6.5, 2500.0)])))
    # Several empty scans inside the window — position stays open.
    for _ in range(5):
        _run(book.sync_from_scan(_scan_payload([])))
    positions = _run(book.list_positions(status="all"))
    assert len(positions["open_positions"]) == 1
    assert not positions["closed_positions"]


def test_reset_archives_and_zeroes_book(book: CBEPaperBook):
    _run(book.sync_from_scan(_scan_payload([_row("RELIANCE", "bullish", 6.5, 2500.0)])))
    pre = _run(book.capital_status())
    assert pre["open_positions"] == 1

    result = _run(book.reset_account(actor="test"))
    assert result["reset"] is True
    assert result["initial_capital"] == 1_000_000.0

    post = _run(book.capital_status())
    assert post["open_positions"] == 0
    assert post["closed_positions"] == 0
    assert post["available_capital"] == 1_000_000.0
    assert post["reserved_margin"] == 0

    # Archive directory exists with prior state preserved.
    archives = list((Path(book.root) / "archive").glob("*"))
    assert archives, "Expected at least one archive directory"


def test_one_sided_book_is_capped_by_net_exposure(book: CBEPaperBook):
    """A hedge-fund book cannot spend all capital on one directional sleeve."""
    rows = [_row(f"SYM{i}", "bullish", 6.5, 1000.0, sector=f"SECTOR{i}") for i in range(11)]
    summary = _run(book.sync_from_scan(_scan_payload(rows)))
    # 4 positions x 100k = 40% net-long exposure, the configured max.
    assert summary["open_positions"] == 4
    assert summary["long_positions"] == 4
    assert summary["short_positions"] == 0
    assert summary["net_exposure"] == 400_000.0
    assert summary["net_exposure_ratio"] == HEDGE_MAX_NET_EXPOSURE_RATIO
    assert summary["available_capital"] == 600_000.0


def test_sector_concentration_caps_same_sector(book: CBEPaperBook):
    rows = [_row(f"SYM{i}", "bullish", 6.5, 1000.0, sector="BANKS") for i in range(11)]
    summary = _run(book.sync_from_scan(_scan_payload(rows)))
    # Sector cap is 30% of equity budget, so the fourth 100k BANKS name is skipped.
    assert summary["open_positions"] == 3
    assert summary["sector_exposures"][0]["sector"] == "BANKS"
    assert summary["sector_exposures"][0]["gross_exposure_ratio"] == HEDGE_MAX_SECTOR_EXPOSURE_RATIO


def test_balanced_long_short_book_can_use_full_gross_budget(book: CBEPaperBook):
    rows = [
        _row(f"L{i}", "bullish", 6.5, 1000.0, sector=f"LONG{i}")
        if i % 2 == 0
        else _row(f"S{i}", "bearish", 6.5, 1000.0, sector=f"SHORT{i}")
        for i in range(12)
    ]
    summary = _run(book.sync_from_scan(_scan_payload(rows)))
    assert summary["open_positions"] == 10
    assert summary["long_positions"] == 5
    assert summary["short_positions"] == 5
    assert summary["gross_exposure_ratio"] == HEDGE_MAX_GROSS_EXPOSURE_RATIO
    assert summary["net_exposure_ratio"] == 0.0
    assert summary["available_capital"] == 0.0


def test_invalid_latest_close_is_skipped(book: CBEPaperBook):
    rows = [
        _row("BAD1", "bullish", 6.5, 0.0),  # zero
        _row("BAD2", "bullish", 6.5, -10.0),  # negative
        {  # missing latest_close
            "instrument": "BAD3",
            "composite_score": 6.5,
            "directional_bias": "bullish",
            "bias_conviction": 0.6,
        },
        _row("GOOD", "bullish", 6.5, 500.0),  # this one opens
    ]
    summary = _run(book.sync_from_scan(_scan_payload(rows)))
    assert summary["open_positions"] == 1


def test_summary_after_close_includes_realized_pnl(book: CBEPaperBook):
    """After backdating the open so min-hold elapses, a bias flip closes
    and realized_pnl shows up in the summary."""
    from datetime import datetime, timedelta, timezone
    import json

    _run(book.sync_from_scan(_scan_payload([_row("RELIANCE", "bullish", 6.5, 2500.0)])))
    state = json.loads(book.positions_path.read_text())
    state["open_positions"][0]["opened_at"] = (
        datetime.now(timezone.utc) - timedelta(days=MIN_HOLD_TRADING_DAYS * 7 // 5 + 1)
    ).isoformat()
    book.positions_path.write_text(json.dumps(state))

    _run(book.sync_from_scan(_scan_payload([_row("RELIANCE", "bearish", 6.5, 2600.0)])))
    summary = _run(book.capital_status())
    # Long opened at 2500, closed at 2600 (40 shares) → +4000 realized.
    assert summary["realized_pnl"] == 4000.0
    assert summary["total_trades"] == 1
    assert summary["win_rate"] == 1.0
