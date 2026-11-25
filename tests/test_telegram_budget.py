"""Mixed operations, durable cooldown, and before-call vs uncertain effects."""
import asyncio,sqlite3,tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from telegram.error import RetryAfter
from app.telegram_budget import TelegramBudget,BudgetedBot,ApiDeferred
from app.capsule_registry import CapsuleRegistry
from app.shared_onboarding import SharedOnboarding
from app.shared_gateway_runtime import SharedGatewayRuntime
from tests.test_shared_onboarding import bot,REQ,ACTOR,FORUM
from tests.test_forum_scale import Clock

@pytest.mark.asyncio
async def test_mixed_calls_share_budget_and_cooldown_survives_reopen(tmp_path):
    c=Clock();path=tmp_path/'registry.sqlite3';db=sqlite3.connect(path,isolation_level=None);calls=[]
    async def record(*args,**kw):calls.append(c());return NS(id=999)
    names=('get_me','get_chat','get_chat_member','get_chat_member_count','get_updates','send_message','create_forum_topic')
    raw=NS(**{n:AsyncMock(side_effect=record) for n in names});budget=TelegramBudget(db,clock=c,wall=c,sleep=c.sleep);b=BudgetedBot(raw,budget)
    try:
        await asyncio.gather(b.get_me(),b.get_chat(1),b.get_chat_member(1,2),b.get_chat_member_count(1),b.get_updates(),b.send_message(text='x'),b.create_forum_topic(name='x'))
        assert len(calls)==7 and all(y-x>=.05-1e-8 for x,y in zip(calls,calls[1:]))
        raw.get_updates.side_effect=RetryAfter(timedelta(seconds=17))
        with pytest.raises(RetryAfter):await b.get_updates()
        until=c()+17;assert budget.remaining==pytest.approx(17)
        db.close();db=sqlite3.connect(path,isolation_level=None);budget=TelegramBudget(db,clock=c,wall=c,sleep=c.sleep);b=BudgetedBot(raw,budget)
        for op in (b.get_me,b.get_updates,b.send_message,b.create_forum_topic):
            with pytest.raises(ApiDeferred):await op()
        assert len(calls)==7
        c.now=until-.1
        with pytest.raises(ApiDeferred):await b.get_me()
        c.now=until+.1;await b.get_me();assert len(calls)==8
    finally:db.close()

@pytest.mark.asyncio
async def test_managed_queue_hold_recheck_and_actual_retryafter_is_unknown(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r);raw=bot();sid=o.reserve(REQ,ACTOR,FORUM);await o.verify_forum(REQ,raw);o.confirm_provisioned(REQ,check=lambda *_:True);await o.activate(REQ,raw,check=lambda *_:True);o.plane.bind_topic(sid,42,77,ACTOR)
    raw.get_updates=AsyncMock(return_value=[]);raw.send_message=AsyncMock(return_value=NS(message_id=55,chat=NS(id=FORUM),message_thread_id=77));raw.create_forum_topic=AsyncMock(return_value=NS(message_thread_id=78));c=Clock();budget=TelegramBudget(r.db,clock=c,wall=c,sleep=c.sleep)
    with tempfile.TemporaryDirectory(prefix='m2budget-',dir='/tmp') as folder:
        channel=Path(folder);g=SharedGatewayRuntime(r,BudgetedBot(raw,budget),channels={sid:channel},external_only=True,managed=True);g.guard.clock=c
        try:
            await g.start();assert g.guard.allowed(sid)
            g.egress.enqueue(sid,'one','one',max_chat_id=42);g.egress.enqueue(sid,'two','two',max_chat_id=42)
            raw.get_updates.side_effect=RetryAfter(5);assert await g.poll_once()==0;assert g._offset()==0
            assert await g.egress.send_one(g.bot,sid,'one')=='QUEUED'
            assert await g.provision_topic(sid,43,ACTOR,'test')=='DEFERRED'
            raw.send_message.assert_not_awaited();raw.create_forum_topic.assert_not_awaited()
            assert not g.plane.db.execute('SELECT 1 FROM topic_creation').fetchone()
            c.now+=6;raw.get_updates.side_effect=None
            # Invalidate the lease during slot wait, just before a send.
            async def invalidate(seconds):await c.sleep(seconds);g.guard.invalidate(FORUM)
            budget.sleep=invalidate;assert await g.egress.send_one(g.bot,sid,'one')=='QUEUED';raw.send_message.assert_not_awaited()
            budget.sleep=c.sleep;await g.reconcile_channels();assert g.guard.allowed(sid)
            raw.send_message.side_effect=RetryAfter(5)
            assert await g.egress.send_one(g.bot,sid,'one')=='UNKNOWN'
            assert await g.egress.send_one(g.bot,sid,'two')=='QUEUED';assert raw.send_message.await_count==1
            assert await g.poll_once()==0;assert g._offset()==0
        finally:await g.close()
        budget=TelegramBudget(r.db,clock=c,wall=c,sleep=c.sleep);assert budget.remaining>0
        g=SharedGatewayRuntime(r,BudgetedBot(raw,budget),channels={sid:channel},external_only=True,managed=True);g.guard.clock=c
        try:
            with pytest.raises(ApiDeferred):await g.start()
            c.now+=6;raw.send_message.side_effect=None;await g.start();assert g.guard.allowed(sid)
            assert (await g.drain_once())['egress']=={'SENT':1};assert g.egress.row(sid,'one')['status']=='UNKNOWN';assert g.egress.row(sid,'two')['status']=='SENT';assert raw.send_message.await_count==2
            assert await g.provision_topic(sid,43,ACTOR,'test')=='BOUND';raw.create_forum_topic.assert_awaited_once()
        finally:await g.close();r.close()
