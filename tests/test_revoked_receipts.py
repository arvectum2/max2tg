import hashlib,json,sqlite3
from contextlib import closing
import pytest
from app.capsule_registry import CapsuleRegistry
from app.capsule_egress_outbox import CapsuleEgressOutbox
from app.shared_onboarding import SharedOnboarding
from app.telegram_egress import TelegramEgress
from app.runtime_lock import RuntimeLock
from app.revoked_receipts import reconcile
from tests.test_shared_onboarding import bot


def rows(path,table):
    with closing(sqlite3.connect(path)) as d:return d.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall()


async def setup(tmp):
    reg=CapsuleRegistry(tmp/'registry');o=SharedOnboarding(reg);req='a'*24;actor=101;forum=-100001;b=bot(actor,forum);sid=o.reserve(req,actor,forum);await o.verify_forum(req,b);o.confirm_provisioned(req,check=lambda *_:True);await o.activate(req,b,check=lambda *_:True);o.plane.bind_topic(sid,42,77,actor)
    state=tmp/'state';state.mkdir(mode=0o700);acl=state/'worker-acl.json';acl.write_text(json.dumps({'sid':sid,'actor':actor,'forum':forum}));acl.chmod(0o600)
    for path in (reg.root/'shared-gateway.lock',state/'worker.lock'):path.touch(mode=0o600)
    q=CapsuleEgressOutbox(state/'to-telegram.sqlite3',sid);e=TelegramEgress(reg.root/'telegram-egress.sqlite3',o.plane,topic_scoped=True)
    keys={}
    for name,status in [('sent','SENT'),('blocked','BLOCKED'),('unknown','UNKNOWN'),('missing',None),('pending','QUEUED'),('old','SENT'),('unattempted',None)]:
        key='max:'+hashlib.sha256(json.dumps(['42',name],separators=(',',':')).encode()).hexdigest();keys[name]=key
        q.stage(key,'msg-'+name,max_chat_id=42,max_message_id=name)
        if name!='unattempted':q.db.execute("UPDATE worker_telegram_intents SET status=?,submission_attempted=1 WHERE source_key=?",('UNKNOWN' if name=='old' else 'ACCEPTED',key))
        if status:
            e.enqueue(sid,key,'msg-'+name,max_chat_id=42);e.db.execute('UPDATE telegram_egress SET status=?,telegram_id=? WHERE session_id=? AND source_key=?',(status,123 if status=='SENT' else None,sid,key))
    o.revoke(req);q.close();e.close();reg.close();return reg.root,state,sid,keys


@pytest.mark.asyncio
async def test_preview_apply_idempotent_and_central_unchanged(tmp_path):
    r,s,sid,k=await setup(tmp_path);central=r/'telegram-egress.sqlite3';local=s/'to-telegram.sqlite3';before=rows(local,'worker_telegram_intents');central_before=rows(central,'telegram_egress');reg_before=rows(r/'capsules.sqlite3','shared_onboarding')
    assert reconcile(r,s,sid,provisioning_stopped=True)=={'SENT':1,'BLOCKED':1,'UNKNOWN':1,'unresolved':2,'applied':False}
    assert rows(local,'worker_telegram_intents')==before
    assert reconcile(r,s,sid,provisioning_stopped=True,apply=True)=={'SENT':1,'BLOCKED':1,'UNKNOWN':1,'unresolved':2,'applied':True}
    assert rows(central,'telegram_egress')==central_before and rows(r/'capsules.sqlite3','shared_onboarding')==reg_before
    with closing(sqlite3.connect(local)) as db:
        assert dict(db.execute('SELECT source_key,status FROM worker_telegram_intents'))=={k['sent']:'SENT',k['blocked']:'BLOCKED',k['unknown']:'UNKNOWN',k['missing']:'ACCEPTED',k['pending']:'ACCEPTED',k['old']:'UNKNOWN',k['unattempted']:'PENDING'}
        assert db.execute('SELECT telegram_id,receipt_seq FROM worker_telegram_intents WHERE source_key=?',(k['sent'],)).fetchone()==(123,1)
    assert reconcile(r,s,sid,provisioning_stopped=True,apply=True)=={'SENT':0,'BLOCKED':0,'UNKNOWN':0,'unresolved':2,'applied':True}


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['text','route','fingerprint','invalid_id','acl','active'])
async def test_refuse_conflicts_without_partial_updates(tmp_path,change):
    r,s,sid,k=await setup(tmp_path);local=s/'to-telegram.sqlite3';before=rows(local,'worker_telegram_intents')
    if change=='acl':
        (s/'worker-acl.json').write_text(json.dumps({'sid':'b'*24,'actor':101,'forum':-100001}))
    elif change=='active':
        with sqlite3.connect(r/'capsules.sqlite3') as d:d.execute("UPDATE capsules SET desired_active=1 WHERE session_id=?",(sid,))
    else:
        changes={'text':('text','different'),'route':('thread_id',78),'fingerprint':('fingerprint','0'*64),'invalid_id':('telegram_id',0)};column,value=changes[change]
        with sqlite3.connect(r/'telegram-egress.sqlite3') as d:d.execute('UPDATE telegram_egress SET '+column+'=? WHERE session_id=? AND source_key=?',(value,sid,k['sent']))
    with pytest.raises((ValueError,PermissionError)):reconcile(r,s,sid,provisioning_stopped=True,apply=True)
    assert rows(local,'worker_telegram_intents')==before


@pytest.mark.asyncio
async def test_refuse_running_or_unconfirmed_quiescence(tmp_path):
    r,s,sid,k=await setup(tmp_path)
    with pytest.raises(PermissionError):reconcile(r,s,sid,apply=True)
    for lock_path in (r/'shared-gateway.lock',r/'shared-provisioning.lock',s/'worker.lock'):
        lock=RuntimeLock(lock_path)
        try:
            with pytest.raises(BlockingIOError):reconcile(r,s,sid,provisioning_stopped=True,apply=True)
        finally:lock.close()
