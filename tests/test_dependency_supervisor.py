"""Restart budget and identity rejection without Docker or messenger traffic."""
from types import SimpleNamespace as NS
from copy import deepcopy
import pytest
from app.dependency_supervisor import DependencySupervisor

OWNER='a'*24;IMAGE='sha256:'+'b'*64

class Backend:
    def __init__(self,root):
        self.starts=[];self.stops=[];self.containers={}
        for role,targets in [('broker',('/net0','/net1','/nettg')),('gateway',('/registry','/channel0','/channel1','/network'))]:
            name=f'm2tg-proxy-{role}-{OWNER}';binds={'/src/app':str(root/'app'),'/src/tests':str(root/'tests')}
            if role=='broker':binds['/cert']=str(root/'cert')
            mounts=[{'Type':'bind','Destination':d,'Source':s,'RW':False} for d,s in binds.items()]+[{'Type':'volume','Destination':d,'Name':'synthetic-'+d[1:],'RW':True} for d in targets]
            self.containers[name]={'Name':'/'+name,'Id':role+'-id','Image':IMAGE,'Config':{'Labels':{'max2tg.offline':OWNER,'max2tg.dependency':role},'Entrypoint':['python'],'Cmd':['-m','tests.supervised_proxy_fixture',role]+(['[]'] if role=='gateway' else []),'User':'65532:65532'},'HostConfig':{'NetworkMode':'none','ReadonlyRootfs':True,'Privileged':False,'CapAdd':None,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges'],'Memory':268435456,'PidsLimit':64,'RestartPolicy':{'Name':'no'}},'Mounts':mounts,'State':{'Running':False}}
    def inspect(self,name):return deepcopy(self.containers.get(name))
    def volume(self,name):return {'Labels':{'max2tg.offline':OWNER},'Driver':'local','Options':None}
    def start(self,cid):self.starts.append(cid)
    def stop(self,cid):self.stops.append(cid)
    def item(self,role):return self.containers[f'm2tg-proxy-{role}-{OWNER}']


def test_budget_handoff_order_and_missing_identity(tmp_path):
    tmp_path.chmod(0o700);b=Backend(tmp_path);w=NS(backend=b,owner=OWNER,image=IMAGE,code=tmp_path);clock=[100];d=DependencySupervisor(tmp_path,w,clock=lambda:clock[0])
    try:
        for role in ('broker','gateway'):d.register(role);d.desired(role,True)
        d.tick();assert b.starts==['broker-id'] and d.row('gateway')['status']=='WAITING_BROKER'
        d.tick();assert d.row('broker')['status']=='BACKOFF'
        d.close();d=DependencySupervisor(tmp_path,w,clock=lambda:clock[0]);assert d.row('broker')['attempts']==1
        for _ in range(3):clock[0]+=60;d.tick()
        clock[0]+=60;d.tick();assert d.row('broker')['status']=='QUARANTINED' and len(b.starts)==4
        b.item('broker')['State']={'Running':True,'Health':{'Status':'healthy'}};d.tick();assert b.starts[-1]=='gateway-id'
        b.item('gateway')['Id']='foreign';clock[0]+=60;d.tick();assert d.row('gateway')['status']=='BLOCKED' and len(b.starts)==5
        del b.containers[d.row('gateway')['name']];d.tick();assert d.row('gateway')['status']=='MISSING' and len(b.starts)==5
    finally:d.close()


def test_config_drift_and_unhealthy_never_force_killed(tmp_path):
    tmp_path.chmod(0o700);b=Backend(tmp_path);w=NS(backend=b,owner=OWNER,image=IMAGE,code=tmp_path);d=DependencySupervisor(tmp_path,w)
    try:
        d.register('broker');d.desired('broker',True);b.item('broker')['State']={'Running':True,'Health':{'Status':'unhealthy'}};d.tick();assert d.row('broker')['status']=='UNHEALTHY' and not b.stops
        b.item('broker')['HostConfig']['NetworkMode']='bridge';d.tick();assert d.row('broker')['status']=='BLOCKED';d.desired('broker',False);d.tick();assert not b.stops
        with pytest.raises(PermissionError):d.register('broker')
    finally:d.close()
