"""Managed lifecycle contracts; no real accounts or Docker daemon."""
import pytest
import tempfile
from pathlib import Path
from app.lifecycle_reconcile import LifecycleReconciler
from app.container_supervisor import OfflineContainerSupervisor
from tests.test_container_supervisor import Backend,IMAGE,CODE,A,VOLUMES
from tests.test_shared_onboarding import bot,REQ,ACTOR,FORUM
from app.capsule_registry import CapsuleRegistry
from app.shared_onboarding import SharedOnboarding
from app.shared_gateway_runtime import SharedGatewayRuntime


def test_snapshot_gates_start_and_preserves_retry_budget(tmp_path):
    tmp_path.chmod(0o700);clock=[100];b=Backend();s=OfflineContainerSupervisor(tmp_path,IMAGE,CODE,backend=b,clock=lambda:clock[0]);b.owner=s.owner
    def snap(active,ready,at=None):return {'at':clock[0] if at is None else at,'sessions':{A:{'active':active,'ready':ready}}}
    try:
        s.register(A,*VOLUMES);r=LifecycleReconciler(s,clock=lambda:clock[0]);r.reconcile(snap(True,False));assert not b.calls
        r.reconcile(snap(True,True));assert s.row(A)['attempts']==1 and len(b.calls)==1
        clock[0]+=3;r.reconcile(snap(True,False));assert s.row(A)['status']=='WAITING_CHANNEL' and s.row(A)['attempts']==1 and len(b.calls)==1
        with pytest.raises(ValueError):r.reconcile(snap(False,False,at=90))
        with pytest.raises(ValueError):r.reconcile({'at':103,'sessions':{}})
        with pytest.raises(ValueError):r.reconcile(snap(False,True))
        assert s.row(A)['desired']==1
        r.reconcile(snap(False,False));assert s.row(A)['desired']==0
        s.close();s=OfflineContainerSupervisor(tmp_path,IMAGE,CODE,backend=b,clock=lambda:clock[0]);r=LifecycleReconciler(s,clock=lambda:clock[0])
        with pytest.raises(ValueError):r.reconcile(snap(True,True,at=102))
        assert len(b.calls)==1 and s.row(A)['desired']==0
    finally:s.close()


@pytest.mark.asyncio
async def test_managed_channel_activation_and_revocation(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r);b=bot();sid=o.reserve(REQ,ACTOR,FORUM);await o.verify_forum(REQ,b);o.confirm_provisioned(REQ,check=lambda *_:True)
    short=tempfile.TemporaryDirectory(prefix='m2lc-',dir='/tmp');channel=Path(short.name);g=SharedGatewayRuntime(r,b,channels={sid:channel},external_only=True,managed=True)
    try:
        await g.start();assert g.lifecycle_snapshot()['sessions'][sid]=={'active':False,'ready':False};assert not (channel/'egress.sock').exists()
        await o.activate(REQ,b,check=lambda *_:True);assert g.lifecycle_snapshot()['sessions'][sid]=={'active':True,'ready':False}
        assert (await g.reconcile_channels())['sessions'][sid]=={'active':True,'ready':True};assert (channel/'egress.sock').is_socket()
        o.revoke(REQ);assert g.plane.route_group(FORUM,ACTOR) is None
        assert (await g.reconcile_channels())['sessions'][sid]=={'active':False,'ready':False};assert not (channel/'egress.sock').exists()
        assert (await g.reconcile_channels())['sessions'][sid]=={'active':False,'ready':False}
    finally:await g.close();r.close();short.cleanup()
