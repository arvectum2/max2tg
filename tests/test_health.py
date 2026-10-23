"""P0.4 watchdog: real status, bounded recovery and private health reporting."""
import asyncio
import json
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.health import HealthSupervisor, status_text, MIN_DISK_FREE_BYTES
from app.max_client import MaxClient, OpCode, MaxMessage
from app.outbox import OutboxStore
from app.topics import TopicStore
from app.tg_handler import (
    _cmd_status, _on_health_callback, MAX_CLIENT_KEY, TOPIC_STORE_KEY,
    ADMIN_USER_KEY, ALLOWED_USERS_KEY,
)


def supervisor(tmp_path, *, authorized=True, polling=True):
    store=TopicStore(str(tmp_path/"topics.json"))
    store.set_topic(42,777,"Тест")
    outbox=OutboxStore(str(tmp_path/"outbox.sqlite3"))
    client=MaxClient("token","device")
    client._ws=SimpleNamespace(closed=False,close=AsyncMock())
    client._authorized=authorized
    client._last_rx_monotonic=time.monotonic()
    sender=MagicMock()
    sender.chat_id="-100123"
    sender.bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=11))
    updater=SimpleNamespace(running=polling,start_polling=AsyncMock())
    tg_app=SimpleNamespace(running=True,updater=updater)
    h=HealthSupervisor(client,sender,outbox,store,tg_app,str(tmp_path))
    return h,outbox,client,sender,tg_app


def test_healthy_report_only_counts_and_hides_content(tmp_path):
    h,db,c,s,a=supervisor(tmp_path)
    report=h.snapshot()
    assert report["max"]["state"]=="ONLINE"
    assert report["telegram"]["polling"] is True
    assert report["topics"]==1
    assert report["disk"]["ok"]
    assert not report["issues"]
    text=json.dumps(report,ensure_ascii=False)
    assert "token" not in text and "Тест" not in text
    assert "ONLINE" in status_text(report)
    db.close()


def test_reauth_and_queued_items_flagged_separately(tmp_path):
    h,db,c,s,a=supervisor(tmp_path)
    c._auth_required=True
    item=MaxMessage(chat_id=42,message_id="abc",text="private message")
    key=db.enqueue(item)
    db.db.execute("UPDATE jobs SET created=?,next_attempt=0 WHERE key=?",
                  (time.time()-600,key))
    snapshot=h.snapshot()
    assert snapshot["max"]["state"]=="REAUTH_REQUIRED"
    assert "REAUTH_REQUIRED" in snapshot["issues"]
    assert "QUEUE_DELAY" in snapshot["issues"]
    assert snapshot["outbox"]["oldest_due_seconds"]>=590
    assert "private message" not in status_text(snapshot)
    db.close()


async def test_stale_socket_reconnect_has_cooldown(tmp_path):
    h,db,c,s,a=supervisor(tmp_path)
    c._last_rx_monotonic=time.monotonic()-400
    assert h.snapshot()["max"]["state"]=="STALLED"
    await h.repair(h.snapshot())
    await h.repair(h.snapshot())
    c._ws.close.assert_awaited_once()
    db.close()


async def test_polling_repair_does_not_drop_updates(tmp_path):
    h,db,c,s,a=supervisor(tmp_path,polling=False)
    bad=h.snapshot()
    assert "TELEGRAM_POLLING" in bad["issues"]
    await h.repair(bad)
    a.updater.start_polling.assert_awaited_once()
    assert a.updater.start_polling.await_args.kwargs["drop_pending_updates"] is False
    await h.repair(bad)
    assert a.updater.start_polling.await_count==1
    db.close()


async def test_reauth_is_not_automatically_rerolled_into_new_sms_login(tmp_path):
    h,db,c,s,a=supervisor(tmp_path)
    c._auth_required=True
    c._authorized=False
    c._ws.close.reset_mock()
    await h.repair(h.snapshot())
    c._ws.close.assert_not_awaited()
    db.close()


