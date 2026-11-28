"""Actual SIGKILL at three controller boundaries, disposable namespaces only."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from app.egress_policy import EgressDenied
from app.linux_egress_policy import BrokerPins
from app.linux_namespace_gate import prepare_fresh_policy
from app.operator_pin_rotation import NamespaceHandle, frame, request
from app.rotation_fence import RotationFence
from tests.linux_veth_fixture import call, isolated


def pins(expiry):
    old=BrokerPins.parse({'max-text':['8.8.8.8'],'telegram':['9.9.9.9'],'expires_at':expiry})
    new=BrokerPins.parse({'max-text':['1.1.1.1'],'telegram':['9.9.9.9'],'expires_at':expiry+10})
    return old,new


def crash(folder,pid,identity,expiry,phase):
    old,new=pins(expiry)
    handle=NamespaceHandle(pid,expected=identity)
    with RotationFence(folder,handle) as fence:
        raw=frame(request(fence.arm(old,new),old,new))
        original=handle.apply_reviewed
        def killed(*args):
            if phase=='before':
                os.kill(os.getpid(),signal.SIGKILL)
            original(*args)
            if phase=='after':
                os.kill(os.getpid(),signal.SIGKILL)
        handle.apply_reviewed=killed
        fence.execute(raw)
        assert phase=='receipt'
        os.kill(os.getpid(),signal.SIGKILL)
    raise AssertionError('Controller survived')


def scenario(phase):
    keeper=subprocess.Popen(['unshare','--net','sleep','35'])
    try:
        for _ in range(50):
            if os.readlink('/proc/self/ns/net')!=os.readlink(f'/proc/{keeper.pid}/ns/net'):break
            time.sleep(.05)
        else:raise AssertionError('Namespace not created')
        ns=['nsenter','-t',str(keeper.pid),'-n','--']
        call(*ns,'ip','link','set','lo','up')
        isolated(ns,{'lo'})
        info=os.stat(f'/proc/{keeper.pid}/ns/net')
        identity=(info.st_dev,info.st_ino)
        expiry=int(time.time())+25
        old,new=pins(expiry)
        with tempfile.TemporaryDirectory(prefix='rotation-anchor-') as folder:
            handle=NamespaceHandle(keeper.pid,expected=identity)
            try:
                assert prepare_fresh_policy(old,observe=handle.observe,
                    install=lambda batch:call(*ns,'nft','-f','-',data=batch))['verified']
                result=subprocess.run([sys.executable,'-m','tests.linux_rotation_crash_fixture',
                    'crash',folder,str(keeper.pid),str(identity[0]),str(identity[1]),str(expiry),phase],
                    timeout=12,check=False)
                assert result.returncode==-signal.SIGKILL,result.returncode
                with RotationFence(folder,handle) as recovered:
                    evidence=recovered.inspect()
                    assert evidence=={'state':'KERNEL_VERIFIED' if phase=='receipt' else 'IN_FLIGHT',
                        'matches':['current' if phase=='before' else 'replacement'],
                        'automatic_retry':False,'live_launch_authorized':False,'requires_reconciliation':True}
                    before=handle.observe()
                    nonce=json.loads((Path(folder)/'rotation.json').read_text())['nonce']
                    for attempt in (lambda:recovered.arm(old,new),
                                    lambda:recovered.execute(frame(request(nonce,old,new)))):
                        try:attempt()
                        except EgressDenied:pass
                        else:raise AssertionError('Crash recovery reopened old grant')
                    after=handle.observe()
                    # Element lifetime may count down; normalized classification must not change.
                    assert recovered.inspect()==evidence
                    def ids(v):
                        return [(k,o.get('handle')) for e in v['nftables'] for k,o in e.items()]
                    assert ids(before)==ids(after)
                print('ROTATION SIGKILL PASS',phase,evidence,flush=True)
            finally:handle.close()
    finally:
        keeper.terminate()
        keeper.wait(timeout=3)


def main():
    isolated([],{'lo'})
    assert os.getuid()==0 and 'NoNewPrivs:\t1' in Path('/proc/self/status').read_text()
    for phase in ('before','after','receipt'):
        scenario(phase)


if __name__=='__main__':
    if len(sys.argv)==1:main()
    else:
        assert sys.argv[1]=='crash'
        crash(sys.argv[2],int(sys.argv[3]),(int(sys.argv[4]),int(sys.argv[5])),
              int(sys.argv[6]),sys.argv[7])
