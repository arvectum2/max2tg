import asyncio
import time
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager

import pytest

from app.egress_policy import EgressDenied
from app.linux_egress_policy import BrokerPins
from app.pinned_broker import PinnedBroker
from tests.test_pinned_broker import SID, pins, request


@pytest.fixture(name="channels")
def fixture_channels():
    with tempfile.TemporaryDirectory(prefix="m2rot-", dir="/tmp") as d:
        root=Path(d).resolve(); paths={SID:root/"max", "telegram":root/"tg"}
        for p in paths.values(): p.mkdir(mode=0o700)
        yield paths


def replacement(old):
    return BrokerPins.parse({'max-text': ['1.1.1.1'], 'telegram': ['9.9.9.9'],
                             'expires_at': old.expires_at + 60})


RECEIPT = {'verified': True, 'pins': 2, 'rotated': True, 'live_launch_authorized': False}


async def roundtrip(reader, writer, message):
    writer.write(message)
    await writer.drain()
    assert await asyncio.wait_for(reader.readexactly(len(message)), 2) == message


@asynccontextmanager
async def running(channels, *, dial_hook=None, seconds=60):
    calls = []
    writers = []
    async def echo(r, w):
        try:
            while data := await r.read(128):
                w.write(data)
                await w.drain()
        finally:
            w.close()
            await w.wait_closed()
    listener = await asyncio.start_server(echo, '127.0.0.1', 0)
    async def dial(ip, port, **kw):
        calls.append(ip)
        if dial_hook:
            await dial_hook()
        return await asyncio.open_connection('127.0.0.1', listener.sockets[0].getsockname()[1])
    b = PinnedBroker(pins(seconds), channels, dial=dial)
    stop = asyncio.Event()
    task = asyncio.create_task(b.serve(stop))
    async def connect(role, host):
        r,w,head = await request(channels[role], host)
        writers.append(w)
        return r,w,head
    try:
        await asyncio.wait_for(b.ready.wait(), 2)
        yield b, stop, task, connect, calls
    finally:
        stop.set()
        await asyncio.wait_for(task, 3)
        for w in writers:
            w.close()
            await w.wait_closed()
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
async def test_preserve_retained_revoke_removed_pause_and_resume(channels):
    async with running(channels) as (b, _, _, connect, calls):
        old = b.pins
        new = replacement(old)
        mr,mw,head = await connect(SID, 'api.oneme.ru')
        assert b'200' in head
        tr,tw,head = await connect('telegram', 'api.telegram.org')
        assert b'200' in head
        await roundtrip(mr,mw,b'old')
        await roundtrip(tr,tw,b'before')
        entered, release = asyncio.Event(), asyncio.Event()
        async def update(current, candidate):
            assert current == old and candidate == new
            entered.set()
            await release.wait()
            return RECEIPT
        rotate = asyncio.create_task(b.rotate(new, verified_update=update))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not b.ready.is_set()
            assert await asyncio.wait_for(mr.read(), 2) == b''
            await roundtrip(tr,tw,b'during')
            _,_,head = await connect(SID,'api.oneme.ru')
            assert b'403' in head
            assert calls == ['8.8.8.8','9.9.9.9']
            with pytest.raises(EgressDenied):
                await b.rotate(new, verified_update=update)
        finally:
            release.set()
            await rotate
        assert b.ready.is_set() and b.pins == new
        await roundtrip(tr,tw,b'after')
        nr,nw,head = await connect(SID,'api.oneme.ru')
        assert b'200' in head
        await roundtrip(nr,nw,b'new')
        assert calls[-1] == '1.1.1.1'


@pytest.mark.asyncio
async def test_inflight_dial_fenced_even_for_retained_ip(channels):
    started, release = asyncio.Event(), asyncio.Event()
    async def slow():
        started.set()
        await release.wait()
    async with running(channels, dial_hook=slow) as (b, _, _, connect, calls):
        pending=asyncio.create_task(connect('telegram','api.telegram.org'))
        await asyncio.wait_for(started.wait(),2)
        async def update(*_):return RECEIPT
        await b.rotate(replacement(b.pins), verified_update=update)
        release.set()
        _,_,head=await pending
        assert b'403' in head and not b._streams
        _,_,head=await connect('telegram','api.telegram.org')
        assert b'200' in head and calls==['9.9.9.9','9.9.9.9']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['timeout','receipt','cancel','stop'])
async def test_ambiguous_or_stopped_rotation_never_reopens(channels,mode):
    async with running(channels) as (b, stop, task, connect, _):
        r,w,head=await connect('telegram','api.telegram.org')
        assert b'200' in head
        entered=asyncio.Event()
        async def update(*_):
            entered.set()
            if mode=='timeout':raise TimeoutError('unknown')
            if mode=='receipt':return {'verified':True}
            if mode=='stop':
                stop.set()
                await asyncio.sleep(.15)
                return RECEIPT
            await asyncio.Event().wait()
        rotation=asyncio.create_task(b.rotate(replacement(b.pins),verified_update=update))
        await asyncio.wait_for(entered.wait(),2)
        if mode=='cancel':rotation.cancel()
        with pytest.raises((TimeoutError,EgressDenied,asyncio.CancelledError)):
            await rotation
        assert not b.ready.is_set()
        assert await asyncio.wait_for(r.read(),2)==b''
        assert await asyncio.wait_for(task,2) in ('ROTATION_FAILED','STOPPED')
        assert not any((p/'proxy.sock').exists() for p in channels.values())
        with pytest.raises(EgressDenied):
            await b.rotate(replacement(b.pins),verified_update=update)


@pytest.mark.asyncio
async def test_expiry_during_update_cannot_extend_generation(channels):
    async with running(channels,seconds=4) as (b, _, task, connect, _):
        r,w,head=await connect('telegram','api.telegram.org')
        assert b'200' in head
        async def update(*_):
            await asyncio.sleep(5)
            return RECEIPT
        b._deadline=time.monotonic()+.2
        with pytest.raises((EgressDenied,TimeoutError)):
            await b.rotate(replacement(b.pins),verified_update=update)
        assert await asyncio.wait_for(r.read(),2)==b''
        assert await asyncio.wait_for(task,2) in ('EXPIRED','ROTATION_FAILED')
        assert not b.ready.is_set()
