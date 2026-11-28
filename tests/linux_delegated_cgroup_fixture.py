"""Synthetic acceptance inside a dedicated systemd-delegated Linux service.

Never touches other cgroups, never adopts an external PID, no live launch.
The runner is outside the test subtree; only its own children enter it.
"""
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


UNIT=re.compile(r'max2tg-acceptance-[0-9]+-[0-9]+\.service')


def membership():
    lines=Path('/proc/self/cgroup').read_text().splitlines()
    assert len(lines)==1 and lines[0].startswith('0::/')
    return lines[0][3:]


def child(fd):
    # Move only this fixture child, through its inherited leaf directory FD.
    pfd=os.open('cgroup.procs',os.O_WRONLY|os.O_NOFOLLOW,dir_fd=fd)
    try:assert os.write(pfd,str(os.getpid()).encode())>0
    finally:os.close(pfd)
    assert _read(fd,'cgroup.procs').splitlines()==[str(os.getpid())]
    os.close(fd)
    print('READY',flush=True)
    time.sleep(25)


def main(unit):
    assert sys.platform=='linux' and os.getuid()!=0 and UNIT.fullmatch(unit)
    expected='/system.slice/'+unit
    assert membership()==expected
    assert Path('/proc/self/status').read_text().split('NoNewPrivs:\t')[1].splitlines()[0]=='1'
    path=Path('/sys/fs/cgroup')/expected.lstrip('/')
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    cohort=None;leaves=[];children=[];journal=None
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
        for name in ('broker','worker'):
            os.mkdir(name,mode=0o700,dir_fd=cohort)
            leaf=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=cohort)
            leaves.append((name,leaf))
            assert readback(leaf,expected=identity(leaf))['recursively_empty']
            p=subprocess.Popen([sys.executable,'-m','tests.linux_delegated_cgroup_fixture',
                'child',str(leaf)],pass_fds=(leaf,),stdout=subprocess.PIPE,text=True)
            children.append(p)
            assert select.select([p.stdout],[],[],5)[0] and p.stdout.readline().strip()=='READY'
            assert p.poll() is None and _read(leaf,'cgroup.procs').splitlines()==[str(p.pid)]
        evidence=readback(cohort,expected=target)
        assert evidence['populated'] and evidence['direct_process_count']==0
        assert not evidence['recursively_empty'] and membership()==expected
        assert sorted(os.listdir(cohort)).count('broker')==1 and 'worker' in os.listdir(cohort)
        def owned():
            # Exact two leaf memberships; no extra nested groups.
            actual={name for name in os.listdir(cohort)
                    if (path/'max2tg-fixture'/name).is_dir()}
            if actual!={'broker','worker'}:return False
            for (name,leaf),p in zip(leaves,children):
                if p.poll() is not None or _read(leaf,'cgroup.procs').splitlines()!=[str(p.pid)]:
                    return False
                if any((path/'max2tg-fixture'/name/member).is_dir() for member in os.listdir(leaf)):
                    return False
            return True
        assert owned()
        evidence=retirement.retire(operator_approved=True,verify_owned=owned)
        assert evidence['state']=='RETIRED' and not evidence['live_launch_authorized']
        assert all(p.wait(timeout=5)==-signal.SIGKILL for p in children)
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
    if sys.argv[1]=='child':child(int(sys.argv[2]))
    else:main(sys.argv[1])
