from datetime import datetime, timedelta
from unittest.mock import AsyncMock
import pytest
from market_data import stock_spot_sweeper as sweeper


def rows(start='2026-09-07T09:15:00+05:30', count=10):
    stamp = datetime.fromisoformat(start)
    return [{'time': stamp + timedelta(minutes=3*i), 'open': 100+i,
             'high': 102+i, 'low': 99+i, 'close': 101+i, 'volume': 10}
            for i in range(count)]


def test_aggregate_does_not_emit_forming_or_gapped_bar():
    data = rows()
    assert sweeper.aggregate_stock_30minute(data, datetime.fromisoformat('2026-09-07T09:44:59+05:30')) == []
    assert sweeper.aggregate_stock_30minute(data[:4]+data[5:], datetime.fromisoformat('2026-09-07T09:45:00+05:30')) == []
    bar, = sweeper.aggregate_stock_30minute(data, datetime.fromisoformat('2026-09-07T09:45:00+05:30'))
    assert (bar['open'], bar['high'], bar['low'], bar['close'], bar['volume']) == (100,111,99,110,100)


def test_short_final_period_and_complete_input_filter():
    data = rows('2026-09-07T15:15:00+05:30', 5)
    before = datetime.fromisoformat('2026-09-07T15:29:59+05:30')
    assert len(sweeper.completed_rows(data, '3minute', before)) == 4
    assert sweeper.aggregate_stock_30minute(data, before) == []
    assert len(sweeper.aggregate_stock_30minute(data, before+timedelta(seconds=1))) == 1


@pytest.mark.asyncio
async def test_one_fetch_supplies_both_consumers(monkeypatch):
    monkeypatch.setattr(sweeper, '_stock_universe', AsyncMock(return_value=[('TEST','key')]))
    monkeypatch.setattr(sweeper, '_coverage', AsyncMock(return_value=1))
    fetch = AsyncMock(return_value=rows())
    monkeypatch.setattr(sweeper, '_fetch_window', fetch)
    monkeypatch.setattr(sweeper, '_normalize', lambda raw: raw)
    put = AsyncMock(return_value=10)
    monkeypatch.setattr(sweeper, '_upsert', put)
    monkeypatch.setattr(sweeper.settings, 'STOCK_SPOT_SWEEP_SLEEP_SECONDS', 0)
    monkeypatch.setattr(sweeper.settings, 'STOCK_SPOT_SWEEP_ENABLED', True)
    result = await sweeper.sweep_stock_spot(intervals=['3minute','30minute'],days=1,derive_30minute=True)
    assert fetch.await_count == 1
    assert [call.args[2] for call in put.await_args_list] == ['3minute','30minute']
    assert result['intervals']['3minute']['derived_30minute_rows'] == 10

@pytest.mark.asyncio
async def test_partial_sweep_resumes_after_last_attempted_symbol(monkeypatch):
    monkeypatch.setattr(sweeper, '_stock_universe', AsyncMock(return_value=[('A','a'),('B','b')]))
    monkeypatch.setattr(sweeper, '_coverage', AsyncMock(return_value=0))
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(sweeper, '_fetch_window', fetch)
    monkeypatch.setattr(sweeper, '_upsert', AsyncMock(return_value=0))
    monkeypatch.setattr(sweeper.settings, 'STOCK_SPOT_SWEEP_SLEEP_SECONDS', 0)
    monkeypatch.setattr(sweeper.settings, 'STOCK_SPOT_SWEEP_ENABLED', True)
    monkeypatch.setattr(sweeper, '_INTRADAY_CURSOR', 0)
    clock = iter([0, 0, 200, 200])
    monkeypatch.setattr(sweeper, 'monotonic', lambda: next(clock))
    result = await sweeper.sweep_stock_spot(intervals=['3minute'],days=1,derive_30minute=True,deadline_seconds=170)
    assert result['status'] == 'partial'
    assert fetch.await_args.args[1] == 'a'
    assert sweeper._INTRADAY_CURSOR == 1
    monkeypatch.setattr(sweeper, 'monotonic', lambda: 0)
    await sweeper.sweep_stock_spot(intervals=['3minute'],days=1,derive_30minute=True)
    assert fetch.await_args_list[1].args[1] == 'b'


def test_postclose_sweep_keeps_short_last_30minute_bar():
    data = rows('2026-09-07T15:15:00+05:30', 1)
    assert sweeper.completed_rows(data, '30minute', datetime.fromisoformat('2026-09-07T15:35:00+05:30')) == data
    assert sweeper.completed_rows(data, '30minute', datetime.fromisoformat('2026-09-07T15:29:00+05:30')) == []
