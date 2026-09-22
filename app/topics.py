"""Persistent map between Max chat IDs and Telegram forum topic IDs."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

log = logging.getLogger(__name__)


def _coerce(key: str) -> Any:
    """Restore the original numeric type of a Max chat ID stored as a JSON key."""
    try:
        return int(key)
    except (ValueError, TypeError):
        return key


class TopicStore:
    """JSON-backed bidirectional map: Max chat ID ↔ Telegram forum topic (thread) ID."""

    def __init__(self, path: str):
        self._path = path
        self._chats: dict[str, dict] = {}    # str(max_chat_id) → {"topic_id": int, "title": str}
        self._by_topic: dict[int, Any] = {}  # topic_id → max_chat_id (original type)
        self._messages: dict[str, dict[str, int]] = {}  # chat_id → max_message_id → tg_message_id
        self._by_tg_message: dict[tuple[str, int], str] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            self._chats = data.get("chats", {})
            self._messages = data.get("messages", {})
            for key, rec in self._chats.items():
                tid = rec.get("topic_id")
                if tid is not None:
                    self._by_topic[int(tid)] = _coerce(key)
            for chat_key, mapping in self._messages.items():
                if not isinstance(mapping, dict):
                    continue
                for max_message_id, tg_message_id in mapping.items():
                    self._by_tg_message[(chat_key, int(tg_message_id))] = str(max_message_id)
            log.info(
                "Loaded %d topic mappings and %d message maps from %s",
                len(self._chats), sum(len(v) for v in self._messages.values()), self._path,
            )
        except Exception:
            log.exception("Failed to load topic store %s — starting empty", self._path)
            self._chats = {}
            self._by_topic = {}
            self._messages = {}
            self._by_tg_message = {}

    def _save(self) -> None:
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    {"chats": self._chats, "messages": self._messages},
                    f, ensure_ascii=False, indent=2,
                )
            os.replace(tmp_path, self._path)
        except Exception:
            log.exception("Failed to save topic store %s", self._path)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def get_topic(self, max_chat_id: Any) -> int | None:
        rec = self._chats.get(str(max_chat_id))
        if rec and rec.get("topic_id") is not None:
            return int(rec["topic_id"])
        return None

    def get_title(self, max_chat_id: Any) -> str | None:
        rec = self._chats.get(str(max_chat_id))
        return rec.get("title") if rec else None

    def set_topic(self, max_chat_id: Any, topic_id: int, title: str) -> None:
        self._chats[str(max_chat_id)] = {"topic_id": int(topic_id), "title": title}
        self._by_topic[int(topic_id)] = max_chat_id
        self._save()

    def update_title(self, max_chat_id: Any, title: str) -> None:
        rec = self._chats.get(str(max_chat_id))
        if rec:
            rec["title"] = title
            self._save()

    def chat_for_topic(self, topic_id: int) -> Any | None:
        return self._by_topic.get(int(topic_id))

    def set_message(self, max_chat_id: Any, max_message_id: Any, tg_message_id: int) -> None:
        """Remember one MAX ↔ Telegram message pair for native replies."""
        if max_message_id in (None, "") or tg_message_id is None:
            return
        chat_key = str(max_chat_id)
        max_key = str(max_message_id)
        mapping = self._messages.setdefault(chat_key, {})
        old_tg = mapping.get(max_key)
        if old_tg is not None:
            self._by_tg_message.pop((chat_key, int(old_tg)), None)
        mapping[max_key] = int(tg_message_id)
        self._by_tg_message[(chat_key, int(tg_message_id))] = max_key

        # Keep state bounded while retaining enough recent history for replies.
        while len(mapping) > 1000:
            oldest_max_id = next(iter(mapping))
            oldest_tg_id = mapping.pop(oldest_max_id)
            self._by_tg_message.pop((chat_key, int(oldest_tg_id)), None)
        self._save()

    def tg_for_max_message(self, max_chat_id: Any, max_message_id: Any) -> int | None:
        mapping = self._messages.get(str(max_chat_id), {})
        tg_id = mapping.get(str(max_message_id))
        return int(tg_id) if tg_id is not None else None

    def max_for_tg_message(self, max_chat_id: Any, tg_message_id: int) -> str | None:
        return self._by_tg_message.get((str(max_chat_id), int(tg_message_id)))

    def remove(self, max_chat_id: Any) -> int | None:
        """Drop topic and message mappings for a MAX chat."""
        chat_key = str(max_chat_id)
        rec = self._chats.pop(chat_key, None)
        message_map = self._messages.pop(chat_key, {})
        for tg_message_id in message_map.values():
            self._by_tg_message.pop((chat_key, int(tg_message_id)), None)

        tid = rec.get("topic_id") if rec else None
        if tid is not None:
            self._by_topic.pop(int(tid), None)
        if rec or message_map:
            self._save()
        return int(tid) if tid is not None else None
