"""Opt-in shared text runtime with injected MAX client, never personal polling.

No production entrypoint. Registry access is trusted same-UID prototype only.
"""
from __future__ import annotations
import asyncio,json,time
from dataclasses import asdict
from app.capsule_egress_outbox import CapsuleEgressOutbox
from app.capsule_shared_ingress import SharedCapsuleTextIngress
from app.capsule_shared_listener import SharedCapsuleTextListener
from app.telegram_control_plane import TelegramControlPlane
from app.telegram_egress_ipc import GatewayEgressIpcClient,egress_ipc_path
from app.telegram_ipc import CapsuleIpcServer,ensure_ipc_key
from app.telegram_inbox import WorkerUnavailable
from app.shared_gateway_runtime import RuntimeLock,remove_dead_socket
from app.tg_outbox import TelegramOutbound
from app.topics import TopicStore
from app.stage_profile import measure,started,finished

class SharedWorkerRuntime:
    def __init__(self,registry,sid,client):
        self.profile=None;self.registry=registry;self.sid=sid;self.client=client;self.started=False;self.closed=False;self._lock=asyncio.Lock()
        row=registry.get(sid)
        if row['telegram_mode']!='shared' or not row['desired_active']:raise PermissionError('Shared active capsule required')
        base=registry.capsule_dir(sid);self.owner=RuntimeLock(base/'worker.lock');self.ledger=None;self.egress=None
        try:
            self.plane=TelegramControlPlane(registry);state=base/'state'
            ipc=GatewayEgressIpcClient(sid,egress_ipc_path(registry.root),ensure_ipc_key(registry,sid))
            self._init_local(state,ipc,lambda handler:CapsuleIpcServer(registry,sid,handler))
        except BaseException:
            if self.ledger:self.ledger.close()
            if self.egress:self.egress.close()
            self.owner.close();raise

    def _init_local(self,state,ipc,server_factory):
        self.topics=TopicStore(str(state/'topics.json'),strict=True)
        self.ledger=TelegramOutbound(state/'tg-outbound.sqlite3',self.topics)
        self.egress=CapsuleEgressOutbox(state/'to-telegram.sqlite3',self.sid)
        self.egress.db.execute('CREATE TABLE IF NOT EXISTS worker_event_gaps(max_chat_id TEXT NOT NULL,max_message_id TEXT NOT NULL,reason TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(max_chat_id,max_message_id))')
        self.listener=SharedCapsuleTextListener(self.client,self.topics,self.ledger,self.egress,ipc)
        self.ingress=SharedCapsuleTextIngress(self.client,self.topics,self.ledger,trusted_topic=self.trusted_topic)
        self.server=server_factory(self._receive)

    def trusted_topic(self,thread):return self.plane.max_chat_for_thread(self.sid,thread)
    def route_available(self,cid):return self.plane.topic_route(self.sid,cid) is not None

    async def refresh_routes(self):
        for r in self.plane.db.execute('SELECT max_chat_id,thread_id FROM tg_topic_routes WHERE session_id=? AND active=1',(self.sid,)).fetchall():
            cid,tid=r
            if self.plane.topic_route(self.sid,cid) is None:continue
            old=self.topics.get_topic(cid);other=self.topics.chat_for_topic(tid)
            if old is not None and old!=tid or other is not None and str(other)!=cid:raise RuntimeError('Conflicting local topic map')
            if old is None:self.topics.set_topic(cid,tid,'MAX '+cid)
        if self.topics.persist_failed:raise RuntimeError('Topic state persistence failed')

    async def start(self):
        if self.started or self.closed:raise RuntimeError('Worker lifecycle conflict')
        await self.refresh_routes();self.ledger.repair_mappings(self.topics);self.listener.repair_receipts()
        await remove_dead_socket(self.server.path);await self.server.start()
        self.client.on_message(self.on_message);self.started=True

    async def _receive(self,update):
        at=started(self.profile)
        async with self._lock:
            finished(self.profile,"worker.lock",at)
            if not self.started:raise WorkerUnavailable()
            try:
                with measure(self.profile,"worker.route"):await self.refresh_routes()
            except (OSError,EOFError,asyncio.TimeoutError,ValueError):raise WorkerUnavailable() from None
            with measure(self.profile,"worker.ingress"):return await self.ingress(update)

    async def on_message(self,msg):
        # Do not hold the ingress lock across the client's echo correlation:
        # it may await the in-flight MAX RPC acknowledgement.
        if not self.started:raise RuntimeError('Worker not started')
        cid=TelegramControlPlane.max_chat_key(msg.chat_id)
        try:
            with measure(self.profile,"worker.route"):await self.refresh_routes()
        except (OSError,EOFError,asyncio.TimeoutError,ValueError):outcome='ROUTE_UNAVAILABLE'
        else:outcome=await self._classify(msg,cid)
        if outcome in ('UNBOUND','PAUSED','UNSUPPORTED','BLOCKED','SELF_UNVERIFIED','ROUTE_UNAVAILABLE'):
            raw=json.dumps(asdict(msg),ensure_ascii=False,default=str)
            if len(raw.encode())>1024*1024:raw=json.dumps({'reason':'oversize payload omitted'})
            self.egress.db.execute('INSERT OR IGNORE INTO worker_event_gaps VALUES(?,?,?,?,?)',(cid,str(msg.message_id),outcome,raw,time.time()))
        return outcome

    async def _classify(self,msg,cid):
        if not self.route_available(cid):outcome='UNBOUND'
        elif msg.is_self:
            # Only proven echoes can be discarded; quarantine unverified
            # self messages rather than risking duplicate Telegram delivery.
            mid=msg.message_id
            if self.ledger.db.execute("SELECT 1 FROM outbound WHERE max_chat=? AND max_id=? AND status='SENT'",(cid,mid)).fetchone() or await self.client.telegram_echo_tg_id(msg) is not None:outcome='ECHO'
            else:outcome='SELF_UNVERIFIED'
        else:outcome=await self.listener.on_message(msg)
        return outcome

    async def drain_once(self):
        async with self._lock:
            if not self.started:raise RuntimeError('Worker not started')
            try:
                with measure(self.profile,"worker.route"):await self.refresh_routes()
            except (OSError,EOFError,asyncio.TimeoutError,ValueError):return {"ROUTE_UNAVAILABLE":1}
            return await self.listener.drain()

    async def run(self,stop):
        """Drive actual/injected MAX client; cancellation always closes IPC."""
        task=None
        try:
            await self.start();task=asyncio.create_task(self.client.run())
            while not stop.is_set():
                if task.done():await task;raise RuntimeError('MAX client exited')
                await self.drain_once()
                try:await asyncio.wait_for(stop.wait(),1)
                except asyncio.TimeoutError:pass
        finally:
            if task is not None:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
            await self.close()

    async def close(self):
        async with self._lock:
            self.started=False
            if self.closed:return
            self.closed=True
            await self.server.stop();self.ledger.close();self.egress.close();self.owner.close()
