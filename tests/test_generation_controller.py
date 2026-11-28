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
