import pytest

from app.cgroup_readback import parse_events, readback
from app.egress_policy import EgressDenied


@pytest.mark.parametrize('raw',['populated 1\nfrozen 0\npopulated 0\n','populated 2\nfrozen 0\n',
    'populated 0\n','populated 0\nfrozen 0\nforeign 1\n','populated 0 0\nfrozen 0\n'])
def test_invalid_events(raw):
    with pytest.raises(EgressDenied):parse_events(raw)


@pytest.mark.parametrize('populated',[False,True])
def test_readback_bound_and_recursive(monkeypatch,populated):
    from app import cgroup_readback as c
    expected={'boot':'synthetic','mount':1,'device':2,'inode':3}
    monkeypatch.setattr(c,'identity',lambda fd:expected)
    data={'cgroup.type':'domain\n','cgroup.events':f'populated {int(populated)}\nfrozen 0\n','cgroup.procs':''}
    monkeypatch.setattr(c,'_read',lambda fd,name:data[name])
    out=readback(10,expected=expected)
    assert out['recursively_empty'] is not populated
    assert out['direct_process_count']==0 and not out['live_launch_authorized']
    with pytest.raises(EgressDenied):readback(10,expected=dict(expected,inode=4))


def test_inconsistent_population_denied(monkeypatch):
    from app import cgroup_readback as c
    monkeypatch.setattr(c,'identity',lambda fd:{})
    data={'cgroup.type':'domain\n','cgroup.events':'populated 0\nfrozen 0\n','cgroup.procs':'42\n'}
    monkeypatch.setattr(c,'_read',lambda fd,name:data[name])
    with pytest.raises(EgressDenied):readback(10,expected={})
