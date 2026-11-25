from copy import deepcopy
import pytest
from app.deployment_plan import build_plan,plan_digest,verify_stopped_container,validate_bind_sources

A='a'*24;B='b'*24;D='d'*24;I='sha256:'+'1'*64

def plan(**kw):
    return build_plan(D,I,[B,A],**({'data':'/srv/max2tg/data','keys':'/run/max2tg-keys','backup':'/srv/max2tg/backup'}|kw))


def evidence(s):
    return {'Name':'/'+s['container_name'],'Image':s['image'],'Config':{'Labels':s['labels'],'User':s['user'],'Entrypoint':s['entrypoint'],'Cmd':s['command']},'State':{'Running':False,'Status':'created'},'HostConfig':{'NetworkMode':'none','Privileged':False,'CapAdd':None,'CapDrop':['ALL'],'ReadonlyRootfs':True,'SecurityOpt':['no-new-privileges:true'],'Memory':268435456,'NanoCpus':500000000,'PidsLimit':64,'RestartPolicy':{'Name':'no'},'IpcMode':'private','Ulimits':[{'Name':'core','Soft':0,'Hard':0}],'LogConfig':{'Type':'local','Config':{'max-size':'1m','max-file':'2'}}},'Mounts':[{'Type':'bind','Source':m['source'],'Destination':m['target'],'RW':not m['read_only'],'Propagation':'rprivate'} for m in s['volumes']]}


def test_plan_deterministic_exact_roles_and_hold():
    p=plan();assert plan_digest(p)==plan_digest(build_plan(D,I,[A,B],data='/srv/max2tg/data',keys='/run/max2tg-keys',backup='/srv/max2tg/backup'))
    assert set(p['services'])=={'broker','gateway','worker-'+A,'worker-'+B}
    for role,s in p['services'].items():
        assert s['network_mode']=='none' and s['command']==['-c','raise SystemExit(78)'] and s['profiles']==['placement-only']
        assert verify_stopped_container(s,evidence(s))['live_launch_authorized'] is False
        sources={m['source'] for m in s['volumes']}
        if role=='broker':assert all('/network/' in x for x in sources)
        if role.startswith('worker-'):assert all(role[7:] in x for x in sources)
        assert all(m['read_only'] for m in s['volumes'] if m['target'] in ('/keys','/secret') or m['target']=='/network')
        assert not any(m['source'].startswith('/srv/max2tg/backup') for m in s['volumes'])


@pytest.mark.parametrize('change',['network','privilege','key','extra','command','image','running','log','core','missing'])
def test_actual_inspect_drift_refused(change):
    s=plan()['services']['worker-'+A];c=deepcopy(evidence(s));h=c['HostConfig']
    if change=='network':h['NetworkMode']='bridge'
    elif change=='privilege':h['Privileged']=True
    elif change=='key':c['Mounts'][-1]['Source']=c['Mounts'][-1]['Source'].replace(A,B)
    elif change=='extra':c['Mounts'].append({'Type':'bind','Source':'/var/run/docker.sock','Destination':'/docker.sock','RW':True,'Propagation':'rprivate'})
    elif change=='command':c['Config']['Cmd']=['-m','app.main']
    elif change=='image':c['Image']='sha256:'+'2'*64
    elif change=='running':c['State']['Running']=True
    elif change=='log':h['LogConfig']['Config']['max-size']='100m'
    elif change=='core':h['Ulimits']=[]
    else:del h['Memory']
    with pytest.raises(PermissionError):verify_stopped_container(s,c)


def test_bad_roots_and_identity():
    with pytest.raises(ValueError):plan(keys='/srv/max2tg/data/keys')
    with pytest.raises(ValueError):plan(keys='/srv/max2tg/data')
    with pytest.raises(ValueError):plan(data='relative')
    with pytest.raises(ValueError):build_plan(D,'latest',[A],data='/data',keys='/keys',backup='/backup')
    with pytest.raises(ValueError):build_plan(D,I,[A,A],data='/data',keys='/keys',backup='/backup')


def test_private_existing_bind_sources(tmp_path):
    from pathlib import Path
    p=build_plan(D,I,[A,B],data=tmp_path/'data',keys=tmp_path/'keys',backup=tmp_path/'backup')
    paths={Path(m['source']) for s in p['services'].values() for m in s['volumes']}
    for x in paths:x.mkdir(parents=True,exist_ok=True,mode=0o700)
    assert validate_bind_sources(p)==14
    key=tmp_path/'keys'/'workers'/A;key.chmod(0o755)
    with pytest.raises(PermissionError):validate_bind_sources(p)
    key.rmdir();key.symlink_to(tmp_path/'keys'/'workers'/B,target_is_directory=True)
    with pytest.raises(ValueError):validate_bind_sources(p)
