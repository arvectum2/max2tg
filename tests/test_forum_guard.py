"""Synthetic forum policy changes, expired verification and transport failures."""
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from app.capsule_registry import CapsuleRegistry
from app.shared_onboarding import SharedOnboarding
from app.shared_gateway_runtime import SharedGatewayRuntime
from tests.test_shared_onboarding import bot,REQ,ACTOR,FORUM
from tests.test_shared_gateway_ipc import _telegram_update

@pytest.mark.asyncio
async def test_membership_guard_holds_queues_then_revokes(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r);b=bot();sid=o.reserve(REQ,ACTOR,FORUM)
    await o.verify_forum(REQ,b);o.confirm_provisioned(REQ,check=lambda *_:True);await o.activate(REQ,b,check=lambda *_:True);o.plane.bind_topic(sid,42,77,ACTOR)
    short=tempfile.TemporaryDirectory(prefix='m2fg-',dir='/tmp');g=SharedGatewayRuntime(r,b,channels={sid:Path(short.name)},external_only=True,managed=True)
    clock=[100];g.guard.clock=lambda:clock[0]
    try:
        await g.start();assert g.guard.allowed(sid)
        g.inbox.ingest(_telegram_update(1,ACTOR,FORUM,77,'hello'));g.egress.enqueue(sid,'one','hello',max_chat_id=42)
        clock[0]+=31;assert not g.guard.allowed(sid)
        b.get_chat.side_effect=TimeoutError('synthetic API timeout')
        snap=await g.reconcile_channels();assert snap['sessions'][sid]=={'active':True,'ready':False}
        assert g.guard.status[sid]=='UNAVAILABLE'
        assert await g.drain_once()=={'ingress':{},'egress':{}}
        assert await g.inbox.dispatch(1,g.engine.deliver)=='RECEIVED'
        assert await g.egress.send_one(b,sid,'one')=='QUEUED'
        assert g.inbox.row(1)['status']=='RECEIVED' and g.egress.row(sid,'one')['status']=='QUEUED'
        clock[0]+=6;b.get_chat.side_effect=None
        assert (await g.reconcile_channels())['sessions'][sid]=={'active':True,'ready':True}
        g.guard.invalidate(FORUM);assert not g.guard.allowed(sid)
        b.get_chat_member_count.return_value=3
        assert (await g.reconcile_channels())['sessions'][sid]=={'active':False,'ready':False}
        assert o.row(REQ)['stage']=='REVOKED';assert not r.get(sid)['desired_active']
        assert await g.drain_once()=={'ingress':{'BLOCKED':1},'egress':{'BLOCKED':1}}
        b.get_chat_member_count.return_value=2;await g.reconcile_channels();assert o.row(REQ)['stage']=='REVOKED'
    finally:await g.close();r.close();short.cleanup()

@pytest.mark.asyncio
async def test_member_event_invalidates_and_restart_rechecks(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r);b=bot();sid=o.reserve(REQ,ACTOR,FORUM)
    await o.verify_forum(REQ,b);o.confirm_provisioned(REQ,check=lambda *_:True);await o.activate(REQ,b,check=lambda *_:True);o.plane.bind_topic(sid,42,77,ACTOR)
    short=tempfile.TemporaryDirectory(prefix='m2fg-',dir='/tmp');channel=Path(short.name);g=SharedGatewayRuntime(r,b,channels={sid:channel},external_only=True,managed=True)
    from unittest.mock import AsyncMock
    b.get_updates=AsyncMock(return_value=[{'update_id':1,'chat_member':{'chat':{'id':FORUM}}}]);b.send_message=AsyncMock(return_value=NS(message_id=1,chat=NS(id=FORUM),message_thread_id=77))
    try:
        await g.start();g.egress.enqueue(sid,'one','hello',max_chat_id=42)
        await g.poll_once();assert not g.guard.allowed(sid);assert g._offset()==2
        assert (await g.drain_once())['egress']=={};b.send_message.assert_not_awaited()
        await g.reconcile_channels();assert (await g.drain_once())['egress']=={'SENT':1};b.send_message.assert_awaited_once()
    finally:await g.close()
    g=SharedGatewayRuntime(r,b,channels={sid:channel},external_only=True,managed=True)
    try:
        assert not g.guard.allowed(sid);b.get_chat_member.return_value=None;b.get_chat_member.side_effect=TimeoutError('synthetic outage')
        await g.start();assert not g.guard.allowed(sid);assert g.egress.row(sid,'one')['status']=='SENT'
    finally:await g.close();r.close();short.cleanup()

@pytest.mark.asyncio
async def test_explicit_join_revokes_even_if_snapshot_later_private(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r);b=bot();sid=o.reserve(REQ,ACTOR,FORUM)
    await o.verify_forum(REQ,b);o.confirm_provisioned(REQ,check=lambda *_:True);await o.activate(REQ,b,check=lambda *_:True)
    from app.forum_guard import ForumGuard
    g=ForumGuard(r,b)
    try:
        await g.refresh({sid});assert g.allowed(sid)
        g.observe({'chat_member':{'chat':{'id':FORUM},'new_chat_member':{'user':{'id':333},'status':'member'}}})
        assert not g.allowed(sid) and o.row(REQ)['stage']=='REVOKED';assert o.plane.route_group(FORUM,ACTOR) is None
        await g.refresh({sid});assert not g.allowed(sid)
    finally:r.close()
