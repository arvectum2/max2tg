"""P2.2 durable central Telegram ingress before isolated-capsule dispatch.

A dedicated dispatcher (single getUpdates consumer) validates chat ownership
before enqueuing updates. Attempts are acknowledged only after a trusted
worker response. Uncertain attempts are UNKNOWN, never blindly replayed.
Payloads contain private content and must stay in a protected local DB.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Awaitable,Callable

from app.telegram_control_plane import TelegramControlPlane

MAX_UPDATE_BYTES=512*1024
GROUP_CONTENT_TYPES=("message","edited_message","channel_post",
                     "edited_channel_post","callback_query",
                     "message_reaction","message_reaction_count")


def identity(update:dict) -> tuple[int | None,int | None]:
    """Read authoritative Telegram update fields, not client-supplied text.

    Do not route anonymous posts, bot-only reactions, or reaction-count
    events without a verified owner identity.
    """
    if not isinstance(update,dict):
        return None,None
    for key in ("message","edited_message"):
        event=update.get(key)
        if isinstance(event,dict):
            return _integer(event.get("chat",{}).get("id")), _integer(
                event.get("from",{}).get("id"))
    event=update.get("callback_query")
    if isinstance(event,dict):
        msg=event.get("message") or {}
        return _integer(msg.get("chat",{}).get("id")), _integer(
            event.get("from",{}).get("id"))
    event=update.get("message_reaction")
    if isinstance(event,dict):
        return _integer(event.get("chat",{}).get("id")), _integer(
            event.get("user",{}).get("id"))
    return None,None


def _integer(raw):
    if type(raw) is int:
        return raw
    return None


class TelegramInbox:
    """Fail-closed durable ingress ledger for a *single* Telegram bot."""

    def __init__(self,db_path:str|Path,plane:TelegramControlPlane):
        self.plane=plane
        self.path=Path(db_path).absolute()
        if self.path.is_symlink():
            raise ValueError("Symlinked inbox forbidden")
        if self.path.parent!=plane.capsules.root:
            raise ValueError("Inbox must reside inside the private registry root")
        self.db=sqlite3.connect(self.path,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        import os
        os.chmod(self.path,0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=1000")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS telegram_ingress(
            update_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            tg_chat_id TEXT NOT NULL,
            tg_actor_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at REAL NOT NULL,
            CHECK(status IN('RECEIVED','INFLIGHT','ACKED','UNKNOWN','BLOCKED'))
          );
          CREATE INDEX IF NOT EXISTS ingress_status
          ON telegram_ingress(status,updated_at);
        """)
        # A process killed after a network handoff can have delivered MAX
        # messages. Mark uncertainty explicitly rather than replaying.
        self.db.execute("UPDATE telegram_ingress SET status='UNKNOWN' "
                        "WHERE status='INFLIGHT'")
        for ext in ("-wal","-shm"):
            p=self.path.with_name(self.path.name+ext)
            if p.exists():
                os.chmod(p,0o600)

    def ingest(self,update:dict) -> tuple[int,str] | None:
        """Persist authorized update or reject without saving private content."""
        chat,actor=identity(update)
        session=self.plane.route_group(chat,actor)
        if not session:
            return None
        update_id=update.get("update_id")
        if type(update_id) is not int or update_id<0:
            raise ValueError("Telegram update_id missing/invalid")
        raw=json.dumps(update,ensure_ascii=False,separators=(",",":"))
        encoded=raw.encode("utf-8")
        if len(encoded)>MAX_UPDATE_BYTES:
            raise ValueError("Telegram update too large")
        digest=hashlib.sha256(encoded).hexdigest()
        current=self.db.execute(
            "SELECT session_id,fingerprint FROM telegram_ingress WHERE update_id=?",
            (update_id,)).fetchone()
        if current:
            if current["fingerprint"]!=digest or current["session_id"]!=session:
                raise ValueError("Duplicate update_id has different routing or payload")
            return update_id,session
        self.db.execute(
            "INSERT INTO telegram_ingress VALUES(?,?,?,?,? ,?,?,?)",
            (update_id,session,str(chat),str(actor),raw,digest,
             "RECEIVED",time.time()))
        return update_id,session

    def row(self,update_id:int):
        current=self.db.execute(
            "SELECT * FROM telegram_ingress WHERE update_id=?",
            (update_id,)).fetchone()
        return dict(current) if current else None

    def ready(self,limit:int=100) -> list[int]:
        if not 1<=limit<=100:
            raise ValueError("Invalid batch size")
        return [r[0] for r in self.db.execute(
            "SELECT update_id FROM telegram_ingress WHERE status='RECEIVED' "
            "ORDER BY update_id LIMIT ?",(limit,))]

    async def dispatch(self,update_id:int,deliver:Callable[[str,dict],Awaitable[bool]]) -> str:
        """Dispatch a persisted update. A false/uncertain response is UNKNOWN.

        A definitive local failure before the first transport byte can be
        raised as WorkerUnavailable, leaving it RECEIVED for later retry.
        """
        row=self.row(update_id)
        if not row:
            raise KeyError("Update does not exist")
        if row["status"]!="RECEIVED":
            return row["status"]
        # Revoked access must be checked at delivery time as well.
        session=self.plane.route_group(int(row["tg_chat_id"]),
                                       int(row["tg_actor_id"]))
        if session!=row["session_id"]:
            self.db.execute(
                "UPDATE telegram_ingress SET status='BLOCKED',updated_at=? "
                "WHERE update_id=?",(time.time(),update_id))
            return "BLOCKED"
        self.db.execute(
            "UPDATE telegram_ingress SET status='INFLIGHT',updated_at=? "
            "WHERE update_id=? AND status='RECEIVED'",
            (time.time(),update_id))
        try:
            confirmed=await deliver(row["session_id"],json.loads(row["payload"]))
        except WorkerUnavailable:
            # No request made to worker; retry is unambiguously safe.
            self.db.execute(
                "UPDATE telegram_ingress SET status='RECEIVED',updated_at=? "
                "WHERE update_id=?",(time.time(),update_id))
            return "RECEIVED"
        except Exception:
            self.db.execute(
                "UPDATE telegram_ingress SET status='UNKNOWN',updated_at=? "
                "WHERE update_id=?",(time.time(),update_id))
            return "UNKNOWN"
        status="ACKED" if confirmed is True else "UNKNOWN"
        self.db.execute(
            "UPDATE telegram_ingress SET status=?,updated_at=? "
            "WHERE update_id=?",(status,time.time(),update_id))
        return status

    def counts(self):
        return dict(self.db.execute(
            "SELECT status,COUNT(*) FROM telegram_ingress GROUP BY status"))

    def close(self):
        self.db.close()


class WorkerUnavailable(Exception):
    """Worker not connected; no attempt was made to deliver payload."""
