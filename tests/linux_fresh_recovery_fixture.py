"""Explicit operator recovery in a fresh synthetic namespace; never live authority."""
import asyncio
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time

from app.capsule_registry import CapsuleRegistry
from app.egress_policy import EgressDenied
from app.rotation_fence import RotationFence
from app.generation_ledger import GenerationLedger
from app.generation_controller import GenerationController
from app.isolated_transport import isolated_max_client, isolated_telegram_bot
from app.isolated_worker_runtime import IsolatedWorkerRuntime
from app.linux_egress_policy import BrokerPins
from app.recovery_transition import RecoveryTransition
from app.operator_pin_rotation import NamespaceHandle, digest, frame, read_frame_fd
from app.pinned_broker import PinnedBroker
from app.shared_gateway_runtime import SharedGatewayRuntime
from app.telegram_budget import TelegramBudget
from tests.linux_journal_rotation_fixture import ACTOR, FORUM, rows, until
from tests.linux_veth_fixture import IPS, DROP, call, denied, isolated, unprivileged


async def broker(root,expiry,identity,fd,nonce=None):
    os.close(fd)
    info=os.stat('/proc/self/ns/net')
    assert (info.st_dev,info.st_ino)==identity
    unprivileged()
    isolated([],{'lo','eth0'})
    pins=BrokerPins.parse({'max-text':[IPS[2]],'telegram':[IPS[1]],'expires_at':expiry})
    sid=(root/'sid').read_text()
    b=PinnedBroker(pins,{sid:root/'recovery-max','telegram':root/'recovery-tg'})
    stop=asyncio.Event()
    task=asyncio.create_task(b.serve(stop))
    try:
        await asyncio.wait_for(b.ready.wait(),2)
        await denied(IPS[0],443)
        if nonce is not None:
            os.write(sys.stdout.fileno(),frame({'nonce':nonce,'digest':digest(b.pins),'ready':b.ready.is_set()}))
        (root/'recovery-ready').touch()
        await until((root/'recovery-done').is_file,timeout=20)
        assert b.ready.is_set()
    finally:
        stop.set()
        assert await asyncio.wait_for(task,3)=='STOPPED'
    print('FRESH BROKER PASS',flush=True)


async def client(cert,root):
    unprivileged()
    isolated([],{'lo'})
    sid=(root/'sid').read_text()
    await until((root/'recovery-ready').is_file)
    ctx=ssl.create_default_context(cafile=cert+'/cert.pem')
    reg=CapsuleRegistry(root/'registry')
    stop=asyncio.Event()
    try:
        async with isolated_telegram_bot(root/'recovery-tg'/'proxy.sock','123456:synthetic',
                                        context=ctx,budget=TelegramBudget(reg.db)) as bot:
            g=SharedGatewayRuntime(reg,bot,channels={sid:root/'channel'},external_only=True,managed=True)
            try:
                await g.start()
                assert g._offset()==3
                assert g.inbox.counts()=={'ACKED':1,'UNKNOWN':1}
                async with isolated_max_client(root/'recovery-max'/'proxy.sock',root/'secret',sid,
                        context=ctx,key_path=root/'keys'/'max.key',require_account_binding=True) as m:
                    w=IsolatedWorkerRuntime(sid,root/'state',root/'channel',root/'state'/'key',FORUM,ACTOR,m)
                    task=asyncio.create_task(w.run(stop))
                    try:
                        await until(lambda:w.started and m._authorized)
                        assert m._my_id==101
                        assert w.ledger.counts()=={'SENT':1,'UNKNOWN':1}
                        assert await g.inbox.dispatch(1,g.engine.deliver)=='ACKED'
                        assert await g.inbox.dispatch(2,g.engine.deliver)=='UNKNOWN'
                        assert await g.engine.deliver(sid,json.loads(g.inbox.row(1)['payload'])) is True
                        assert await g.engine.deliver(sid,json.loads(g.inbox.row(2)['payload'])) is False
                        assert await g.poll_once()==1
                        assert (await g.drain_once())['ingress']=={'ACKED':1}
                        await until(lambda:w.egress.counts().get('ACCEPTED',0)+w.egress.counts().get('SENT',0)==2)
                        await g.drain_once()
                        await until(lambda:w.egress.counts()=={'SENT':2})
                        assert g._offset()==4
                        assert g.inbox.counts()=={'ACKED':2,'UNKNOWN':1}
                        assert w.ledger.counts()=={'SENT':2,'UNKNOWN':1}
                        assert g.egress.counts()=={'SENT':2}
                    finally:
                        stop.set()
                        await asyncio.wait_for(task,5)
            finally:await g.close()
    finally:reg.close()
    assert rows(root/'registry'/'inbox.sqlite3','SELECT update_id,status FROM telegram_ingress ORDER BY update_id')==[(1,'ACKED'),(2,'UNKNOWN'),(3,'ACKED')]
    assert rows(root/'state'/'tg-outbound.sqlite3','SELECT telegram_id,status,max_id FROM outbound ORDER BY telegram_id')==[(101,'SENT','101'),(102,'UNKNOWN',None),(103,'SENT','103')]
    assert rows(root/'registry'/'telegram-egress.sqlite3','SELECT status,count(*) FROM telegram_egress GROUP BY status')==[('SENT',2)]
    assert rows(root/'state'/'to-telegram.sqlite3','SELECT status,count(*) FROM worker_telegram_intents GROUP BY status')==[('SENT',2)]
    (root/'recovery-done').touch()
    print('FRESH JOURNALS PASS: same account, offset 3 -> 4, UNKNOWN retained, new delivery confirmed',flush=True)


