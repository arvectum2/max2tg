"""P1 reliability: durable Telegram text -> MAX send ledger.

At-most-one *attempt*, not exactly-once network delivery: an ambiguous MAX
RPC outcome remains UNKNOWN until explicitly reconciled. SQLite is fsynced
before the first attempt; MAX reply mappings are restored after restart.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import time


log = logging.getLogger(__name__)


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
        # Collecting members are BLOCKED in the legacy state machine: an
        # older worker after rollback cannot dispatch them individually.
        cols={r['name'] for r in self.db.execute('PRAGMA table_info(outbound)')}
        for name,typ in [('album_id','TEXT'),('album_sender','TEXT'),('album_thread','INTEGER'),('album_pending','INTEGER NOT NULL DEFAULT 0')]:
            if name not in cols:self.db.execute(f'ALTER TABLE outbound ADD COLUMN {name} {typ}')
        self.db.execute('CREATE INDEX IF NOT EXISTS outbound_album ON outbound(telegram_chat,album_id,album_pending)')
        # A killed worker may have sent MAX message before it died.
        self.db.execute("UPDATE outbound SET status='UNKNOWN',updated=? "
                        "WHERE status='SENDING'",(time.time(),))
        self.topics=topics
        self._lock=asyncio.Lock()
        self.album_wakeup=asyncio.Event()
        if topics:
            self.repair_mappings(topics)

    @staticmethod
    def _fingerprint(max_chat,payload):
        canonical=json.dumps([str(max_chat),payload],ensure_ascii=False,
                             sort_keys=True,separators=(",",":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def enqueue(self,tg_chat,tg_id,max_chat,payload,*,album=None):
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
        if album is not None:
            if (not isinstance(album,tuple) or len(album)!=3 or 'media' not in payload
                or not isinstance(album[0],str) or not 1<=len(album[0])<=128
                or type(album[1]) is not int or album[1]<=0
                or type(album[2]) is not int or album[2]<=0):
                raise ValueError('Invalid Telegram album identity')
        key=(str(tg_chat),int(tg_id))
        fp=self._fingerprint(max_chat,payload if album is None else [payload,album])
        row=self.row(*key)
        if row:
            if row["fingerprint"]!=fp or row["max_chat"]!=str(max_chat):
                self.db.execute(
                    "UPDATE outbound SET status='UNKNOWN',updated=? "
                    "WHERE telegram_chat=? AND telegram_id=? AND status='RECEIVED'",
                    (time.time(),*key))
                if row.get('album_pending'):
                    self.db.execute("UPDATE outbound SET album_pending=0,updated=? WHERE telegram_chat=? AND album_id=? AND album_pending=1",(time.time(),key[0],row['album_id']))
                raise ValueError("Existing Telegram message changed during retry")
            return 'RECEIVED' if row.get('album_pending') else row['status']
        if album is not None:
            aid,uid,thread=album
            previous=self.db.execute('SELECT max_chat,album_sender,album_thread,album_pending FROM outbound WHERE telegram_chat=? AND album_id=?',(key[0],aid)).fetchall()
            valid=all(r['album_pending'] and r['max_chat']==str(max_chat) and r['album_sender']==str(uid) and r['album_thread']==thread for r in previous) and len(previous)<10
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if not valid:
                    self.db.execute('UPDATE outbound SET album_pending=0,updated=? WHERE telegram_chat=? AND album_id=? AND album_pending=1',(time.time(),key[0],aid))
                self.db.execute("INSERT INTO outbound (telegram_chat,telegram_id,max_chat,payload,fingerprint,status,created,updated,album_id,album_sender,album_thread,album_pending) VALUES(?,?,?,?,?,'BLOCKED',?,?,?,?,?,?)",(*key,str(max_chat),json.dumps(payload,ensure_ascii=False),fp,time.time(),time.time(),aid,str(uid),thread,int(valid)))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK');raise
            if valid:self.album_wakeup.set()
            return 'RECEIVED' if valid else 'BLOCKED'
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
        return dict(self.db.execute("SELECT CASE WHEN album_pending=1 THEN 'RECEIVED' ELSE status END AS effective_status,COUNT(*) FROM outbound GROUP BY effective_status"))

    def pending(self,limit=20):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM outbound WHERE status='RECEIVED' "
            "ORDER BY created,telegram_id LIMIT ?",(int(limit),))]

    def repair_mappings(self,topics):
        repaired=0
        for rec in self.db.execute(
            "SELECT max_chat,max_id,MIN(telegram_id) AS telegram_id FROM outbound "
            "WHERE status='SENT' AND max_id IS NOT NULL GROUP BY max_chat,max_id"):
            if topics.tg_for_max_message(rec["max_chat"],rec["max_id"]) is None:
                topics.set_message(rec["max_chat"],rec["max_id"],rec["telegram_id"])
                repaired+=1
        return repaired

    def confirmed_album_reply(self,tg_chat,tg_id,max_chat):
        row=self.row(tg_chat,tg_id)
        if row and row.get('album_id') and row['max_chat']==str(max_chat) and row['status']=='SENT':
            return row['max_id']
        return None

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
                    elif meta["kind"]=="video":
                        attached=await client.upload_video(data,chat_id=target,filename=meta["filename"],mimetype=meta["mime"])
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
                except Exception as exc:
                    # File preparation did not issue MAX SEND_MESSAGE.
                    # Log only the exception class, never attachment tokens,
                    # private text or user-provided filenames.
                    log.warning("Telegram media preparation failed: telegram_id=%s, cause=%s",
                                tg_id, type(exc).__name__)
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

    def has_pending_albums(self):
        return bool(self.db.execute('SELECT 1 FROM outbound WHERE album_pending=1 LIMIT 1').fetchone())

    @staticmethod
    def _album_caption(payloads):
        captions=[(i,p) for i,p in enumerate(payloads,1) if p['text']]
        if len(captions)==1:return captions[0][1]
        text='';elements=[]
        for i,p in captions:
            prefix=('\n\n' if text else '')+f'[{i}] '
            text+=prefix;offset=len(text);text+=p['text']
            for e in p['elements']:
                elements.append(dict(e,**{'from':e['from']+offset}))
        return {'text':text,'elements':elements}

    def _album_intake_waiting(self,key):
        return bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbound_updates'").fetchone() and self.db.execute("SELECT 1 FROM inbound_updates WHERE telegram_chat=? AND json_extract(payload,'$.message.media_group_id')=? LIMIT 1",key).fetchone())

    async def drain_albums(self,client,*,is_allowed,tg_bot,is_media_allowed=None,on_result=None):
        result={}
        groups=self.db.execute("SELECT telegram_chat,album_id FROM outbound WHERE album_pending=1 GROUP BY telegram_chat,album_id ORDER BY MIN(created) LIMIT 20").fetchall()
        for group in groups:
            async with self._lock:
                key=(group['telegram_chat'],group['album_id'])
                rows=[dict(r) for r in self.db.execute('SELECT * FROM outbound WHERE telegram_chat=? AND album_id=? ORDER BY telegram_id',key)]
                if not rows or not all(r['album_pending'] for r in rows):continue
                leader=rows[0];target=leader['max_chat'];thread=leader['album_thread']
                def allowed():
                    return (getattr(client,'_authorized',False) and is_allowed(target)
                        and (is_media_allowed is None or is_media_allowed(target))
                        and (self.topics is None or self.topics.get_topic(target)==thread))
                if not allowed() or tg_bot is None:continue
                age=time.time()-max(r['created'] for r in rows)
                if age<3 or self._album_intake_waiting(key):continue
                if len(rows)<2 and age<60:continue
                payloads=[json.loads(r['payload']) for r in rows]
                caption=self._album_caption(payloads)
                links=[p['link'] for p in payloads if p['link'] is not None]
                state=None;mid=None;prepared=[]
                if (not 2<=len(rows)<=10 or len(caption['text'])>50000 or len(caption['elements'])>1000 or any(x!=links[0] for x in links)
                    or any(p['media']['kind'] not in ('photo','video') for p in payloads)):
                    state='BLOCKED'
                else:
                    try:
                        size=0
                        for p in payloads:
                            meta=p['media']
                            if meta['size']>25*1024*1024:raise ValueError('size')
                            f=await tg_bot.get_file(meta['file_id']);data=bytes(await f.download_as_bytearray());size+=len(data)
                            if not data or len(data)>25*1024*1024 or size>50*1024*1024:raise ValueError('size')
                            dest=int(target) if target.lstrip('-').isdigit() else target
                            if meta['kind']=='photo':
                                att=await client.upload_photo(data,chat_id=dest)
                                valid=isinstance(att,dict) and att.get('_type')=='PHOTO' and bool(att.get('photoToken'))
                            else:
                                att=await client.upload_video(data,chat_id=dest,filename=meta['filename'],mimetype=meta['mime'])
                                valid=isinstance(att,dict) and att.get('_type')=='VIDEO' and att.get('videoId') is not None and bool(att.get('token'))
                            if not valid:raise ValueError('album preparation')
                            prepared.append(json.dumps(att,ensure_ascii=False))
                        if len(set(prepared))!=len(prepared) or any(len(a)>100000 for a in prepared):raise ValueError('attachment identity')
                    except Exception as exc:
                        log.warning('Telegram album preparation failed: %s',type(exc).__name__);state='FAILED'
                # Revalidate membership and authorization after awaited uploads.
                current=self.db.execute('SELECT telegram_id,album_pending FROM outbound WHERE telegram_chat=? AND album_id=? ORDER BY telegram_id',key).fetchall()
                if [(r['telegram_id'],r['album_pending']) for r in current]!=[(r['telegram_id'],1) for r in rows]:continue
                if not allowed() or self._album_intake_waiting(key):continue
                if state is None:
                    self.db.execute('BEGIN IMMEDIATE')
                    try:
                        for row,att in zip(rows,prepared):
                            self.db.execute("UPDATE outbound SET status='SENDING',album_pending=0,prepared=?,updated=? WHERE telegram_chat=? AND telegram_id=? AND album_pending=1",(att,time.time(),row['telegram_chat'],row['telegram_id']))
                        self.db.execute('COMMIT')
                    except BaseException:
                        self.db.execute('ROLLBACK');raise
                    try:
                        response=await client.send_message(int(target) if target.lstrip('-').isdigit() else target,caption['text'],elements=caption['elements'],link=links[0] if links else None,telegram_message_id=leader['telegram_id'],attaches=[json.loads(a) for a in prepared])
                        mid=self._ack_id(response)
                        received=(response.get('message') or {}).get('attaches') if mid else None
                        exact=isinstance(received,list) and len(received)==len(rows) and [a.get('_type') if isinstance(a,dict) else None for a in received]==[p['media']['kind'].upper() for p in payloads]
                        state='SENT' if mid and exact else ('FAILED' if isinstance(response,dict) and response.get('_max_error') else 'UNKNOWN')
                    except BaseException:
                        self.db.execute("UPDATE outbound SET status='UNKNOWN',updated=? WHERE telegram_chat=? AND album_id=? AND status='SENDING'",(time.time(),*key));raise
                self.db.execute("UPDATE outbound SET status=?,album_pending=0,max_id=?,updated=? WHERE telegram_chat=? AND album_id=? AND (album_pending=1 OR status='SENDING')",(state,mid,time.time(),*key))
                if state=='SENT' and self.topics:self.topics.set_message(target,mid,leader['telegram_id'])
            result[state]=result.get(state,0)+len(rows)
            if on_result:
                for row in (rows if state=='SENT' else rows[:1]):
                    try:await on_result(row,state)
                    except Exception:log.warning('Could not surface album delivery result')
        return result

    async def drain(self,client,*,is_allowed,limit=20,tg_bot=None,
                    is_media_allowed=None,on_result=None):
        result=await self.drain_albums(client,is_allowed=is_allowed,tg_bot=tg_bot,is_media_allowed=is_media_allowed,on_result=on_result)
        for item in self.pending(limit):
            if not getattr(client,"_authorized",False):
                break
            if (is_media_allowed is not None and "media" in json.loads(item["payload"])
                and not is_media_allowed(item["max_chat"])):
                continue
            try:
                outcome=await self.deliver_one(
                    client,item["telegram_chat"],item["telegram_id"],
                    is_allowed=is_allowed,tg_bot=tg_bot)
            except Exception:
                outcome="UNKNOWN"
            result[outcome]=result.get(outcome,0)+1
            if on_result is not None and outcome in ("SENT","FAILED","UNKNOWN"):
                try:
                    await on_result(item,outcome)
                except Exception:
                    log.warning("Could not surface deferred delivery result: telegram_id=%s",
                                item["telegram_id"])
        return result

    def close(self):
        self.db.close()
