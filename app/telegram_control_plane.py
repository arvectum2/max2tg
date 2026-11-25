"""P2.2 shared Telegram-bot routing registry, isolated from MAX credentials.

Only an operator-approved owner can bind their Telegram identity and a
separate forum supergroup to a capsule. Telegram's `from_user` and `chat`
metadata are used for routing, never callback-data or message text.
"""
from __future__ import annotations

import hashlib
import secrets
import time

from app.capsule_registry import CapsuleRegistry


class TelegramControlPlane:
    def __init__(self, capsules: CapsuleRegistry):
        self.capsules=capsules
        self.db=capsules.db
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS tg_owners(
            tg_user_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL UNIQUE REFERENCES accounts(user_id)
          );
          CREATE TABLE IF NOT EXISTS tg_claims(
            code_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES accounts(user_id),
            expires_at REAL NOT NULL,
            consumed_at REAL
          );
          CREATE TABLE IF NOT EXISTS tg_routes(
            tg_chat_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE REFERENCES capsules(session_id),
            user_id TEXT NOT NULL REFERENCES accounts(user_id),
            status TEXT NOT NULL DEFAULT 'PENDING',
            updated_at REAL NOT NULL,
            CHECK(status IN ('PENDING','ACTIVE','DISABLED'))
          );
          CREATE INDEX IF NOT EXISTS tg_routes_owner ON tg_routes(user_id);
          CREATE TABLE IF NOT EXISTS tg_topic_routes(
            session_id TEXT NOT NULL REFERENCES capsules(session_id),
            max_chat_id TEXT NOT NULL,
            tg_chat_id TEXT NOT NULL REFERENCES tg_routes(tg_chat_id),
            thread_id INTEGER NOT NULL CHECK(thread_id>1),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN(0,1)),
            PRIMARY KEY(session_id,max_chat_id),
            UNIQUE(tg_chat_id,thread_id)
          );
        """)

    @staticmethod
    def _user_id(value: int | str) -> str:
        try:
            number=int(value)
        except (ValueError,TypeError,OverflowError) as exc:
            raise ValueError("Invalid Telegram user ID") from exc
        if number<=0 or str(number)!=str(value):
            raise ValueError("Invalid Telegram user ID")
        return str(number)

    @staticmethod
    def _chat_id(value: int | str) -> str:
        try:
            number=int(value)
        except (ValueError,TypeError,OverflowError) as exc:
            raise ValueError("Invalid Telegram chat ID") from exc
        if number>=0 or str(number)!=str(value):
            raise ValueError("Expected negative private forum group ID")
        return str(number)

    def generate_claim(self,user_id:str,valid_for:int=600) -> str:
        if not 60<=valid_for<=3600:
            raise ValueError("Claim expiry must be 60..3600 seconds")
        self.capsules._check_id(user_id)
        if not self.db.execute("SELECT 1 FROM accounts WHERE user_id=?",
                               (user_id,)).fetchone():
            raise KeyError("Unknown account")
        code=secrets.token_urlsafe(32)
        digest=hashlib.sha256(code.encode()).hexdigest()
        self.db.execute(
            "INSERT INTO tg_claims(code_hash,user_id,expires_at) VALUES(?,?,?)",
            (digest,user_id,time.time()+valid_for))
        return code  # Never write raw claims to logs, database or disk.

    def claim_owner(self,telegram_user:int,code:str) -> str:
        uid=self._user_id(telegram_user)
        if not isinstance(code,str) or len(code)>256:
            raise ValueError("Invalid claim")
        digest=hashlib.sha256(code.encode()).hexdigest()
        row=self.db.execute(
            "SELECT user_id,expires_at,consumed_at FROM tg_claims WHERE code_hash=?",
            (digest,)).fetchone()
        if row is None or row["consumed_at"] is not None or row["expires_at"]<time.time():
            raise ValueError("Claim expired or invalid")
        with self.db:
            already=self.db.execute(
                "SELECT user_id FROM tg_owners WHERE tg_user_id=?",(uid,)
            ).fetchone()
            if already and already["user_id"]!=row["user_id"]:
                raise PermissionError("Telegram identity belongs to another account")
            other=self.db.execute(
                "SELECT tg_user_id FROM tg_owners WHERE user_id=?",
                (row["user_id"],)).fetchone()
            if other and other["tg_user_id"]!=uid:
                raise PermissionError("Account already has a Telegram owner")
            self.db.execute(
                "INSERT OR IGNORE INTO tg_owners(tg_user_id,user_id) VALUES(?,?)",
                (uid,row["user_id"]))
            self.db.execute(
                "UPDATE tg_claims SET consumed_at=? WHERE code_hash=? AND consumed_at IS NULL",
                (time.time(),digest))
        return row["user_id"]

    def owner(self,telegram_user:int | str) -> str | None:
        uid=self._user_id(telegram_user)
        row=self.db.execute(
            "SELECT user_id FROM tg_owners WHERE tg_user_id=?",(uid,)
        ).fetchone()
        return row["user_id"] if row else None

    def bind_forum(self,session_id:str,group_id:int|str,telegram_user:int) -> None:
        sid=self.capsules._check_id(session_id)
        gid=self._chat_id(group_id)
        entry=self.capsules.get(sid)
        owner=self.owner(telegram_user)
        if owner is None or owner!=entry["user_id"]:
            raise PermissionError("Only the bound owner may attach a forum")
        # Cannot route a capsule bound to an incompatible existing forum.
        if entry["tg_chat_id"] and entry["tg_chat_id"]!=gid:
            raise ValueError("Capsule uses a different configured Telegram forum")
        current=self.db.execute(
            "SELECT session_id FROM tg_routes WHERE tg_chat_id=?",(gid,)
        ).fetchone()
        if current and current["session_id"]!=sid:
            raise ValueError("Telegram forum is already assigned")
        self.db.execute(
            "INSERT INTO tg_routes(tg_chat_id,session_id,user_id,status,updated_at) "
            "VALUES(?,?,?,'PENDING',?) ON CONFLICT(tg_chat_id) DO UPDATE "
            "SET status='PENDING',updated_at=excluded.updated_at",
            (gid,sid,owner,time.time()))

    def confirm_forum(self,group_id:int|str,*,is_private_forum:bool,
                      owner_is_member:bool,bot_is_admin:bool) -> None:
        """Call only after getChat/getChatMember verification by a trusted bot."""
        gid=self._chat_id(group_id)
        if not (is_private_forum and owner_is_member and bot_is_admin):
            raise PermissionError("Telegram forum membership not verified")
        cursor=self.db.execute(
            "UPDATE tg_routes SET status='ACTIVE',updated_at=? "
            "WHERE tg_chat_id=? AND status='PENDING'",(time.time(),gid))
        if cursor.rowcount!=1:
            raise ValueError("No pending forum awaiting verification")

    def disable_forum(self,group_id:int|str) -> None:
        self.db.execute(
            "UPDATE tg_routes SET status='DISABLED',updated_at=? WHERE tg_chat_id=?",
            (time.time(),self._chat_id(group_id)))

    def route_group(self,group_id:int|str,actor_id:int|str) -> str | None:
        """Fail closed on absent/anonymous actors, unknown chats, and wrong owner."""
        try:
            gid=self._chat_id(group_id)
            actor=self._user_id(actor_id)
        except ValueError:
            return None
        row=self.db.execute(
            "SELECT r.session_id FROM tg_routes r "
            "JOIN tg_owners o ON o.user_id=r.user_id "
            "JOIN capsules c ON c.session_id=r.session_id "
            "WHERE r.tg_chat_id=? AND o.tg_user_id=? "
            "AND r.status='ACTIVE' AND c.desired_active=1",
            (gid,actor)).fetchone()
        return row["session_id"] if row else None

    @staticmethod
    def max_chat_key(value):
        if type(value) not in (str,int):raise ValueError('Invalid MAX chat ID')
        try:number=int(value)
        except (ValueError,TypeError):raise ValueError('Invalid MAX chat ID') from None
        if not -(2**63)<=number<2**63 or number==0 or str(number)!=str(value):raise ValueError('Invalid MAX chat ID')
        return str(number)

    def bind_topic(self,sid,max_chat_id,thread_id,telegram_user):
        """Trusted operator only, after verifying the topic belongs to the forum.

        Never take a thread ID from a worker. No automatic topic reuse/remap.
        """
        self.capsules._check_id(sid);cid=self.max_chat_key(max_chat_id)
        if type(thread_id) is not int or not 1<thread_id<2**31:raise ValueError('Invalid forum topic')
        row=self.db.execute("SELECT tg_chat_id FROM tg_routes WHERE session_id=? AND status='ACTIVE'",(sid,)).fetchone()
        if row is None or self.route_group(row['tg_chat_id'],telegram_user)!=sid:raise PermissionError('No active owned forum')
        previous=self.db.execute('SELECT * FROM tg_topic_routes WHERE session_id=? AND max_chat_id=?',(sid,cid)).fetchone()
        if previous:
            if previous['tg_chat_id']!=row['tg_chat_id'] or previous['thread_id']!=thread_id or not previous['active']:raise ValueError('Existing topic binding cannot be reused')
            return
        self.db.execute('INSERT INTO tg_topic_routes(session_id,max_chat_id,tg_chat_id,thread_id) VALUES(?,?,?,?)',(sid,cid,row['tg_chat_id'],thread_id))

    def topic_route(self,sid,max_chat_id):
        self.capsules._check_id(sid);cid=self.max_chat_key(max_chat_id)
        r=self.db.execute("SELECT t.tg_chat_id,t.thread_id FROM tg_topic_routes t JOIN tg_routes r ON r.tg_chat_id=t.tg_chat_id AND r.session_id=t.session_id JOIN capsules c ON c.session_id=r.session_id AND c.user_id=r.user_id JOIN tg_owners o ON o.user_id=r.user_id WHERE t.session_id=? AND t.max_chat_id=? AND t.active=1 AND r.status='ACTIVE' AND c.desired_active=1",(sid,cid)).fetchone()
        return (r['tg_chat_id'],r['thread_id']) if r else None

    def max_chat_for_thread(self,sid,thread_id):
        self.capsules._check_id(sid)
        if type(thread_id) is not int or thread_id<=1:return None
        r=self.db.execute('SELECT max_chat_id FROM tg_topic_routes WHERE session_id=? AND thread_id=? AND active=1',(sid,thread_id)).fetchone()
        if not r:return None
        cid=r['max_chat_id'];route=self.topic_route(sid,cid)
        return cid if route and route[1]==thread_id else None

    def disable_topic(self,sid,max_chat_id):
        self.capsules._check_id(sid);cid=self.max_chat_key(max_chat_id)
        self.db.execute('UPDATE tg_topic_routes SET active=0 WHERE session_id=? AND max_chat_id=?',(sid,cid))

    def accessible_capsules(self,actor_id:int|str) -> list[dict]:
        owner=self.owner(actor_id)
        if not owner:
            return []
        return [self.capsules.get(r["session_id"]) for r in
                self.db.execute("SELECT session_id FROM capsules WHERE user_id=?",
                                (owner,))]
