"""Actual clients + actual bridge runtimes, synthetic TLS endpoints only."""
import asyncio,ssl,json,os,sys,signal,time
from pathlib import Path
from aiohttp import web,WSMsgType
from telegram.error import NetworkError
from app.egress_tunnel import TunnelServer
from app.isolated_transport import isolated_max_client,isolated_telegram_bot
from app.telegram_budget import TelegramBudget
from app.max_wire import encode,decode
from app.max_client import OpCode
from app.capsule_registry import CapsuleRegistry
from app.shared_gateway_runtime import SharedGatewayRuntime,remove_dead_socket
from app.isolated_worker_runtime import IsolatedWorkerRuntime
from tests.isolated_container_fixture import prepare,assert_sandbox
from tests.supervised_bridge_fixture import atomic
from tests.synthetic_long_poll import UpdateWakeup
from tests.synthetic_mixed_load import schedule


def user(uid):return {'id':uid,'is_bot':uid==900,'first_name':'Synthetic','username':'synthetic_bot' if uid==900 else 'synthetic_user'}
def chat(gid):return {'id':gid,'type':'supergroup','title':'Synthetic','is_forum':True}
def message(gid,mid,text,actor=900):return {'message_id':mid,'date':1700000000,'chat':chat(gid),'from':user(actor),'message_thread_id':77,'text':text,'is_topic_message':True}

