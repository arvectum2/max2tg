"""Proxy-only composition contracts, no live network or secrets."""
import asyncio,ssl,tempfile
from pathlib import Path
import aiohttp
import pytest
from app.isolated_transport import ProxySessionFactory,verified_context,isolated_telegram_bot,isolated_max_client
from app.worker_secrets import install_worker_secret
from app.max_client import MaxClient
from app.egress_tunnel import read_connect
from app.egress_policy import EgressDenied
from telegram.error import NetworkError


def test_transport_requires_tls_and_loopback():
    ctx=ssl._create_unverified_context()
    with pytest.raises(ValueError):verified_context(ctx)
    for url in ('https://127.0.0.1:8','http://localhost:8','http://192.168.0.1:8','http://127.0.0.1:8/path','http://u:p@127.0.0.1:8','http://127.0.0.1'):
        with pytest.raises(ValueError):ProxySessionFactory(url)
    assert verified_context().check_hostname

@pytest.mark.asyncio
async def test_personal_factory_unchanged_and_isolated_closed(monkeypatch,tmp_path):
    calls=[]
    def factory(**kw):calls.append(kw);return 'personal-session'
    monkeypatch.setattr(aiohttp,'ClientSession',factory)
    client=MaxClient('synthetic','device');assert client._new_session(headers={})=='personal-session';assert calls==[{'headers':{}}]
    p=tmp_path/'secret';p.mkdir(mode=0o700);sid='a'*24;install_worker_secret(p,sid,{'session_id':sid,'MAX_TOKEN':'synthetic','MAX_DEVICE_ID':'device'})
    async with isolated_max_client(tmp_path/'absent.sock',p,sid) as m:
        assert m._session_factory.url.startswith('http://127.0.0.1:')
        with pytest.raises(ValueError):m._new_session(trust_env=True)
    with pytest.raises(RuntimeError):m._new_session()

@pytest.mark.asyncio
async def test_no_proxy_env_bypass_or_fallback(monkeypatch):
    hit=asyncio.Event()
    async def decoy(r,w):hit.set();w.close();await w.wait_closed()
    server=await asyncio.start_server(decoy,'127.0.0.1',0);port=server.sockets[0].getsockname()[1]
    monkeypatch.setenv('NO_PROXY','*');monkeypatch.setenv('HTTPS_PROXY',f'http://127.0.0.1:{port}')
    from app.egress_tunnel import LoopbackProxy
    with tempfile.TemporaryDirectory(prefix='m2px-',dir='/tmp') as root:
        proxy=LoopbackProxy(Path(root)/'missing.sock');url=await proxy.start();factory=ProxySessionFactory(url)
        try:
            async with factory(timeout=aiohttp.ClientTimeout(total=2)) as s:
                assert not s.trust_env
                with pytest.raises(aiohttp.ClientError):await s.get(f'https://127.0.0.1:{port}/')
            assert not hit.is_set()
            # Initialization must fail without a broker, never use HTTPS_PROXY.
            with pytest.raises(NetworkError):
                async with isolated_telegram_bot(Path(root)/'missing.sock','123456:synthetic'):pass
            assert not hit.is_set()
        finally:await proxy.close();server.close();await server.wait_closed()

@pytest.mark.asyncio
async def test_browser_connect_headers_only_local():
    raw=b'CONNECT api.oneme.ru:443 HTTP/1.1\r\nHost: api.oneme.ru\r\nAccept-Language: ru\r\nsec-ch-ua: synthetic\r\n\r\n'
    def reader():
        r=asyncio.StreamReader();r.feed_data(raw);r.feed_eof();return r
    with pytest.raises(EgressDenied):await read_connect(reader())
    assert await read_connect(reader(),local_adapter=True)=='api.oneme.ru:443'
