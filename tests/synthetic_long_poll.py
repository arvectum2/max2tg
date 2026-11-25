"""Bounded synthetic Telegram wait: event wakeup, offset selection, cancellation.
Not production code; no access to Telegram or real account data.
"""
import asyncio

class UpdateWakeup:
    def __init__(self):self.changed=asyncio.Event()
    def notify(self):self.changed.set()
    async def get(self,select,timeout):
        if type(timeout) is not int or not 0<=timeout<=30:raise ValueError('Invalid synthetic timeout')
        deadline=asyncio.get_running_loop().time()+timeout
        while True:
            # Clear before selecting to avoid a lost wakeup. All operations
            # before await execute in the same broker event loop.
            self.changed.clear();rows=select()
            remaining=deadline-asyncio.get_running_loop().time()
            if rows or remaining<=0:return rows
            try:await asyncio.wait_for(self.changed.wait(),remaining)
            except asyncio.TimeoutError:return select()
