"""Telegram→MAX crash recovery and idempotency tests; no live messages."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import sqlite3
import pytest

from app.tg_outbox import TelegramOutbound
from app.topics import TopicStore
from app.tg_handler import (_on_topic_message,TG_OUTBOUND_KEY,
    SUPERGROUP_KEY, TOPIC_STORE_KEY, MAX_CLIENT_KEY)
from tests.test_tg_handler import _make_update,_make_context,_make_topic_store


def env(tmp_path):
    store=TopicStore(str(tmp_path/"topics.json"))
    store.set_topic(42,10,"Museum")
    return store,TelegramOutbound(tmp_path/"tg-outbound.sqlite3",store)


def payload(text="Хлеб"):
    return {"text":text,"elements":[{"type":"STRONG","from":0,"length":4}],
            "link":None}


def mock_client(result=None):
    client=SimpleNamespace(
        _authorized=True,send_message=AsyncMock(return_value=(
            result if result is not None else {"message":{"id":"max123"}}
        )))
    return client


async def test_persist_before_network_and_confirm_mapping(tmp_path):
    store,ledger=env(tmp_path)
    client=mock_client()
    assert ledger.enqueue(-10088,1,42,payload())=="RECEIVED"
    assert ledger.row(-10088,1)["status"]=="RECEIVED"
    assert await ledger.deliver_one(client,-10088,1)=="SENT"
    assert ledger.row(-10088,1)["max_id"]=="max123"
    assert store.tg_for_max_message(42,"max123")==1
    assert await ledger.deliver_one(client,-10088,1)=="SENT"
    assert client.send_message.await_count==1
    ledger.close()


async def test_uncertain_timeout_is_not_resent(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,7,42,payload())
    client=mock_client()
    client.send_message.side_effect=TimeoutError("uncertain")
    with pytest.raises(TimeoutError):
        await ledger.deliver_one(client,-10088,7)
    assert ledger.row(-10088,7)["status"]=="UNKNOWN"
    ledger.close()
    again=TelegramOutbound(tmp_path/"tg-outbound.sqlite3",store)
    client.send_message=AsyncMock(return_value={"message":{"id":"later"}})
    assert await again.deliver_one(client,-10088,7)=="UNKNOWN"
    assert client.send_message.await_count==0
    assert again.pending()==[]
    again.close()


def test_inflight_crash_marked_unknown_on_restart(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,9,42,payload())
    ledger.db.execute("UPDATE outbound SET status='SENDING'")
    ledger.close()
    again=TelegramOutbound(tmp_path/"tg-outbound.sqlite3",store)
    assert again.row(-10088,9)["status"]=="UNKNOWN"
    again.close()


async def test_offline_is_received_then_drained_once(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,11,42,payload())
    client=mock_client()
    client._authorized=False
    assert await ledger.deliver_one(client,-10088,11)=="RECEIVED"
    client.send_message.assert_not_awaited()
    client._authorized=True
    assert (await ledger.drain(
        client,is_allowed=lambda cid:store.get_topic(cid) is not None)
    )=={"SENT":1}
    assert client.send_message.await_count==1
    ledger.close()


async def test_paused_keeps_pending_until_resume(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,12,42,payload())
    client=mock_client()
    assert await ledger.deliver_one(client,-10088,12,is_allowed=lambda _:False)=="PAUSED"
    assert ledger.pending()[0]["status"]=="RECEIVED"
    client.send_message.assert_not_awaited()
    assert await ledger.deliver_one(client,-10088,12,is_allowed=lambda _:True)=="SENT"
    ledger.close()


async def test_incorrect_duplicate_update_refuses_modified_payload(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,1,42,payload())
    with pytest.raises(ValueError):
        ledger.enqueue(-10088,1,42,payload("changed"))
    assert ledger.row(-10088,1)["status"]=="UNKNOWN"
    ledger.close()


async def test_explicit_max_rejection_stays_failed(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,1,42,payload())
    client=mock_client({"_max_error":{"message":"bad"}})
    assert await ledger.deliver_one(client,-10088,1)=="FAILED"
    assert await ledger.deliver_one(client,-10088,1)=="FAILED"
    assert client.send_message.await_count==1
    ledger.close()


async def test_missing_ack_id_is_unknown(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,1,42,payload())
    client=mock_client({"ok":True})
    assert await ledger.deliver_one(client,-10088,1)=="UNKNOWN"
    ledger.close()


def test_sent_ack_recovers_json_mapping_on_restart(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,15,42,payload())
    ledger.db.execute(
        "UPDATE outbound SET status='SENT',max_id='abc' "
        "WHERE telegram_id=15")
    ledger.close()
    assert store.tg_for_max_message(42,"abc") is None
    again=TelegramOutbound(tmp_path/"tg-outbound.sqlite3",store)
    assert store.tg_for_max_message(42,"abc")==15
    again.close()


async def test_same_telegram_text_twice_calls_max_once(tmp_path):
    store,ledger=env(tmp_path)
    client=mock_client()
    update=_make_update("Хлеб",thread_id=10)
    ctx=_make_context(max_client=client,topic_store=store)
    ctx.bot_data[SUPERGROUP_KEY]=-10088
    ctx.bot_data[TG_OUTBOUND_KEY]=ledger
    await _on_topic_message(update,ctx)
    await _on_topic_message(update,ctx)
    assert client.send_message.await_count==1
    assert ledger.counts()=={"SENT":1}
    assert update.message.set_reaction.await_count==2
    ledger.close()


async def test_outbound_errors_surface_without_repeat(tmp_path):
    store,ledger=env(tmp_path)
    client=mock_client()
    client.send_message.side_effect=TimeoutError()
    update=_make_update("Хлеб",thread_id=10)
    ctx=_make_context(max_client=client,topic_store=store)
    ctx.bot_data[SUPERGROUP_KEY]=-10088
    ctx.bot_data[TG_OUTBOUND_KEY]=ledger
    await _on_topic_message(update,ctx)
    assert ledger.counts()=={"UNKNOWN":1}
    await _on_topic_message(update,ctx)
    assert client.send_message.await_count==1
    assert update.message.reply_text.await_count==2
    ledger.close()


def test_persistent_db_private_integrity(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,1,42,payload())
    assert ledger.db.execute("PRAGMA quick_check").fetchone()[0]=="ok"
    import stat
    assert stat.S_IMODE((tmp_path/"tg-outbound.sqlite3").stat().st_mode)==0o600
    ledger.close()
