"""Fail-closed offline placement plan, not a runnable SaaS deployment.

No Docker or network operations here. Render only stopped/held candidates with
all network disabled. A later reviewed real entrypoint and firewall are separate.
"""
import hashlib,json,re,stat
from pathlib import Path

HEX=re.compile(r'[0-9a-f]{24}')
IMAGE=re.compile(r'sha256:[0-9a-f]{64}')
HOLD=['-c','raise SystemExit(78)']
UID='65532:65532'


def _root(value):
    p=Path(value)
    if not p.is_absolute() or p==Path('/') or p.resolve()!=p or any(q.is_symlink() for q in (p,*p.parents)) or not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(p)):raise ValueError('Canonical absolute deployment root required')
    return p


def build_plan(deployment,image,sids,*,data,keys,backup):
    if not isinstance(deployment,str) or not HEX.fullmatch(deployment) or not isinstance(image,str) or not IMAGE.fullmatch(image):raise ValueError('Pinned image and deployment identity required')
    if type(sids) not in (list,tuple) or not 1<=len(sids)<=100 or any(type(s) is not str or not HEX.fullmatch(s) for s in sids) or len(set(sids))!=len(sids):raise ValueError('Explicit unique tenant set required')
    data,keys,backup=map(_root,(data,keys,backup))
    for p in (data,keys,backup):
        if any(p!=q and (p.is_relative_to(q) or q.is_relative_to(p)) for q in (data,keys,backup)) or len({data,keys,backup})!=3:raise ValueError('Separate storage roots required')
    services={};prefix='m2tg-placement-'+deployment+'-'
    def service(role,mounts):
        return {'container_name':prefix+role,'image':image,'pull_policy':'never','profiles':['placement-only'],'entrypoint':['python'],'command':HOLD.copy(),'user':UID,'network_mode':'none','read_only':True,'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],'restart':'no','mem_limit':'256m','cpus':0.5,'pids_limit':64,'ulimits':{'core':{'soft':0,'hard':0}},'logging':{'driver':'local','options':{'max-size':'1m','max-file':'2'}},'labels':{'max2tg.placement':deployment,'max2tg.role':role},'volumes':[{'type':'bind','source':str(p),'target':target,'read_only':ro,'bind':{'create_host_path':False,'propagation':'rprivate'}} for p,target,ro in mounts]}
    services['broker']=service('broker',[(data/'network'/sid,'/network/'+sid,False) for sid in sorted(sids)]+[(data/'network'/'telegram','/network/telegram',False)])
    services['gateway']=service('gateway',[(data/'registry','/registry',False),(data/'gateway-secret','/secret',True),(keys/'gateway','/keys',True),(data/'network'/'telegram','/network',True)]+[(data/'channels'/sid,'/channels/'+sid,False) for sid in sorted(sids)])
    for sid in sorted(sids):services['worker-'+sid]=service('worker-'+sid,[(data/'workers'/sid/'state','/state',False),(data/'channels'/sid,'/channel',False),(data/'workers'/sid/'secret','/secret',True),(data/'network'/sid,'/network',True),(keys/'workers'/sid,'/keys',True)])
    return {'name':'m2tg-placement-'+deployment,'services':services}


def validate_bind_sources(plan):
    """Require existing, private, non-aliased source directories at inspection.

    Does not attest encrypted backing storage; host storage preflight is separate.
    Trusted operator must prevent concurrent mount/path changes.
    """
    paths=set()
    for spec in plan['services'].values():
        paths.update(m['source'] for m in spec['volumes'])
    identities=set();checked=[]
    for source in sorted(paths):
        p=_root(source);st=p.stat()
        if not stat.S_ISDIR(st.st_mode) or st.st_mode&0o077:raise PermissionError('Private bind directory required')
        ident=(st.st_dev,st.st_ino)
        if ident in identities or any(p.is_relative_to(q) or q.is_relative_to(p) for q in checked):raise PermissionError('Aliased or overlapping bind sources')
        identities.add(ident);checked.append(p)
    return len(checked)


def plan_digest(plan):return hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def verify_stopped_container(spec,c):
    """Compare actual Docker inspect to exact generated placement spec."""
    try:
        h=c['HostConfig'];cfg=c['Config'];state=c['State'];mounts=c['Mounts']
        if c['Name']!='/'+spec['container_name'] or c['Image']!=spec['image'] or any(cfg['Labels'].get(k)!=v for k,v in spec['labels'].items()) or cfg['User']!=UID or cfg['Entrypoint']!=['python'] or cfg['Cmd']!=HOLD or state['Running'] or state.get('Status')!='created':raise PermissionError('Container identity or hold mismatch')
        if h['NetworkMode']!='none' or h['Privileged'] or h['CapAdd'] or set(h['CapDrop'] or [])!={'ALL'} or not h['ReadonlyRootfs'] or not any(x in ('no-new-privileges','no-new-privileges:true') for x in h['SecurityOpt'] or []) or h['Memory']!=268435456 or h['NanoCpus']!=500000000 or h['PidsLimit']!=64 or h['RestartPolicy']['Name'] not in ('','no') or h.get('PortBindings') or h.get('PublishAllPorts') or h.get('Devices') or h.get('DeviceRequests') or h.get('VolumesFrom') or h.get('PidMode') or h.get('IpcMode') not in ('','private'):raise PermissionError('Container isolation mismatch')
        if not any(u['Name']=='core' and u['Soft']==u['Hard']==0 for u in h.get('Ulimits') or []):raise PermissionError('Core dump restriction missing')
        log=h['LogConfig']
        if log['Type']!='local' or log['Config'].get('max-size')!='1m' or log['Config'].get('max-file')!='2':raise PermissionError('Log bounds missing')
        expected={(m['source'],m['target'],not m['read_only'],'rprivate') for m in spec['volumes']}
        actual={(m['Source'],m['Destination'],m['RW'],m.get('Propagation')) for m in mounts if m['Type']=='bind'}
        if len(mounts)!=len(expected) or actual!=expected:raise PermissionError('Exact role bind mounts required')
    except (KeyError,TypeError,AttributeError):raise PermissionError('Incomplete container evidence') from None
    return {'role':spec['labels']['max2tg.role'],'placement':'PASS','live_launch_authorized':False}
