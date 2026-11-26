"""Synthetic-only Linux bind-placement rehearsal using actual runtime loops."""
import asyncio,json,os,signal,ssl,sys,shutil
from pathlib import Path
from types import SimpleNamespace as NS
from app.capsule_registry import CapsuleRegistry
from app.stage_profile import StageProfile
from app.shared_onboarding import SharedOnboarding
from app.encrypted_storage import create_key
from app.encrypted_credentials import install_encrypted_worker,install_encrypted_bot
from app.worker_provision import prepare_worker,check_worker
from app.isolated_transport import run_isolated_gateway,run_isolated_worker
from tests.supervised_proxy_fixture import broker,assert_sandbox

UID=65532

def own(p):
    for q in p.rglob('*'):os.chown(q,UID,UID)
    os.chown(p,UID,UID)

async def prepare():
    root=Path('/fixture');data=root/'data';keys=root/'keys'
    # Empty disposable fixture only, never consume host credentials.
    assert not data.exists() and not keys.exists()
    for p in (data,keys,root/'backup'):p.mkdir(mode=0o755)
    def private(p):p.mkdir(parents=True,mode=0o700);p.chmod(0o700);return p
    registry=private(data/'registry');r=CapsuleRegistry(registry);o=SharedOnboarding(r);sessions=[];leaves=[registry]
    for i in range(2):
        actor=100+i;forum=-10000-i;req=f'{i+1:024x}';sid=o.reserve(req,actor,forum)
        class VerifyBot:
            async def get_me(self):return NS(id=900)
            async def get_chat(self,gid):return NS(id=gid,type='supergroup',is_forum=True)
            async def get_chat_member(self,gid,uid):return NS(user=NS(id=uid),status='creator' if uid==actor else 'administrator',can_manage_topics=True)
            async def get_chat_member_count(self,gid):return 2
        b=VerifyBot();await o.verify_forum(req,b)
        state,channel,secret,net,kd=[private(p) for p in (data/'workers'/sid/'state',data/'channels'/sid,data/'workers'/sid/'secret',data/'network'/sid,keys/'workers'/sid)]
        leaves.extend((state,channel,secret,net,kd));kp=kd/'worker.key';create_key(kp,'max:'+sid);install_encrypted_worker(secret,sid,{'session_id':sid,'MAX_TOKEN':f'synthetic-secret-{i}','MAX_DEVICE_ID':f'synthetic-{i}'},kp)
        key=o.ipc_key(req).read_bytes();assert prepare_worker(state,channel,secret,sid,actor,forum,key,key_path=kp)
        def check(s,a,f,k):return check_worker(state,channel,secret,s,a,f,k.read_bytes(),key_path=kp)
        o.confirm_provisioned(req,check=check);await o.activate(req,b,check=check);o.plane.bind_topic(sid,42,77,actor)
        shutil.copyfile('/cert/cert.pem',state/'test-ca.pem');(state/'test-ca.pem').chmod(0o600);sessions.append([sid,actor,forum])
    # Broker TLS test identity is deliberately independent of runtime keys.
    botsecret=private(data/'gateway-secret');botkeys=private(keys/'gateway');net=private(data/'network'/'telegram');leaves.extend((botsecret,botkeys,net));create_key(botkeys/'bot.key','telegram:123456');install_encrypted_bot(botsecret,123456,'123456:synthetic',botkeys/'bot.key');shutil.copyfile('/cert/cert.pem',registry/'test-ca.pem');(registry/'test-ca.pem').chmod(0o600)
    r.close()
    for p in leaves:own(p)
    print(json.dumps(sessions),flush=True)


def stop_event():
    stop=asyncio.Event();asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set);return stop

def profile_sink(enabled):
    if not enabled:return None
    profile=StageProfile()
    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1,lambda:print('PROFILE SNAPSHOT '+json.dumps(profile.snapshot(),sort_keys=True),flush=True))
    return profile

async def placed_worker(sid,enabled=False,wal=False):
    assert_sandbox();assert not Path('/registry').exists();cfg=json.loads(Path('/state/worker-acl.json').read_text());assert cfg['sid']==sid and 100<=cfg['actor']<164
    assert sorted(p.name for p in Path('/keys').iterdir())==['worker.key'] and sorted(p.name for p in Path('/secret').iterdir())==['max.enc']
    ctx=ssl.create_default_context(cafile='/state/test-ca.pem')
    await run_isolated_worker(stop_event(),socket_path='/network/proxy.sock',secret_dir='/secret',sid=sid,state='/state',channel='/channel',key_file='/state/key',forum=cfg['forum'],actor=cfg['actor'],context=ctx,secret_key_path='/keys/worker.key',profile=profile_sink(enabled),wal_maintenance=wal)

