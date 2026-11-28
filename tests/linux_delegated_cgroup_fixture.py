"""Synthetic acceptance inside a dedicated systemd-delegated Linux service.

Never touches other cgroups, never adopts an external PID, no live launch.
The runner is outside the test subtree; only its own children enter it.
"""
import asyncio
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import time

from app.cgroup_readback import identity, readback, _read
from app.cgroup_retirement import RetirementJournal, DelegatedRetirement
from app.egress_policy import EgressDenied
from app.cohort_guard import CohortGuard
from app.controller_lease import renew
from app.pinned_broker import PinnedBroker
from app.linux_egress_policy import BrokerPins


UNIT=re.compile(r'max2tg-acceptance-[0-9]+-[0-9]+\.service')


def membership():
    lines=Path('/proc/self/cgroup').read_text().splitlines()
    assert len(lines)==1 and lines[0].startswith('0::/')
    return lines[0][3:]


async def real_broker(progress_fd,gate,action):
    root=Path(gate).parent
    channels={'a'*24:root/'actual-max','telegram':root/'actual-tg'}
    for p in channels.values():p.mkdir(mode=0o700)
    pins=BrokerPins.parse({'max-text':['8.8.8.8'],'telegram':['9.9.9.9'],
                           'expires_at':int(time.time())+60})
    # No external network in the private systemd service; even an
    # unexpected connection request cannot perform an outbound dial.
    async def deny_dial(*args,**kwargs):
        raise EgressDenied('Acceptance fixture has no upstream')
    b=PinnedBroker(pins,channels,dial=deny_dial)
    stop=asyncio.Event()
    task=asyncio.create_task(b.serve(stop,progress_fd=progress_fd))
    try:
        await asyncio.wait_for(b.ready.wait(),2)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if task.done():raise AssertionError('Actual broker stopped early')
            if Path(gate).exists() and action=='stall':
                # Block the same event loop that services the real broker.
                time.sleep(20)
                raise AssertionError('Actual stalled broker survived')
            await asyncio.sleep(.05)
        raise AssertionError('Guard failed to retire actual broker')
    finally:
        stop.set()
        await asyncio.gather(task,return_exceptions=True)


def child(fd,progress_fd=None,gate=None,action="run",role="synthetic"):
    # Move only this fixture child, through its inherited leaf directory FD.
    pfd=os.open('cgroup.procs',os.O_WRONLY|os.O_NOFOLLOW,dir_fd=fd)
    try:assert os.write(pfd,str(os.getpid()).encode())>0
    finally:os.close(pfd)
    assert _read(fd,'cgroup.procs').splitlines()==[str(os.getpid())]
    os.close(fd)
    print('READY',flush=True)
    if progress_fd is None:
        time.sleep(25)
        return
    if role=='broker':
        asyncio.run(real_broker(progress_fd,gate,action))
        return
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        renew(progress_fd)
        if Path(gate).exists() and action=='exit':
            os._exit(17)
        if Path(gate).exists() and action=='stall':
            # Genuine blocked progress while retaining the sole writer FD.
            time.sleep(20)
            raise AssertionError('Stalled child survived')
        time.sleep(.05)
    raise AssertionError('Guard failed to retire cohort')


