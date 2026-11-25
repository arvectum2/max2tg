#!/usr/bin/env python3
"""End-to-end synthetic recovery under the Docker supervisor; no network."""
import argparse,json,tempfile,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.container_supervisor import OfflineContainerSupervisor,DockerBackend
from app.lifecycle_reconcile import LifecycleReconciler

def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);args=p.parse_args();d=DockerBackend();code=Path(__file__).resolve().parents[1];image=d.call('image','inspect','--format','{{.Id}}',args.image)
    with tempfile.TemporaryDirectory(prefix='m2tg-e2e-') as tmp:
        clock=[time.time()];reconciler=None;s=OfflineContainerSupervisor(tmp,image,code,clock=lambda:clock[0]);owner=s.owner;vol={};names=[]
        def run_args(name,mounts,mode,*extra,init=False):
            names.append(name);cmd=['run','--name',name,'--pull','never','--label',f'max2tg.offline={owner}','--network','none','--read-only','--security-opt','no-new-privileges','--memory','256m','--pids-limit','64','--mount',f'type=bind,source={code / "app"},target=/src/app,readonly','--mount',f'type=bind,source={code / "tests"},target=/src/tests,readonly','--workdir','/src']
            if not init:cmd+=['-d','--user','65532:65532','--cap-drop','ALL']
            for key,target in mounts:cmd+=['--mount',f'type=volume,source={vol[key]},target=/{target}']
            return cmd+['--entrypoint','python',image,'-m','tests.supervised_bridge_fixture',mode,*extra]
        def wait(check,timeout=15):
            end=time.monotonic()+timeout
            while time.monotonic()<end:
                if reconciler is None:s.tick()
                else:
                    snap=read(gateway,"/registry/lifecycle.json")
                    if snap:reconciler.reconcile(snap)
                if check():return
                time.sleep(.2)
            raise RuntimeError('Condition not met: '+str([s.row(x)['status'] for x in ids]))
        def read(cid,path):
            return json.loads(d.call('exec',cid,'python','-c',f'from pathlib import Path;print(Path({path!r}).read_text() if Path({path!r}).exists() else "null")'))
        def touch(cid,path):d.call('exec',cid,'python','-c',f'from pathlib import Path;Path({path!r}).touch()')
        try:
            for k in ('registry','channel0','channel1','state0','state1','secret0','secret1'):vol[k]=d.call('volume','create','--label',f'max2tg.offline={owner}',f'm2tg-orch-{owner}-{k}')
            sessions=json.loads(d.call(*run_args('m2tg-e2e-init-'+owner,[(k,k) for k in vol],'init',init=True)));ids=[x[0] for x in sessions];a,b=ids
            gateway=d.call(*run_args('m2tg-e2e-gateway-'+owner,[(k,k) for k in ('registry','channel0','channel1')],'gateway',json.dumps(sessions)))
            wait(lambda:'READY' in d.call('logs',gateway))
            for i,sid in enumerate(ids):s.register(sid,vol[f'state{i}'],vol[f'channel{i}'],vol[f'secret{i}'],fixture='bridge');names.append(s.name(sid))
            def progress():return read(gateway,'/registry/progress.json') or {}
            reconciler=LifecycleReconciler(s)
            wait(lambda:s.row(a)['status']=='HEALTHY' and s.row(b)['status']=='STOPPED')
            assert s.row(b)['cid'] is None and s.row(b)['attempts']==0
            touch(gateway,'/registry/activate-second');wait(lambda:s.row(b)['status']=='HEALTHY');touch(gateway,'/registry/phase1')
            print('Onboarding activation opened second channel and launched worker',flush=True)
            wait(lambda:all(s.row(x)['status']=='HEALTHY' for x in ids) and progress().get('ingress')=={'ACKED':2} and progress().get('egress')=={'SENT':2})
            aid=s.row(a)['cid'];bid=s.row(b)['cid'];print('Initial 2+2 deliveries confirmed',flush=True)
            touch(aid,'/state/crash');wait(lambda:s.row(a)['status']=='BACKOFF');assert s.row(b)['status']=='HEALTHY'
            touch(gateway,'/registry/phase2');wait(lambda:progress().get('rows')=={'1':'ACKED','2':'ACKED','3':'RECEIVED','4':'ACKED'});print('Offline tenant retained RECEIVED; other tenant ACKED',flush=True)
            s.close();s=OfflineContainerSupervisor(tmp,image,code,clock=lambda:clock[0]);assert s.row(a)['attempts']==1;reconciler=LifecycleReconciler(s);clock[0]+=3
            wait(lambda:s.row(a)['status']=='HEALTHY' and progress().get('ingress')=={'ACKED':4});assert s.row(a)['cid']==aid and s.row(a)['attempts']==2 and s.row(b)['cid']==bid and s.row(b)['attempts']==1
            assert progress()['egress']=={'SENT':2} and sorted(progress()['bot_calls'])==sorted(x[2] for x in sessions)
            def worker_done(cid,expected):
                p=read(cid,'/state/progress.json') or {};return p.get('egress')=={'SENT':1} and p.get('ingress')==expected and (p.get('mapping') or 0)>0
            wait(lambda:all(worker_done(cid,{'SENT':2}) for cid in (aid,bid)))
            for i,cid in enumerate((aid,bid)):
                p=read(cid,'/state/progress.json');assert p['egress']=={'SENT':1} and p['ingress']=={'SENT':2} and p['mapping']>0
                calls=d.call('exec',cid,'python','-c','from pathlib import Path;print(Path("/state/calls").read_text())').splitlines();assert calls==[f'incoming-{100+i}-0',f'incoming-{100+i}-1']
            print('Recovered 4 inbound ACKs, 2 outbound receipts, exactly 6 fake sends',flush=True)
            touch(gateway,'/registry/phase3');wait(lambda:s.row(a)['status']=='BACKOFF' and progress().get('rows',{}).get('5')=='UNKNOWN');assert s.row(b)['status']=='HEALTHY'
            clock[0]+=5;wait(lambda:s.row(a)['status']=='HEALTHY' and worker_done(aid,{'SENT':2,'UNKNOWN':1}));assert s.row(a)['attempts']==3 and s.row(b)['attempts']==1
            assert progress()['ingress']=={'ACKED':4,'UNKNOWN':1} and progress()['egress']=={'SENT':2}
            calls=d.call('exec',aid,'python','-c','from pathlib import Path;print(Path("/state/calls").read_text())').splitlines();assert calls==['incoming-100-0','incoming-100-1','incoming-100-ambiguous']
            print('Post-send crash: UNKNOWN preserved, no replay, other tenant unaffected',flush=True)
            touch(gateway,'/registry/revoke-first');wait(lambda:s.row(a)['status']=='STOPPED');assert s.row(b)['status']=='HEALTHY'
            assert not read(gateway,'/registry/lifecycle.json')['sessions'][a]['ready']
            touch(gateway,'/registry/phase4');wait(lambda:progress().get('rows',{}).get('7')=='ACKED')
            assert '6' not in progress()['rows'] and progress()['ingress']=={'ACKED':5,'UNKNOWN':1}
            assert progress()['egress']=={'SENT':2} and s.row(a)['cid']==aid and s.row(a)['attempts']==3
            print('Revocation closed channel and stopped A; B delivered; UNKNOWN retained',flush=True)
            reconciler=None
            for sid in ids:s.desired(sid,False)
            wait(lambda:all(s.row(x)['status']=='STOPPED' for x in ids));touch(gateway,'/registry/stop');assert d.call('wait',gateway)=='0'
        finally:
            s.close()
            for n in reversed(names):
                c=d.inspect(n)
                if c and c['Config']['Labels'].get('max2tg.offline')==owner:d.call('rm','-f',c['Id'])
            for n in vol.values():
                if d.volume(n).get('Labels',{}).get('max2tg.offline')==owner:d.call('volume','rm',n)
            assert not d.call('ps','-aq','--filter',f'label=max2tg.offline={owner}') and not d.call('volume','ls','-q','--filter',f'label=max2tg.offline={owner}')
    print('SUPERVISED BRIDGE PASS; disposable resources removed',flush=True)

if __name__=='__main__':main()
