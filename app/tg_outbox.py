"""P1 reliability: durable Telegram text -> MAX send ledger.

At-most-one *attempt*, not exactly-once network delivery: an ambiguous MAX
RPC outcome remains UNKNOWN until explicitly reconciled. SQLite is fsynced
before the first attempt; MAX reply mappings are restored after restart.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


class TelegramOutbound:
    def __init__(self,path,topics=None):
        path=Path(path)
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if path.is_symlink():
            raise ValueError("Refusing symlinked TG outbound database")
        self.db=sqlite3.connect(path,isolation_level=None,timeout=10)
        self.db.row_factory=sqlite3.Row
        os.chmod(path,0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=1000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS outbound (
            telegram_chat TEXT NOT NULL,
            telegram_id INTEGER NOT NULL,
            max_chat TEXT NOT NULL,
            payload TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            max_id TEXT,
            prepared TEXT,
            created REAL NOT NULL,
            updated REAL NOT NULL,
            PRIMARY KEY(telegram_chat,telegram_id),
            CHECK(status IN ('RECEIVED','SENDING','SENT','UNKNOWN','FAILED','BLOCKED'))
        );
        CREATE INDEX IF NOT EXISTS outbound_state ON outbound(status,updated);
        """)
        if "prepared" not in {row["name"] for row in
                              self.db.execute("PRAGMA table_info(outbound)")}:
            self.db.execute("ALTER TABLE outbound ADD COLUMN prepared TEXT")
        # A killed worker may have sent MAX message before it died.
        self.db.execute("UPDATE outbound SET status='UNKNOWN',updated=? "
                        "WHERE status='SENDING'",(time.time(),))
        self.topics=topics
        self._lock=asyncio.Lock()
        if topics:
            self.repair_mappings(topics)

    @staticmethod
    def _fingerprint(max_chat,payload):
        canonical=json.dumps([str(max_chat),payload],ensure_ascii=False,
                             sort_keys=True,separators=(",",":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def enqueue(self,tg_chat,tg_id,max_chat,payload):
        if type(tg_id) is not int or tg_id<=0:
            raise ValueError("Invalid Telegram message ID")
        if not isinstance(payload,dict):
            raise ValueError("Invalid MAX outbound payload")
        required={"text","elements","link"}
        if (set(payload) not in (required,required|{"media"})
            or not isinstance(payload["text"],str)
            or not isinstance(payload["elements"],list)
            or payload["link"] is not None and not isinstance(payload["link"],dict)):
            raise ValueError("Unsupported outbound payload shape")
        if "media" in payload:
            media=payload["media"]
            if (not isinstance(media,dict) or
                set(media)!={"kind","file_id","filename","mime","size"}
                or media["kind"] not in ("photo","voice","audio","document","video")
                or not isinstance(media["file_id"],str)
                or not 1<=len(media["file_id"])<=1024
                or type(media["size"]) is not int or media["size"]<0
                or not isinstance(media["filename"],str)
                or not isinstance(media["mime"],str)):
                raise ValueError("Invalid media reference")
        if len(payload["text"])>50000:
            raise ValueError("Message exceeds safe queue size")
        key=(str(tg_chat),int(tg_id))
        fp=self._fingerprint(max_chat,payload)
        row=self.row(*key)
        if row:
            if row["fingerprint"]!=fp or row["max_chat"]!=str(max_chat):
                self.db.execute(
                    "UPDATE outbound SET status='UNKNOWN',updated=? "
                    "WHERE telegram_chat=? AND telegram_id=? AND status='RECEIVED'",
                    (time.time(),*key))
                raise ValueError("Existing Telegram message changed during retry")
            return row["status"]
        self.db.execute(
            "INSERT INTO outbound "
            "(telegram_chat,telegram_id,max_chat,payload,fingerprint,"
            "status,created,updated) "
            "VALUES(?,?,?,?,?,'RECEIVED',?,?)",
            (*key,str(max_chat),json.dumps(payload,ensure_ascii=False),
             fp,time.time(),time.time()))
        return "RECEIVED"

    def row(self,tg_chat,tg_id):
        value=self.db.execute(
            "SELECT * FROM outbound WHERE telegram_chat=? AND telegram_id=?",
            (str(tg_chat),int(tg_id))).fetchone()
        return dict(value) if value else None

    def counts(self):
        return dict(self.db.execute("SELECT status,COUNT(*) FROM outbound GROUP BY status"))

    def pending(self,limit=20):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM outbound WHERE status='RECEIVED' "
            "ORDER BY created,telegram_id LIMIT ?",(int(limit),))]

    def repair_mappings(self,topics):
        repaired=0
        for rec in self.db.execute(
            "SELECT max_chat,max_id,telegram_id FROM outbound "
            "WHERE status='SENT' AND max_id IS NOT NULL"):
            if topics.tg_for_max_message(rec["max_chat"],rec["max_id"]) is None:
                topics.set_message(rec["max_chat"],rec["max_id"],rec["telegram_id"])
                repaired+=1
        return repaired

    @staticmethod
    def _ack_id(response):
        if not isinstance(response,dict) or response.get("_max_error"):
            return None
        message=response.get("message")
        if isinstance(message,dict) and message.get("id") not in (None,""):
            return str(message["id"])
        return None

    async def deliver_one(self,client,tg_chat,tg_id,*,
                          is_allowed=None,tg_bot=None):
        """Safely sends RECEIVED. If worker not authorized, leave queued."""
        async with self._lock:
            item=self.row(tg_chat,tg_id)
            if not item: return "MISSING"
            if item["status"]!="RECEIVED":
                return item["status"]
            if is_allowed is not None and not is_allowed(item["max_chat"]):
                # Pause or deleted chat: preserve, not ack or dispatch.
                return "PAUSED"
            if getattr(client,"_authorized",True) is False:
                return "RECEIVED"
            payload=json.loads(item["payload"])
            prepared=item.get("prepared")
            if "media" in payload and not prepared:
                if tg_bot is None:
                    return "RECEIVED"
                # No MAX SEND_MESSAGE request is possible before preparing
                # the attachment. Any failure here is definite/non-ambiguous
                # w.r.t. chat message delivery.
                meta=payload["media"]
                if meta["size"]>25*1024*1024:
                    self._mark(tg_chat,tg_id,"FAILED")
                    return "FAILED"
                try:
                    file=await tg_bot.get_file(meta["file_id"])
                    data=bytes(await file.download_as_bytearray())
                    if len(data)>25*1024*1024:
                        self._mark(tg_chat,tg_id,"FAILED")
                        return "FAILED"
                    target=int(item["max_chat"]) if item["max_chat"].lstrip("-").isdigit() else item["max_chat"]
                    if meta["kind"]=="photo":
                        attached=await client.upload_photo(data,chat_id=target)
                    elif meta["kind"]=="voice":
                        attached=await client.upload_audio(
                            data,chat_id=target,filename=meta["filename"],
                            mimetype=meta["mime"])
                    else:
                        attached=await client.upload_file(
                            data,chat_id=target,filename=meta["filename"],
                            mimetype=meta["mime"])
                    if not isinstance(attached,dict) or not attached:
                        self._mark(tg_chat,tg_id,"FAILED")
                        return "FAILED"
                    prepared=json.dumps(attached,ensure_ascii=False)
                    if len(prepared)>100000:
                        self._mark(tg_chat,tg_id,"FAILED")
                        return "FAILED"
                    self.db.execute(
                        "UPDATE outbound SET prepared=?,updated=? "
                        "WHERE telegram_chat=? AND telegram_id=?",
                        (prepared,time.time(),str(tg_chat),int(tg_id)))
                except Exception:
                    # File preparation did not issue MAX SEND_MESSAGE.
                    # Mark FAILED for explicit operator review, no silent
                    # expensive re-download/re-upload loops.
                    self._mark(tg_chat,tg_id,"FAILED")
                    return "FAILED"
            self.db.execute(
                "UPDATE outbound SET status='SENDING',updated=? "
                "WHERE telegram_chat=? AND telegram_id=?",
                (time.time(),str(tg_chat),int(tg_id)))
            try:
                kwargs={"elements":payload["elements"],
                        "link":payload["link"],
                        "telegram_message_id":int(tg_id)}
                if prepared:
                    kwargs["attaches"]=[json.loads(prepared)]
                response=await client.send_message(
                    int(item["max_chat"]) if item["max_chat"].lstrip("-").isdigit()
                    else item["max_chat"],payload["text"],**kwargs)
            except BaseException:
                self.db.execute(
                    "UPDATE outbound SET status='UNKNOWN',updated=? "
                    "WHERE telegram_chat=? AND telegram_id=?",
                    (time.time(),str(tg_chat),int(tg_id)))
                raise
            max_id=self._ack_id(response)
            if max_id is None:
                state="FAILED" if isinstance(response,dict) and response.get("_max_error") else "UNKNOWN"
                self.db.execute(
                    "UPDATE outbound SET status=?,updated=? "
                    "WHERE telegram_chat=? AND telegram_id=?",
                    (state,time.time(),str(tg_chat),int(tg_id)))
                return state
            # Persist MAX ID before updating JSON mapping.
            self.db.execute(
                "UPDATE outbound SET status='SENT',max_id=?,updated=? "
                "WHERE telegram_chat=? AND telegram_id=?",
                (max_id,time.time(),str(tg_chat),int(tg_id)))
            if self.topics:
                self.topics.set_message(item["max_chat"],max_id,int(tg_id))
            return "SENT"

    def _mark(self,tg_chat,tg_id,state):
        self.db.execute(
            "UPDATE outbound SET status=?,updated=? "
            "WHERE telegram_chat=? AND telegram_id=?",
            (state,time.time(),str(tg_chat),int(tg_id)))

    async def drain(self,client,*,is_allowed,limit=20,tg_bot=None):
        result={}
        for item in self.pending(limit):
            if not getattr(client,"_authorized",False):
                break
            try:
                outcome=await self.deliver_one(
                    client,item["telegram_chat"],item["telegram_id"],
                    is_allowed=is_allowed,tg_bot=tg_bot)
            except Exception:
                outcome="UNKNOWN"
            result[outcome]=result.get(outcome,0)+1
        return result

    def close(self):
        self.db.close()
