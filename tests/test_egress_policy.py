import asyncio,socket
from unittest.mock import AsyncMock
import pytest
from app.egress_policy import approved_host,public_address,connect_public,EgressDenied
from app.egress_tunnel import TunnelServer,LoopbackProxy

@pytest.mark.parametrize('role,target',[('max-text','api.telegram.org:443'),('telegram','api.oneme.ru:443'),('max-text','api.oneme.ru.evil.test:443'),('max-text','api.oneme.ru:80'),('max-text','api.oneme.ru.:443'),('max-text','127.0.0.1:443'),('max-text','api.oneme.ru@evil.test:443'),('max-text','api.oneme.ru:443/path'),('unknown','api.oneme.ru:443')])
def test_denied_targets(role,target):
    with pytest.raises(EgressDenied):approved_host(role,target)

@pytest.mark.parametrize('addr',['127.0.0.1','10.0.0.1','192.168.1.1','172.16.0.1','169.254.169.254','100.64.0.1','0.0.0.0','224.0.0.1','::1','fe80::1','fc00::1','::ffff:8.8.8.8','64:ff9b::808:808','2002:0808:0808::1'])
def test_denied_addresses(addr):
    with pytest.raises(EgressDenied):public_address(addr)

@pytest.mark.asyncio
async def test_mixed_dns_answer_rejected_without_dial():
    def record(ip):return (socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',(ip,443))
    resolve=AsyncMock(return_value=[record('8.8.8.8'),record('127.0.0.1')]);dial=AsyncMock()
    with pytest.raises(EgressDenied):await connect_public('max-text','api.oneme.ru:443',resolve=resolve,dial=dial)
    dial.assert_not_awaited();resolve.return_value=[record('8.8.8.8')]
    await connect_public('max-text','api.oneme.ru:443',resolve=resolve,dial=dial)
    dial.assert_awaited_once_with('8.8.8.8',443,family=socket.AF_INET,flags=socket.AI_NUMERICHOST)

@pytest.mark.asyncio
async def test_tunnel_role_and_protocol():
    import tempfile
    from pathlib import Path
    async def echo(reader,writer):
        try:writer.write(await reader.readexactly(4));await writer.drain()
        finally:writer.close();await writer.wait_closed()
    upstream=await asyncio.start_server(echo,'127.0.0.1',0);port=upstream.sockets[0].getsockname()[1];calls=[]
    async def connect(role,target):calls.append((role,target));return await asyncio.open_connection('127.0.0.1',port)
    with tempfile.TemporaryDirectory(prefix='m2net-',dir='/tmp') as d:
        path=Path(d)/'egress.sock';broker=TunnelServer('max-text',path,connector=connect);await broker.start();proxy=LoopbackProxy(path);url=await proxy.start();p=int(url.rsplit(':',1)[1])
        try:
            for target in ('api.telegram.org:443','127.0.0.1:443','api.oneme.ru:443'):
                r,w=await asyncio.open_connection('127.0.0.1',p);w.write(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode());await w.drain();head=await r.readuntil(b'\r\n\r\n')
                if target=='api.oneme.ru:443':
                    assert b'200' in head;w.write(b'test');await w.drain();assert await r.readexactly(4)==b'test'
                else:assert b'403' in head
                w.close();await w.wait_closed()
            assert calls==[('max-text','api.oneme.ru:443')]
        finally:await proxy.close();await broker.close();upstream.close();await upstream.wait_closed()

@pytest.mark.asyncio
@pytest.mark.parametrize('raw',[b'GET / HTTP/1.1\r\n\r\n',b'CONNECT api.oneme.ru:443 HTTP/1.1\r\nHost: evil.test\r\n\r\n',b'CONNECT api.oneme.ru:443 HTTP/1.1\r\nContent-Length: 1\r\n\r\n',b'CONNECT api.oneme.ru:443 HTTP/1.1\r\nHost: api.oneme.ru\r\nHost: api.oneme.ru\r\n\r\n'])
async def test_malformed_connect_rejected(raw):
    from app.egress_tunnel import read_connect
    reader=asyncio.StreamReader();reader.feed_data(raw);reader.feed_eof()
    with pytest.raises(EgressDenied):await read_connect(reader)

@pytest.mark.asyncio
async def test_connection_cap_and_shutdown():
    import tempfile
    from pathlib import Path
    entered=asyncio.Event();release=asyncio.Event()
    async def connect(*args):entered.set();await release.wait()
    with tempfile.TemporaryDirectory(prefix='m2cap-',dir='/tmp') as d:
        path=Path(d)/'egress.sock';b=TunnelServer('max-text',path,connector=connect,max_connections=1);await b.start()
        a,w=await asyncio.open_unix_connection(str(path));w.write(b'CONNECT api.oneme.ru:443 HTTP/1.1\r\n\r\n');await w.drain();await asyncio.wait_for(entered.wait(),2)
        x,y=await asyncio.open_unix_connection(str(path))
        try:
            assert await asyncio.wait_for(x.read(),2)==b''
            await asyncio.wait_for(b.close(),3);assert not path.exists() and not b.tasks
            assert await asyncio.wait_for(a.read(),2)==b''
        finally:
            y.close();w.close();await y.wait_closed();await w.wait_closed();await b.close()

@pytest.mark.parametrize('addr',['64:ff9b:1::808:808','2001:1::1','3fff::1234'])
def test_special_ipv6_refused(addr):
    with pytest.raises(EgressDenied):public_address(addr)

@pytest.mark.asyncio
async def test_dns_family_mismatch_no_dial():
    resolve=AsyncMock(return_value=[(socket.AF_INET6,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('8.8.8.8',443,0,0))]);dial=AsyncMock()
    with pytest.raises(EgressDenied):await connect_public('telegram','api.telegram.org:443',resolve=resolve,dial=dial)
    dial.assert_not_awaited()
