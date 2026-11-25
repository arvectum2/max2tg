"""Gateway-owned routes without worker access to the control plane."""
import pytest
from tests import test_shared_gateway_ipc as fixtures
from tests.test_shared_gateway_ipc import _telegram_update
from tests.test_shared_runtime import bot
from tests.shared_runtime_child import SyntheticMax
from app.shared_gateway_runtime import SharedGatewayRuntime
from app.isolated_worker_runtime import IsolatedWorkerRuntime
from app.telegram_egress_ipc import GatewayEgressIpcClient
from app.telegram_ipc import ensure_ipc_key
from app.max_client import MaxMessage
shared_setup=fixtures.setup

@pytest.mark.asyncio
async def test_isolated_routes_and_two_tenant_channel_acl(shared_setup):
    reg,plane,old,_,sessions=shared_setup;old.close();channels={};local=[];workers=[]
    for i,(sid,actor,forum) in enumerate(sessions):
        plane.bind_topic(sid,42,77,actor)
        c=reg.root.parent/f'c{i}';c.mkdir(mode=0o700);state=reg.root.parent/f'w{i}';state.mkdir(mode=0o700)
        key=state/'key';key.write_bytes(ensure_ipc_key(reg,sid).read_bytes());key.chmod(0o600);channels[sid]=c;local.append((state,key))
    b=bot();g=SharedGatewayRuntime(reg,b,channels=channels);await g.start()
    try:
        for (sid,actor,forum),(state,key) in zip(sessions,local):
            w=IsolatedWorkerRuntime(sid,state,channels[sid],key,forum,actor,SyntheticMax());await w.start();workers.append(w)
            assert not hasattr(w,'registry') and not hasattr(w,'plane')
            assert w.topics.get_topic(42)==77
            assert await w.on_message(MaxMessage(chat_id=42,message_id='123',text=f'only {actor}'))=='PENDING';assert await w.drain_once()=={'ACCEPTED':1}
        assert (await g.drain_once())['egress']=={'SENT':2}
        assert [(c.kwargs['chat_id'],c.kwargs['text']) for c in b.send_message.await_args_list]==[(f,f'only {a}') for _,a,f in sessions]
        for w in workers:assert await w.drain_once()=={'SENT':1}
        b.get_updates.return_value=[_telegram_update(i+1,a,f,77,'incoming') for i,(_,a,f) in enumerate(sessions)];assert await g.poll_once()==2;assert (await g.drain_once())['ingress']=={'ACKED':2}
        sid,actor,forum=sessions[0];other=sessions[1][0]
        # A valid own key does not authorize another tenant or channel.
        wrong=GatewayEgressIpcClient(other,channels[sid]/'egress.sock',local[0][1])
        with pytest.raises((EOFError,OSError,ValueError)):await wrong.routes()
        wrong=GatewayEgressIpcClient(sid,channels[other]/'egress.sock',local[0][1])
        with pytest.raises((EOFError,OSError,ValueError)):await wrong.routes()
        plane.disable_topic(sid,42);assert await workers[0].on_message(MaxMessage(chat_id=42,message_id='124',text='revoked'))=='UNBOUND'
        assert await workers[1].on_message(MaxMessage(chat_id=42,message_id='124',text='allowed'))=='PENDING'
        await workers[1].drain_once();assert (await g.drain_once())['egress']=={'SENT':1};assert b.send_message.await_args.kwargs['chat_id']==sessions[1][2]
    finally:
        for w in workers:await w.close()
        await g.close()


def test_channels_cannot_expose_registry(shared_setup):
    reg,_,old,_,sessions=shared_setup;old.close();sid=sessions[0][0]
    with pytest.raises(ValueError,match='expose registry'):SharedGatewayRuntime(reg,bot(),channels={sid:reg.root})
