from types import SimpleNamespace

import pytest

from app.egress_policy import EgressDenied
from app.linux_egress_policy import BrokerPins
from app.recovery_transition import RecoveryTransition
from app.rotation_fence import RotationFence
from tests.test_rotation_fence import setup, arm


@pytest.fixture
def prepared_source(tmp_path):
    folder,old_ns,old,new=setup(tmp_path)
    old_ns.retired=lambda:True
    with RotationFence(folder,old_ns) as fence:
        fence.execute(arm(fence,old,new))
    pins=BrokerPins.parse(dict(new.record(),expires_at=new.expires_at+60))
    # Reuse the existing kernel observation double; no kernel commands.
    fake=type(old_ns)(old,pins)
    fake.binding=lambda:dict(old_ns.binding(),pid=13,namespace=[1,3])
    observed=fake.observe
    fake.observe=lambda:observed() if fake.changed else {'nftables':[{'metainfo':{'json_schema_version':1}}]}
    state=SimpleNamespace(stopped=True,installs=0)
    def install(batch):
        state.installs+=1
        fake.changed=True
    with RotationFence(folder,old_ns) as retained:
        yield retained,fake,pins,state,install


def prepare(t,pins,state,install,approved=True):
    return t.prepare(pins,operator_approved=approved,
                     stopped=lambda:state.stopped,install=install)


def test_one_preparation_only(prepared_source):
    retained,ns,pins,state,install=prepared_source
    original=retained.path.read_bytes()
    t=RecoveryTransition(retained,ns)
    assert prepare(t,pins,state,install)['recovery_prepared']
    assert t.inspect()['prepared_policy_matches']
    assert not t.inspect()['live_launch_authorized']
    with pytest.raises(EgressDenied):
        prepare(RecoveryTransition(retained,ns),pins,state,install)
    assert state.installs==1
    assert retained.path.read_bytes()==original


@pytest.mark.parametrize('missing',['approval','stopped','retired','fresh_identity','new_expiry'])
def test_missing_precondition(prepared_source,missing):
    retained,ns,pins,state,install=prepared_source
    if missing=='stopped':state.stopped=False
    if missing=='retired':retained.namespace.retired=lambda:False
    if missing=='fresh_identity':ns.binding=lambda:retained.identity
    if missing=='new_expiry':pins=BrokerPins.parse(retained._load()['replacement'])
    with pytest.raises(EgressDenied):
        prepare(RecoveryTransition(retained,ns),pins,state,install,missing!='approval')
    assert state.installs==0
    assert not (retained.root/'recovery.json').exists()


def test_uncertain_install_stays_blocked(prepared_source):
    retained,ns,pins,state,install=prepared_source
    def incomplete(batch):
        install(batch)
        raise TimeoutError('synthetic incomplete operation')
    t=RecoveryTransition(retained,ns)
    with pytest.raises(TimeoutError):
        prepare(t,pins,state,incomplete)
    assert t.inspect()['state']=='PREPARING'
    assert not t.inspect()['prepared_policy_matches']
    with pytest.raises(EgressDenied):
        prepare(RecoveryTransition(retained,ns),pins,state,install)
    assert state.installs==1


def test_existing_policy_not_adopted(prepared_source):
    retained,ns,pins,state,install=prepared_source
    ns.changed=True
    t=RecoveryTransition(retained,ns)
    with pytest.raises(EgressDenied):
        prepare(t,pins,state,install)
    assert state.installs==0
    assert not t.inspect()['prepared_policy_matches']


def test_lost_stop_boundary(prepared_source):
    retained,ns,pins,state,install=prepared_source
    def interrupted(batch):
        install(batch)
        state.stopped=False
    t=RecoveryTransition(retained,ns)
    with pytest.raises(EgressDenied):
        prepare(t,pins,state,interrupted)
    assert state.installs==1
    assert t.inspect()['state']=='PREPARING'
