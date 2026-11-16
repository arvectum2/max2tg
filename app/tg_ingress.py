"""Durable Telegram intake before the Bot API polling offset is advanced.

Only authorized text/media messages in bound, enabled MAX topics are staged.
They are cleared after the existing outbound ledger has accepted them, never
merely because the in-memory Application handler returned.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from telegram import Update
from telegram.ext import Application

from app.chat_preferences import get_chat_prefs

log = logging.getLogger(__name__)


class DurableIngressQueue(asyncio.Queue):
    def __init__(self, ledger, topics, supergroup_id, allowed_users):
        super().__init__()
        self.ledger = ledger
        self.topics = topics
        self.group_id = int(supergroup_id)
        self.allowed_users = frozenset(allowed_users)
        self.queued = set()
        ledger.db.execute("""
            CREATE TABLE IF NOT EXISTS inbound_updates (
                update_id INTEGER PRIMARY KEY,
                telegram_chat TEXT NOT NULL,
                telegram_id INTEGER NOT NULL,
                max_chat TEXT NOT NULL,
                thread_id INTEGER NOT NULL,
                payload TEXT NOT NULL,
                created REAL NOT NULL
            )
        """)

    def _destination(self, update):
        if not isinstance(update, Update) or not update.message:
            return None
        m = update.message
        if (m.chat_id != self.group_id or not m.from_user
            or m.from_user.is_bot
            or (self.allowed_users and m.from_user.id not in self.allowed_users)
            or m.message_thread_id is None):
            return None
        cid = self.topics.chat_for_topic(m.message_thread_id)
        if cid is None:
            return None
        pref = get_chat_prefs(self.topics, cid)
        if not pref.sync:
            return None
        text = m.text or ""
        if text == "☰ Меню" or any(
            e.type == "bot_command" and e.offset == 0 for e in m.entities
        ):
            return None
        has_media = bool(m.photo or m.voice or m.audio or m.document or m.video)
        if not text and not (has_media and pref.media):
            return None
        return str(cid)

    def put_nowait(self, update):
        cid = self._destination(update)
        if cid is not None:
            m = update.message
            if update.update_id in self.queued:
                return
            # PTB advances getUpdates offset only after Queue.put returns.
            # The FULL synchronous outbound SQLite connection commits here
            # before the update can be acknowledged to Telegram.
            if self.ledger.row(m.chat_id, m.message_id) is None:
                self.ledger.db.execute(
                    "INSERT OR IGNORE INTO inbound_updates VALUES (?,?,?,?,?,?,?)",
                    (update.update_id, str(m.chat_id), m.message_id, cid,
                     m.message_thread_id, update.to_json(), time.time()),
                )
                log.info("Telegram intake staged: id=%s thread=%s",
                         m.message_id, m.message_thread_id)
            self.queued.add(update.update_id)
        super().put_nowait(update)

    def may_process(self, update):
        if not isinstance(update, Update) or not update.message:
            return True
        rec = self.ledger.db.execute(
            "SELECT max_chat FROM inbound_updates WHERE update_id=?",
            (update.update_id,),
        ).fetchone()
        if rec is None:
            return True
        current = self.topics.chat_for_topic(update.message.message_thread_id)
        if current is not None and str(current) == rec["max_chat"]:
            return True
        self.queued.discard(update.update_id)
        log.error("Telegram intake retained: original topic mapping changed; id=%s",
                  update.message.message_id)
        return False

    def finish(self, update):
        if not isinstance(update, Update) or not update.message:
            return
        m = update.message
        self.queued.discard(update.update_id)
        if self.ledger.row(m.chat_id, m.message_id) is not None:
            self.ledger.db.execute(
                "DELETE FROM inbound_updates WHERE update_id=?", (update.update_id,))
        elif self.ledger.db.execute(
            "SELECT 1 FROM inbound_updates WHERE update_id=?", (update.update_id,)
        ).fetchone():
            log.error("Telegram intake not accepted by outbound ledger: id=%s thread=%s",
                      m.message_id, m.message_thread_id)

    def replay(self, bot):
        restored = 0
        for rec in self.ledger.db.execute(
            "SELECT * FROM inbound_updates ORDER BY update_id"
        ).fetchall():
            if self.ledger.row(rec["telegram_chat"], rec["telegram_id"]):
                self.ledger.db.execute(
                    "DELETE FROM inbound_updates WHERE update_id=?", (rec["update_id"],))
                continue
            if rec["update_id"] in self.queued:
                continue
            update = Update.de_json(json.loads(rec["payload"]), bot)
            # Do not replay into a reassigned thread or a paused chat.
            if self._destination(update) != rec["max_chat"]:
                continue
            self.put_nowait(update)
            restored += 1
        if restored:
            log.info("Replayed %d durable Telegram intake updates", restored)
        return restored

    def pending_count(self, *, older_than=0):
        return self.ledger.db.execute(
            "SELECT count(*) FROM inbound_updates WHERE created<=?",
            (time.time()-older_than,),
        ).fetchone()[0]


class DurableIngressApplication(Application):
    async def process_update(self, update):
        if not self.update_queue.may_process(update):
            return
        await super().process_update(update)
        # A crash before this point leaves the durable staged copy intact.
        self.update_queue.finish(update)
