"""Trusted host-side lifecycle rehearsal for pre-created synthetic containers.
No public entrypoint, live credentials, create or delete. Bounded recovery
only for confirmed synthetic abnormal exits; no retry of ambiguous commands.
Only the reviewed network-none fixture commands are supported. Readiness is
an observation, not permission for production launch.
"""
import hashlib,json,time,sys,math
from copy import deepcopy
from app.deployment_plan import HEX,_verify_stopped,plan_digest
from app.operator_stage import CID,_daemon
from app.recovery_manifest import _private_dir,_file,_publish
from app.runtime_lock import RuntimeLock


class SyntheticLifecycle:
    MAX_ATTEMPTS=4
    def __init__(self,root,plan,backend,probe,*,clock=time.monotonic,sleep=time.sleep,wall=time.time,observe=None):
        self.root=_private_dir(root);self.plan=deepcopy(plan);self.d=backend;self.probe=probe;self.clock=clock;self.sleep=sleep;self.wall=wall;self.observe=observe or probe
        self.roles=self.plan['services'];self.order=['broker','gateway',*sorted(r for r in self.roles if r.startswith('worker-'))]
        if len(self.order)!=4 or set(self.order)!=set(self.roles):raise ValueError('Two synthetic workers required')
        sids=[r[7:] for r in self.order[2:]]
        if any(not HEX.fullmatch(s) for s in sids):raise ValueError('Invalid synthetic session')
        owner=self.roles['broker']['labels']['max2tg.placement']
        if not HEX.fullmatch(owner):raise ValueError('Invalid rehearsal identity')
        for role,s in self.roles.items():
            if s['container_name']!='m2tg-placement-'+owner+'-'+role or s['labels']!={'max2tg.placement':owner,'max2tg.role':role} or s['profiles']!=['acceptance-only']:raise PermissionError('Unexpected synthetic role')
            cmd=s['command'];mode='worker' if role.startswith('worker-') else role
            if len(cmd)!=5 or cmd[-1]!='operator' or cmd[:3]!=['-m','tests.placement_runtime_fixture',mode]:raise PermissionError('Only reviewed fixture commands')
            if mode=='worker':
                if cmd[3]!=role[7:]:raise PermissionError('Wrong worker session')
            else:
                sessions=json.loads(cmd[3]);expected=sorted([[sid,100+i,-10000-i] for i,sid in enumerate([v[0] for v in sessions])])
                if len(sessions)!=2 or sorted(sessions)!=expected or sorted(v[0] for v in sessions)!=sids:raise PermissionError('Synthetic identities required')
        if self.roles['broker']['command'][3]!=self.roles['gateway']['command'][3]:raise PermissionError('Session mismatch')
        self.lock=RuntimeLock(self.root/'lifecycle.lock')
    def close(self):self.lock.close()
    def _save(self,m):_publish(self.root/'lifecycle.json',m)
    def _observed(self,role):
        s=self.roles[role];c=self.d.inspect(s['container_name'])
        if not c or not CID.fullmatch(c.get('Id','')):raise PermissionError('Exact container unavailable')
        c=deepcopy(c);state=c['State'];running=state['Running'];status=state['Status']
        if (status,running) not in (('created',False),('exited',False),('running',True)) or c.get('RestartCount',0)!=0:raise PermissionError('Unexpected container state')
        if sys.platform=='darwin':
            expected={m['target']:m['source'] for m in s['volumes'] if m['type']=='bind'}
            for m in c['Mounts']:
                if m['Type']=='bind' and m['Destination'] in expected and m['Source']=='/host_mnt'+expected[m['Destination']]:m['Source']=expected[m['Destination']]
        # Reuse structural comparison; only normalize the explicitly checked mutable state.
        c['State']={'Running':False,'Status':'created'};_verify_stopped(s,c)
        # Docker normalizes unset OOM-kill-disable to false after first start.
        if c['HostConfig'].get('OomKillDisable') not in (None,False):raise PermissionError('OOM protection changed')
        c['HostConfig']['OomKillDisable']=False
        fingerprint=hashlib.sha256(json.dumps([c['Image'],c['Config'],c['HostConfig'],sorted(c['Mounts'],key=lambda m:m['Destination'])],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return {'id':c['Id'],'fingerprint':fingerprint},status,state
    def register(self):
        if (self.root/'lifecycle.json').exists() or (self.root/'lifecycle.json').is_symlink():raise PermissionError('Existing inventory; reopen only')
        if {p.name for p in self.root.iterdir()}!={'lifecycle.lock'}:raise PermissionError('Empty private lifecycle root required')
        inventory={}
        for role in self.order:
            item,status,_=self._observed(role)
            if status!='created':raise PermissionError('Fresh held fixture required')
            inventory[role]=item
        if len({v['id'] for v in inventory.values()})!=4:raise PermissionError('Duplicate container identity')
        self._save({'version':1,'digest':plan_digest(self.plan),'daemon':_daemon(self.d),'inventory':inventory,'phase':'HELD','recovery':{r:{'attempts':0,'retry_at':0} for r in self.order}})
    def _load(self):
        m=json.loads(_file(self.root,'lifecycle.json').read_text())
        if set(m)!={'version','digest','daemon','inventory','phase','recovery'} or m['version']!=1 or m['digest']!=plan_digest(self.plan) or m['daemon']!=_daemon(self.d) or set(m['inventory'])!=set(self.roles) or m['phase'] not in ('HELD','STARTING','RUNNING','STOPPING','STOPPED','BLOCKED','RECOVERING'):raise PermissionError('Lifecycle inventory changed')
        recovery=m['recovery']
        if not isinstance(recovery,dict) or set(recovery)!=set(self.roles):raise PermissionError('Invalid recovery inventory')
        for entry in recovery.values():
            if not isinstance(entry,dict) or set(entry)!={'attempts','retry_at'} or type(entry['attempts']) is not int or not 0<=entry['attempts']<=self.MAX_ATTEMPTS or type(entry['retry_at']) not in (int,float) or not math.isfinite(entry['retry_at']) or entry['retry_at']<0:raise PermissionError('Invalid recovery budget')
        return m
    def _check(self,m):
        result={}
        for role in self.order:
            item,status,state=self._observed(role)
            if item!=m['inventory'][role]:raise PermissionError('Container identity or configuration changed')
            result[role]=(status,state)
        return result
    def status(self):
        m=self._load();observed=self._check(m);n=sum(s=='running' for s,_ in observed.values());phase=m['phase']
        if phase in ('STARTING','STOPPING','BLOCKED','RECOVERING') or (phase=='RUNNING' and n!=4) or (phase in ('HELD','STOPPED') and n!=0):phase='BLOCKED'
        return {'state':phase,'running':n,'containers':4,'live_launch_authorized':False}
    def start(self):
        m=self._load();observed=self._check(m)
        if m['phase']=='RUNNING' and all(s=='running' for s,_ in observed.values()):return self.status()
        if m['phase'] not in ('HELD','STOPPED') or any(s=='running' for s,_ in observed.values()):raise PermissionError('Explicit clean stopped boundary required')
        m['phase']='STARTING';m['recovery']={r:{'attempts':0,'retry_at':0} for r in self.order};self._save(m)
        try:
            for role in self.order:
                states=self._check(m);idx=self.order.index(role)
                if any((states[r][0]=='running')!=(i<idx) for i,r in enumerate(self.order)):raise PermissionError('Unexpected start sequence')
                cid=m['inventory'][role]['id'];self.d.start(cid);deadline=self.clock()+25
                while True:
                    item,status,state=self._observed(role)
                    if item!=m['inventory'][role] or status!='running':raise RuntimeError('Role did not stay running')
                    if self.probe(role,cid,state):break
                    if self.clock()>=deadline:raise TimeoutError('Role readiness not confirmed')
                    self.sleep(.25)
            if any(s!='running' for s,_ in self._check(m).values()):raise RuntimeError('Incomplete startup')
            m['phase']='RUNNING';self._save(m);return self.status()
        except BaseException:
            m['phase']='BLOCKED';self._save(m);raise
    def monitor(self):
        """Read-only fresh observations; not a continuous health guarantee."""
        m=self._load();states=self._check(m);out={}
        for role,(status,state) in states.items():
            ready=status=='running' and self.observe(role,m['inventory'][role]['id'],state) is True
            budget=m['recovery'][role]
            out[role]={'state':status,'ready':ready,'attempts':budget['attempts']}
        phase=m['phase'];healthy=phase=='RUNNING' and all(v['ready'] for v in out.values())
        return {'state':phase,'healthy':healthy,'roles':out,'live_launch_authorized':False}

    def recover_once(self):
        """At most one definite abnormal exit, never kill a running role.

        Persist attempt before action. Interrupted/ambiguous start blocks all
        further recovery. Explicit operator invocation, no background daemon.
        """
        m=self._load();states=self._check(m)
        if m['phase']!='RUNNING':raise PermissionError('Recovery requires explicit running intent')
        for role in self.order:
            status,state=states[role];cid=m['inventory'][role]['id']
            if status=='running':
                if self.observe(role,cid,state) is not True:return 'WAITING_READINESS'
                continue
            if status!='exited' or state.get('ExitCode')!=137 or state.get('OOMKilled') is not False:return 'BLOCKED_EXIT'
            entry=m['recovery'][role]
            if entry['attempts']>=self.MAX_ATTEMPTS:return 'QUARANTINED'
            now=self.wall()
            if not math.isfinite(now) or now<0:raise ValueError('Invalid recovery clock')
            if now<entry['retry_at']:return 'BACKOFF'
            current=self._check(m)[role]
            if current!=(status,state):raise PermissionError('Recovery observation changed')
            entry['attempts']+=1;entry['retry_at']=now+2**entry['attempts'];m['phase']='RECOVERING';self._save(m)
            try:
                self.d.start(cid);deadline=self.clock()+25
                while True:
                    item,actual,info=self._observed(role)
                    if item!=m['inventory'][role] or actual!='running':raise RuntimeError('Restart not confirmed')
                    if self.probe(role,cid,info) is True:break
                    if self.clock()>=deadline:raise TimeoutError('Restart readiness not confirmed')
                    self.sleep(.25)
                m['phase']='RUNNING';self._save(m);return 'RECOVERED'
            except BaseException:
                m['phase']='BLOCKED';self._save(m);raise
        return 'NO_ACTION'

    def stop(self):
        m=self._load();self._check(m)
        # Explicit stop is also the recovery path from an incomplete start; no restart.
        m['phase']='STOPPING';self._save(m)
        try:
            for role in reversed(self.order):
                if self._check(m)[role][0]=='running':self.d.stop(m['inventory'][role]['id'])
                status,state=self._check(m)[role]
                if status=='running' or status=='exited' and state.get('ExitCode')!=0:raise RuntimeError('Clean stop not confirmed')
            m['phase']='STOPPED';self._save(m);return self.status()
        except BaseException:
            m['phase']='BLOCKED';self._save(m);raise
