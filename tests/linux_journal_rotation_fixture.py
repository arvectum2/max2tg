"""Actual managed gateway/worker, signed IPC and durable journals during rotation."""
import asyncio
import sqlite3
import json
from contextlib import closing
import ssl
from types import SimpleNamespace

from app.capsule_registry import CapsuleRegistry
from app.encrypted_credentials import install_encrypted_worker
from app.encrypted_storage import create_key, install_envelope
from app.isolated_transport import isolated_max_client, isolated_telegram_bot
from app.isolated_worker_runtime import IsolatedWorkerRuntime
from app.shared_gateway_runtime import SharedGatewayRuntime
from app.shared_onboarding import SharedOnboarding
from app.telegram_budget import TelegramBudget
from app.worker_provision import prepare_worker, check_worker
from tests.linux_veth_fixture import isolated, unprivileged

ACTOR=100
FORUM=-10001
REQUEST='b'*24


async def until(check, timeout=12):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(.05)


async def provision(root):
    for name in ('registry','state','channel','secret','keys'):
        (root/name).mkdir(mode=0o700)
    r=CapsuleRegistry(root/'registry')
    try:
        o=SharedOnboarding(r)
        sid=o.reserve(REQUEST,ACTOR,FORUM)
        class VerifyBot:
            async def get_me(self):return SimpleNamespace(id=900)
            async def get_chat(self,gid):return SimpleNamespace(id=gid,type='supergroup',is_forum=True)
            async def get_chat_member(self,gid,uid):
                return SimpleNamespace(user=SimpleNamespace(id=uid),status='creator' if uid==ACTOR else 'administrator',can_manage_topics=True)
            async def get_chat_member_count(self,gid):return 2
        bot=VerifyBot()
        await o.verify_forum(REQUEST,bot)
        key=root/'keys'/'max.key'
        create_key(key,'max:'+sid)
        install_encrypted_worker(root/'secret',sid,{'session_id':sid,'MAX_TOKEN':'synthetic-max',
            'MAX_DEVICE_ID':'synthetic-device'},key)
        install_envelope(root/'secret'/'account.enc',b'101',key,'max:'+sid,'account-binding')
        folders=(root/'state',root/'channel',root/'secret')
        assert prepare_worker(*folders,sid,ACTOR,FORUM,o.ipc_key(REQUEST).read_bytes(),key_path=key)
        def check(s,a,f,k):return check_worker(*folders,s,a,f,k.read_bytes(),key_path=key)
        o.confirm_provisioned(REQUEST,check=check)
        assert await o.activate(REQUEST,bot,check=check)==sid
        o.plane.bind_topic(sid,42,77,ACTOR)
        return sid
    finally:r.close()


def rows(path,query):
    with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as db:
        return db.execute(query).fetchall()


