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


def test_rolling_checks_improve_slow_api_but_regress_high_load():
    slow=run(64,3,latency=.25,scheduler='rolling');assert slow['warmup']=='PASS' and slow['drained']
    assert slow['min_ready']==64 and slow['peak_pending']<20
    warmup=run(64,3,latency=.35,scheduler='rolling');assert warmup['warmup']=='PASS' and warmup['drained']
    assert warmup['min_ready']>=56 and warmup['sent']==360
    overloaded=run(64,8,latency=.04,scheduler='rolling')
    assert overloaded['warmup']=='PASS' and overloaded['min_ready']==64
    assert overloaded['pending_at_120s']>200
    assert overloaded['terminal'].get('UNKNOWN',0)==0
    if not overloaded['drained']:assert overloaded['p95_to_api_start_s'] is None


def test_rolling_cooldown_and_smaller_cohort():
    cooldown=run(64,3,True,scheduler='rolling');assert cooldown['drained'] and cooldown['sent']==360 and cooldown['held_tenant_fraction']>0
    smaller=run(48,3,latency=.35,scheduler='rolling');assert smaller['min_ready']==48 and smaller['drained']
    assert smaller['pending_at_120s']>0 # serial 350ms send ceiling is below offered 3/s
