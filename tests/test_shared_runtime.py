"""Isolated process and transaction fault tests; no real messenger calls."""
import asyncio,os,sys,sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from tests import test_shared_gateway_ipc as fixtures
from tests.test_shared_gateway_ipc import _telegram_update
from app.shared_gateway_runtime import SharedGatewayRuntime
from app.shared_worker_runtime import SharedWorkerRuntime
from app.max_client import MaxMessage
from tests.shared_runtime_child import SyntheticMax
shared_setup=fixtures.setup


def bot():
    async def send(**kw):return SimpleNamespace(message_id=4001,chat=SimpleNamespace(id=kw['chat_id']),message_thread_id=kw['message_thread_id'])
    return SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=999)),get_updates=AsyncMock(return_value=[]),send_message=AsyncMock(side_effect=send),create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=77)))

@pytest.mark.asyncio
async def test_atomic_polling_and_restart(shared_setup):
    reg,plane,old,_,sessions=shared_setup;old.close();sid,actor,forum=sessions[0];plane.bind_topic(sid,42,77,actor);b=bot();g=SharedGatewayRuntime(reg,b);await g.start()
    try:
        with pytest.raises(BlockingIOError):SharedGatewayRuntime(reg,b)
        u=_telegram_update(1,actor,forum,77,'first');v=_telegram_update(2,actor,forum,77,'second');wrong=_telegram_update(3,actor,forum,78,'unbound');foreign=_telegram_update(4,sessions[1][1],forum,77,'foreign')
        b.get_updates.return_value=[u,v,wrong,foreign]
        original=g.inbox.ingest
        def fail_second(update):
            if update['update_id']==2:raise OSError('synthetic storage failure')
            return original(update)
        g.inbox.ingest=fail_second
        with pytest.raises(OSError):await g.poll_once()
        assert g._offset()==0 and g.inbox.counts()=={}
        g.inbox.ingest=original;assert await g.poll_once()==3;assert g._offset()==5;assert g.inbox.counts()=={'RECEIVED':2,'BLOCKED':1}
        assert g.inbox.row(4) is None
    finally:await g.close()
    g=SharedGatewayRuntime(reg,b);await g.start()
    try:
        b.get_updates.return_value=[];await g.poll_once();assert b.get_updates.await_args.kwargs['offset']==5
        assert g.inbox.counts()=={'RECEIVED':2,'BLOCKED':1}
        assert (await g.drain_once())['ingress']=={'RECEIVED':2}
    finally:await g.close()
    b.get_me.return_value=SimpleNamespace(id=998);g=SharedGatewayRuntime(reg,b)
    try:
        with pytest.raises(PermissionError):await g.start()
    finally:await g.close()

@pytest.mark.asyncio
async def test_provision_and_worker_gaps(shared_setup):
    reg,plane,old,_,sessions=shared_setup;old.close();sid,actor,forum=sessions[0];b=bot();g=SharedGatewayRuntime(reg,b);await g.start();w=None
    try:
        assert await g.provision_topic(sid,42,actor,'synthetic')=='BOUND';assert await g.provision_topic(sid,42,actor,'same')=='BOUND';b.create_forum_topic.assert_awaited_once()
        b.create_forum_topic.side_effect=TimeoutError('lost response');assert await g.provision_topic(sid,43,actor,'unknown')=='UNKNOWN';assert await g.provision_topic(sid,43,actor,'unknown')=='UNKNOWN';assert b.create_forum_topic.await_count==2
        w=SharedWorkerRuntime(reg,sid,SyntheticMax());await w.start();assert w.topics.get_topic(42)==77
        for cid,mid,atts,reason in [(43,'9',[],'UNBOUND'),(42,'10',[{'_type':'PHOTO'}],'UNSUPPORTED')]:
            assert await w.on_message(MaxMessage(chat_id=cid,message_id=mid,text='synthetic',attaches=atts))==reason
        assert await w.on_message(MaxMessage(chat_id=42,message_id='11',text='synthetic',is_self=True))=='SELF_UNVERIFIED'
        assert w.egress.db.execute('SELECT COUNT(*) FROM worker_event_gaps').fetchone()[0]==3
        assert not w.egress.counts()
    finally:
        if w:await w.close()
        await g.close()

async def launch(root,sid):
    env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR','LANG')}
    p=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.shared_runtime_child',str(root),sid,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        line=await asyncio.wait_for(p.stdout.readline(),10);assert line==b'READY\n',line
    except BaseException:
        if p.returncode is None:p.kill()
        await p.wait();raise
    return p

async def kill(p):
    if p.returncode is None:p.kill()
    await asyncio.wait_for(p.wait(),5)

