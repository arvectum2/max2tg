"""Single managed gateway Bot API budget; no implicit retry of mutations.

Persistent wall-clock cooldown in the central registry; local monotonic pacing.
A trusted gateway owns the runtime lock. Already in-flight calls cannot be
recalled. Only ApiDeferred proves this wrapper did not invoke the API.
"""
import asyncio,math,time,sqlite3
from datetime import timedelta
from telegram.error import RetryAfter

class ApiDeferred(Exception):
    pass

class TelegramBudget:
    def __init__(self,db,*,rate=20,clock=time.monotonic,wall=time.time,sleep=asyncio.sleep):
        if type(rate) not in (int,float) or not 0<rate<=100:raise ValueError('Invalid API rate')
        self.db=db;self.clock=clock;self.wall=wall;self.sleep=sleep;self.interval=1/rate;self.next_slot=0;self.lock=asyncio.Lock()
        db.execute('CREATE TABLE IF NOT EXISTS telegram_api_budget(singleton INTEGER PRIMARY KEY CHECK(singleton=1),until REAL NOT NULL)')
        db.execute('INSERT OR IGNORE INTO telegram_api_budget VALUES(1,0)')
        self._monotonic_until=0;self.faulted=False
    @property
    def remaining(self):
        if self.faulted:raise RuntimeError('API budget persistence failed')
        until=self.db.execute('SELECT until FROM telegram_api_budget WHERE singleton=1').fetchone()[0]
        if type(until) not in (int,float) or not math.isfinite(until) or until<0:raise ValueError('Invalid durable cooldown')
        return max(0,until-self.wall(),self._monotonic_until-self.clock())
    def defer(self,delay):
        if isinstance(delay,timedelta):delay=delay.total_seconds()
        if type(delay) not in (float,int) or not math.isfinite(delay) or delay<0:delay=60
        delay=max(1,delay);until=self.wall()+delay
        if not math.isfinite(until):raise ValueError('Invalid cooldown deadline')
        self._monotonic_until=max(self._monotonic_until,self.clock()+delay)
        try:self.db.execute('UPDATE telegram_api_budget SET until=MAX(until,?) WHERE singleton=1',(until,))
        except sqlite3.Error:self.faulted=True;raise
    async def call(self,method,*args,before=None,**kwargs):
        async with self.lock:
            if self.remaining:raise ApiDeferred('Telegram API cooldown')
            await self.sleep(max(0,self.next_slot-self.clock()))
            if self.remaining:raise ApiDeferred('Telegram API cooldown')
            if before is not None and not before():raise ApiDeferred('Delivery authorization changed')
            self.next_slot=self.clock()+self.interval
        try:return await method(*args,**kwargs)
        except RetryAfter as exc:
            self.defer(exc.retry_after);raise

class BudgetedBot:
    """Explicit allowlist of gateway calls, sharing one budget."""
    def __init__(self,bot,budget):self._bot=bot;self.budget=budget
    async def get_me(self):return await self.budget.call(self._bot.get_me)
    async def get_chat(self,*args):return await self.budget.call(self._bot.get_chat,*args)
    async def get_chat_member(self,*args):return await self.budget.call(self._bot.get_chat_member,*args)
    async def get_chat_member_count(self,*args):return await self.budget.call(self._bot.get_chat_member_count,*args)
    async def get_updates(self,**kw):return await self.budget.call(self._bot.get_updates,**kw)
    async def send_message(self,*,before=None,**kw):return await self.budget.call(self._bot.send_message,before=before,**kw)
    async def create_forum_topic(self,*,before=None,**kw):return await self.budget.call(self._bot.create_forum_topic,before=before,**kw)
