import sqlite3
from unittest.mock import AsyncMock
import pytest
from app.stage_profile import StageProfile,measure,aggregate_profiles,difference
from app.telegram_budget import TelegramBudget,ApiDeferred


def test_bounded_aggregate_and_delta():
    now=[0.0];p=StageProfile(lambda:now[0]);before=p.snapshot()
    with measure(p,'worker.route'):now[0]=.25
    with pytest.raises(RuntimeError):
        with measure(p,'worker.ingress'):now[0]=.5;raise RuntimeError('synthetic private payload')
    after=p.snapshot();assert after=={'worker.ingress':{'count':1,'seconds':.25},'worker.route':{'count':1,'seconds':.25}}
    assert difference(before,after)==after
    result=aggregate_profiles({'gateway':{},'w1':before,'w2':before},{'gateway':{},'w1':after,'w2':after});assert result['workers']['worker.route']=={'count':2,'seconds':.5,'mean_ms':250.0}
    with pytest.raises(ValueError):p.record('secret:payload',1)
    with pytest.raises(ValueError):difference(after,before)
    p.record('worker.route',float('nan'));p.record('worker.route',-1);assert p.snapshot()==after

@pytest.mark.asyncio
async def test_profile_budget_distinguishes_local_hold_and_network_call():
    now=[10.0];p=StageProfile(lambda:now[0]);db=sqlite3.connect(':memory:')
    async def sleep(s):now[0]+=s
    async def send():now[0]+=.01;return 'receipt'
    b=TelegramBudget(db,clock=lambda:now[0],wall=lambda:now[0],sleep=sleep,profile=p)
    try:
        assert await b.call(send,metric='send')=='receipt';assert await b.call(send,metric='send')=='receipt'
        blocked=AsyncMock()
        with pytest.raises(ApiDeferred):await b.call(blocked,metric='send',before=lambda:False)
        blocked.assert_not_awaited();s=p.snapshot();assert s['api.send.call']['count']==2 and s['api.send.call']['seconds']==pytest.approx(.02)
        assert s['api.send.pace']['count']==3 and s['api.send.pace']['seconds']==pytest.approx(.08)
        assert s['api.send.deferred']=={'count':1,'seconds':0.0}
    finally:db.close()
