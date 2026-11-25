"""Virtual-time regression: characterize capacity without weakening authorization."""
from tests.virtual_budget_load import run


def test_64_forums_fast_api_and_cooldown():
    normal=run(64,3)
    assert normal['warmup']=='PASS' and normal['sent']==normal['offered']==360
    assert normal['min_ready']==64 and normal['peak_pending']<20
    fault=run(64,3,True)
    assert fault['sent']==360 and fault['terminal']=={'SENT':360}
    assert fault['min_ready']<64 and fault['held_tenant_fraction']>0
    assert fault['cooldown_s']==12 and fault['pending_at_120s']<20


def test_64_forums_slow_api_exposes_capacity_boundary():
    overloaded=run(64,8,latency=.15)
    assert overloaded['warmup']=='PASS' and overloaded['sent']==960
    assert overloaded['pending_at_120s']>100 and overloaded['held_tenant_fraction']>.1
    assert overloaded['terminal']=={'SENT':960}
    slow=run(64,3,latency=.35)
    assert slow['warmup']=='FAIL' and slow['ready_at_deadline']<64 and slow['sent']==0
