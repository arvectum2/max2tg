"""Durable capsule outbound intent; tenant never receives Telegram token."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import sqlite3
import time

from app.capsule_registry import CapsuleRegistry
from app.telegram_egress import SOURCE_KEY
from app.telegram_egress_ipc import GatewayEgressIpcClient

TERMINAL = frozenset({"SENT", "UNKNOWN", "BLOCKED"})


class CapsuleEgressOutbox:
    def __init__(self, path: str | Path, sid: str):
        CapsuleRegistry._check_id(sid)
        self.sid = sid
        self.path = Path(path).absolute()
        parent = self.path.parent
        if (parent.is_symlink() or not parent.is_dir()
            or parent.stat().st_mode & 0o077):
            raise PermissionError("Worker state folder must be owner-only")
        if self.path.is_symlink() or (
            self.path.exists() and (
                not self.path.is_file() or self.path.stat().st_mode & 0o077
            )
        ):
            raise PermissionError("Unsafe worker egress database")
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=1000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS worker_telegram_intents(
                source_key TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK(
                    status IN ('PENDING','ACCEPTED','SENT','UNKNOWN','BLOCKED')
                ),
                telegram_id INTEGER,
                updated_at REAL NOT NULL
            );
        """)
        columns={r['name'] for r in self.db.execute('PRAGMA table_info(worker_telegram_intents)')}
        for name in ('max_chat_id','max_message_id'):
            if name not in columns:self.db.execute(f'ALTER TABLE worker_telegram_intents ADD COLUMN {name} TEXT')
        # Legacy pending rows have no proof of non-submission. Conservative migration.
        if 'submission_attempted' not in columns:self.db.execute('ALTER TABLE worker_telegram_intents ADD COLUMN submission_attempted INTEGER NOT NULL DEFAULT 1')
        self._init_receipt_index()
        for suffix in ("-wal", "-shm"):
            extra = self.path.with_name(self.path.name + suffix)
            if extra.exists():
                os.chmod(extra, 0o600)
        self._lock = asyncio.Lock()

    def _init_receipt_index(self):
        # Single owner runtime lock; one transactional migration. Unlike rowid
        # or wall clock, sequence advances when an older intent becomes SENT.
        cols={r['name'] for r in self.db.execute('PRAGMA table_info(worker_telegram_intents)')}
        migration="" if 'receipt_seq' in cols else """
            ALTER TABLE worker_telegram_intents ADD COLUMN receipt_seq INTEGER NOT NULL DEFAULT 0;
            UPDATE worker_telegram_intents SET receipt_seq=rowid WHERE status='SENT';
        """
        self.db.executescript("BEGIN IMMEDIATE;"+migration+"""
            CREATE TABLE IF NOT EXISTS worker_receipt_clock(singleton INTEGER PRIMARY KEY CHECK(singleton=1),seq INTEGER NOT NULL);
            INSERT OR IGNORE INTO worker_receipt_clock SELECT 1,COALESCE(MAX(receipt_seq),0) FROM worker_telegram_intents;
            CREATE UNIQUE INDEX IF NOT EXISTS worker_receipt_seq ON worker_telegram_intents(receipt_seq) WHERE receipt_seq>0;
            CREATE INDEX IF NOT EXISTS worker_receipt_chat ON worker_telegram_intents(max_chat_id,receipt_seq DESC) WHERE status='SENT' AND receipt_seq>0;
            CREATE INDEX IF NOT EXISTS worker_intent_pending ON worker_telegram_intents(updated_at,source_key) WHERE status IN('PENDING','ACCEPTED');
            CREATE TRIGGER IF NOT EXISTS worker_receipt_insert AFTER INSERT ON worker_telegram_intents WHEN NEW.status='SENT' BEGIN
                UPDATE worker_receipt_clock SET seq=seq+1 WHERE singleton=1;
                UPDATE worker_telegram_intents SET receipt_seq=(SELECT seq FROM worker_receipt_clock WHERE singleton=1) WHERE source_key=NEW.source_key;
            END;
            CREATE TRIGGER IF NOT EXISTS worker_receipt_confirm AFTER UPDATE OF status ON worker_telegram_intents WHEN NEW.status='SENT' AND OLD.status!='SENT' BEGIN
                UPDATE worker_receipt_clock SET seq=seq+1 WHERE singleton=1;
                UPDATE worker_telegram_intents SET receipt_seq=(SELECT seq FROM worker_receipt_clock WHERE singleton=1) WHERE source_key=NEW.source_key;
            END;
            COMMIT;
        """)

    def receipt_position(self):
        return self.db.execute('SELECT seq FROM worker_receipt_clock WHERE singleton=1').fetchone()[0]

    def recent_receipts(self,cid,after=0,limit=1000):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=1000:raise ValueError('Invalid receipt range')
        rows=self.db.execute("SELECT max_chat_id,max_message_id,telegram_id FROM worker_telegram_intents WHERE status='SENT' AND receipt_seq>0 AND max_chat_id=? AND receipt_seq>? ORDER BY receipt_seq DESC LIMIT ?",(str(cid),after,limit)).fetchall()
        return reversed(rows)

    def row(self, key: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM worker_telegram_intents WHERE source_key=?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def stage(self, key: str, text: str, *, max_chat_id=None,max_message_id=None) -> str:
        if not isinstance(key, str) or not SOURCE_KEY.fullmatch(key):
            raise ValueError("Invalid source event key")
        if not isinstance(text, str) or not text or "\x00" in text:
            raise ValueError("Invalid outbound text")
        if len(text.encode("utf-16-le")) // 2 > 4096:
            raise ValueError("Telegram text too long")
        from app.telegram_control_plane import TelegramControlPlane
        if max_chat_id is not None:
            max_chat_id=TelegramControlPlane.max_chat_key(max_chat_id)
            if not isinstance(max_message_id,str) or not SOURCE_KEY.fullmatch(max_message_id):raise ValueError('Invalid MAX message ID')
        elif max_message_id is not None:raise ValueError('Incomplete MAX identity')
        identity=text if max_chat_id is None else max_chat_id+'\0'+max_message_id+'\0'+text
        fp=hashlib.sha256(identity.encode('utf-8')).hexdigest()
        old = self.row(key)
        if old:
            if old["fingerprint"] != fp:
                self.db.execute(
                    "UPDATE worker_telegram_intents SET status='UNKNOWN',"
                    "updated_at=? WHERE source_key=? AND status='PENDING'",
                    (time.time(), key)
                )
                raise ValueError("Source event mutated before gateway acceptance")
            return old["status"]
        self.db.execute(
            "INSERT INTO worker_telegram_intents "
            "(source_key,text,fingerprint,status,updated_at,max_chat_id,max_message_id,submission_attempted) "
            "VALUES(?,?,?,'PENDING',?,?,?,0)",
            (key, text, fp, time.time(),max_chat_id,max_message_id)
        )
        return "PENDING"

    async def reconcile_one(self, client: GatewayEgressIpcClient,
                            key: str) -> str:
        """Idempotently ask gateway; never call Telegram Bot API."""
        async with self._lock:
            old = self.row(key)
            if old is None:
                return "MISSING"
            if old["status"] in TERMINAL:
                return old["status"]
            try:
                result = await client.status_verified(key,old['text'],max_chat_id=old['max_chat_id'])
                if result['status']=='CONFLICT':
                    result={'status':'UNKNOWN'}
                elif result['status']=='MISSING':
                    if old['status']=='ACCEPTED' or old['submission_attempted']:
                        # Missing central evidence is not evidence of non-delivery.
                        result={'status':'UNKNOWN'}
                    else:
                        # Commit before any IPC enqueue bytes, including cancellation.
                        changed=self.db.execute("UPDATE worker_telegram_intents SET submission_attempted=1 WHERE source_key=? AND status='PENDING' AND submission_attempted=0",(key,))
                        if changed.rowcount!=1:return self.row(key)['status']
                        result = await client.enqueue(key, old['text'],max_chat_id=old['max_chat_id']) if old['max_chat_id'] is not None else await client.enqueue(key, old['text'])
            except (OSError, EOFError, asyncio.TimeoutError, ValueError):
                # Reconcile on the next signed status; never resubmit blindly.
                return old['status']
            state = result["status"]
            if state in ("QUEUED", "SENDING"):
                final = "ACCEPTED"  # NOT a Telegram delivery acknowledgement.
            elif state in TERMINAL:
                final = state
            else:
                return old["status"]
            message_id = result.get("telegram_id") if final == "SENT" else None
            self.db.execute(
                "UPDATE worker_telegram_intents "
                "SET status=?,telegram_id=?,updated_at=? "
                "WHERE source_key=? AND status IN('PENDING','ACCEPTED')",
                (final, message_id, time.time(), key)
            )
            return self.row(key)["status"]

    async def drain(self, client: GatewayEgressIpcClient,
                    limit: int = 50) -> dict[str, int]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid worker egress batch")
        rows = self.db.execute(
            "SELECT source_key FROM worker_telegram_intents "
            "WHERE status IN('PENDING','ACCEPTED') "
            "ORDER BY updated_at,source_key LIMIT ?", (limit,)
        ).fetchall()
        outcomes: dict[str, int] = {}
        for row in rows:
            status = await self.reconcile_one(client, row["source_key"])
            outcomes[status] = outcomes.get(status, 0) + 1
        return outcomes

    def counts(self) -> dict[str, int]:
        return dict(self.db.execute(
            "SELECT status,COUNT(*) FROM worker_telegram_intents "
            "GROUP BY status"
        ))

    def close(self):
        self.db.close()
