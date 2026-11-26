import json
from copy import deepcopy
import pytest
from app.operator_lifecycle import SyntheticLifecycle
from tests.test_deployment_plan import plan,evidence,A,B

class Docker:
    def __init__(self,p):
        self.daemon='fixture-daemon';self.containers={};self.events=[]
        for i,(r,s) in enumerate(p['services'].items()):
            c=evidence(s);c['Id']=f'{i+1:064x}';c['RestartCount']=0;self.containers[s['container_name']]=c
    def call(self,*args):assert args==('info','--format','{{.ID}}');return self.daemon
    def inspect(self,name):return deepcopy(self.containers.get(name))
    def change(self,cid,running):
        c=next(c for c in self.containers.values() if c['Id']==cid);r=c['Config']['Labels']['max2tg.role'];self.events.append(('start' if running else 'stop',r));c['State']={'Running':running,'Status':'running' if running else 'exited','ExitCode':0};c['HostConfig']['OomKillDisable']=False
    def start(self,cid):self.change(cid,True)
    def stop(self,cid):self.change(cid,False)


def fixture(tmp):
    p=plan();sessions=json.dumps([[A,100,-10000],[B,101,-10001]])
    for role,s in p['services'].items():
        mode='worker' if role.startswith('worker-') else role;s['profiles']=['acceptance-only'];s['command']=['-m','tests.placement_runtime_fixture',mode,role[7:] if mode=='worker' else sessions,'operator']
    root=tmp/'state';root.mkdir(mode=0o700);return root,p,Docker(p)


def test_two_cycles_reopen_same_ids(tmp_path):
    root,p,d=fixture(tmp_path);s=SyntheticLifecycle(root,p,d,lambda *_:True)
    try:
        s.register();assert s.status()['state']=='HELD';assert s.start()['state']=='RUNNING';assert s.start()['state']=='RUNNING';assert s.stop()['state']=='STOPPED'
    finally:s.close()
    s=SyntheticLifecycle(root,p,d,lambda *_:True)
    try:assert s.start()['state']=='RUNNING';assert s.stop()['state']=='STOPPED'
    finally:s.close()
    order=['broker','gateway','worker-'+A,'worker-'+B];assert d.events==([('start',r) for r in order]+[('stop',r) for r in reversed(order)])*2


@pytest.mark.parametrize('drift',['id','missing','daemon','mount','command','running','oom'])
def test_drift_refused_before_start(tmp_path,drift):
    root,p,d=fixture(tmp_path);s=SyntheticLifecycle(root,p,d,lambda *_:True)
    try:
        s.register();name=p['services']['gateway']['container_name'];c=d.containers[name]
        if drift=='id':c['Id']='f'*64
        elif drift=='missing':del d.containers[name]
        elif drift=='daemon':d.daemon='another'
        elif drift=='mount':c['Mounts'][0]['Source']='/other'
        elif drift=='command':c['Config']['Cmd']=['-m','app.main']
        elif drift=='oom':c['HostConfig']['OomKillDisable']=True
        else:c['State']={'Running':True,'Status':'running'}
        with pytest.raises(PermissionError):s.start()
        assert not d.events
    finally:s.close()


def test_failed_readiness_no_later_start_no_automatic_retry(tmp_path):
    root,p,d=fixture(tmp_path);now=[0];s=SyntheticLifecycle(root,p,d,lambda role,*_:role!='gateway',clock=lambda:now[0],sleep=lambda t:now.__setitem__(0,now[0]+t))
    try:
        s.register()
        with pytest.raises(TimeoutError):s.start()
        assert s.status()=={'state':'BLOCKED','running':2,'containers':4,'live_launch_authorized':False}
        with pytest.raises(PermissionError):s.start()
        assert d.events==[('start','broker'),('start','gateway')]
        assert s.stop()['state']=='STOPPED' and d.events[-2:]==[('stop','gateway'),('stop','broker')]
    finally:s.close()


def test_real_command_rejected(tmp_path):
    root,p,d=fixture(tmp_path);p['services']['gateway']['command']=['-m','app.main']
    with pytest.raises(PermissionError):SyntheticLifecycle(root,p,d,lambda *_:True)
    assert not d.events


def crash(d,p,role,code=137,oom=False):
    d.containers[p['services'][role]['container_name']]['State']={'Running':False,'Status':'exited','ExitCode':code,'OOMKilled':oom}


