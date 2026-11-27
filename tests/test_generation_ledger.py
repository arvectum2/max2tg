import json

import pytest

from app.egress_policy import EgressDenied
from app.generation_ledger import GenerationLedger
from app.linux_egress_policy import BrokerPins
from app.operator_pin_rotation import digest, frame, request
from tests.test_rotation_fence import setup


def next_pins(old,new,**patch):
    return BrokerPins.parse(dict(old.record(),expires_at=new.expires_at+60)|patch)


def rotate(ledger,ns,old,new,*,confirm=True):
    ns.old=old;ns.new=new;ns.changed=False
    nonce=ledger.arm(old,new)
    raw=frame(request(nonce,old,new))
    ledger.execute(raw)
    if confirm:ledger.confirm_adopted(nonce=nonce,pin_digest=digest(new),broker_ready=True)
    return nonce,raw


def test_two_generations_and_reopen(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    third=next_pins(old,new)
    with GenerationLedger(folder,ns) as g:
        g.initialize(old,operator_approved=True)
        _,raw=rotate(g,ns,old,new)
        rotate(g,ns,new,third)
        assert g.inspect()=={'generation':2,'state':'ADOPTED','matching_generations':[2],
                            'automatic_retry':False,'live_launch_authorized':False}
        with pytest.raises(EgressDenied):g.execute(raw)
    with GenerationLedger(folder,ns) as g:
        assert g.inspect()['generation']==2
        with pytest.raises(EgressDenied):g.arm(third,next_pins(new,third))
    assert len(ns.calls)==2


def test_old_role_cannot_return_as_other_role(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    with GenerationLedger(folder,ns) as g:
        g.initialize(old,operator_approved=True)
        rotate(g,ns,old,new)
        bad=next_pins(new,new,telegram=list(old.max_text))
        with pytest.raises(EgressDenied):g.arm(new,bad)
        assert len(ns.calls)==1


@pytest.mark.parametrize('mode',['missing','wrong_nonce','wrong_digest','not_ready'])
def test_adoption_required(tmp_path,mode):
    folder,ns,old,new=setup(tmp_path)
    with GenerationLedger(folder,ns) as g:
        g.initialize(old,operator_approved=True)
        nonce,_=rotate(g,ns,old,new,confirm=False)
        if mode!='missing':
            with pytest.raises(EgressDenied):
                g.confirm_adopted(nonce='0'*48 if mode=='wrong_nonce' else nonce,
                                  pin_digest='bad' if mode=='wrong_digest' else digest(new),
                                  broker_ready=mode!='not_ready')
        with pytest.raises(EgressDenied):g.arm(new,next_pins(old,new))
        assert g.inspect()['state']=='KERNEL_VERIFIED'
    assert len(ns.calls)==1


def test_uncertainty_blocks_restart(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    with GenerationLedger(folder,ns) as g:
        g.initialize(old,operator_approved=True)
        nonce=g.arm(old,new)
        original=ns.apply_reviewed
        def incomplete(*args):
            original(*args)
            raise TimeoutError('synthetic incomplete operation')
        ns.apply_reviewed=incomplete
        with pytest.raises(TimeoutError):g.execute(frame(request(nonce,old,new)))
    with GenerationLedger(folder,ns) as g:
        assert g.inspect()['state']=='IN_FLIGHT'
        with pytest.raises(EgressDenied):g.arm(new,next_pins(old,new))
    assert len(ns.calls)==1


def test_skipped_generation_and_concurrent_writer(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    with GenerationLedger(folder,ns) as g:
        g.initialize(old,operator_approved=True)
        with pytest.raises(OSError):GenerationLedger(folder,ns)
        with pytest.raises(EgressDenied):g.arm(new,next_pins(old,new))
        assert not ns.calls


def test_history_tampering_denied(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    with GenerationLedger(folder,ns) as g:
        g.initialize(old,operator_approved=True)
        rotate(g,ns,old,new)
        data=json.loads(g.path.read_text())
        data['rotations'][0]['generation']=3
        g.path.write_text(json.dumps(data))
        with pytest.raises(EgressDenied):g.inspect()
        with pytest.raises(EgressDenied):g.arm(new,next_pins(old,new))
        assert len(ns.calls)==1


def test_bounded_history(tmp_path,monkeypatch):
    folder,ns,old,new=setup(tmp_path)
    monkeypatch.setattr('app.generation_ledger.MAX_ROTATIONS',1)
    with GenerationLedger(folder,ns) as g:
        g.initialize(old,operator_approved=True)
        rotate(g,ns,old,new)
        with pytest.raises(EgressDenied):g.arm(new,next_pins(old,new))
    assert len(ns.calls)==1


def test_intent_write_failure_no_mutation(tmp_path,monkeypatch):
    folder,ns,old,new=setup(tmp_path)
    with GenerationLedger(folder,ns) as g:
        g.initialize(old,operator_approved=True)
        nonce=g.arm(old,new)
        def failed(*args,**kwargs):raise OSError('synthetic persistence failure')
        monkeypatch.setattr('app.generation_ledger._write',failed)
        with pytest.raises(OSError):g.execute(frame(request(nonce,old,new)))
        with pytest.raises(EgressDenied):g.execute(frame(request(nonce,old,new)))
        assert g.inspect()['state']=='ARMED'
    assert not ns.calls


def test_invalid_adoption_not_retried(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    with GenerationLedger(folder,ns) as g:
        g.initialize(old,operator_approved=True)
        nonce,_=rotate(g,ns,old,new,confirm=False)
        with pytest.raises(EgressDenied):
            g.confirm_adopted(nonce=nonce,pin_digest=digest(new),broker_ready=False)
        with pytest.raises(EgressDenied):
            g.confirm_adopted(nonce=nonce,pin_digest=digest(new),broker_ready=True)
        assert g.inspect()['state']=='KERNEL_VERIFIED'
