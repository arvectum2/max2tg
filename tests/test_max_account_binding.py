import pytest
from unittest.mock import AsyncMock
from app.max_account_binding import bind_initial,bound_viewer,replace_bound_credentials
from app.encrypted_credentials import load_encrypted_worker
from app.max_client import MaxClient,OpCode
from app.runtime_lock import RuntimeLock
from tests.test_operator_sessions import layout

SID='a'*24

@pytest.mark.asyncio
async def test_pin_and_same_account_replacement(tmp_path):
    state,channel,secret,key=layout(tmp_path,SID);old=load_encrypted_worker(secret,SID,key);candidate=dict(old,MAX_TOKEN='replacement')
    probe=AsyncMock(return_value=100)
    with pytest.raises(PermissionError):await replace_bound_credentials(state,secret,SID,key,candidate,probe=probe)
    assert await bind_initial(state,secret,SID,key,probe=probe)==100
    assert bound_viewer(secret,SID,key,required=True)==100
    pin=(secret/'account.enc').read_bytes();original=(secret/'max.enc').read_bytes();(state/'topics.json').write_text('{}')
    with pytest.raises(PermissionError):await bind_initial(state,secret,SID,key,probe=probe)
    for wrong in (101,None,True):
        with pytest.raises(PermissionError):await replace_bound_credentials(state,secret,SID,key,candidate,probe=AsyncMock(return_value=wrong))
        assert (secret/'max.enc').read_bytes()==original
    assert await replace_bound_credentials(state,secret,SID,key,candidate,probe=probe)==100
    assert load_encrypted_worker(secret,SID,key)==candidate and (secret/'account.enc').read_bytes()==pin and (state/'topics.json').read_text()=='{}'

@pytest.mark.asyncio
async def test_binding_rejects_existing_history_running_and_tamper(tmp_path):
    state,channel,secret,key=layout(tmp_path,SID);probe=AsyncMock(return_value=100)
    lock=RuntimeLock(state/'worker.lock')
    try:
        with pytest.raises(BlockingIOError):await bind_initial(state,secret,SID,key,probe=probe)
    finally:lock.close()
    (state/'topics.json').touch()
    with pytest.raises(PermissionError):await bind_initial(state,secret,SID,key,probe=probe)
    (state/'topics.json').unlink();await bind_initial(state,secret,SID,key,probe=probe)
    p=secret/'account.enc';data=p.read_bytes();p.write_bytes(data[:-1]+bytes([data[-1]^1]))
    with pytest.raises(ValueError):bound_viewer(secret,SID,key,required=True)

@pytest.mark.asyncio
async def test_pinned_client_rejects_foreign_auth_and_pre_auth_events():
    c=MaxClient('synthetic','device',expected_viewer=100);c._ws=AsyncMock();c._ws.closed=False;c._on_message_cb=AsyncMock();c._on_ready_cb=AsyncMock()
    packet={'opcode':OpCode.DISPATCH,'cmd':0,'payload':{'chatId':42,'message':{'id':123,'sender':999,'text':'synthetic'}}}
    await c._handle(packet);assert c._dispatch_counter==0
    assert await c._send(OpCode.SEND_MESSAGE,{})==-1
    await c._handle({'opcode':OpCode.AUTH_SNAPSHOT,'cmd':1,'payload':{'profile':{'contact':{'id':101}}}})
    assert not c._authorized and c._auth_required and c._my_id is None;c._ws.close.assert_awaited_once();c._on_ready_cb.assert_not_called()
    await c._handle(packet);assert c._dispatch_counter==0
    await c._handle({'opcode':OpCode.AUTH_SNAPSHOT,'cmd':1,'payload':{'profile':{'contact':{'id':100}}}});assert c._authorized and c._my_id==100

@pytest.mark.asyncio
async def test_required_binding_gates_activation_and_runtime(tmp_path):
    from app.capsule_registry import CapsuleRegistry
    from app.operator_sessions import OperatorSessions
    from app.isolated_transport import isolated_max_client
    from tests.test_shared_onboarding import bot,REQ,ACTOR,FORUM
    r=CapsuleRegistry(tmp_path/'registry');o=OperatorSessions(r,provisioning_stopped=True,require_account_binding=True);b=bot()
    try:
        sid=o.reserve(REQ,ACTOR,FORUM)['sid'];args=layout(tmp_path,sid);await o.verify(REQ,b);o.provision(REQ,*args)
        with pytest.raises(PermissionError):await o.activate(REQ,b,*args)
        with pytest.raises(PermissionError):
            async with isolated_max_client(tmp_path/'missing.sock',args[2],sid,key_path=args[3],require_account_binding=True):pass
        await o.bind_account(REQ,*args,probe=AsyncMock(return_value=100));await o.activate(REQ,b,*args)
        async with isolated_max_client(tmp_path/'missing.sock',args[2],sid,key_path=args[3],require_account_binding=True) as client:assert client._expected_viewer==100
        with pytest.raises(PermissionError):await o.reauthenticate(REQ,*args,{},probe=AsyncMock(return_value=100))
        o.pause(REQ,*args);(args[2]/'account.enc').unlink()
        with pytest.raises(PermissionError):await o.activate(REQ,b,*args)
        assert o.status(REQ)['state']=='PAUSED'
    finally:o.close();r.close()
