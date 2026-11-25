"""Explicit proxy-only client lifecycle for isolated text workers/gateway.

No default personal-client change, no production entrypoint. Caller must
provide a dedicated broker socket and a network-isolated container.
"""
from contextlib import asynccontextmanager,AsyncExitStack
import ssl
import aiohttp
from telegram import Bot
from telegram.request import HTTPXRequest
from app.egress_tunnel import LoopbackProxy
from app.max_client import MaxClient
from app.worker_secrets import load_worker_secret


def verified_context(context=None):
    ctx=context if context is not None else ssl.create_default_context()
    if not isinstance(ctx,ssl.SSLContext) or not ctx.check_hostname or ctx.verify_mode!=ssl.CERT_REQUIRED:raise ValueError('Verified TLS required')
    return ctx


class ProxySessionFactory:
    def __init__(self,url,context=None):
        from urllib.parse import urlsplit
        p=urlsplit(url)
        if p.scheme!='http' or p.hostname!='127.0.0.1' or not p.port or p.username or p.password or p.path or p.query or p.fragment:raise ValueError('Dedicated loopback proxy required')
        self.url=url;self.context=verified_context(context);self.closed=False
    def __call__(self,**kwargs):
        if self.closed:raise RuntimeError('Proxy transport closed')
        if set(kwargs)-{'headers','timeout'}:raise ValueError('Unexpected session options')
        return aiohttp.ClientSession(**kwargs,proxy=self.url,trust_env=False,connector=aiohttp.TCPConnector(ssl=self.context,limit=8))


@asynccontextmanager
async def isolated_max_client(socket_path,secret_dir,sid,*,context=None):
    ctx=verified_context(context);values=load_worker_secret(secret_dir,sid);proxy=LoopbackProxy(socket_path);factory=None
    try:
        url=await proxy.start();factory=ProxySessionFactory(url,ctx)
        yield MaxClient(token=values['MAX_TOKEN'],device_id=values['MAX_DEVICE_ID'],debug=False,session_factory=factory)
    finally:
        if factory:factory.closed=True
        await proxy.close()


@asynccontextmanager
async def isolated_telegram_bot(socket_path,token,*,context=None):
    ctx=verified_context(context);proxy=LoopbackProxy(socket_path);requests=[]
    async with AsyncExitStack() as stack:
        stack.push_async_callback(proxy.close)
        url=await proxy.start()
        for pool in (8,1):
            req=HTTPXRequest(proxy=url,connection_pool_size=pool,httpx_kwargs={'trust_env':False,'verify':ctx})
            requests.append(req);stack.push_async_callback(req.shutdown)
        bot=Bot(token,request=requests[0],get_updates_request=requests[1])
        # initialize validates identity through the same tunnel; no direct fallback.
        await bot.initialize();stack.push_async_callback(bot.shutdown)
        yield bot


async def run_isolated_worker(stop,*,socket_path,secret_dir,sid,state,channel,key_file,forum,actor,context=None):
    """Trusted composition; caller owns stop event and container isolation."""
    from app.isolated_worker_runtime import IsolatedWorkerRuntime
    async with isolated_max_client(socket_path,secret_dir,sid,context=context) as client:
        runtime=IsolatedWorkerRuntime(sid,state,channel,key_file,forum,actor,client)
        await runtime.run(stop)


async def run_isolated_gateway(stop,*,socket_path,token,registry,channels,context=None):
    """Trusted composition; no public entrypoint or production supervisor."""
    from app.shared_gateway_runtime import SharedGatewayRuntime
    async with isolated_telegram_bot(socket_path,token,context=context) as bot:
        runtime=SharedGatewayRuntime(registry,bot,channels=channels,external_only=True,managed=True)
        await runtime.run(stop)
