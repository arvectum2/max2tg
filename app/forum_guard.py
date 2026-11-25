"""Short-lived positive membership evidence for the offline managed gateway.

No persisted positive lease survives restart. Explicit violations revoke;
transport uncertainty holds, never turns an uncertain send into a retry.
"""
import asyncio,time
from app.shared_onboarding import SharedOnboarding,inspect_forum,ForumPolicyViolation

class ForumGuard:
    def __init__(self,registry,bot,*,ttl=30,clock=time.monotonic):
        if not 1<=ttl<=60:raise ValueError('Invalid forum check interval')
        self.onboarding=SharedOnboarding(registry);self.bot=bot;self.ttl=ttl;self.clock=clock
        self.valid={};self.next_check={};self.generation={};self.status={};self.lock=asyncio.Lock()

    def allowed(self,sid):return self.clock()<self.valid.get(sid,-1)

    def invalidate(self,forum):
        if type(forum) is not int:return
        for r in self.onboarding.db.execute('SELECT sid FROM shared_onboarding WHERE forum=?',(str(forum),)):
            sid=r[0];self.valid.pop(sid,None);self.next_check.pop(sid,None);self.generation[sid]=self.generation.get(sid,0)+1;self.status[sid]='RECHECK'

    def observe(self,update):
        # Only trusted Telegram polling metadata, never message text.
        for kind in ('chat_member','my_chat_member','message'):
            event=update.get(kind)
            if not isinstance(event,dict) or not isinstance(event.get('chat'),dict):continue
            forum=event['chat'].get('id')
            if type(forum) is not int:continue
            if kind=='message' and not any(k in event for k in ('new_chat_members','left_chat_member','migrate_to_chat_id','migrate_from_chat_id')):continue
            self.invalidate(forum)
            row=self.onboarding.db.execute("SELECT * FROM shared_onboarding WHERE forum=? AND stage='ACTIVE'",(str(forum),)).fetchone()
            if not row:continue
            actor=int(row['actor']);bot=int(row['bot_id']);violation=False
            if kind=='message':
                joins=event.get('new_chat_members') or []
                violation=any(isinstance(m,dict) and type(m.get('id')) is int and m['id'] not in (actor,bot) for m in joins)
                left=event.get('left_chat_member') or {}
                violation=violation or (isinstance(left,dict) and left.get('id') in (actor,bot)) or any(k in event for k in ('migrate_to_chat_id','migrate_from_chat_id'))
            else:
                member=event.get('new_chat_member') or {};user=member.get('user') or {}
                uid=user.get('id');status=member.get('status')
                if type(uid) is int and status in ('creator','administrator','member','restricted','left','kicked'):
                    if uid==actor:violation=status!='creator'
                    elif uid==bot:violation=status!='administrator' or member.get('can_manage_topics') is not True
                    else:violation=status in ('creator','administrator','member') or status=='restricted' and member.get('is_member') is True
            if violation:
                self.onboarding.revoke(row['request_id']);self.status[row['sid']]='REVOKED'

    def held_sessions(self):
        return {r[0] for r in self.onboarding.db.execute("SELECT o.sid FROM shared_onboarding o JOIN capsules c ON c.session_id=o.sid JOIN tg_routes r ON r.session_id=o.sid WHERE o.stage='ACTIVE' AND c.desired_active=1 AND r.status='ACTIVE'") if not self.allowed(r[0])}

    async def refresh(self,active):
        async with self.lock:
            for sid in set(self.valid)-set(active):self.valid.pop(sid,None)
            rows=[dict(r) for r in self.onboarding.db.execute("SELECT * FROM shared_onboarding WHERE stage='ACTIVE'") if r['sid'] in active]
            for row in rows:
                sid=row['sid'];now=self.clock()
                if now<self.next_check.get(sid,-1):continue
                gen=self.generation.get(sid,0);self.valid.pop(sid,None)
                try:
                    bid=await asyncio.wait_for(inspect_forum(row,self.bot),timeout=8)
                    if str(bid)!=row['bot_id']:raise ForumPolicyViolation('Bot identity changed')
                except ForumPolicyViolation:
                    self.onboarding.revoke(row['request_id']);self.status[sid]='REVOKED';self.next_check.pop(sid,None);continue
                except (OSError,TimeoutError):
                    self.status[sid]='UNAVAILABLE';self.next_check[sid]=self.clock()+5;continue
                except Exception:
                    # API-specific failures (including forbidden/429) lack a
                    # complete membership snapshot; hold instead of guessing.
                    self.status[sid]='UNAVAILABLE';self.next_check[sid]=self.clock()+5;continue
                if self.generation.get(sid,0)!=gen or self.onboarding.row(row['request_id'])['stage']!='ACTIVE':continue
                # Age measured from start, not completion of the multi-call read.
                self.valid[sid]=now+self.ttl;self.next_check[sid]=now+self.ttl;self.status[sid]='VERIFIED'
