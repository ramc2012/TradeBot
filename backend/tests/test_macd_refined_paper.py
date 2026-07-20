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


def test_macd_refined_rejects_entry_that_exceeds_available_capital(tmp_path: Path, monkeypatch) -> None:
    # With SIGNAL_VALIDATION_UNCAPPED=False the cash gate is ENFORCED (the
    # bypass path is covered in tests/test_signal_validation_uncapped.py).
    from core.config import settings

    monkeypatch.setattr(settings, "SIGNAL_VALIDATION_UNCAPPED", False)
    store = _store(tmp_path)
    proposal = _proposal()
    proposal["quantity_units"] = 300_000

    payload = store.sync_cycle(
        proposals=[proposal],
        marks={},
        now="2026-07-13T04:00:00+00:00",
    )

    assert payload["admitted_this_cycle"] == 0
    assert payload["capital_blocked_this_cycle"] == 1
    assert store.list_positions(status="open")["open_positions"] == []
    journal = store.list_journal(limit=10)["records"]
    assert journal[0]["reason"] == "insufficient_available_capital"


def test_incremental_sync_does_not_remark_positions_without_a_mark(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.sync_cycle(proposals=[_proposal()], marks={}, now="2026-07-07T09:21:18+00:00")
    before = store.list_positions(status="open")["open_positions"][0]

    store.sync_cycle(
        proposals=[],
        marks={},
        now="2026-07-13T04:00:00+00:00",
        allow_entries=False,
    )
    after = store.list_positions(status="open")["open_positions"][0]

    assert after["updated_at"] == before["updated_at"]
    assert after["latest_premium"] == before["latest_premium"]


# ══════════════════════════════════════════════════════════════════════════
# forced_expiry_roll_2td in the refined paper book (owner rule 2026-07-21)
# ══════════════════════════════════════════════════════════════════════════
def _open_one(tmp_path: Path):
    store = _store(tmp_path)
    store.sync_cycle(proposals=[_proposal()], marks={}, now="2026-07-07T09:21:18+00:00")
    return store, store.list_positions(status="open")["open_positions"][0]["position_id"]


def test_forced_close_books_the_remainder_with_its_own_reason(tmp_path: Path) -> None:
    store, pid = _open_one(tmp_path)
    payload = store.sync_cycle(
        proposals=[],
        marks={pid: {"premium": 26.0, "spot": 1050.0, "fresh": True,
                     "window_end_passed": False, "forced_close": True}},
        now="2026-07-24T09:51:22+00:00",
        allow_entries=False,
    )
    assert payload["open_positions"] == 0
    closed = store.list_positions(status="closed")["closed_positions"][0]
    assert closed["close_reason"] == "forced_expiry_roll_2td"


def test_window_end_keeps_attribution_when_both_flags_are_set(tmp_path: Path) -> None:
    """The compulsory closure is a BACKSTOP placed AFTER window_end — it must
    never re-label a closure the existing rule already owns."""
    store, pid = _open_one(tmp_path)
    store.sync_cycle(
        proposals=[],
        marks={pid: {"premium": 26.0, "spot": 1050.0, "fresh": True,
                     "window_end_passed": True, "forced_close": True}},
        now="2026-07-24T09:51:22+00:00",
        allow_entries=False,
    )
    closed = store.list_positions(status="closed")["closed_positions"][0]
    assert closed["close_reason"] == "window_end"


def test_forced_close_survives_a_stale_mark(tmp_path: Path) -> None:
    """Time-based, exactly like window_end: a frozen feed can never buy a
    physically-settled contract another day."""
    store, pid = _open_one(tmp_path)
    store.sync_cycle(
        proposals=[],
        marks={pid: {"premium": 26.0, "spot": 1050.0, "fresh": False,
                     "window_end_passed": False, "forced_close": True}},
        now="2026-07-24T09:51:22+00:00",
        allow_entries=False,
    )
    closed = store.list_positions(status="closed")["closed_positions"][0]
    assert closed["close_reason"] == "forced_expiry_roll_2td"


def test_absent_forced_close_flag_is_a_no_op(tmp_path: Path) -> None:
    """Flags OFF ⇒ live.py never puts `forced_close` in the mark ⇒ the book
    behaves byte-identically to today."""
    store, pid = _open_one(tmp_path)
    payload = store.sync_cycle(
        proposals=[],
        marks={pid: {"premium": 26.0, "spot": 1050.0, "fresh": True,
                     "window_end_passed": False}},
        now="2026-07-24T09:51:22+00:00",
        allow_entries=False,
    )
    assert payload["open_positions"] == 1


def test_live_forced_close_flag_is_off_by_default(monkeypatch) -> None:
    from datetime import date

    from macd_refined.live import _forced_close_flag

    assert _forced_close_flag("RELIANCE", "2026-07-28", date(2026, 7, 24)) is False

    from core.config import settings

    monkeypatch.setattr(settings, "EXPIRY_POLICY_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "EXPIRY_POLICY_FORCED_CLOSE_ENABLED", True, raising=False)
    assert _forced_close_flag("RELIANCE", "2026-07-28", date(2026, 7, 24)) is True
    assert _forced_close_flag("RELIANCE", "2026-07-28", date(2026, 7, 23)) is False
    # Index: separate knob, default 0 = disabled.
    assert _forced_close_flag("NIFTY", "2026-07-28", date(2026, 7, 24)) is False
    # Unparseable expiry can never crash the mark pass.
    assert _forced_close_flag("RELIANCE", "", date(2026, 7, 24)) is False
