import json

import pytest

from app.egress_policy import EgressDenied
from app.operator_pin_rotation import frame, request
from app.rotation_fence import RotationFence
from tests.test_operator_pin_rotation import fixture


def setup(tmp_path):
    ns,_,old,new=fixture()
    ns.binding=lambda:{'boot':'synthetic','pid':12,'start':42,'namespace':[1,2]}
    folder=tmp_path/'anchor'
    folder.mkdir(mode=0o700)
    return folder,ns,old,new


def arm(f,old,new):
    return frame(request(f.arm(old,new),old,new))


def test_success_persists_without_restart_authority(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    with RotationFence(folder,ns) as f:
        raw=arm(f,old,new)
        assert f.execute(raw)
        assert f.inspect()=={'state':'KERNEL_VERIFIED','matches':['replacement'],
            'automatic_retry':False,'live_launch_authorized':False,'requires_reconciliation':True}
    with RotationFence(folder,ns) as f:
        assert f.inspect()['state']=='KERNEL_VERIFIED'
        with pytest.raises(EgressDenied):f.execute(raw)
        with pytest.raises(EgressDenied):f.arm(old,new)
    assert len(ns.calls)==1


@pytest.mark.parametrize('phase',['before','after'])
def test_ambiguous_apply_retains_intent(tmp_path,phase):
    folder,ns,old,new=setup(tmp_path)
    apply=ns.apply_reviewed
    def fail(*args):
        if phase=='after':apply(*args)
        raise TimeoutError('unknown')
    ns.apply_reviewed=fail
    with RotationFence(folder,ns) as f:
        raw=arm(f,old,new)
        with pytest.raises(TimeoutError):f.execute(raw)
    with RotationFence(folder,ns) as f:
        assert f.inspect()['state']=='IN_FLIGHT'
        assert f.inspect()['matches']==(['current'] if phase=='before' else ['replacement'])
        with pytest.raises(EgressDenied):f.execute(raw)
        with pytest.raises(EgressDenied):f.arm(old,new)
    assert len(ns.calls)==(phase=='after')


def test_armed_restart_is_not_rearmed(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    with RotationFence(folder,ns) as f:raw=arm(f,old,new)
    with RotationFence(folder,ns) as f:
        assert f.inspect()['state']=='ARMED'
        with pytest.raises(EgressDenied):f.execute(raw)
        with pytest.raises(EgressDenied):f.arm(old,new)
    assert not ns.calls


def test_concurrent_controller_and_closed_handle_denied(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    with RotationFence(folder,ns) as f:
        with pytest.raises(OSError):RotationFence(folder,ns)
    with pytest.raises(EgressDenied):f.arm(old,new)


def test_foreign_binding_refused(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    with RotationFence(folder,ns) as f:arm(f,old,new)
    ns.binding=lambda:{'boot':'other','pid':12,'start':42,'namespace':[1,2]}
    with RotationFence(folder,ns) as f:
        with pytest.raises(EgressDenied):f.inspect()
        with pytest.raises(EgressDenied):f.arm(old,new)


def test_corrupt_or_changed_fence_never_mutates(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    with RotationFence(folder,ns) as f:
        raw=arm(f,old,new)
        p=folder/'rotation.json'
        value=json.loads(p.read_text());value['nonce']='0'*48
        p.write_text(json.dumps(value))
        with pytest.raises(EgressDenied):f.execute(raw)
        assert not ns.calls
        with pytest.raises(EgressDenied):f.execute(raw)


def test_symlink_record_refused(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    target=tmp_path/'target';target.write_text('untouched')
    (folder/'rotation.json').symlink_to(target)
    with RotationFence(folder,ns) as f:
        with pytest.raises(EgressDenied):f.arm(old,new)
        with pytest.raises(OSError):f.inspect()
    assert target.read_text()=='untouched'


def test_invalid_request_consumes_durable_intent(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    with RotationFence(folder,ns) as f:
        arm(f,old,new)
        with pytest.raises(EgressDenied):f.execute(b'{}\n')
        assert f.inspect()['state']=='IN_FLIGHT'
        assert not ns.calls


def test_missing_or_changed_rule_handle_not_adopted(tmp_path):
    folder,ns,old,new=setup(tmp_path)
    with RotationFence(folder,ns) as f:
        raw=arm(f,old,new)
        observe=ns.observe
        def changed():
            v=observe();v['nftables'][6]['rule']['handle']=999
            return v
        ns.observe=changed
        assert f.inspect()['matches']==[]
        with pytest.raises(EgressDenied):f.execute(raw)
        assert not ns.calls


def test_intent_write_failure_prevents_apply(tmp_path,monkeypatch):
    folder,ns,old,new=setup(tmp_path)
    with RotationFence(folder,ns) as f:
        raw=arm(f,old,new)
        def fail(*args,**kwargs):raise OSError('synthetic fsync failure')
        monkeypatch.setattr('app.rotation_fence._write',fail)
        with pytest.raises(OSError):f.execute(raw)
        assert not ns.calls
        with pytest.raises(EgressDenied):f.execute(raw)
        assert f.inspect()['state']=='ARMED'


def test_verified_write_failure_does_not_emit_receipt(tmp_path,monkeypatch):
    folder,ns,old,new=setup(tmp_path)
    with RotationFence(folder,ns) as f:
        raw=arm(f,old,new)
        publish=f._publish
        def fail(record,**kwargs):
            if record['state']=='KERNEL_VERIFIED':raise OSError('synthetic fsync failure')
            publish(record,**kwargs)
        monkeypatch.setattr(f,'_publish',fail)
        with pytest.raises(OSError):f.execute(raw)
    with RotationFence(folder,ns) as f:
        assert f.inspect()['state']=='IN_FLIGHT'
        assert f.inspect()['matches']==['replacement']
        with pytest.raises(EgressDenied):f.execute(raw)
    assert len(ns.calls)==1
