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
