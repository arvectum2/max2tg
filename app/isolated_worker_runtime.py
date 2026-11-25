"""Worker has only its own state, credentials and dedicated IPC channel."""
from __future__ import annotations
import asyncio,os
from pathlib import Path
from app.capsule_registry import CapsuleRegistry
from app.shared_worker_runtime import SharedWorkerRuntime
from app.shared_gateway_runtime import RuntimeLock
from app.telegram_control_plane import TelegramControlPlane
from app.telegram_egress_ipc import GatewayEgressIpcClient,_read_private_key
from app.telegram_ipc import CapsuleIpcServer

class IsolatedWorkerRuntime(SharedWorkerRuntime):
    def __init__(self,sid,state,channel,key_file,forum,actor,client):
        CapsuleRegistry._check_id(sid)
        if type(forum) is not int or forum>=0 or type(actor) is not int or actor<=0:raise ValueError('Invalid local ACL')
        state=Path(state).absolute();channel=Path(channel).absolute();key_file=Path(key_file).absolute()
        for p in (state,channel):
            if p.is_symlink() or not p.is_dir() or p.stat().st_uid!=os.getuid() or p.stat().st_mode&0o077:raise PermissionError('Unsafe local directory')
        if state==channel or state.is_relative_to(channel) or channel.is_relative_to(state):raise ValueError('State and channel must be separate')
        key=_read_private_key(key_file)
        self.profile=None;self.sid=sid;self.client=client;self.forum=forum;self.actor=actor;self._routes={};self.started=False;self.closed=False;self._lock=asyncio.Lock();self._route_lock=asyncio.Lock()
        self.owner=RuntimeLock(state/'worker.lock');self.ledger=None;self.egress=None
        try:
            ipc=GatewayEgressIpcClient(sid,channel/'egress.sock',key_file)
            self._init_local(state,ipc,lambda handler:CapsuleIpcServer.isolated(sid,channel/'ingress.sock',key,forum,actor,handler))
        except BaseException:
            if self.ledger:self.ledger.close()
            if self.egress:self.egress.close()
            self.owner.close();raise

    def route_available(self,cid):return str(cid) in self._routes
    def trusted_topic(self,thread):return next((cid for cid,tid in self._routes.items() if tid==thread),None)

    async def refresh_routes(self):
        async with self._route_lock:
            self._routes={}
            snapshot=await self.listener.ipc.routes()
            if snapshot['forum'] is None and snapshot['actor'] is None and not snapshot['routes']:return
            if type(snapshot['forum']) is not int or type(snapshot['actor']) is not int or (snapshot['forum'],snapshot['actor'])!=(self.forum,self.actor):raise ValueError('Foreign route snapshot')
            routes={};threads=set()
            for entry in snapshot['routes']:
                if not isinstance(entry,list) or len(entry)!=2:raise ValueError('Invalid route')
                cid,tid=entry;cid=TelegramControlPlane.max_chat_key(cid)
                if type(tid) is not int or not 1<tid<2**31 or cid in routes or tid in threads:raise ValueError('Ambiguous route')
                old=self.topics.get_topic(cid);other=self.topics.chat_for_topic(tid)
                if old is not None and old!=tid or other is not None and str(other)!=cid:raise RuntimeError('Conflicting local topic map')
                routes[cid]=tid;threads.add(tid)
            for cid,tid in routes.items():
                if self.topics.get_topic(cid) is None:self.topics.set_topic(cid,tid,'MAX '+cid)
            if self.topics.persist_failed:raise RuntimeError('Topic state persistence failed')
            self._routes=routes
