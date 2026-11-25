from copy import deepcopy
import pytest
from app.load_metrics import summarize_load


def sample(mem='45MiB',cpu='2.5%',pending=0):
    return {'resources':[{'Name':'m2tg-placement-'+('d'*24)+'-'+r,'MemUsage':mem+' / 256MiB','CPUPerc':cpu} for r in ('broker','gateway','worker-'+'a'*24,'worker-'+'b'*24)],'inbox':{'RECEIVED':pending,'ACKED':2},'egress':{'QUEUED':pending,'SENT':2}}


def test_burst_measurements_and_sampled_pending():
    effects=[[f'load-{d}-{i}-0',t] for d in ('in','out') for i,t in enumerate((1.,2.))]
    result=summarize_load(1,sample(),[sample('0.05GiB','7%',3),sample()],effects,4)
    assert result['messages']==4 and result['confirmed_messages_per_second']==1
    assert result['latency_seconds_burst_to_effect']['in']=={'p50':1,'p95':2,'p100':2}
    assert result['sampled_peak_pending']=={'inbox':3,'egress':3}
    assert result['resources']['broker']=={'baseline_mib':45,'peak_mib':51.2,'peak_cpu_percent':7}
    for malformed in (effects[:-1],effects[:-1]+[effects[0]]):
        with pytest.raises(ValueError):summarize_load(1,sample(),[sample()],malformed,4)
    bad=deepcopy(sample());bad['resources'].pop()
    with pytest.raises(ValueError):summarize_load(1,sample(),[bad],effects,4)


def test_larger_cohort_requires_all_workers_and_effects():
    n=4;baseline=sample();baseline['resources'] += [dict(baseline['resources'][-1],Name='m2tg-placement-'+('d'*24)+'-worker-'+(x*24)) for x in ('c','e')]
    effects=[[f'load-{d}-{i}-0',float(i+1)] for d in ('in','out') for i in range(n)]
    result=summarize_load(1,baseline,[deepcopy(baseline)],effects,5,tenants=n)
    assert result['messages']==8 and result['tenants']==4 and len(result['resources'])==6
    with pytest.raises(ValueError):summarize_load(1,baseline,[sample()],effects,5,tenants=n)
    with pytest.raises(ValueError):summarize_load(1,baseline,[baseline],effects,5,tenants=8)
