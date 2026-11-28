import asyncio,socket,time,tempfile
from unittest.mock import AsyncMock
from pathlib import Path
import pytest
from app.pinned_broker import PinnedBroker
from app.linux_egress_policy import BrokerPins

SID='a'*24


def pins(seconds=60):return BrokerPins.parse({'max-text':['8.8.8.8'],'telegram':['9.9.9.9'],'expires_at':int(time.time())+seconds})


@pytest.fixture
def channels():
    with tempfile.TemporaryDirectory(prefix='m2pin-',dir='/tmp') as d:
        root=Path(d).resolve();result={SID:root/'max','telegram':root/'tg'}
        for p in result.values():p.mkdir(mode=0o700)
        yield result


async def request(path,host):
    r,w=await asyncio.open_unix_connection(str(path/'proxy.sock'));w.write(f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode());await w.drain();return r,w,await asyncio.wait_for(r.readuntil(b'\r\n\r\n'),2)


@pytest.mark.asyncio
async def test_roles_expiry_and_live_tunnel_shutdown(channels):
    calls=[]
    async def echo(r,w):
        try:
            while data:=await r.read(32):w.write(data);await w.drain()
        finally:w.close();await w.wait_closed()
    listener=await asyncio.start_server(echo,'127.0.0.1',0)
    async def dial(*args,**kw):
        calls.append((args,kw));return await asyncio.open_connection('127.0.0.1',listener.sockets[0].getsockname()[1])
    broker=PinnedBroker(pins(5),channels,dial=dial);stop=asyncio.Event();task=asyncio.create_task(broker.serve(stop));writers=[];readers=[]
    try:
        await asyncio.wait_for(broker.ready.wait(),2)
        for role,host in ((SID,'api.telegram.org'),('telegram','api.oneme.ru')):
            r,w,head=await request(channels[role],host);writers.append(w);assert b'403' in head
        assert not calls
        for role,host,ip in ((SID,'api.oneme.ru','8.8.8.8'),('telegram','api.telegram.org','9.9.9.9')):
            r,w,head=await request(channels[role],host);writers.append(w);readers.append(r);assert b'200' in head;w.write(b'echo');await w.drain();assert await r.readexactly(4)==b'echo';assert calls[-1]==((ip,443),{'family':socket.AF_INET,'flags':socket.AI_NUMERICHOST})
        assert await asyncio.wait_for(task,6)=='EXPIRED'
        for r in readers:assert await asyncio.wait_for(r.read(),2)==b''
        assert not broker.ready.is_set() and not any((p/'proxy.sock').exists() for p in channels.values())
        assert not any(s.tasks for s in broker.servers.values())
        with pytest.raises(PermissionError):await broker.serve(stop)
    finally:
        stop.set();await asyncio.gather(task,return_exceptions=True)
        for w in writers:w.close();await w.wait_closed()
        listener.close();await listener.wait_closed()


def test_bad_layout_refused(channels):
    with pytest.raises(PermissionError):PinnedBroker(pins(),{SID:channels[SID],'telegram':channels[SID]})
    with pytest.raises(ValueError):PinnedBroker(pins(),{SID:channels[SID]})
    link=channels[SID].parent/'link';link.symlink_to(channels[SID])
    with pytest.raises(PermissionError):PinnedBroker(pins(),{SID:link,'telegram':channels['telegram']})
    (channels[SID]/'foreign').touch()
    with pytest.raises(PermissionError):PinnedBroker(pins(),channels)


@pytest.mark.asyncio
async def test_partial_startup_rolls_back_without_touching_foreign_file(channels):
    dial=AsyncMock();broker=PinnedBroker(pins(),channels,dial=dial)
    p=channels['telegram']/'proxy.sock';p.write_text('foreign')
    with pytest.raises(PermissionError):await broker.serve(asyncio.Event())
    assert not (channels[SID]/'proxy.sock').exists() and p.read_text()=='foreign';assert not broker.ready.is_set();dial.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_during_dial_closes_upstream(channels):
    broker=PinnedBroker(pins(),channels);broker._accepting=True;broker._deadline=time.monotonic()+10
    writer=AsyncMock();writer.close=lambda:None
    async def dial(*_,**kw):broker._deadline=time.monotonic()-1;return None,writer
    broker._dial=dial
    with pytest.raises(PermissionError):await broker._connect('max-text','api.oneme.ru:443')
    writer.wait_closed.assert_awaited_once()


async def test_guarded_broker_preclosed_lifeline_never_admits(channels):
    import os
    rd,wr=os.pipe()
    os.close(wr)
    broker=PinnedBroker(pins(),channels)
    stop=asyncio.Event()
    assert await broker.serve_guarded(stop,lifeline_fd=rd)==('STOPPED','EOF')
    assert stop.is_set() and not broker.ready.is_set()
    assert all(not (p/'proxy.sock').exists() for p in channels.values())


async def test_guarded_broker_lifeline_loss_after_ready(channels):
    import os
    rd,wr=os.pipe()
    broker=PinnedBroker(pins(),channels)
    stop=asyncio.Event()
    task=asyncio.create_task(broker.serve_guarded(stop,lifeline_fd=rd))
    try:
        await asyncio.wait_for(broker.ready.wait(),2)
        os.close(wr);wr=None
        assert await asyncio.wait_for(task,2)==('STOPPED','EOF')
        assert not broker.ready.is_set() and not broker._streams
        assert all(not (p/'proxy.sock').exists() for p in channels.values())
    finally:
        if wr is not None:os.close(wr)
        stop.set()
        await asyncio.gather(task,return_exceptions=True)
