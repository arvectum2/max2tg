"""No-network Docker fixture: real TLS/HTTPS/WSS over Unix channel."""
import asyncio,os,ssl,sys,time
from pathlib import Path
import aiohttp,httpx
from aiohttp import web
from app.egress_tunnel import TunnelServer,LoopbackProxy
from app.isolated_transport import isolated_max_client,isolated_telegram_bot
from app.worker_secrets import install_worker_secret
from app.max_wire import encode,decode
from app.max_client import OpCode
from telegram.error import NetworkError
from tests.isolated_container_fixture import assert_sandbox

async def broker(pinned=False):
    assert_sandbox();ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain('/cert/cert.pem','/cert/key.pem')
    async def hello(req):return web.json_response({'synthetic':True})
    evidence={'handshake':0,'auth':0,'max_send':0,'getMe':0,'getUpdates':0,'sendMessage':0}
    async def api(req):
        method=req.match_info['method'];assert method in ('getMe','getUpdates','sendMessage');evidence[method]+=1
        user={'id':900,'is_bot':True,'first_name':'Synthetic','username':'synthetic_bot'}
        if method=='getMe':data=user
        elif method=='getUpdates':data=[]
        else:
            form=await req.post();assert form['text']=='synthetic tg' and int(form['chat_id'])==-10001
            data={'message_id':17,'date':1700000000,'chat':{'id':-10001,'type':'supergroup'},'text':'synthetic tg'}
        return web.json_response({'ok':True,'result':data})
    async def ws(req):
        result=web.WebSocketResponse();await result.prepare(req)
        async for msg in result:
            if msg.type==aiohttp.WSMsgType.TEXT:await result.send_str(msg.data);continue
            pkt=decode(msg.data);op=pkt['opcode']
            if op==OpCode.HANDSHAKE:evidence['handshake']+=1;assert pkt['payload']['deviceId']=='synthetic-device';payload={}
            elif op==OpCode.AUTH_SNAPSHOT:evidence['auth']+=1;assert pkt['payload']['token']=='synthetic-max';payload={'profile':{'contact':{'id':101}}}
            elif op==OpCode.SEND_MESSAGE:evidence['max_send']+=1;assert pkt['payload']['chatId']==42 and pkt['payload']['message']['text']=='synthetic max';payload={'message':{'id':12345}}
            else:payload={}
            await result.send_bytes(encode({'cmd':1,'seq':pkt['seq'],'opcode':op,'payload':payload}))
        return result
    app=web.Application();app.router.add_get('/hello',hello);app.router.add_get('/websocket',ws);app.router.add_post('/bot123456:synthetic/{method}',api);runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0,ssl_context=ctx);await site.start();port=site._server.sockets[0].getsockname()[1]
    calls=[]
    async def local(role,target):
        calls.append((role,target));return await asyncio.open_connection('127.0.0.1',port)
    task=None;stop=asyncio.Event()
    if pinned:
        from app.linux_egress_policy import BrokerPins
        from app.pinned_broker import PinnedBroker
        pins=BrokerPins.parse({'max-text':['8.8.8.8'],'telegram':['9.9.9.9'],'expires_at':int(time.time())+120})
        async def dial(address,port,*,family,flags):
            import socket
            assert family==socket.AF_INET and flags==socket.AI_NUMERICHOST and port==443
            role={'8.8.8.8':'max-text','9.9.9.9':'telegram'}[address];target={'max-text':'api.oneme.ru:443','telegram':'api.telegram.org:443'}[role]
            return await local(role,target)
        controller=PinnedBroker(pins,{'a'*24:'/channel0','telegram':'/channel1'},dial=dial)
        servers=list(controller.servers.values())
    else:servers=[TunnelServer(role,f'/channel{i}/proxy.sock',connector=local) for i,role in enumerate(('max-text','telegram'))]
    try:
        if pinned:
            task=asyncio.create_task(controller.serve(stop));await asyncio.wait_for(controller.ready.wait(),3)
        else:
            for s in servers:await s.start()
        print('READY',flush=True)
        for _ in range(300):
            for i,server in enumerate(servers):
                if Path(f'/channel{i}/disconnect').exists() and not Path(f'/channel{i}/disconnected').exists():
                    await server.close();Path(f'/channel{i}/disconnected').touch()
            if all(Path(f'/channel{i}/done').is_file() for i in range(2)):break
            await asyncio.sleep(.05)
        assert all(Path(f'/channel{i}/done').is_file() for i in range(2))
        assert set(calls)=={('max-text','api.oneme.ru:443'),('telegram','api.telegram.org:443')}
        assert evidence=={'handshake':1,'auth':1,'max_send':1,'getMe':1,'getUpdates':1,'sendMessage':1},evidence
        print('BROKER PASS: actual client handshake/auth/send + Bot API receipts; no fallback',flush=True)
    finally:
        if task:
            stop.set();assert await task=='STOPPED';assert not controller.ready.is_set()
        else:
            for s in servers:await s.close()
        await runner.cleanup()
    if pinned:print('PINNED BROKER PASS: numeric role pins, verified TLS, separate clients',flush=True)

