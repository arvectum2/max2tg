"""Synthetic live broker rotation; controller uses only an exact pre-reviewed bundle."""
import asyncio
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile

from app.linux_egress_policy import BrokerPins
from app.egress_policy import EgressDenied
from app.operator_pin_rotation import NamespaceHandle, RotationGrant, frame, request as grant_request, read_frame_fd, read_receipt
from app.pinned_broker import PinnedBroker
from tests.linux_veth_fixture import IPS, DROP, denied, isolated, unprivileged
from tests.pinned_broker_linux_fixture import request


def candidate(old):
    return BrokerPins.parse({'max-text': [IPS[2]], 'telegram': [IPS[1]],
                             'expires_at': old.expires_at + 8})


async def exercise(cert, expiry, nonce, identity, inherited_fd):
    # No namespace descriptor retained by the broker.
    os.close(inherited_fd)
    info=os.stat("/proc/self/ns/net")
    assert (info.st_dev,info.st_ino)==identity
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
                os.write(sys.stdout.fileno(),frame(grant_request(nonce,current,replacement)))
                raw=await asyncio.to_thread(read_frame_fd,sys.stdin.fileno())
                return read_receipt(raw,nonce,current,replacement)
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
    # The trusted launcher owns the keeper; no caller-supplied PID or commands.
    pid=int(ns[2])
    info=os.stat(f'/proc/{pid}/ns/net')
    identity=(info.st_dev,info.st_ino)
    try:
        NamespaceHandle(pid,expected=(0,0))
    except EgressDenied:
        pass
    else:
        raise AssertionError('Foreign namespace accepted')
    handle=NamespaceHandle(pid,expected=identity)
    new=candidate(old)
    grant=RotationGrant(handle,old,new)
    child=None
    try:
        child=subprocess.Popen(
            ['/usr/bin/nsenter',f'--net=/proc/self/fd/{handle.fd}','--',
             *DROP,sys.executable,'-m','tests.linux_live_rotation_fixture',
             cert,str(old.expires_at),grant.nonce,str(identity[0]),str(identity[1]),str(handle.fd)],
            pass_fds=(handle.fd,),stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        raw=read_frame_fd(child.stdout.fileno(),timeout=6)
        receipt=grant.execute(raw)
        try:
            grant.execute(raw)
        except EgressDenied:
            pass
        else:
            raise AssertionError('Replay accepted')
        out,_=child.communicate(receipt,timeout=12)
        print(out.decode(),flush=True)
        assert child.returncode==0 and b'LIVE ROTATION TLS PASS' in out
        print('BOUND ROTATION PASS: held namespace FD, exact grant, replay denied',flush=True)
    finally:
        try:
            if child is not None and child.poll() is None:
                if not child.stdin.closed:child.stdin.close()
                child.wait(timeout=8)
        finally:
            handle.close()


if __name__=='__main__':
    asyncio.run(exercise(sys.argv[1],int(sys.argv[2]),sys.argv[3],
                         (int(sys.argv[4]),int(sys.argv[5])),int(sys.argv[6])))
