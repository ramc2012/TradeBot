"""Behavioral regressions for the September 5 paper-lane review."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from model.swing_marks import summarize_path
from model.return_calibration import calibrate_returns, expected_net_return
from model.preclose_swing import resolve_chain_bar
from vanguard.tests.test_preclose_swing_layers import _ChainConnection
from fusion.m7_risk import event_guard_blocks

IST = ZoneInfo('Asia/Kolkata')
D = date(2026, 9, 3)
def stamp(day, hour=14, minute=45): return datetime.combine(day, datetime.min.time(), IST).replace(hour=hour, minute=minute)
def row(day, close, high=None, low=None):
    return dict(time=stamp(day), interval='30minute', close=close, high=high or close, low=low or close)
def item(horizon=1, **kwargs):
    return dict(source_session=D, horizon_sessions=horizon, decision_at=stamp(D,14,55), cost_pct=.01, **kwargs)

def test_each_contract_settles_on_its_selected_horizon():
    sessions=[date(2026,9,4),date(2026,9,7)]
    path=[row(D,100,900,1),row(sessions[0],110),row(sessions[1],80)]
    first=summarize_path(item(),path,sessions,stamp(sessions[1],16))
    second=summarize_path(item(2),path,sessions,stamp(sessions[1],16))
    assert first['status']==second['status']=='closed'
    assert first['return_pct']==pytest.approx(.1)
    assert first['net_return_pct']==pytest.approx(.09)
    assert first['max_return_pct']==pytest.approx(.1)  # excludes pre-entry spike
    assert first['min_return_pct']==0
    assert second['return_pct']==pytest.approx(-.2)
    assert 2 not in first['day_marks']

def test_entry_is_frozen_and_incomplete_candles_are_excluded():
    path=[row(D,200),row(date(2026,9,4),300)]
    result=summarize_path(item(entry_ts=stamp(D),entry_mark=100),path,[date(2026,9,4)],stamp(date(2026,9,4),15,0))
    assert result['entry_mark']==100
    assert result['latest_mark']==100
    assert result['status']=='tracking'

def test_missing_planned_entry_never_uses_a_later_close():
    assert summarize_path(item(),[row(date(2026,9,4),120)],[date(2026,9,4)],stamp(date(2026,9,4),16)) is None

def test_a_late_decision_cannot_claim_an_earlier_paper_fill():
    late=item();late['decision_at']=stamp(D,15,15)
    assert summarize_path(late,[row(D,100)],[],stamp(D,16)) is None

def test_yesterdays_chain_cannot_replace_missing_current_data():
    ts=stamp(D,14,15)
    assert resolve_chain_bar(_ChainConnection([(ts-timedelta(days=1),200)]),ts,['A']*200) is None

def test_rank_without_net_payoff_calibration_refuses():
    assert expected_net_return(None,100,1)['expected_net_lower'] is None
    scores=list(range(100));sessions=[str(i//5) for i in scores]
    cal=calibrate_returns(scores,[.1]*100,sessions,[1]*100,bins=1)
    assert expected_net_return(cal,50,1)['expected_net_lower']==pytest.approx(.1)
    assert expected_net_return(cal,101,1)['expected_net_lower'] is None
    sparse=calibrate_returns(scores,[.1]*100,['one']*100,[1]*100,bins=1)
    assert expected_net_return(sparse,50,1)['expected_net_lower'] is None

class Cursor:
    def __init__(self,fresh,event):self.fresh,self.event=fresh,event
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,sql,params):self.result=self.fresh if 'ingest_log' in sql else self.event
    def fetchone(self):return (self.result,) if self.result else None
class Connection:
    def __init__(self,fresh,event=None):self.fresh,self.event=fresh,event
    def cursor(self):return Cursor(self.fresh,self.event)

def test_event_calendar_fails_closed_when_missing_or_stale():
    now=stamp(D,14,55)
    for fresh in (None, now-timedelta(days=4)):
        assert 'unavailable or stale' in event_guard_blocks(Connection(fresh),'TCS',now)
    assert event_guard_blocks(Connection(now),'TCS',now) is None

def test_event_guard_uses_the_exchange_holiday_calendar():
    # Monday September 14 is configured closed; Tuesday reports block Friday.
    now=stamp(date(2026,9,11),14,55)
    assert event_guard_blocks(Connection(now,date(2026,9,15)),'TCS',now)

def test_history_rate_limit_retry_keeps_the_same_request(monkeypatch):
    import httpx
    from ingest import futures_oi
    calls=[]
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(429,headers={'Retry-After':'0'},json={}) if len(calls)==1 else httpx.Response(200,json={'data':{'candles':[]}})
    monkeypatch.setattr(futures_oi,'_throttle',lambda:None)
    monkeypatch.setattr(futures_oi,'_window_throttle',lambda url:None)
    monkeypatch.setattr(futures_oi.time,'sleep',lambda seconds:None)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert futures_oi._get_json(client,'https://api.upstox.com/v2/test')['data']['candles']==[]
    assert len(calls)==2 and calls[0]==calls[1]


def test_history_throttle_honours_the_long_window(monkeypatch):
    from ingest import futures_oi
    now=[0.]
    monkeypatch.setattr(futures_oi,'RATE_WINDOW_CALLS',2)
    monkeypatch.setattr(futures_oi,'RATE_WINDOW_SECONDS',10)
    monkeypatch.setattr(futures_oi.time,'monotonic',lambda:now[0])
    monkeypatch.setattr(futures_oi.time,'sleep',lambda delay:now.__setitem__(0,now[0]+delay))
    futures_oi._api_calls.clear()
    for _ in range(3):futures_oi._window_throttle('https://api.upstox.com/v2/test')
    assert now[0]>=10
    futures_oi._api_calls.clear()


def test_contract_expiry_must_cover_the_actual_second_nse_session():
    from model.preclose_swing import _business_day
    assert _business_day(date(2026,9,11),2)==date(2026,9,16)


def test_futures_discovery_never_uses_a_same_symbol_debt_security():
    from ingest.futures_oi import underlying_keys_for_discovery
    rows=[dict(segment='NSE_EQ',trading_symbol='MOTHERSON',instrument_type='D1',instrument_key='debt'),
          dict(segment='NSE_EQ',trading_symbol='MOTHERSON',instrument_type='EQ',instrument_key='equity')]
    assert underlying_keys_for_discovery(rows,['MOTHERSON'])['MOTHERSON']=='equity'
    assert 'MOTHERSON' not in underlying_keys_for_discovery(rows[:1],['MOTHERSON'])
