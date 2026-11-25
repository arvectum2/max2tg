"""Two tenants, real signed Unix sockets, synthetic messenger boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from tests import test_shared_gateway_ipc as shared_fixtures
shared_setup=shared_fixtures.setup
from tests.test_shared_gateway_ipc import client,worker_queue,FakeMaxClient,_telegram_update
from app.capsule_shared_listener import SharedCapsuleTextListener
from app.capsule_shared_ingress import SharedCapsuleTextIngress
from app.max_client import MaxMessage
from app.telegram_gateway import TelegramGatewayEngine
from app.telegram_inbox import TelegramInbox
from app.telegram_ipc import CapsuleIpcServer
from app.tg_outbox import TelegramOutbound
from app.topics import TopicStore


class BoundMax(FakeMaxClient):
    def on_message(self,callback):self.callback=callback
    async def telegram_echo_tg_id(self,msg):return None


@pytest.mark.asyncio
async def test_two_workers_callback_to_exact_topics_and_durable_receipt_return(shared_setup):
    reg,plane,central,server,sessions=shared_setup;await server.start();workers=[]
    inbox=TelegramInbox(reg.root/'inbox.sqlite3',plane)
    try:
        for sid,actor,forum in sessions:
            # Same MAX ID, message ID and thread number across tenants.
            plane.bind_topic(sid,42,77,actor)
            state=reg.capsule_dir(sid)/'state';t=TopicStore(str(state/'topics.json'),strict=True);t.set_topic(42,77,'synthetic')
            l=TelegramOutbound(state/'tg-outbound.sqlite3',t);q=worker_queue(reg,sid);maxc=BoundMax()
            adapter=SharedCapsuleTextListener(maxc,t,l,q,client(reg,sid));adapter.attach()
            incoming=CapsuleIpcServer(reg,sid,SharedCapsuleTextIngress(maxc,t,l,trusted_topic=lambda thread,s=sid:plane.max_chat_for_thread(s,thread)))
            await incoming.start();workers.append((incoming,l,q,maxc,adapter,state))
            msg=MaxMessage(chat_id=42,message_id='123',text=f'only tenant {actor}')
            assert await maxc.callback(msg)=='PENDING';assert await adapter.drain()=={'ACCEPTED':1};assert t.tg_for_max_message(42,'123') is None
        async def send(**kw):
            assert kw['message_thread_id']==77
            actor=next(a for _,a,f in sessions if f==kw['chat_id']);assert kw['text']==f'only tenant {actor}'
            return SimpleNamespace(message_id=actor+1000,chat=SimpleNamespace(id=kw['chat_id']),message_thread_id=77)
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=send));assert await central.drain(bot)=={'SENT':2}
        engine=TelegramGatewayEngine(plane,inbox,topic_scoped=True)
        for i,(incoming,l,q,m,a,state) in enumerate(workers):
            sid,actor,forum=sessions[i];assert await a.drain()=={'SENT':1};assert a.topics.tg_for_max_message(42,'123')==actor+1000
            # Rebuild the derived message map from durable ACK after restart.
            fresh=TopicStore(str(state/'fresh-topics.json'),strict=True);fresh.set_topic(42,77,'synthetic');a.topics=fresh;assert a.repair_receipts()==1;assert fresh.tg_for_max_message(42,'123')==actor+1000
            u=_telegram_update(100+i,actor,forum,77,'response');assert await engine.accept(u)=='ACKED';assert await engine.accept(u)=='ACKED';assert len(m.sent)==1
            assert await m.callback(MaxMessage(chat_id=42,message_id='1001',is_self=True,text='response'))=='ECHO'
            assert await engine.accept(_telegram_update(300+i,actor,forum,78,'unbound'))=='DENIED'
            assert await m.callback(MaxMessage(chat_id=43,message_id='124',text='unbound'))=='UNBOUND'
            assert await m.callback(MaxMessage(chat_id=42,message_id='125',text='caption',attaches=[{'_type':'PHOTO'}]))=='UNSUPPORTED'
        assert bot.send_message.await_count==2;assert central.counts()=={'SENT':2}
        assert await engine.accept(_telegram_update(500,sessions[1][1],sessions[0][2],77,'foreign'))=='DENIED'
    finally:
        for incoming,l,q,*_ in workers:await incoming.stop();l.close();q.close()
        inbox.close();await server.stop()


@pytest.mark.asyncio
async def test_topic_revoked_after_admission_and_wrong_receipt_quarantine(shared_setup):
    reg,plane,ledger,server,sessions=shared_setup;sid,actor,forum=sessions[0];plane.bind_topic(sid,42,77,actor);plane.bind_topic(sid,43,78,actor)
    await server.start()
    try:
        c=client(reg,sid);assert await c.enqueue('scoped:1','first',max_chat_id='42')=={'status':'QUEUED'};plane.disable_topic(sid,42)
        bot=SimpleNamespace(send_message=AsyncMock());assert await ledger.send_one(bot,sid,'scoped:1')=='BLOCKED';bot.send_message.assert_not_awaited()
        with pytest.raises((OSError,EOFError,ValueError)):await c.enqueue('scoped:2','denied',max_chat_id='42')
        assert await c.enqueue('scoped:3','second',max_chat_id='43')=={'status':'QUEUED'}
        bot.send_message.return_value=SimpleNamespace(message_id=1,chat=SimpleNamespace(id=forum),message_thread_id=77)
        assert await ledger.send_one(bot,sid,'scoped:3')=='UNKNOWN';assert await ledger.send_one(bot,sid,'scoped:3')=='UNKNOWN';bot.send_message.assert_awaited_once()
    finally:await server.stop()


def test_binding_is_owner_scoped_and_immutable(shared_setup):
    reg,plane,_,_,sessions=shared_setup;sid,actor,forum=sessions[0]
    with pytest.raises(PermissionError):plane.bind_topic(sid,42,77,sessions[1][1])
    plane.bind_topic(sid,42,77,actor);plane.bind_topic(sid,42,77,actor)
    with pytest.raises(ValueError):plane.bind_topic(sid,42,78,actor)
    with pytest.raises(ValueError):plane.bind_topic(sid,43,True,actor)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):plane.bind_topic(sid,43,77,actor)
    plane.disable_topic(sid,42)
    assert plane.topic_route(sid,42) is None
    with pytest.raises(ValueError):plane.bind_topic(sid,42,77,actor)


@pytest.mark.asyncio
async def test_scoped_status_requires_matching_source_route(shared_setup):
    reg,plane,ledger,server,sessions=shared_setup;sid,actor,forum=sessions[0];plane.bind_topic(sid,42,77,actor);plane.bind_topic(sid,43,78,actor)
    await server.start()
    try:
        c=client(reg,sid);assert await c.enqueue('scoped:key','hello',max_chat_id='42')=={'status':'QUEUED'}
        for route in (None,'43'):
            with pytest.raises((EOFError,OSError,ValueError)):await c.status('scoped:key',max_chat_id=route)
        assert await c.status('scoped:key',max_chat_id='42')=={'status':'QUEUED'}
    finally:await server.stop()


@pytest.mark.asyncio
async def test_worker_rejects_mismatched_local_topic_even_if_gateway_trusted(shared_setup):
    reg,plane,_,_,sessions=shared_setup;sid,actor,forum=sessions[0];plane.bind_topic(sid,42,77,actor)
    state=reg.capsule_dir(sid)/'state';t=TopicStore(str(state/'topics.json'));t.set_topic(43,77,'wrong')
    l=TelegramOutbound(state/'tg-outbound.sqlite3',t);m=BoundMax();s=CapsuleIpcServer(reg,sid,SharedCapsuleTextIngress(m,t,l,trusted_topic=lambda thread:plane.max_chat_for_thread(sid,thread)));await s.start();inbox=TelegramInbox(reg.root/'inbox.sqlite3',plane)
    try:
        e=TelegramGatewayEngine(plane,inbox,topic_scoped=True);assert await e.accept(_telegram_update(7,actor,forum,77,'wrong local mapping'))=='UNKNOWN';assert not m.sent;assert not l.counts()
    finally:await s.stop();l.close();inbox.close()


@pytest.mark.asyncio
async def test_strict_gateway_disallows_legacy_general_fallback(shared_setup):
    from app.telegram_egress_ipc import GatewayEgressIpcServer
    reg,plane,ledger,_,sessions=shared_setup;sid,actor,forum=sessions[0];plane.bind_topic(sid,42,77,actor)
    s=GatewayEgressIpcServer(reg,ledger,topic_scoped=True);await s.start()
    try:
        c=client(reg,sid)
        with pytest.raises((EOFError,OSError,ValueError)):await c.enqueue('general:1','must not go to General')
        assert ledger.counts()=={}
        assert await c.enqueue('scoped:1','right topic',max_chat_id='42')=={'status':'QUEUED'}
    finally:await s.stop()
