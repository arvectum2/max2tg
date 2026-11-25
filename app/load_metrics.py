"""Summarize bounded synthetic benchmark; samples are not capacity guarantees."""
import math,re

def _mib(value):
    m=re.fullmatch(r'([0-9.]+)(B|KiB|MiB|GiB|TiB)',value.strip());
    if not m:raise ValueError('Invalid Docker memory sample')
    return float(m[1])*{'B':2**-20,'KiB':2**-10,'MiB':1,'GiB':1024,'TiB':1024**2}[m[2]]

def summarize_load(n,baseline,samples,effects,elapsed):
    if not samples or type(n) is not int or not 1<=n<=25 or not math.isfinite(elapsed) or elapsed<=0:raise ValueError('Invalid benchmark')
    expected={f'load-{d}-{i}-{j}' for d in ('in','out') for i in range(2) for j in range(n)}
    if len(effects)!=len(expected) or {x[0] for x in effects}!=expected or any(not math.isfinite(x[1]) or x[1]<0 for x in effects):raise ValueError('Incomplete or duplicate effects')
    resources={}
    for index,s in enumerate([baseline,*samples]):
        seen=set()
        for row in s['resources']:
            role=row['Name'].split('-',3)[-1]
            if role in seen or not re.fullmatch(r'broker|gateway|worker-[0-9a-f]{24}',role):raise ValueError('Invalid container role')
            seen.add(role);mem=_mib(row['MemUsage'].split('/')[0]);cpu=float(row['CPUPerc'].removesuffix('%'))
            if not math.isfinite(cpu) or cpu<0 or not math.isfinite(mem):raise ValueError('Invalid resource sample')
            if index==0:resources[role]={'baseline_mib':mem,'peak_mib':mem,'peak_cpu_percent':cpu}
            else:
                r=resources[role];r['peak_mib']=max(r['peak_mib'],mem);r['peak_cpu_percent']=max(r['peak_cpu_percent'],cpu)
        if seen!=set(resources) or len(seen)!=4 or not {'broker','gateway'}<=seen:raise ValueError('Missing container sample')
    latency={}
    for d in ('in','out'):
        values=sorted(t for name,t in effects if name.startswith('load-'+d+'-'));latency[d]={f'p{p}':round(values[math.ceil(len(values)*p/100)-1],3) for p in (50,95,100)}
    queues={k:max(sum(v for status,v in s[k].items() if status not in ('ACKED','SENT','UNKNOWN','BLOCKED')) for s in samples) for k in ('inbox','egress')}
    return {'tenants':2,'messages':4*n,'samples':len(samples),'resources':resources,'latency_seconds_burst_to_effect':latency,'sampled_peak_pending':queues,'seconds_to_confirmed_drain':round(elapsed,3),'confirmed_messages_per_second':round(4*n/elapsed,3)}
