import pytest

from app.egress_policy import EgressDenied
from app.generation_controller import GenerationController
from app.generation_ledger import GenerationLedger
from app.operator_pin_rotation import digest, frame, parse, request
from tests.test_rotation_fence import setup


@pytest.mark.parametrize('failure',[None,'request','ack','shutdown'])
def test_controller_transaction(tmp_path,failure):
    folder,ns,old,new=setup(tmp_path)
    sent=[]
    stops=[]
    with GenerationLedger(folder,ns) as ledger:
        ledger.initialize(old,operator_approved=True)
        def receive():
            nonce=parse(sent[0])['nonce']
            if len(sent)==1:
                if failure=='request':raise EOFError('synthetic closed channel')
                return frame(request(nonce,old,new))
            if failure in ('ack','shutdown'):raise EOFError('synthetic missing adoption')
            return frame({'nonce':nonce,'digest':digest(new),'ready':True})
        def stop():
            stops.append(True)
            return failure!='shutdown'
        c=GenerationController(ledger,send=sent.append,receive=receive,stop_and_verify=stop)
        if failure is None:
            assert c.rotate(old,new)['state']=='ADOPTED'
            assert not stops and len(ns.calls)==1
        else:
            with pytest.raises(EgressDenied if failure=='shutdown' else EOFError):
                c.rotate(old,new)
            assert stops==[True] and c.failed
            with pytest.raises(EgressDenied):c.rotate(old,new)
            assert len(ns.calls)==int(failure!='request')
            assert ledger.inspect()['state']==('ARMED' if failure=='request' else 'KERNEL_VERIFIED')


@pytest.mark.parametrize('phase',['initial','armed','foreign','reopened'])
def test_only_usable_exact_generation_renews(tmp_path,phase):
    import os
    from app.controller_lease import FRAME
    folder,ns,old,new=setup(tmp_path)
    rd,wr=os.pipe()
    os.set_blocking(rd,False)
    stops=[]
    try:
        with GenerationLedger(folder,ns) as ledger:
            ledger.initialize(old,operator_approved=True)
            c=GenerationController(ledger,send=lambda _:None,receive=lambda:None,
                stop_and_verify=lambda:stops.append(True) or True)
            if phase=='initial':
                c.renew_progress(wr)
                assert len(os.read(rd,FRAME.size))==FRAME.size and not stops
            elif phase in ('armed','foreign'):
                if phase=='armed':ledger.arm(old,new)
                else:ns.changed=True
                with pytest.raises(EgressDenied):c.renew_progress(wr)
                assert c.failed and stops==[True]
        if phase=='reopened':
            with GenerationLedger(folder,ns) as ledger:
                c=GenerationController(ledger,send=lambda _:None,receive=lambda:None,
                    stop_and_verify=lambda:stops.append(True) or True)
                with pytest.raises(EgressDenied):c.renew_progress(wr)
                assert c.failed and stops==[True]
        with pytest.raises(BlockingIOError):os.read(rd,FRAME.size)
    finally:os.close(rd);os.close(wr)
