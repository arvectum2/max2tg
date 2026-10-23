"""Durable Telegram outbox invariants and recovery tests (P0.2)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import TimedOut, RetryAfter, Forbidden

from app.outbox import OutboxStore
from app.max_client import MaxMessage
from app.max_listener import create_max_client
from app.tg_sender import TelegramSender
from app.topics import TopicStore


def fixture(tmp_path):
    state = TopicStore(str(tmp_path/"topics.json"))
    state.set_topic(42,777,"Получатель")
    sender = TelegramSender.__new__(TelegramSender)
    sender._topics = state
    sender._chat_id = "-100"
    sender._topic_lock = asyncio.Lock()
    sender._bot = MagicMock()
    outbox=OutboxStore(str(tmp_path/"outbox.sqlite3"))
    sender.outbox=outbox
    client=create_max_client("token", "device", sender)
    client._my_id=100
    resolver=client.resolver
    resolver.chats_raw={42:{"id":42,"type":"DIALOG","status":"ACTIVE",
                           "participants":{"100":1,"200":1}}}
    resolver.chats={42:"DM:200"}
    resolver.chat_types={42:"DIALOG"}
    resolver._my_id=100
    resolver.users[200]="Друг"
    resolver.resolve_user=AsyncMock(return_value="Друг")
    return client,sender,state,outbox


def event(mid="10", text="привет"):
    return MaxMessage(chat_id=42,sender_id=200,
                      message_id=mid,text=text,
                      timestamp=1792700000000)


async def test_live_event_persists_and_marks_sent(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=501))
    await client._on_message_cb(event())
    job=db.job(db.job_key(42,"10"))
    assert job["status"]=="SENT"
    assert state.tg_for_max_message(42,"10")==501
    assert db.counts()=={"SENT":1}
    await client._on_message_cb(event())
    sender._bot.send_message.assert_awaited_once()


async def test_long_text_resumes_without_resending_first_part(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    large = "<b>" + "Ж"*8000 + "</b>"
    # A first success, then definite RetryAfter on the second part.
    sender._bot.send_message=AsyncMock(side_effect=[
        SimpleNamespace(message_id=501), RetryAfter(1)])
    msg=event("long",large)
    # Sender string is escaped into HTML by the listener: still splits.
    await client._on_message_cb(msg)
    key=db.job_key(42,"long")
    assert db.job(key)["status"]=="RETRY"
    assert db._part(key,0)["state"]=="SENT"
    assert db._part(key,1)["state"]=="RETRY"
    # Advance the due timestamp, simulating the rate-limit expiry.
    db.db.execute("UPDATE jobs SET next_attempt=0 WHERE key=?", (key,))
    sender._bot.send_message=AsyncMock(side_effect=[
        SimpleNamespace(message_id=502),SimpleNamespace(message_id=503)])
    await client._on_message_cb(msg)
    assert db.job(key)["status"]=="SENT"
    assert state.tg_for_max_message(42,"long")==501
    assert db._part(key,0)["tg_id"]==501
    assert sender._bot.send_message.await_count==2  # only unfinished parts


async def test_network_timeout_never_resends_automatically(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    sender._bot.send_message=AsyncMock(side_effect=TimedOut())
    msg=event("timeout")
    await client._on_message_cb(msg)
    key=db.job_key(42,"timeout")
    assert db.job(key)["status"]=="UNKNOWN"
    assert db._part(key,0)["state"]=="UNKNOWN"
    assert state.tg_for_max_message(42,"timeout") is None
    assert not db.recoverable()
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=9))
    await client._on_message_cb(msg)
    sender._bot.send_message.assert_not_awaited()


async def test_restart_with_send_in_progress_becomes_unknown(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=event("crash")
    key=db.enqueue(msg)
    db._insert_part(key,0,"send_message","digest",{"text":"abc"},None)
    db._update_part(key,0,"SENDING")
    db._set_job(key,"SENDING")
    db.close()
    restored=OutboxStore(str(tmp_path/"outbox.sqlite3"))
    assert restored.job(key)["status"]=="UNKNOWN"
    assert restored._part(key,0)["state"]=="UNKNOWN"
    assert not restored.recoverable()
    restored.close()


async def test_restart_before_first_send_is_safe_to_resume(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    key=db.enqueue(event("not-started"))
    db._set_job(key,"SENDING")
    db.close()
    restored=OutboxStore(str(tmp_path/"outbox.sqlite3"))
    assert restored.job(key)["status"]=="RETRY"
    assert len(restored.recoverable())==1
    restored.close()


async def test_definitive_forbidden_is_terminal(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    sender._bot.send_message=AsyncMock(side_effect=Forbidden("no access"))
    await client._on_message_cb(event("forbidden"))
    assert db.job(db.job_key(42,"forbidden"))["status"]=="FAILED"
    assert db.counts()=={"FAILED":1}


async def test_media_payload_stored_and_cleared_after_ack(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=event("photo")
    msg.attaches=[{"_type":"PHOTO","url":"https://i.oneme.ru/photo"}]
    client.download_file=AsyncMock(return_value=b"photo-bytes")
    sender._bot.send_photo=AsyncMock(return_value=SimpleNamespace(message_id=54))
    await client._on_message_cb(msg)
    key=db.job_key(42,"photo")
    assert db.job(key)["status"]=="SENT"
    assert db._part(key,0)["method"]=="send_photo"
    assert db._part(key,0)["media"] is None
    assert state.tg_for_max_message(42,"photo")==54


async def test_unsent_media_is_stored_during_rate_limit(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=event("photo-retry")
    msg.attaches=[{"_type":"PHOTO","url":"https://i.oneme.ru/photo"}]
    client.download_file=AsyncMock(return_value=b"real-bytes")
    sender._bot.send_photo=AsyncMock(side_effect=RetryAfter(1))
    await client._on_message_cb(msg)
    key=db.job_key(42,"photo-retry")
    assert db._part(key,0)["media"]==b"real-bytes"
    assert db.job(key)["status"]=="RETRY"


async def test_disconnected_backlog_replayed_when_worker_available(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=event("queued")
    db.enqueue(msg)
    assert len(db.recoverable())==1
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=89))
    for pending in db.recoverable():
        await client._on_message_cb(pending)
    assert db.job(db.job_key(42,"queued"))["status"]=="SENT"
    assert state.tg_for_max_message(42,"queued")==89


async def test_outbox_duplicate_event_is_unique_by_max_id(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    key=db.enqueue(event("x"))
    assert db.enqueue(event("x", text="amended"))==key
    rows=db.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert rows==1
    assert db.job(key)["status"]=="RECEIVED"


async def test_media_retry_does_not_need_expired_max_download_url(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=event("photo-resume")
    msg.attaches=[{"_type":"PHOTO","url":"https://i.oneme.ru/expiring-token"}]
    client.download_file=AsyncMock(return_value=b"immutable-bytes")
    sender._bot.send_photo=AsyncMock(side_effect=RetryAfter(1))
    await client._on_message_cb(msg)
    key=db.job_key(42,msg.message_id)
    assert db._part(key,0)["media"]==b"immutable-bytes"
    db.db.execute("UPDATE jobs SET next_attempt=0 WHERE key=?", (key,))
    client.download_file.reset_mock()
    client.download_file.return_value=None
    sender._bot.send_photo=AsyncMock(return_value=SimpleNamespace(message_id=909))
    await client._on_message_cb(msg)
    client.download_file.assert_not_awaited()
    sender._bot.send_photo.assert_awaited_once()
    assert state.tg_for_max_message(42,msg.message_id)==909


async def test_sqlite_receipt_repairs_lost_topic_json_mapping(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=808))
    msg=event("receipt")
    await client._on_message_cb(msg)
    key=db.job_key(42,"receipt")
    assert db.job(key)["tg_primary"]==808
    # Simulate a crash after the SQLite ack but before durable JSON snapshot.
    stale_path=tmp_path/"stale.json"
    old=TopicStore(str(stale_path))
    old.set_topic(42,777,"Получатель")
    assert old.tg_for_max_message(42,"receipt") is None
    assert db.reconcile_mappings(old)==1
    assert db.reconcile_mappings(old)==0
    assert TopicStore(str(stale_path)).tg_for_max_message(42,"receipt")==808


async def test_three_part_text_crash_after_two_confirmed_parts_is_unknown(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    long=event("uncertain-long", "🌉"*6000)
    sender._bot.send_message=AsyncMock(side_effect=[
        SimpleNamespace(message_id=1),SimpleNamespace(message_id=2),TimedOut()])
    await client._on_message_cb(long)
    key=db.job_key(42,"uncertain-long")
    assert db.job(key)["status"]=="UNKNOWN"
    assert db._part(key,0)["state"]=="SENT"
    assert db._part(key,1)["state"]=="SENT"
    assert db._part(key,2)["state"]=="UNKNOWN"
    assert state.tg_for_max_message(42,"uncertain-long") is None
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=3))
    await client._on_message_cb(long)
    sender._bot.send_message.assert_not_awaited()


def test_sqlite_outbox_file_has_private_permissions(tmp_path):
    import stat
    db=OutboxStore(str(tmp_path/"private.sqlite3"))
    mode=stat.S_IMODE((tmp_path/"private.sqlite3").stat().st_mode)
    assert mode == 0o600
    db.close()


async def test_history_import_uses_durable_outbox(tmp_path):
    from app.max_listener import import_recent_history
    client,sender,state,db=fixture(tmp_path)
    client.fetch_history=AsyncMock(return_value={"messages":[
        {"id":"history-durable","sender":200,"time":1792700000000,"text":"архив"}
    ]})
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=302))
    assert await import_recent_history(client,sender,client.resolver,42,777,20)==1
    key=db.job_key(42,"history-durable")
    assert db.job(key)["status"]=="SENT"
    assert state.tg_for_max_message(42,"history-durable")==302

async def test_previous_mapping_is_recorded_in_sqlite_without_resend(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=event('already-there')
    state.set_message(42,'already-there',901)
    sender._bot.send_message=AsyncMock()
    await client._on_message_cb(msg)
    receipt=db.job(db.job_key(42,'already-there'))
    assert receipt['status']=='SENT'
    assert receipt['tg_primary']==901
    sender._bot.send_message.assert_not_awaited()
