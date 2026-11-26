"""Explicit proxy-only client lifecycle for isolated text workers/gateway.

No default personal-client change, no production entrypoint. Caller must
provide a dedicated broker socket and a network-isolated container.
"""
from contextlib import asynccontextmanager,AsyncExitStack
import ssl,asyncio
import aiohttp
from telegram import Bot
from telegram.request import HTTPXRequest
from app.egress_tunnel import LoopbackProxy
from app.max_client import MaxClient
from app.worker_secrets import load_worker_secret
from app.telegram_budget import TelegramBudget,BudgetedBot,ApiDeferred
from telegram.error import RetryAfter


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
async def isolated_max_client(socket_path,secret_dir,sid,*,context=None,key_path=None,require_account_binding=False):
    from app.max_account_binding import bound_viewer
    if require_account_binding and key_path is None:raise PermissionError('Encrypted account binding required')
    viewer=bound_viewer(secret_dir,sid,key_path,required=require_account_binding) if key_path is not None else None
    ctx=verified_context(context);values=load_worker_secret(secret_dir,sid,key_path=key_path);proxy=LoopbackProxy(socket_path);factory=None
    try:
        url=await proxy.start();factory=ProxySessionFactory(url,ctx)
        yield MaxClient(token=values['MAX_TOKEN'],device_id=values['MAX_DEVICE_ID'],debug=False,session_factory=factory,expected_viewer=viewer)
    finally:
        if factory:factory.closed=True
        await proxy.close()


@asynccontextmanager
async def isolated_telegram_bot(socket_path,token,*,context=None,budget=None,pool_size=8):
    ctx=verified_context(context);proxy=LoopbackProxy(socket_path);requests=[]
    async with AsyncExitStack() as stack:
        stack.push_async_callback(proxy.close)
        url=await proxy.start()
        if pool_size not in (8,12):raise ValueError("Invalid request pool")
        for pool in (pool_size,1):
            req=HTTPXRequest(proxy=url,connection_pool_size=pool,httpx_kwargs={'trust_env':False,'verify':ctx})
            requests.append(req);stack.push_async_callback(req.shutdown)
        bot=Bot(token,request=requests[0],get_updates_request=requests[1])
        # initialize validates identity through the same tunnel; no direct fallback.
        if budget is None:await bot.initialize()
        else:
            # Initialization performs a read-only getMe; honor persisted cooldown
            # before making it, without consuming the dependency crash budget.
            while True:
                try:await budget.call(bot.initialize,metric="initialize");break
                except (ApiDeferred,RetryAfter):await asyncio.sleep(max(.05,min(1,budget.remaining)))
        stack.push_async_callback(bot.shutdown)
        yield BudgetedBot(bot,budget) if budget is not None else bot


async def run_isolated_worker(stop,*,socket_path,secret_dir,sid,state,channel,key_file,forum,actor,context=None,secret_key_path=None,profile=None,wal_maintenance=False,require_account_binding=False):
    """Trusted composition; caller owns stop event and container isolation."""
    from app.isolated_worker_runtime import IsolatedWorkerRuntime
    async with isolated_max_client(socket_path,secret_dir,sid,context=context,key_path=secret_key_path,require_account_binding=require_account_binding) as client:
        runtime=IsolatedWorkerRuntime(sid,state,channel,key_file,forum,actor,client)
        runtime.profile=profile;runtime.listener.profile=profile
        if wal_maintenance:
            try:runtime.enable_wal_maintenance()
            except BaseException:
                await runtime.close();raise
        await runtime.run(stop)


async def run_isolated_gateway(stop,*,socket_path,registry,channels,context=None,token=None,secret_dir=None,secret_key_path=None,bot_id=None,profile=None,wal_maintenance=False,balanced=False,experimental_64=False):
    """Trusted composition; no public entrypoint or production supervisor."""
    from app.shared_gateway_runtime import SharedGatewayRuntime
    if any(x is not None for x in (secret_dir,secret_key_path,bot_id)):
        if token is not None or any(x is None for x in (secret_dir,secret_key_path,bot_id)):raise ValueError('Explicit encrypted gateway credentials required')
        from app.encrypted_credentials import load_encrypted_bot
        token=load_encrypted_bot(secret_dir,bot_id,secret_key_path)
    elif token is None:raise ValueError('Gateway credentials required')
    if experimental_64 and (not balanced or len(channels)!=64):raise ValueError('Explicit 64-session balanced rehearsal required')
    if balanced:
        from app.weighted_budget import WeightedBudget
        if not 1<=len(channels)<= (64 if experimental_64 else 32):raise ValueError('Experimental cohort limit exceeded')
        budget=WeightedBudget(registry.db,profile=profile,verify_weight=4 if experimental_64 else 2)
    else:budget=TelegramBudget(registry.db,profile=profile)
    async with isolated_telegram_bot(socket_path,token,context=context,budget=budget,pool_size=12 if balanced else 8) as bot:
        runtime=SharedGatewayRuntime(registry,bot,channels=channels,external_only=True,managed=True,profile=profile,balanced=balanced)
        if wal_maintenance:
            try:runtime.enable_wal_maintenance()
            except BaseException:
                await runtime.close();raise
        await runtime.run(stop)


async def probe_max_identity(socket_path,values,*,context=None):
    """Trusted proxy-only authentication probe, no worker, journals or handlers."""
    ctx=verified_context(context);proxy=LoopbackProxy(socket_path);factory=None;task=None
    try:
        url=await proxy.start();factory=ProxySessionFactory(url,ctx)
        client=MaxClient(token=values['MAX_TOKEN'],device_id=values['MAX_DEVICE_ID'],debug=False,session_factory=factory)
        ready=asyncio.get_running_loop().create_future()
        async def observed(_):
            if not ready.done() and client._authorized:ready.set_result(client._my_id)
        client.on_ready(observed);task=asyncio.create_task(client.run())
        done,_=await asyncio.wait((ready,task),timeout=10,return_when=asyncio.FIRST_COMPLETED)
        if ready not in done or not client._authorized or client._my_id!=ready.result():raise PermissionError('MAX identity not confirmed')
        return ready.result()
    finally:
        if task:task.cancel();await asyncio.gather(task,return_exceptions=True)
        if factory:factory.closed=True
        await proxy.close()
