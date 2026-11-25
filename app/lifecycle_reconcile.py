"""Trusted local host adapter for synthetic lifecycle reconciliation.

Input must come from the operator-owned gateway, never an HTTP/user payload.
No start or stop on missing/stale/malformed evidence. No worker data deletion.
"""
import math,time

class LifecycleReconciler:
    def __init__(self,supervisor,*,clock=time.time,max_age=10):
        self.supervisor=supervisor;self.clock=clock;self.max_age=max_age
        supervisor.db.execute("CREATE TABLE IF NOT EXISTS lifecycle_checkpoint(singleton INTEGER PRIMARY KEY CHECK(singleton=1),at REAL NOT NULL)")

    def reconcile(self,snapshot):
        if not isinstance(snapshot,dict) or set(snapshot)!={'at','sessions'}:raise ValueError('Invalid lifecycle snapshot')
        at=snapshot['at'];now=self.clock();last=self.supervisor.db.execute('SELECT at FROM lifecycle_checkpoint WHERE singleton=1').fetchone()
        if type(at) not in (int,float) or not math.isfinite(at) or not -2<=now-at<=self.max_age or last is not None and at<last[0]:raise ValueError('Stale lifecycle snapshot')
        rows={r['sid']:dict(r) for r in self.supervisor.db.execute('SELECT * FROM workers')}
        sessions=snapshot['sessions']
        if type(sessions) is not dict or set(sessions)!=set(rows):raise ValueError('Incomplete registered sessions')
        for sid,value in sessions.items():
            if type(value) is not dict or set(value)!={'active','ready'} or any(type(v) is not bool for v in value.values()) or value['ready'] and not value['active']:raise ValueError('Invalid session state')
        # Do not turn an active-but-temporarily-unready worker off/on: that
        # would reset its crash budget. Readiness gates starts, not stops.
        eligible=set()
        self.supervisor.db.execute('BEGIN IMMEDIATE')
        try:
            for sid,value in sessions.items():
                if value['active'] and value['ready']:
                    eligible.add(sid);self.supervisor.desired(sid,True)
                elif not value['active']:self.supervisor.desired(sid,False)
            self.supervisor.db.execute('INSERT INTO lifecycle_checkpoint VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET at=excluded.at',(at,))
            self.supervisor.db.execute('COMMIT')
        except BaseException:self.supervisor.db.execute('ROLLBACK');raise
        self.supervisor.tick(eligible=eligible)
        return {sid:self.supervisor.row(sid)['status'] for sid in rows}