async def broker(network_paths=None,*,long_poll=False,mixed=False,cycles=1,delay=0,fault=False,account_binding=False):
    assert_sandbox();assert not any(Path(p).exists() for p in ('/keys','/secret','/registry','/keys0','/keys1','/botkeys','/botsecret'));ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain('/cert/cert.pem','/cert/key.pem');paths=network_paths or ['/net0','/net1','/nettg'];tenants=len(paths)-1;assert tenants in (2,4,8,16,32,64);control=Path(paths[-1])
    evidence=control/'counts.json';counts=json.loads(evidence.read_text()) if evidence.exists() else {'max':[],'tg':[],'auth':[0]*tenants};servers=[];load_sockets={};load_n=0
    (control/'cut-a').unlink(missing_ok=True);(control/'cut-confirmed').unlink(missing_ok=True)
    def report():atomic(control/'counts.json',counts)
    wakeup=UpdateWakeup();mixed_started=None;mixed_index=0;mixed_updates=[];mixed_at={};waves=schedule(tenants,cycles) if mixed else []
    fault_emitted=False;fault_waiting=False;crash_emitted=False
    def updates(offset):
        phase=2 if (control/'phase2').exists() else 1 if (control/'phase1').exists() else 0
        data=[{'update_id':tenants*j+i+1,'message':message(-10000-i,100+tenants*j+i,f'incoming-{i}-{j}',100+i)} for j in range(phase) for i in range(tenants) if tenants*j+i+1>=offset]
        if (control/'phase3').exists() and offset<=5:data.append({'update_id':5,'message':message(-10000,104,'ambiguous-0',100)})
        if load_n:
            data.extend({'update_id':tenants*max(1,phase)+1+tenants*j+i,'message':message(-10000-i,200+tenants*j+i,f'load-in-{i}-{j}',100+i)} for j in range(load_n) for i in range(tenants) if tenants*max(1,phase)+1+tenants*j+i>=offset)
        data.extend(u for u in mixed_updates if u["update_id"]>=offset)
        if fault and counts.get('revocation_emitted') and offset<=6:
            event=message(-10000,999,'synthetic-membership',100);event.pop('text');event['new_chat_members']=[user(777)]
            data.append({'update_id':6,'message':event})
        return data[:100]

    async def api(req):
        nonlocal fault_waiting
        method=req.match_info['method'];form=await req.post();gid=int(form.get('chat_id',-10000));uid=int(form.get('user_id',900))
        if (control/'rate-trigger').exists() and method=='getUpdates' and 'rate_until' not in counts:
            counts['rate_until']=time.time()+8;counts['early_calls']=0;report()
            return web.json_response({'ok':False,'error_code':429,'description':'synthetic rate limit','parameters':{'retry_after':8}},status=429)
        if time.time()<counts.get('rate_until',0):
            counts['early_calls']+=1;counts.setdefault('early_at',[]).append(time.time());report()
        elif 'rate_until' in counts:counts['after_rate']=True;report()
        if method=='getMe':data=user(900)
        elif method=='getChat':data={**chat(gid),'accent_color_id':0,'max_reaction_count':1,'accepted_gift_types':{'unlimited_gifts':False,'limited_gifts':False,'unique_gifts':False,'premium_subscription':False,'gifts_from_channels':False}}
        elif method=='getChatMemberCount':
            data=2
            if mixed_started is not None:
                counts['mixed_verified'].setdefault(str(-10000-gid),[]).append(time.monotonic()-mixed_started);report()
        elif method=='getChatMember':
            data={'user':user(uid),'status':'creator' if uid!=900 else 'administrator','is_anonymous':False}
            if uid==900:data.update({k:True for k in ('can_be_edited','can_manage_chat','can_delete_messages','can_manage_video_chats','can_restrict_members','can_promote_members','can_change_info','can_invite_users','can_post_stories','can_edit_stories','can_delete_stories','can_manage_topics')})
        elif method=='getUpdates':
            offset=int(form.get('offset',0));timeout=int(form.get('timeout',0))
            data=await wakeup.get(lambda:updates(offset),timeout) if long_poll else updates(offset)
        elif method=='sendMessage':
            text=form['text'];i=-10000-gid;assert 0<=i<tenants and int(form['message_thread_id'])==77 and (text==f'outgoing-{i}' or text in [f'load-out-{i}-{j}' for j in range(load_n)] or text.startswith(f'mixed-out-{i}-') and text in mixed_at or fault and text in (f'fault-out-{i}-0',f'fault-out-{i}-1',f'crash-out-{i}-0'))
            if fault and text.startswith('fault-out-'):
                if i==0 and text.endswith('-1'):
                    counts['forbidden_calls']=counts.get('forbidden_calls',0)+1;report()
                    raise AssertionError('Revoked successor invoked')
                if i==1 and fault_waiting:counts['parallel_during_revoke']=True
            counts['tg'].append([gid,text])
            if text.startswith('load-out-'):counts['load_effects'].append([text,time.time()-counts['load_started']])
            if text in mixed_at:counts['mixed_effects'].append([text,time.monotonic()-mixed_at[text],time.monotonic()-mixed_started])
            report();data=message(gid,5000+len(counts['tg']),text)
            if fault and text=='crash-out-0-0':
                counts['crash_effect_observed']=True;report();await asyncio.sleep(8)
            if fault and text=='fault-out-0-0':
                fault_waiting=True;counts['revocation_emitted']=True;report();wakeup.notify()
                try:await asyncio.sleep(2)
                finally:fault_waiting=False
                counts['revocation_response_released']=True;report()
        else:raise AssertionError('Unexpected method '+method)
        if delay and method!='getUpdates':await asyncio.sleep(delay)
        return web.json_response({'ok':True,'result':data})
    def handler(i):
        async def ws(req):
            w=web.WebSocketResponse();await w.prepare(req);authenticated=False
            async for msg in w:
                assert msg.type==WSMsgType.BINARY;pkt=decode(msg.data);op=pkt['opcode'];body=pkt['payload'];payload={}
                if op==OpCode.HANDSHAKE:assert body['deviceId']==f'synthetic-{i}'
                elif op==OpCode.AUTH_SNAPSHOT:
                    token=body['token'];allowed={f'synthetic-secret-{i}':100+i}
                    if account_binding:allowed.update({f'synthetic-renewed-{i}':100+i,f'synthetic-foreign-{i}':777})
                    assert token in allowed;authenticated=True;counts['auth'][i]+=1;report();payload={'profile':{'contact':{'id':allowed[token]}}}
                elif op==OpCode.SEND_MESSAGE:
                    assert authenticated and body['chatId']==42;text=body['message']['text'];assert text in (f'incoming-{i}-0',f'incoming-{i}-1',f'ambiguous-{i}') or text in [f'load-in-{i}-{j}' for j in range(load_n)] or text.startswith(f'mixed-in-{i}-') and text in mixed_at;counts['max'].append([i,text])
                    if text.startswith('load-in-'):counts['load_effects'].append([text,time.time()-counts['load_started']])
                    if text in mixed_at:counts['mixed_effects'].append([text,time.monotonic()-mixed_at[text],time.monotonic()-mixed_started])
                    report()
                    if text.startswith('ambiguous'):await w.close();return w
                    payload={'message':{'id':10000+len(counts['max'])}}
                await w.send_bytes(encode({'cmd':1,'seq':pkt['seq'],'opcode':op,'payload':payload}))
                if op==OpCode.AUTH_SNAPSHOT:
                    load_sockets[i]=w
                    await w.send_bytes(encode({'cmd':0,'seq':0,'opcode':OpCode.DISPATCH,'payload':{'chatId':42,'message':{'id':123,'sender':999,'text':f'outgoing-{i}'}}}))
            return w
        return ws
    runners=[]
    try:
        for i in range(tenants+1):
            app=web.Application()
            if i<tenants:app.router.add_get('/websocket',handler(i))
            else:app.router.add_post('/bot123456:synthetic/{method}',api)
            runner=web.AppRunner(app,handler_cancellation=True);await runner.setup();runners.append(runner);site=web.TCPSite(runner,'127.0.0.1',0,ssl_context=ctx);await site.start();port=site._server.sockets[0].getsockname()[1]
            async def connect(role,target,p=port):return await asyncio.open_connection('127.0.0.1',p)
            path=Path(paths[i])/'proxy.sock';await remove_dead_socket(path)
            servers.append(TunnelServer('max-text' if i<tenants else 'telegram',path,connector=connect));await servers[-1].start()
        report();atomic(control/'broker-boot.json',{'boot':sum(counts['auth'])});print('READY',flush=True)
        previous=None
        while not (control/'stop-broker').exists():
            atomic(control/'broker-health.json',{'pid':os.getpid(),'at':time.time(),'connected':sorted(i for i,w in load_sockets.items() if not w.closed)})
            if (control/'cut-a').exists() and servers[0].server:await servers[0].close();(control/'cut-confirmed').touch()
            if (control/'restore-a').exists() and not servers[0].server and (control/'cut-confirmed').exists():
                (control/'cut-a').unlink(missing_ok=True);await servers[0].start();(control/'restore-a').unlink()
            if (control/'load-count').exists() and not load_n:
                n=int((control/'load-count').read_text());assert 1<=n<=25 and set(load_sockets)==set(range(tenants)) and all(not w.closed for w in load_sockets.values())
                load_n=n;counts['load_started']=time.time();counts['load_effects']=[];report()
                for j in range(n):
                    for i,w in load_sockets.items():
                        await w.send_bytes(encode({'cmd':0,'seq':0,'opcode':OpCode.DISPATCH,'payload':{'chatId':42,'message':{'id':1000+j,'sender':999,'text':f'load-out-{i}-{j}'}}}))
            if mixed and mixed_started is None and (control/'mixed-start').exists():
                assert not load_n and set(load_sockets)==set(range(tenants)) and all(not w.closed for w in load_sockets.values())
                mixed_started=time.monotonic();counts['mixed_effects']=[];counts['mixed_verified']={};counts['mixed_emitted']=0;report()
            if mixed_started is not None:
                while mixed_index<len(waves) and time.monotonic()-mixed_started>=waves[mixed_index][0]:
                    _,pairs=waves[mixed_index]
                    for i,j in pairs:
                        uid=tenants+1+len(mixed_updates);text=f'mixed-in-{i}-{j}';mixed_at[text]=time.monotonic()
                        mixed_updates.append({'update_id':uid,'message':message(-10000-i,1000+uid,text,100+i)})
                    wakeup.notify()
                    for i,j in pairs:
                        text=f'mixed-out-{i}-{j}';mixed_at[text]=time.monotonic()
                        await load_sockets[i].send_bytes(encode({'cmd':0,'seq':0,'opcode':OpCode.DISPATCH,'payload':{'chatId':42,'message':{'id':2000+j,'sender':999,'text':text}}}))
                    mixed_index+=1;counts['mixed_emitted']=2*len(mixed_updates);report()
            if fault and not crash_emitted and (control/'crash-send').exists():
                assert set(load_sockets)=={0,1} and all(not w.closed for w in load_sockets.values())
                crash_emitted=True
                for i in (0,1):
                    await load_sockets[i].send_bytes(encode({'cmd':0,'seq':0,'opcode':OpCode.DISPATCH,'payload':{'chatId':42,'message':{'id':4000,'sender':999,'text':f'crash-out-{i}-0'}}}))
                counts['crash_emitted']=True;report()
            if fault and not fault_emitted and (control/'fault-send').exists():
                assert set(load_sockets)=={0,1} and all(not w.closed for w in load_sockets.values())
                fault_emitted=True
                for j in range(2):
                    for i in (0,1):
                        await load_sockets[i].send_bytes(encode({'cmd':0,'seq':0,'opcode':OpCode.DISPATCH,'payload':{'chatId':42,'message':{'id':3000+j,'sender':999,'text':f'fault-out-{i}-{j}'}}}))
                counts['fault_emitted']=True;report()
            state=(load_n,mixed_index,*((control/name).exists() for name in ('phase1','phase2','phase3')))
            if state!=previous:previous=state;wakeup.notify()
            await asyncio.sleep(.05)
    finally:
        for s in servers:await s.close()
        for r in runners:await r.cleanup()

