"""Trusted onboarding contract tests, synthetic identities only."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.capsule_registry import CapsuleRegistry
from app.shared_onboarding import SharedOnboarding

REQ='a'*24;ACTOR=101;FORUM=-100001

def bot(actor=ACTOR,forum=FORUM):
    return NS(get_me=AsyncMock(return_value=NS(id=999)),get_chat=AsyncMock(return_value=NS(id=forum,type='supergroup',is_forum=True,username=None,active_usernames=[],linked_chat_id=None)),get_chat_member=AsyncMock(side_effect=lambda chat,user:NS(user=NS(id=user),status='creator' if user==actor else 'administrator',can_manage_topics=True)),get_chat_member_count=AsyncMock(return_value=2))

@pytest.mark.asyncio
async def test_resume_registration_and_activation(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r);b=bot()
    sid=o.reserve(REQ,ACTOR,FORUM);assert o.reserve(REQ,ACTOR,FORUM)==sid and len(r.list_capsules())==1
    assert o.plane.route_group(FORUM,ACTOR) is None
    with pytest.raises(PermissionError):o.ipc_key(REQ)
    assert await o.verify_forum(REQ,b)=='FORUM_VERIFIED';key=o.ipc_key(REQ).read_bytes();assert len(key)==32
    r.close();r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r)
    try:
        assert o.reserve(REQ,ACTOR,FORUM)==sid and o.ipc_key(REQ).read_bytes()==key
        with pytest.raises(PermissionError):await o.activate(REQ,b,check=lambda *_:True)
        with pytest.raises(PermissionError):o.confirm_provisioned(REQ,check=lambda *_:False)
        check=lambda s,a,f,k:(s,a,f,k.read_bytes())==(sid,ACTOR,FORUM,key)
        o.confirm_provisioned(REQ,check=check);assert not r.get(sid)['desired_active']
        assert await o.activate(REQ,b,check=check)==sid;assert await o.activate(REQ,b,check=check)==sid
        assert o.plane.route_group(FORUM,ACTOR)==sid and o.row(REQ)['stage']=='ACTIVE'
        assert not (r.capsule_dir(sid)/'credentials.json').exists()
        o.revoke(REQ);assert o.plane.route_group(FORUM,ACTOR) is None and not r.get(sid)['desired_active']
        with pytest.raises(PermissionError):await o.activate(REQ,b,check=check)
    finally:r.close()

@pytest.mark.asyncio
async def test_conflicting_actor_forum_and_invalid_membership(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r);b=bot();o.reserve(REQ,ACTOR,FORUM)
    try:
        for req,actor,forum in [(REQ,202,FORUM),('b'*24,ACTOR,-100002),('b'*24,202,FORUM)]:
            with pytest.raises(PermissionError):o.reserve(req,actor,forum)
        assert len(r.list_capsules())==1
        b.get_chat_member_count.return_value=3
        with pytest.raises(PermissionError):await o.verify_forum(REQ,b)
        assert o.row(REQ)['stage']=='RESERVED'
        b.get_chat_member_count.return_value=2;b.get_chat.return_value.username='public'
        with pytest.raises(PermissionError):await o.verify_forum(REQ,b)
        b.get_chat.return_value.username=None;await o.verify_forum(REQ,b);o.confirm_provisioned(REQ,check=lambda *_:True)
        b.get_chat_member_count.return_value=3
        with pytest.raises(PermissionError):await o.activate(REQ,b,check=lambda *_:True)
        assert o.row(REQ)['stage']=='PROVISIONED' and o.plane.route_group(FORUM,ACTOR) is None
    finally:r.close()


def test_reservation_rolls_back_metadata_on_failure(tmp_path,monkeypatch):
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r)
    def fail(*args):raise OSError('synthetic disk fault')
    try:
        with monkeypatch.context() as m:
            m.setattr(o.plane,'bind_forum',fail)
            with pytest.raises(OSError):o.reserve(REQ,ACTOR,FORUM)
        assert r.list_capsules()==[] and o.plane.owner(ACTOR) is None
        sid=o.reserve(REQ,ACTOR,FORUM);assert o.row(REQ)['sid']==sid
        # Failed allocation can leave empty private directories; no active route.
        assert o.plane.route_group(FORUM,ACTOR) is None
    finally:r.close()

@pytest.mark.asyncio
async def test_pinned_bot_identity_and_revocation_during_verification(tmp_path):
    from app.shared_gateway_runtime import SharedGatewayRuntime
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r);b=bot();sid=o.reserve(REQ,ACTOR,FORUM)
    try:
        await o.verify_forum(REQ,b);o.confirm_provisioned(REQ,check=lambda *_:True)
        b.get_me.return_value=NS(id=998)
        with pytest.raises(PermissionError,match='Bot identity changed'):await o.activate(REQ,b,check=lambda *_:True)
        b.get_me.return_value=NS(id=999);await o.activate(REQ,b,check=lambda *_:True)
        channel=tmp_path/'channel';channel.mkdir(mode=0o700);g=SharedGatewayRuntime(r,b,channels={sid:channel},external_only=True)
        try:
            b.get_me.return_value=NS(id=998)
            with pytest.raises(PermissionError,match='different bot'):await g.start()
        finally:await g.close()
        b.get_me.return_value=NS(id=999)
        async def revoke(*args):o.revoke(REQ);return 2
        b.get_chat_member_count.side_effect=revoke
        with pytest.raises(PermissionError,match='Revoked'):await o.verify_forum(REQ,b)
        assert not r.get(sid)['desired_active']
    finally:r.close()


def test_local_worker_provision_resume_and_key_conflict(tmp_path):
    from app.worker_provision import prepare_worker,check_worker
    from app.worker_secrets import install_worker_secret
    folders=[tmp_path/k for k in ('state','channel','secret')]
    for p in folders:p.mkdir(mode=0o700)
    sid='c'*24;key=b'x'*32;state,channel,secret=folders
    with pytest.raises(FileNotFoundError):prepare_worker(*folders,sid,ACTOR,FORUM,key)
    assert not (state/'key').exists()
    install_worker_secret(secret,sid,{'session_id':sid,'MAX_TOKEN':'synthetic-private','MAX_DEVICE_ID':'device'})
    assert prepare_worker(*folders,sid,ACTOR,FORUM,key)
    # Simulated interruption before ACL publication; the same key is retained.
    (state/'worker-acl.json').unlink()
    assert prepare_worker(*folders,sid,ACTOR,FORUM,key)
    assert check_worker(*folders,sid,ACTOR,FORUM,key)
    with pytest.raises(PermissionError,match='conflict'):prepare_worker(*folders,sid,ACTOR,FORUM,b'y'*32)
    assert (state/'key').read_bytes()==key
    with pytest.raises(PermissionError,match='conflict'):prepare_worker(*folders,sid,202,FORUM,key)
    with pytest.raises(ValueError,match='Overlapping'):prepare_worker(state,state,secret,sid,ACTOR,FORUM,key)
