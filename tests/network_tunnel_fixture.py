"""No-network Docker fixture: real TLS/HTTPS/WSS over Unix channel."""
import asyncio,os,ssl,sys
from pathlib import Path
import aiohttp,httpx
from aiohttp import web
from app.egress_tunnel import TunnelServer,LoopbackProxy
from tests.isolated_container_fixture import assert_sandbox

async def broker():
    assert_sandbox();ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain('/cert/cert.pem','/cert/key.pem')
    async def hello(req):return web.json_response({'synthetic':True})
    async def ws(req):
        result=web.WebSocketResponse();await result.prepare(req)
        async for msg in result:
            await result.send_str(msg.data)
        return result
    app=web.Application();app.router.add_get('/hello',hello);app.router.add_get('/websocket',ws);runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0,ssl_context=ctx);await site.start();port=site._server.sockets[0].getsockname()[1]
    calls=[]
    async def local(role,target):
        calls.append((role,target));return await asyncio.open_connection('127.0.0.1',port)
    servers=[TunnelServer(role,f'/channel{i}/egress.sock',connector=local) for i,role in enumerate(('max-text','telegram'))]
    try:
        for s in servers:await s.start()
        print('READY',flush=True)
        for _ in range(300):
            if all(Path(f'/channel{i}/done').is_file() for i in range(2)):break
            await asyncio.sleep(.05)
        assert all(Path(f'/channel{i}/done').is_file() for i in range(2))
        assert set(calls)=={('max-text','api.oneme.ru:443'),('telegram','api.telegram.org:443')}
        print('BROKER PASS: only role-approved connections',flush=True)
    finally:
        for s in servers:await s.close()
        await runner.cleanup()

async def client(role):
    assert_sandbox();assert not Path('/channel0').exists() and not Path('/channel1').exists() and not Path('/cert/key.pem').exists()
    ctx=ssl.create_default_context(cafile='/cert/cert.pem');assert ctx.check_hostname and ctx.verify_mode==ssl.CERT_REQUIRED
    host='api.oneme.ru' if role=='max-text' else 'api.telegram.org';foreign='api.telegram.org' if role=='max-text' else 'api.oneme.ru'
    proxy=LoopbackProxy('/channel/egress.sock');url=await proxy.start()
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
        Path('/channel/done').write_text('pass');print(role+' PASS: verified TLS, forbidden destinations denied',flush=True)
    finally:await proxy.close()

if __name__=='__main__':
    if sys.argv[1]=='init':
        for p in ('/channel0','/channel1'):os.chmod(p,0o700);os.chown(p,65532,65532)
    elif sys.argv[1]=='broker':asyncio.run(broker())
    else:asyncio.run(client(sys.argv[1]))
