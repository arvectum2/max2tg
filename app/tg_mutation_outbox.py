"""Durable journal for Telegram-originated MAX edits and reactions."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


class TgMutationOutbox:
    def __init__(self, path):
        path=Path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.is_symlink():
            raise ValueError("Unsafe database path")
        self.db=sqlite3.connect(path,isolation_level=None,timeout=10)
        self.db.row_factory=sqlite3.Row
        os.chmod(path,0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS tg_mutations(
          key TEXT PRIMARY KEY,kind TEXT NOT NULL,chat_id TEXT NOT NULL,
          max_id TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,
          created REAL NOT NULL,updated REAL NOT NULL)""")
        self.db.execute("UPDATE tg_mutations SET status='UNKNOWN' WHERE status='SENDING'")
        self._lock=asyncio.Lock()

    def enqueue(self,kind,chat_id,max_id,payload):
        if kind not in ("edit","reaction") or not max_id or not isinstance(payload,dict):
            raise ValueError("Invalid mutation")
        if kind=="edit" and (set(payload)!={"text","elements"}
            or not isinstance(payload["text"],str)
            or not isinstance(payload["elements"],list)
            or len(payload["text"])>50000):
            raise ValueError("Invalid edit")
        if kind=="reaction" and set(payload)!={"emoji"}:
            raise ValueError("Invalid reaction")
        raw=json.dumps([kind,str(chat_id),str(max_id),payload],
                       sort_keys=True,ensure_ascii=False)
        key=hashlib.sha256(raw.encode()).hexdigest()
        now=time.time()
        self.db.execute(
            "INSERT OR IGNORE INTO tg_mutations VALUES(?,?,?,?,?,'RECEIVED',?,?)",
            (key,kind,str(chat_id),str(max_id),
             json.dumps(payload,ensure_ascii=False),now,now))
        return key

    async def apply(self,key,client):
        async with self._lock:
            row=self.db.execute("SELECT * FROM tg_mutations WHERE key=?",(key,)).fetchone()
            if row is None:return "MISSING"
            if row["status"]!="RECEIVED":return row["status"]
            if getattr(client,"_authorized",True) is False:return "RECEIVED"
            self.db.execute(
                "UPDATE tg_mutations SET status='SENDING',updated=? WHERE key=?",
                (time.time(),key))
            payload=json.loads(row["payload"])
            try:
                chat=int(row["chat_id"]) if row["chat_id"].lstrip("-").isdigit() else row["chat_id"]
                if row["kind"]=="edit":
                    result=await client.edit_message(
                        chat,row["max_id"],payload["text"],payload["elements"])
                else:
                    result=await client.react_to_message(
                        chat,row["max_id"],payload["emoji"])
            except BaseException:
                self.db.execute(
                    "UPDATE tg_mutations SET status='UNKNOWN',updated=? WHERE key=?",
                    (time.time(),key))
                raise
            if isinstance(result,dict) and not result.get("_max_error"):
                if row["kind"]=="edit":
                    msg=result.get("message")
                    status=("SENT" if isinstance(msg,dict)
                            and str(msg.get("id"))==row["max_id"] else "UNKNOWN")
                else:
                    status="SENT" if result else "UNKNOWN"
            else:
                status=("FAILED" if isinstance(result,dict)
                        and result.get("_max_error") else "UNKNOWN")
            self.db.execute(
                "UPDATE tg_mutations SET status=?,updated=? WHERE key=?",
                (status,time.time(),key))
            return status

    def counts(self):
        return dict(self.db.execute(
            "SELECT status,COUNT(*) FROM tg_mutations GROUP BY status"))

    def close(self):
        self.db.close()
