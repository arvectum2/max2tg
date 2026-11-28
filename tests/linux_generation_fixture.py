"""Two successive reviewed rotations in one isolated broker lifetime."""
import asyncio
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile

from app.egress_policy import EgressDenied
from app.generation_ledger import GenerationLedger
from app.generation_controller import GenerationController
from app.operator_boundary import OwnedCohort
from app.linux_egress_policy import BrokerPins
from app.operator_pin_rotation import NamespaceHandle, digest, frame, parse, request, read_frame_fd, read_receipt
from app.pinned_broker import PinnedBroker
from tests.linux_live_rotation_fixture import candidate
from tests.linux_veth_fixture import IPS, DROP, denied, isolated, unprivileged
from tests.pinned_broker_linux_fixture import request as connect_request


def third(old,new):
    return BrokerPins.parse(dict(old.record(),expires_at=new.expires_at+8))


async def exercise(cert,expiry,identity,fd,lifeline_fd,leased=False):
    os.close(fd)
    info=os.stat('/proc/self/ns/net')
    assert (info.st_dev,info.st_ino)==identity
    unprivileged()
    isolated([],{'lo','eth0'})
    old=BrokerPins.parse({'max-text':[IPS[0]],'telegram':[IPS[1]],'expires_at':expiry})
    new=candidate(old)
    final=third(old,new)
    ctx=ssl.create_default_context(cafile=cert+'/cert.pem')
    with tempfile.TemporaryDirectory(prefix='generation-broker-') as root:
        channels={k:Path(root)/str(i) for i,k in enumerate(('a'*24,'telegram'))}
        for p in channels.values():p.mkdir(mode=0o700)
        b=PinnedBroker(old,channels)
        stop=asyncio.Event()
        service=b.serve_leased(stop,lease_fd=lifeline_fd,seconds=5) if leased else b.serve_guarded(stop,lifeline_fd=lifeline_fd)
        task=asyncio.create_task(service)
        writers=[]
        async def connect(role,host):
            reader,writer,head=await connect_request(channels[role],host)
            writers.append(writer)
            assert b'200' in head
            await writer.start_tls(ctx,server_hostname=host,ssl_handshake_timeout=2)
            return reader,writer
        async def echo(r,w):
            w.write(b'confirmed')
            await w.drain()
            assert await asyncio.wait_for(r.readexactly(9),2)==b'confirmed'
        async def change(current,replacement,revoked_reader,revoked_ip):
            grant=parse(await asyncio.to_thread(read_frame_fd,sys.stdin.fileno()))
            assert set(grant)=={'nonce'}
            nonce=grant['nonce']
            async def update(a,z):
                assert (a,z)==(current,replacement) and not b.ready.is_set()
                os.write(sys.stdout.fileno(),frame(request(nonce,a,z)))
                raw=await asyncio.to_thread(read_frame_fd,sys.stdin.fileno())
                return read_receipt(raw,nonce,a,z)
            await b.rotate(replacement,verified_update=update)
            assert b.pins==replacement and b.ready.is_set()
            try:assert await asyncio.wait_for(revoked_reader.read(),2)==b''
            except (ConnectionResetError,ssl.SSLEOFError):pass
            await denied(revoked_ip,443)
            os.write(sys.stdout.fileno(),frame({'nonce':nonce,'digest':digest(b.pins),'ready':True}))
        try:
            await asyncio.wait_for(b.ready.wait(),2)
            mr,mw=await connect('a'*24,'api.oneme.ru')
            tr,tw=await connect('telegram','api.telegram.org')
            await echo(mr,mw);await echo(tr,tw)
            await change(old,new,mr,IPS[0])
            nr,nw=await connect('a'*24,'api.oneme.ru')
            await echo(nr,nw);await echo(tr,tw)
            await change(new,final,nr,IPS[2])
            fr,fw=await connect('a'*24,'api.oneme.ru')
            await echo(fr,fw);await echo(tr,tw)
            print('TWO GENERATIONS TLS PASS',flush=True)
            assert await asyncio.wait_for(task,8)==('STOPPED','LEASE_EXPIRED' if leased else 'EOF')
            assert not b.ready.is_set() and not b._streams
            assert all(not (p/'proxy.sock').exists() for p in channels.values())
            for r in (fr,tr):
                try:assert await asyncio.wait_for(r.read(),2)==b''
                except (ConnectionResetError,ssl.SSLEOFError):pass
            print(('IDLE LEASE EXPIRY PASS' if leased else 'IDLE LIFELINE EOF PASS')+': both retained TLS streams and listeners closed',flush=True)
        finally:
            stop.set()
            await asyncio.wait_for(task,3)
            for w in writers:
                w.close()
                try:await asyncio.wait_for(w.wait_closed(),.5)
                except (OSError,TimeoutError):pass


