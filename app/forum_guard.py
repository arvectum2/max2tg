"""Short-lived positive membership evidence for the offline managed gateway.

No persisted positive lease survives restart. Explicit violations revoke;
transport uncertainty holds, never turns an uncertain send into a retry.
"""
import asyncio,time,math
from datetime import timedelta
from telegram.error import RetryAfter
from app.telegram_budget import BudgetedBot,ApiDeferred
from app.shared_onboarding import SharedOnboarding,inspect_forum,ForumPolicyViolation

class VerificationCooldown(Exception):
    pass

class PacedVerificationBot:
    """Pace verification reads only; no retries inside a request."""
    def __init__(self,bot,*,rate=20,clock=time.monotonic,sleep=asyncio.sleep):
        if not 0<rate<=100:raise ValueError('Invalid verification rate')
        self.bot=bot;self.interval=1/rate;self.clock=clock;self.sleep=sleep
        self.lock=asyncio.Lock();self.next_slot=0;self.cooldown=0

    async def call(self,name,*args):
        async with self.lock:
            now=self.clock()
            if now<self.cooldown:raise VerificationCooldown()
            await self.sleep(max(0,self.next_slot-now))
            now=self.clock()
            if now<self.cooldown:raise VerificationCooldown()
            self.next_slot=now+self.interval
        try:return await getattr(self.bot,name)(*args)
        except RetryAfter as exc:
            delay=exc.retry_after
            if isinstance(delay,timedelta):delay=delay.total_seconds()
            if type(delay) not in (int,float) or not math.isfinite(delay) or delay<0:delay=60
            self.cooldown=max(self.cooldown,self.clock()+max(1,delay))
            raise VerificationCooldown() from None

    async def get_me(self):return await self.call('get_me')
    async def get_chat(self,*args):return await self.call('get_chat',*args)
    async def get_chat_member(self,*args):return await self.call('get_chat_member',*args)
    async def get_chat_member_count(self,*args):return await self.call('get_chat_member_count',*args)

class ForumGuard:
    def __init__(self,registry,bot,*,ttl=30,clock=time.monotonic,concurrency=4,rate=20):
        if not 1<=ttl<=60:raise ValueError('Invalid forum check interval')
        if type(concurrency) is not int or not 1<=concurrency<=16:raise ValueError("Invalid verification concurrency")
        self.onboarding=SharedOnboarding(registry);self.bot=bot if isinstance(bot,BudgetedBot) else PacedVerificationBot(bot,rate=rate);self.ttl=ttl;self.clock=clock;self.concurrency=concurrency
        self.failures={}
        self.valid={};self.next_check={};self.generation={};self.status={};self.lock=asyncio.Lock()

    def allowed(self,sid):return self.clock()<self.valid.get(sid,-1)

    def invalidate(self,forum):
        if type(forum) is not int:return
        for r in self.onboarding.db.execute('SELECT sid FROM shared_onboarding WHERE forum=?',(str(forum),)):
            sid=r[0];self.valid.pop(sid,None);self.next_check.pop(sid,None);self.generation[sid]=self.generation.get(sid,0)+1;self.status[sid]='RECHECK'

    def observe(self,update):
        # Only trusted Telegram polling metadata, never message text.
        for kind in ('chat_member','my_chat_member','message'):
            event=update.get(kind)
            if not isinstance(event,dict) or not isinstance(event.get('chat'),dict):continue
            forum=event['chat'].get('id')
            if type(forum) is not int:continue
            if kind=='message' and not any(k in event for k in ('new_chat_members','left_chat_member','migrate_to_chat_id','migrate_from_chat_id')):continue
            self.invalidate(forum)
            row=self.onboarding.db.execute("SELECT * FROM shared_onboarding WHERE forum=? AND stage='ACTIVE'",(str(forum),)).fetchone()
            if not row:continue
            actor=int(row['actor']);bot=int(row['bot_id']);violation=False
            if kind=='message':
                joins=event.get('new_chat_members') or []
                violation=any(isinstance(m,dict) and type(m.get('id')) is int and m['id'] not in (actor,bot) for m in joins)
                left=event.get('left_chat_member') or {}
                violation=violation or (isinstance(left,dict) and left.get('id') in (actor,bot)) or any(k in event for k in ('migrate_to_chat_id','migrate_from_chat_id'))
            else:
                member=event.get('new_chat_member') or {};user=member.get('user') or {}
                uid=user.get('id');status=member.get('status')
                if type(uid) is int and status in ('creator','administrator','member','restricted','left','kicked'):
                    if uid==actor:violation=status!='creator'
                    elif uid==bot:violation=status!='administrator' or member.get('can_manage_topics') is not True
                    else:violation=status in ('creator','administrator','member') or status=='restricted' and member.get('is_member') is True
            if violation:
                self.onboarding.revoke(row['request_id']);self.status[row['sid']]='REVOKED'

    def held_sessions(self):
        return {r[0] for r in self.onboarding.db.execute("SELECT o.sid FROM shared_onboarding o JOIN capsules c ON c.session_id=o.sid JOIN tg_routes r ON r.session_id=o.sid WHERE o.stage='ACTIVE' AND c.desired_active=1 AND r.status='ACTIVE'") if not self.allowed(r[0])}

    async def _check(self,row):
        sid=row['sid'];now=self.clock();gen=self.generation.get(sid,0)
        def current():return self.generation.get(sid,0)==gen and self.onboarding.row(row['request_id'])['stage']=='ACTIVE'
        try:
            bid=await asyncio.wait_for(inspect_forum(row,self.bot),timeout=8)
            if str(bid)!=row['bot_id']:raise ForumPolicyViolation('Bot identity changed')
        except ForumPolicyViolation:
            if current():
                self.valid.pop(sid,None);self.onboarding.revoke(row['request_id']);self.status[sid]='REVOKED';self.next_check.pop(sid,None)
        except (VerificationCooldown,ApiDeferred,RetryAfter):
            if current():
                self.valid.pop(sid,None);self.status[sid]='RATE_LIMITED';self.next_check[sid]=self.clock()+max(1,self._remaining())
        except Exception:
            if current():
                self.valid.pop(sid,None);self.status[sid]='UNAVAILABLE';n=self.failures.get(sid,0)+1;self.failures[sid]=n;self.next_check[sid]=self.clock()+min(60,5*2**min(n-1,4))
        else:
            if current():
                self.failures.pop(sid,None);self.valid[sid]=now+self.ttl
                self.next_check[sid]=now+self.ttl*0.7;self.status[sid]='VERIFIED'

    def _remaining(self):
        if isinstance(self.bot,BudgetedBot):return self.bot.budget.remaining
        return max(0,self.bot.cooldown-self.bot.clock())

    async def refresh(self,active):
        """One fair, bounded wave. Unchecked sessions never get a lease."""
        async with self.lock:
            for sid in set(self.valid)-set(active):self.valid.pop(sid,None)
            now=self.clock()
            if self._remaining():return
            rows=[dict(r) for r in self.onboarding.db.execute("SELECT * FROM shared_onboarding WHERE stage='ACTIVE'") if r['sid'] in active and now>=self.next_check.get(r['sid'],-1)]
            rows.sort(key=lambda r:(self.next_check.get(r['sid'],-1),r['sid']))
            tasks=[asyncio.create_task(self._check(row)) for row in rows[:self.concurrency]]
            try:
                if tasks:await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():task.cancel()
                if tasks:await asyncio.gather(*tasks,return_exceptions=True)