async def client(role):
    assert_sandbox();assert not Path('/channel0').exists() and not Path('/channel1').exists() and not Path('/cert/key.pem').exists()
    ctx=ssl.create_default_context(cafile='/cert/cert.pem');assert ctx.check_hostname and ctx.verify_mode==ssl.CERT_REQUIRED
    host='api.oneme.ru' if role=='max-text' else 'api.telegram.org';foreign='api.telegram.org' if role=='max-text' else 'api.oneme.ru'
    proxy=LoopbackProxy('/channel/proxy.sock');url=await proxy.start()
    try:
        # All container network interfaces except loopback are down.
        if role=='max-text':
            async with aiohttp.ClientSession(trust_env=False,timeout=aiohttp.ClientTimeout(total=5)) as s:
                async with s.ws_connect('wss://'+host+'/websocket',proxy=url,ssl=ctx) as ws:
                    await ws.send_str('synthetic');assert (await ws.receive()).data=='synthetic'
                for target in (foreign,'127.0.0.1','unapproved.invalid'):
                    try:await s.get('https://'+target+'/hello',proxy=url,ssl=ctx)
                    except aiohttp.ClientError:pass
                    else:raise AssertionError('Unexpected allowed destination')
                try:await s.get('https://'+host+'/hello',proxy=url)
                except aiohttp.ClientError:pass
                else:raise AssertionError('Untrusted certificate accepted')
        else:
            async with httpx.AsyncClient(proxy=url,verify=ctx,trust_env=False,timeout=5) as s:
                response=await s.get('https://'+host+'/hello');assert response.json()=={'synthetic':True}
                try:await s.get('https://'+foreign+'/hello')
                except httpx.ProxyError:pass
                else:raise AssertionError('Foreign role permitted')
        async def disconnect():
            Path('/channel/disconnect').touch()
            for _ in range(100):
                if Path('/channel/disconnected').exists():return
                await asyncio.sleep(.05)
            raise AssertionError('Broker did not disconnect')
        if role=='max-text':
            sid='a'*24
            async with isolated_max_client('/channel/proxy.sock','/secret',sid,context=ctx) as max_client:
                ready=asyncio.Event()
                @max_client.on_ready
                async def on_ready(_):ready.set()
                task=asyncio.create_task(max_client.run())
                try:
                    await asyncio.wait_for(ready.wait(),5);assert max_client._authorized
                    receipt=await max_client.send_message(42,'synthetic max');assert receipt['message']['id']==12345
                finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
                await disconnect()
                async with max_client._new_session(timeout=aiohttp.ClientTimeout(total=3)) as s:
                    try:await s.get('https://api.oneme.ru/hello')
                    except aiohttp.ClientError:pass
                    else:raise AssertionError('Unexpected success without broker')
        else:
            async with isolated_telegram_bot('/channel/proxy.sock','123456:synthetic',context=ctx) as bot:
                assert bot.id==900;assert not await bot.get_updates(timeout=0)
                receipt=await bot.send_message(chat_id=-10001,text='synthetic tg');assert receipt.message_id==17 and receipt.chat.id==-10001
                await disconnect()
                try:await bot.get_me()
                except NetworkError:pass
                else:raise AssertionError('Unexpected success without broker')
        Path('/channel/done').write_text('pass');print(role+' PASS: actual client receipt, verified TLS, no fallback',flush=True)
    finally:await proxy.close()

if __name__=='__main__':
    if sys.argv[1]=='init':
        for p in ('/channel0','/channel1'):os.chmod(p,0o700);os.chown(p,65532,65532)
        os.chmod('/secret',0o700);install_worker_secret('/secret','a'*24,{'session_id':'a'*24,'MAX_TOKEN':'synthetic-max','MAX_DEVICE_ID':'synthetic-device'});os.chown('/secret/max.json',65532,65532);os.chown('/secret',65532,65532)
    elif sys.argv[1] in ('broker','broker-pinned'):asyncio.run(broker(sys.argv[1]=='broker-pinned'))
    else:asyncio.run(client(sys.argv[1]))
