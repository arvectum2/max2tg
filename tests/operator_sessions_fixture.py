"""Offline synthetic operator actions, never authenticate a real account."""
import asyncio,json,sys,sqlite3
from contextlib import closing
from types import SimpleNamespace as NS
from app.capsule_registry import CapsuleRegistry
from app.operator_sessions import OperatorSessions
from unittest.mock import AsyncMock

def bot(actor,forum):
    return NS(get_me=AsyncMock(return_value=NS(id=900)),get_chat=AsyncMock(return_value=NS(id=forum,type="supergroup",is_forum=True,username=None,active_usernames=[],linked_chat_id=None)),get_chat_member=AsyncMock(side_effect=lambda chat,user:NS(user=NS(id=user),status="creator" if user==actor else "administrator",can_manage_topics=True)),get_chat_member_count=AsyncMock(return_value=2))
from tests.supervised_proxy_fixture import assert_sandbox


def receipts():
    out=[]
    for path,table in (('/registry/inbox.sqlite3','telegram_ingress'),('/registry/telegram-egress.sqlite3','telegram_egress'),('/state/to-telegram.sqlite3','worker_telegram_intents')):
        with closing(sqlite3.connect('file:'+path+'?mode=ro',uri=True)) as db:out.append(sorted(db.execute('SELECT * FROM '+table).fetchall(),key=repr))
    return out

async def main():
    assert_sandbox();action,sid=sys.argv[1:];r=CapsuleRegistry('/registry');o=OperatorSessions(r,provisioning_stopped=True)
    try:
        row=r.db.execute('SELECT request_id,actor,forum,bot_id FROM shared_onboarding WHERE sid=?',(sid,)).fetchone();req,actor,forum,bid=row;actor=int(actor);forum=int(forum);assert actor==100 and forum==-10000 and bid=='900'
        args=('/state','/channel','/secret','/keys/worker.key');before=receipts();b=bot(actor,forum);b.get_me.return_value=NS(id=900)
        if action=='pause':
            assert o.status(req)['state']=='ACTIVE';assert o.pause(req,*args)['state']=='PAUSED';assert o.onboarding.plane.route_group(forum,actor) is None
        elif action=='resume':
            assert o.status(req)['state']=='PAUSED';b.get_chat_member_count.return_value=3
            try:await o.activate(req,b,*args)
            except PermissionError:pass
            else:raise AssertionError('Invalid forum resumed')
            assert o.status(req)['state']=='PAUSED';b.get_chat_member_count.return_value=2;assert (await o.activate(req,b,*args))['state']=='ACTIVE';assert o.onboarding.plane.route_group(forum,actor)==sid
        elif action=='revoke':
            assert o.status(req)['state']=='ACTIVE';assert o.revoke(req,*args)['state']=='REVOKED'
            try:await o.activate(req,b,*args)
            except PermissionError:pass
            else:raise AssertionError('Revoked session resumed')
            assert o.onboarding.plane.route_group(forum,actor) is None
        else:raise ValueError('Unknown action')
        assert receipts()==before
        assert r.db.execute('SELECT desired_active FROM capsules WHERE session_id<>?',(sid,)).fetchone()[0]==1
        print(json.dumps({'state':o.status(req)['state'],'receipts_unchanged':True,'other_active':True}))
    finally:o.close();r.close()

if __name__=='__main__':asyncio.run(main())
