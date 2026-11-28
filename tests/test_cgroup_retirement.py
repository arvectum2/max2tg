import json

import pytest

from app.cgroup_retirement import RetirementJournal, DelegatedRetirement
from app.egress_policy import EgressDenied

PARENT={'boot':'12345678-1234-1234-1234-123456789abc','mount':1,'device':2,'inode':3}
TARGET=dict(PARENT,inode=4)
UNIT='/system.slice/max2tg-acceptance-123-1.service'


@pytest.fixture
def folder(tmp_path):
    path=tmp_path/'retained'
    path.mkdir(mode=0o700)
    return path


@pytest.mark.parametrize('state',['ARMED','STOPPING','RETIRED'])
def test_reopened_is_observation_only(folder,state):
    with RetirementJournal(folder) as j:
        j.arm(PARENT,TARGET,UNIT)
        if state!='ARMED':j.advance('STOPPING')
        if state=='RETIRED':j.advance('RETIRED')
    with RetirementJournal(folder) as j:
        evidence=j.inspect()
        assert evidence['state']==state and not evidence['live_launch_authorized']
        assert not evidence['automatic_retry']
        with pytest.raises(EgressDenied):j.arm(PARENT,TARGET,UNIT)
        with pytest.raises(EgressDenied):j.advance('RETIRED')


@pytest.mark.parametrize('target,unit',[(PARENT,UNIT),(dict(TARGET,mount=2),UNIT),
    (dict(TARGET,device=3),UNIT),(dict(TARGET,inode=True),UNIT),(TARGET,'/system.slice/ssh.service')])
def test_foreign_identity_denied(folder,target,unit):
    with RetirementJournal(folder) as j:
        with pytest.raises(EgressDenied):j.arm(PARENT,target,unit)
        assert not j.path.exists()


def test_no_skip_or_concurrent_writer(folder):
    with RetirementJournal(folder) as j:
        j.arm(PARENT,TARGET,UNIT)
        with pytest.raises(OSError):RetirementJournal(folder)
        with pytest.raises(EgressDenied):j.advance('RETIRED')
        j.advance('STOPPING')
        j.advance('RETIRED')
        with pytest.raises(EgressDenied):j.advance('STOPPING')


def test_tampered_record_denied(folder):
    with RetirementJournal(folder) as j:
        j.arm(PARENT,TARGET,UNIT)
        data=json.loads(j.path.read_text())
        data['target']['inode']=5
        j.path.write_text(json.dumps(data))
        with pytest.raises(EgressDenied):j.advance('STOPPING')


def test_failed_persistence_does_not_grant_retirement(folder,monkeypatch):
    with RetirementJournal(folder) as j:
        j.arm(PARENT,TARGET,UNIT)
        def fail(*args,**kwargs):raise OSError('synthetic fsync failure')
        monkeypatch.setattr('app.cgroup_retirement._write',fail)
        with pytest.raises(OSError):j.advance('STOPPING')
        assert j.inspect()['state']=='ARMED'


def test_no_approval_no_operation():
    # No cgroup access; exercise the one-use authorization gate.
    r=DelegatedRetirement.__new__(DelegatedRetirement)
    r._used=False
    def forbidden():raise AssertionError('unexpected cgroup access')
    r._check=forbidden
    with pytest.raises(EgressDenied):r.retire(operator_approved=False,verify_owned=forbidden)
    with pytest.raises(EgressDenied):r.retire(operator_approved=True,verify_owned=forbidden)
