"""P2.2b shared bot worker mode and bidirectional IPC, entirely offline."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.capsule_registry import CapsuleRegistry
from app.capsule_supervisor import build_worker_env
from app.capsule_egress_outbox import CapsuleEgressOutbox
from app.telegram_control_plane import TelegramControlPlane
from app.telegram_egress import TelegramEgress
from app.telegram_egress_ipc import (
    GatewayEgressIpcServer, GatewayEgressIpcClient, egress_ipc_path
)
from app.telegram_ipc import ensure_ipc_key, ipc_key_path


@pytest.fixture
def setup():
    # macOS Unix domain sockets limit path length. Use short private /tmp root.
    with tempfile.TemporaryDirectory(prefix="m2ipc-", dir="/tmp") as folder:
        root = Path(folder) / "r"
        registry = CapsuleRegistry(root)
        plane = TelegramControlPlane(registry)
        sessions = []
        for actor, forum in [(101, -100001), (202, -100002)]:
            owner = registry.create_account()
            sid = registry.create_capsule(owner)
            registry.configure(sid, {
                "MAX_TOKEN": "synthetic-max-token",
                "MAX_DEVICE_ID": f"synthetic-{actor}",
                "TG_CHAT_ID": str(forum),
                "TG_ADMIN_USER_ID": str(actor),
            }, telegram_mode="shared")
            registry.set_active(sid, True)
            registry.db.execute(
                "INSERT INTO tg_owners VALUES(?,?)", (str(actor), owner)
            )
            registry.db.execute(
                "INSERT INTO tg_routes VALUES(?,?,?,?,?)",
                (str(forum), sid, owner, "ACTIVE", 1.0)
            )
            ensure_ipc_key(registry, sid)
            sessions.append((sid, actor, forum))
        ledger = TelegramEgress(root / "telegram-egress.sqlite3", plane)
        server = GatewayEgressIpcServer(registry, ledger)
        try:
            yield registry, plane, ledger, server, sessions
        finally:
            ledger.close()
            registry.close()


def client(reg, sid):
    return GatewayEgressIpcClient(
        sid, egress_ipc_path(reg.root), ipc_key_path(reg, sid)
    )


def worker_queue(reg, sid):
    return CapsuleEgressOutbox(
        reg.capsule_dir(sid) / "state" / "to-telegram.sqlite3", sid
    )


def test_shared_mode_never_stores_or_inherits_bot_token(setup):
    reg, _, _, _, sessions = setup
    first = sessions[0][0]
    assert reg.get(first)["telegram_mode"] == "shared"
    assert reg.get(first)["bot_fingerprint"] is None
    assert "TG_BOT_TOKEN" not in reg.credentials(first)
    data = (reg.capsule_dir(first) / "credentials.json").read_text()
    assert "TG_BOT_TOKEN" not in data
    env = build_worker_env(reg, first, {
        "HOME": "/tmp", "TG_BOT_TOKEN": "secret-from-parent",
        "TG_PROXY": "proxy-secret", "MAX2TG_DOTENV_PATH": "/secret"
    })
    assert "TG_BOT_TOKEN" not in env
    assert "TG_PROXY" not in env
    assert "MAX2TG_DOTENV_PATH" not in env
    assert env["MAX2TG_TELEGRAM_MODE"] == "shared"
    assert env["MAX2TG_NO_DOTENV"] == "1"
    assert env["REPLY_ENABLED"] == "false"
    assert "synthetic-max-token" in env["MAX_TOKEN"]


def test_shared_mode_rejects_bot_token_or_invalid_mode(setup):
    reg, _, _, _, sessions = setup
    sid = sessions[0][0]
    reg.set_active(sid, False)
    values = reg.credentials(sid)
    values["TG_BOT_TOKEN"] = "must-not-exist"
    with pytest.raises(ValueError, match="must not store"):
        reg.configure(sid, values, telegram_mode="shared")
    values.pop("TG_BOT_TOKEN")
    with pytest.raises(ValueError, match="Unknown"):
        reg.configure(sid, values, telegram_mode="bad")


@pytest.mark.asyncio
async def test_two_tenant_worker_intents_through_single_signed_gateway(setup):
    reg, _, ledger, server, sessions = setup
    await server.start()
    try:
        queues = []
        for sid, _, _ in sessions:
            q = worker_queue(reg, sid)
            q.stage("max:42", f"offline event {sid[:6]}")
            assert await q.drain(client(reg, sid)) == {"ACCEPTED": 1}
            queues.append(q)
        assert ledger.counts() == {"QUEUED": 2}
        bot = SimpleNamespace(send_message=AsyncMock(
            side_effect=[SimpleNamespace(message_id=501),
                         SimpleNamespace(message_id=502)]
        ))
        assert await ledger.drain(bot) == {"SENT": 2}
        assert [x.kwargs["chat_id"] for x in
                bot.send_message.await_args_list] == [
                    sessions[0][2], sessions[1][2]
                ]
        for idx, (sid, _, _) in enumerate(sessions):
            q = queues[idx]
            assert await q.drain(client(reg, sid)) == {"SENT": 1}
            assert q.row("max:42")["telegram_id"] == 501 + idx
            q.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_bad_capsule_key_cannot_impersonate_second_tenant(setup):
    reg, _, ledger, server, sessions = setup
    await server.start()
    try:
        wrong = GatewayEgressIpcClient(
            sessions[1][0], server.path, ipc_key_path(reg, sessions[0][0])
        )
        with pytest.raises((OSError, ValueError, asyncio.IncompleteReadError)):
            await wrong.enqueue("foreign:1", "not allowed")
        assert ledger.counts() == {}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_gateway_double_binding_refused(setup):
    reg, _, ledger, server, _ = setup
    await server.start()
    try:
        another = GatewayEgressIpcServer(reg, ledger)
        with pytest.raises(RuntimeError, match="double owner"):
            await another.start()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_lost_ack_reconciles_idempotently_without_second_send(setup):
    reg, _, ledger, server, sessions = setup
    sid = sessions[0][0]
    class LostReply(GatewayEgressIpcClient):
        async def enqueue(self, key, text):
            await super().enqueue(key,text)
            raise asyncio.TimeoutError("signed ack lost after gateway commit")

    await server.start()
    queue = worker_queue(reg,sid)
    try:
        queue.stage("max:lost-ack", "private synthetic event")
        client_lost = LostReply(
            sid,server.path,ipc_key_path(reg,sid)
        )
        assert await queue.reconcile_one(client_lost,"max:lost-ack") == "PENDING"
        assert ledger.counts() == {"QUEUED": 1}
        assert await queue.reconcile_one(client(reg,sid),"max:lost-ack") == "ACCEPTED"
        bot = SimpleNamespace(send_message=AsyncMock(
            return_value=SimpleNamespace(message_id=734)
        ))
        assert await ledger.drain(bot) == {"SENT":1}
        assert await queue.reconcile_one(client(reg,sid),"max:lost-ack") == "SENT"
        assert queue.row("max:lost-ack")["telegram_id"] == 734
        bot.send_message.assert_awaited_once()
    finally:
        queue.close()
        await server.stop()


@pytest.mark.asyncio
async def test_revocation_prevents_worker_delivery_and_no_bot_call(setup):
    reg,plane,ledger,server,sessions=setup
    sid,_,forum=sessions[0]
    await server.start()
    queue=worker_queue(reg,sid)
    try:
        queue.stage("max:revoked", "not allowed")
        plane.disable_forum(forum)
        # The gateway rejects route already at enqueue time, and no event
        # can reach its central Bot API queue.
        assert await queue.reconcile_one(client(reg,sid),"max:revoked")=="PENDING"
        assert ledger.counts()=={}
        bot=SimpleNamespace(send_message=AsyncMock())
        assert await ledger.drain(bot)=={}
        bot.send_message.assert_not_awaited()
    finally:
        queue.close()
        await server.stop()


@pytest.mark.asyncio
async def test_no_token_or_chat_id_leaks_into_worker_ipc_frames(setup):
    reg,_,ledger,server,sessions=setup
    sid=sessions[0][0]
    await server.start()
    try:
        worker=client(reg,sid)
        assert await worker.enqueue("max:token-safe","synthetic") == {"status":"QUEUED"}
        row=ledger.row(sid,"max:token-safe")
        assert row is not None
        assert "synthetic" in row["text"]
        assert "TG_BOT_TOKEN" not in worker.__dict__
        assert all("bot_token" not in field for field in row)
        answer=await worker.status("max:token-safe")
        assert answer=={"status":"QUEUED"}
    finally:
        await server.stop()


def test_shared_p22b_mode_cannot_start_legacy_personal_worker(setup):
    reg,_,_,_,sessions=setup
    sid=sessions[0][0]
    env=build_worker_env(reg,sid,{})
    assert env["MAX2TG_TELEGRAM_MODE"]=="shared"
    assert "TG_BOT_TOKEN" not in env
    assert env["REPLY_ENABLED"]=="false"
    assert reg.credentials(sid)["TG_CHAT_ID"]==str(sessions[0][2])


def test_worker_sqlite_outbox_is_private_and_immutable(setup):
    reg,_,_,_,sessions=setup
    sid=sessions[0][0]
    worker=worker_queue(reg,sid)
    try:
        assert worker.stage("max:stable","first")== "PENDING"
        assert worker.stage("max:stable","first")== "PENDING"
        with pytest.raises(ValueError,match="mutated"):
            worker.stage("max:stable","changed")
        assert worker.row("max:stable")["status"]=="UNKNOWN"
        assert worker.path.stat().st_mode & 0o077 == 0
    finally:
        worker.close()


class FakeMaxClient:
    def __init__(self, *, authorized=True, error=None):
        self._authorized=authorized
        self.error=error
        self.sent=[]

    async def send_message(self,chat_id,text,**kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_id,text,kwargs))
        return {"message":{"id":str(1000+len(self.sent))}}


def _telegram_update(update_id,actor,forum,topic,text):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id+100,
            "from": {"id":actor},
            "chat": {"id":forum},
            "message_thread_id":topic,
            "text":text,
        },
    }


@pytest.mark.asyncio
async def test_shared_gateway_ingress_to_two_workers_and_max(tmp_path,setup):
    from app.telegram_gateway import TelegramGatewayEngine
    from app.telegram_inbox import TelegramInbox
    from app.telegram_ipc import CapsuleIpcServer
    from app.capsule_shared_ingress import SharedCapsuleTextIngress
    from app.topics import TopicStore
    from app.tg_outbox import TelegramOutbound

    reg,plane,_,_,sessions=setup
    inbox=TelegramInbox(reg.root/"inbox.sqlite3",plane)
    workers=[]
    senders=[]
    try:
        for i,(sid,_,_) in enumerate(sessions):
            folder=reg.capsule_dir(sid)/"state"
            topic=TopicStore(str(folder/"topics.json"),strict=True)
            topic.set_topic(7000+i,100+i,"synthetic")
            out=TelegramOutbound(folder/"tg-outbound.sqlite3",topic)
            max_client=FakeMaxClient()
            worker=CapsuleIpcServer(
                reg,sid,SharedCapsuleTextIngress(max_client,topic,out)
            )
            await worker.start()
            workers.append((worker,out))
            senders.append(max_client)
        engine=TelegramGatewayEngine(plane,inbox)
        for i,(_,actor,forum) in enumerate(sessions):
            msg=_telegram_update(610+i,actor,forum,100+i,f"message {i}")
            assert await engine.accept(msg)=="ACKED"
            assert await engine.accept(msg)=="ACKED"
        assert [len(x.sent) for x in senders]==[1,1]
        assert [x.sent[0][0] for x in senders]==[7000,7001]
        assert engine.inbox.counts()=={"ACKED":2}
        assert await engine.accept(
            _telegram_update(613,202,sessions[0][2],100,"foreign")
        )=="DENIED"
        assert [len(x.sent) for x in senders]==[1,1]
    finally:
        for worker,out in workers:
            await worker.stop()
            out.close()
        inbox.close()


@pytest.mark.asyncio
async def test_definitely_offline_max_worker_preserves_gateway_received(setup):
    from app.telegram_gateway import TelegramGatewayEngine
    from app.telegram_inbox import TelegramInbox
    from app.telegram_ipc import CapsuleIpcServer
    from app.capsule_shared_ingress import SharedCapsuleTextIngress
    from app.topics import TopicStore
    from app.tg_outbox import TelegramOutbound
    reg,plane,_,_,sessions=setup
    sid,actor,forum=sessions[0]
    folder=reg.capsule_dir(sid)/"state"
    topics=TopicStore(str(folder/"topics.json"),strict=True)
    topics.set_topic(7001,101,"synthetic")
    ledger=TelegramOutbound(folder/"tg-outbound.sqlite3",topics)
    max_client=FakeMaxClient(authorized=False)
    server=CapsuleIpcServer(
        reg,sid,SharedCapsuleTextIngress(max_client,topics,ledger)
    )
    inbox=TelegramInbox(reg.root/"inbox.sqlite3",plane)
    await server.start()
    try:
        engine=TelegramGatewayEngine(plane,inbox)
        update=_telegram_update(618,actor,forum,101,"pending offline")
        assert await engine.accept(update)=="RECEIVED"
        assert max_client.sent==[]
        max_client._authorized=True
        assert await engine.drain()=={"ACKED":1}
        assert len(max_client.sent)==1
    finally:
        await server.stop()
        ledger.close()
        inbox.close()


@pytest.mark.asyncio
async def test_ambiguous_max_reply_never_replayed_by_gateway(setup):
    from app.telegram_gateway import TelegramGatewayEngine
    from app.telegram_inbox import TelegramInbox
    from app.telegram_ipc import CapsuleIpcServer
    from app.capsule_shared_ingress import SharedCapsuleTextIngress
    from app.topics import TopicStore
    from app.tg_outbox import TelegramOutbound
    reg,plane,_,_,sessions=setup
    sid,actor,forum=sessions[0]
    folder=reg.capsule_dir(sid)/"state"
    topics=TopicStore(str(folder/"topics.json"),strict=True)
    topics.set_topic(7001,101,"synthetic")
    ledger=TelegramOutbound(folder/"tg-outbound.sqlite3",topics)
    max_client=FakeMaxClient(error=TimeoutError("uncertain outbound MAX"))
    server=CapsuleIpcServer(
        reg,sid,SharedCapsuleTextIngress(max_client,topics,ledger)
    )
    inbox=TelegramInbox(reg.root/"inbox.sqlite3",plane)
    await server.start()
    try:
        engine=TelegramGatewayEngine(plane,inbox)
        msg=_telegram_update(622,actor,forum,101,"uncertain")
        assert await engine.accept(msg)=="UNKNOWN"
        assert inbox.ready()==[]
        assert ledger.counts()=={"UNKNOWN":1}
        assert await engine.drain()=={}
    finally:
        await server.stop()
        ledger.close()
        inbox.close()


def test_shared_supervisor_does_not_boot_legacy_polling(setup):
    from app.capsule_supervisor import CapsuleSupervisor
    reg,_,_,_,sessions=setup
    root=Path(__file__).resolve().parents[1]
    supervisor=CapsuleSupervisor(reg,root,clock=lambda:100.)
    try:
        supervisor.tick()
        assert supervisor._children=={}
        for sid,_,_ in sessions:
            assert reg.get(sid)["status"]=="ERROR"
            assert reg.get(sid)["last_error"]=="SHARED_RUNTIME_NOT_READY"
    finally:
        supervisor.close()


def test_shared_worker_entry_rejects_dedicated_bot_start(setup,monkeypatch):
    from app.capsule_worker import main
    reg,_,_,_,sessions=setup
    sid=sessions[0][0]
    env=build_worker_env(reg,sid,{})
    monkeypatch.setenv("MAX2TG_CAPSULE_LOCK",env["MAX2TG_CAPSULE_LOCK"])
    monkeypatch.setenv("MAX2TG_TELEGRAM_MODE","shared")
    with pytest.raises(SystemExit,match="cannot start dedicated"):
        main()
    assert not Path(env["MAX2TG_CAPSULE_LOCK"]).exists()


def test_shared_rejects_proxy_credentials_in_private_file(setup):
    reg,_,_,_,sessions=setup
    sid=sessions[0][0]
    reg.set_active(sid,False)
    credential=reg.credentials(sid)
    credential["TG_PROXY"]="http://synthetic:1234"
    with pytest.raises(ValueError, match="bot or proxy secrets"):
        reg.configure(sid,credential,telegram_mode="shared")
    assert "TG_PROXY" not in reg.credentials(sid)


def test_dedicated_database_schema_migrates_to_explicit_mode(tmp_path):
    reg=CapsuleRegistry(tmp_path/"legacy")
    owner=reg.create_account()
    sid=reg.create_capsule(owner)
    reg.configure(sid,{
        "MAX_TOKEN":"synthetic",
        "MAX_DEVICE_ID":"synthetic",
        "TG_BOT_TOKEN":"synthetic:legacy-bot",
        "TG_CHAT_ID":"-1001",
        "TG_ADMIN_USER_ID":"123",
    })
    reg.close()
    db=tmp_path/"legacy"/"capsules.sqlite3"
    import sqlite3
    connection=sqlite3.connect(db)
    connection.execute("ALTER TABLE capsules DROP COLUMN telegram_mode")
    connection.commit()
    connection.close()
    after=CapsuleRegistry(tmp_path/"legacy")
    try:
        assert after.get(sid)["telegram_mode"]=="dedicated"
        assert after.credentials(sid)["TG_BOT_TOKEN"]=="synthetic:legacy-bot"
    finally:
        after.close()
