"""Offline-only trusted Docker supervisor; never accepts tenant-supplied commands.

Resources must be explicitly pre-provisioned synthetic volumes. No deletion,
no production credentials, no network, no Docker socket in a worker.
"""
from __future__ import annotations
import hashlib,json,re,secrets,sqlite3,subprocess,time,os
from pathlib import Path
from app.runtime_lock import RuntimeLock

HEX=re.compile(r'^[0-9a-f]{24}$')
IMAGE=re.compile(r'^sha256:[0-9a-f]{64}$')

class DockerBackend:
    def call(self,*args):
        return subprocess.run(['docker',*args],check=True,capture_output=True,text=True,timeout=15).stdout.strip()
    def inspect(self,name):
        # Empty exact-name list is definite absence; daemon errors propagate.
        found=self.call('ps','-aq','--no-trunc','--filter',f'name=^/{name}$').splitlines()
        if not found:return None
        if len(found)!=1:raise RuntimeError('Ambiguous container identity')
        return json.loads(self.call('inspect',found[0]))[0]
    def volume(self,name):return json.loads(self.call('volume','inspect',name))[0]
    def create(self,args):return self.call('create',*args)
    def start(self,cid):self.call('start',cid)
    def stop(self,cid):self.call('stop','--time','5',cid)