async def gateway(sessions,encrypted=False):
    assert_sandbox();ctx=ssl.create_default_context(cafile='/registry/test-ca.pem');r=CapsuleRegistry('/registry')
    try:
        if encrypted:
            from app.encrypted_credentials import load_encrypted_bot
            assert sorted(p.name for p in Path('/keys').iterdir())==['bot.key'];assert sorted(p.name for p in Path('/secret').iterdir())==['telegram.enc']
            token=load_encrypted_bot('/secret',123456,'/keys/bot.key')
            for p in ('/keys/probe','/secret/probe'):
                try:Path(p).touch()
                except OSError:pass
                else:raise AssertionError('Secret/key mount writable')
            assert not any(Path(p).exists() for p in ('/keys0','/keys1','/secret0','/secret1'))
        else:token='123456:synthetic'
        async with isolated_telegram_bot('/network/proxy.sock',token,context=ctx,budget=TelegramBudget(r.db)) as bot:
            g=SharedGatewayRuntime(r,bot,channels={sid:Path(f'/channel{i}') for i,(sid,_,_) in enumerate(sessions)},external_only=True,managed=True)
            try:
                for name in ('lifecycle.json','progress.json','paused','pause'):(Path('/registry')/name).unlink(missing_ok=True)
                await g.start();print('READY',flush=True)
                while not Path('/registry/stop').exists():
                    atomic('/registry/lifecycle.json',await g.reconcile_channels())
                    atomic('/registry/gateway-health.json',{'pid':os.getpid(),'at':time.time()})
                    try:await g.poll_once()
                    except NetworkError:pass
                    await g.drain_once()
                    atomic('/registry/progress.json',{'in':g.inbox.counts(),'out':g.egress.counts(),'cooldown':g.bot.budget.remaining,'rows':{str(i):g.inbox.row(i)['status'] for i in range(1,6) if g.inbox.row(i)}})
                    if Path('/registry/pause').exists():
                        Path('/registry/paused').touch()
                        while Path('/registry/pause').exists():await asyncio.sleep(.05)
                    await asyncio.sleep(.1)
            finally:await g.close()
    finally:r.close()

