"""Guards the frozen-feed detector (2026-06-05).

The dead-feed watchdog catches a feed that STOPS ticking (timestamp age grows).
It cannot see a feed that keeps ticking but repeats the same value — a stuck WS
session. An NSE/BSE index recomputes continuously from its constituents during
market hours, so an LTP byte-identical for >90s while ticks keep arriving is a
frozen feed, not a quiet market. The DataQualityAgent now tracks when the value
last changed and rolls a frozen required index into "critical" so the NSE lane's
critical-gate skips entries (fail-safe) instead of trading on a stale price.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import market_data.data_quality_agent as dqa_mod
from market_data.data_quality_agent import DataQualityAgent

IST = timezone(timedelta(hours=5, minutes=30))


def _mkt_hours_base() -> datetime:
    # 11:00 IST on a Thursday → NSE open, in UTC.
    return datetime(2026, 6, 4, 11, 0, 0, tzinfo=IST).astimezone(timezone.utc)


def _feed(agent: DataQualityAgent, symbol: str, source: str, value: float, at: datetime) -> None:
    agent.record_tick(symbol=symbol, source=source, observed_at=at, last_value=value)


def test_moving_index_is_not_frozen():
    """A normally-moving index stays healthy."""
    agent = DataQualityAgent()
    base = _mkt_hours_base()
    for i in range(40):
        _feed(agent, "NSE:NIFTY50-INDEX", "fyers_tick", 23400.0 + i, base + timedelta(seconds=2 * i))
    now = base + timedelta(seconds=80)
    v = agent.assess_freshness(symbol="NSE:NIFTY50-INDEX", source="fyers_tick", now=now)
    assert v.stale is False
    snap = agent.snapshot(now=now)
    assert snap["frozen_count"] == 0


def test_frozen_index_flags_stale_and_critical():
    """Ticks keep arriving on time but the value is stuck >90s → frozen → critical."""
    agent = DataQualityAgent()
    base = _mkt_hours_base()
    # Same value, every 2s, for 100s of ticks — feed alive but value frozen.
    for i in range(60):
        _feed(agent, "NSE:NIFTY50-INDEX", "fyers_tick", 23416.55, base + timedelta(seconds=2 * i))
    now = base + timedelta(seconds=2 * 59)  # last tick is fresh (age 0)
    v = agent.assess_freshness(symbol="NSE:NIFTY50-INDEX", source="fyers_tick", now=now)
    assert v.stale is True
    assert v.reason is not None and "frozen feed" in v.reason
    snap = agent.snapshot(now=now)
    assert snap["frozen_count"] == 1
    assert snap["overall"] == "critical"  # NSE lane gates on this


def test_frozen_recovers_when_value_moves():
    """Once the value moves again, frozen clears and overall returns to healthy."""
    agent = DataQualityAgent()
    base = _mkt_hours_base()
    for i in range(60):
        _feed(agent, "NSE:NIFTY50-INDEX", "fyers_tick", 23416.55, base + timedelta(seconds=2 * i))
    assert agent.snapshot(now=base + timedelta(seconds=2 * 59))["overall"] == "critical"
    # Value moves → change time resets → not frozen.
    _feed(agent, "NSE:NIFTY50-INDEX", "fyers_tick", 23418.0, base + timedelta(seconds=122))
    snap = agent.snapshot(now=base + timedelta(seconds=124))
    assert snap["frozen_count"] == 0
    assert snap["overall"] != "critical"


def test_dead_feed_is_not_double_counted_as_frozen():
    """A feed that STOPPED ticking is stale-by-age, not 'frozen' (age > budget)."""
    agent = DataQualityAgent()
    base = _mkt_hours_base()
    _feed(agent, "NSE:NIFTY50-INDEX", "fyers_tick", 23416.55, base)
    now = base + timedelta(seconds=300)  # 5 min later, no new ticks
    v = agent.assess_freshness(symbol="NSE:NIFTY50-INDEX", source="fyers_tick", now=now)
    assert v.stale is True  # stale by age
    snap = agent.snapshot(now=now)  # entry is old by age → not frozen
    frozen = [e for e in snap["entries"] if e.get("frozen")]
    assert frozen == []


def test_non_index_symbol_never_frozen():
    """MCX futures can legitimately sit flat in low liquidity — not frozen."""
    agent = DataQualityAgent()
    base = _mkt_hours_base()
    for i in range(60):
        _feed(agent, "MCX:CRUDEOIL26JUNFUT", "mcx_tick", 8929.0, base + timedelta(seconds=2 * i))
    now = base + timedelta(seconds=2 * 59)
    v = agent.assess_freshness(symbol="MCX:CRUDEOIL26JUNFUT", source="mcx_tick", now=now)
    assert v.stale is False
    assert agent.snapshot(now=now)["frozen_count"] == 0


def test_off_hours_flat_index_not_frozen(monkeypatch):
    """Pre-market flat index (the phantom carry-forward bars) must NOT flag —
    frozen detection only applies during NSE market hours."""
    agent = DataQualityAgent()
    # 07:00 IST — pre-market.
    base = datetime(2026, 6, 4, 7, 0, 0, tzinfo=IST).astimezone(timezone.utc)
    for i in range(60):
        _feed(agent, "NSE:NIFTY50-INDEX", "fyers_tick", 23416.55, base + timedelta(seconds=2 * i))
    now = base + timedelta(seconds=2 * 59)
    v = agent.assess_freshness(symbol="NSE:NIFTY50-INDEX", source="fyers_tick", now=now)
    assert v.stale is False  # off-hours flat is expected, not a fault
    assert agent.snapshot()["frozen_count"] == 0


def test_static_option_contract_never_frozen():
    """Regression (2026-06-10): deep-OTM option premiums legitimately sit
    byte-identical for minutes while ticks keep arriving. They match the broad
    NSE: prefix, so the old index matcher counted them as frozen — six static
    option feeds rolled the aggregate to critical and the S1 gate blocked every
    scan from 14:27 IST to the close. Frozen detection must apply ONLY to
    index spot symbols (NSE:/BSE: + -INDEX suffix)."""
    agent = DataQualityAgent()
    base = _mkt_hours_base()
    for sym in ("NSE:ASHOKLEY26JUN145PE", "NSE:NIFTY26JUN23400CE"):
        for i in range(60):
            _feed(agent, sym, "fyers_tick", 0.05, base + timedelta(seconds=2 * i))
    now = base + timedelta(seconds=2 * 59)
    for sym in ("NSE:ASHOKLEY26JUN145PE", "NSE:NIFTY26JUN23400CE"):
        v = agent.assess_freshness(symbol=sym, source="fyers_tick", now=now)
        assert v.stale is False, f"{sym} wrongly stale: {v.reason}"
    snap = agent.snapshot(now=now)
    assert snap["frozen_count"] == 0
    assert snap["overall"] != "critical"


def test_frozen_index_still_critical_alongside_static_options():
    """The gate stays armed for the real failure: a frozen INDEX feed must
    still roll the aggregate to critical even while static (healthy) option
    feeds coexist in the ledger."""
    agent = DataQualityAgent()
    base = _mkt_hours_base()
    for i in range(60):
        _feed(agent, "NSE:ASHOKLEY26JUN145PE", "fyers_tick", 0.05, base + timedelta(seconds=2 * i))
        _feed(agent, "NSE:NIFTY50-INDEX", "fyers_tick", 23416.55, base + timedelta(seconds=2 * i))
    now = base + timedelta(seconds=2 * 59)
    snap = agent.snapshot(now=now)
    assert snap["frozen_count"] == 1  # the index, not the option
    assert snap["overall"] == "critical"


def test_fyers_chain_drops_no_arb_zombie_strike_rows():
    """Regression (2026-06-11, INDIANB): post-corporate-action zombie strikes
    come back from Fyers with garbage LTPs (PE 820 @ 1298.8 — a put can never
    exceed its strike; a call can never exceed spot). One row marked an S1
    position 49× and booked a ₹19L phantom exit. get_option_chain must drop
    no-arb rows at ingest."""
    import asyncio

    from brokers.fyers import FyersAdapter

    adapter = FyersAdapter.__new__(FyersAdapter)  # no auth needed for parsing

    async def fake_get_data_json(_path, _params):
        return {
            "data": {
                "expiryData": [{"expiry": "1782819000"}],  # 2026-06-30 epoch-ish
                "optionsChain": [
                    {"option_type": "", "strike_price": -1, "ltp": 821.8, "fp": 821.8},
                    {"option_type": "PE", "strike_price": 820, "ltp": 1298.8, "oi": 0, "symbol": "NSE:X820PE"},
                    {"option_type": "CE", "strike_price": 820, "ltp": 900.0, "oi": 0, "symbol": "NSE:X820CE"},
                    {"option_type": "PE", "strike_price": 821.75, "ltp": 26.25, "oi": 1000, "symbol": "NSE:X821.75PE"},
                    {"option_type": "CE", "strike_price": 821.75, "ltp": 25.0, "oi": 1000, "symbol": "NSE:X821.75CE"},
                ],
            }
        }

    adapter._get_data_json = fake_get_data_json  # type: ignore[attr-defined]
    chain = asyncio.run(FyersAdapter.get_option_chain(adapter, "NSE:INDIANB-EQ", ""))
    pairs = {(e.strike, e.option_type): e.ltp for e in chain.entries}
    assert (820.0, "PE") not in pairs  # 1298.8 > strike — dropped
    assert (820.0, "CE") not in pairs  # 900 > spot 821.8×1.05 — dropped
    assert pairs[(821.75, "PE")] == 26.25
    assert pairs[(821.75, "CE")] == 25.0