async def client(cert,root,crash=False):
    unprivileged()
    isolated([],{'lo'})
    sid=await provision(root)
    (root/'sid.tmp').write_text(sid)
    (root/'sid.tmp').rename(root/'sid')
    await until((root/'ready').is_file)
    ctx=ssl.create_default_context(cafile=cert+'/cert.pem')
    reg=CapsuleRegistry(root/'registry')
    stop=asyncio.Event()
    async with isolated_telegram_bot(root/'tg'/'proxy.sock','123456:synthetic',
                                     context=ctx,budget=TelegramBudget(reg.db)) as bot:
        g=SharedGatewayRuntime(reg,bot,channels={sid:root/'channel'},external_only=True,managed=True)
        try:
            await g.start()
            async with isolated_max_client(root/'max'/'proxy.sock',root/'secret',sid,context=ctx,
                                           key_path=root/'keys'/'max.key',require_account_binding=True) as m:
                w=IsolatedWorkerRuntime(sid,root/'state',root/'channel',root/'state'/'key',FORUM,ACTOR,m)
                task=asyncio.create_task(w.run(stop))
                try:
                    await until(lambda:w.started and m._authorized)
                    assert m._my_id==101
                    (root/'phase1').touch()
                    assert await g.poll_once()==1
                    assert (await g.drain_once())['ingress']=={'ACKED':1}
                    await until(lambda:w.egress.counts().get('ACCEPTED',0)+w.egress.counts().get('SENT',0)==1)
                    await g.drain_once()
                    await until(lambda:w.egress.counts()=={'SENT':1})
                    assert g.egress.counts()=={'SENT':1}
                    (root/'phase2').touch()
                    assert await g.poll_once()==1
                    assert (await g.drain_once())['ingress']=={'UNKNOWN':1}
                    assert g.inbox.row(2)['status']=='UNKNOWN'
                    assert w.ledger.row(FORUM,102)['status']=='UNKNOWN'
                    if crash:
                        await until((root/'broker-failed').is_file)
                        assert not m._authorized
                        (root/'phase3').touch()
                        assert await g.poll_once()==0
                        assert g.poll_failures==1 and g._offset()==3
                        assert g.inbox.row(3) is None
                        assert g.inbox.counts()=={'ACKED':1,'UNKNOWN':1}
                        assert w.ledger.counts()=={'SENT':1,'UNKNOWN':1}
                        assert await g.inbox.dispatch(2,g.engine.deliver)=='UNKNOWN'
                        assert await g.inbox.dispatch(1,g.engine.deliver)=='ACKED'
                        assert g.egress.counts()==w.egress.counts()=={'SENT':1}
                    else:
                        await until((root/'rotated').is_file)
                        # A new update while disconnected is durably queued, not falsely ACKed.
                        (root/'phase3').touch()
                        assert await g.poll_once()==1
                        assert not m._authorized
                        assert (await g.drain_once())['ingress']=={'RECEIVED':1}
                        assert g.inbox.row(3)['status']=='RECEIVED'
                        assert w.ledger.row(FORUM,103) is None
                        await until(lambda:m._authorized and m._my_id==101)
                        assert (await g.drain_once())['ingress']=={'ACKED':1}
                        # Explicit re-dispatch of terminal UNKNOWN and ACKED rows must not invoke MAX.
                        assert await g.inbox.dispatch(2,g.engine.deliver)=='UNKNOWN'
                        assert await g.inbox.dispatch(1,g.engine.deliver)=='ACKED'
                        # Even a direct repeat through the existing signed worker IPC
                        # cannot replay either terminal worker journal entry.
                        assert await g.engine.deliver(sid,json.loads(g.inbox.row(2)['payload'])) is False
                        assert await g.engine.deliver(sid,json.loads(g.inbox.row(1)['payload'])) is True
                        await until(lambda:w.egress.counts().get('ACCEPTED',0)+w.egress.counts().get('SENT',0)==2)
                        await g.drain_once()
                        await until(lambda:w.egress.counts()=={'SENT':2})
                        assert g.inbox.counts()=={'ACKED':2,'UNKNOWN':1}
                        assert w.ledger.counts()=={'SENT':2,'UNKNOWN':1}
                        assert g.egress.counts()=={'SENT':2}
                        assert g._offset()==4
                finally:
                    stop.set()
                    await asyncio.wait_for(task,5)
        finally:await g.close()
    reg.close()
    if crash:
        assert rows(root/'registry'/'inbox.sqlite3','SELECT update_id,status FROM telegram_ingress ORDER BY update_id')==[(1,'ACKED'),(2,'UNKNOWN')]
        assert rows(root/'state'/'tg-outbound.sqlite3','SELECT telegram_id,status,max_id FROM outbound ORDER BY telegram_id')==[(101,'SENT','101'),(102,'UNKNOWN',None)]
        assert rows(root/'registry'/'telegram-egress.sqlite3','SELECT status,count(*) FROM telegram_egress GROUP BY status')==[('SENT',1)]
        assert rows(root/'state'/'to-telegram.sqlite3','SELECT status,count(*) FROM worker_telegram_intents GROUP BY status')==[('SENT',1)]
        (root/'done').touch()
        print('LIVE CRASH JOURNALS PASS: confirmed and UNKNOWN retained, polling offset unchanged',flush=True)
        return
    assert rows(root/'registry'/'inbox.sqlite3','SELECT update_id,status FROM telegram_ingress ORDER BY update_id')==[(1,'ACKED'),(2,'UNKNOWN'),(3,'ACKED')]
    assert rows(root/'state'/'tg-outbound.sqlite3','SELECT telegram_id,status,max_id FROM outbound ORDER BY telegram_id')==[(101,'SENT','101'),(102,'UNKNOWN',None),(103,'SENT','103')]
    assert rows(root/'registry'/'telegram-egress.sqlite3','SELECT status,count(*) FROM telegram_egress GROUP BY status')==[('SENT',2)]
    assert rows(root/'state'/'to-telegram.sqlite3','SELECT status,count(*) FROM worker_telegram_intents GROUP BY status')==[('SENT',2)]
    (root/'done').touch()
    print('JOURNAL ROTATION PASS: central ACKED/UNKNOWN/ACKED, worker SENT/UNKNOWN/SENT, two confirmed egress, persisted readback',flush=True)