class OfflineContainerSupervisor:
    MAX_ATTEMPTS=4
    def __init__(self,root,image,code,*,backend=None,clock=time.time):
        root=Path(root);code=Path(code).resolve()
        if not root.is_dir() or root.is_symlink() or (root.stat().st_mode&0o077 or root.stat().st_uid!=os.getuid()):raise PermissionError('Private supervisor directory required')
        if not IMAGE.fullmatch(image) or not (code/'tests'/'supervised_container_fixture.py').is_file():raise ValueError('Pinned image and synthetic fixture required')
        self.lock=RuntimeLock(root/'container-supervisor.lock');self.image=image;self.code=code;self.backend=backend or DockerBackend();self.clock=clock
        self.db=None
        try:
            path=root/'supervisor.sqlite3'
            if path.is_symlink():raise PermissionError('Unsafe supervisor database')
            self.db=sqlite3.connect(path,isolation_level=None);self.db.row_factory=sqlite3.Row;path.chmod(0o600)
            self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''CREATE TABLE IF NOT EXISTS owner(id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workers(sid TEXT PRIMARY KEY, spec TEXT NOT NULL, desired INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, retry REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'STOPPED', cid TEXT);''')
            self.db.execute('INSERT INTO owner SELECT ? WHERE NOT EXISTS(SELECT 1 FROM owner)',(secrets.token_hex(12),));self.owner=self.db.execute('SELECT id FROM owner').fetchone()[0]
        except BaseException:
            if self.db:self.db.close()
            self.lock.close();raise
    def name(self,sid):return 'm2tg-offline-'+self.owner+'-'+sid
    def row(self,sid):return dict(self.db.execute('SELECT * FROM workers WHERE sid=?',(sid,)).fetchone())
    def register(self,sid,state,channel,secret):
        if not isinstance(sid,str) or not HEX.fullmatch(sid):raise ValueError('Invalid session')
        names=[state,channel,secret]
        if len(set(names))!=3 or any(not re.fullmatch(r'm2tg-orch-[a-z0-9-]{1,100}',n) for n in names):raise ValueError('Only separate test volumes')
        for n in names:
            v=self.backend.volume(n)
            if v.get('Labels',{}).get('max2tg.offline')!=self.owner or v.get('Driver')!='local' or v.get('Options'):raise PermissionError('Foreign or unsafe volume')
        spec=json.dumps({'image':self.image,'code':str(self.code),'volumes':names},sort_keys=True)
        for r in self.db.execute('SELECT sid,spec FROM workers'):
            if r['sid']==sid:
                if r['spec']!=spec:raise ValueError('Immutable worker spec')
            elif set(json.loads(r['spec'])['volumes'])&set(names):raise ValueError('Cross-tenant volume reuse')
        self.db.execute('INSERT OR IGNORE INTO workers(sid,spec) VALUES(?,?)',(sid,spec))
    def desired(self,sid,active):
        if type(active) is not bool:raise ValueError('Boolean desired state required')
        self.row(sid)
        # Only an explicit off -> on resets the persistent crash budget.
        self.db.execute('UPDATE workers SET attempts=CASE WHEN desired=0 AND ?=1 THEN 0 ELSE attempts END,retry=CASE WHEN desired=0 AND ?=1 THEN 0 ELSE retry END,desired=? WHERE sid=?',(int(active),int(active),int(active),sid))
    def _args(self,r):
        spec=json.loads(r['spec']);sid=r['sid'];digest=hashlib.sha256(r['spec'].encode()).hexdigest()
        if spec['image']!=self.image or spec['code']!=str(self.code):raise PermissionError('Supervisor configuration changed')
        args=['--pull','never','--name',self.name(sid),'--label','max2tg.offline='+self.owner,'--label','max2tg.sid='+sid,'--label','max2tg.spec='+digest,'--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','65532:65532','--memory','256m','--cpus','0.5','--pids-limit','64','--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2','--restart','no','--mount',f'type=bind,source={self.code / "tests"},target=/src/tests,readonly','--workdir','/src']
        for n,target in zip(spec['volumes'],('state','channel','secret')):args+=['--mount',f'type=volume,source={n},target=/{target}'+(',readonly' if target=='secret' else '')]
        args+=['--health-cmd','python -c "import pathlib,time; assert time.time()-pathlib.Path(\'/state/heartbeat\').stat().st_mtime<5"','--health-interval','1s','--health-timeout','1s','--health-retries','3','--entrypoint','python',self.image,'-m','tests.supervised_container_fixture',sid]
        return args
    def _verify(self,r,c):
        if c is None:return
        labels=c['Config']['Labels'];digest=hashlib.sha256(r['spec'].encode()).hexdigest()
        if labels.get('max2tg.offline')!=self.owner or labels.get('max2tg.sid')!=r['sid'] or labels.get('max2tg.spec')!=digest or c['Image']!=self.image or (r['cid'] and c['Id']!=r['cid']):raise PermissionError('Foreign container; do not operate')
        spec=json.loads(r['spec']);host=c['HostConfig'];conf=c['Config']
        mounts={m['Destination']:(m.get('Name'),m['RW']) for m in c['Mounts'] if m['Type']=='volume'}
        if mounts!=dict(zip(('/state','/channel','/secret'),zip(spec['volumes'],(True,True,False)))):raise PermissionError('Unexpected volume mounts')
        binds=[m for m in c['Mounts'] if m['Type']=='bind']
        if len(c['Mounts'])!=4 or len(binds)!=1 or binds[0]['Destination']!='/src/tests' or binds[0]['RW'] or binds[0]['Source']!=str(self.code/'tests'):raise PermissionError('Unexpected code mount')
        if host['NetworkMode']!='none' or not host['ReadonlyRootfs'] or host['Privileged'] or host['CapAdd'] or 'ALL' not in (host['CapDrop'] or []) or not any(x in ('no-new-privileges','no-new-privileges:true') for x in (host['SecurityOpt'] or [])) or conf['User']!='65532:65532' or host['Memory']!=268435456 or host['PidsLimit']!=64 or host['NanoCpus']!=500000000 or host['RestartPolicy']['Name'] not in ('no','') or conf['Cmd']!=['-m','tests.supervised_container_fixture',r['sid']] or conf['Entrypoint']!=['python']:raise PermissionError('Unexpected runtime policy')

    def tick(self):
        for r in list(self.db.execute('SELECT * FROM workers ORDER BY sid')):
            r=dict(r);sid=r['sid'];state='ERROR'
            try:
                c=self.backend.inspect(self.name(sid));self._verify(r,c)
                if c is not None and not r['cid']:self.db.execute('UPDATE workers SET cid=? WHERE sid=?',(c['Id'],sid))
                if c is None and r['cid']:state='MISSING' # external removal; no blind recreation
                elif not r['desired']:
                    if c and c['State']['Running']:self.backend.stop(c['Id']);state='STOPPING'
                    else:state='STOPPED'
                elif c and c['State']['Running']:
                    health=c['State'].get('Health',{}).get('Status');state='HEALTHY' if health=='healthy' else 'UNHEALTHY' if health=='unhealthy' else 'STARTING'
                elif r['attempts']>=self.MAX_ATTEMPTS:state='QUARANTINED'
                elif self.clock()<r['retry']:state='BACKOFF'
                else:
                    # Persist attempt before any Docker start/create, including timeout.
                    n=r['attempts']+1;self.db.execute('UPDATE workers SET attempts=?,retry=?,status=\'STARTING\' WHERE sid=?',(n,self.clock()+min(60,2**n),sid))
                    for name in json.loads(r['spec'])['volumes']:
                        v=self.backend.volume(name)
                        if v.get('Labels',{}).get('max2tg.offline')!=self.owner or v.get('Driver')!='local' or v.get('Options'):raise PermissionError('Unsafe volume at launch')
                    if c is None:
                        self.backend.create(self._args(r));c=self.backend.inspect(self.name(sid));self._verify(r,c)
                        if c is None:raise RuntimeError('Create not confirmed')
                    self.db.execute('UPDATE workers SET cid=? WHERE sid=?',(c['Id'],sid));self.backend.start(c['Id']);state='STARTING'
            except PermissionError:state='BLOCKED'
            except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError):state='ERROR'
            self.db.execute('UPDATE workers SET status=? WHERE sid=?',(state,sid))
    def close(self):
        # Handoff keeps already-running containers; explicit desired=False stops.
        self.db.close();self.lock.close()
