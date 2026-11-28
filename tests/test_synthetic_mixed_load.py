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
    extended=schedule(64,2);assert len(extended)==32 and extended[-1][0]==164
    assert Counter(i for _,pairs in extended for i,j in pairs)=={i:4 for i in range(64)}
    assert len({(i,j) for _,pairs in extended for i,j in pairs})==256
    effects=[[f'mixed-{d}-{i}-{j}',.5,at+.5] for at,pairs in extended for i,j in pairs for d in ('in','out')]
    checks={str(i):[10,35,60,125,150] for i in range(64)};samples=[{'at':t,'pending_in':0,'pending_out':0,'ready':64} for t in (34,45,125,135,165)]
    assert summarize(64,effects,checks,samples,cycles=2)['messages']==512
    with pytest.raises(ValueError):summarize(64,effects,checks,samples[:2],cycles=2)
    with pytest.raises(ValueError):schedule(64,3)


def test_phase_anchor_requires_all_four_fresh_checks():
    from tests.synthetic_mixed_load import phase_anchor_ready
    assert phase_anchor_ready(4,{0:20,1:21,2:22,3:23},24)
    assert not phase_anchor_ready(4,{0:19.999,1:21,2:22,3:23},24)
    assert not phase_anchor_ready(4,{0:23,1:23,2:23},24)
    assert not phase_anchor_ready(4,{0:23,1:23,2:23,3:25},24)
    assert not phase_anchor_ready(4,{0:23,1:23,2:23,3:float('nan')},24)
    assert not phase_anchor_ready(32,{i:23 for i in range(32)},24)


def test_existing_53_second_gate_not_relaxed():
    effects=[[f'mixed-{d}-{i}-{j}',.5,at+.5] for at,pairs in schedule(4) for i,j in pairs for d in ('in','out')]
    verified={str(i):[10,31.9,53.058,74] for i in range(4)}
    samples=[{'at':at,'pending_in':0,'pending_out':0,'ready':4} for at in (34,45)]
    with pytest.raises(ValueError,match='Missing repeated forum checks'):
        summarize(4,effects,verified,samples)


def test_hour_soak_preserves_all_forty_idle_gates():
    waves=schedule(4,40)
    assert len(waves)==480 and waves[-1][0]==3580
    effects=[[f'mixed-{d}-{i}-{j}',.5,at+.5] for at,pairs in waves for i,j in pairs for d in ('in','out')]
    assert len(effects)==4160 and len({r[0] for r in effects})==4160
    verified={str(i):[35+90*c for c in range(40)] for i in range(4)}
    samples=[{'at':at+90*c,'pending_in':0,'pending_out':0,'ready':4} for c in range(40) for at in (35,45)]
    assert summarize(4,effects,verified,samples,cycles=40)['messages']==4160
    with pytest.raises(ValueError):summarize(4,effects,verified,samples[:-1],cycles=40)
    with pytest.raises(ValueError):schedule(4,39)
    with pytest.raises(ValueError):schedule(16,40)
