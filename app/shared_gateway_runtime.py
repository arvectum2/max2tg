"""Injected-transport gateway lifecycle; no production entrypoint."""
from __future__ import annotations
import asyncio,fcntl,os,time,stat
from app.telegram_inbox import TelegramInbox
from app.telegram_control_plane import TelegramControlPlane
from app.telegram_egress import TelegramEgress
from app.telegram_egress_ipc import GatewayEgressIpcServer
from app.telegram_gateway import TelegramGatewayEngine
from app.shared_topic_provision import SharedTopicProvisioner
from app.capsule_shared_ingress import is_plain_text_update

class RuntimeLock:
    def __init__(self,path):
        self.fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise PermissionError('Unsafe runtime lock')
            fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd);self.fd=None;raise
    def close(self):
        if self.fd is not None:os.close(self.fd);self.fd=None

async def remove_dead_socket(path):
    """Caller holds exclusive runtime lock. Never unlink a live listener."""
    if not path.exists() and not path.is_symlink():return
    info=path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid():raise PermissionError('Unsafe IPC socket')
    try:
        _,writer=await asyncio.wait_for(asyncio.open_unix_connection(str(path)),timeout=1)
    except ConnectionRefusedError:
        now=path.lstat()
        if (now.st_dev,now.st_ino)!=(info.st_dev,info.st_ino):raise RuntimeError('Socket changed during probe')
        path.unlink();return
    writer.close();await writer.wait_closed();raise RuntimeError('IPC listener already active')

class SharedGatewayRuntime:
    def __init__(self,registry,bot):
        self.registry=registry;self.bot=bot;self.lock=RuntimeLock(registry.root/'shared-gateway.lock')
        self.inbox=None;self.egress=None;self.server=None;self.started=False;self._poll_lock=asyncio.Lock();self._drain_lock=asyncio.Lock()
        try:
            self.plane=TelegramControlPlane(registry)
            self.provisioner=SharedTopicProvisioner(self.plane)
            self.inbox=TelegramInbox(registry.root/'inbox.sqlite3',self.plane)
            self.egress=TelegramEgress(registry.root/'telegram-egress.sqlite3',self.plane,topic_scoped=True)
            self.engine=TelegramGatewayEngine(self.plane,self.inbox,egress=self.egress,topic_scoped=True)
            self.server=GatewayEgressIpcServer(registry,self.egress,topic_scoped=True)
            self.inbox.db.execute('CREATE TABLE IF NOT EXISTS polling_checkpoint (singleton INTEGER PRIMARY KEY CHECK(singleton=1),bot_id INTEGER NOT NULL,next_offset INTEGER NOT NULL)')
        except BaseException:
            if self.inbox:self.inbox.close()
            if self.egress:self.egress.close()
            self.lock.close();raise

    async def start(self):
        if self.started:raise RuntimeError('Already started')
        me=await self.bot.get_me();bid=getattr(me,'id',None)
        if type(bid) is not int or bid<=0:raise ValueError('Bot identity unavailable')
        row=self.inbox.db.execute('SELECT bot_id FROM polling_checkpoint WHERE singleton=1').fetchone()
        if row and row[0]!=bid:raise PermissionError('Different bot cannot reuse polling checkpoint')
        self.inbox.db.execute('INSERT OR IGNORE INTO polling_checkpoint VALUES(1,?,0)',(bid,))
        await remove_dead_socket(self.server.path);await self.server.start();self.started=True

    def _offset(self):return self.inbox.db.execute('SELECT next_offset FROM polling_checkpoint WHERE singleton=1').fetchone()[0]

    async def poll_once(self,*,timeout=0):
        if type(timeout) is not int or not 0<=timeout<=30:raise ValueError('Invalid polling timeout')
        async with self._poll_lock:
            if not self.started:raise RuntimeError('Gateway not started')
            offset=self._offset()
            updates=await self.bot.get_updates(offset=offset,limit=100,timeout=timeout,allowed_updates=['message','edited_message','message_reaction','callback_query'])
            raw=[u.to_dict() if hasattr(u,'to_dict') else u for u in updates]
            ids=[u.get('update_id') if isinstance(u,dict) else None for u in raw]
            if len(ids)>100 or any(type(i) is not int or i<offset or i>=2**63-1 for i in ids) or ids!=sorted(set(ids)):raise ValueError('Invalid polling batch')
            count=0;db=self.inbox.db
            db.execute('BEGIN IMMEDIATE')
            try:
                for u in raw:
                    admitted=self.inbox.ingest(u)
                    if admitted is None:continue
                    uid,sid=admitted;count+=1
                    if not self.engine._trusted_topic(sid,u) or not is_plain_text_update(u):
                        db.execute("UPDATE telegram_ingress SET status='BLOCKED',updated_at=? WHERE update_id=? AND status='RECEIVED'",(time.time(),uid))
                if ids:db.execute('UPDATE polling_checkpoint SET next_offset=? WHERE singleton=1',(ids[-1]+1,))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK');raise
            return count

    async def drain_once(self):
        async with self._drain_lock:
            if not self.started:raise RuntimeError('Gateway not started')
            return {'ingress':await self.engine.drain(),'egress':await self.engine.drain_egress(self.bot)}

    async def provision_topic(self,sid,max_chat_id,owner,title):
        async with self._drain_lock:
            if not self.started:raise RuntimeError('Gateway not started')
            return await self.provisioner.create(self.bot,sid,max_chat_id,owner,title)

    async def run(self,stop):
        """Separate polling and delivery loops; fail closed on storage failure."""
        async def polls():
            while not stop.is_set():
                await self.poll_once(timeout=20)
                await asyncio.sleep(.05)
        async def drains():
            while not stop.is_set():
                await self.drain_once()
                try:await asyncio.wait_for(stop.wait(),1)
                except asyncio.TimeoutError:pass
        tasks=[]
        try:
            await self.start()
            tasks=[asyncio.create_task(polls()),asyncio.create_task(drains()),asyncio.create_task(stop.wait())]
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:await task
        finally:
            for task in tasks:task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            await self.close()

    async def close(self):
        async with self._poll_lock,self._drain_lock:
            if self.server:await self.server.stop()
            self.started=False
            if self.inbox:self.inbox.close();self.inbox=None
            if self.egress:self.egress.close();self.egress=None
            self.lock.close()
