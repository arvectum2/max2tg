"""Deterministic bounded sparse/burst/idle scenario; synthetic messages only."""
from collections import Counter
import math

IDLE=(32,50)

def schedule(tenants,cycles=1):
    if type(cycles) is not int or not 1<=cycles<=3:raise ValueError('Invalid cycle count')
    if tenants not in (2,4,8,16):raise ValueError('Invalid cohort')
    seq=Counter();waves=[]
    def add(at,counts):
        entries=[]
        for i,n in counts:
            for _ in range(n):
                j=seq[i];seq[i]+=1;entries.append((i,j))
        waves.append((at,tuple(entries)))
    for k,at in enumerate((0,3,6,9)):
        add(at,[(i,1) for i in range(tenants) if i%min(4,tenants)==k%min(4,tenants)])
    add(12,[(i,12 if i==0 else 4) for i in range(tenants)])
    for k,at in enumerate((15,19)):
        add(at,[(i,1) for i in range(tenants) if i%2==k])
    add(53,[(i,4) for i in range(tenants)])
    for k,at in enumerate((58,62,66,70)):
        add(at,[(i,1) for i in range(tenants) if i%min(4,tenants)==k%min(4,tenants)])
    first=tuple(waves)
    for cycle in range(1,cycles):
        for at,pairs in first:
            entries=[]
            for i,_ in pairs:
                j=seq[i];seq[i]+=1;entries.append((i,j))
            waves.append((at+90*cycle,tuple(entries)))
    return waves


def summarize(tenants,effects,verified,samples,*,cycles=1):
    waves=schedule(tenants,cycles);expected={f'mixed-{d}-{i}-{j}' for _,pairs in waves for i,j in pairs for d in ('in','out')}
    if len(effects)!=len(expected) or {r[0] for r in effects}!=expected:raise ValueError('Missing or duplicate effects')
    if any(len(r)!=3 or any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in r[1:]) for r in effects):raise ValueError('Invalid timing')
    if set(verified)!=set(map(str,range(tenants))) or any(len(v)<2 or not any(IDLE[0]<=x<=53 for x in v) for v in verified.values()):raise ValueError('Missing repeated forum checks')
    if not samples:raise ValueError('No backlog samples')
    # Actual host samples are approximate relative to the broker clock.
    idle=[]
    for cycle in range(cycles):
        lo,hi=(90*cycle+v for v in IDLE)
        batch=[s for s in samples if lo<=s['at']<=hi]
        if len(batch)<2 or any(s['pending_in'] or s['pending_out'] or s['ready']!=tenants for s in batch):raise ValueError('Idle backlog or readiness regression')
        if any(not any(lo<=v<=hi+3 for v in checks) for checks in verified.values()):raise ValueError('Missing idle verification')
        idle.extend(batch)
    def p95(v):return sorted(v)[math.ceil(len(v)*.95)-1]
    result={}
    for d in ('in','out'):
        per={i:[r[1] for r in effects if r[0].startswith(f'mixed-{d}-{i}-')] for i in range(tenants)}
        values=[v for vv in per.values() for v in vv]
        result[d]={'messages':len(values),'p95_s':round(p95(values),3),'max_s':round(max(values),3),'worst_tenant_p95_s':round(max(p95(v) for v in per.values()),3),'best_tenant_p95_s':round(min(p95(v) for v in per.values()),3),'hot_tenant_p95_s':round(p95(per[0]),3)}
    return {'tenants':tenants,'messages':len(effects),'directions':result,'idle_samples':len(idle),'peak_pending_in':max(s['pending_in'] for s in samples),'peak_pending_out':max(s['pending_out'] for s in samples),'min_ready_sample':min(s['ready'] for s in samples),'verification_rounds_min':min(map(len,verified.values())),'verification_rounds_max':max(map(len,verified.values()))}
