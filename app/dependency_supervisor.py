"""Offline restart-only controller for two pre-created synthetic dependencies.

No container creation, replacement, removal or network activation. Trusted
operator provisions exact containers; persistent configuration fingerprint
and identity must match on every operation. No unhealthy-container kill.
"""
import hashlib,json,sqlite3,subprocess,time
from app.runtime_lock import RuntimeLock
from app.recovery_manifest import _private_dir

ROLES=('broker','gateway')

class DependencySupervisor:
    MAX_ATTEMPTS=4
    def __init__(self,root,worker_supervisor,*,clock=time.time):
        self.root=_private_dir(root);self.lock=RuntimeLock(self.root/'dependencies.lock');self.workers=worker_supervisor;self.backend=worker_supervisor.backend;self.clock=clock
        self.db=None
        try:
            p=self.root/'dependencies.sqlite3'
            if p.is_symlink():raise PermissionError('Unsafe dependency database')
            self.db=sqlite3.connect(p,isolation_level=None);self.db.row_factory=sqlite3.Row;p.chmod(0o600)
            self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS dependencies(role TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,cid TEXT NOT NULL UNIQUE,spec TEXT NOT NULL,desired INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,retry REAL NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT \'STOPPED\')')
        except BaseException:
            if self.db:self.db.close()
            self.lock.close();raise
    def row(self,role):
        r=self.db.execute('SELECT * FROM dependencies WHERE role=?',(role,)).fetchone()
        if not r:raise KeyError('Unknown dependency')
        return dict(r)
    def _fingerprint(self,role,c):
        w=self.workers;conf=c['Config'];host=c['HostConfig'];labels=conf['Labels'];cmd=conf['Cmd'];expected='m2tg-proxy-'+role+'-'+w.owner
        if c['Name']!='/'+expected or labels.get('max2tg.offline')!=w.owner or labels.get('max2tg.dependency')!=role or c['Image']!=w.image or conf['Entrypoint']!=['python'] or cmd[:2]!=['-m','tests.supervised_proxy_fixture'] or cmd[2] not in ((role,'gateway-encrypted') if role=='gateway' else (role,)) or len(cmd)!=(3 if role=='broker' else 4):raise PermissionError('Unexpected dependency identity')
        if host['NetworkMode']!='none' or not host['ReadonlyRootfs'] or host['Privileged'] or host['CapAdd'] or 'ALL' not in (host['CapDrop'] or []) or conf['User']!='65532:65532' or host['Memory']!=268435456 or host['PidsLimit']!=64 or host['RestartPolicy']['Name'] not in ('','no') or not any(x in ('no-new-privileges','no-new-privileges:true') for x in host['SecurityOpt'] or []):raise PermissionError('Unexpected dependency sandbox')
        mounts=c['Mounts'];expected_vols={'/net0','/net1','/nettg'} if role=='broker' else {'/registry','/channel0','/channel1','/network'}
        if role=='gateway' and cmd[2]=='gateway-encrypted':expected_vols|={'/secret','/keys'}
        volumes=[m for m in mounts if m['Type']=='volume'];binds=[m for m in mounts if m['Type']=='bind'];expected_binds={'/src/app':str(w.code/'app'),'/src/tests':str(w.code/'tests')}
        if role=='broker':expected_binds['/cert']=str(self.root/'cert')
        if len(mounts)!=len(volumes)+len(binds) or len(volumes)!=len(expected_vols) or {m['Destination'] for m in volumes}!=expected_vols or len(binds)!=len(expected_binds) or {m['Destination'] for m in binds}!=set(expected_binds) or any(m['Source'] not in (expected_binds[m['Destination']],'/host_mnt'+expected_binds[m['Destination']]) for m in binds) or any(m['RW'] for m in binds):raise PermissionError('Unexpected dependency mounts')
        if len({m['Name'] for m in volumes})!=len(volumes) or any(m['RW'] for m in volumes if m['Destination'] in ('/secret','/keys')):raise PermissionError('Unsafe dependency secret mounts')
        if role=='gateway' and cmd[2]=='gateway-encrypted':
            prefix='m2tg-orch-'+w.owner+'-';expected={'/registry':'registry','/channel0':'channel0','/channel1':'channel1','/network':'nettg','/keys':'botkeys','/secret':'botsecret'}
            if {m['Destination']:m['Name'] for m in volumes}!={k:prefix+v for k,v in expected.items()}:raise PermissionError('Encrypted gateway role volume mismatch')
        if role=='broker' and any(m['Name'].endswith(('keys0','keys1','botkeys','botsecret','secret0','secret1')) for m in volumes):raise PermissionError('Broker must not mount secret storage')
        for m in volumes:
            v=self.backend.volume(m['Name'])
            if v.get('Labels',{}).get('max2tg.offline')!=w.owner or v.get('Driver')!='local' or v.get('Options'):raise PermissionError('Foreign dependency volume')
        # Pin all inspected immutable configuration, not mutable process state.
        return hashlib.sha256(json.dumps([c['Image'],conf,{k:host.get(k) for k in ('NetworkMode','ReadonlyRootfs','Privileged','CapAdd','CapDrop','SecurityOpt','Memory','PidsLimit','RestartPolicy','PortBindings','Devices','DeviceRequests','PidMode','IpcMode','LogConfig','Binds','Mounts')},sorted(mounts,key=lambda m:m['Destination'])],sort_keys=True).encode()).hexdigest()
    def register(self,role):
        if role not in ROLES:raise ValueError('Invalid dependency')
        name='m2tg-proxy-'+role+'-'+self.workers.owner;c=self.backend.inspect(name)
        if not c:raise FileNotFoundError('Pre-created dependency required')
        spec=self._fingerprint(role,c)
        old=self.db.execute('SELECT cid,spec FROM dependencies WHERE role=?',(role,)).fetchone()
        if old and tuple(old)!=(c['Id'],spec):raise PermissionError('Immutable dependency changed')
        self.db.execute('INSERT OR IGNORE INTO dependencies(role,name,cid,spec) VALUES(?,?,?,?)',(role,name,c['Id'],spec))
    def desired(self,role,active):
        if type(active) is not bool:raise ValueError('Boolean desired state required')
        self.row(role);self.db.execute('UPDATE dependencies SET attempts=CASE WHEN desired=0 AND ?=1 THEN 0 ELSE attempts END,retry=CASE WHEN desired=0 AND ?=1 THEN 0 ELSE retry END,desired=? WHERE role=?',(int(active),int(active),int(active),role))
    def tick(self):
        # Broker first on startup, gateway first on explicit shutdown.
        rows={r['role']:dict(r) for r in self.db.execute('SELECT * FROM dependencies')}
        ready=False
        for role in ('gateway','broker') if rows and not any(r['desired'] for r in rows.values()) else ROLES:
            if role not in rows:continue
            r=rows[role];status='ERROR'
            try:
                c=self.backend.inspect(r['name'])
                if not c:status='MISSING'
                elif c['Id']!=r['cid'] or self._fingerprint(role,c)!=r['spec']:status='BLOCKED'
                elif not r['desired']:
                    if c['State']['Running']:self.backend.stop(r['cid']);status='STOPPING'
                    else:status='STOPPED'
                elif c['State']['Running']:
                    h=c['State'].get('Health',{}).get('Status');status='HEALTHY' if h=='healthy' else 'UNHEALTHY' if h=='unhealthy' else 'STARTING'
                elif role=='gateway' and not ready:status='WAITING_BROKER'
                elif r['attempts']>=self.MAX_ATTEMPTS:status='QUARANTINED'
                elif self.clock()<r['retry']:status='BACKOFF'
                else:
                    n=r['attempts']+1;self.db.execute('UPDATE dependencies SET attempts=?,retry=?,status=\'STARTING\' WHERE role=?',(n,self.clock()+min(60,2**n),role));self.backend.start(r['cid']);status='STARTING'
            except PermissionError:status='BLOCKED'
            except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError):status='ERROR'
            self.db.execute('UPDATE dependencies SET status=? WHERE role=?',(status,role))
            if role=='broker':ready=status=='HEALTHY'
        return {role:self.row(role)['status'] for role in rows}
    def close(self):
        self.db.close();self.lock.close()
