from datetime import datetime,timezone
import pytest
from api.routers import vanguard

@pytest.mark.asyncio
async def test_followup_keeps_models_and_missing_horizons_separate(monkeypatch):
    async def fetch(sql,params=None):
        if 'to_regclass' in sql:return [{'name':'vanguard_observation_followup'}]
        assert params=={'lane':'swing'}
        result=[]
        for model,day,val,late in [('A','2026-09-01',.5,False),('A','2026-09-01',-.1,False),('A','2026-09-02',10,True),('B','2026-09-01',-.8,False)]:
            result.append({'updated_at':datetime(2026,9,9,tzinfo=timezone.utc),'payload':{'model_version':model,'source_session':day,'qualified':False,'entry_on_time':not late,'horizons':{'1':{'return':val,'direction_return':.02},'2':None}}})
        return result
    monkeypatch.setattr(vanguard,'_fetch_all',fetch)
    result=await vanguard.observation_followup(lane='swing')
    a=next(q for q in result['quality'] if q['model_version']=='A' and q['horizon']==1)
    assert a['n']==2 and a['sessions']==1 and a['mean_return']==pytest.approx(.2)
    assert a['positive_fraction']==.5 and a['direction_hit_rate']==1
    assert all(q['n']==0 and q['mean_return'] is None for q in result['quality'] if q['horizon']==2)
    assert len(result['items'])==4 and result['paper_only']
    gate_a=next(g for g in result['promotion_gates'] if g['model_version']=='A')
    assert gate_a['status']=='hold_shadow' and gate_a['gate_passed'] is False
    assert gate_a['primary_horizons']==[1,2]
    assert any('1/20 source sessions' in reason for reason in gate_a['reasons'])
    assert any('session +2' in reason for reason in gate_a['reasons'])

@pytest.mark.asyncio
async def test_missing_table_is_explicit_not_zero_quality(monkeypatch):
    async def fetch(*args,**kwargs):return [{'name':None}]
    monkeypatch.setattr(vanguard,'_fetch_all',fetch)
    result=await vanguard.observation_followup(lane='swing')
    assert result['status']=='not_initialized' and result['quality']==[]