def recover(ns,cert,root,old_handle,client_ns,anchor,lineage=False):
    ledger_type=GenerationLedger if lineage else RotationFence
    # Fixture-only explicit operator decision. Old broker and clients already
    # exited, original grant retained. Retire the exact fixture link, not rules.
    old_handle.check()
    isolated(ns,{'lo','eth0'})
    isolated([],{'lo','m2server'})
    counts=[e['counter']['packets'] for i in old_handle.observe()['nftables']
            if 'rule' in i and i['rule']['chain']=='output' for e in i['rule']['expr'] if 'counter' in e]
    assert counts[0]>2 and counts[1]>=1
    print('RETIRED KERNEL COUNTERS PASS',counts,flush=True)
    call('ip','link','delete','m2server')
    isolated(ns,{'lo'})
    isolated([],{'lo'})
    assert not json.loads(call(*ns,'ip','-j','-4','route','show','table','main'))
    keeper=subprocess.Popen(['unshare','--net','sleep','35'])
    handle=None;children=[];successor=None;nonce=None
    try:
        for _ in range(50):
            if os.readlink('/proc/self/ns/net')!=os.readlink(f'/proc/{keeper.pid}/ns/net'):break
            time.sleep(.05)
        else:raise AssertionError('Fresh namespace not created')
        fresh=['nsenter','-t',str(keeper.pid),'-n','--']
        info=os.stat(f'/proc/{keeper.pid}/ns/net')
        identity=(info.st_dev,info.st_ino)
        assert identity!=old_handle.identity
        handle=NamespaceHandle(keeper.pid,expected=identity)
        with ledger_type(anchor,handle) as foreign:
            try:foreign.inspect()
            except EgressDenied:pass
            else:raise AssertionError('Old fence adopted in new namespace')
        print('FRESH NAMESPACE FOREIGN FENCE DENIED',flush=True)
        call('ip','link','add','m2server','type','veth','peer','name','m2broker')
        call('ip','link','set','m2broker','netns',str(keeper.pid))
        call('ip','addr','add','192.0.2.1/30','dev','m2server')
        call('ip','link','set','m2server','up')
        call(*fresh,'ip','link','set','m2broker','name','eth0')
        call(*fresh,'ip','addr','add','192.0.2.2/30','dev','eth0')
        call(*fresh,'ip','link','set','eth0','up')
        call(*fresh,'ip','link','set','lo','up')
        for ip in IPS:call(*fresh,'ip','route','add',ip+'/32','via','192.0.2.1','dev','eth0')
        isolated(fresh,{'lo','eth0'})
        isolated(ns,{'lo'})
        with ledger_type(anchor,old_handle) as retained:
            latest=retained._load()[1][-1].expires_at if lineage else retained._load()['replacement']['expires_at']
            expiry=max(int(time.time())+28,latest+1)
            pins=BrokerPins.parse({'max-text':[IPS[2]],'telegram':[IPS[1]],'expires_at':expiry})
            transition=RecoveryTransition(retained,handle)
            options=dict(operator_approved=True,stopped=lambda:not children,
                install=lambda batch:handle._nft(['-f','-'],data=batch))
            if lineage:successor,nonce,result=transition.prepare_successor(pins,**options)
            else:result=transition.prepare(pins,**options)
            assert result['verified'] and result['recovery_prepared'] and not result['live_launch_authorized']
            assert transition.inspect()['prepared_policy_matches']
            try:RecoveryTransition(retained,handle).prepare(pins,operator_approved=True,
                stopped=lambda:not children,install=lambda batch:handle._nft(['-f','-'],data=batch))
            except EgressDenied:pass
            else:raise AssertionError('Prepared recovery replayed')
            print('DURABLE RECOVERY TRANSITION PASS',flush=True)
        call(*DROP,sys.executable,'-c','from pathlib import Path;import sys;p=Path(sys.argv[1]);[(p/n).mkdir(mode=0o700) for n in ("recovery-max","recovery-tg")]',root)
        children.append(subprocess.Popen(['/usr/bin/nsenter',f'--net=/proc/self/fd/{handle.fd}','--',
            *DROP,sys.executable,'-m','tests.linux_fresh_recovery_fixture','broker',root,str(expiry),
            str(identity[0]),str(identity[1]),str(handle.fd),*([nonce] if lineage else [])],
            pass_fds=(handle.fd,),stdout=subprocess.PIPE if lineage else None))
        if lineage:
            def stopped():
                # Fixture broker has a bounded lifetime; caller cannot claim
                # verified stop until the process actually exits.
                try:children[0].wait(timeout=25)
                except subprocess.TimeoutExpired:return False
                return children[0].poll() is not None
            controller=GenerationController(successor,send=lambda _:None,
                receive=lambda:read_frame_fd(children[0].stdout.fileno(),timeout=5),stop_and_verify=stopped)
            assert old_handle.retired()
            evidence=controller.adopt_recovery(nonce)
            assert evidence['generation']==2 and evidence['state']=='ADOPTED'
            # Even addresses no longer in the immediately preceding bundle
            # remain bound to their original role across namespace recovery.
            bad=BrokerPins.parse(dict(pins.record(),telegram=[IPS[0]],expires_at=expiry+8))
            try:successor.arm(pins,bad)
            except EgressDenied:pass
            else:raise AssertionError('Historical role changed across recovery')
            print('RECOVERED GENERATION ADOPTED PASS',flush=True)
        children.append(subprocess.Popen([*client_ns,*DROP,sys.executable,'-m','tests.linux_fresh_recovery_fixture','client',cert,root]))
        assert children[1].wait(timeout=23)==0
        assert children[0].wait(timeout=5)==0
        if lineage:
            out=children[0].stdout.read()
            children[0].stdout.close()
            assert b'FRESH BROKER PASS' in out
            print(out.decode(),flush=True)
            successor.close();successor=None
            with GenerationLedger(Path(anchor)/'successor',handle) as reopened:
                assert reopened.inspect()['generation']==2
                assert reopened.inspect()['state']=='ADOPTED'
                assert len(reopened._load()[1])==3
        counts=[e['counter']['packets'] for i in handle.observe()['nftables']
                if 'rule' in i and i['rule']['chain']=='output' for e in i['rule']['expr'] if 'counter' in e]
        assert counts[0]>2 and counts[1]>=1
        print('FRESH KERNEL RECOVERY PASS',counts,flush=True)
    finally:
        try:
            for child in children:
                if child.poll() is None:child.wait(timeout=25)
        finally:
            if successor is not None:successor.close()
            if handle is not None:handle.close()
            keeper.terminate();keeper.wait(timeout=3)


if __name__=='__main__':
    if sys.argv[1]=='broker':
        asyncio.run(broker(Path(sys.argv[2]),int(sys.argv[3]),(int(sys.argv[4]),int(sys.argv[5])),int(sys.argv[6]),sys.argv[7] if len(sys.argv)>7 else None))
    elif sys.argv[1]=='client':asyncio.run(client(sys.argv[2],Path(sys.argv[3])))
    else:raise ValueError('Unknown fixture action')
