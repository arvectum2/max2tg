"""Crash-safe MAX-to-Telegram delivery ledger.

Messages and each outbound API operation are stored before network calls.
An interrupted send is UNKNOWN, not automatically retried: Telegram does
not provide an idempotency token for sendMessage/sendPhoto/etc.
"""
from __future__ import annotations

import contextvars
from datetime import timedelta
from dataclasses import asdict
import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Awaitable

from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest, Forbidden

from app.max_client import MaxMessage

log = logging.getLogger(__name__)

_delivery = contextvars.ContextVar("max2tg_active_delivery", default=None)


class DeliveryInterrupted(Exception):
    def __init__(self, state: str, detail: str):
        super().__init__(detail)
        self.state = state


class DeliveryContext:
    def __init__(self, store: "OutboxStore", key: str):
        self.store = store
        self.key = key
        self.ordinal = 0

    def cached_media(self, methods: tuple[str, ...]) -> bytes | None:
        """Reuse persisted bytes when source CDN URLs expire before retry."""
        stored = self.store._part(self.key, self.ordinal)
        if stored and stored["method"] in methods and stored["media"] is not None:
            return bytes(stored["media"])
        return None

    async def send_part(
        self,
        method: str,
        meta: dict[str, Any],
        send: Callable[[], Awaitable[Any]],
        media: bytes | None = None,
    ):
        index = self.ordinal
        self.ordinal += 1
        fingerprint = hashlib.sha256(
            json.dumps([method, meta], ensure_ascii=False, sort_keys=True,
                       default=str).encode("utf-8")
            + (hashlib.sha256(media).digest() if media is not None else b"")
        ).hexdigest()
        stored = self.store._part(self.key, index)
        if stored:
            if stored["fingerprint"] != fingerprint:
                self.store._set_job(self.key, "UNKNOWN", "changed part after restart")
                raise DeliveryInterrupted("UNKNOWN", "outbound part changed after restart")
            if stored["state"] == "SENT":
                return SimpleNamespace(message_id=int(stored["tg_id"]))
            if stored["state"] in ("SENDING", "UNKNOWN", "FAILED"):
                raise DeliveryInterrupted(stored["state"], "previous uncertain/failed delivery")
        else:
            self.store._insert_part(self.key, index, method, fingerprint, meta, media)

        self.store._update_part(self.key, index, "SENDING")
        try:
            result = await send()
            if result is None or getattr(result, "message_id", None) is None:
                self.store._update_part(self.key, index, "UNKNOWN")
                raise DeliveryInterrupted("UNKNOWN", "Telegram response has no message_id")
            self.store._update_part(self.key, index, "SENT", int(result.message_id))
            return result
        except RetryAfter as exc:
            self.store._update_part(self.key, index, "RETRY")
            wait = exc.retry_after
            seconds = wait.total_seconds() if isinstance(wait, timedelta) else float(wait)
            self.store._schedule_retry(self.key, max(1, seconds),
                                       "Telegram rate limit")
            raise DeliveryInterrupted("RETRY", "rate limit; safe to retry") from exc
        except (TimedOut, NetworkError, TimeoutError) as exc:
            self.store._update_part(self.key, index, "UNKNOWN")
            self.store._set_job(self.key, "UNKNOWN", type(exc).__name__)
            raise DeliveryInterrupted("UNKNOWN", "Telegram delivery outcome uncertain") from exc
        except (BadRequest, Forbidden) as exc:
            self.store._update_part(self.key, index, "FAILED")
            self.store._set_job(self.key, "FAILED", type(exc).__name__)
            raise DeliveryInterrupted("FAILED", "Telegram rejected request") from exc
        except DeliveryInterrupted:
            raise
        except BaseException as exc:
            # Cancellation or unexpected failure can arrive AFTER the Telegram
            # server accepted a message. Never re-send it automatically.
            self.store._update_part(self.key, index, "UNKNOWN")
            self.store._set_job(self.key, "UNKNOWN", type(exc).__name__)
            raise


def current_delivery() -> DeliveryContext | None:
    return _delivery.get()


class OutboxStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                key TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                max_id TEXT NOT NULL,
                event TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'RECEIVED',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                tg_primary INTEGER,
                created REAL NOT NULL,
                updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS parts (
                job_key TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                method TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                meta TEXT NOT NULL,
                media BLOB,
                state TEXT NOT NULL DEFAULT 'PENDING',
                tg_id INTEGER,
                PRIMARY KEY(job_key, ordinal),
                FOREIGN KEY(job_key) REFERENCES jobs(key) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS jobs_due
                ON jobs(status, next_attempt, created);
        """)
        # Process was killed between marking SENDING and receiving/storing an
        # acknowledgement. Those calls MUST NOT be repeated blindly.
        with self.db:
            self.db.execute(
                "UPDATE parts SET state='UNKNOWN' WHERE state='SENDING'"
            )
            self.db.execute(
                """UPDATE jobs SET status='UNKNOWN', last_error='interrupted send'
                WHERE status='SENDING' AND EXISTS
                  (SELECT 1 FROM parts WHERE job_key=jobs.key AND state='UNKNOWN')"""
            )
            self.db.execute(
                "UPDATE jobs SET status='RETRY', next_attempt=0 "
                "WHERE status='SENDING' AND NOT EXISTS "
                "(SELECT 1 FROM parts WHERE job_key=jobs.key AND state='UNKNOWN')"
            )

    @staticmethod
    def job_key(chat_id, message_id) -> str:
        return json.dumps([str(chat_id), str(message_id)], ensure_ascii=False)

    def enqueue(self, msg: MaxMessage) -> str | None:
        if msg.message_id in (None, "") or msg.chat_id is None:
            return None
        key = self.job_key(msg.chat_id, msg.message_id)
        serialized = json.dumps(asdict(msg), ensure_ascii=False, default=str)
        now = time.time()
        self.db.execute(
            "INSERT OR IGNORE INTO jobs"
            "(key,chat_id,max_id,event,status,created,updated)"
            " VALUES(?,?,?,?,?,?,?)",
            (key, str(msg.chat_id), str(msg.message_id),
             serialized, "RECEIVED", now, now),
        )
        return key

    def job(self, key: str):
        return self.db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()

    def record_mapping(self, key: str, tg_message_id: int):
        """Commit MAX→TG primary ID before the secondary JSON topic mapping."""
        self.db.execute("UPDATE jobs SET tg_primary=?, updated=? WHERE key=?",
                        (int(tg_message_id), time.time(), key))

    def reconcile_mappings(self, topic_store) -> int:
        """Repair lost JSON mappings from already committed SQLite receipts."""
        fixed = 0
        for row in self.db.execute(
            "SELECT chat_id,max_id,tg_primary FROM jobs "
            "WHERE tg_primary IS NOT NULL AND status='SENT'"
        ):
            if topic_store.tg_for_max_message(row["chat_id"], row["max_id"]) is None:
                topic_store.set_message(row["chat_id"], row["max_id"],
                                        int(row["tg_primary"]))
                fixed += 1
        return fixed

    def _part(self, key, index):
        return self.db.execute("SELECT * FROM parts WHERE job_key=? AND ordinal=?",
                               (key, index)).fetchone()

    def _insert_part(self, key, index, method, fingerprint, meta, media):
        self.db.execute(
            "INSERT INTO parts(job_key,ordinal,method,fingerprint,meta,media,state)"
            " VALUES(?,?,?,?,?,?,?)",
            (key,index,method,fingerprint,json.dumps(meta,ensure_ascii=False,default=str),
             media,"PENDING"),
        )

    def _update_part(self, key, index, state, message_id=None):
        self.db.execute("UPDATE parts SET state=?, tg_id=COALESCE(?,tg_id)"
                        " WHERE job_key=? AND ordinal=?",
                        (state,message_id,key,index))

    def _set_job(self, key, status, error=None):
        self.db.execute(
            "UPDATE jobs SET status=?, last_error=?, updated=? WHERE key=?",
            (status,error,time.time(),key),
        )

    def _schedule_retry(self, key, delay, reason):
        self.db.execute(
            "UPDATE jobs SET status='RETRY', attempts=attempts+1, "
            "next_attempt=?, last_error=?, updated=? WHERE key=?",
            (time.time()+delay,reason,time.time(),key),
        )

    def ready(self, limit: int = 50):
        return self.db.execute(
            "SELECT * FROM jobs WHERE status IN ('RECEIVED','RETRY') "
            "AND next_attempt <= ? ORDER BY created, rowid LIMIT ?",
            (time.time(),limit),
        ).fetchall()

    def counts(self) -> dict[str,int]:
        return {r[0]:r[1] for r in self.db.execute(
            "SELECT status,COUNT(*) FROM jobs GROUP BY status"
        ).fetchall()}

    async def process(self, msg: MaxMessage, callback) -> bool:
        key = self.enqueue(msg)
        if not key:
            return bool(await callback())
        current = self.job(key)
        if current["status"] == "SENT":
            return True
        if current["status"] not in ("RECEIVED", "RETRY"):
            return False
        if current["next_attempt"] > time.time():
            return False
        self._set_job(key, "SENDING")
        context = DeliveryContext(self,key)
        token = _delivery.set(context)
        try:
            result = await callback()
            if not result:
                self._schedule_retry(key, 15, "delivery not complete")
                return False
            self._set_job(key, "SENT")
            # Keep fingerprints/IDs for dedup; discard media blobs after ack.
            self.db.execute("UPDATE parts SET media=NULL WHERE job_key=?", (key,))
            return True
        except DeliveryInterrupted as exc:
            log.warning("Outbox delivery paused chat=%s state=%s",
                        msg.chat_id, exc.state)
            if exc.state == "RETRY":
                return False
            self._set_job(key, "UNKNOWN" if exc.state=="SENDING" else exc.state,
                          str(exc))
            return False
        except Exception:
            self._schedule_retry(key, 30, "processing exception")
            log.exception("Outbox processing failed chat=%s", msg.chat_id)
            return False
        finally:
            _delivery.reset(token)

    def recoverable(self, limit=50) -> list[MaxMessage]:
        out=[]
        for row in self.ready(limit):
            try:
                out.append(MaxMessage(**json.loads(row["event"])))
            except (ValueError, TypeError):
                self._set_job(row["key"], "FAILED", "malformed stored event")
        return out

    def close(self):
        self.db.close()