def controller(ns,cert,old,leased=False):
    pid=int(ns[2])
    info=os.stat(f'/proc/{pid}/ns/net')
    identity=(info.st_dev,info.st_ino)
    handle=NamespaceHandle(pid,expected=identity)
    child=None;owner=None;read_fd=None;write_fd=None
    new=candidate(old);final=third(old,new)
    try:
        with tempfile.TemporaryDirectory(prefix='generation-anchor-') as root:
            with GenerationLedger(root,handle) as ledger:
                ledger.initialize(old,operator_approved=True)
                read_fd,write_fd=os.pipe()
                if not leased:
                    owner=subprocess.Popen([sys.executable,'-c',
                        'import sys,os;sys.stdin.buffer.read();os._exit(17)'],
                        pass_fds=(write_fd,),stdin=subprocess.PIPE)
                    os.close(write_fd);write_fd=None
                child=subprocess.Popen(['/usr/bin/nsenter',f'--net=/proc/self/fd/{handle.fd}','--',
                    *DROP,sys.executable,'-m','tests.linux_generation_fixture',cert,str(old.expires_at),
                    str(identity[0]),str(identity[1]),str(handle.fd),str(read_fd),*(["leased"] if leased else [])],
                    pass_fds=(handle.fd,read_fd),stdin=subprocess.PIPE,stdout=subprocess.PIPE)
                os.close(read_fd);read_fd=None
                cohort=OwnedCohort((child,) if leased else (owner,child))
                def send(data):
                    assert child.stdin.write(data)==len(data)
                    child.stdin.flush()
                def request_stop():
                    if owner is not None and not owner.stdin.closed:owner.stdin.close()
                    if not child.stdin.closed:child.stdin.close()
                    return True
                def stopped():
                    return cohort.stop_once(request_stop,timeout=10)
                controller=GenerationController(ledger,send=send,
                    receive=lambda:read_frame_fd(child.stdout.fileno(),timeout=8),stop_and_verify=stopped)
                if leased:controller.renew_progress(write_fd)
                for current,replacement in ((old,new),(new,final)):
                    assert controller.rotate(current,replacement)['state']=='ADOPTED'
                    if leased:controller.renew_progress(write_fd)
                    if current==old:
                        bad=BrokerPins.parse(dict(new.record(),telegram=list(old.max_text),expires_at=final.expires_at))
                        try:ledger.arm(new,bad)
                        except EgressDenied:pass
                        else:raise AssertionError('Historical role transfer accepted')
                assert read_frame_fd(child.stdout.fileno(),timeout=5)==b'TWO GENERATIONS TLS PASS\n'
                # Only the auxiliary owner has the write FD. Its abnormal exit
                # produces real EOF while the broker has active TLS streams.
                if not leased:
                    owner.stdin.close()
                    assert owner.wait(timeout=3)==17
                # Leased case: actual coordinating process remains alive,
                # retains its only write FD, and makes no further progress.
                out,_=child.communicate(timeout=8)
                print('TWO GENERATIONS TLS PASS',flush=True)
                print(out.decode(),flush=True)
                assert child.returncode==0 and (b'IDLE LEASE EXPIRY PASS' if leased else b'IDLE LIFELINE EOF PASS') in out
                assert cohort.stopped()
                assert ledger.inspect()['matching_generations']==[2]
            with GenerationLedger(root,handle) as reopened:
                assert reopened.inspect()['state']=='ADOPTED'
                try:reopened.arm(final,BrokerPins.parse(dict(final.record(),expires_at=final.expires_at+8)))
                except EgressDenied:pass
                else:raise AssertionError('Reopened controller unexpectedly armed')
            print('PERSISTENT GENERATIONS PASS',flush=True)
    finally:
        try:
            if write_fd is not None:os.close(write_fd)
            if read_fd is not None:os.close(read_fd)
            if owner is not None:
                if not owner.stdin.closed:owner.stdin.close()
                owner.wait(timeout=3)
            if child is not None and child.poll() is None:
                if not child.stdin.closed:child.stdin.close()
                child.wait(timeout=10)
        finally:handle.close()


if __name__=='__main__':
    asyncio.run(exercise(sys.argv[1],int(sys.argv[2]),(int(sys.argv[3]),int(sys.argv[4])),int(sys.argv[5]),int(sys.argv[6]),'leased' in sys.argv[7:]))
