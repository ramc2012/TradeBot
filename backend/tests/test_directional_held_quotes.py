import json
import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from market_data.data_router import DataRouter

module = importlib.import_module('market_data.data_router')


@pytest.mark.asyncio
async def test_shared_quote_preserves_newest_observation_time(monkeypatch):
    symbol = 'NSE:ITC26SEP265CE'
    now = datetime.now(timezone.utc)
    router = DataRouter()
    router._tick_buffer[symbol] = SimpleNamespace(symbol=symbol, ltp=2.5, timestamp=now-timedelta(seconds=50))
    stamp = (now-timedelta(seconds=10)).isoformat()
    class Redis:
        async def get(self, key):
            assert key == f'tick:{symbol}'
            return json.dumps({'symbol': symbol, 'ltp': 2.6, 'timestamp': stamp})
    async def redis(): return Redis()
    monkeypatch.setattr(module, 'get_redis', redis)
    quote = await router.get_live_quote(symbol)
    assert quote == {'premium': 2.6, 'mark_time': stamp, 'price_source': 'shared_live_tick'}


@pytest.mark.asyncio
@pytest.mark.parametrize('age', [121, -30])
async def test_shared_quote_rejects_stale_and_future_ticks(monkeypatch, age):
    class Redis:
        async def get(self, key):
            return json.dumps({'ltp': 10, 'timestamp': (datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat()})
    async def redis(): return Redis()
    monkeypatch.setattr(module, 'get_redis', redis)
    assert await DataRouter().get_live_quote('NSE:ITC26SEP265CE') is None


@pytest.mark.asyncio
async def test_directional_held_contract_uses_shared_tick_without_atm_board(monkeypatch):
    from directional_options.service import DirectionalOptionsService
    service = object.__new__(DirectionalOptionsService)
    service.config = {'universe': ['NIFTY', 'BANKNIFTY', 'SENSEX']}
    stamp = datetime.now(timezone.utc).isoformat()
    async def quote(symbol, **kwargs):
        return {'premium': 180, 'mark_time': stamp, 'price_source': 'shared_live_tick'} if symbol == 'NSE:MARUTI26SEP12600PE' else None
    monkeypatch.setattr(module.data_router, 'get_live_quote', quote)
    result = await service.resolve_position_mark({'underlying': 'MARUTI', 'expiry': '2026-09-29',
        'strike': 12600, 'option_type': 'PE', 'instrument_key': 'NSE_FO|126424'})
    assert result['premium'] == 180 and result['mark_time'] == stamp


@pytest.mark.asyncio
async def test_shared_subscription_refresh_includes_directional_held_legs(monkeypatch):
    from market_data import option_subscription_manager as manager
    from market_data import live_marks
    async def none(): return []
    async def held(): return [{'underlying': 'MARUTI', 'expiry': '2026-09-29', 'strike': 12600,
        'option_type': 'PE', 'instrument_key': 'NSE_FO|126424', 'trading_symbol': 'MARUTI 12600 PE'}]
    async def resolve(**kwargs): return 'NSE:MARUTI26SEP12600PE'
    monkeypatch.setattr(manager, '_open_nse_option_positions', lambda: [])
    monkeypatch.setattr(manager, '_strategy1_watchlist_legs', none)
    monkeypatch.setattr(manager, '_vanguard_swing_watchlist_legs', none)
    monkeypatch.setattr(manager, '_directional_held_legs', held)
    monkeypatch.setattr(manager, '_resolve_held_option_app_symbol', resolve)
    monkeypatch.setattr(manager, '_is_enabled', lambda: False)
    monkeypatch.setattr(live_marks, '_APP_SYMBOL_BY_POSITION', {})
    result = await manager.refresh_held_position_subscriptions()
    assert result['watchlist_resolved'] == 1
    assert live_marks.registered_app_symbol('NSE_FO|126424') == 'NSE:MARUTI26SEP12600PE'
