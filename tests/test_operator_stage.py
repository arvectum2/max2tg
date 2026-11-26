import json
from pathlib import Path
from copy import deepcopy
import pytest
from app.operator_stage import OperatorStage
from app.deployment_plan import build_plan
from tests.test_deployment_plan import evidence,A,B,D,I

class FakeDocker:
    def __init__(self):self.containers={};self.creates=0;self.daemon='fake-daemon';self.fail=None;self.calls=[]
    def inspect(self,name):return deepcopy(self.containers.get(name))
    def call(self,*args):
        self.calls.append(args)
        if args[0]=='info':return self.daemon
        if args[0]=='image':return I
        assert args[0]=='compose'
        if 'config' in args:return ''
        assert args[4]=='--profile' or args[3]=='--profile' # only reviewed create command
        assert 'create' in args and '--no-recreate' in args and '--pull' in args and 'never' in args
        p=json.loads(Path(args[2]).read_text());s=p['services'][args[-1]];self.creates+=1
        c=evidence(s);c['Id']=f'{self.creates:064x}';self.containers[s['container_name']]=c
        if self.fail==self.creates:raise TimeoutError('ambiguous create')
        return ''

def fixture(tmp):
    root=tmp/'operator';root.mkdir(mode=0o700);cfg=dict(deployment=D,image=I,sids=[A,B],data=str(tmp/'data'),keys=str(tmp/'keys'),backup=str(tmp/'backup'));p=build_plan(**cfg)
    for s in p['services'].values():
        for m in s['volumes']:Path(m['source']).mkdir(parents=True,exist_ok=True,mode=0o700)
    return root,cfg,p,FakeDocker()


def test_persistent_prepare_and_read_only_status(tmp_path):
    root,cfg,p,d=fixture(tmp_path);s=OperatorStage(root,d)
    try:
        result=s.prepare(cfg);assert result['state']=='HELD' and result['containers']==4 and not result['live_launch_authorized'];assert d.creates==4
        with pytest.raises(BlockingIOError):OperatorStage(root,d)
    finally:s.close()
    s=OperatorStage(root,d)
    try:
        assert s.status()==result and s.prepare(cfg)==result and d.creates==4
        with pytest.raises(PermissionError):s.prepare(dict(cfg,image='sha256:'+'2'*64))
        assert d.creates==4
    finally:s.close()


@pytest.mark.parametrize('drift',['daemon','id','missing','running','mount','plan','directory'])
def test_drift_refused_without_mutation(tmp_path,drift):
    root,cfg,p,d=fixture(tmp_path);s=OperatorStage(root,d)
    try:
        s.prepare(cfg);name=p['services']['gateway']['container_name'];c=d.containers[name]
        if drift=='daemon':d.daemon='other-daemon'
        elif drift=='id':c['Id']='f'*64
        elif drift=='missing':del d.containers[name]
        elif drift=='running':c['State']['Running']=True
        elif drift=='mount':c['Mounts'][0]['Source']='/different'
        elif drift=='plan':(root/'compose.json').write_text('{}')
        else:
            q=Path(p['services']['gateway']['volumes'][0]['source']);q.rename(q.with_name('old-registry'));q.mkdir(mode=0o700)
        with pytest.raises(PermissionError):s.status()
        assert d.creates==4
    finally:s.close()


def test_ambiguous_create_never_adopted_or_retried(tmp_path):
    root,cfg,p,d=fixture(tmp_path);d.fail=2;s=OperatorStage(root,d)
    try:
        with pytest.raises(TimeoutError):s.prepare(cfg)
        assert s.status()['state']=='BLOCKED' and s.status()['recorded']==1
    finally:s.close()
    s=OperatorStage(root,d)
    try:assert s.prepare(cfg)['state']=='BLOCKED' and d.creates==2
    finally:s.close()


def test_existing_foreign_name_no_create(tmp_path):
    root,cfg,p,d=fixture(tmp_path);spec=p['services']['broker'];d.containers[spec['container_name']]=evidence(spec);s=OperatorStage(root,d)
    try:
        with pytest.raises(PermissionError):s.prepare(cfg)
        assert d.creates==0 and not (root/'stage.json').exists()
    finally:s.close()
