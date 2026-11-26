"""Synthetic live broker rotation; controller uses only an exact pre-reviewed bundle."""
import asyncio
import json
from pathlib import Path
import select
import ssl
import subprocess
import sys
import tempfile

from app.linux_egress_policy import BrokerPins
from app.linux_pin_rotation import rotate_quiesced_policy
from app.pinned_broker import PinnedBroker
from tests.linux_veth_fixture import IPS, DROP, call, denied, isolated, unprivileged
from tests.pinned_broker_linux_fixture import request


def candidate(old):
    return BrokerPins.parse({'max-text': [IPS[2]], 'telegram': [IPS[1]],
                             'expires_at': old.expires_at + 8})


async def exercise(cert, expiry):
    unprivileged()
    isolated([], {'lo', 'eth0'})
    old = BrokerPins.parse({'max-text': [IPS[0]], 'telegram': [IPS[1]],
                            'expires_at': expiry})
    new = candidate(old)
    ctx = ssl.create_default_context(cafile=cert + '/cert.pem')
    assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
    with tempfile.TemporaryDirectory(prefix='veth-live-') as d:
        channels = {k: Path(d) / str(i) for i,k in enumerate(('a'*24,'telegram'))}
        for path in channels.values():
            path.mkdir(mode=0o700)
        b = PinnedBroker(old, channels)
        stop = asyncio.Event()
        task = asyncio.create_task(b.serve(stop))
        writers = []
        async def connect(role, host):
            r,w,head = await request(channels[role],host)
            writers.append(w)
            assert b'200' in head
            await w.start_tls(ctx,server_hostname=host,ssl_handshake_timeout=2)
            return r,w
        async def echo(r,w,message):
            w.write(message)
            await w.drain()
            assert await asyncio.wait_for(r.readexactly(len(message)),2) == message
        try:
            await asyncio.wait_for(b.ready.wait(),2)
            mr,mw = await connect('a'*24,'api.oneme.ru')
            tr,tw = await connect('telegram','api.telegram.org')
            await echo(mr,mw,b'old MAX')
            await echo(tr,tw,b'old TG')
            async def update(current,replacement):
                assert current==old and replacement==new and not b.ready.is_set()
                # No mutation arguments cross the pipe. Parent has both bundles.
                print('ROTATE',flush=True)
                line=await asyncio.to_thread(sys.stdin.readline)
                return json.loads(line)
            await b.rotate(new,verified_update=update)
            assert b.ready.is_set() and b.pins==new
            try:
                assert await asyncio.wait_for(mr.read(),2)==b''
            except (ConnectionResetError,ssl.SSLEOFError):
                pass
            await denied(IPS[0],443)
            await echo(tr,tw,b'same TG TLS stream')
            nr,nw=await connect('a'*24,'api.oneme.ru')
            await echo(nr,nw,b'new MAX')
            print('LIVE ROTATION TLS PASS: retained stream, revoked stream closed, new TLS',flush=True)
        finally:
            stop.set()
            await asyncio.wait_for(task,3)
            for w in writers:
                w.close()
                try:
                    await asyncio.wait_for(w.wait_closed(),.5)
                except (OSError,TimeoutError):
                    pass


def controller(ns, cert, old):
    # This is a fixture-only trusted controller in the private network-none lab.
    # It accepts exactly one fixed signal and applies the predetermined candidate.
    new = candidate(old)
    child = subprocess.Popen([*ns,*DROP,sys.executable,'-m','tests.linux_live_rotation_fixture',
                              cert,str(old.expires_at)],stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,text=True)
    try:
        assert select.select([child.stdout],[],[],6)[0], 'Rotation request timeout'
        assert child.stdout.readline().strip()=='ROTATE'
        def observe():
            return json.loads(call(*ns,'nft','-j','list','ruleset'))
        receipt=rotate_quiesced_policy(old,new,observe=observe,
                                      apply=lambda batch:call(*ns,'nft','-f','-',data=batch))
        out,_=child.communicate(json.dumps(receipt)+'\n',timeout=12)
        print(out,flush=True)
        assert child.returncode==0 and 'LIVE ROTATION TLS PASS' in out
    finally:
        if child.poll() is None:
            # No CAP_KILL needed: EOF closes the fixture control protocol.
            if not child.stdin.closed:
                child.stdin.close()
            child.wait(timeout=8)


if __name__=='__main__':
    asyncio.run(exercise(sys.argv[1],int(sys.argv[2])))
