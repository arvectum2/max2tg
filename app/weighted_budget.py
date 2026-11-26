"""Experimental admission arbiter; actual TelegramBudget enforces global pace.
Opt-in experimental policy, never enabled by default. Work-conserving weighted slots for waiting
verification/send groups, priority to the single polling caller. No separate
rate limits or cooldown bypass. One turn held only until invocation begins.
"""
import asyncio
from collections import deque
from app.telegram_budget import TelegramBudget
from app.stage_profile import API_GROUPS

class WeightedBudget(TelegramBudget):
    def __init__(self,*args,verify_weight,**kwargs):
        if verify_weight not in (2,3,4):raise ValueError('Invalid weight')
        super().__init__(*args,**kwargs)
        self.pattern=['verify']*verify_weight+['send']*(5-verify_weight);self.position=0
        self.waiters={k:deque() for k in ('verify','send','poll')};self.busy=False
    def _grant(self):
        if self.busy:return
        for q in self.waiters.values():
            while q and q[0].done():q.popleft()
        group='poll' if self.waiters['poll'] else None
        if group is None:
            for _ in self.pattern:
                group=self.pattern[self.position];self.position=(self.position+1)%len(self.pattern)
                if self.waiters[group]:break
            else:return
        self.busy=True;self.waiters[group].popleft().set_result(None)
    async def call(self,method,*args,before=None,metric='other',**kwargs):
        if metric not in API_GROUPS:raise ValueError('Unknown API group')
        group='verify' if metric in ('verify','identity') else 'send' if metric=='send' else 'poll'
        ticket=asyncio.get_running_loop().create_future();self.waiters[group].append(ticket);self._grant();released=False
        def release():
            nonlocal released
            if not released:released=True;self.busy=False;self._grant()
        async def invoke(*a,**kw):
            release();return await method(*a,**kw)
        try:
            await ticket
            return await super().call(invoke,*args,before=before,metric=metric,**kwargs)
        finally:
            if ticket.done() and not ticket.cancelled():release()
            else:
                ticket.cancel();self._grant()