async def placed_gateway(sessions,enabled=False,wal=False,balanced=False,fault=False,operator=False):
    assert_sandbox();assert sorted(p.name for p in Path('/keys').iterdir())==['bot.key'];assert sorted(p.name for p in Path('/secret').iterdir())==['telegram.enc'];r=CapsuleRegistry('/registry')
    try:
        if operator:
            from app.shared_gateway_runtime import SharedGatewayRuntime
            from tests.supervised_bridge_fixture import atomic
            original_snapshot=SharedGatewayRuntime.lifecycle_snapshot
            def operator_snapshot(runtime):
                result=original_snapshot(runtime);atomic(Path('/registry/operator-health.json'),result);return result
            SharedGatewayRuntime.lifecycle_snapshot=operator_snapshot
        if fault:
            from app.shared_gateway_runtime import SharedGatewayRuntime
            original_drain=SharedGatewayRuntime.drain_once
            pause=Path('/registry/test-drain-pause');paused=Path('/registry/test-drain-paused')
            async def controlled_drain(runtime):
                if pause.exists():
                    paused.touch(mode=0o600)
                    return {'ingress':{},'egress':{}}
                paused.unlink(missing_ok=True)
                return await original_drain(runtime)
            SharedGatewayRuntime.drain_once=controlled_drain
        if len(sessions) in (32,64):
            # Fixture-only observation of actual lease-aware lifecycle result,
            # not a replacement for the authorization or dispatch check.
            from app.shared_gateway_runtime import SharedGatewayRuntime
            from tests.supervised_bridge_fixture import atomic
            original=SharedGatewayRuntime.lifecycle_snapshot
            tally={'samples':0,'min_ready':len(sessions),'all_ready':0};marker=Path('/registry/lease-observe')
            def snapshot(runtime):
                result=original(runtime)
                if marker.exists():
                    ready=sum(v['ready'] for v in result['sessions'].values());tally['samples']+=1;tally['min_ready']=min(tally['min_ready'],ready);tally['all_ready']+=ready==len(sessions)
                    if len(sessions)==64:
                        now=runtime.guard.clock()
                        margin=min(runtime.guard.valid.get(sid,now)-now for sid,_,_ in sessions)
                        value=round(margin*1000)
                        tally['min_lease_margin_ms']=min(value,tally.get('min_lease_margin_ms',value))
                        tally['below_2s']=tally.get('below_2s',0)+(value<2000)
                    atomic(Path('/registry/lease-observation.json'),tally)
                return result
            SharedGatewayRuntime.lifecycle_snapshot=snapshot
        await run_isolated_gateway(stop_event(),socket_path='/network/proxy.sock',registry=r,channels={sid:Path('/channels')/sid for sid,_,_ in sessions},context=ssl.create_default_context(cafile='/registry/test-ca.pem'),secret_dir='/secret',secret_key_path='/keys/bot.key',bot_id=123456,profile=profile_sink(enabled),wal_maintenance=wal,balanced=balanced,experimental_64=len(sessions)==64)
    finally:r.close()

async def placed_broker(sessions,args):
    marker=Path('/network/telegram/stop-broker')
    if 'operator' in args:
        marker.unlink(missing_ok=True)
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,marker.touch)
    await broker(['/network/'+sid for sid,_,_ in sessions]+['/network/telegram'],long_poll='long-poll' in args,mixed='mixed' in args,cycles=(2 if len(sessions)==64 else 3) if 'soak' in args else 1,delay=.15 if 'delay150' in args else 0,fault='balanced-fault' in args)

if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='init':asyncio.run(prepare())
    elif mode=='broker':
        asyncio.run(placed_broker(json.loads(sys.argv[2]),sys.argv[3:]))
    elif mode=='gateway':asyncio.run(placed_gateway(json.loads(sys.argv[2]),'profile' in sys.argv[3:],'wal' in sys.argv[3:],'balanced' in sys.argv[3:],'balanced-fault' in sys.argv[3:],'operator' in sys.argv[3:]))
    elif mode=='worker':asyncio.run(placed_worker(sys.argv[2],'profile' in sys.argv[3:],'wal' in sys.argv[3:]))
    else:raise SystemExit(78)