async def worker(sid,encrypted=False):
    assert_sandbox();assert not Path('/registry').exists();cfg=json.loads(Path('/state/worker-acl.json').read_text());assert cfg['sid']==sid
    stop=asyncio.Event();asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set);ctx=ssl.create_default_context(cafile='/state/test-ca.pem')
    kp=None
    if encrypted:
        assert sorted(p.name for p in Path('/keys').iterdir())==['worker.key'];assert sorted(p.name for p in Path('/secret').iterdir())==['max.enc'];kp='/keys/worker.key'
        assert not any(Path(p).exists() for p in ('/keys0','/keys1','/secret0','/secret1','/botkeys','/botsecret'))
        for p in ('/keys/probe','/secret/probe'):
            try:Path(p).touch()
            except OSError:pass
            else:raise AssertionError('Secret/key mount writable')
    elif Path('/keys').exists():raise AssertionError('Unexpected key mount')
    async with isolated_max_client('/network/proxy.sock','/secret',sid,context=ctx,key_path=kp) as client:
        client.RECONNECT_SEC=0.5 # disposable fixture only
        w=IsolatedWorkerRuntime(sid,'/state','/channel','/state/key',cfg['forum'],cfg['actor'],client);heartbeat=Path('/state/heartbeat');heartbeat.unlink(missing_ok=True)
        task=asyncio.create_task(w.run(stop))
        try:
            while not stop.is_set():
                if task.done():await task;break
                if w.started:
                    heartbeat.touch() # liveness, not connection readiness
                    atomic('/state/progress.json',{'authorized':client._authorized,'in':w.ledger.counts(),'out':w.egress.counts()})
                await asyncio.sleep(.1)
        finally:
            stop.set();await task

if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='init':
        tenants=int(sys.argv[2]) if len(sys.argv)>2 else 2;assert tenants in (2,4,8,16,32,64);encrypted=Path('/botkeys').exists();prepare(encrypted=encrypted,tenants=tenants,defer_all='account-binding' in sys.argv[3:])
        if encrypted:
            from app.encrypted_storage import create_key
            from app.encrypted_credentials import install_encrypted_bot
            for p in ('/botkeys','/botsecret'):Path(p).chmod(0o700)
            create_key('/botkeys/bot.key','telegram:123456');install_encrypted_bot('/botsecret',123456,'123456:synthetic','/botkeys/bot.key')
            for p in ('/botkeys','/botsecret'):
                os.chown(p,65532,65532)
                for f in Path(p).iterdir():os.chown(f,65532,65532)
        for p in [f'/net{i}' for i in range(tenants)]+['/nettg']:os.chmod(p,0o700);os.chown(p,65532,65532)
        for p in [f'/state{i}' for i in range(tenants)]+['/registry']:
            target=Path(p)/'test-ca.pem';target.write_bytes(Path('/cert/cert.pem').read_bytes());target.chmod(0o600);os.chown(target,65532,65532)
    elif mode=='broker':asyncio.run(broker())
    elif mode in ('gateway','gateway-encrypted'):asyncio.run(gateway(json.loads(sys.argv[2]),encrypted=mode=='gateway-encrypted'))
    else:asyncio.run(worker(mode,encrypted=sys.argv[2:]==['encrypted']))
