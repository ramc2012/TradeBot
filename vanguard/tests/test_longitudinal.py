from datetime import datetime,date,timedelta,timezone
import pytest
from journal.longitudinal import summarize
UTC=timezone.utc
ENTRY=datetime(2026,9,4,9,15,tzinfo=UTC)
NOW=datetime(2026,9,9,11,tzinfo=UTC)

def item(**kw):
    return dict(entry_mark=100.,entry_ts=ENTRY,expiry=date(2026,9,29),option_type='CE',**kw)

def bar(day,close,*,hour=9,minute=15,high=None,low=None):
    return dict(time=datetime(2026,9,day,hour,minute,tzinfo=UTC),interval='30minute',close=close,high=high if high is not None else close,low=low if low is not None else close)

def test_tracks_after_closed_horizon_and_separates_underlying_move():
    r=summarize(item(status='closed'),[bar(4,100),bar(7,90),bar(8,140),bar(9,200)],
                [bar(4,1000),bar(7,1010),bar(8,1090),bar(9,1100)],NOW)
    assert r['latest']['return']==1
    assert r['latest']['spot_return']==pytest.approx(.1)
    assert r['horizons']['1']['return']==pytest.approx(-.1)
    assert r['horizons']['2']['return']==pytest.approx(.4)
    assert r['horizons']['5'] is None
    assert r['first_reach']['0.4']['session_offset']==2
    assert r['first_reach']['1']['session_offset']==3


def test_no_entry_candle_extremes_or_future_candles():
    r=summarize(item(),[bar(4,100,high=1000,low=1),bar(7,110,high=120,low=95),bar(10,500)],[],NOW)
    assert r['peak_return']==pytest.approx(.2)
    assert r['adverse_return']==pytest.approx(-.05)
    assert r['latest']['session']==date(2026,9,7)
    assert r['missing_sessions']==2
    assert r['latest']['spot_return'] is None


def test_missing_horizon_does_not_slide_to_next_available_session():
    r=summarize(item(),[bar(4,100),bar(8,150)],[],NOW)
    assert r['horizons']['1'] is None
    assert r['horizons']['2']['return']==.5
    assert r['paths'][1]['return'] is None


def test_partial_session_is_not_horizon_close_and_pe_direction_is_signed():
    i=item();i['option_type']='PE'
    r=summarize(i,[bar(4,100),bar(7,120,hour=4,minute=15)],
                [bar(4,1000),bar(7,900,hour=4,minute=15)],NOW)
    assert r['horizons']['1'] is None
    assert r['latest']['direction_return']==pytest.approx(.1)


def test_no_future_spot_used_for_entry_and_expiry_caps_followup():
    i=item();i['expiry']=date(2026,9,7)
    r=summarize(i,[bar(4,100),bar(7,0),bar(8,1000)],[bar(4,1100,hour=9,minute=45)],NOW)
    assert r['spot_entry'] is None
    assert r['latest']['return']==-1
    assert r['status']=='expiry_reached'
    assert len(r['paths'])==2


def test_unentered_observation_does_not_manufacture_zero_return():
    i=item();i['entry_mark']=None
    r=summarize(i,[bar(9,100)],[],NOW)
    assert r['status']=='awaiting_entry' and not r['paths'] and r.get('latest') is None


def test_unopened_session_is_pending_not_missing():
    r=summarize(item(),[bar(4,100),bar(7,110)],[],datetime(2026,9,8,2,tzinfo=UTC))
    assert r['paths'][-1]['data_state']=='pending'
    assert r['pending_sessions']==1 and r['missing_sessions']==0
    assert r['horizons']['2'] is None
