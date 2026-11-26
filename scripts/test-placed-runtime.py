#!/usr/bin/env python3
"""Run the actual composition functions on disposable Linux bind placements."""
import argparse,json,secrets,subprocess,tempfile,time,sys
from copy import deepcopy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.deployment_plan import build_plan,_verify_stopped,validate_bind_sources
from app.container_supervisor import DockerBackend

def main():
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--native-volumes',action='store_true');p.add_argument('--load',type=int,choices=range(1,26));p.add_argument('--tenants',type=int,choices=(2,4,8,16,32,64),default=2);p.add_argument('--profile',action='store_true');p.add_argument('--long-poll',action='store_true');p.add_argument('--mixed',action='store_true');p.add_argument('--soak',action='store_true');p.add_argument('--storage-check',action='store_true');p.add_argument('--wal-maintenance',action='store_true');p.add_argument('--delay150',action='store_true');p.add_argument('--balanced',action='store_true');p.add_argument('--balanced-fault',action='store_true');p.add_argument('--reconcile-revoked',action='store_true');p.add_argument('--operator-lifecycle',action='store_true');p.add_argument('--operator-recovery',action='store_true');p.add_argument('--operator-backup',action='store_true');p.add_argument('--operator-restore',action='store_true');p.add_argument('--operator-sessions',action='store_true');p.add_argument('--account-binding',action='store_true');a=p.parse_args();
    if a.account_binding and not (a.native_volumes and a.tenants==2 and not (a.load or a.mixed or a.balanced_fault or a.operator_lifecycle)):p.error('Account binding requires separate two-session native fixture')
    if a.operator_sessions and not (a.operator_lifecycle and not a.operator_backup):p.error('Session operations require a separate stopped lifecycle fixture')
    if a.operator_restore and not a.operator_backup:p.error('Restore requires operator backup fixture')
    if a.operator_backup and not a.operator_lifecycle:p.error('Backup requires operator lifecycle fixture')
    if a.operator_recovery and not a.operator_lifecycle:p.error('Recovery requires operator lifecycle fixture')
    if a.operator_lifecycle and not (a.native_volumes and a.tenants==2 and not (a.load or a.mixed or a.balanced_fault or a.balanced or a.profile or a.long_poll or a.wal_maintenance or a.delay150 or a.storage_check or a.soak)):p.error('Operator lifecycle requires simple two-session native fixture')
    if a.reconcile_revoked and not a.balanced_fault:p.error('Reconciliation requires balanced fault fixture')
    if a.balanced_fault and not (a.native_volumes and a.tenants==2 and not (a.mixed or a.load or a.profile or a.long_poll or a.wal_maintenance)):p.error('Fault mode requires two native sessions without load flags')
    if a.balanced_fault:a.balanced=True
    if (a.delay150 or a.balanced) and not a.balanced_fault and not (a.mixed and a.profile and a.long_poll and a.native_volumes):p.error('Experimental comparison requires native profiled mixed long polling')
    if a.storage_check and not a.mixed:p.error('Storage check requires mixed workload')
    if a.wal_maintenance and not (a.storage_check or a.soak):p.error('WAL candidate requires resource checkpoints')
    if a.soak and not a.mixed:p.error('Soak requires mixed workload')
    if a.mixed and (a.load or not (a.native_volumes and a.profile and a.long_poll)):p.error('Mixed requires native volumes, profile, long-poll and no load')
    if a.long_poll and not ((a.load or a.mixed) and a.profile):p.error('Long polling comparison requires profiled bounded load')
    if a.profile and not (a.load or a.mixed):p.error('Profile requires bounded load mode')
    if a.tenants!=2 and (not a.native_volumes or not (a.load or a.mixed)):p.error('Larger cohorts require native volumes and bounded load mode')
    if a.tenants==32 and not (a.mixed and a.native_volumes and a.profile and a.long_poll and a.storage_check and a.wal_maintenance and not a.soak):p.error('32 tenants require single mixed cycle with WAL and storage observations')
    if a.tenants==64 and not (a.balanced and a.delay150 and a.mixed and a.native_volumes and a.profile and a.long_poll and a.storage_check and a.wal_maintenance):p.error('64 requires paced delayed balanced workload with storage observations')
    ram_cap={32:2048,64:3584}.get(a.tenants,1024);cpu_cap={32:350,64:450}.get(a.tenants,250)
    d=DockerBackend()
    if a.tenants>=32:
        from app.load_metrics import _mib
        vm=int(d.call('info','--format','{{.MemTotal}}'))/2**20
        running=[json.loads(x) for x in d.call('stats','--no-stream','--format','{{json .}}').splitlines()]
        occupied=sum(_mib(x['MemUsage'].split('/')[0]) for x in running)
        if vm<7000 or vm-occupied<(6144 if a.tenants==64 else 4096):raise RuntimeError('Insufficient sampled Docker VM headroom')
        print('COHORT PREFLIGHT '+json.dumps({'vm_mib':round(vm,1),'existing_sampled_mib':round(occupied,1),'test_sampled_cap_mib':ram_cap,'test_cpu_cap_percent':cpu_cap}),flush=True)
    code=Path(__file__).resolve().parents[1];image=d.call('image','inspect','--format','{{.Id}}',a.image);owner=secrets.token_hex(12);label='max2tg.placement='+owner
    with tempfile.TemporaryDirectory(prefix='runtime-',dir=code) as td:
        root=Path(td).resolve();fixture=root/'fixture';fixture.mkdir(mode=0o700);cert=root/'cert';cert.mkdir();subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=synthetic','-addext','subjectAltName=DNS:api.oneme.ru,DNS:api.telegram.org','-keyout',str(cert/'key.pem'),'-out',str(cert/'cert.pem')],check=True,capture_output=True);(cert/'key.pem').chmod(0o644)
        plan=None;restore_plan=None;restore_owner=None;ids={};vol={};initname='m2tg-placement-'+owner+'-init';adminname='m2tg-placement-'+owner+'-receipt-admin'
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
            d.call('run','-d','--name',initname,*args,'-m',module,'init',*([str(a.tenants)] if a.native_volumes else []),*(['account-binding'] if a.account_binding else []));wait(lambda:d.inspect(initname)['State']['Status']=='exited',timeout=150 if a.tenants==64 else 100 if a.tenants==32 else 75)
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
                if a.operator_lifecycle:s['command'].append('operator')
                if a.account_binding:s['command'].append('account-binding')
                if role=='broker':
                    s['volumes'].append(bind(cert,'/cert'))
                    if a.long_poll:s['command'].append('long-poll')
                    if a.mixed:s['command'].append('mixed')
                    if a.soak:s['command'].append('soak')
                    if a.delay150:s['command'].append('delay150')
                    if a.balanced_fault:s['command'].append('balanced-fault')
                else:
                    if a.profile:s['command'].append('profile')
                    if a.wal_maintenance:s['command'].append('wal')
                    if role=='gateway' and a.balanced:s['command'].append('balanced')
                    if role=='gateway' and a.balanced_fault:s['command'].append('balanced-fault')
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
            if a.operator_lifecycle:
                from app.operator_lifecycle import SyntheticLifecycle
                from datetime import datetime
                lifecycle_root=root/'lifecycle';lifecycle_root.mkdir(mode=0o700)
                auth_baseline=[0,0]
                def probe(role,cid,state):
                    try:
                        start=datetime.fromisoformat(state['StartedAt'].replace('Z','+00:00')).timestamp()
                        if role=='broker':return (read(cid,'/network/telegram/broker-health.json') or {}).get('at',0)>=start
                        if role=='gateway':
                            snap=read(cid,'/registry/operator-health.json') or {};return snap.get('at',0)>=start and set(snap.get('sessions',{}))==set(sids) and all(v=={'active':True,'ready':True} for v in snap['sessions'].values())
                        i=sids.index(role[7:]);return counts().get('auth',[0,0])[i]>auth_baseline[i] and exec_py(cid,'from pathlib import Path;print(Path("/channel/ingress.sock").is_socket())')=='True'
                    except subprocess.CalledProcessError:return False
                def observation(role,cid,state):
                    try:
                        start=datetime.fromisoformat(state['StartedAt'].replace('Z','+00:00')).timestamp()
                        if role=='broker':snap=read(cid,'/network/telegram/broker-health.json') or {}
                        elif role=='gateway':
                            snap=read(cid,'/registry/operator-health.json') or {}
                            if set(snap.get('sessions',{}))!=set(sids) or not all(v=={'active':True,'ready':True} for v in snap['sessions'].values()):return False
                        else:
                            snap=read(broker,'/network/telegram/broker-health.json') or {}
                            if sids.index(role[7:]) not in snap.get('connected',[]) or exec_py(cid,'from pathlib import Path;print(Path("/channel/ingress.sock").is_socket())')!='True':return False
                        at=snap.get('at',0);return type(at) in (int,float) and start<=at and -1<=time.time()-at<=5
                    except subprocess.CalledProcessError:return False
                def controller():return SyntheticLifecycle(lifecycle_root,plan,d,probe,observe=observation)
                def session_action(action):
                    assert ctl.status()['state']=='STOPPED'
                    assert all(d.inspect(s['container_name'])['State']['Status']=='exited' for s in plan['services'].values())
                    mounts=[z for n,target,ro in [('registry','registry',False),('state0','state',False),('channel0','channel',False),('secret0','secret',True),('keys0','keys',True)] for z in ('--mount',f'type=volume,source={vol[n]},target=/{target}'+(',readonly' if ro else ''))]
                    return json.loads(d.call('run','--rm','--name',adminname,'--network','none','--read-only','--pull','never','--user','65532:65532','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','256m','--pids-limit','64','--label',label,*mounts,'--mount',f'type=bind,source={code / "tests"},target=/app/tests,readonly','--entrypoint','python',image,'-m','tests.operator_sessions_fixture',action,sids[0]))

                ctl=controller()
                try:
                    ctl.register();assert ctl.status()['state']=='HELD';assert ctl.start()['state']=='RUNNING'
                    touch('phase1');wait(lambda:receipts(2) and len(counts().get('tg',[]))==len(counts().get('max',[]))==2,timeout=45)
                    wait(lambda:all(ledger(cid,'/state/to-telegram.sqlite3','worker_telegram_intents')=={'SENT':1} for cid in workers),timeout=15)
                    if a.operator_recovery:
                        wait(lambda:ctl.monitor()['healthy']);assert ctl.recover_once()=='NO_ACTION'
                        for role,cid in [('broker',broker),('gateway',gateway),('worker-'+sids[0],workers[0])]:
                            auth_baseline[:]=counts()['auth'];d.call('kill','--signal','KILL',cid)
                            assert not ctl.monitor()['healthy']
                            ctl.close();ctl=controller()
                            wait(lambda:all(v['ready'] for r,v in ctl.monitor()['roles'].items() if r in ctl.order[:ctl.order.index(role)]),timeout=25)
                            assert ctl.recover_once()=='RECOVERED';wait(lambda:ctl.monitor()['healthy'],timeout=30)
                            assert ctl.monitor()['roles'][role]['attempts']==1
                            assert receipts(2) and len(counts()['tg'])==len(counts()['max'])==2
                            assert all(d.inspect(s['container_name'])['Id']==ids[r] for r,s in plan['services'].items())
                        print('OPERATOR RECOVERY PASS '+json.dumps({'recovered_roles':3,'same_ids':True,'exact_effects_unchanged':True,'persisted_attempts':3},sort_keys=True),flush=True)
                    auth_baseline[:]=counts()['auth'];assert ctl.start()['state']=='RUNNING';assert ctl.stop()['state']=='STOPPED'
                    if a.operator_sessions:
                        assert session_action('pause')=={'state':'PAUSED','receipts_unchanged':True,'other_active':True}
                        assert session_action('resume')=={'state':'ACTIVE','receipts_unchanged':True,'other_active':True}

                finally:ctl.close()
                ctl=controller()
                try:
                    assert ctl.status()['state']=='STOPPED';assert ctl.start()['state']=='RUNNING';touch('phase2');wait(lambda:receipts(4) and len(counts().get('max',[]))==4,timeout=35)
                    assert sorted(counts()['tg'])==[[-10001,'outgoing-1'],[-10000,'outgoing-0']]
                    assert sorted(counts()['max'])==sorted([[i,f'incoming-{i}-{j}'] for i in range(2) for j in range(2)])
                    assert all(d.inspect(s['container_name'])['Id']==ids[role] for role,s in plan['services'].items())
                    assert all(ledger(cid,'/state/to-telegram.sqlite3','worker_telegram_intents')=={'SENT':1} for cid in workers)
                    auth_baseline[:]=counts()['auth'];assert ctl.stop()['state']=='STOPPED'
                    if a.operator_sessions:
                        assert session_action('revoke')=={'state':'REVOKED','receipts_unchanged':True,'other_active':True}
                        print('OPERATOR SESSIONS PASS '+json.dumps({'paused':True,'resumed':True,'revoked':True,'receipts_unchanged':True,'other_active':True,'new_deliveries_after_resume':2},sort_keys=True),flush=True)
                    if a.operator_backup:
                        names=('snapshots','archive','backupkeys','anchor','restores')
                        for n in names:
                            vol[n]=d.call('volume','create','--label',label,'m2tg-placement-'+owner+'-'+n)
                            v=d.volume(vol[n]);assert v.get('Labels',{}).get('max2tg.placement')==owner and v['Driver']=='local' and not v.get('Options')
                        mounts=[z for n in names for z in ('--mount',f'type=volume,source={vol[n]},target=/{n}')]
                        base=['run','--rm','--name',adminname,'--network','none','--read-only','--pull','never','--label',label,'--memory','256m','--pids-limit','64']
                        d.call(*base,*mounts,'--entrypoint','python',image,'-c','import os;from pathlib import Path;[(os.chown(p,65532,65532),p.chmod(0o700)) for p in map(Path,'+repr(['/'+n for n in names])+')]')
                        def capture():
                            sources=[z for n in ('registry','state0','state1') for z in ('--mount',f'type=volume,source={vol[n]},target=/{n}')]
                            return json.loads(d.call(*base,'--user','65532:65532','--cap-drop','ALL','--security-opt','no-new-privileges',*mounts,*sources,'--mount',f'type=bind,source={code / "tests"},target=/app/tests,readonly','--entrypoint','python',image,'-m','tests.operator_backup_fixture',json.dumps(sids)))
                        result=ctl.checkpoint(capture);assert result['members']==11 and result['logical_rows_equal'] and result['credentials_excluded'] and ctl.status()['state']=='ARCHIVED'
                        for action in (ctl.start,ctl.stop,ctl.recover_once):
                            try:action()
                            except PermissionError:pass
                            else:raise AssertionError('Archived lifecycle reopened')
                        print('OPERATOR BACKUP PASS '+json.dumps(result,sort_keys=True),flush=True)
                        if a.operator_restore:
                            fresh=('newregistry','newstate0','newstate1','newchannel0','newchannel1')
                            for n in fresh:
                                vol[n]=d.call('volume','create','--label',label,'m2tg-placement-'+owner+'-'+n)
                                v=d.volume(vol[n]);assert v.get('Labels',{}).get('max2tg.placement')==owner and v['Driver']=='local' and not v.get('Options')
                            def mount(name,target=None,ro=False):return ['--mount',f'type=volume,source={vol[name]},target=/{target or name}'+(',readonly' if ro else '')]
                            newmounts=[z for n in fresh for z in mount(n)]
                            d.call(*base,*newmounts,'--entrypoint','python',image,'-c','import os;from pathlib import Path;[(os.chown(p,65532,65532),p.chmod(0o700)) for p in map(Path,'+repr(['/'+n for n in fresh])+')]')
                            retained=[z for n in ('secret0','secret1','keys0','keys1','botsecret','botkeys') for z in mount(n,ro=True)]
                            def offline(action):
                                assert ctl.status()['state']=='ARCHIVED'
                                old=[*mount('registry','oldregistry',True),*mount('state0','oldstate0',True),*mount('state1','oldstate1',True)] if action=='prepare' else []
                                return json.loads(d.call(*base,'--user','65532:65532','--cap-drop','ALL','--security-opt','no-new-privileges',*newmounts,*retained,*old,*mount('anchor'),*mount('restores',ro=True),'--mount',f'type=bind,source={code / "tests"},target=/app/tests,readonly','--entrypoint','python',image,'-m','tests.operator_restore_fixture',action,json.dumps(sids)))
                            assert offline('prepare')=={'prepared':True,'dirty':False}
                            restore_owner=secrets.token_hex(12);restore_plan=deepcopy(plan);restore_plan['name']='m2tg-placement-'+restore_owner
                            replacements={vol[n]:vol['new'+n] for n in ('registry','state0','state1','channel0','channel1')}
                            for role,s in restore_plan['services'].items():
                                s['container_name']='m2tg-placement-'+restore_owner+'-'+role;s['labels']['max2tg.placement']=restore_owner
                                for m in s['volumes']:
                                    if m['source'] in replacements:m['source']=replacements[m['source']]
                            restore_plan['volumes']={m['source']:{'external':True,'name':m['source']} for s in restore_plan['services'].values() for m in s['volumes'] if m['type']=='volume'}
                            restored_path=root/'restore-compose.json';restored_path.write_text(json.dumps(restore_plan));restore_cmd=('compose','-f',str(restored_path),'--profile','acceptance-only');d.call(*restore_cmd,'config','--quiet');d.call(*restore_cmd,'create','--no-build','--pull','never')
                            restored_root=root/'restored-lifecycle';restored_root.mkdir(mode=0o700)
                            new_ids={role:d.inspect(s['container_name'])['Id'] for role,s in restore_plan['services'].items()};assert not set(new_ids.values())&set(ids.values())
                            broker=new_ids['broker'];gateway=new_ids['gateway'];workers=[new_ids['worker-'+sid] for sid in sids]
                            restored_ctl=SyntheticLifecycle(restored_root,restore_plan,d,probe,observe=observation)
                            try:
                                restored_ctl.register();assert restored_ctl.status()['state']=='HELD'
                                assert offline('gate')=={'verified':True,'dirty':True,'live_launch_authorized':False}
                                assert offline('gate')=={'verified':False,'dirty':True}
                                assert restored_ctl.start()['state']=='RUNNING'
                                wait(lambda:restored_ctl.monitor()['healthy'],timeout=30)
                                assert receipts(4) and sorted(counts()['tg'])==[[-10001,'outgoing-1'],[-10000,'outgoing-0']] and len(counts()['max'])==4
                                exec_py(broker,'from pathlib import Path;Path("/network/telegram/load-count").write_text("1")')
                                wait(lambda:ledger(gateway,'/registry/inbox.sqlite3','telegram_ingress')=={'ACKED':6} and ledger(gateway,'/registry/telegram-egress.sqlite3','telegram_egress')=={'SENT':4},timeout=40)
                                wait(lambda:all(ledger(cid,'/state/to-telegram.sqlite3','worker_telegram_intents')=={'SENT':2} for cid in workers),timeout=15)
                                assert sorted(counts()['tg'])==sorted([[-10000-i,t] for i in range(2) for t in (f'outgoing-{i}',f'load-out-{i}-0')])
                                assert sorted(counts()['max'])==sorted([[i,t] for i in range(2) for t in (f'incoming-{i}-0',f'incoming-{i}-1',f'load-in-{i}-0')])
                                assert ctl.status()['state']=='ARCHIVED';assert restored_ctl.stop()['state']=='STOPPED'
                                print('OPERATOR RESTORE PASS '+json.dumps({'new_container_ids':4,'fresh_delivery_volumes':5,'old_effects_preserved':6,'new_effects':4,'dirty_anchor_reuse_refused':True,'source_archived':True},sort_keys=True),flush=True)
                            finally:restored_ctl.close()

                    print('OPERATOR LIFECYCLE PASS '+json.dumps({'containers':4,'cycles':2,'same_ids':True,'max_effects':4,'tg_effects':2,'clean_stop':True,'live_launch_authorized':False},sort_keys=True),flush=True)
                finally:ctl.close()
                return
            start_broker()
            def account_action(i,action):
                assert d.inspect(plan['services']['gateway']['container_name'])['State']['Running'] is False
                assert d.inspect(plan['services']['worker-'+sids[i]]['container_name'])['State']['Running'] is False
                mounts=[z for n,target,ro in [('registry','registry',False),(f'state{i}','state',False),(f'channel{i}','channel',False),(f'secret{i}','secret',False),(f'keys{i}','keys',True),(f'net{i}','network',True)] for z in ('--mount',f'type=volume,source={vol[n]},target=/{target}'+(',readonly' if ro else ''))]
                return json.loads(d.call('run','--rm','--name',adminname,'--network','none','--read-only','--pull','never','--user','65532:65532','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','256m','--pids-limit','64','--label',label,*mounts,'--mount',f'type=bind,source={code / "tests"},target=/app/tests,readonly','--entrypoint','python',image,'-m','tests.account_binding_fixture',action,sids[i]))
            if a.account_binding:
                for i in range(2):assert account_action(i,'bind')=={'bound':True}
                assert not counts()['tg'] and not counts()['max']
            d.start(gateway);wait(ready,timeout=90 if a.tenants==64 else 45 if a.tenants==32 else 20)
            for i,cid in enumerate(workers):
                d.start(cid)
                if a.tenants==64 and (i+1)%16==0:
                    from app.load_metrics import _mib
                    running=[json.loads(x) for x in d.call('stats','--no-stream','--format','{{json .}}',gateway,broker,*workers[:i+1]).splitlines()]
                    if sum(_mib(x['MemUsage'].split('/')[0]) for x in running)>ram_cap or sum(float(x['CPUPerc'].rstrip('%')) for x in running)>cpu_cap:raise RuntimeError('Cohort startup resource stop limit exceeded')
            touch('phase1');wait(lambda:receipts(a.tenants) and len(counts().get('tg',[]))==a.tenants and len(counts().get('max',[]))==a.tenants,timeout=120 if a.tenants==64 else 75 if a.tenants==32 else 45)
            print(f'Actual run_isolated_gateway/worker: {a.tenants}+{a.tenants} confirmed on '+('native volume' if a.native_volumes else 'bind')+' placement',flush=True)
            if a.account_binding:
                wait(lambda:all(ledger(cid,'/state/to-telegram.sqlite3','worker_telegram_intents')=={'SENT':1} for cid in workers),timeout=15)
                d.stop(workers[0]);d.stop(gateway)
                assert account_action(0,'renew')=={'foreign_refused':True,'same_account_renewed':True,'receipts_unchanged':True}
                assert sorted(counts()['tg'])==[[-10001,'outgoing-1'],[-10000,'outgoing-0']] and len(counts()['max'])==2
                baseline=counts()['auth'][0];d.start(gateway);wait(ready);d.start(workers[0]);wait(lambda:counts()['auth'][0]>baseline and 0 in (read(broker,'/network/telegram/broker-health.json') or {}).get('connected',[]))
                touch('phase2');wait(lambda:receipts(4) and len(counts()['max'])==4,timeout=35)
                exec_py(broker,'from pathlib import Path;Path("/network/telegram/load-count").write_text("1")')
                wait(lambda:ledger(gateway,'/registry/inbox.sqlite3','telegram_ingress')=={'ACKED':6} and ledger(gateway,'/registry/telegram-egress.sqlite3','telegram_egress')=={'SENT':4},timeout=40)
                wait(lambda:all(ledger(cid,'/state/to-telegram.sqlite3','worker_telegram_intents')=={'SENT':2} for cid in workers),timeout=15)
                assert sorted(counts()['tg'])==sorted([[-10000-i,t] for i in range(2) for t in (f'outgoing-{i}',f'load-out-{i}-0')])
                assert sorted(counts()['max'])==sorted([[i,t] for i in range(2) for t in (f'incoming-{i}-0',f'incoming-{i}-1',f'load-in-{i}-0')])
                print('ACCOUNT BINDING PASS '+json.dumps({'bound_accounts':2,'foreign_refused':True,'same_account_renewed':True,'previous_effects':4,'new_effects':6,'receipts_unchanged_on_renewal':True},sort_keys=True),flush=True)
                return
            if a.load or a.mixed:
                from app.load_metrics import summarize_load,_mib
                samples=[]
                def sample():
                    raw=d.call('stats','--no-stream','--format','{{json .}}',*ids.values());rows=[json.loads(x) for x in raw.splitlines()];
                    if sum(_mib(x['MemUsage'].split('/')[0]) for x in rows)>ram_cap or sum(float(x['CPUPerc'].rstrip('%')) for x in rows)>cpu_cap:raise RuntimeError('Synthetic load resource stop limit exceeded')
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
                if a.mixed:
                    from collections import Counter
                    from tests.synthetic_mixed_load import schedule,summarize
                    from app.stage_profile import aggregate_profiles
                    cycles=(2 if a.tenants==64 else 3) if a.soak else 1;waves=schedule(a.tenants,cycles);per=Counter(i for _,pairs in waves for i,j in pairs);n=sum(per.values());target=a.tenants+n
                    def resources_snapshot():
                        by_role={}
                        for role,cid in ids.items():
                            roots=['/registry'] if role=='gateway' else ['/state'] if role.startswith('worker-') else []
                            by_role[role]=json.loads(d.call('exec',cid,'python','-m','tests.soak_observe',*roots))
                        return by_role
                    checkpoints=[]
                    if a.soak or a.storage_check:checkpoints.append({'at':0,'roles':resources_snapshot()})
                    if a.tenants>=32:exec_py(gateway,'from pathlib import Path;Path("/registry/lease-observe").touch()')
                    begin=time.monotonic();touch('mixed-start');observations=[];deadline=begin+105+90*(cycles-1)
                    while True:
                        sample();last=samples[-1];result=counts()
                        ready_count=int(exec_py(gateway,'from pathlib import Path;print(sum((p/"egress.sock").is_socket() for p in Path("/channels").iterdir()))'))
                        observations.append({'at':time.monotonic()-begin,'pending_in':sum(v for k,v in last['inbox'].items() if k!='ACKED'),'pending_out':sum(v for k,v in last['egress'].items() if k!='SENT'),'ready':ready_count})
                        if (a.soak or a.storage_check) and len(checkpoints)<=cycles and time.monotonic()-begin>=32+90*(len(checkpoints)-1):
                            if observations[-1]['pending_in']==observations[-1]['pending_out']==0 and ready_count==a.tenants:
                                checkpoints.append({'at':round(time.monotonic()-begin,3),'roles':resources_snapshot()})
                        if result.get('mixed_emitted')==2*n and last['inbox']=={'ACKED':target} and last['egress']=={'SENT':target}:break
                        if time.monotonic()>deadline:raise RuntimeError('Mixed workload did not drain')
                    expected_max=[[i,f'incoming-{i}-0'] for i in range(a.tenants)]+[[i,f'mixed-in-{i}-{j}'] for _,pairs in waves for i,j in pairs]
                    expected_tg=[[-10000-i,f'outgoing-{i}'] for i in range(a.tenants)]+[[-10000-i,f'mixed-out-{i}-{j}'] for _,pairs in waves for i,j in pairs]
                    assert sorted(result['max'])==sorted(expected_max) and sorted(result['tg'])==sorted(expected_tg)
                    wait(lambda:all(ledger(cid,'/state/to-telegram.sqlite3','worker_telegram_intents')=={'SENT':per[i]+1} for i,cid in enumerate(workers)),timeout=15)
                    result=counts();
                    if a.tenants==64:
                        print("COHORT OBSERVATION "+json.dumps({"effects":len(result["mixed_effects"]),"lease":read(gateway,"/registry/lease-observation.json"),"idle_samples":[v for v in observations if any(32+90*c<=v["at"]<=50+90*c for c in range(cycles))],"verified_min":min(map(len,result["mixed_verified"].values()),default=0)},sort_keys=True),flush=True)
                    try:metrics=summarize(a.tenants,result['mixed_effects'],result['mixed_verified'],observations,cycles=cycles)
                    except ValueError:
                        # Capture the actual synthetic verification timeline on acceptance failure.
                        print('MIXED ACCEPTANCE DIAGNOSTIC '+json.dumps({'verify_times':{i:[round(t,3) for t in times] for i,times in result['mixed_verified'].items()},'effects':len(result['mixed_effects']),'idle_observations':[v for v in observations if any(32+90*c<=v['at']<=50+90*c for c in range(cycles))]},sort_keys=True),flush=True)
                        raise
                    metrics['cycles']=cycles;metrics['elapsed_s']=round(time.monotonic()-begin,3);metrics['samples']=len(samples);metrics['max_sampled_total_ram_mib']=round(max(sum(_mib(r['MemUsage'].split('/')[0]) for r in s['resources']) for s in samples),2)
                    stages=aggregate_profiles(before,profiles(2));assert stages['gateway']['api.send.call']['count']==n and stages['gateway']['ingress.ipc']['count']>=n
                    metrics['api_calls']={k:v['count'] for k,v in stages['gateway'].items() if k.startswith('api.') and k.endswith('.call')}
                    if a.soak or a.storage_check:
                        from tests.soak_observe import summarize_resources
                        checkpoints.append({'at':round(time.monotonic()-begin,3),'roles':resources_snapshot()})
                        metrics['resources']=summarize_resources(checkpoints,cycles=cycles)
                    if a.tenants>=32:
                        metrics['lease_observation']=read(gateway,'/registry/lease-observation.json')
                        assert metrics['lease_observation']['samples']>0
                        if a.tenants==64:
                            assert metrics['lease_observation']['min_ready']==64, 'Sampled lease readiness regression'
                            assert metrics['lease_observation']['min_lease_margin_ms']>=2000 and metrics['lease_observation']['below_2s']==0, 'Sampled lease margin below 2 seconds'
                    metrics['wal_maintenance']=a.wal_maintenance;metrics['balanced']=a.balanced;metrics['delay_ms']=150 if a.delay150 else 0
                    print('MIXED RESULT '+json.dumps(metrics,sort_keys=True),flush=True)
                    print('PROFILE RESULT '+json.dumps(stages,sort_keys=True),flush=True)
                    return

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
            if a.balanced_fault:
                def persisted_until():return float(exec_py(gateway,'import sqlite3;d=sqlite3.connect("file:/registry/capsules.sqlite3?mode=ro",uri=True);print(d.execute("SELECT until FROM telegram_api_budget").fetchone()[0])'))
                touch('rate-trigger');wait(lambda:counts().get('rate_until',0)>time.time() and persisted_until()>time.time(),timeout=15)
                observed=time.time();d.call('kill','--signal','KILL',gateway);d.start(gateway)
                wait(lambda:ready() and counts().get('after_rate'),timeout=35)
                rate=counts();assert not any(t>=observed for t in rate.get('early_at',[]))
                assert len(rate['tg'])==2 and len(rate['max'])==5
                print('Balanced RetryAfter: persisted cooldown across gateway kill, no calls in observed cooldown interval',flush=True)
                def central_rows():
                    return json.loads(exec_py(gateway,'import sqlite3,json;d=sqlite3.connect("file:/registry/telegram-egress.sqlite3?mode=ro",uri=True);print(json.dumps(dict(d.execute("SELECT text,status FROM telegram_egress"))))'))
                def hold_drain():
                    exec_py(gateway,'from pathlib import Path;Path("/registry/test-drain-pause").touch(mode=0o600)')
                    wait(lambda:exec_py(gateway,'from pathlib import Path;print(Path("/registry/test-drain-paused").exists())')=='True')
                def release_drain():
                    exec_py(gateway,'from pathlib import Path;Path("/registry/test-drain-pause").unlink()')
                hold_drain();touch('crash-send')
                wait(lambda:all(central_rows().get(f'crash-out-{i}-0')=='QUEUED' for i in (0,1)),timeout=20)
                release_drain()
                def crash_boundary():
                    rows=central_rows();return counts().get('crash_effect_observed') and rows.get('crash-out-0-0')=='SENDING' and rows.get('crash-out-1-0')=='SENT'
                wait(crash_boundary,timeout=15)
                d.call('kill','--signal','KILL',gateway);d.start(gateway);wait(ready,timeout=30)
                wait(lambda:ledger(workers[0],'/state/to-telegram.sqlite3','worker_telegram_intents')=={'SENT':1,'UNKNOWN':1} and ledger(workers[1],'/state/to-telegram.sqlite3','worker_telegram_intents')=={'SENT':2},timeout=20)
                assert central_rows()['crash-out-0-0']=='UNKNOWN' and central_rows()['crash-out-1-0']=='SENT'
                assert counts()['tg'].count([-10000,'crash-out-0-0'])==1 and counts()['tg'].count([-10001,'crash-out-1-0'])==1
                print('Balanced gateway killed during actual send: one UNKNOWN, other tenant SENT, no replay',flush=True)
                hold_drain();touch('fault-send')
                wait(lambda:all(central_rows().get(f'fault-out-{i}-{j}')=='QUEUED' for i in (0,1) for j in (0,1)),timeout=20)
                wait(lambda:ledger(workers[0],'/state/to-telegram.sqlite3','worker_telegram_intents')=={'ACCEPTED':2,'SENT':1,'UNKNOWN':1},timeout=15)
                release_drain()
                def revoked():return exec_py(gateway,'import sqlite3;d=sqlite3.connect("file:/registry/capsules.sqlite3?mode=ro",uri=True);print(d.execute("SELECT stage FROM shared_onboarding WHERE forum=\'-10000\'").fetchone()[0])')=='REVOKED'
                wait(revoked,timeout=20)
                during=counts();assert during.get('revocation_emitted') and not during.get('revocation_response_released'),'Revocation not observed before response release'
                wait(lambda:counts().get('revocation_response_released') and counts()['tg'].count([-10001,'fault-out-1-1'])==1,timeout=20)
                wait(lambda:ledger(workers[1],'/state/to-telegram.sqlite3','worker_telegram_intents')=={'SENT':4},timeout=15)
                wait(lambda:exec_py(gateway,f'from pathlib import Path;print(not Path("/channels/{sids[0]}/egress.sock").exists() and Path("/channels/{sids[1]}/egress.sock").is_socket())')=='True')
                result=counts();assert result.get('parallel_during_revoke') and not result.get('forbidden_calls'),result.get('tg')
                assert sorted(result['tg'])==sorted([[-10000,'outgoing-0'],[-10001,'outgoing-1'],[-10000,'crash-out-0-0'],[-10001,'crash-out-1-0'],[-10000,'fault-out-0-0'],[-10001,'fault-out-1-0'],[-10001,'fault-out-1-1']])
                out=ledger(gateway,'/registry/telegram-egress.sqlite3','telegram_egress');assert out=={'SENT':6,'UNKNOWN':1,'BLOCKED':1};rows=central_rows();assert rows['fault-out-0-0']=='SENT' and rows['fault-out-0-1']=='BLOCKED'
                assert ledger(gateway,'/registry/inbox.sqlite3','telegram_ingress')=={'ACKED':4,'UNKNOWN':1}
                print('BALANCED FAULT RESULT '+json.dumps({'cooldown_early_after_observation':0,'crash_during_send':True,'revoked_before_response':True,'other_tenant_parallel':True,'central_egress':out,'worker_b':ledger(workers[1],'/state/to-telegram.sqlite3','worker_telegram_intents'),'worker_a':ledger(workers[0],'/state/to-telegram.sqlite3','worker_telegram_intents'),'max_effects':len(result['max']),'tg_effects':len(result['tg'])},sort_keys=True),flush=True)

                if a.reconcile_revoked:
                    assert ledger(workers[0],'/state/to-telegram.sqlite3','worker_telegram_intents')=={'ACCEPTED':2,'SENT':1,'UNKNOWN':1}
                    d.call('stop','--time','5',gateway,workers[0]);assert all(d.inspect(plan['services'][role]['container_name'])['State']['Status']=='exited' for role in ('gateway','worker-'+sids[0]))
                    def reconcile_admin(apply=False):
                        args=['run','--rm','--name',adminname,'--network','none','--read-only','--pull','never','--user','65532:65532','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','256m','--pids-limit','64','--label',label,'--mount',f'type=volume,source={vol["registry"]},target=/registry','--mount',f'type=volume,source={vol["state0"]},target=/state','--entrypoint','python',image,'-m','app.revoked_receipts','--registry','/registry','--worker-state','/state','--sid',sids[0],'--provisioning-stopped']
                        return json.loads(d.call(*args,*(['--apply'] if apply else [])))
                    expected={'SENT':1,'BLOCKED':1,'UNKNOWN':0,'unresolved':0}
                    assert reconcile_admin()==dict(expected,applied=False)
                    assert reconcile_admin(True)==dict(expected,applied=True)
                    assert reconcile_admin(True)=={'SENT':0,'BLOCKED':0,'UNKNOWN':0,'unresolved':0,'applied':True}
                    # Only synthetic stopped-worker state; separate read-only probe.
                    observed=json.loads(d.call('run','--rm','--name',adminname,'--network','none','--read-only','--pull','never','--user','65532:65532','--cap-drop','ALL','--label',label,'--mount',f'type=volume,source={vol["state0"]},target=/state','--entrypoint','python',image,'-c','import sqlite3,json;d=sqlite3.connect("file:/state/to-telegram.sqlite3?mode=ro",uri=True);print(json.dumps(dict(d.execute("SELECT status,count(*) FROM worker_telegram_intents GROUP BY status"))))'))
                    assert observed=={'SENT':2,'BLOCKED':1,'UNKNOWN':1}
                    assert counts()['tg']==result['tg'] and counts()['max']==result['max']
                    print('REVOKED RECEIPTS RESULT '+json.dumps({'worker_a':observed,'central_receipt_counts':out,'external_effects_unchanged':True,'idempotent':True,'gateway_and_worker_stopped':True},sort_keys=True),flush=True)

        except Exception as exc:
            if isinstance(exc,subprocess.CalledProcessError):print(exc.stdout,exc.stderr,flush=True)
            for cid in [initname,*ids.values()]:
                try:print(cid,d.call('logs','--tail','12',cid),flush=True)
                except subprocess.CalledProcessError:pass
            raise
        finally:
            names=[initname,adminname]+([s['container_name'] for s in plan['services'].values()] if plan else [])+([s['container_name'] for s in restore_plan['services'].values()] if restore_plan else [])
            for name in names:
                c=d.inspect(name)
                if c and c['Config']['Labels'].get('max2tg.placement') in (owner,restore_owner) and c['Config']['Labels'].get('max2tg.placement'):d.call('rm','-f',c['Id'])
            assert not d.call('ps','-aq','--filter','label='+label)
            if restore_owner:assert not d.call('ps','-aq','--filter','label=max2tg.placement='+restore_owner)
            # Only remove contents of this fresh isolated mount, not a caller-supplied path.
            if a.native_volumes:
                for v in vol.values():
                    if d.volume(v).get('Labels',{}).get('max2tg.placement')==owner:d.call('volume','rm',v)
                assert not d.call('volume','ls','-q','--filter','label='+label)
                if a.load or a.mixed or a.balanced_fault or a.operator_lifecycle or a.account_binding:print('LOAD CLEANUP PASS',flush=True)
            else:subprocess.run(['docker','run','--rm',*setup_args(),'-c','import shutil;from pathlib import Path;[(shutil.rmtree(p) if p.is_dir() else p.unlink()) for p in Path("/fixture").iterdir()]'],check=True,capture_output=True,timeout=60)
    print('PLACED RUNTIME PASS; disposable resources removed',flush=True)
if __name__=='__main__':main()