@pytest.mark.asyncio
async def test_two_actual_worker_processes_and_crash_recovery(shared_setup):
    reg,plane,old,_,sessions=shared_setup;old.close();b=bot();g=SharedGatewayRuntime(reg,b);await g.start();children=[]
    try:
        for sid,actor,forum in sessions:plane.bind_topic(sid,42,77,actor);children.append(await launch(reg.root,sid))
        assert children[0].pid!=children[1].pid
        for _ in range(60):
            await g.drain_once()
            if g.egress.counts()=={'SENT':2}:break
            await asyncio.sleep(.05)
        assert g.egress.counts()=={'SENT':2};assert b.send_message.await_count==2
        b.get_updates.return_value=[_telegram_update(i+1,a,f,77,'text') for i,(_,a,f) in enumerate(sessions)];assert await g.poll_once()==2;assert (await g.drain_once())['ingress']=={'ACKED':2}
        await kill(children[0]);sid,actor,forum=sessions[0];b.get_updates.return_value=[_telegram_update(3,actor,forum,77,'offline')];await g.poll_once();assert (await g.drain_once())['ingress']=={'RECEIVED':1}
        children[0]=await launch(reg.root,sid);assert (await g.drain_once())['ingress']=={'ACKED':1};await asyncio.sleep(.15);await g.drain_once();assert b.send_message.await_count==2
        for i,(sid,_,_) in enumerate(sessions):
            p=reg.capsule_dir(sid)/'state'/'tg-outbound.sqlite3'
            with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as c:assert c.execute("SELECT COUNT(*) FROM outbound WHERE status='SENT'").fetchone()[0]==(2 if i==0 else 1)
    finally:
        for p in children:await kill(p)
        await g.close()

@pytest.mark.asyncio
async def test_legacy_general_queue_and_unsupported_text_fail_closed(shared_setup):
    reg,plane,old,_,sessions=shared_setup;sid,actor,forum=sessions[0];plane.bind_topic(sid,42,77,actor);old.enqueue(sid,'legacy:1','do not send');old.close();b=bot();g=SharedGatewayRuntime(reg,b);await g.start()
    try:
        update=_telegram_update(1,actor,forum,77,'/start');other=_telegram_update(2,actor,forum,77,'format');other['message']['entities']=[{'type':'bold','offset':0,'length':6}]
        b.get_updates.return_value=[update,other];assert await g.poll_once()==2;assert g.inbox.counts()=={'BLOCKED':2};assert (await g.drain_once())['egress']=={'BLOCKED':1};b.send_message.assert_not_awaited()
    finally:await g.close()

@pytest.mark.asyncio
async def test_gateway_run_stop_and_cancelled_ingress(shared_setup):
    reg,plane,old,_,sessions=shared_setup;old.close();sid,actor,forum=sessions[0];plane.bind_topic(sid,42,77,actor);b=bot();g=SharedGatewayRuntime(reg,b)
    stop=asyncio.Event();ready=asyncio.Event()
    async def poll(**kw):ready.set();await asyncio.sleep(30)
    b.get_updates.side_effect=poll;task=asyncio.create_task(g.run(stop));await asyncio.wait_for(ready.wait(),5);stop.set();await asyncio.wait_for(task,5);assert not g.server.path.exists()
    b.get_updates.side_effect=None;b.get_updates.return_value=[_telegram_update(1,actor,forum,77,'pending')];g=SharedGatewayRuntime(reg,b);await g.start()
    try:
        await g.poll_once();ready.clear()
        async def hang(*args):ready.set();await asyncio.sleep(30)
        g.engine.deliver=hang;task=asyncio.create_task(g.drain_once());await asyncio.wait_for(ready.wait(),5);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert g.inbox.counts()=={'UNKNOWN':1};assert (await g.drain_once())['ingress']=={}
    finally:await g.close()

@pytest.mark.asyncio
async def test_parallel_ingress_fair_and_ordered_with_unavailable_worker(shared_setup):
    from app.telegram_inbox import WorkerUnavailable
    reg,plane,old,_,sessions=shared_setup;old.close();b=bot();g=SharedGatewayRuntime(reg,b);await g.start()
    a,actor,forum=sessions[0];other,actor2,forum2=sessions[1];plane.bind_topic(a,42,77,actor);plane.bind_topic(other,42,77,actor2)
    try:
        b.get_updates.return_value=[_telegram_update(i,actor,forum,77,'a') for i in range(1,7)]+[_telegram_update(7,actor2,forum2,77,'b'),_telegram_update(8,actor2,forum2,77,'b')];await g.poll_once()
        assert g.inbox.ready(4,fair=True)==[1,7,2,8]
        entered=asyncio.Event();release=asyncio.Event();seen=[]
        async def deliver(sid,u):
            seen.append((sid,u['update_id']))
            if sid==a:await release.wait();raise WorkerUnavailable()
            entered.set();return True
        g.engine.deliver=deliver;task=asyncio.create_task(g.engine.drain(4,concurrency=2,fair=True))
        try:
            await asyncio.wait_for(entered.wait(),2);assert (a,1) in seen
        finally:release.set()
        assert await task=={'ACKED':2,'RECEIVED':1};assert [u for s,u in seen if s==a]==[1];assert [u for s,u in seen if s==other]==[7,8]
        assert g.inbox.row(2)['status']=='RECEIVED'
        # Simulate a competing recovery marking a claimed row UNKNOWN;
        # a late ACK must not overwrite that durable uncertainty.
        async def late(sid,u):
            g.inbox.db.execute("UPDATE telegram_ingress SET status='UNKNOWN' WHERE update_id=?",(u['update_id'],));return True
        assert await g.inbox.dispatch(1,late)=='UNKNOWN';assert g.inbox.row(1)['status']=='UNKNOWN'
    finally:await g.close()