def test_recovery_budget_persists_and_quarantines(tmp_path):
    root,p,d=fixture(tmp_path);now=[100];s=SyntheticLifecycle(root,p,d,lambda *_:True,wall=lambda:now[0])
    try:
        s.register();s.start()
        for i in range(4):
            crash(d,p,'broker');assert not s.monitor()['healthy'];assert s.recover_once()=='RECOVERED'
            assert s.monitor()['roles']['broker']['attempts']==i+1
            s.close();s=SyntheticLifecycle(root,p,d,lambda *_:True,wall=lambda:now[0])
            crash(d,p,'broker');assert s.recover_once()==('QUARANTINED' if i==3 else 'BACKOFF');now[0]+=60
        assert len([e for e in d.events if e==('start','broker')])==5
    finally:s.close()


@pytest.mark.parametrize('code,oom',[(0,False),(1,False),(137,True),(137,None)])
def test_other_exits_require_review(tmp_path,code,oom):
    root,p,d=fixture(tmp_path);s=SyntheticLifecycle(root,p,d,lambda *_:True)
    try:
        s.register();s.start();before=d.events[:];crash(d,p,'broker',code,oom)
        assert s.recover_once()=='BLOCKED_EXIT' and d.events==before
    finally:s.close()


def test_unready_dependency_never_killed_or_restarted(tmp_path):
    root,p,d=fixture(tmp_path);s=SyntheticLifecycle(root,p,d,lambda *_:True,observe=lambda role,*_:role!='broker')
    try:
        s.register();s.start();before=d.events[:];crash(d,p,'gateway')
        assert not s.monitor()['healthy'] and s.recover_once()=='WAITING_READINESS' and d.events==before
    finally:s.close()


def test_ambiguous_restart_blocks_across_reopen(tmp_path):
    root,p,d=fixture(tmp_path);s=SyntheticLifecycle(root,p,d,lambda *_:True)
    try:
        s.register();s.start();crash(d,p,'broker');original=d.start
        def ambiguous(cid):original(cid);raise TimeoutError('ambiguous Docker response')
        d.start=ambiguous
        with pytest.raises(TimeoutError):s.recover_once()
        assert s.monitor()['roles']['broker']['attempts']==1 and not s.monitor()['healthy']
    finally:s.close()
    s=SyntheticLifecycle(root,p,d,lambda *_:True)
    try:
        before=d.events[:]
        with pytest.raises(PermissionError):s.recover_once()
        assert d.events==before and s.status()['state']=='BLOCKED'
        assert s.stop()['state']=='STOPPED'
    finally:s.close()


def test_interrupted_recovery_is_not_replayed(tmp_path):
    root,p,d=fixture(tmp_path);s=SyntheticLifecycle(root,p,d,lambda *_:True)
    try:
        s.register();s.start();crash(d,p,'broker');m=s._load();m['phase']='RECOVERING';m['recovery']['broker']={'attempts':1,'retry_at':100};s._save(m);before=d.events[:]
        with pytest.raises(PermissionError):s.recover_once()
        assert s.status()['state']=='BLOCKED' and d.events==before
    finally:s.close()


def test_checkpoint_terminal_handoff_persisted(tmp_path):
    root,p,d=fixture(tmp_path);s=SyntheticLifecycle(root,p,d,lambda *_:True);calls=[]
    def capture():calls.append(1);return {'verified':True}
    try:
        s.register();s.start()
        with pytest.raises(PermissionError):s.checkpoint(capture)
        assert not calls;s.stop();assert s.checkpoint(capture)=={'verified':True}
        assert s.status()['state']=='ARCHIVED'
    finally:s.close()
    s=SyntheticLifecycle(root,p,d,lambda *_:True)
    try:
        before=d.events[:]
        for act in (s.start,s.stop,s.recover_once,lambda:s.checkpoint(capture)):
            with pytest.raises(PermissionError):act()
        assert calls==[1] and d.events==before
    finally:s.close()


@pytest.mark.parametrize('interrupted',[False,True])
def test_failed_or_interrupted_checkpoint_refuses_restart(tmp_path,interrupted):
    root,p,d=fixture(tmp_path);s=SyntheticLifecycle(root,p,d,lambda *_:True)
    try:
        s.register();s.start();s.stop()
        if interrupted:
            m=s._load();m['phase']='BACKING_UP';s._save(m)
        else:
            with pytest.raises(RuntimeError):s.checkpoint(lambda:{'verified':False})
    finally:s.close()
    s=SyntheticLifecycle(root,p,d,lambda *_:True)
    try:
        assert s.status()['state']=='BLOCKED';before=d.events[:]
        for act in (s.start,s.stop,s.recover_once):
            with pytest.raises(PermissionError):act()
        assert d.events==before
    finally:s.close()
