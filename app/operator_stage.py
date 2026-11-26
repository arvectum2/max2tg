"""Persistent operator staging of held, network-none SaaS placement.
No start, restart, removal, credentials, or authorization to launch.
A failed/ambiguous create is deliberately not adopted or retried.
"""
import argparse,json,re,sys
from pathlib import Path
from app.deployment_plan import build_plan,plan_digest,validate_bind_sources,verify_stopped_container
from app.container_supervisor import DockerBackend
from app.recovery_manifest import _private_dir,_file,_publish
from app.runtime_lock import RuntimeLock

CID=re.compile(r'[0-9a-f]{64}')


def _paths(plan):
    validate_bind_sources(plan)
    result={}
    for s in plan['services'].values():
        for m in s['volumes']:
            p=Path(m['source']);st=p.stat();result[str(p)]=[st.st_dev,st.st_ino,st.st_uid]
    return result


def _inspect(d,s):
    c=d.inspect(s['container_name'])
    if not c:return None
    if sys.platform=='darwin':
        expected={m['target']:m['source'] for m in s['volumes']}
        for m in c['Mounts']:
            if m['Type']=='bind' and m['Destination'] in expected and m['Source']=='/host_mnt'+expected[m['Destination']]:m['Source']=expected[m['Destination']]
    verify_stopped_container(s,c)
    if not CID.fullmatch(c.get('Id','')):raise PermissionError('Exact container ID required')
    return c['Id']


def _daemon(d):
    value=d.call('info','--format','{{.ID}}')
    if not value or len(value)>128 or not re.fullmatch(r'[A-Za-z0-9:-]+',value):raise PermissionError('Docker daemon identity unavailable')
    return value


class OperatorStage:
    def __init__(self,root,backend=None):
        p=Path(root)
        if not p.is_absolute() or p.resolve()!=p:raise PermissionError('Canonical operator directory required')
        self.root=_private_dir(p);self.d=backend or DockerBackend();self.lock=RuntimeLock(self.root/'operator.lock')
    def close(self):self.lock.close()
    def _save(self,m):_publish(self.root/'stage.json',m)
    def _load(self):
        m=json.loads(_file(self.root,'stage.json').read_text())
        if set(m)!={'version','config','digest','daemon','paths','phase','ids'} or m['version']!=1 or m['phase'] not in ('PLANNED','CREATING','HELD','BLOCKED'):raise ValueError('Invalid stage manifest')
        plan=build_plan(**m['config']);stored=json.loads(_file(self.root,'compose.json').read_text())
        if plan!=stored or plan_digest(plan)!=m['digest']:raise PermissionError('Placement manifest changed')
        if not isinstance(m['ids'],dict) or not set(m['ids'])<=set(plan['services']) or any(not isinstance(v,str) or not CID.fullmatch(v) for v in m['ids'].values()) or len(set(m['ids'].values()))!=len(m['ids']):raise PermissionError('Invalid container inventory')
        if self.root in (Path(m['config'][k]) for k in ('data','keys','backup')) or any(self.root.is_relative_to(Path(m['config'][k])) or Path(m['config'][k]).is_relative_to(self.root) for k in ('data','keys','backup')):raise PermissionError('Separate operator root required')
        return m,plan
    def _check(self,m,plan):
        if m['daemon']!=_daemon(self.d) or m['paths']!=_paths(plan):raise PermissionError('Daemon or bind identity changed')
        for role,s in plan['services'].items():
            if _inspect(self.d,s)!=m['ids'].get(role):raise PermissionError('Missing, foreign or unrecorded container')
    def status(self):
        m,plan=self._load()
        if m['phase']!='HELD' or set(m['ids'])!=set(plan['services']):return {'state':'BLOCKED','reason':'incomplete staging requires operator review','recorded':len(m['ids']),'expected':len(plan['services']),'live_launch_authorized':False}
        self._check(m,plan)
        return {'state':'HELD','containers':len(m['ids']),'plan_digest':m['digest'],'live_launch_authorized':False}
    def prepare(self,config):
        if (self.root/'stage.json').exists() or (self.root/'stage.json').is_symlink():
            m,_=self._load()
            if m['config']!=config:raise PermissionError('Immutable staging configuration')
            return self.status()
        if {p.name for p in self.root.iterdir()}!={'operator.lock'}:raise PermissionError('New empty operator directory required')
        plan=build_plan(**config)
        for k in ('data','keys','backup'):
            p=Path(config[k]);
            if p.is_relative_to(self.root) or self.root.is_relative_to(p):raise PermissionError('Separate operator root required')
        paths=_paths(plan);daemon=_daemon(self.d)
        if any(self.d.inspect(s['container_name']) is not None for s in plan['services'].values()):raise PermissionError('Existing container; no adoption')
        if self.d.call('image','inspect','--format','{{.Id}}',config['image'])!=config['image']:raise PermissionError('Pinned local image required')
        _publish(self.root/'compose.json',plan)
        m={'version':1,'config':config,'digest':plan_digest(plan),'daemon':daemon,'paths':paths,'phase':'PLANNED','ids':{}};self._save(m)
        cmd=('compose','-f',str(self.root/'compose.json'),'--profile','placement-only')
        try:
            self.d.call(*cmd,'config','--quiet')
            m['phase']='CREATING';self._save(m)
            for role,s in plan['services'].items():
                self._check(m,plan)
                self.d.call(*cmd,'create','--no-recreate','--no-build','--pull','never',role)
                cid=_inspect(self.d,s)
                if cid is None:raise RuntimeError('Create not confirmed')
                m['ids'][role]=cid;self._save(m)
            self._check(m,plan);m['phase']='HELD';self._save(m)
            return self.status()
        except BaseException:
            m['phase']='BLOCKED';self._save(m);raise


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',required=True);sub=p.add_subparsers(dest='action',required=True);prep=sub.add_parser('prepare')
    for key in ('deployment','image','data','keys','backup'):prep.add_argument('--'+key,required=True)
    prep.add_argument('--sid',action='append',required=True);sub.add_parser('status');a=p.parse_args();stage=OperatorStage(a.state)
    try:
        config={k:getattr(a,k) for k in ('deployment','image','data','keys','backup')}|{'sids':sorted(a.sid)} if a.action=='prepare' else None
        result=stage.prepare(config) if config else stage.status();print(json.dumps(result,sort_keys=True));return 0 if result['state']=='HELD' else 2
    finally:stage.close()

if __name__=='__main__':raise SystemExit(main())
