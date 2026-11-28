import json
from types import SimpleNamespace

import pytest

from app.egress_policy import EgressDenied
from app.generation_ledger import GenerationLedger
from app.linux_egress_policy import BrokerPins
from app.operator_pin_rotation import digest
from app.recovery_transition import RecoveryTransition
from tests.test_generation_ledger import rotate, next_pins
from tests.test_rotation_fence import setup


@pytest.fixture
def lineage(tmp_path):
    folder,old_ns,old,new=setup(tmp_path)
    old_ns.retired=lambda:True
    with GenerationLedger(folder,old_ns) as g:
        g.initialize(old,operator_approved=True)
        rotate(g,old_ns,old,new,confirm=False)
    pins=BrokerPins.parse(dict(new.record(),expires_at=new.expires_at+60))
    ns=type(old_ns)(new,pins)
    ns.binding=lambda:dict(old_ns.binding(),pid=13,namespace=[1,3])
    observe=ns.observe
    ns.observe=lambda:observe() if ns.changed else {'nftables':[{'metainfo':{'json_schema_version':1}}]}
    state=SimpleNamespace(installs=0,stopped=True)
    def install(batch):
        state.installs+=1
        ns.changed=True
    with GenerationLedger(folder,old_ns) as g:
        yield g,ns,pins,state,install,old


def successor(g,ns,pins,state,install):
    return RecoveryTransition(g,ns).prepare_successor(pins,operator_approved=True,
        stopped=lambda:state.stopped,install=install)


def test_recovery_keeps_history_and_continues(lineage):
    g,ns,pins,state,install,old=lineage
    original=g.path.read_bytes()
    fresh,nonce,result=successor(g,ns,pins,state,install)
    with fresh:
        assert result['recovery_prepared'] and not result['live_launch_authorized']
        assert fresh.inspect()['generation']==2
        assert fresh.inspect()['state']=='PREPARED'
        with pytest.raises(EgressDenied):fresh.arm(pins,next_pins(pins,pins))
        fresh.confirm_recovered(nonce=nonce,pin_digest=digest(pins),broker_ready=True)
        bad=next_pins(pins,pins,telegram=list(old.max_text))
        with pytest.raises(EgressDenied):fresh.arm(pins,bad)
        final=next_pins(old,pins)
        # Restore the ordinary kernel observation double for further rotations.
        ns.old=pins;ns.new=final;ns.changed=True
        ns.observe=type(ns).observe.__get__(ns)
        ns.old=ns.new=pins
        rotate(fresh,ns,pins,final)
        assert fresh.inspect()['generation']==3
    with GenerationLedger(g.root/'successor',ns) as reopened:
        assert reopened.inspect()['state']=='ADOPTED'
        with pytest.raises(EgressDenied):reopened.arm(final,next_pins(pins,final))
    assert g.path.read_bytes()==original and state.installs==1
    with pytest.raises(EgressDenied):successor(g,ns,pins,state,install)


def test_lost_activation_never_reopens(lineage):
    g,ns,pins,state,install,_=lineage
    fresh,nonce,_=successor(g,ns,pins,state,install)
    with fresh:
        with pytest.raises(EgressDenied):
            fresh.confirm_recovered(nonce=nonce,pin_digest=digest(pins),broker_ready=False)
        with pytest.raises(EgressDenied):
            fresh.confirm_recovered(nonce=nonce,pin_digest=digest(pins),broker_ready=True)
    with GenerationLedger(g.root/'successor',ns) as reopened:
        assert reopened.inspect()['state']=='PREPARED'
        with pytest.raises(EgressDenied):reopened.arm(pins,next_pins(pins,pins))


def test_source_change_blocks_successor(lineage):
    g,ns,pins,state,install,_=lineage
    fresh,nonce,_=successor(g,ns,pins,state,install)
    with fresh:
        g.path.write_bytes(g.path.read_bytes()+b' ')
        with pytest.raises(EgressDenied):
            fresh.confirm_recovered(nonce=nonce,pin_digest=digest(pins),broker_ready=True)
        with pytest.raises(EgressDenied):fresh.inspect()


def test_uncertain_preparation_no_successor(lineage):
    g,ns,pins,state,install,_=lineage
    def incomplete(batch):
        install(batch)
        raise TimeoutError('synthetic incomplete operation')
    with pytest.raises(TimeoutError):successor(g,ns,pins,state,incomplete)
    assert not (g.root/'successor').exists()
    with pytest.raises(EgressDenied):successor(g,ns,pins,state,install)
    assert state.installs==1


def test_transition_change_blocks_activation(lineage):
    g,ns,pins,state,install,_=lineage
    fresh,nonce,_=successor(g,ns,pins,state,install)
    with fresh:
        p=g.root/'recovery.json'
        data=json.loads(p.read_text())
        data['state']='PREPARING'
        p.write_text(json.dumps(data))
        with pytest.raises(EgressDenied):
            fresh.confirm_recovered(nonce=nonce,pin_digest=digest(pins),broker_ready=True)
        with pytest.raises(EgressDenied):fresh.inspect()


def test_second_recovery_preserves_prior_epochs(lineage):
    g,ns,pins,state,install,old=lineage
    first,nonce,_=successor(g,ns,pins,state,install)
    with first:
        first.confirm_recovered(nonce=nonce,pin_digest=digest(pins),broker_ready=True)
    ns.retired=lambda:True
    renewed=next_pins(pins,pins)
    fresh_ns=type(ns)(pins,renewed)
    fresh_ns.binding=lambda:dict(ns.binding(),pid=14,namespace=[1,4])
    observe=fresh_ns.observe
    fresh_ns.observe=lambda:observe() if fresh_ns.changed else {'nftables':[{'metainfo':{'json_schema_version':1}}]}
    with GenerationLedger(g.root/'successor',ns) as parent:
        fresh,nonce,_=successor(parent,fresh_ns,renewed,state,
            lambda batch:setattr(fresh_ns,'changed',True))
        with fresh:
            fresh.confirm_recovered(nonce=nonce,pin_digest=digest(renewed),broker_ready=True)
            assert fresh.inspect()['generation']==3
            assert len(fresh._load()[1])==4
            bad=next_pins(renewed,renewed,telegram=list(old.max_text))
            with pytest.raises(EgressDenied):fresh.arm(renewed,bad)
