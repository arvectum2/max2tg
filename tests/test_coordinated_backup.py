"""Real stopped gateway/worker state, synthetic messenger boundaries."""
import sqlite3,tempfile,shutil
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.capsule_registry import CapsuleRegistry
from app.shared_onboarding import SharedOnboarding
from app.worker_secrets import install_worker_secret
from app.worker_provision import prepare_worker,check_worker
from app.shared_gateway_runtime import SharedGatewayRuntime
from app.isolated_worker_runtime import IsolatedWorkerRuntime
from app.max_client import MaxMessage
from app.recovery_manifest import RecoveryAnchor
from app.coordinated_backup import capture_delivery_state,materialize_verified,GATEWAY,WORKER
from app.restored_start_gate import launch_verified_restore
from app.runtime_lock import RuntimeLock
from tests.test_shared_onboarding import bot
from tests.test_shared_gateway_ipc import FakeMaxClient,_telegram_update

class Max(FakeMaxClient):
    def on_message(self,callback):self.callback=callback
    async def telegram_echo_tg_id(self,msg):return None

@pytest.mark.asyncio
async def test_capture_and_materialize_real_stopped_runtime(tmp_path):
    with tempfile.TemporaryDirectory(prefix='m2bk-',dir='/tmp') as short:
        base=Path(short);registry=CapsuleRegistry(base/'registry');o=SharedOnboarding(registry);b=bot();states={};channels={};workers=[];sessions=[]
        for i in range(2):
            actor=101+i;forum=-100001-i;req=f'{i+1:024x}';b=bot(actor,forum);sid=o.reserve(req,actor,forum);await o.verify_forum(req,b)
            folders=[base/f'{n}{i}' for n in ('state','channel','secret')]
            for p in folders:p.mkdir(mode=0o700)
            state,channel,secret=folders;install_worker_secret(secret,sid,{'session_id':sid,'MAX_TOKEN':'synthetic-private','MAX_DEVICE_ID':'synthetic'});key=o.ipc_key(req)
            assert prepare_worker(*folders,sid,actor,forum,key.read_bytes())
            def check(s,a,f,k):return check_worker(*folders,s,a,f,k.read_bytes())
            o.confirm_provisioned(req,check=check);await o.activate(req,b,check=check);o.plane.bind_topic(sid,42,77,actor)
            states[sid]=state;channels[sid]=channel;sessions.append((sid,actor,forum))
            workers.append(IsolatedWorkerRuntime(sid,state,channel,state/'key',forum,actor,Max()))
        output=base/'backups';restores=base/'restores';anchor_dir=base/'anchor'
        for p in (output,restores,anchor_dir):p.mkdir(mode=0o700)
        anchor=RecoveryAnchor(anchor_dir);b=bot();b.send_message=AsyncMock(side_effect=lambda **kw: NS(message_id=777,chat=NS(id=kw['chat_id']),message_thread_id=77));g=SharedGatewayRuntime(registry,b,channels=channels,external_only=True)
        def capture(name,workers=states,**kw):return capture_delivery_state(registry.root,workers,output,anchor,name=name,provisioning_stopped=True,**kw)
        try:
            await g.start()
            for w,(sid,actor,forum) in zip(workers,sessions):
                await w.start();assert await w.on_message(MaxMessage(chat_id=42,message_id='123',text='synthetic outgoing'))=='PENDING';await w.drain_once()
                assert await g.engine.accept(_telegram_update(actor,actor,forum,77,'synthetic incoming'))=='ACKED'
            await g.drain_once()
            for w in workers:await w.drain_once()
            assert g.egress.counts()=={'SENT':2};assert g.inbox.counts()=={'ACKED':2}
            with pytest.raises(BlockingIOError):capture('snapshot-running')
        finally:await g.close()
        # Gateway stopped but worker locks still held.
        try:
            with pytest.raises(BlockingIOError):capture('snapshot-worker-running')
        finally:
            for w in workers:await w.close()
            registry.close()
        assert not list(output.iterdir())
        provision=RuntimeLock(registry.root/'shared-provisioning.lock',shared=True)
        try:
            with pytest.raises(BlockingIOError):capture('snapshot-provisioning')
        finally:provision.close()
        with pytest.raises(PermissionError,match='every external capsule'):capture('snapshot-partial',workers={sessions[0][0]:states[sessions[0][0]]})
        assert not list(output.iterdir())
        target,record=capture('snapshot-001');assert len(record['manifest'])==11
        restored,evidence=materialize_verified(target,restores,anchor,name='snapshot-restore');assert evidence['members']==11 and evidence['structural_only']
        assert not any('secret' in n or n.endswith('key') for n in record['manifest'])
        for p in restored.iterdir():assert b'synthetic-private' not in p.read_bytes()
        with closing(sqlite3.connect((restored/'gateway-inbox.sqlite3').as_uri()+'?mode=ro&immutable=1',uri=True)) as db:assert db.execute("SELECT count(*) FROM telegram_ingress WHERE status='ACKED'").fetchone()[0]==2
        for sid,_,_ in sessions:
            with closing(sqlite3.connect((restored/f'{sid}-to-telegram.sqlite3').as_uri()+'?mode=ro&immutable=1',uri=True)) as db:assert db.execute("SELECT telegram_id FROM worker_telegram_intents WHERE status='SENT'").fetchone()[0]==777
        with pytest.raises(FileExistsError):materialize_verified(target,restores,anchor,name='snapshot-restore')
        # Actual reconstructed runtime layout, independent retained secrets/keys.
        newroot=base/'newregistry';newroot.mkdir(mode=0o700);(newroot/'tenants').mkdir(mode=0o700)
        for n in GATEWAY:shutil.copy2(restored/f'gateway-{n}',newroot/n)
        for n in ('shared-gateway.lock','shared-provisioning.lock'):(newroot/n).touch(mode=0o600)
        layout={}
        for i,(sid,actor,forum) in enumerate(sessions):
            state=base/f'newstate{i}';channel=base/f'newchannel{i}';secret=base/f'newsecret{i}'
            for p in (state,channel,secret):p.mkdir(mode=0o700)
            for n in WORKER:shutil.copy2(restored/f'{sid}-{n}',state/n)
            (state/'worker.lock').touch(mode=0o600);shutil.copy2(states[sid]/'key',state/'key')
            capsule=newroot/'tenants'/sid;capsule.mkdir(mode=0o700);shutil.copy2(state/'key',capsule/'telegram-ipc.key')
            shutil.copy2(base/f'secret{i}'/'max.json',secret/'max.json');layout[sid]=(state,channel,secret)
        async def start():
            assert anchor._load()['dirty']
            rr=CapsuleRegistry(newroot);gg=SharedGatewayRuntime(rr,b,channels={sid:p[1] for sid,p in layout.items()},external_only=True);ww=[]
            try:
                await gg.start()
                for sid,actor,forum in sessions:
                    state,channel,_=layout[sid];w=IsolatedWorkerRuntime(sid,state,channel,state/'key',forum,actor,Max());ww.append(w);await w.start()
                    await w.on_message(MaxMessage(chat_id=42,message_id='123',text='synthetic outgoing'));await w.drain_once()
                await gg.drain_once();assert gg.egress.counts()=={'SENT':2};assert gg.inbox.counts()=={'ACKED':2};return 'started'
            finally:
                for w in ww:await w.close()
                await gg.close();rr.close()
        unexpected=AsyncMock(side_effect=AssertionError('Must not start'))
        k=layout[sessions[0][0]][0]/'key';original=k.read_bytes();k.write_bytes(b'z'*32)
        with pytest.raises(PermissionError,match='IPC key mismatch'):await launch_verified_restore(anchor,newroot,layout,start=unexpected)
        k.write_bytes(original)
        sidecar=newroot/'inbox.sqlite3-wal';sidecar.touch(mode=0o600)
        with pytest.raises(PermissionError,match='sidecar'):await launch_verified_restore(anchor,newroot,layout,start=unexpected)
        sidecar.unlink()
        corrupt=newroot/'inbox.sqlite3';original=corrupt.read_bytes();corrupt.write_bytes(original+b'x')
        with pytest.raises(PermissionError,match='differs'):await launch_verified_restore(anchor,newroot,layout,start=unexpected)
        corrupt.write_bytes(original);unexpected.assert_not_awaited();assert not anchor._load()['dirty']
        assert await launch_verified_restore(anchor,newroot,layout,start=start)=='started'
        with pytest.raises(PermissionError,match='Dirty anchor'):await launch_verified_restore(anchor,newroot,layout,start=unexpected)
        unexpected.assert_not_awaited()
        with pytest.raises(PermissionError,match='Dirty epoch'):capture('snapshot-dirty')
        assert not (output/'snapshot-dirty').exists()
        with pytest.raises(PermissionError):materialize_verified(target,restores,anchor,name='snapshot-denied')
        assert not (restores/'snapshot-denied').exists()
        assert b.send_message.await_count==2
