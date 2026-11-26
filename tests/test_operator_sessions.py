import pytest
from app.capsule_registry import CapsuleRegistry
from app.operator_sessions import OperatorSessions
from app.encrypted_storage import create_key
from app.encrypted_credentials import install_encrypted_worker
from app.runtime_lock import RuntimeLock
from tests.test_shared_onboarding import bot,REQ,ACTOR,FORUM


def layout(tmp,sid):
    paths=[tmp/n for n in ('state','channel','secret','keys')]
    for p in paths:p.mkdir(mode=0o700)
    key=paths[3]/'worker.key';create_key(key,'max:'+sid);install_encrypted_worker(paths[2],sid,{'session_id':sid,'MAX_TOKEN':'synthetic','MAX_DEVICE_ID':'synthetic-device'},key)
    return (*paths[:3],key)


@pytest.mark.asyncio
async def test_operator_enroll_pause_reactivate_revoke(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');o=OperatorSessions(r,provisioning_stopped=True);b=bot()
    try:
        result=o.reserve(REQ,ACTOR,FORUM);sid=result['sid'];assert result['state']=='RESERVED' and not result['desired_active'];args=layout(tmp_path,sid)
        assert o.reserve(REQ,ACTOR,FORUM)==result
        with pytest.raises(PermissionError):o.provision(REQ,*args)
        assert (await o.verify(REQ,b))['state']=='FORUM_VERIFIED'
        assert o.provision(REQ,*args)['state']=='PROVISIONED';assert o.provision(REQ,*args)['state']=='PROVISIONED'
        assert (await o.activate(REQ,b,*args))['state']=='ACTIVE';assert o.onboarding.plane.route_group(FORUM,ACTOR)==sid
        assert o.pause(REQ,*args)['state']=='PAUSED';assert o.pause(REQ,*args)['state']=='PAUSED';assert o.onboarding.plane.route_group(FORUM,ACTOR) is None
        o.close();o=OperatorSessions(r,provisioning_stopped=True);assert o.status(REQ)['state']=='PAUSED'
        b.get_chat_member_count.return_value=3
        with pytest.raises(PermissionError):await o.activate(REQ,b,*args)
        assert o.status(REQ)['state']=='PAUSED';b.get_chat_member_count.return_value=2
        assert (await o.activate(REQ,b,*args))['state']=='ACTIVE';assert o.onboarding.plane.route_group(FORUM,ACTOR)==sid
        (args[2]/'max.enc').unlink();assert o.pause(REQ,*args)['state']=='PAUSED';assert o.revoke(REQ,*args)['state']=='REVOKED';assert o.revoke(REQ,*args)['state']=='REVOKED'
        with pytest.raises(PermissionError):await o.activate(REQ,b,*args)
        assert o.onboarding.plane.route_group(FORUM,ACTOR) is None
    finally:o.close();r.close()


@pytest.mark.asyncio
async def test_fences_and_wrong_worker(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry')
    try:
        with pytest.raises(PermissionError):OperatorSessions(r)
        lock=RuntimeLock(r.root/'shared-gateway.lock')
        try:
            with pytest.raises(BlockingIOError):OperatorSessions(r,provisioning_stopped=True)
        finally:lock.close()
        o=OperatorSessions(r,provisioning_stopped=True);b=bot()
        try:
            with pytest.raises(BlockingIOError):OperatorSessions(r,provisioning_stopped=True)
            with pytest.raises(BlockingIOError):RuntimeLock(r.root/'shared-provisioning.lock')
            sid=o.reserve(REQ,ACTOR,FORUM)['sid'];args=layout(tmp_path,sid);await o.verify(REQ,b);o.provision(REQ,*args)
            lock=RuntimeLock(args[0]/'worker.lock')
            try:
                with pytest.raises(BlockingIOError):await o.activate(REQ,b,*args)
            finally:lock.close()
            await o.activate(REQ,b,*args);(args[0]/'worker-acl.json').write_text('{}')
            for action in (o.pause,o.revoke):
                with pytest.raises(PermissionError):action(REQ,*args)
            assert o.status(REQ)['state']=='ACTIVE'
        finally:o.close()
        with pytest.raises(RuntimeError):o.reserve('b'*24,202,-100002)
    finally:r.close()
