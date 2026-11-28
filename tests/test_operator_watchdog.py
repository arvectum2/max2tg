import json

import pytest

from app.egress_policy import EgressDenied
from app.operator_watchdog import WatchdogJournal

TARGET={'pid':42,'boot':'12345678-1234-1234-1234-123456789abc','inode':100}


@pytest.fixture
def anchor(tmp_path):
    p=tmp_path/'watchdog'
    p.mkdir(mode=0o700)
    return p


@pytest.mark.parametrize('phase',['ARMED','STOPPING','STOPPED','EXITED','UNVERIFIED'])
def test_reopen_observation_only(anchor,phase):
    with WatchdogJournal(anchor) as journal:
        journal.arm(TARGET)
        if phase=='EXITED':journal.advance('EXITED','TARGET_EXIT')
        elif phase!='ARMED':
            journal.advance('STOPPING','LEASE_EXPIRED')
            if phase!='STOPPING':journal.advance(phase,'LEASE_EXPIRED')
    with WatchdogJournal(anchor) as journal:
        evidence=journal.inspect()
        assert evidence['state']==phase
        assert not evidence['automatic_retry'] and not evidence['live_launch_authorized']
        with pytest.raises(EgressDenied):journal.arm(TARGET)
        with pytest.raises(EgressDenied):journal.advance('STOPPED','LEASE_EXPIRED')


def test_concurrent_writer_and_wrong_transition(anchor):
    with WatchdogJournal(anchor) as journal:
        journal.arm(TARGET)
        with pytest.raises(OSError):WatchdogJournal(anchor)
        with pytest.raises(EgressDenied):journal.advance('STOPPED','LEASE_EXPIRED')
        journal.advance('STOPPING','EOF')
        with pytest.raises(EgressDenied):journal.advance('STOPPED','LEASE_EXPIRED')
        journal.advance('STOPPED','EOF')


@pytest.mark.parametrize('corruption',['duplicate','reason','pid','inode','boot','version'])
def test_invalid_evidence(anchor,corruption):
    with WatchdogJournal(anchor) as journal:
        journal.arm(TARGET)
        raw=journal.path.read_text()
        data=json.loads(raw)
        if corruption=='duplicate':raw=raw.replace('"version":1','"version":1,"version":1')
        else:
            if corruption=='reason':data['reason']='EOF'
            if corruption=='pid':data['target']['pid']=True
            if corruption=='inode':data['target']['inode']=0
            if corruption=='boot':data['target']['boot']='x'*36
            if corruption=='version':data['version']=True
            raw=json.dumps(data)
        journal.path.write_text(raw)
        with pytest.raises(EgressDenied):journal.inspect()
        with pytest.raises(EgressDenied):journal.advance('STOPPING','EOF')


def test_ambiguous_persistence_not_advanced(anchor,monkeypatch):
    with WatchdogJournal(anchor) as journal:
        journal.arm(TARGET)
        journal.advance('STOPPING','EOF')
        def failure(*args,**kwargs):raise OSError('synthetic fsync failure')
        monkeypatch.setattr('app.operator_watchdog._write',failure)
        with pytest.raises(OSError):journal.advance('STOPPED','EOF')
    with WatchdogJournal(anchor) as journal:
        assert journal.inspect()['state']=='STOPPING'
        with pytest.raises(EgressDenied):journal.arm(TARGET)