def main(unit,stalled=None):
    assert stalled in (None,"controller","broker","controller_exit")
    assert sys.platform=='linux' and os.getuid()!=0 and UNIT.fullmatch(unit)
    expected='/system.slice/'+unit
    assert membership()==expected
    assert Path('/proc/self/status').read_text().split('NoNewPrivs:\t')[1].splitlines()[0]=='1'
    path=Path('/sys/fs/cgroup')/expected.lstrip('/')
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    cohort=None;leaves=[];children=[];journal=None;progress={}
    anchor=tempfile.TemporaryDirectory(prefix="cgroup-retirement-")
    try:
        source=identity(fd)
        assert os.fstat(fd).st_uid==os.getuid()
        assert readback(fd,expected=source)['populated']
        # Exclusive fresh subtree; fail if a group already exists.
        os.mkdir('max2tg-fixture',mode=0o700,dir_fd=fd)
        cohort=os.open('max2tg-fixture',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        target=identity(cohort)
        assert target!=source and target['mount']==source['mount']
        assert readback(cohort,expected=target)['recursively_empty']
        journal=RetirementJournal(anchor.name)
        retirement=DelegatedRetirement(journal,parent_fd=fd,target_fd=cohort)
        assert not retirement.stopped()
        names=('broker','controller') if stalled else ('broker','worker')
        for name in names:
            os.mkdir(name,mode=0o700,dir_fd=cohort)
            leaf=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=cohort)
            leaves.append((name,leaf))
            assert readback(leaf,expected=identity(leaf))['recursively_empty']
            extra=[];fds=(leaf,);wr=None
            if stalled:
                rd,wr=os.pipe()
                progress[name]=rd
                fds+=(wr,)
                action='stall' if name==stalled else 'exit' if stalled=='controller_exit' and name=='controller' else 'run'
                extra=[str(wr),str(Path(anchor.name)/'admitted'),action,name]
            try:
                p=subprocess.Popen([sys.executable,'-m','tests.linux_delegated_cgroup_fixture',
                    'child',str(leaf),*extra],pass_fds=fds,stdout=subprocess.PIPE,text=True)
            finally:
                if wr is not None:os.close(wr)
            children.append(p)
            assert select.select([p.stdout],[],[],5)[0] and p.stdout.readline().strip()=='READY'
            assert p.poll() is None and _read(leaf,'cgroup.procs').splitlines()==[str(p.pid)]
        evidence=readback(cohort,expected=target)
        assert evidence['populated'] and evidence['direct_process_count']==0
        assert not evidence['recursively_empty'] and membership()==expected
        assert all(name in os.listdir(cohort) for name in names)
        def owned():
            # Exact two leaf memberships; no extra nested groups.
            actual={name for name in os.listdir(cohort)
                    if (path/'max2tg-fixture'/name).is_dir()}
            if actual!=set(names):return False
            for (name,leaf),p in zip(leaves,children):
                code=p.poll()
                members=_read(leaf,'cgroup.procs').splitlines()
                if code is None:
                    if members!=[str(p.pid)]:return False
                elif members or stalled!='controller_exit' or name!='controller' or code!=17:
                    return False
                if any((path/'max2tg-fixture'/name/member).is_dir() for member in os.listdir(leaf)):
                    return False
            return True
        assert owned()
        if stalled:
            def admit():
                assert owned()
                assert all((Path(anchor.name)/name/'proxy.sock').is_socket() for name in ('actual-max','actual-tg'))
                (Path(anchor.name)/'admitted').touch(exist_ok=False)
                return True
            guard=CohortGuard(retirement)
            result=asyncio.run(guard.monitor(controller_fd=progress['controller'],
                broker_fd=progress['broker'],seconds=2,operator_approved=True,
                verify_owned=owned,admit=admit))
            progress.clear() # monitor owns and closes both reader FDs
            expected_reason={'controller':'EOF'} if stalled=='controller_exit' else {stalled:'LEASE_EXPIRED'}
            assert result['reasons']==expected_reason
            assert result['state']=='RETIRED' and not result['live_launch_authorized']
            print('COHORT GUARD PASS: '+stalled+' progress expired, entire owned group retired',flush=True)
            evidence=journal.inspect()
            for name in ('actual-max','actual-tg'):
                import socket
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                    sock.settimeout(.5)
                    try:sock.connect(str(Path(anchor.name)/name/'proxy.sock'))
                    except OSError:pass
                    else:raise AssertionError('Retired actual broker accepted connection')
            print('ACTUAL PINNED BROKER RETIRED PASS',flush=True)
        else:
            evidence=retirement.retire(operator_approved=True,verify_owned=owned)
        assert evidence['state']=='RETIRED' and not evidence['live_launch_authorized']
        for (name,_),p in zip(leaves,children):
            expected_code=17 if stalled=='controller_exit' and name=='controller' else -signal.SIGKILL
            assert p.wait(timeout=5)==expected_code
        assert retirement.stopped()
        try:retirement.retire(operator_approved=True,verify_owned=owned)
        except EgressDenied:pass
        else:raise AssertionError('Retirement replayed')
        journal.close();journal=None
        with RetirementJournal(anchor.name) as reopened:
            assert reopened.inspect()==evidence
            try:reopened.arm(source,target,expected)
            except EgressDenied:pass
            else:raise AssertionError('Reopened retirement armed')
        print('DURABLE CGROUP RETIREMENT PASS: retained intent, verified empty, restart readback only',flush=True)
        assert membership()==expected and identity(fd)==source
        assert readback(fd,expected=source)['populated']
        print('DELEGATED CGROUP PASS: two owned descendant leaves, recursive kill, empty subtree, supervisor survived',flush=True)
    finally:
        for rd in progress.values():
            try:os.close(rd)
            except OSError:pass
        if journal is not None:journal.close()
        anchor.cleanup()
        for p in children:
            if p.poll() is None:p.kill()
            p.wait(timeout=5)
            p.stdout.close()
        for name,leaf in reversed(leaves):
            os.close(leaf)
            os.rmdir(name,dir_fd=cohort)
        if cohort is not None:
            os.close(cohort)
            os.rmdir('max2tg-fixture',dir_fd=fd)
        os.close(fd)


if __name__=='__main__':
    if sys.argv[1]=='child':
        if len(sys.argv)==3:child(int(sys.argv[2]))
        else:child(int(sys.argv[2]),int(sys.argv[3]),sys.argv[4],sys.argv[5],sys.argv[6])
    else:main(sys.argv[1],sys.argv[2] if len(sys.argv)>2 else None)
