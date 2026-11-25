"""No Docker daemon required for deterministic persistent supervisor contracts."""
from pathlib import Path
import subprocess
import pytest
from app.container_supervisor import OfflineContainerSupervisor

IMAGE='sha256:'+'1'*64
CODE=Path(__file__).resolve().parents[1]
A='a'*24;B='b'*24
VOLUMES=['m2tg-orch-test-state','m2tg-orch-test-channel','m2tg-orch-test-secret']

class Backend:
    owner=None
    def __init__(self):self.calls=[];self.container=None;self.foreign=False
    def volume(self,n):return {'Labels':{'max2tg.offline':self.owner if not self.foreign else 'other'},'Driver':'local','Options':None}
    def inspect(self,n):return self.container
    def create(self,args):
        self.calls.append(('create',args));raise subprocess.TimeoutExpired('synthetic create',1)
    def start(self,cid):self.calls.append(('start',cid))
    def stop(self,cid):self.calls.append(('stop',cid))


def test_attempts_persist_and_quarantine(tmp_path):
    tmp_path.chmod(0o700);clock=[0];b=Backend();s=OfflineContainerSupervisor(tmp_path,IMAGE,CODE,backend=b,clock=lambda:clock[0]);b.owner=s.owner
    try:
        s.register(A,*VOLUMES);s.desired(A,True);s.tick();assert s.row(A)['attempts']==1 and s.row(A)['status']=='ERROR'
        s.tick();assert s.row(A)['status']=='BACKOFF' and len(b.calls)==1
        s.close();s=OfflineContainerSupervisor(tmp_path,IMAGE,CODE,backend=b,clock=lambda:clock[0]);assert s.owner==b.owner
        for _ in range(3):clock[0]+=100;s.tick()
        s.tick();assert s.row(A)['status']=='QUARANTINED' and len(b.calls)==4
        s.desired(A,True);s.tick();assert len(b.calls)==4
        s.desired(A,False);s.tick();assert s.row(A)['status']=='STOPPED'
        s.desired(A,True);s.tick();assert s.row(A)['attempts']==1 and len(b.calls)==5
    finally:s.close()


def test_foreign_identity_and_volume_overlap_fail_closed(tmp_path):
    tmp_path.chmod(0o700);b=Backend();s=OfflineContainerSupervisor(tmp_path,IMAGE,CODE,backend=b);b.owner=s.owner
    try:
        s.register(A,*VOLUMES)
        with pytest.raises(ValueError,match='reuse'):s.register(B,VOLUMES[0],'m2tg-orch-other-channel','m2tg-orch-other-secret')
        b.foreign=True
        with pytest.raises(PermissionError):s.register(B,'m2tg-orch-other-state','m2tg-orch-other-channel','m2tg-orch-other-secret')
        b.container={'Config':{'Labels':{'max2tg.offline':'foreign'}}};s.desired(A,True);s.tick();assert s.row(A)['status']=='BLOCKED';assert not b.calls
        s.desired(A,False);s.tick();assert not b.calls
        with pytest.raises(BlockingIOError):OfflineContainerSupervisor(tmp_path,IMAGE,CODE,backend=b)
    finally:s.close()


def test_fixture_selection_is_immutable_and_allowlisted(tmp_path):
    tmp_path.chmod(0o700);b=Backend();s=OfflineContainerSupervisor(tmp_path,IMAGE,CODE,backend=b);b.owner=s.owner
    try:
        with pytest.raises(ValueError):s.register(A,*VOLUMES,fixture='arbitrary.module')
        s.register(A,*VOLUMES,fixture='bridge');args=s._args(s.row(A))
        assert args[-3:]==['-m','tests.supervised_bridge_fixture',A]
        assert f'type=bind,source={CODE / "app"},target=/src/app,readonly' in args
        assert '--network' in args and args[args.index('--network')+1]=='none'
        with pytest.raises(ValueError,match='Immutable'):s.register(A,*VOLUMES)
    finally:s.close()
