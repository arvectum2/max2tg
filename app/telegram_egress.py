"""P2.2b: central, durable text-only Telegram egress (offline first).

Only the trusted gateway provides its Bot API sender. Capsule workers never
receive a shared bot token or select arbitrary target Telegram forums.
Uncertain Bot API outcomes remain UNKNOWN and cannot be automatically replayed.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time

from app.telegram_control_plane import TelegramControlPlane

SOURCE_KEY = re.compile(r"^[A-Za-z0-9:_./-]{1,128}$")


class TelegramEgress:
    """A single owner-private SQLite journal under the capsule registry root."""

    def __init__(self, path: str | Path, plane: TelegramControlPlane):
        self.plane = plane
        self.path = Path(path).absolute()
        if self.path.parent != plane.capsules.root:
            raise ValueError("Egress database must reside in private registry root")
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError("Unsafe egress database path")
        if self.path.exists() and self.path.stat().st_mode & 0o077:
            raise PermissionError("Egress database permissions unsafe")
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=1000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS telegram_egress(
                session_id TEXT NOT NULL,
                source_key TEXT NOT NULL,
                tg_chat_id TEXT NOT NULL,
                text TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK(
                    status IN ('QUEUED','SENDING','SENT','UNKNOWN','BLOCKED')
                ),
                telegram_id INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(session_id,source_key)
            );
            CREATE INDEX IF NOT EXISTS egress_ready
                ON telegram_egress(status,created_at);
        """)
        self.db.execute(
            "UPDATE telegram_egress SET status='UNKNOWN',updated_at=? "
            "WHERE status='SENDING'", (time.time(),),
        )
        for ext in ("-wal", "-shm"):
            p = self.path.with_name(self.path.name + ext)
            if p.exists():
                os.chmod(p, 0o600)
        self._lock = asyncio.Lock()

    def _route(self, sid: str) -> str | None:
        self.plane.capsules._check_id(sid)
        row = self.plane.db.execute(
            "SELECT r.tg_chat_id FROM tg_routes r "
            "JOIN capsules c ON c.session_id=r.session_id AND c.user_id=r.user_id "
            "JOIN tg_owners o ON o.user_id=r.user_id "
            "WHERE r.session_id=? AND r.status='ACTIVE' AND c.desired_active=1",
            (sid,),
        ).fetchone()
        return row["tg_chat_id"] if row else None

    def row(self, sid: str, source_key: str) -> dict | None:
        self.plane.capsules._check_id(sid)
        row = self.db.execute(
            "SELECT * FROM telegram_egress WHERE session_id=? AND source_key=?",
            (sid, source_key),
        ).fetchone()
        return dict(row) if row else None

    def enqueue(self, sid: str, source_key: str, text: str) -> str:
        """Durably admit one immutable event for an ACTIVE owned forum.

        No caller-supplied chat ID, thread ID or bot credential is accepted.
        For now, send plain text to the forum general topic only.
        """
        self.plane.capsules._check_id(sid)
        if not isinstance(source_key, str) or not SOURCE_KEY.fullmatch(source_key):
            raise ValueError("Invalid source event key")
        if not isinstance(text, str) or not text or "\x00" in text:
            raise ValueError("Invalid text content")
        if len(text.encode("utf-16-le")) // 2 > 4096:
            raise ValueError("Telegram text too long")
        chat = self._route(sid)
        if chat is None:
            raise PermissionError("No active Telegram forum for session")
        fingerprint = hashlib.sha256((chat + "\0" + text).encode()).hexdigest()
        previous = self.row(sid, source_key)
        if previous is not None:
            if previous["fingerprint"] != fingerprint:
                # Changed events are never silently delivered in place.
                self.db.execute(
                    "UPDATE telegram_egress SET status='UNKNOWN',updated_at=? "
                    "WHERE session_id=? AND source_key=? AND status='QUEUED'",
                    (time.time(), sid, source_key),
                )
                raise ValueError("Existing source event changed")
            return previous["status"]
        now = time.time()
        self.db.execute(
            "INSERT INTO telegram_egress "
            "(session_id,source_key,tg_chat_id,text,fingerprint,status,"
            "created_at,updated_at) VALUES(?,?,?,?,?,'QUEUED',?,?)",
            (sid, source_key, chat, text, fingerprint, now, now),
        )
        return "QUEUED"

    def ready(self, limit: int = 50) -> list[tuple[str, str]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid egress batch limit")
        return [
            (r["session_id"], r["source_key"])
            for r in self.db.execute(
                "SELECT session_id,source_key FROM telegram_egress "
                "WHERE status='QUEUED' ORDER BY created_at,session_id,source_key "
                "LIMIT ?", (limit,),
            )
        ]

    def counts(self) -> dict[str, int]:
        return dict(self.db.execute(
            "SELECT status,COUNT(*) FROM telegram_egress GROUP BY status"
        ))

    def _uncertain(self, sid: str, source_key: str) -> None:
        self.db.execute(
            "UPDATE telegram_egress SET status='UNKNOWN',updated_at=? "
            "WHERE session_id=? AND source_key=? AND status='SENDING'",
            (time.time(), sid, source_key),
        )

    async def send_one(self, bot, sid: str, source_key: str) -> str:
        """Only the central bot sender performs network I/O.

        Every failure after marking SENDING could follow a successful API
        call and is therefore UNKNOWN; never automatically retry it.
        """
        async with self._lock:
            row = self.row(sid, source_key)
            if row is None:
                return "MISSING"
            if row["status"] != "QUEUED":
                return row["status"]
            if self._route(sid) != row["tg_chat_id"]:
                self.db.execute(
                    "UPDATE telegram_egress SET status='BLOCKED',updated_at=? "
                    "WHERE session_id=? AND source_key=? AND status='QUEUED'",
                    (time.time(), sid, source_key),
                )
                return "BLOCKED"
            # The compare-and-swap prevents sending the same event from
            # competing egress instances sharing the same SQLite file.
            claim = self.db.execute(
                "UPDATE telegram_egress SET status='SENDING',updated_at=? "
                "WHERE session_id=? AND source_key=? AND status='QUEUED'",
                (time.time(), sid, source_key),
            )
            if claim.rowcount != 1:
                return self.row(sid, source_key)["status"]
            try:
                response = await bot.send_message(
                    chat_id=int(row["tg_chat_id"]),
                    text=row["text"],
                    parse_mode=None,
                )
            except asyncio.CancelledError:
                self._uncertain(sid, source_key)
                raise
            except Exception:
                self._uncertain(sid, source_key)
                return "UNKNOWN"
            message_id = getattr(response, "message_id", None)
            if type(message_id) is not int or message_id <= 0:
                self._uncertain(sid, source_key)
                return "UNKNOWN"
            self.db.execute(
                "UPDATE telegram_egress SET status='SENT',telegram_id=?,updated_at=? "
                "WHERE session_id=? AND source_key=? AND status='SENDING'",
                (message_id, time.time(), sid, source_key),
            )
            return "SENT"

    async def drain(self, bot, limit: int = 50) -> dict[str, int]:
        outcomes: dict[str, int] = {}
        for sid, source_key in self.ready(limit):
            outcome = await self.send_one(bot, sid, source_key)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return outcomes

    def close(self) -> None:
        self.db.close()