async def test_alerts_after_grace_and_consecutive_checks(tmp_path):
    h,db,c,s,a=supervisor(tmp_path)
    h.started=time.monotonic()-200
    c._auth_required=True
    report=h.snapshot()
    await h.notify(report)
    await h.notify(report)
    s.bot.send_message.assert_not_awaited()
    await h.notify(report)
    s.bot.send_message.assert_awaited_once()
    assert (tmp_path/"health-alerts.json").exists()
    assert "REAUTH_REQUIRED" in json.loads(
        (tmp_path/"health-alerts.json").read_text())
    await h.notify(report)
    assert s.bot.send_message.await_count==1
    db.close()


async def test_atomic_health_file_private_and_recoverable(tmp_path):
    h,db,c,s,a=supervisor(tmp_path)
    h._write_atomic(h.path,h.snapshot())
    saved=json.loads(h.path.read_text())
    assert saved["max"]["state"]=="ONLINE"
    assert stat.S_IMODE(h.path.stat().st_mode)==0o600
    assert list(tmp_path.glob(".max2tg-health*"))==[]
    h._write_atomic(h.path,{"max":{"state":"RECONNECTING"}})
    assert json.loads(h.path.read_text())["max"]["state"]=="RECONNECTING"
    db.close()


def test_problematic_outbox_and_mutation_counts_reported_without_content(tmp_path):
    h,db,c,s,a=supervisor(tmp_path)
    msg=MaxMessage(chat_id=42,message_id="zz",text="Sensitive message")
    key=db.enqueue(msg)
    db._set_job(key,"UNKNOWN")
    db.db.execute(
        "CREATE TABLE message_mutations "
        "(key TEXT, state TEXT)")
    db.db.execute("INSERT INTO message_mutations VALUES('x','BLOCKED')")
    r=h.snapshot()
    assert r["outbox"]["counts"]["UNKNOWN"]==1
    assert r["mutations"]["BLOCKED"]==1
    assert "OUTBOX_UNKNOWN" in r["issues"]
    assert "MUTATION_BLOCKED" in r["issues"]
    assert "Sensitive message" not in status_text(r)
    db.close()


async def test_max_auth_status_monotonic_and_logout():
    c=MaxClient("t","d")
    c._last_rx_monotonic=None
    c._on_ready_cb=AsyncMock()
    await c._handle({"opcode":OpCode.AUTH_SNAPSHOT,"cmd":1,
                     "payload":{"profile":{"id":17}}})
    assert c._authorized is True
    assert c._last_rx_monotonic is not None
    await c._handle({"opcode":OpCode.LOGOUT,"cmd":0,"payload":{}})
    assert c._auth_required is True and c._authorized is False


async def test_admin_only_status_command_does_not_disclose_to_strangers(tmp_path):
    h,db,c,s,a=supervisor(tmp_path)
    c.health=h
    update=MagicMock()
    update.message.reply_text=AsyncMock()
    update.effective_user.id=222
    ctx=SimpleNamespace(bot_data={
        ADMIN_USER_KEY:111,ALLOWED_USERS_KEY:frozenset({111,222}),
        MAX_CLIENT_KEY:c,
    })
    await _cmd_status(update,ctx)
    update.message.reply_text.assert_not_awaited()
    update.effective_user.id=111
    await _cmd_status(update,ctx)
    update.message.reply_text.assert_awaited_once()
    assert "ONLINE" in update.message.reply_text.await_args.args[0]
    db.close()


async def test_admin_only_reauth_instructions_without_exposing_sms(tmp_path):
    h,db,c,s,a=supervisor(tmp_path)
    update=MagicMock()
    update.effective_user.id=111
    update.callback_query.data="health:reauth"
    update.callback_query.answer=AsyncMock()
    update.callback_query.message.reply_text=AsyncMock()
    ctx=SimpleNamespace(bot_data={
        ADMIN_USER_KEY:111,ALLOWED_USERS_KEY:frozenset({111}),
        MAX_CLIENT_KEY:c,
    })
    await _on_health_callback(update,ctx)
    update.callback_query.message.reply_text.assert_awaited_once()
    msg=update.callback_query.message.reply_text.await_args.args[0]
    assert "Chrome" in msg
    db.close()
