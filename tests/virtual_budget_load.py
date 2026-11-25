"""Deterministic virtual asyncio clock, actual guard/budget/egress ledger.
No sockets, external accounts, wall-clock sleep, or production configuration.
SQLite and Python execution have zero simulated duration. Not a capacity SLA.
"""
import asyncio,math,selectors,tempfile,json
from pathlib import Path
from types import SimpleNamespace as NS
from telegram.error import RetryAfter
from app.capsule_registry import CapsuleRegistry
from app.shared_onboarding import SharedOnboarding
from app.forum_guard import ForumGuard
from app.telegram_budget import TelegramBudget,BudgetedBot,ApiDeferred
from app.telegram_egress import TelegramEgress

class VirtualSelector(selectors.DefaultSelector):
    def __init__(self):super().__init__();self.now=1000.
    def select(self,timeout=None):
        events=super().select(0)
        if not events:
            if timeout is None:raise RuntimeError('Virtual scheduler deadlock')
            self.now+=max(0,timeout)
        return events


def virtual_run(coro):
    def factory():
        selector=VirtualSelector();loop=asyncio.SelectorEventLoop(selector=selector);loop.time=lambda:selector.now;return loop
    with asyncio.Runner(loop_factory=factory) as runner:return runner.run(coro)

class SyntheticBot:
    def __init__(self,clock):self.clock=clock;self.calls=[];self.sent={};self.guard=None;self.origin=None;self.fault=False;self.cooldown=None;self.by_forum={};self.by_message={};self.latency=.04
    async def record(self,group):
        self.calls.append((self.clock(),group));await asyncio.sleep(self.latency)
    async def get_me(self):await self.record('verify');return NS(id=9999)
    async def get_chat(self,gid):await self.record('verify');return NS(id=gid,type='supergroup',is_forum=True)
    async def get_chat_member(self,gid,uid):await self.record('verify');return NS(user=NS(id=uid),status='administrator' if uid==9999 else 'creator',can_manage_topics=True)
    async def get_chat_member_count(self,gid):await self.record('verify');return 2
    async def get_updates(self,**kwargs):
        if self.fault and self.origin is not None and self.clock()-self.origin>=60 and self.cooldown is None:
            self.calls.append((self.clock(),'poll'));self.cooldown=(self.clock(),self.clock()+12);raise RetryAfter(12)
        self.calls.append((self.clock(),'poll'));await asyncio.sleep(20);return []
    async def send_message(self,*,chat_id,text,message_thread_id,**kwargs):
        assert self.by_forum[chat_id]==self.by_message[text] and self.guard.allowed(self.by_forum[chat_id]) and message_thread_id==77
        assert text not in self.sent
        self.sent[text]=self.clock();await self.record('send')
        return NS(message_id=len(self.sent),chat=NS(id=chat_id),message_thread_id=77)

