"""Actual clients + actual bridge runtimes, synthetic TLS endpoints only."""
import asyncio,ssl,json,os,sys,signal
from pathlib import Path
from aiohttp import web,WSMsgType
from telegram.error import NetworkError
from app.egress_tunnel import TunnelServer
from app.isolated_transport import isolated_max_client,isolated_telegram_bot
from app.max_wire import encode,decode
from app.max_client import OpCode
from app.capsule_registry import CapsuleRegistry
from app.shared_gateway_runtime import SharedGatewayRuntime
from app.isolated_worker_runtime import IsolatedWorkerRuntime
from tests.isolated_container_fixture import prepare,assert_sandbox
from tests.supervised_bridge_fixture import atomic


def user(uid):return {'id':uid,'is_bot':uid==900,'first_name':'Synthetic','username':'synthetic_bot' if uid==900 else 'synthetic_user'}
def chat(gid):return {'id':gid,'type':'supergroup','title':'Synthetic','is_forum':True}
def message(gid,mid,text,actor=900):return {'message_id':mid,'date':1700000000,'chat':chat(gid),'from':user(actor),'message_thread_id':77,'text':text,'is_topic_message':True}

async def broker():
    assert_sandbox();ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain('/cert/cert.pem','/cert/key.pem');control=Path('/nettg')
    counts={'max':[],'tg':[],'auth':[0,0]};servers=[]
    def report():atomic(control/'counts.json',counts)
    async def api(req):
        method=req.match_info['method'];form=await req.post();gid=int(form.get('chat_id',-10000));uid=int(form.get('user_id',900))
        if method=='getMe':data=user(900)
        elif method=='getChat':data={**chat(gid),'accent_color_id':0,'max_reaction_count':1,'accepted_gift_types':{'unlimited_gifts':False,'limited_gifts':False,'unique_gifts':False,'premium_subscription':False,'gifts_from_channels':False}}
        elif method=='getChatMemberCount':data=2
        elif method=='getChatMember':
            data={'user':user(uid),'status':'creator' if uid!=900 else 'administrator','is_anonymous':False}
            if uid==900:data.update({k:True for k in ('can_be_edited','can_manage_chat','can_delete_messages','can_manage_video_chats','can_restrict_members','can_promote_members','can_change_info','can_invite_users','can_post_stories','can_edit_stories','can_delete_stories','can_manage_topics')})
        elif method=='getUpdates':
            phase=2 if (control/'phase2').exists() else 1 if (control/'phase1').exists() else 0
            data=[{'update_id':2*j+i+1,'message':message(-10000-i,100+2*j+i,f'incoming-{i}-{j}',100+i)} for j in range(phase) for i in range(2) if 2*j+i+1>=int(form.get('offset',0))]
            if (control/'phase3').exists() and int(form.get('offset',0))<=5:data.append({'update_id':5,'message':message(-10000,104,'ambiguous-0',100)})
        elif method=='sendMessage':
            text=form['text'];assert int(form['message_thread_id'])==77 and text==f'outgoing-{-10000-gid}';counts['tg'].append([gid,text]);report();data=message(gid,5000+len(counts['tg']),text)
        else:raise AssertionError('Unexpected method '+method)
        return web.json_response({'ok':True,'result':data})
    def handler(i):
        async def ws(req):
            w=web.WebSocketResponse();await w.prepare(req);authenticated=False
            async for msg in w:
                assert msg.type==WSMsgType.BINARY;pkt=decode(msg.data);op=pkt['opcode'];body=pkt['payload'];payload={}
                if op==OpCode.HANDSHAKE:assert body['deviceId']==f'synthetic-{i}'
                elif op==OpCode.AUTH_SNAPSHOT:
                    assert body['token']==f'synthetic-secret-{i}';authenticated=True;counts['auth'][i]+=1;report();payload={'profile':{'contact':{'id':100+i}}}
                elif op==OpCode.SEND_MESSAGE:
                    assert authenticated and body['chatId']==42;text=body['message']['text'];assert text in (f'incoming-{i}-0',f'incoming-{i}-1',f'ambiguous-{i}');counts['max'].append([i,text]);report()
                    if text.startswith('ambiguous'):await w.close();return w
                    payload={'message':{'id':10000+len(counts['max'])}}
                await w.send_bytes(encode({'cmd':1,'seq':pkt['seq'],'opcode':op,'payload':payload}))
                if op==OpCode.AUTH_SNAPSHOT:
                    await w.send_bytes(encode({'cmd':0,'seq':0,'opcode':OpCode.DISPATCH,'payload':{'chatId':42,'message':{'id':123,'sender':999,'text':f'outgoing-{i}'}}}))
            return w
        return ws
    runners=[]
    try:
        for i in range(3):
            app=web.Application()
            if i<2:app.router.add_get('/websocket',handler(i))
            else:app.router.add_post('/bot123456:synthetic/{method}',api)
            runner=web.AppRunner(app);await runner.setup();runners.append(runner);site=web.TCPSite(runner,'127.0.0.1',0,ssl_context=ctx);await site.start();port=site._server.sockets[0].getsockname()[1]
            async def connect(role,target,p=port):return await asyncio.open_connection('127.0.0.1',p)
            servers.append(TunnelServer('max-text' if i<2 else 'telegram',f'/net{i}/proxy.sock' if i<2 else '/nettg/proxy.sock',connector=connect));await servers[-1].start()
        report();print('READY',flush=True)
        while not (control/'stop-broker').exists():
            if (control/'cut-a').exists() and servers[0].server:await servers[0].close();(control/'cut-confirmed').touch()
            if (control/'restore-a').exists() and not servers[0].server and (control/'cut-confirmed').exists():
                (control/'cut-a').unlink(missing_ok=True);await servers[0].start();(control/'restore-a').unlink()
            await asyncio.sleep(.05)
    finally:
        for s in servers:await s.close()
        for r in runners:await r.cleanup()

