"""Token-free Telegram text ingress for a shared-bot MAX capsule.

The signed inbound CapsuleIpcServer owns Telegram actor/forum ACL. This
adapter checks the per-capsule MAX-chat/thread mapping and journals the
Telegram->MAX send before any MAX network call. Media/edits are not accepted.
"""
from __future__ import annotations

from app.telegram_inbox import WorkerUnavailable
from app.tg_outbox import TelegramOutbound
from app.topics import TopicStore


class SharedCapsuleTextIngress:
    def __init__(self, client, topics: TopicStore, ledger: TelegramOutbound):
        self.client = client
        self.topics = topics
        self.ledger = ledger

    async def __call__(self, update: dict) -> bool:
        message = update.get("message")
        if not isinstance(message, dict) or (
            "edited_message" in update or "callback_query" in update
        ):
            return False
        chat = message.get("chat")
        if not isinstance(chat,dict):
            return False
        thread_id = message.get("message_thread_id")
        if type(thread_id) is not int or thread_id <= 0:
            return False
        max_chat_id = self.topics.chat_for_topic(thread_id)
        if max_chat_id is None:
            return False
        telegram_id = message.get("message_id")
        text = message.get("text")
        if (type(telegram_id) is not int or telegram_id <= 0
            or not isinstance(text,str) or not text or len(text)>50000
            or "reply_to_message" in message):
            return False
        # No MAX network operation if the connection is not authenticated.
        if getattr(self.client,"_authorized",False) is not True:
            raise WorkerUnavailable()
        try:
            status = self.ledger.enqueue(
                chat["id"],telegram_id,max_chat_id,
                {"text":text,"elements":[],"link":None}
            )
        except (TypeError,ValueError,KeyError):
            return False
        if status == "SENT":
            return True
        if status in ("UNKNOWN","FAILED","BLOCKED"):
            return False
        result = await self.ledger.deliver_one(
            self.client,chat["id"],telegram_id,
            is_allowed=lambda target: str(target)==str(max_chat_id)
        )
        # A successful MAX reply ID is mandatory before signing an IPC ACK.
        return result == "SENT"
