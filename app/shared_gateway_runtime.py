"""Injected-transport gateway lifecycle; no production entrypoint."""
from __future__ import annotations
import asyncio,os,time,stat
from pathlib import Path
from app.runtime_lock import RuntimeLock
from app.stage_profile import measure,started,finished
from app.telegram_inbox import TelegramInbox
from app.telegram_control_plane import TelegramControlPlane
from app.telegram_egress import TelegramEgress
from app.telegram_egress_ipc import GatewayEgressIpcServer
from app.telegram_gateway import TelegramGatewayEngine
from app.telegram_ipc import CapsuleIpcClient
from app.shared_topic_provision import SharedTopicProvisioner
from app.capsule_shared_ingress import is_plain_text_update
from app.forum_guard import ForumGuard
from app.telegram_budget import TelegramBudget,BudgetedBot,ApiDeferred
from telegram.error import RetryAfter,NetworkError,TimedOut

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
    def __init__(self,registry,bot,*,channels=None,external_only=False,managed=False,profile=None):
        if managed and (channels is None or not external_only):raise ValueError("Managed mode requires external dedicated channels")
        self.wal_maintenance=None;self.profile=profile;self.managed=managed;self._opened=set();self.bot_id=None;self.poll_failures=0
        self.registry=registry;self.bot=bot;self.lock=RuntimeLock(registry.root/'shared-gateway.lock')
        self.inbox=None;self.egress=None;self.server=None;self.servers=[];self.started=False;self._poll_lock=asyncio.Lock();self._drain_lock=asyncio.Lock()
        try:
            if external_only:
                if channels is None:raise PermissionError('External-only gateway requires dedicated channels')
                for row in registry.list_capsules():
                    path=registry.capsule_dir(row['session_id'])/'credentials.json'
                    if path.exists() or path.is_symlink() or (row['credential_mode']!='external' and row['status']!='UNCONFIGURED'):raise PermissionError('Legacy credentials prohibited in external-only gateway')
                    if row['desired_active'] and row['session_id'] not in channels:raise PermissionError('Active external capsule missing channel')
            if managed:
                if isinstance(bot,BudgetedBot) and bot.budget.db is not registry.db:raise PermissionError("Foreign budget")
                self.bot=bot if isinstance(bot,BudgetedBot) else BudgetedBot(bot,TelegramBudget(registry.db,profile=profile))
            self.plane=TelegramControlPlane(registry)
            self.provisioner=SharedTopicProvisioner(self.plane)
            self.guard=ForumGuard(registry,self.bot) if managed else None
            allowed=self._dispatch_allowed if self.guard else None
            self.inbox=TelegramInbox(registry.root/'inbox.sqlite3',self.plane,dispatch_allowed=allowed,profile=profile)
            self.egress=TelegramEgress(registry.root/'telegram-egress.sqlite3',self.plane,topic_scoped=True,dispatch_allowed=allowed,profile=profile)
            self.channels=None
            if channels is not None:
                self.channels={}
                for sid,folder in channels.items():
                    if registry.get(sid)['telegram_mode']!='shared':raise ValueError('Shared channel required')
                    if managed and registry.get(sid)['credential_mode']!='external':raise PermissionError('Managed channel requires external capsule')
                    if registry.get(sid)['credential_mode']=='external':
                        legacy=registry.capsule_dir(sid)/'credentials.json'
                        if legacy.exists() or legacy.is_symlink():raise PermissionError('External capsule contains legacy credentials')
                    p=Path(folder).absolute()
                    if p.is_symlink() or not p.is_dir() or p.stat().st_uid!=os.getuid() or p.stat().st_mode&0o077:raise PermissionError('Unsafe channel directory')
                    p=p.resolve();root=registry.root.resolve()
                    if p==root or p.is_relative_to(root) or root.is_relative_to(p):raise ValueError('Channel must not expose registry')
                    if any(p==q or p.is_relative_to(q) or q.is_relative_to(p) for q in self.channels.values()):raise ValueError('Overlapping tenant channels')
                    self.channels[sid]=p
                def socket_for(sid):
                    if sid not in self.channels:
                        from app.telegram_inbox import WorkerUnavailable
                        raise WorkerUnavailable()
                    return self.channels[sid]/'ingress.sock'
                delivery=CapsuleIpcClient(registry,socket_for=socket_for).deliver
                self.servers=[GatewayEgressIpcServer(registry,self.egress,topic_scoped=True,channel_path=p/'egress.sock',bound_sid=sid) for sid,p in self.channels.items()]
            else:
                if any(r['credential_mode']=='external' and r['desired_active'] for r in registry.list_capsules()):raise PermissionError('External capsules require dedicated channels')
                delivery=None
                self.server=GatewayEgressIpcServer(registry,self.egress,topic_scoped=True)
                self.servers=[self.server]
            self.engine=TelegramGatewayEngine(self.plane,self.inbox,deliver=delivery,egress=self.egress,topic_scoped=True)
            self.inbox.db.execute('CREATE TABLE IF NOT EXISTS polling_checkpoint (singleton INTEGER PRIMARY KEY CHECK(singleton=1),bot_id INTEGER NOT NULL,next_offset INTEGER NOT NULL)')
        except BaseException:
            if self.inbox:self.inbox.close()
            if self.egress:self.egress.close()
            self.lock.close();raise

    async def start(self):
        if self.started:raise RuntimeError('Already started')
        me=await self.bot.get_me();bid=getattr(me,'id',None)
        if type(bid) is not int or bid<=0:raise ValueError('Bot identity unavailable')
        if self.plane.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_onboarding'").fetchone():
            for entry in self.plane.db.execute("SELECT o.bot_id FROM shared_onboarding o JOIN capsules c ON c.session_id=o.sid WHERE o.stage='ACTIVE' AND c.desired_active=1"):
                if entry[0]!=str(bid):raise PermissionError('Active onboarding belongs to a different bot')
        row=self.inbox.db.execute('SELECT bot_id FROM polling_checkpoint WHERE singleton=1').fetchone()
        if row and row[0]!=bid:raise PermissionError('Different bot cannot reuse polling checkpoint')
        self.inbox.db.execute('INSERT OR IGNORE INTO polling_checkpoint VALUES(1,?,0)',(bid,))
        self.bot_id=bid
        if self.managed:
            self.started=True
            try:await self.reconcile_channels()
            except BaseException:self.started=False;raise
        else:
            for server in self.servers:
                await remove_dead_socket(server.path);await server.start()
            self.started=True

    def _desired_channels(self):
        rows=self.plane.db.execute("SELECT o.sid,o.stage,o.bot_id,c.desired_active,c.credential_mode,r.status FROM shared_onboarding o JOIN capsules c ON c.session_id=o.sid LEFT JOIN tg_routes r ON r.session_id=o.sid").fetchall()
        active={r['sid'] for r in rows if r['stage']=='ACTIVE' and r['desired_active']==1 and r['credential_mode']=='external' and r['status']=='ACTIVE' and r['bot_id']==str(self.bot_id)}
        return active

    def _dispatch_allowed(self,sid):
        return not self.poll_failures and self.guard.allowed(sid)

    def _invalidate_poll_leases(self):
        # A missed membership event cannot be replaced by stale positive leases.
        for r in self.plane.db.execute("SELECT forum FROM shared_onboarding WHERE stage='ACTIVE'"):
            self.guard.invalidate(int(r[0]))

    async def reconcile_channels(self):
        """Only pre-mounted channels; never mounts paths or accepts tenant input."""
        if not self.managed or not self.started:raise RuntimeError('Managed gateway not started')
        with measure(self.profile,"gateway.forum_refresh"):
            await self.guard.refresh(self._desired_channels())
        at=started(self.profile)
        async with self._drain_lock:
            finished(self.profile,"gateway.lifecycle_lock",at)
            at=started(self.profile)
            active={sid for sid in self._desired_channels() if self._dispatch_allowed(sid)}
            for server in self.servers:
                sid=server.bound_sid
                if sid not in active and sid in self._opened:
                    await server.stop();self._opened.discard(sid)
                elif sid in active and sid not in self._opened:
                    await remove_dead_socket(server.path);await server.start();self._opened.add(sid)
            result=self.lifecycle_snapshot();finished(self.profile,"gateway.lifecycle",at);return result

    def lifecycle_snapshot(self):
        if not self.managed or not self.started:raise RuntimeError('Managed gateway not started')
        active=self._desired_channels()
        return {'at':time.time(),'sessions':{sid:{'active':sid in active,'ready':sid in active and sid in self._opened and self._dispatch_allowed(sid)} for sid in self.channels}}


    def _offset(self):return self.inbox.db.execute('SELECT next_offset FROM polling_checkpoint WHERE singleton=1').fetchone()[0]

    async def poll_once(self,*,timeout=0):
        if type(timeout) is not int or not 0<=timeout<=30:raise ValueError('Invalid polling timeout')
        async with self._poll_lock:
            if not self.started:raise RuntimeError('Gateway not started')
            offset=self._offset()
            try:updates=await self.bot.get_updates(offset=offset,limit=100,timeout=timeout,allowed_updates=['message','edited_message','message_reaction','callback_query','chat_member','my_chat_member'])
            except (ApiDeferred,RetryAfter):
                if not self.managed:raise
                return 0
            except NetworkError as exc:
                # BadRequest derives from NetworkError in PTB; retry only the
                # exact transport error classes on read-only getUpdates.
                if not self.managed or type(exc) not in (NetworkError,TimedOut):raise
                if not self.poll_failures:self._invalidate_poll_leases()
                self.poll_failures=min(16,self.poll_failures+1)
                return 0
            raw=[u.to_dict() if hasattr(u,'to_dict') else u for u in updates]
            ids=[u.get('update_id') if isinstance(u,dict) else None for u in raw]
            if len(ids)>100 or any(type(i) is not int or i<offset or i>=2**63-1 for i in ids) or ids!=sorted(set(ids)):raise ValueError('Invalid polling batch')
            if self.guard:
                for u in raw:self.guard.observe(u)
            count=0;db=self.inbox.db;at=started(self.profile)
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
            finished(self.profile,'gateway.ingest_commit',at)
            if self.poll_failures:
                self._invalidate_poll_leases()
                self.poll_failures=0
            return count

    async def drain_once(self):
        at=started(self.profile)
        async with self._drain_lock:
            finished(self.profile,"gateway.drain_lock",at)
            if not self.started:raise RuntimeError('Gateway not started')
            held=(self._desired_channels() if self.poll_failures else self.guard.held_sessions()) if self.guard else ()
            if self.managed:
                # Small rounds release the lifecycle lock often. Only separate
                # worker IPCs run concurrently; Bot API sends remain serial.
                with measure(self.profile,'gateway.ingress_round'):
                    ingress=await self.engine.drain(32,exclude=held,concurrency=4,fair=True)
                with measure(self.profile,'gateway.egress_round'):
                    egress=await self.engine.drain_egress(self.bot,16,exclude=held,fair=True)
                return {'ingress':ingress,'egress':egress}
            return {'ingress':await self.engine.drain(exclude=held),'egress':await self.engine.drain_egress(self.bot,exclude=held)}

    async def provision_topic(self,sid,max_chat_id,owner,title):
        async with self._drain_lock:
            if not self.started:raise RuntimeError('Gateway not started')
            if self.guard and not self._dispatch_allowed(sid):raise PermissionError('Forum verification unavailable')
            return await self.provisioner.create(self.bot,sid,max_chat_id,owner,title,dispatch_allowed=self._dispatch_allowed if self.guard else None)

    def enable_wal_maintenance(self):
        from app.wal_maintenance import WalMaintenance
        if not self.managed:raise PermissionError('Managed gateway required')
        self.wal_maintenance=WalMaintenance((self.registry.db,self.inbox.db,self.egress.db))

    async def run(self,stop):
        """Separate polling and delivery loops; fail closed on storage failure."""
        async def polls():
            while not stop.is_set():
                await self.poll_once(timeout=20)
                delay=min(1,self.bot.budget.remaining) if self.managed else 0
                if self.poll_failures:delay=max(delay,min(30,2**min(self.poll_failures-1,5)))
                try:await asyncio.wait_for(stop.wait(),max(.05,delay))
                except asyncio.TimeoutError:pass
        async def drains():
            while not stop.is_set():
                result=await self.drain_once()
                if self.wal_maintenance is not None and not any(result.values()):self.wal_maintenance.idle()
                progress=any(result[d].get(s,0) for d in ("ingress","egress") for s in ("ACKED","SENT","UNKNOWN","BLOCKED"))
                try:await asyncio.wait_for(stop.wait(),.05 if self.managed and progress else 1)
                except asyncio.TimeoutError:pass
        async def checks():
            while not stop.is_set():
                await self.reconcile_channels()
                try:await asyncio.wait_for(stop.wait(),.2)
                except asyncio.TimeoutError:pass
        tasks=[]
        try:
            await self.start()
            tasks=[asyncio.create_task(polls()),asyncio.create_task(drains()),asyncio.create_task(stop.wait())]
            if self.managed:tasks.append(asyncio.create_task(checks()))
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:await task
        finally:
            for task in tasks:task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            await self.close()

    async def close(self):
        async with self._poll_lock,self._drain_lock:
            for server in self.servers:await server.stop()
            self.started=False;self._opened.clear()
            if self.inbox:self.inbox.close();self.inbox=None
            if self.egress:self.egress.close();self.egress=None
            self.lock.close()
