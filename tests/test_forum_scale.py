"""Deterministic load/clock simulation; no external Telegram traffic."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS
import pytest
from telegram.error import RetryAfter
from app.capsule_registry import CapsuleRegistry
from app.forum_guard import ForumGuard,VerificationCooldown
from app.shared_onboarding import SharedOnboarding
from tests.test_shared_onboarding import bot,REQ,ACTOR,FORUM

class Clock:
    def __init__(self):self.now=100.
    def __call__(self):return self.now
    async def sleep(self,seconds):self.now+=seconds;await asyncio.sleep(0)

class LoadBot:
    def __init__(self,clock):self.clock=clock;self.calls=[];self.running=0;self.peak=0
    async def record(self):
        self.calls.append(self.clock());self.running+=1;self.peak=max(self.peak,self.running)
        try:await asyncio.sleep(0);await asyncio.sleep(0)
        finally:self.running-=1
    async def get_me(self):await self.record();return NS(id=9999)
    async def get_chat(self,gid):await self.record();return NS(id=gid,type='supergroup',is_forum=True)
    async def get_chat_member(self,gid,uid):await self.record();return NS(user=NS(id=uid),status='administrator' if uid==9999 else 'creator',can_manage_topics=True)
    async def get_chat_member_count(self,gid):await self.record();return 2

@pytest.mark.asyncio
async def test_100_forums_paced_fair_and_bounded(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r);c=Clock();b=LoadBot(c);g=ForumGuard(r,b,clock=c,concurrency=4,rate=20);g.bot.clock=c;g.bot.sleep=c.sleep;active=set()
    try:
        # Provision through trusted onboarding; reset only synthetic API counters.
        for i in range(100):
            req=f'{i+1:024x}';sid=o.reserve(req,100+i,-10000-i);await o.verify_forum(req,b);o.confirm_provisioned(req,check=lambda *_:True);await o.activate(req,b,check=lambda *_:True);active.add(sid)
        b.calls.clear();b.peak=0;start=c();seen=set()
        for _ in range(25):
            await g.refresh(active);seen.update(s for s in active if g.status.get(s)=='VERIFIED')
        assert seen==active and len(b.calls)==500 and 1<=b.peak<=4
        assert all(y-x>=.05-1e-8 for x,y in zip(b.calls,b.calls[1:]))
        assert 24.9<=c()-start<26
        assert all(g.allowed(s) for s in active)
        # Saturation cannot grant extra lease time or bypass verification.
        c.now+=31;assert not any(g.allowed(s) for s in active)
        await g.refresh(active);assert sum(g.allowed(s) for s in active)==4
        print(f'100 forums / 500 reads / virtual {b.calls[499]-b.calls[0]:.2f}s / peak {b.peak}')
    finally:r.close()

@pytest.mark.asyncio
async def test_retry_after_global_cooldown_and_backoff(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r);b=bot();sid=o.reserve(REQ,ACTOR,FORUM);await o.verify_forum(REQ,b);o.confirm_provisioned(REQ,check=lambda *_:True);await o.activate(REQ,b,check=lambda *_:True);c=Clock();g=ForumGuard(r,b,clock=c);g.bot.clock=c;g.bot.sleep=c.sleep
    try:
        b.get_chat.side_effect=RetryAfter(timedelta(seconds=17));await g.refresh({sid});assert g.status[sid]=='RATE_LIMITED' and not g.allowed(sid);until=g.bot.cooldown;n=b.get_chat.await_count
        g.invalidate(FORUM);await g.refresh({sid});assert b.get_chat.await_count==n
        c.now=until-.1
        with pytest.raises(VerificationCooldown):await g.bot.get_me()
        c.now=until+.1;b.get_chat.side_effect=TimeoutError();await g.refresh({sid});assert g.status[sid]=='UNAVAILABLE';assert g.next_check[sid]-c()==pytest.approx(5)
        c.now+=5;await g.refresh({sid});assert g.next_check[sid]-c()==pytest.approx(10)
        c.now+=10;b.get_chat.side_effect=None;await g.refresh({sid});assert g.allowed(sid) and sid not in g.failures
    finally:r.close()

@pytest.mark.asyncio
async def test_invalidation_during_inflight_check_cannot_restore_lease(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r);b=bot();sid=o.reserve(REQ,ACTOR,FORUM);await o.verify_forum(REQ,b);o.confirm_provisioned(REQ,check=lambda *_:True);await o.activate(REQ,b,check=lambda *_:True);c=Clock();g=ForumGuard(r,b,clock=c);g.bot.clock=c;g.bot.sleep=c.sleep
    entered=asyncio.Event();resume=asyncio.Event()
    async def count(*args):entered.set();await resume.wait();return 2
    b.get_chat_member_count.side_effect=count
    try:
        t=asyncio.create_task(g.refresh({sid}));await asyncio.wait_for(entered.wait(),2);g.invalidate(FORUM);resume.set();await t
        assert not g.allowed(sid) and g.status[sid]=='RECHECK';b.get_chat_member_count.side_effect=None;await g.refresh({sid});assert g.allowed(sid)
    finally:r.close()

@pytest.mark.asyncio
async def test_rolling_pool_removal_and_cancel_release_owned_checks(tmp_path):
    r=CapsuleRegistry(tmp_path/'registry');o=SharedOnboarding(r);b=bot();sid=o.reserve(REQ,ACTOR,FORUM);await o.verify_forum(REQ,b);o.confirm_provisioned(REQ,check=lambda *_:True);await o.activate(REQ,b,check=lambda *_:True)
    c=Clock();g=ForumGuard(r,b,clock=c);g.bot.clock=c;g.bot.sleep=c.sleep
    entered=asyncio.Event();cancelled=asyncio.Event();active={sid};stop=asyncio.Event()
    async def count(*args):
        entered.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    b.get_chat_member_count.side_effect=count
    task=asyncio.create_task(g.run_rolling(lambda:active,stop))
    try:
        await asyncio.wait_for(entered.wait(),2);active.clear();await asyncio.wait_for(cancelled.wait(),2)
        assert not g.allowed(sid) and g.generation[sid]>0
        entered.clear();cancelled.clear();active.add(sid);await asyncio.wait_for(entered.wait(),2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert cancelled.is_set() and not g.allowed(sid) and not g.lock.locked()
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True);r.close()
