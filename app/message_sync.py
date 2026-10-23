"""Safety-limited message mutation synchronization for the personal bridge.

No destructive MAX DELETE RPC is exposed: an observed opcode-66 forMe=false
has documented watermark-related cascade risk. A Telegram deletion has no
Bot API update and cannot be inferred.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from html import escape
from typing import Any

from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError, TimedOut

from app.outbox import OutboxStore
from app.max_formatting import format_max_html
from app.topics import TopicStore

log = logging.getLogger(__name__)
REACTION_EMOJI = frozenset({
    "👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬",
    "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴",
    "😍", "🐳", "❤️‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆",
    "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈", "😴", "😭",
    "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝",
    "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿", "🆒",
    "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂", "🤷",
    "🤷‍♀", "😡",
})


def parse_mutation(opcode: int, payload: dict) -> tuple[str, str, str, dict] | None:
    """Only accept unambiguous, exact-id edit/delete/reaction events."""
    if not isinstance(payload, dict):
        return None
    chat_id = payload.get("chatId")
    item = payload.get("message") or {}
    if not isinstance(item, dict):
        item = {}
    mid = payload.get("messageId") or item.get("id")
    if opcode == 128:
        if not (item.get("status") == "EDITED" or item.get("updateTime")):
            return None
        kind = "edit"
    elif opcode == 142:
        # A range/bulk delete (opcode 140) is deliberately not mirrored.
        ids = payload.get("messageIds")
        if ids is not None:
            if not isinstance(ids, list) or len(ids) != 1:
                return None
            mid = ids[0]
        if not mid or payload.get("forMe") is True:
            return None
        kind = "delete"
    elif opcode in (155, 156):
        if "reactionInfo" not in payload or not isinstance(payload["reactionInfo"], dict):
            return None
        kind = "reaction"
    else:
        return None
    if chat_id is None or mid is None or isinstance(mid, bool):
        return None
    return kind, str(chat_id), str(mid), payload


class MessageSync:
    """Map MAX events onto the one Telegram message this bot actually owns.

    Store each mutation before execution. Retry idempotent API edits/reactions,
    but do not automatically repeat ambiguous deletions after a timeout.
    """

    def __init__(self, outbox: OutboxStore, topics: TopicStore, bot, tg_chat_id: str):
        self.outbox = outbox
        self.topics = topics
        self.bot = bot
        self.tg_chat_id = int(tg_chat_id)
        self.db = outbox.db
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS message_mutations (
                key TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                max_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                updated REAL NOT NULL
            )
        """)

    def _event_key(self, kind: str, chat_id: str, mid: str, payload: dict):
        # Edit variants with identical text and updatedTime are idempotent;
        # reactions include complete snapshot, not a click/delta.
        if kind == "edit":
            msg = payload.get("message") or {}
            significant = [msg.get("updateTime"), msg.get("text"), msg.get("status")]
        elif kind == "reaction":
            significant = payload.get("reactionInfo")
        else:
            significant = "delete"
        source = json.dumps([kind,chat_id,mid,significant],
                            ensure_ascii=False,sort_keys=True)
        return hashlib.sha256(source.encode()).hexdigest()

    def _known_bot_message_ids(self, chat_id: str, mid: str) -> list[int]:
        row = self.db.execute(
            "SELECT key FROM jobs WHERE chat_id=? AND max_id=? "
            "AND status='SENT'", (chat_id,mid),
        ).fetchone()
        if row is None:
            return []
        ids=[]
        for part in self.db.execute(
            "SELECT tg_id,tg_ids FROM parts WHERE job_key=? "
            "AND state='SENT' AND tg_id IS NOT NULL "
            "ORDER BY ordinal DESC",(row["key"],)
        ):
            if part["tg_ids"]:
                ids.extend(reversed(json.loads(part["tg_ids"])))
            else:
                ids.append(int(part["tg_id"]))
        return ids

    async def accept(self, opcode: int, payload: dict) -> bool:
        parsed = parse_mutation(opcode, payload)
        if parsed is None:
            return False
        kind, chat_id, mid, body = parsed
        if self.topics.get_topic(chat_id) is None:
            return False
        key = self._event_key(kind,chat_id,mid,body)
        self.db.execute(
            "INSERT OR IGNORE INTO message_mutations "
            "(key,chat_id,max_id,kind,payload,updated) VALUES(?,?,?,?,?,?)",
            (key,chat_id,mid,kind,
             json.dumps(body,ensure_ascii=False),time.time()),
        )
        return await self.apply(key)

    async def apply(self, key: str) -> bool:
        row = self.db.execute(
            "SELECT * FROM message_mutations WHERE key=?", (key,),
        ).fetchone()
        if row is None or row["state"] in ("APPLIED","BLOCKED","UNKNOWN"):
            return False
        kind, chat_id, mid = row["kind"], row["chat_id"], row["max_id"]
        payload = json.loads(row["payload"])
        tg_id = self.topics.tg_for_max_message(chat_id,mid)
        if tg_id is None:
            # Defer until the initial message is imported.
            return False
        try:
            if kind == "edit":
                msg = payload.get("message") or {}
                # Avoid stale out-of-order edited events overwriting a newer
                # change already reflected in Telegram.
                try:
                    ts = int(msg.get("updateTime") or 0)
                except (ValueError,TypeError):
                    ts = 0
                if ts:
                    for older in self.db.execute(
                        "SELECT payload FROM message_mutations "
                        "WHERE chat_id=? AND max_id=? AND kind='edit' "
                        "AND state='APPLIED'",
                        (chat_id,mid),
                    ):
                        previous = json.loads(older["payload"]).get("message") or {}
                        try:
                            previous_ts = int(previous.get("updateTime") or 0)
                        except (ValueError,TypeError):
                            previous_ts = 0
                        if previous_ts > ts:
                            self._set(key,"BLOCKED")
                            return False
                if not isinstance(msg.get("text"),str):
                    return False
                text = ("✏️ <b>Изменено в MAX</b>\n"
                        + format_max_html(msg["text"], msg.get("elements")))
                # Only edit original bot-authored messages. A TG-originated
                # message belongs to a user and is not editable by the bot.
                ids = self._known_bot_message_ids(chat_id,mid)
                if not ids or len(ids) > 1:
                    # Editing only the first part of a long message would
                    # leave its old continuation visible and misleading.
                    # Require an atomic multi-part edit plan in a later step.
                    self._set(key,"BLOCKED")
                    return False
                # Text/caption selection is based on original API method.
                method = self.db.execute(
                    "SELECT method FROM parts WHERE job_key=? AND tg_id=?",
                    (self.outbox.job_key(chat_id,mid),tg_id),
                ).fetchone()
                if method and method["method"] in (
                    "send_photo","send_video","send_document","send_voice","send_audio"):
                    # caption cap is 1024; don't destroy prior media, skip overly long text
                    if len(msg["text"]) > 750:
                        self._set(key,"BLOCKED")
                        return False
                    await self.bot.edit_message_caption(
                        chat_id=self.tg_chat_id,message_id=tg_id,
                        caption=text,parse_mode=ParseMode.HTML)
                else:
                    if len(msg["text"]) > 3500:
                        self._set(key,"BLOCKED")
                        return False
                    await self.bot.edit_message_text(
                        chat_id=self.tg_chat_id,message_id=tg_id,
                        text=text,parse_mode=ParseMode.HTML)
            elif kind == "delete":
                ids = self._known_bot_message_ids(chat_id,mid)
                if not ids:
                    self._set(key,"BLOCKED")
                    return False
                # Never infer deletes of arbitrary users' own Telegram
                # messages. Limit to bot-authored MAX-to-TG outbox parts.
                for item in ids:
                    try:
                        await self.bot.delete_message(
                            chat_id=self.tg_chat_id,message_id=item)
                    except BadRequest as exc:
                        if "message to delete not found" not in str(exc).lower():
                            raise
            elif kind == "reaction":
                info = payload["reactionInfo"]
                # Telegram reactions are attributed to the bot, not MAX
                # participants. Mirror only the current MAX account's
                # reaction, not aggregated reactions from third parties.
                if "yourReaction" not in info:
                    self._set(key,"BLOCKED")
                    return False
                emoji = info["yourReaction"]
                if emoji and emoji not in REACTION_EMOJI:
                    self._set(key,"BLOCKED")
                    return False
                from telegram import ReactionTypeEmoji
                reactions = [ReactionTypeEmoji(emoji)] if emoji else []
                await self.bot.set_message_reaction(
                    chat_id=self.tg_chat_id,message_id=tg_id,
                    reaction=reactions,
                )
        except TimedOut:
            self._set(key,"UNKNOWN" if kind=="delete" else "PENDING")
            return False
        except TelegramError as exc:
            log.warning("MAX mutation %s not applied: %s",kind,type(exc).__name__)
            self._set(key,"BLOCKED")
            return False
        except Exception:
            log.exception("MAX mutation failed kind=%s",kind)
            return False
        self._set(key,"APPLIED")
        return True

    def _set(self,key,state):
        self.db.execute(
            "UPDATE message_mutations SET state=?,attempts=attempts+1,"
            "updated=? WHERE key=?", (state,time.time(),key))

    async def recover_pending(self,limit=50):
        rows=self.db.execute(
            "SELECT key FROM message_mutations WHERE state='PENDING' "
            "ORDER BY updated LIMIT ?",(limit,),
        ).fetchall()
        for row in rows:
            await self.apply(row["key"])
