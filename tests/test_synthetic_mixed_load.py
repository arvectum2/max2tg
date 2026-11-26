from collections import Counter
import pytest
from tests.synthetic_mixed_load import schedule,summarize

@pytest.mark.parametrize('tenants',[2,4,8,16,32])
def test_schedule_unique_ordered_and_bounded(tenants):
    waves=schedule(tenants);seq=Counter()
    assert [t for t,_ in waves]==sorted({t for t,_ in waves}) and waves[-1][0]==70
    for _,pairs in waves:
        for i,j in pairs:assert j==seq[i];seq[i]+=1
    assert set(seq)==set(range(tenants)) and seq[0]>min(seq.values()) and max(seq.values())<=25
    assert not any(20<=t<53 for t,_ in waves)


def test_mixed_metrics_require_exact_effects_and_idle_and_checks():
    effects=[[f'mixed-{d}-{i}-{j}',.5,at+.5] for at,pairs in schedule(4) for i,j in pairs for d in ('in','out')]
    verified={str(i):[10,35,60] for i in range(4)};samples=[{'at':at,'pending_in':0,'pending_out':0,'ready':4} for at in (34,45,70)]
    r=summarize(4,effects,verified,samples);assert r['messages']==len(effects) and r['idle_samples']==2 and r['directions']['out']['p95_s']==.5
    with pytest.raises(ValueError):summarize(4,effects[:-1]+[effects[0]],verified,samples)
    with pytest.raises(ValueError):summarize(4,effects,{'0':[10,35]},samples)
    with pytest.raises(ValueError):summarize(4,effects,verified,[dict(samples[0],pending_out=1),*samples[1:]])
    with pytest.raises(ValueError):summarize(4,effects,verified,[dict(samples[0],ready=3),*samples[1:]])


def test_32_tenant_mixed_exact_effects():
    waves=schedule(32);effects=[[f'mixed-{d}-{i}-{j}',.5,at+.5] for at,pairs in waves for i,j in pairs for d in ('in','out')]
    verified={str(i):[10,35,60] for i in range(32)};samples=[{'at':at,'pending_in':0,'pending_out':0,'ready':32} for at in (34,45,70)]
    result=summarize(32,effects,verified,samples)
    assert result['messages']==720 and result['min_ready_sample']==32
    with pytest.raises(ValueError):summarize(32,effects[:-1],verified,samples)


def test_64_paced_rehearsal_is_bounded():
    waves=schedule(64);assert len(waves)==16 and waves[-1][0]==74
    assert all(len(pairs)==8 for _,pairs in waves)
    assert Counter(i for _,pairs in waves for i,j in pairs)=={i:2 for i in range(64)}
    effects=[[f'mixed-{d}-{i}-{j}',.5,at+.5] for at,pairs in waves for i,j in pairs for d in ('in','out')]
    assert summarize(64,effects,{str(i):[10,35,60] for i in range(64)},[{'at':t,'pending_in':0,'pending_out':0,'ready':64} for t in (34,45,75)])['messages']==256
    with pytest.raises(ValueError):schedule(64,2)
