import asyncio,sqlite3
from types import SimpleNamespace as NS
import pytest
from app.telegram_budget import ApiDeferred
from tests.weighted_budget import WeightedBudget
from tests.virtual_budget_load import virtual_run,run


def test_weighted_admission_spacing_cancellation_and_denial():
    async def exercise():
        db=sqlite3.connect(':memory:');clock=asyncio.get_running_loop().time;b=WeightedBudget(db,clock=clock,wall=clock,verify_weight=3);calls=[]
        async def record(group):calls.append((clock(),group));await asyncio.sleep(.2);return NS(id=1)
        tasks=[]
        try:
            for i in range(15):
                tasks.append(asyncio.create_task(b.call(record,'verify',metric='verify')));tasks.append(asyncio.create_task(b.call(record,'send',metric='send')))
            await asyncio.sleep(.001);tasks[-1].cancel()
            result=await asyncio.gather(*tasks,return_exceptions=True);assert isinstance(result[-1],asyncio.CancelledError)
            assert all(not isinstance(x,BaseException) for x in result[:-1]);assert len(calls)==29
            assert all(y[0]-x[0]>=.05-1e-7 for x,y in zip(calls,calls[1:]));assert set(g for _,g in calls[:8])=={'verify','send'}
            with pytest.raises(ApiDeferred):await b.call(record,'send',metric='send',before=lambda:False)
            b.defer(5)
            with pytest.raises(ApiDeferred):await b.call(record,'verify',metric='verify')
            assert len(calls)==29 and not b.busy
            await asyncio.sleep(5);await b.call(record,'send',metric='send');assert len(calls)==30
        finally:
            for t in tasks:t.cancel()
            await asyncio.gather(*tasks,return_exceptions=True);db.close()
    virtual_run(exercise())


@pytest.mark.parametrize('tenants,rate,latency',[(32,8,.15),(64,3,.35)])
def test_balanced_candidate_bounded_queue(tenants,rate,latency):
    result=run(tenants,rate,latency=latency,scheduler='balanced',concurrency=8,send_concurrency=4)
    assert result['warmup']=='PASS' and result['drained'] and result['min_ready']==tenants
    assert result['pending_at_120s']<20 and result['p95_to_api_start_s']<3
    assert 2<=result['peak_concurrent_sends']<=4


def test_balanced_cooldown_and_overload_remain_safe():
    fault=run(64,3,True,scheduler='balanced',concurrency=8,send_concurrency=4)
    assert fault['drained'] and fault['sent']==360 and fault['held_tenant_fraction']>0
    overload=run(64,8,latency=.04,scheduler='balanced',concurrency=8,send_concurrency=4)
    assert overload['pending_at_120s']>200 and overload['terminal'].get('UNKNOWN',0)==0


def test_64_sustained_envelope_and_overload():
    moderate=run(64,3,latency=.35,scheduler='balanced',concurrency=8,send_concurrency=4,duration=240)
    assert moderate['min_ready']==64 and moderate['sent']==720 and moderate['min_lease_margin_s']>=2 and moderate['pending_at_end']<20
    excessive=run(64,5,latency=.35,scheduler='balanced',concurrency=8,send_concurrency=4,duration=240)
    assert excessive['pending_at_end']>50 and excessive['terminal'].get('UNKNOWN',0)==0
