#!/usr/bin/env python3
"""Run the actual composition functions on disposable Linux bind placements."""
import argparse,json,secrets,subprocess,tempfile,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.deployment_plan import build_plan,_verify_stopped,validate_bind_sources
from app.container_supervisor import DockerBackend

def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--native-volumes',action='store_true');p.add_argument('--load',type=int,choices=range(1,26));p.add_argument('--tenants',type=int,choices=(2,4,8,16),default=2);p.add_argument('--profile',action='store_true');p.add_argument('--long-poll',action='store_true');a=p.parse_args();
    if a.long_poll and not (a.load and a.profile):p.error('Long polling comparison requires profiled bounded load')
    if a.profile and not a.load:p.error('Profile requires bounded load mode')
    if a.tenants!=2 and (not a.native_volumes or not a.load):p.error('Larger cohorts require native volumes and bounded load mode')
    d=DockerBackend();code=Path(__file__).resolve().parents[1];image=d.call('image','inspect','--format','{{.Id}}',a.image);owner=secrets.token_hex(12);label='max2tg.placement='+owner
    with tempfile.TemporaryDirectory(prefix='runtime-',dir=code) as td:
        root=Path(td).resolve();fixture=root/'fixture';fixture.mkdir(mode=0o700);cert=root/'cert';cert.mkdir();subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=synthetic','-addext','subjectAltName=DNS:api.oneme.ru,DNS:api.telegram.org','-keyout',str(cert/'key.pem'),'-out',str(cert/'cert.pem')],check=True,capture_output=True);(cert/'key.pem').chmod(0o644)
        plan=None;ids={};vol={};initname='m2tg-placement-'+owner+'-init'
        def bind(source,target,ro=True):return {'type':'bind','source':str(source),'target':target,'read_only':ro,'bind':{'create_host_path':False,'propagation':'rprivate'}}
        def exec_py(cid,source):return d.call('exec',cid,'python','-c',source)
        def read(cid,path):return json.loads(exec_py(cid,f'from pathlib import Path;print(Path({path!r}).read_text() if Path({path!r}).exists() else "null")'))
        def wait(check,timeout=20):
            end=time.monotonic()+timeout
            while time.monotonic()<end:
                try:
                    if check():return
                except (subprocess.CalledProcessError,subprocess.TimeoutExpired):pass
                time.sleep(.35)
            raise RuntimeError('Placed runtime condition not met')
        def setup_args():return ['--network','none','--read-only','--pull','never','--label',label,'--mount',f'type=bind,source={fixture},target=/fixture','--mount',f'type=bind,source={cert},target=/cert,readonly','--mount',f'type=bind,source={code / "tests"},target=/app/tests,readonly','--entrypoint','python',image]
        try:
            args=setup_args();module='tests.placement_runtime_fixture'
            if a.native_volumes:
                module='tests.supervised_proxy_fixture'
                for n in ['registry','nettg','botkeys','botsecret']+[f'{part}{i}' for i in range(a.tenants) for part in ('channel','state','secret','net','keys')]:
                    vol[n]=d.call('volume','create','--label',label,'m2tg-placement-'+owner+'-'+n)
                    v=d.volume(vol[n]);assert v.get('Labels',{}).get('max2tg.placement')==owner and v['Driver']=='local' and not v.get('Options')
                args=args[:-3]+[z for n,v in vol.items() for z in ('--mount',f'type=volume,source={v},target=/{n}')]+args[-3:]
            d.call('run','-d','--name',initname,*args,'-m',module,'init',*([str(a.tenants)] if a.native_volumes else []));wait(lambda:d.inspect(initname)['State']['Status']=='exited',timeout=75)
            assert d.inspect(initname)['State']['ExitCode']==0,d.call('logs',initname)
            sessions=json.loads(d.call('logs',initname));assert len(sessions)==a.tenants
            sids=[x[0] for x in sessions];plan=build_plan(owner,image,sids,data=fixture/'data',keys=fixture/'keys',backup=fixture/'backup')
            if a.native_volumes:
                mapping={fixture/'data'/'registry':'registry',fixture/'data'/'gateway-secret':'botsecret',fixture/'keys'/'gateway':'botkeys',fixture/'data'/'network'/'telegram':'nettg'}
                for i,sid in enumerate(sids):
                    for base,n in ((fixture/'data'/'workers'/sid/'state','state'),(fixture/'data'/'workers'/sid/'secret','secret'),(fixture/'data'/'network'/sid,'net'),(fixture/'data'/'channels'/sid,'channel'),(fixture/'keys'/'workers'/sid,'keys')):mapping[base]=n+str(i)
                for spec in plan['services'].values():
                    spec['volumes']=[{'type':'volume','source':vol[mapping[Path(m['source'])]],'target':m['target'],'read_only':m['read_only'],'volume':{'nocopy':True}} for m in spec['volumes']]
                plan['volumes']={v:{'external':True,'name':v} for v in vol.values()}
            else:assert validate_bind_sources(plan)==14
            for role,s in plan['services'].items():
                s['profiles']=['acceptance-only'];s['command']=['-m','tests.placement_runtime_fixture',role if not role.startswith('worker-') else 'worker',json.dumps(sessions) if role in ('gateway','broker') else role[7:]];s['volumes'].append(bind(code/'tests','/app/tests'))
                if role=='broker':
                    s['volumes'].append(bind(cert,'/cert'))
                    if a.long_poll:s['command'].append('long-poll')
                elif a.profile:s['command'].append('profile')
            path=root/'compose.json';path.write_text(json.dumps(plan));cmd=('compose','-f',str(path),'--profile','acceptance-only');d.call(*cmd,'config','--quiet');d.call(*cmd,'create','--no-build','--pull','never')
            for role,s in plan['services'].items():
                c=d.inspect(s['container_name'])
                # Docker Desktop may prefix exact host bind sources with /host_mnt.
                if sys.platform=='darwin':
                    expected={m['target']:m['source'] for m in s['volumes'] if m['type']=='bind'}
                    for m in c['Mounts']:
                        if m['Type']=='bind' and m['Destination'] in expected and m['Source']=='/host_mnt'+expected[m['Destination']]:m['Source']=expected[m['Destination']]
                _verify_stopped(s,c)
                ids[role]=c['Id']
            broker=ids['broker'];gateway=ids['gateway'];workers=[ids['worker-'+sid] for sid in sids]
            def counts():return read(broker,'/network/telegram/counts.json') or {}
            def ledger(cid,path,table,column='status'):
                return json.loads(exec_py(cid,f'import sqlite3,json;d=sqlite3.connect("file:{path}?mode=ro",uri=True);print(json.dumps(dict(d.execute("SELECT {column},count(*) FROM {table} GROUP BY {column}"))))'))
            def receipts(n):return ledger(gateway,'/registry/inbox.sqlite3','telegram_ingress')=={'ACKED':n} and ledger(gateway,'/registry/telegram-egress.sqlite3','telegram_egress')=={'SENT':a.tenants}
            def ready():return exec_py(gateway,'from pathlib import Path;print(all((p/"egress.sock").is_socket() for p in Path("/channels").iterdir()))')=='True'
            def touch(name):exec_py(broker,f'from pathlib import Path;Path("/network/telegram/{name}").touch()')
            def start_broker():
                at=time.time();d.start(broker);wait(lambda:(read(broker,'/network/telegram/broker-health.json') or {}).get('at',0)>=at)
            start_broker();d.start(gateway);wait(ready)
            for cid in workers:d.start(cid)
            touch('phase1');wait(lambda:receipts(a.tenants) and len(counts().get('tg',[]))==a.tenants and len(counts().get('max',[]))==a.tenants,timeout=45)
            print(f'Actual run_isolated_gateway/worker: {a.tenants}+{a.tenants} confirmed on '+('native volume' if a.native_volumes else 'bind')+' placement',flush=True)
            if a.load:
                from app.load_metrics import summarize_load,_mib
                samples=[]
                def sample():
                    raw=d.call('stats','--no-stream','--format','{{json .}}',*ids.values());rows=[json.loads(x) for x in raw.splitlines()];
                    if sum(_mib(x['MemUsage'].split('/')[0]) for x in rows)>1024 or sum(float(x['CPUPerc'].rstrip('%')) for x in rows)>250:raise RuntimeError('Synthetic load resource stop limit exceeded')
                    samples.append({'resources':rows,'inbox':ledger(gateway,'/registry/inbox.sqlite3','telegram_ingress'),'egress':ledger(gateway,'/registry/telegram-egress.sqlite3','telegram_egress')})
                def profiles(index):
                    targets={role:cid for role,cid in ids.items() if role!='broker'}
                    d.call('kill','--signal','USR1',*targets.values());snapshots={}
                    for role,cid in targets.items():
                        def read_snapshot():
                            lines=[x for x in d.call('logs',cid).splitlines() if x.startswith('PROFILE SNAPSHOT ')]
                            if len(lines)<index:return False
                            assert len(lines)==index;snapshots[role]=json.loads(lines[-1].split('PROFILE SNAPSHOT ',1)[1]);return True
                        wait(read_snapshot,timeout=10)
                    return snapshots
                sample();baseline=samples.pop()
                before=profiles(1) if a.profile else None
                begin=time.monotonic();exec_py(broker,f'from pathlib import Path;Path("/network/telegram/load-count").write_text({str(a.load)!r})')
                target=a.tenants*(1+a.load);deadline=begin+60
                while True:
                    sample();last=samples[-1]
                    if last['inbox']=={'ACKED':target} and last['egress']=={'SENT':target}:break
                    if time.monotonic()>deadline:raise RuntimeError('Load did not drain')
                expected_max=[[i,f'incoming-{i}-0'] for i in range(a.tenants)]+[[i,f'load-in-{i}-{j}'] for i in range(a.tenants) for j in range(a.load)]
                expected_tg=[[-10000-i,f'outgoing-{i}'] for i in range(a.tenants)]+[[-10000-i,f'load-out-{i}-{j}'] for i in range(a.tenants) for j in range(a.load)]
                result=counts();assert sorted(result['max'])==sorted(expected_max) and sorted(result['tg'])==sorted(expected_tg)
                wait(lambda:all(ledger(cid,'/state/to-telegram.sqlite3','worker_telegram_intents')=={'SENT':a.load+1} for cid in workers),timeout=15)
                print('LOAD RESULT '+json.dumps(summarize_load(a.load,baseline,samples,result['load_effects'],time.monotonic()-begin,tenants=a.tenants),sort_keys=True),flush=True)
                if a.profile:
                    from app.stage_profile import aggregate_profiles
                    stages=aggregate_profiles(before,profiles(2));assert stages['gateway']['api.send.call']['count']==a.tenants*a.load
                    assert stages['gateway']['ingress.ipc']['count']>=a.tenants*a.load
                    assert stages['workers']['worker.receipt_repair']['count']>0
                    print('PROFILE RESULT '+json.dumps(stages,sort_keys=True),flush=True)
                    print('POLL MODE '+('long-poll' if a.long_poll else 'immediate'),flush=True)
                return
            worker_starts={sid:d.inspect(plan['services']['worker-'+sid]['container_name'])['State']['StartedAt'] for sid in sids}
            # Broker-only crash: real gateway polling must stay alive, hold
            # delivery until a successful poll and refreshed forum evidence.
            gateway_start=d.inspect(plan['services']['gateway']['container_name'])['State']['StartedAt']
            d.call('kill','--signal','KILL',broker)
            wait(lambda:exec_py(gateway,'from pathlib import Path;print(not any((p/"egress.sock").exists() for p in Path("/channels").iterdir()))')=='True')
            assert d.inspect(plan['services']['gateway']['container_name'])['State']['Running']
            start_broker();touch('phase2');wait(lambda:receipts(4) and len(counts().get('max',[]))==4,timeout=35)
            c=d.inspect(plan['services']['gateway']['container_name']);assert c['Id']==gateway and c['State']['StartedAt']==gateway_start and c['RestartCount']==0
            print('Broker-only outage recovered: gateway stayed running; 4 ACKED',flush=True)
            # Preserve original dual dependency crash at confirmed boundary.
            prior=counts()['auth'][:]
            for cid in (gateway,broker):d.call('kill','--signal','KILL',cid)
            start_broker();d.start(gateway);wait(lambda:receipts(4) and all(x>y for x,y in zip(counts().get('auth',[]),prior)) and len(counts().get('max',[]))==4,timeout=35)
            auth_before=counts()['auth'][0]
            touch('phase3');wait(lambda:ledger(gateway,'/registry/inbox.sqlite3','telegram_ingress')=={'ACKED':4,'UNKNOWN':1},timeout=30)
            wait(lambda:counts().get('auth',[0,0])[0]>auth_before,timeout=15)
            assert sorted(counts()['max'])==sorted([[0,'incoming-0-0'],[1,'incoming-1-0'],[0,'incoming-0-1'],[1,'incoming-1-1'],[0,'ambiguous-0']]);assert sorted(counts()['tg'])==[[-10001,'outgoing-1'],[-10000,'outgoing-0']]
            for sid,cid in zip(sids,workers):
                c=d.inspect(plan['services']['worker-'+sid]['container_name']);assert c['Id']==cid and c['State']['StartedAt']==worker_starts[sid] and c['State']['Running'] and c['RestartCount']==0
            print('4 ACKED + 2 SENT + 1 UNKNOWN; exact external effects, same workers',flush=True)
        except Exception as exc:
            if isinstance(exc,subprocess.CalledProcessError):print(exc.stdout,exc.stderr,flush=True)
            for cid in [initname,*ids.values()]:
                try:print(cid,d.call('logs','--tail','12',cid),flush=True)
                except subprocess.CalledProcessError:pass
            raise
        finally:
            names=[initname]+([s['container_name'] for s in plan['services'].values()] if plan else [])
            for name in names:
                c=d.inspect(name)
                if c and c['Config']['Labels'].get('max2tg.placement')==owner:d.call('rm','-f',c['Id'])
            assert not d.call('ps','-aq','--filter','label='+label)
            # Only remove contents of this fresh isolated mount, not a caller-supplied path.
            if a.native_volumes:
                for v in vol.values():
                    if d.volume(v).get('Labels',{}).get('max2tg.placement')==owner:d.call('volume','rm',v)
                assert not d.call('volume','ls','-q','--filter','label='+label)
                if a.load:print('LOAD CLEANUP PASS',flush=True)
            else:subprocess.run(['docker','run','--rm',*setup_args(),'-c','import shutil;from pathlib import Path;[(shutil.rmtree(p) if p.is_dir() else p.unlink()) for p in Path("/fixture").iterdir()]'],check=True,capture_output=True,timeout=60)
    print('PLACED RUNTIME PASS; disposable resources removed',flush=True)
if __name__=='__main__':main()
