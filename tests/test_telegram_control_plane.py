"""Offline tests for P2.2 shared Telegram routing."""
from app.capsule_registry import CapsuleRegistry
from app.telegram_control_plane import TelegramControlPlane
from app.telegram_inbox import TelegramInbox,WorkerUnavailable,identity

def test_new_account_has_no_telegram_route(tmp_path):
    registry=CapsuleRegistry(tmp_path/'private')
    plane=TelegramControlPlane(registry)
    uid=registry.create_account()
    assert plane.owner(12345) is None
    assert plane.route_group(-100001,12345) is None
    registry.close()

import pytest

def test_event_identity_comes_from_telegram_metadata():
    assert identity({'message':{'chat':{'id':-1001},'from':{'id':123}}})==(-1001,123)
    assert identity({'message':{'chat':{'id':-1001},'text':'hello'}})==(-1001,None)
    assert identity({'callback_query':{'from':{'id':123},'message':{'chat':{'id':-1001}}}})==(-1001,123)

def routing_fixture(tmp_path):
    registry=CapsuleRegistry(tmp_path/'store')
    plane=TelegramControlPlane(registry)
    sessions=[]
    for actor,group in ((111,-100001),(222,-100002)):
        user=registry.create_account()
        sid=registry.create_capsule(user)
        registry.db.execute('INSERT INTO tg_owners VALUES(?,?)',(str(actor),user))
        registry.db.execute('UPDATE capsules SET desired_active=1 WHERE session_id=?',(sid,))
        registry.db.execute('INSERT INTO tg_routes VALUES(?,?,?,?,?)',
                            (str(group),sid,user,'ACTIVE',1.0))
        sessions.append(sid)
    return registry,plane,sessions


def update(number,group,actor):
    return dict(update_id=number,message={'chat':{'id':group},'from':{'id':actor},'text':'hello'})

def test_group_routing_uses_real_actor_and_forum(tmp_path):
    reg,plane,(first,second)=routing_fixture(tmp_path)
    assert plane.route_group(-100001,111)==first
    assert plane.route_group(-100002,222)==second
    assert plane.route_group(-100001,222) is None
    assert plane.route_group(-100002,111) is None
    assert plane.route_group(-100001,None) is None
    reg.close()


def test_inbox_dedup_and_actor_acl(tmp_path):
    reg,plane,(first,second)=routing_fixture(tmp_path)
    inbox=TelegramInbox(reg.root/'updates.sqlite3',plane)
    assert inbox.ingest(update(15,-100001,111))==(15,first)
    assert inbox.ingest(update(15,-100001,111))==(15,first)
    assert inbox.ingest(update(16,-100001,222)) is None
    assert inbox.ready()==[15]
    assert inbox.counts()=={'RECEIVED':1}
    inbox.close();reg.close()

async def test_queued_updates_go_to_correct_worker(tmp_path):
    reg,plane,(first,second)=routing_fixture(tmp_path)
    inbox=TelegramInbox(reg.root/'updates.sqlite3',plane)
    inbox.ingest(update(17,-100001,111))
    inbox.ingest(update(18,-100002,222))
    delivered=[]
    async def receiver(session,payload):
        delivered.append((session,payload['update_id']))
        return True
    assert await inbox.dispatch(17,receiver)=='ACKED'
    assert await inbox.dispatch(18,receiver)=='ACKED'
    assert await inbox.dispatch(17,receiver)=='ACKED'
    assert delivered==[(first,17),(second,18)]
    inbox.close();reg.close()

async def test_revoked_group_blocks_even_previously_saved_update(tmp_path):
    reg,plane,(first,second)=routing_fixture(tmp_path)
    inbox=TelegramInbox(reg.root/'updates.sqlite3',plane)
    inbox.ingest(update(19,-100001,111))
    plane.disable_forum(-100001)
    async def must_not_call(*args):
        raise AssertionError('unauthorized route')
    assert await inbox.dispatch(19,must_not_call)=='BLOCKED'
    inbox.close();reg.close()

async def test_uncertain_dispatch_not_replayed(tmp_path):
    reg,plane,_=routing_fixture(tmp_path)
    ledger=TelegramInbox(reg.root/'updates.sqlite3',plane)
    ledger.ingest(update(20,-100001,111))
    async def ambiguous(*args):
        raise TimeoutError('outcome unknown')
    assert await ledger.dispatch(20,ambiguous)=='UNKNOWN'
    ledger.close()
    restored=TelegramInbox(reg.root/'updates.sqlite3',plane)
    assert restored.ready()==[]
    restored.close();reg.close()

async def test_definite_worker_offline_safely_retryable(tmp_path):
    reg,plane,_=routing_fixture(tmp_path)
    ledger=TelegramInbox(reg.root/'updates.sqlite3',plane)
    ledger.ingest(update(22,-100001,111))
    async def unavailable(*args):
        raise WorkerUnavailable()
    assert await ledger.dispatch(22,unavailable)=='RECEIVED'
    assert ledger.ready()==[22]
    ledger.close();reg.close()

from app.telegram_ipc import CapsuleIpcClient, CapsuleIpcServer

from app.telegram_gateway import TelegramGatewayEngine

async def test_gateway_engine_ingress_to_correct_session(tmp_path):
    reg,plane,(first,second)=routing_fixture(tmp_path)
    ledger=TelegramInbox(reg.root/'updates.sqlite3',plane)
    output=[]
    async def local_handler(session,event):
        output.append((session,event['update_id']))
        return True
    engine=TelegramGatewayEngine(plane,ledger,local_handler)
    assert await engine.accept(update(51,-100001,111))=='ACKED'
    assert await engine.accept(update(52,-100002,222))=='ACKED'
    assert await engine.accept(update(53,-100001,222))=='DENIED'
    assert output==[(first,51),(second,52)]
    ledger.close();reg.close()

def test_modified_same_update_id_cannot_be_replayed(tmp_path):
    reg,plane,_=routing_fixture(tmp_path)
    inbox=TelegramInbox(reg.root/'inbox.sqlite3',plane)
    assert inbox.ingest(update(71,-100001,111)) is not None
    altered=update(71,-100001,111)
    altered['message']['text']='different content'
    with pytest.raises(ValueError):
        inbox.ingest(altered)
    inbox.close();reg.close()

def test_crashed_inflight_is_unknown_after_reopen(tmp_path):
    reg,plane,_=routing_fixture(tmp_path)
    file=reg.root/'inbox.sqlite3'
    ledger=TelegramInbox(file,plane)
    ledger.ingest(update(72,-100001,111))
    ledger.db.execute('UPDATE telegram_ingress SET status=? WHERE update_id=?',('INFLIGHT',72))
    ledger.close()
    again=TelegramInbox(file,plane)
    assert again.ready()==[]
    assert again.row(72)['status']=='UNKNOWN'
    again.close();reg.close()

def test_single_gateway_application_can_be_built_offline(tmp_path):
    from app.telegram_gateway import build_telegram_application
    reg,plane,_=routing_fixture(tmp_path)
    inbox=TelegramInbox(reg.root/'inbox.sqlite3',plane)
    engine=TelegramGatewayEngine(plane,inbox)
    app=build_telegram_application('123456:synthetic-test-token',engine)
    assert app.running is False
    assert app.updater is not None
    assert len(app.handlers)==1
    inbox.close();reg.close()
