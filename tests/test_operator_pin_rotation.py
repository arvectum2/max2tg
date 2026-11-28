import os
import time
from copy import deepcopy

import pytest

from app.egress_policy import EgressDenied
from app.linux_egress_policy import BrokerPins
from app.operator_pin_rotation import (
    RotationGrant, frame, parse, request, read_receipt, read_frame_fd, MAX_FRAME)
from tests.test_linux_namespace_gate import sample


class Namespace:
    def __init__(self, old, new):
        self.old=old;self.new=new;self.calls=[];self.changed=False;self.fail=False
    def check(self):
        if self.fail:raise EgressDenied('Identity changed')
    def observe(self):
        self.check()
        p=self.new if self.changed else self.old
        v=deepcopy(sample())
        v['nftables'][5]['set']['elem']=[
            {'elem':{'val':ip,'timeout':p.remaining(),'expires':p.remaining()-2}}
            for ip in p.max_text+p.telegram]
        return v
    def apply_reviewed(self,old,new,batch):
        self.check()
        assert old==self.old and new==self.new
        self.calls.append(batch);self.changed=True


def fixture():
    now=int(time.time())
    old=BrokerPins.parse({'max-text':['8.8.8.8'],'telegram':['9.9.9.9'],'expires_at':now+60})
    new=BrokerPins.parse({'max-text':['1.1.1.1'],'telegram':['9.9.9.9'],'expires_at':now+120})
    ns=Namespace(old,new)
    return ns, RotationGrant(ns,old,new),old,new


def test_one_exact_grant_and_matching_receipt():
    ns,g,old,new=fixture()
    req=frame(request(g.nonce,old,new))
    raw=g.execute(req)
    assert read_receipt(raw,g.nonce,old,new)=={
        'verified':True,'pins':2,'rotated':True,'live_launch_authorized':False}
    assert g.state=='VERIFIED' and len(ns.calls)==1
    with pytest.raises(EgressDenied):g.execute(req)
    assert len(ns.calls)==1


@pytest.mark.parametrize('patch',[{'nonce':'wrong'},{'version':True},{'current':'wrong'},
    {'replacement':'wrong'},{'operation':'run'},{'extra':'nft'}])
def test_bad_request_consumes_grant_without_mutation(patch):
    ns,g,old,new=fixture()
    with pytest.raises(EgressDenied):g.execute(frame(request(g.nonce,old,new)|patch))
    assert g.state=='BLOCKED' and not ns.calls
    with pytest.raises(EgressDenied):g.execute(frame(request(g.nonce,old,new)))


def test_identity_change_denies_before_apply():
    ns,g,old,new=fixture();ns.fail=True
    with pytest.raises(EgressDenied):g.execute(frame(request(g.nonce,old,new)))
    assert not ns.calls and g.state=='BLOCKED'


def test_wrong_receipt_rejected():
    ns,g,old,new=fixture()
    raw=g.execute(frame(request(g.nonce,old,new)))
    with pytest.raises(EgressDenied):read_receipt(raw,'other',old,new)


@pytest.mark.parametrize('raw',[b'{}',b'{"a":1,"a":2}\n',b'[]\n',b'\xff\n',b'{'*1024+b'\n'])
def test_malformed_frames(raw):
    with pytest.raises(EgressDenied):parse(raw)


def test_frame_reader_bounded():
    r,w=os.pipe()
    try:
        os.write(w,b'{}\n')
        assert read_frame_fd(r)==b'{}\n'
        os.write(w,b'{')
        with pytest.raises(EgressDenied,match='timeout'):read_frame_fd(r,timeout=.02)
        os.write(w,b'x'*MAX_FRAME)
        with pytest.raises(EgressDenied,match='large'):read_frame_fd(r)
    finally:
        os.close(r);os.close(w)


def test_identity_change_after_mutation_blocks_receipt_without_retry():
    ns,g,old,new=fixture()
    apply=ns.apply_reviewed
    def change(*args):
        apply(*args)
        ns.fail=True
    ns.apply_reviewed=change
    raw=frame(request(g.nonce,old,new))
    with pytest.raises(EgressDenied):g.execute(raw)
    assert len(ns.calls)==1 and g.state=='BLOCKED'
    with pytest.raises(EgressDenied):g.execute(raw)
    assert len(ns.calls)==1
