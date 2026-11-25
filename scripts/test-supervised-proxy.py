#!/usr/bin/env python3
"""Disposable actual-client bridge, no external network or real credentials."""
import argparse,json,subprocess,tempfile,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.container_supervisor import OfflineContainerSupervisor,DockerBackend
from app.lifecycle_reconcile import LifecycleReconciler
from app.dependency_supervisor import DependencySupervisor

def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--encrypted',action='store_true');args=p.parse_args();d=DockerBackend();code=Path(__file__).resolve().parents[1];image=d.call('image','inspect','--format','{{.Id}}',args.image)
    with tempfile.TemporaryDirectory(prefix='m2tg-proxy-e2e-') as td:
        root=Path(td).resolve();root.chmod(0o700);cert=root/'cert';cert.mkdir();subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=synthetic','-addext','subjectAltName=DNS:api.oneme.ru,DNS:api.telegram.org','-keyout',str(cert/'key.pem'),'-out',str(cert/'cert.pem')],check=True,capture_output=True);(cert/'key.pem').chmod(0o644)
        s=OfflineContainerSupervisor(root,image,code);owner=s.owner;vol={};names=[];rec=None;gateway=None;ids=[];dep=None;manage_dependencies=True
        def run(name,mode,mounts,*extra,init=False,certificate=False):
            names.append(name);a=['run','--name',name,'--pull','never','--label',f'max2tg.offline={owner}','--network','none','--read-only','--security-opt','no-new-privileges','--memory','256m','--pids-limit','64','--mount',f'type=bind,source={code / "app"},target=/src/app,readonly','--mount',f'type=bind,source={code / "tests"},target=/src/tests,readonly','--workdir','/src']
            if not init:
                a+=['--user','65532:65532','--cap-drop','ALL','--label',f'max2tg.dependency={"gateway" if mode=="gateway-encrypted" else mode}','--restart','no']
                health='/nettg/broker-health.json' if mode=='broker' else '/registry/gateway-health.json'
                # PID 1 and current container start time: old file cannot satisfy readiness.
                probe=f"import json,time,pathlib,os; h=json.loads(pathlib.Path({health!r}).read_text()); assert h['pid']==1 and 0<=time.time()-h['at']<5; assert h['at']>=time.time()-float(pathlib.Path('/proc/uptime').read_text().split()[0])+int(pathlib.Path('/proc/1/stat').read_text().split()[21])/os.sysconf('SC_CLK_TCK')-0.2"
                a+=['--health-cmd','python -c '+__import__('shlex').quote(probe),'--health-interval','1s','--health-timeout','1s','--health-retries','3']
            for k,target in mounts:a+=['--mount',f'type=volume,source={vol[k]},target=/{target}'+(',readonly' if not init and target in ('secret','keys') else '')]
            if certificate:a+=['--mount',f'type=bind,source={cert},target=/cert,readonly']
            return d.call(*(a if init else ['create',*a[1:]]),'--entrypoint','python',image,'-m','tests.supervised_proxy_fixture',mode,*extra)
        def read(cid,path):return json.loads(d.call('exec',cid,'python','-c',f'from pathlib import Path;print(Path({path!r}).read_text() if Path({path!r}).exists() else "null")'))
        def touch(cid,path):d.call('exec',cid,'python','-c',f'from pathlib import Path;Path({path!r}).touch()')
        def wait(check,timeout=15):
            end=time.monotonic()+timeout
            while time.monotonic()<end:
                if dep and manage_dependencies:dep.tick()
                if rec:
                    snap=read(gateway,'/registry/lifecycle.json')
                    if snap:rec.reconcile(snap)
                else:s.tick()
                if check():return
                time.sleep(.2)
            raise RuntimeError('Condition not met: '+str([dict(x) for x in s.db.execute('SELECT sid,status FROM workers')])+' dependencies '+str([dep.row(x) for x in ('broker','gateway')] if dep else []))
        try:
            for k in ('registry','channel0','channel1','state0','state1','secret0','secret1','net0','net1','nettg')+(('keys0','keys1','botkeys','botsecret') if args.encrypted else ()):vol[k]=d.call('volume','create','--label',f'max2tg.offline={owner}',f'm2tg-orch-{owner}-{k}')
            sessions=json.loads(run('m2tg-proxy-init-'+owner,'init',[(k,k) for k in vol],init=True,certificate=True));ids=[x[0] for x in sessions];a,b=ids
            broker=run('m2tg-proxy-broker-'+owner,'broker',[(k,k) for k in ('net0','net1','nettg')],certificate=True)
            gateway=run('m2tg-proxy-gateway-'+owner,'gateway-encrypted' if args.encrypted else 'gateway',[(k,k) for k in ('registry','channel0','channel1')]+[('nettg','network')]+([('botkeys','keys'),('botsecret','secret')] if args.encrypted else []),json.dumps(sessions))
            dep=DependencySupervisor(root,s);dep.register('broker');dep.register('gateway');dep.desired('broker',True);dep.desired('gateway',True)
            wait(lambda:all(dep.row(x)['status']=='HEALTHY' for x in ('broker','gateway')),timeout=25)
            for i,sid in enumerate(ids):s.register(sid,vol[f'state{i}'],vol[f'channel{i}'],vol[f'secret{i}'],fixture='proxy-bridge',network_channel=vol[f'net{i}'],key_channel=vol[f'keys{i}'] if args.encrypted else None);names.append(s.name(sid))
            rec=LifecycleReconciler(s)
            def progress():return read(gateway,'/registry/progress.json') or {}
            def counts():return read(gateway,'/network/counts.json') or {}
            wait(lambda:all(s.row(x)['status']=='HEALTHY' for x in ids) and progress().get('out')=={'SENT':2});aid=s.row(a)['cid'];bid=s.row(b)['cid']
            def wp(cid):return read(cid,'/state/progress.json') or {}
            touch(gateway,'/network/phase1');wait(lambda:progress().get('in')=={'ACKED':2} and all(wp(c).get('out')=={'SENT':1} for c in (aid,bid)));print('Initial actual-client 2+2 receipts',flush=True)
            touch(gateway,'/network/cut-a');wait(lambda:wp(aid).get('authorized') is False and wp(bid).get('authorized') is True)
            touch(gateway,'/network/phase2');wait(lambda:progress().get('rows')=={'1':'ACKED','2':'ACKED','3':'RECEIVED','4':'ACKED'});assert len(counts()['max'])==3;print('A network cut: pending ingress retained, B confirmed',flush=True)
            # Crash at a known durable queue boundary, not during an ambiguous send.
            touch(gateway,'/registry/pause')
            wait(lambda:d.call('exec',gateway,'python','-c','from pathlib import Path;print(Path("/registry/paused").exists())')=='True')
            rec=None;manage_dependencies=False
            for cid in (gateway,broker):d.call('kill','--signal','KILL',cid)
            wait(lambda:wp(aid).get('authorized') is False and wp(bid).get('authorized') is False)
            dep.close();dep=DependencySupervisor(root,s)
            assert all(dep.row(x)['attempts']==1 for x in ('broker','gateway'))
            manage_dependencies=True
            wait(lambda:all(dep.row(x)['status']=='HEALTHY' and dep.row(x)['attempts']==2 for x in ('broker','gateway')) and progress().get('in')=={'ACKED':4} and wp(aid).get('authorized') is True and wp(bid).get('authorized') is True,timeout=25)
            assert dep.row('broker')['cid']==broker and dep.row('gateway')['cid']==gateway
            rec=LifecycleReconciler(s);assert len(counts()['max'])==4 and len(counts()['tg'])==2
            print('Automatic dependency restart after controller handoff: pending ingress recovered, receipts retained',flush=True)
            touch(gateway,'/network/phase3');wait(lambda:progress().get('rows',{}).get('5')=='UNKNOWN' and wp(aid).get('authorized') is True and wp(aid).get('in')=={'SENT':2,'UNKNOWN':1});assert counts()['max'].count([0,'ambiguous-0'])==1
            assert sorted(counts()['max'])==sorted([[0,'incoming-0-0'],[1,'incoming-1-0'],[0,'incoming-0-1'],[1,'incoming-1-1'],[0,'ambiguous-0']]);assert sorted(counts()['tg'])==[[-10001,'outgoing-1'],[-10000,'outgoing-0']]
            assert s.row(a)['cid']==aid and s.row(b)['cid']==bid and all(s.row(x)['attempts']==1 for x in ids);print('Recovered 4 ACKED + 2 SENT, 1 UNKNOWN; no resend, no container restart',flush=True)
            # Real PTB 429 response, persist before killing gateway. Broker keeps
            # independent evidence of any premature API request after restart.
            touch(gateway,'/network/rate-trigger')
            wait(lambda:progress().get('cooldown',0)>0 and 'rate_until' in counts())
            assert counts()['early_calls']==0
            rec=None;d.call('kill','--signal','KILL',gateway)
            wait(lambda:dep.row('gateway')['status']=='HEALTHY' and dep.row('gateway')['attempts']==3 and counts().get('after_rate') and progress().get('in')=={'ACKED':4,'UNKNOWN':1},timeout=30)
            assert counts()['early_calls']==0 and len(counts()['max'])==5 and len(counts()['tg'])==2
            assert all(s.row(x)['attempts']==1 for x in ids)
            print('Real PTB RetryAfter: no premature API call across gateway restart',flush=True)
            rec=None;manage_dependencies=False
            for sid in ids:s.desired(sid,False)
            wait(lambda:all(s.row(x)['status']=='STOPPED' for x in ids));touch(gateway,'/registry/stop');assert d.call('wait',gateway)=='0';touch(broker,'/nettg/stop-broker');assert d.call('wait',broker)=='0'
        except Exception as exc:
            if isinstance(exc,subprocess.CalledProcessError):print(exc.stdout,exc.stderr,flush=True)
            for name in names:
                try:print(name,d.call('logs','--tail','20',name), d.call('inspect','--format','{{json .State.Health}}',name),flush=True)
                except Exception:pass
            raise
        finally:
            if dep:dep.close()
            s.close()
            for n in reversed(names):
                c=d.inspect(n)
                if c and c['Config']['Labels'].get('max2tg.offline')==owner:d.call('rm','-f',c['Id'])
            for n in vol.values():
                if d.volume(n).get('Labels',{}).get('max2tg.offline')==owner:d.call('volume','rm',n)
            assert not d.call('ps','-aq','--filter',f'label=max2tg.offline={owner}') and not d.call('volume','ls','-q','--filter',f'label=max2tg.offline={owner}')
    print('SUPERVISED PROXY BRIDGE PASS; disposable resources removed',flush=True)
if __name__=='__main__':main()
