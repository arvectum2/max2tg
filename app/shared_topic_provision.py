"""Trusted gateway-only topic creation; ambiguous creation never replayed."""
from __future__ import annotations
import asyncio,time
from app.telegram_budget import BudgetedBot,ApiDeferred

class SharedTopicProvisioner:
    def __init__(self,plane):
        self.plane=plane;self.db=plane.db;self._lock=asyncio.Lock()
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS topic_creation(
        session_id TEXT NOT NULL,max_chat_id TEXT NOT NULL,forum TEXT NOT NULL,
        title TEXT NOT NULL,state TEXT NOT NULL,thread_id INTEGER,updated REAL NOT NULL,
        PRIMARY KEY(session_id,max_chat_id),CHECK(state IN('SENDING','BOUND','UNKNOWN','BLOCKED')))""")
        self.db.execute("UPDATE topic_creation SET state='UNKNOWN' WHERE state='SENDING'")

    async def create(self,bot,sid,max_chat_id,owner,title,*,dispatch_allowed=None):
        cid=self.plane.max_chat_key(max_chat_id)
        if not isinstance(title,str) or not title.strip() or len(title.encode('utf-16-le'))//2>128 or '\x00' in title:raise ValueError('Invalid topic title')
        async with self._lock:
            r=self.db.execute("SELECT tg_chat_id FROM tg_routes WHERE session_id=? AND status='ACTIVE'",(sid,)).fetchone()
            if r is None or self.plane.route_group(r[0],owner)!=sid:raise PermissionError('No active owned forum')
            forum=r[0];existing=self.plane.topic_route(sid,cid)
            if existing:return 'BOUND'
            if self.db.execute('SELECT 1 FROM tg_topic_routes WHERE session_id=? AND max_chat_id=?',(sid,cid)).fetchone():return 'BLOCKED'
            old=self.db.execute('SELECT state FROM topic_creation WHERE session_id=? AND max_chat_id=?',(sid,cid)).fetchone()
            if old:return old[0]
            self.db.execute("INSERT INTO topic_creation VALUES(?,?,?,?,'SENDING',NULL,?)",(sid,cid,forum,title,time.time()))
            state='UNKNOWN';thread=None
            try:
                def still_allowed():return self.plane.route_group(forum,owner)==sid and (dispatch_allowed is None or dispatch_allowed(sid)) and self.plane.topic_route(sid,cid) is None
                result=await bot.create_forum_topic(chat_id=int(forum),name=title,**({'before':still_allowed} if isinstance(bot,BudgetedBot) else {}))
                thread=getattr(result,'message_thread_id',None)
                if type(thread) is not int or not 1<thread<2**31:thread=None;return state
                if self.plane.route_group(forum,owner)!=sid:state='BLOCKED';return state
                self.plane.bind_topic(sid,cid,thread,owner);state='BOUND';return state
            except ApiDeferred:
                self.db.execute("DELETE FROM topic_creation WHERE session_id=? AND max_chat_id=? AND state='SENDING'",(sid,cid));return 'DEFERRED'
            except asyncio.CancelledError:raise
            except Exception:return state
            finally:
                self.db.execute("UPDATE topic_creation SET state=?,thread_id=?,updated=? WHERE session_id=? AND max_chat_id=? AND state='SENDING'",(state,thread,time.time(),sid,cid))
