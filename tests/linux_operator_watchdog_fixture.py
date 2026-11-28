"""Disposable Linux: a separate guardian stops a stalled TLS broker."""
import asyncio
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import tempfile
import time

from app.egress_policy import EgressDenied
from app.linux_egress_policy import BrokerPins
from app.operator_watchdog import WatchdogJournal, watch
from app.pinned_broker import PinnedBroker
from tests.linux_veth_fixture import IPS, denied, isolated, unprivileged
from tests.pinned_broker_linux_fixture import request


async def broker(root,expiry,progress_fd):
    unprivileged()
    isolated([],{'lo','eth0'})
    pins=BrokerPins.parse({'max-text':[IPS[0]],'telegram':[IPS[1]],'expires_at':expiry})
    channels={'a'*24:root/'max','telegram':root/'tg'}
    b=PinnedBroker(pins,channels)
    stop=asyncio.Event()
    task=asyncio.create_task(b.serve(stop,progress_fd=progress_fd))
    try:
        await asyncio.wait_for(b.ready.wait(),2)
        (root/'ready').touch()
        while not (root/'hang').exists():
            await asyncio.sleep(.1)
        # Actual blocked event loop; the lease writer remains open.
        (root/'hung').touch()
        time.sleep(15)
        raise AssertionError('Stalled broker survived watchdog')
    finally:
        stop.set()
        await asyncio.gather(task,return_exceptions=True)
        os.close(progress_fd)


async def until(path,timeout=5):
    deadline=time.monotonic()+timeout
    while not path.exists():
        if time.monotonic()>=deadline:raise TimeoutError(str(path.name))
        await asyncio.sleep(.025)


async def exercise(cert,expiry):
    unprivileged()
    isolated([],{'lo','eth0'})
    with tempfile.TemporaryDirectory(prefix='watchdog-test-') as d:
        root=Path(d)
        for name in ('max','tg','anchor'):(root/name).mkdir(mode=0o700)
        rd,wr=os.pipe()
        broker_process=None;guardian=None;pidfd=None
        writers=[]
        try:
            broker_process=subprocess.Popen([sys.executable,'-m','tests.linux_operator_watchdog_fixture',
                'broker',d,str(expiry),str(wr)],pass_fds=(wr,))
            os.close(wr);wr=None
            pidfd=os.pidfd_open(broker_process.pid)
            guardian=subprocess.Popen([sys.executable,'-m','tests.linux_operator_watchdog_fixture',
                'guard',str(root/'anchor'),str(pidfd),str(broker_process.pid),str(rd)],
                pass_fds=(pidfd,rd),stdout=subprocess.PIPE)
            os.close(rd);rd=None
            await until(root/'anchor'/'watchdog.json')
            assert json.loads((root/'anchor'/'watchdog.json').read_text())['state']=='ARMED'
            await until(root/'ready')
            assert guardian.poll() is None
            ctx=ssl.create_default_context(cafile=cert+'/cert.pem')
            readers=[]
            for channel,host in (('max','api.oneme.ru'),('tg','api.telegram.org')):
                r,w,head=await request(root/channel,host)
                writers.append(w);readers.append(r)
                assert b'200' in head
                await w.start_tls(ctx,server_hostname=host,ssl_handshake_timeout=2)
                w.write(b'confirmed');await w.drain()
                assert await asyncio.wait_for(r.readexactly(9),2)==b'confirmed'
            await denied(IPS[2],443)
            (root/'hang').touch()
            await until(root/'hung')
            assert broker_process.poll() is None
            for r in readers:
                try:assert await asyncio.wait_for(r.read(),7)==b''
                except (ConnectionResetError,ssl.SSLEOFError):pass
            assert broker_process.wait(timeout=3)==-signal.SIGKILL
            output,_=guardian.communicate(timeout=5)
            assert guardian.returncode==0,output
            result=json.loads(output)
            assert result['state']=='STOPPED' and result['reason']=='LEASE_EXPIRED'
            assert not result['live_launch_authorized'] and not result['automatic_retry']
            # New operator process is permitted to inspect only; old journal
            # does not authorize another watchdog or a new broker.
            resumed=subprocess.run([sys.executable,'-m','tests.linux_operator_watchdog_fixture',
                'inspect',str(root/'anchor')],capture_output=True,check=True,timeout=5)
            assert json.loads(resumed.stdout)==result
            # SIGKILL can leave Unix socket pathnames, but no listener.
            for channel in ('max','tg'):
                try:await asyncio.open_unix_connection(str(root/channel/'proxy.sock'))
                except OSError:pass
                else:raise AssertionError('Dead broker accepted new connection')
            print('INDEPENDENT WATCHDOG PASS: hung broker killed through exact pidfd, two TLS streams closed, persistent STOPPED, no re-arm',flush=True)
        finally:
            for fd in (rd,wr,pidfd):
                if fd is not None:os.close(fd)
            for w in writers:
                w.close()
                try:await asyncio.wait_for(w.wait_closed(),.5)
                except (OSError,TimeoutError):pass
            # Only these explicitly owned disposable fixture children.
            for p in (broker_process,guardian):
                if p is not None:
                    if p.poll() is None:p.kill()
                    p.wait(timeout=3)


if __name__=='__main__':
    if sys.argv[1]=='broker':
        asyncio.run(broker(Path(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4])))
    elif sys.argv[1]=='guard':
        print(json.dumps(asyncio.run(watch(sys.argv[2],pidfd=int(sys.argv[3]),pid=int(sys.argv[4]),
            progress_fd=int(sys.argv[5]),seconds=3))),flush=True)
    elif sys.argv[1]=='inspect':
        with WatchdogJournal(sys.argv[2]) as retained:
            result=retained.inspect()
            try:retained.arm(result['target'])
            except EgressDenied:pass
            else:raise AssertionError('Retained guardian re-armed')
            print(json.dumps(result),flush=True)
    elif sys.argv[1]=='exercise':
        asyncio.run(exercise(sys.argv[2],int(sys.argv[3])))
    else:raise ValueError('Unknown fixture action')
