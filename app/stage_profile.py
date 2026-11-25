"""Opt-in bounded aggregate timings; no event IDs, payloads, URLs or labels.
Single asyncio event loop per process. Overlapping spans are not additive.
No external telemetry, storage or logging in this module.
"""
import math,time
from contextlib import contextmanager

API_GROUPS=('initialize','identity','verify','poll','send','topic','other')
STAGES=frozenset({f'api.{group}.{part}' for group in API_GROUPS for part in ('lock','pace','call','deferred')}|{
    'gateway.forum_refresh','gateway.lifecycle_lock','gateway.lifecycle','gateway.ingest_commit',
    'gateway.ingress_round','gateway.egress_round','gateway.drain_lock',
    'ingress.pending_age','ingress.ipc','egress.pending_age','egress.send_total',
    'worker.route','worker.ingress','worker.egress_reconcile','worker.receipt_repair','worker.lock',
})

class StageProfile:
    def __init__(self,clock=time.monotonic):self.clock=clock;self._rows={}
    def record(self,stage,seconds):
        if stage not in STAGES:raise ValueError('Unknown profile stage')
        if not math.isfinite(seconds) or seconds<0:return
        row=self._rows.setdefault(stage,[0,0.0]);row[0]+=1;row[1]+=seconds
    def snapshot(self):return {k:{'count':v[0],'seconds':v[1]} for k,v in sorted(self._rows.items())}

@contextmanager
def measure(profile,stage):
    if profile is None:yield;return
    at=profile.clock()
    try:yield
    finally:profile.record(stage,profile.clock()-at)

def started(profile):return profile.clock() if profile is not None else None

def finished(profile,stage,at):
    if profile is not None:profile.record(stage,profile.clock()-at)

def age(profile,stage,stamp):
    if profile is not None:profile.record(stage,time.time()-stamp)


def difference(before,after):
    """Counts/sums of completed spans between two cumulative snapshots."""
    result={}
    for snapshot in (before,after):
        for stage,row in snapshot.items():
            if stage not in STAGES or set(row)!={'count','seconds'} or type(row['count']) is not int or row['count']<0 or type(row['seconds']) not in (float,int) or not math.isfinite(row['seconds']) or row['seconds']<0:raise ValueError('Invalid profile snapshot')
    for stage in set(before)|set(after):
        old=before.get(stage,{'count':0,'seconds':0});new=after.get(stage,{'count':0,'seconds':0})
        count=new['count']-old['count'];seconds=new['seconds']-old['seconds']
        if count<0 or seconds<0 or (not count and seconds):raise ValueError('Profile counter regressed')
        if count:result[stage]={'count':count,'seconds':seconds}
    return result


def aggregate_profiles(before,after):
    if set(before)!=set(after) or 'gateway' not in before:raise ValueError('Profile cohort changed')
    result={'gateway':difference(before['gateway'],after['gateway']),'workers':{}}
    for role in before:
        if role=='gateway':continue
        for stage,row in difference(before[role],after[role]).items():
            dest=result['workers'].setdefault(stage,{'count':0,'seconds':0.0})
            dest['count']+=row['count'];dest['seconds']+=row['seconds']
    for stages in result.values():
        for row in stages.values():row['mean_ms']=round(1000*row['seconds']/row['count'],3);row['seconds']=round(row['seconds'],6)
    return result