async def gateway(sessions):
    assert_sandbox();ctx=ssl.create_default_context(cafile='/registry/test-ca.pem');r=CapsuleRegistry('/registry')
    try:
        async with isolated_telegram_bot('/network/proxy.sock','123456:synthetic',context=ctx) as bot:
            g=SharedGatewayRuntime(r,bot,channels={sid:Path(f'/channel{i}') for i,(sid,_,_) in enumerate(sessions)},external_only=True,managed=True)
            try:
                await g.start();print('READY',flush=True)
                while not Path('/registry/stop').exists():
                    atomic('/registry/lifecycle.json',await g.reconcile_channels())
                    try:await g.poll_once()
                    except NetworkError:pass
                    await g.drain_once()
                    atomic('/registry/progress.json',{'in':g.inbox.counts(),'out':g.egress.counts(),'rows':{str(i):g.inbox.row(i)['status'] for i in range(1,6) if g.inbox.row(i)}})
                    await asyncio.sleep(.1)
            finally:await g.close()
    finally:r.close()

async def worker(sid):
    assert_sandbox();assert not Path('/registry').exists();cfg=json.loads(Path('/state/worker-acl.json').read_text());assert cfg['sid']==sid
    stop=asyncio.Event();asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set);ctx=ssl.create_default_context(cafile='/state/test-ca.pem')
    async with isolated_max_client('/network/proxy.sock','/secret',sid,context=ctx) as client:
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
        prepare()
        for p in ('/net0','/net1','/nettg'):os.chmod(p,0o700);os.chown(p,65532,65532)
        for p in ('/state0','/state1','/registry'):
            target=Path(p)/'test-ca.pem';target.write_bytes(Path('/cert/cert.pem').read_bytes());target.chmod(0o600);os.chown(target,65532,65532)
    elif mode=='broker':asyncio.run(broker())
    elif mode=='gateway':asyncio.run(gateway(json.loads(sys.argv[2])))
    else:asyncio.run(worker(mode))
