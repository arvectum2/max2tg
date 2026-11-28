"""Real controller death after verified nft mutation, before broker receipt."""
import os
import signal
import subprocess
import sys
import tempfile
import time

from app.egress_policy import EgressDenied
from app.operator_pin_rotation import NamespaceHandle, frame, request, read_frame_fd
from app.rotation_fence import RotationFence
from app.operator_boundary import OwnedCohort, RetirementBoundary
from app.generation_ledger import GenerationLedger
from app.linux_egress_policy import BrokerPins
from tests.linux_live_rotation_fixture import candidate
from tests.linux_veth_fixture import IPS, DROP, call, isolated


def crash(folder,pid,identity,expiry,request_fd,receipt_fd,lineage=False):
    old=BrokerPins.parse({'max-text':[IPS[0]],'telegram':[IPS[1]],'expires_at':expiry})
    handle=NamespaceHandle(pid,expected=identity)
    with (GenerationLedger if lineage else RotationFence)(folder,handle) as fence:
        if lineage:fence.initialize(old,operator_approved=True)
        nonce=fence.arm(old,candidate(old))
        os.write(sys.stdout.fileno(),nonce.encode()+b'\n')
        raw=read_frame_fd(request_fd,timeout=20)
        fence.execute(raw)
        # Intentionally never write a receipt. Only this process holds the
        # receipt pipe writer, so death produces genuine EOF in the broker.
        assert receipt_fd>=0
        os.kill(os.getpid(),signal.SIGKILL)
    raise AssertionError('Controller survived')


def controller(ns,cert,old,root,recovery=False,lineage=False):
    assert not lineage or recovery
    ledger_type=GenerationLedger if lineage else RotationFence
    pid=int(ns[2])
    info=os.stat(f'/proc/{pid}/ns/net')
    identity=(info.st_dev,info.st_ino)
    handle=NamespaceHandle(pid,expected=identity)
    keeper=subprocess.Popen(['unshare','--net','sleep','45'])
    children=[]
    fds=set()
    def pipe():
        pair=os.pipe();fds.update(pair);return pair
    def close(fd):
        os.close(fd);fds.remove(fd)
    try:
        for _ in range(50):
            if os.readlink('/proc/self/ns/net')!=os.readlink(f'/proc/{keeper.pid}/ns/net'):break
            time.sleep(.05)
        else:raise AssertionError('Client namespace not created')
        client_ns=['nsenter','-t',str(keeper.pid),'-n','--']
        call(*client_ns,'ip','link','set','lo','up')
        isolated(client_ns,{'lo'})
        assert os.readlink(f'/proc/{keeper.pid}/ns/net')!=os.readlink(f'/proc/{pid}/ns/net')
        with tempfile.TemporaryDirectory(prefix='live-crash-fence-') as anchor:
            req_r,req_w=pipe();ack_r,ack_w=pipe()
            ctrl=subprocess.Popen([sys.executable,'-m','tests.linux_live_controller_crash_fixture',
                'crash',anchor,str(pid),str(identity[0]),str(identity[1]),str(old.expires_at),
                str(req_r),str(ack_w),*(['lineage'] if lineage else [])],pass_fds=(req_r,ack_w),stdout=subprocess.PIPE)
            children.append(ctrl)
            close(req_r);close(ack_w)
            nonce=read_frame_fd(ctrl.stdout.fileno(),timeout=5).decode().strip()
            assert len(nonce)==48
            ctrl.stdout.close()
            broker=subprocess.Popen(['/usr/bin/nsenter',f'--net=/proc/self/fd/{handle.fd}','--',
                *DROP,sys.executable,'-m','tests.linux_client_rotation_fixture','broker',
                root,str(old.expires_at),nonce,str(identity[0]),str(identity[1]),str(handle.fd),
                'journals','crash'],pass_fds=(handle.fd,),stdin=ack_r,stdout=req_w)
            children.append(broker)
            close(ack_r);close(req_w)
            client=subprocess.Popen([*client_ns,*DROP,sys.executable,'-m',
                'tests.linux_client_rotation_fixture','client',cert,root,'journals','crash'])
            children.append(client)
            cohort=OwnedCohort(tuple(children))
            boundary=RetirementBoundary(cohort,handle)
            assert cohort.wait_stopped(25)
            assert ctrl.returncode==-signal.SIGKILL
            assert broker.returncode==client.returncode==0
            assert boundary.stopped()
            with ledger_type(anchor,handle) as recovered:
                evidence=recovered.inspect()
                if lineage:
                    assert evidence=={'generation':1,'state':'KERNEL_VERIFIED','matching_generations':[1],
                        'automatic_retry':False,'live_launch_authorized':False}
                else:
                    assert evidence=={'state':'KERNEL_VERIFIED','matches':['replacement'],
                        'automatic_retry':False,'live_launch_authorized':False,'requires_reconciliation':True}
                for action in (lambda:recovered.arm(old,candidate(old)),
                               lambda:recovered.execute(frame(request(nonce,old,candidate(old))))):
                    try:action()
                    except EgressDenied:pass
                    else:raise AssertionError('Reopened old grant')
                assert recovered.inspect()==evidence
            call(*DROP,sys.executable,'-c',
                 'from pathlib import Path;import sys;p=Path(sys.argv[1]);assert (p/"broker-failed").is_file() and (p/"done").is_file()',root)
            print('LIVE CONTROLLER SIGKILL PASS: EOF, broker fail closed, durable fence retained',flush=True)
            if recovery:
                from tests.linux_fresh_recovery_fixture import recover
                recover(ns,cert,root,handle,client_ns,anchor,lineage=lineage,boundary=boundary)
                with ledger_type(anchor,handle) as retained:
                    assert retained.inspect()==evidence
                    try:retained.arm(old,candidate(old))
                    except EgressDenied:pass
                    else:raise AssertionError('Retired grant reopened')

    finally:
        for fd in list(fds):close(fd)
        try:
            for child in children:
                if child.poll() is None:child.wait(timeout=15)
        finally:
            handle.close()
            keeper.terminate()
            keeper.wait(timeout=3)


if __name__=='__main__':
    assert sys.argv[1]=='crash'
    crash(sys.argv[2],int(sys.argv[3]),(int(sys.argv[4]),int(sys.argv[5])),
          int(sys.argv[6]),int(sys.argv[7]),int(sys.argv[8]),'lineage' in sys.argv[9:])
