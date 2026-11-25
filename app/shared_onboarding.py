"""Trusted, resumable onboarding metadata. No MAX credentials at the gateway.

Caller is an authenticated provisioning operator, not an exposed HTTP/bot
handler. Forum checks are snapshots; continuous ACL checks remain necessary.
"""
from __future__ import annotations
import asyncio,time
from app.telegram_control_plane import TelegramControlPlane
from app.telegram_ipc import ensure_ipc_key

class SharedOnboarding:
    def __init__(self,registry):
        self.registry=registry;self.plane=TelegramControlPlane(registry);self.db=registry.db
        self._lock=asyncio.Lock()
        self.db.execute('''CREATE TABLE IF NOT EXISTS shared_onboarding(
          request_id TEXT PRIMARY KEY, actor TEXT NOT NULL UNIQUE, forum TEXT NOT NULL UNIQUE,
          sid TEXT NOT NULL UNIQUE REFERENCES capsules(session_id),
          stage TEXT NOT NULL CHECK(stage IN('RESERVED','FORUM_VERIFIED','PROVISIONED','ACTIVE','REVOKED')),
          updated REAL NOT NULL,bot_id TEXT)''')

    def row(self,request_id):
        self.registry._check_id(request_id)
        r=self.db.execute('SELECT * FROM shared_onboarding WHERE request_id=?',(request_id,)).fetchone()
        if not r:raise KeyError('Unknown onboarding request')
        return dict(r)

    def reserve(self,request_id,actor,forum):
        """Bind an already authenticated actor to one new capsule atomically."""
        self.registry._check_id(request_id)
        if type(actor) is not int or not 0<actor<2**63 or type(forum) is not int or not -(2**63)<=forum<0:raise ValueError('Invalid identity')
        db=self.db;db.execute('BEGIN IMMEDIATE')
        try:
            existing=db.execute('SELECT * FROM shared_onboarding WHERE request_id=?',(request_id,)).fetchone()
            if existing:
                if (existing['actor'],existing['forum'])!=(str(actor),str(forum)):raise PermissionError('Onboarding identity conflict')
                sid=existing['sid']
            else:
                if self.plane.owner(actor) is not None:raise PermissionError('Existing owner requires separate flow')
                if db.execute('SELECT 1 FROM capsules WHERE tg_chat_id=? UNION SELECT 1 FROM tg_routes WHERE tg_chat_id=?',(str(forum),str(forum))).fetchone():raise PermissionError('Forum already assigned')
                owner=self.registry.create_account();sid=self.registry.create_capsule(owner)
                self.registry.configure_external_shared(sid,forum)
                db.execute('INSERT INTO tg_owners VALUES(?,?)',(str(actor),owner))
                self.plane.bind_forum(sid,forum,actor)
                db.execute("INSERT INTO shared_onboarding(request_id,actor,forum,sid,stage,updated) VALUES(?,?,?,?,'RESERVED',?)",(request_id,str(actor),str(forum),sid,time.time()))
            db.execute('COMMIT');return sid
        except BaseException:db.execute('ROLLBACK');raise

    async def verify_forum(self,request_id,bot):
        """Use injected trusted Bot API reads; never create a group or invite."""
        async with self._lock:
            row=self.row(request_id)
            if row['stage']=='REVOKED':raise PermissionError('Revoked onboarding')
            gid=int(row['forum']);actor=int(row['actor']);me=await bot.get_me();bid=getattr(me,'id',None)
            if type(bid) is not int or bid<=0 or bid==actor:raise PermissionError('Invalid bot identity')
            chat=await bot.get_chat(gid);owner=await bot.get_chat_member(gid,actor);admin=await bot.get_chat_member(gid,bid);count=await bot.get_chat_member_count(gid)
            valid=(getattr(chat,'id',None)==gid and getattr(chat,'type',None)=='supergroup' and getattr(chat,'is_forum',None) is True and not getattr(chat,'username',None) and not getattr(chat,'active_usernames',None) and not getattr(chat,'linked_chat_id',None) and getattr(getattr(owner,'user',None),'id',None)==actor and getattr(owner,'status',None)=='creator' and getattr(getattr(admin,'user',None),'id',None)==bid and getattr(admin,'status',None)=='administrator' and getattr(admin,'can_manage_topics',None) is True and type(count) is int and count==2)
            if not valid:raise PermissionError('Forum verification failed')
            db=self.db;db.execute('BEGIN IMMEDIATE')
            try:
                fresh=self.row(request_id)
                if fresh['stage']=='REVOKED':raise PermissionError('Revoked onboarding')
                if fresh['bot_id'] not in (None,str(bid)):raise PermissionError('Bot identity changed')
                route=db.execute('SELECT session_id,status FROM tg_routes WHERE tg_chat_id=?',(row['forum'],)).fetchone()
                if not route or route['session_id']!=row['sid'] or route['status']=='DISABLED':raise PermissionError('Route disabled or changed')
                if route['status']=='PENDING':self.plane.confirm_forum(gid,is_private_forum=True,owner_is_member=True,bot_is_admin=True)
                if fresh['stage']=='RESERVED':db.execute("UPDATE shared_onboarding SET stage='FORUM_VERIFIED',updated=?,bot_id=? WHERE request_id=?",(time.time(),str(bid),request_id))
                db.execute('COMMIT')
            except BaseException:db.execute('ROLLBACK');raise
            return self.row(request_id)['stage']

    def ipc_key(self,request_id):
        row=self.row(request_id)
        if row['stage'] not in ('FORUM_VERIFIED','PROVISIONED','ACTIVE'):raise PermissionError('Forum must be verified')
        return ensure_ipc_key(self.registry,row['sid'])

    def confirm_provisioned(self,request_id,*,check):
        """Trusted local provisioner checks exact sid/ACL/volumes/key/secret.

        No secret values pass through this API. This is not MAX auth proof.
        """
        row=self.row(request_id)
        if row['stage'] not in ('FORUM_VERIFIED','PROVISIONED'):raise PermissionError('Forum must be verified')
        if check(row['sid'],int(row['actor']),int(row['forum']),self.ipc_key(request_id)) is not True:raise PermissionError('Provisioning evidence failed')
        result=self.db.execute("UPDATE shared_onboarding SET stage='PROVISIONED',updated=? WHERE request_id=? AND stage IN ('FORUM_VERIFIED','PROVISIONED')",(time.time(),request_id))
        if result.rowcount!=1:raise PermissionError('Provisioning stage changed')

    async def activate(self,request_id,bot,*,check):
        # Fresh forum verification and local evidence required; no launch here.
        if self.row(request_id)['stage'] not in ('PROVISIONED','ACTIVE'):raise PermissionError('Not provisioned')
        await self.verify_forum(request_id,bot)
        row=self.row(request_id)
        if check(row['sid'],int(row['actor']),int(row['forum']),self.ipc_key(request_id)) is not True:raise PermissionError('Provisioning evidence failed')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.row(request_id)
            route=self.db.execute('SELECT status FROM tg_routes WHERE session_id=?',(row['sid'],)).fetchone()
            if row['stage'] not in ('PROVISIONED','ACTIVE') or not route or route[0]!='ACTIVE':raise PermissionError('Activation gate changed')
            self.registry.set_active(row['sid'],True)
            self.db.execute("UPDATE shared_onboarding SET stage='ACTIVE',updated=? WHERE request_id=?",(time.time(),request_id));self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
        return row['sid']

    def revoke(self,request_id):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.row(request_id);self.registry.set_active(row['sid'],False);self.plane.disable_forum(row['forum'])
            self.db.execute("UPDATE shared_onboarding SET stage='REVOKED',updated=? WHERE request_id=?",(time.time(),request_id));self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