async def scenario(root,tenants,rate,*,fault=False,latency=.04,scheduler="wave",concurrency=4):
    if scheduler not in ("wave","rolling") or concurrency not in (4,8):raise ValueError("Unreviewed scheduler")
    if tenants not in (32,48,64) or rate not in (3,8) or latency not in (.04,.15,.25,.35):raise ValueError("Unreviewed scenario")
    clock=asyncio.get_running_loop().time;r=CapsuleRegistry(root/'registry');o=SharedOnboarding(r);raw=SyntheticBot(clock);raw.latency=latency;sids=[];tasks=[];e=None
    try:
        for i in range(tenants):
            req=f'{i+1:024x}';actor=100+i;forum=-10000-i;sid=o.reserve(req,actor,forum)
            await o.verify_forum(req,raw);o.confirm_provisioned(req,check=lambda *_:True);await o.activate(req,raw,check=lambda *_:True);o.plane.bind_topic(sid,42,77,actor);sids.append(sid);raw.by_forum[forum]=sid
        raw.calls.clear();budget=TelegramBudget(r.db,clock=clock,wall=clock);bot=BudgetedBot(raw,budget);guard=ForumGuard(r,bot,clock=clock,concurrency=concurrency);raw.guard=guard;raw.fault=fault;active=set(sids);e=TelegramEgress(r.root/'telegram-egress.sqlite3',o.plane,topic_scoped=True,dispatch_allowed=guard.allowed)
        lock=asyncio.Lock()
        async def checks():
            if scheduler=='wave':
                while True:
                    await guard.refresh(active)
                    async with lock:pass # same reconcile/drain exclusion, no socket cost
                    await asyncio.sleep(.2)
            else:
                pending={}
                try:
                    while True:
                        for sid,task in list(pending.items()):
                            if task.done():await task;del pending[sid]
                        now=clock()
                        if not budget.remaining:
                            rows=[dict(row) for row in guard.onboarding.db.execute("SELECT * FROM shared_onboarding WHERE stage='ACTIVE'") if row['sid'] in active and row['sid'] not in pending and now>=guard.next_check.get(row['sid'],-1)]
                            rows.sort(key=lambda row:(guard.valid.get(row['sid'],-1),guard.next_check.get(row['sid'],-1),row['sid']))
                            for row in rows[:concurrency-len(pending)]:pending[row['sid']]=asyncio.create_task(guard._check(row))
                        if pending:await asyncio.wait(pending.values(),timeout=.05,return_when=asyncio.FIRST_COMPLETED)
                        else:await asyncio.sleep(.05)
                finally:
                    for task in pending.values():task.cancel()
                    if pending:await asyncio.gather(*pending.values(),return_exceptions=True)
        async def polls():
            while True:
                try:await bot.get_updates(timeout=20)
                except (ApiDeferred,RetryAfter):pass
                await asyncio.sleep(max(.05,min(1,budget.remaining)))
        async def drains():
            while True:
                async with lock:result=await e.drain(bot,16,exclude=guard.held_sessions(),fair=True)
                await asyncio.sleep(.05 if result.get('SENT',0) else 1)
        tasks=[asyncio.create_task(f()) for f in (checks,polls,drains)];start=clock()
        while not all(guard.allowed(s) for s in active):
            if clock()-start>60:
                assert not raw.sent and all(y[0]-x[0]>=.05-1e-7 for x,y in zip(raw.calls,raw.calls[1:]))
                return {'tenants':tenants,'scheduler':scheduler,'concurrency':concurrency,'offered_per_s':rate,'api_latency_s':latency,'fault':fault,'warmup':'FAIL','ready_at_deadline':sum(guard.allowed(s) for s in active),'warmup_limit_s':60,'sent':0}
            await asyncio.sleep(.1)
        warmup=clock()-start;raw.origin=clock();arrived={};samples=[];peak=0;total=rate*120
        for tick in range(1200):
            # Deterministic evenly spaced arrivals, round robin owners.
            target=(tick+1)*total//1200
            for j in range(len(arrived),target):
                key=f'msg-{j:05d}';sid=sids[j%tenants];assert e.enqueue(sid,key,key,max_chat_id=42)=='QUEUED';arrived[key]=clock();raw.by_message[key]=sid
            ready=sum(guard.allowed(s) for s in active);counts=e.counts();pending=counts.get('QUEUED',0)+counts.get('SENDING',0);peak=max(peak,pending);samples.append((ready,pending))
            await asyncio.sleep(.1)
        end_pending=e.counts().get('QUEUED',0)+e.counts().get('SENDING',0);end=clock();deadline=end+90
        while e.counts().get('SENT',0)!=total and clock()<deadline:await asyncio.sleep(.1)
        counts=e.counts();confirmed=counts.get('SENT',0)
        assert counts.get('UNKNOWN',0)==0 and counts.get('BLOCKED',0)==0 and sum(counts.values())==total
        assert 0<=len(raw.sent)-confirmed<=counts.get('SENDING',0)<=1
        assert all(y[0]-x[0]>=.05-1e-7 for x,y in zip(raw.calls,raw.calls[1:]))
        if fault:
            assert raw.cooldown is not None;lo,hi=raw.cooldown;assert not any(lo<t<hi-1e-7 for t,_ in raw.calls)
            assert r.db.execute('SELECT until FROM telegram_api_budget').fetchone()[0]==hi
        delivered=[raw.sent[k]-arrived[k] for k in raw.sent];assert all(v>=0 for v in delivered)
        interval=[g for t,g in raw.calls if raw.origin<=t<end];return {'tenants':tenants,'scheduler':scheduler,'concurrency':concurrency,'offered_per_s':rate,'api_latency_s':latency,'fault':fault,'warmup':'PASS','warmup_s':round(warmup,3),'offered':total,'sent':confirmed,'invoked':len(raw.sent),'drained':confirmed==total,'pending_at_120s':end_pending,'peak_pending':peak,'drain_after_s':round(clock()-end,3),'min_ready':min(a for a,_ in samples),'all_ready_fraction':round(sum(a==tenants for a,_ in samples)/len(samples),4),'held_tenant_fraction':round(sum(tenants-a for a,_ in samples)/(tenants*len(samples)),5),'p95_to_api_start_s':round(sorted(delivered)[math.ceil(len(delivered)*.95)-1],3) if confirmed==total and delivered else None,'starts':{k:interval.count(k) for k in ('verify','poll','send')},'cooldown_s':12 if fault else 0,'terminal':counts}
    finally:
        for task in tasks:task.cancel()
        outcomes=await asyncio.gather(*tasks,return_exceptions=True) if tasks else []
        if e:e.close()
        r.close()
        for x in outcomes:
            if isinstance(x,Exception):raise x


def run(tenants,rate,fault=False,latency=.04,*,scheduler="wave",concurrency=4):
    with tempfile.TemporaryDirectory(prefix='m2budget64-') as td:return virtual_run(scenario(Path(td),tenants,rate,fault=fault,latency=latency,scheduler=scheduler,concurrency=concurrency))

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--compare',action='store_true');args=parser.parse_args()
    if args.compare:
        for n,rate,fault,latency in ((32,3,False,.35),(32,8,False,.15),(48,3,False,.35),(48,3,False,.25),(48,8,False,.04),(64,3,False,.25),(64,3,False,.35),(64,8,False,.15),(64,3,True,.04),(64,8,False,.04)):
            for scheduler,c in [('wave',4),('rolling',4)]+([('rolling',8)] if n==64 else []):
                print(json.dumps(run(n,rate,fault,latency,scheduler=scheduler,concurrency=c),sort_keys=True),flush=True)
        raise SystemExit(0)
    for n,rate,fault,latency in ((32,8,False,.04),(64,3,False,.04),(64,8,False,.04),(64,3,True,.04),(64,8,False,.15),(64,3,False,.25),(64,3,False,.35)):
        print(json.dumps(run(n,rate,fault,latency),sort_keys=True),flush=True)
