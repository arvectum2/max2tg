"""Trusted offline operator metadata/provisioning interface for shared sessions.
No public authentication, live container operations or credential replacement.
Caller stops gateway and affected worker and excludes other provisioning writers.
"""
from contextlib import ExitStack,contextmanager
from pathlib import Path
import json
from app.recovery_manifest import _file
from app.coordinated_backup import _disjoint
from app.recovery_manifest import _private_dir
from app.runtime_lock import RuntimeLock
from app.shared_onboarding import SharedOnboarding
from app.worker_provision import prepare_worker,check_worker


class OperatorSessions:
    def __init__(self,registry,*,provisioning_stopped=False):
        if provisioning_stopped is not True:raise PermissionError('Exclusive provisioning window required')
        self.registry=registry;self.stack=ExitStack();self.closed=False
        try:
            for n,shared in [('shared-gateway.lock',False),('operator-sessions.lock',False),('shared-provisioning.lock',True)]:
                lock=RuntimeLock(registry.root/n,shared=shared);self.stack.callback(lock.close)
            self.onboarding=SharedOnboarding(registry)
        except BaseException:self.stack.close();raise
    def close(self):self.closed=True;self.stack.close()
    def _require_open(self):
        if self.closed:raise RuntimeError('Operator session window closed')
    def status(self,request):
        self._require_open();row=self.onboarding.row(request);c=self.registry.get(row['sid']);route=self.registry.db.execute('SELECT status,tg_chat_id FROM tg_routes WHERE session_id=?',(row['sid'],)).fetchone()
        if not route or c['credential_mode']!='external' or c['telegram_mode']!='shared' or route['tg_chat_id']!=row['forum'] or c['tg_chat_id']!=row['forum']:raise PermissionError('Inconsistent session identity')
        active=bool(c['desired_active']);stage=row['stage']
        if active and (stage!='ACTIVE' or route['status']!='ACTIVE') or stage=='REVOKED' and (active or route['status']!='DISABLED') or stage=='ACTIVE' and route['status']!='ACTIVE':raise PermissionError('Inconsistent session state')
        return {'sid':row['sid'],'state':'PAUSED' if stage=='ACTIVE' and not active else stage,'desired_active':active,'live_launch_authorized':False}
    def reserve(self,request,actor,forum):
        self._require_open();self.onboarding.reserve(request,actor,forum);return self.status(request)
    async def verify(self,request,bot):
        self._require_open();await self.onboarding.verify_forum(request,bot);return self.status(request)
    def _layout(self,state,channel,secret,key_path):
        self._require_open();paths=tuple(_private_dir(p) for p in (state,channel,secret));key=Path(key_path)
        if not key.is_absolute() or key.name!='worker.key':raise PermissionError('Explicit scoped encryption key required')
        _disjoint([self.registry.root.resolve(),*paths,_private_dir(key.parent)])
        return paths,key
    def _check(self,paths,key):
        return lambda sid,actor,forum,ipc:check_worker(*paths,sid,actor,forum,ipc.read_bytes(),key_path=key)
    @contextmanager
    def _stopped_worker(self,request,state,channel,secret,key_path,*,require_secret=True):
        paths,key=self._layout(state,channel,secret,key_path);lock=RuntimeLock(paths[0]/'worker.lock')
        try:
            row=self.onboarding.row(request)
            if json.loads(_file(paths[0],'worker-acl.json').read_text())!={'sid':row['sid'],'actor':int(row['actor']),'forum':int(row['forum'])}:raise PermissionError('Worker ACL mismatch')
            if require_secret and row['stage']=='REVOKED':raise PermissionError('Revoked session')
            if require_secret and not self._check(paths,key)(row['sid'],int(row['actor']),int(row['forum']),self.onboarding.ipc_key(request)):raise PermissionError('Worker evidence mismatch')
            yield paths,key
        finally:lock.close()
    def provision(self,request,state,channel,secret,key_path):
        row=self.onboarding.row(request);paths,key=self._layout(state,channel,secret,key_path)
        if row['stage'] not in ('FORUM_VERIFIED','PROVISIONED'):raise PermissionError('Verified forum required')
        if not prepare_worker(*paths,row['sid'],int(row['actor']),int(row['forum']),self.onboarding.ipc_key(request).read_bytes(),key_path=key):raise PermissionError('Worker preparation failed')
        with self._stopped_worker(request,*paths,key):self.onboarding.confirm_provisioned(request,check=self._check(paths,key))
        return self.status(request)
    async def activate(self,request,bot,state,channel,secret,key_path):
        with self._stopped_worker(request,state,channel,secret,key_path) as (paths,key):await self.onboarding.activate(request,bot,check=self._check(paths,key))
        return self.status(request)
    def pause(self,request,state,channel,secret,key_path):
        with self._stopped_worker(request,state,channel,secret,key_path,require_secret=False):
            db=self.registry.db;db.execute('BEGIN IMMEDIATE')
            try:
                row=self.status(request)
                if row['state'] not in ('ACTIVE','PAUSED'):raise PermissionError('Active session required')
                self.registry.set_active(row['sid'],False);db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise
        return self.status(request)
    def revoke(self,request,state,channel,secret,key_path):
        # Revocation must remain possible if credentials are lost/invalid. Verify
        # exact worker ACL without decrypting secret or recreating any key.
        with self._stopped_worker(request,state,channel,secret,key_path,require_secret=False):self.onboarding.revoke(request)
        return self.status(request)
