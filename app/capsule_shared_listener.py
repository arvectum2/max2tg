"""Token-free MAX callback adapter for prebound shared text topics.

Not the personal listener. Unsupported events are explicit; no media fallback,
no cursor advance, no Telegram API call, no production runtime activation.
"""
from __future__ import annotations
import hashlib,json
from app.chat_preferences import get_chat_prefs
from app.service_events import is_membership_event
from app.telegram_control_plane import TelegramControlPlane


class SharedCapsuleTextListener:
    def __init__(self,client,topics,ingress_ledger,egress,ipc):
        if egress.sid!=ipc.sid:raise ValueError('Mismatched capsule identity')
        self.client=client;self.topics=topics;self.ingress_ledger=ingress_ledger;self.egress=egress;self.ipc=ipc
        self._receipt_store=None;self._receipt_bindings={};self._receipt_position=0

    def attach(self):
        self.client.on_message(self.on_message)

    async def on_message(self,msg):
        cid=TelegramControlPlane.max_chat_key(msg.chat_id);mid=msg.message_id
        if not isinstance(mid,str) or not mid:raise ValueError('Missing MAX identity')
        if is_membership_event(msg.attaches,msg.text,msg.link):return 'IGNORED_SERVICE'
        if self.topics.get_topic(cid) is None:return 'UNBOUND'
        if not get_chat_prefs(self.topics,cid).sync:return 'PAUSED'
        if msg.is_self:
            if self.ingress_ledger.db.execute("SELECT 1 FROM outbound WHERE max_chat=? AND max_id=? AND status='SENT'",(cid,mid)).fetchone():return 'ECHO'
            if await self.client.telegram_echo_tg_id(msg) is not None:return 'ECHO'
        if msg.attaches or msg.link or not msg.text:return 'UNSUPPORTED'
        raw=msg.raw.get("message") or {}
        if isinstance(raw,dict) and raw.get("elements"):return 'UNSUPPORTED'
        key='max:'+hashlib.sha256(json.dumps([cid,mid],separators=(',',':')).encode()).hexdigest()
        try:return self.egress.stage(key,msg.text,max_chat_id=cid,max_message_id=mid)
        except ValueError:return 'BLOCKED'

    def repair_receipts(self):
        # Only the latest bounded per-chat projection is useful in TopicStore.
        # In-memory watermark is rebuilt on restart or rebinding; it is never
        # trusted across a restored DB or independent topic store replacement.
        bindings=self.topics.receipt_bindings();position=self.egress.receipt_position()
        same=self._receipt_store is self.topics and position>=self._receipt_position
        old_bindings=self._receipt_bindings if same else {}
        if same and position==self._receipt_position and bindings==old_bindings:
            if self.topics.persist_failed:raise RuntimeError('Topic index persistence failed')
            return 0
        def rows():
            for cid,topic in sorted(bindings.items()):
                after=self._receipt_position if same and old_bindings.get(cid)==topic else 0
                if after<position:yield from self.egress.recent_receipts(cid,after)
        repaired=self.topics.apply_confirmed_receipts(rows())
        self._receipt_store=self.topics;self._receipt_bindings=bindings;self._receipt_position=position
        return repaired

    async def drain(self):
        results=await self.egress.drain(self.ipc)
        self.repair_receipts()
        return results
