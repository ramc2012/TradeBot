"""Regressions for corrected inputs, chronology and durable paper state."""
import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest
from auction_intelligence.schemas import MarketBar, SessionContext
from auction_intelligence.market_profile.engine import MarketProfileEngine
from auction_intelligence.options.mapper import OptionStrategyMapper
from auction_intelligence.paper.book import PaperPositionBook
from mp_core.cache import cached_json


def bars():
    start = datetime(2026, 9, 4, 9, 15)
    return [MarketBar(start + timedelta(minutes=30*i), 100, 104+i, 98, 101, 100) for i in range(4)]


def test_corrected_high_and_prior_value_invalidate_cache():
    engine = MarketProfileEngine({"tick_size": 1})
    original = bars()
    first = engine.build_profile("TEST", original)
    fixed = [replace(original[0], high=120), *original[1:]]
    second = engine.build_profile("TEST", fixed)
    assert second.high_price == 120
    assert first.high_price == 107
    prior = replace(first, vah=103, val=99)
    a = engine.build_profile("TEST", original, prior)
    b = engine.build_profile("TEST", original, replace(prior, vah=109, val=96))
    assert a.value_area_overlap != b.value_area_overlap
    second.tpo_counts.clear()
    assert engine.build_profile("TEST", fixed).tpo_counts


def test_initial_balance_config_is_part_of_identity():
    a = MarketProfileEngine({"tick_size": 1, "initial_balance_periods": 1}).build_profile("TEST", bars())
    b = MarketProfileEngine({"tick_size": 1, "initial_balance_periods": 2}).build_profile("TEST", bars())
    assert a.initial_balance_high == 104
    assert b.initial_balance_high == 105


def test_session_anchor_does_not_split_first_half_hour():
    start = datetime(2026, 9, 4, 9, 15)
    rows = [MarketBar(start + timedelta(minutes=i), 100, 101, 99, 100, 1) for i in range(60)]
    p = MarketProfileEngine({"tick_size": 1}).build_profile("MINUTES", rows)
    assert p.period_count == 2


def test_concurrent_identical_work_runs_once():
    count = []
    def run(_):
        return cached_json("test-coalesce", [id(count)], lambda: count.append(1) or {"v": 1})
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert all(x == {"v": 1} for x in pool.map(run, range(12)))
    assert len(count) == 1


def test_expiry_refuses_shorter_holding_window():
    mapper = OptionStrategyMapper()
    session = SessionContext("NIFTY", date(2026, 9, 4), 24000, minutes_to_close=10)
    assert mapper._select_expiry(expiries=[date(2026, 9, 4)], session=session, agent_name="swing") is None


def test_unknown_stale_future_and_naive_quotes_refused():
    now = datetime.now(timezone.utc)
    assert OptionStrategyMapper._fresh_quote(now.isoformat())
    for stamp in (None, "bad", now.replace(tzinfo=None).isoformat(), (now - timedelta(minutes=5)).isoformat(), (now + timedelta(minutes=5)).isoformat()):
        assert not OptionStrategyMapper._fresh_quote(stamp)


def test_corrupt_ledger_is_not_an_empty_account(tmp_path):
    book = PaperPositionBook(tmp_path)
    book.path.write_text('{broken')
    with pytest.raises(RuntimeError, match="refusing to reset"):
        asyncio.run(book._load_state())
    assert book.path.read_text() == '{broken'


def test_atomic_paper_state_preserves_entire_history(tmp_path):
    book = PaperPositionBook(tmp_path)
    state = {"open_positions": [{"id": "held"}], "closed_positions": [{"id": n} for n in range(400)]}
    asyncio.run(book._save_state(state))
    assert asyncio.run(book._load_state()) == state
    assert not list(tmp_path.glob('.paper-*.json'))


def test_master_membership_uses_equity_identity_and_current_derivatives():
    from market_data.fno_membership import active_stocks
    expiry = int(datetime(2026, 9, 29, tzinfo=timezone.utc).timestamp() * 1000)
    master = [
        {"segment": "NSE_EQ", "instrument_type": "D1", "trading_symbol": "STOCK", "instrument_key": "debt"},
        {"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "STOCK", "instrument_key": "equity"},
        {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_symbol": "STOCK", "lot_size": 500, "expiry": expiry},
        {"segment": "NSE_FO", "instrument_type": "FUT", "underlying_symbol": "INDEX", "lot_size": 50, "expiry": expiry},
    ]
    assert active_stocks(master, date(2026, 9, 6)) == [{"symbol": "STOCK", "key": "equity", "lot": 500}]
    assert active_stocks(master, date(2026, 10, 1)) == []


def test_same_effective_profile_settings_share_result(monkeypatch):
    from mp_core import cache
    cache._LOCAL.clear()
    a = MarketProfileEngine({"tick_size": 0.5})
    b = MarketProfileEngine({"tick_size": 0.5, "period_minutes": 30, "initial_balance_periods": 2})
    original = MarketProfileEngine._build_profile
    calls = []
    def counted(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(MarketProfileEngine, "_build_profile", counted)
    a.build_profile("SAME", bars())
    b.build_profile("SAME", bars())
    assert len(calls) == 1


def test_no_trade_volume_is_created_from_index_prices():
    from auction_intelligence.live import _infer_trade_prints, _build_trade_prints_from_ticks
    stamp = datetime.now(timezone.utc).isoformat()
    assert _infer_trade_prints([{"time": stamp, "open": 100, "close": 102, "volume": 0}]) == []
    ticks = [{"timestamp": stamp, "ltp": 100, "bid": 99, "ask": 101, "volume": 0},
             {"timestamp": stamp, "ltp": 102, "bid": 101, "ask": 103, "volume": 0}]
    assert _build_trade_prints_from_ticks(ticks, tick_size=0.5) == []
