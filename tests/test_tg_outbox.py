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


def media_payload(kind="photo",size=10):
    return {"text":"Подпись","elements":[],"link":None,
            "media":{"kind":kind,"file_id":"TG-file-reference-1",
                     "filename":"photo.jpg" if kind=="photo" else "doc.txt",
                     "mime":"image/jpeg" if kind=="photo" else "text/plain",
                     "size":size}}


def tg_bot(data=b"image"):
    f=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(data)))
    return SimpleNamespace(get_file=AsyncMock(return_value=f))


async def test_photo_queued_before_download_then_uploaded_and_sent_once(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,71,42,media_payload())
    client=mock_client({"message":{"id":"MAX-media-71"}})
    client.upload_photo=AsyncMock(return_value={"_type":"PHOTO","photoId":3})
    bot=tg_bot(b"jpegbytes")
    assert await ledger.deliver_one(client,-10088,71,tg_bot=bot)=="SENT"
    client.upload_photo.assert_awaited_once_with(b"jpegbytes",chat_id=42)
    assert client.send_message.await_args.kwargs["attaches"]==[
        {"_type":"PHOTO","photoId":3}]
    assert ledger.row(-10088,71)["prepared"] is not None
    assert await ledger.deliver_one(client,-10088,71,tg_bot=bot)=="SENT"
    bot.get_file.assert_awaited_once()
    client.send_message.assert_awaited_once()
    ledger.close()


async def test_offline_media_persisted_and_deferred_until_reconnect(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,72,42,media_payload("document"))
    client=mock_client({"message":{"id":"doc72"}})
    client._authorized=False
    client.upload_file=AsyncMock(return_value={"_type":"FILE","fileId":8})
    bot=tg_bot(b"docbytes")
    assert await ledger.deliver_one(client,-10088,72,tg_bot=bot)=="RECEIVED"
    bot.get_file.assert_not_awaited()
    client._authorized=True
    assert await ledger.deliver_one(client,-10088,72,tg_bot=bot)=="SENT"
    assert client.upload_file.await_args.kwargs["filename"]=="doc.txt"
    ledger.close()


async def test_media_upload_failure_never_sends_chat_message(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,73,42,media_payload())
    client=mock_client()
    client.upload_photo=AsyncMock(return_value=None)
    bot=tg_bot()
    assert await ledger.deliver_one(client,-10088,73,tg_bot=bot)=="FAILED"
    client.send_message.assert_not_awaited()
    assert ledger.pending()==[]
    ledger.close()


async def test_media_send_timeout_unknown_does_not_redownload_on_restart(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,74,42,media_payload())
    client=mock_client()
    client.upload_photo=AsyncMock(return_value={"_type":"PHOTO","photoId":74})
    client.send_message.side_effect=TimeoutError()
    bot=tg_bot()
    with pytest.raises(TimeoutError):
        await ledger.deliver_one(client,-10088,74,tg_bot=bot)
    assert ledger.row(-10088,74)["status"]=="UNKNOWN"
    ledger.close()
    recovered=TelegramOutbound(tmp_path/"tg-outbound.sqlite3",store)
    client.send_message=AsyncMock()
    assert await recovered.deliver_one(client,-10088,74,tg_bot=bot)=="UNKNOWN"
    client.send_message.assert_not_awaited()
    bot.get_file.assert_awaited_once()
    recovered.close()


async def test_media_too_large_blocks_before_network_download(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,75,42,media_payload(size=26*1024*1024))
    client=mock_client()
    bot=tg_bot()
    assert await ledger.deliver_one(client,-10088,75,tg_bot=bot)=="FAILED"
    bot.get_file.assert_not_awaited()
    ledger.close()


async def test_voice_media_is_upload_audio(tmp_path):
    store,ledger=env(tmp_path)
    ledger.enqueue(-10088,76,42,media_payload("voice"))
    client=mock_client()
    client.upload_audio=AsyncMock(return_value={"_type":"FILE","fileId":76})
    assert await ledger.deliver_one(client,-10088,76,tg_bot=tg_bot())=="SENT"
    client.upload_audio.assert_awaited_once()
    ledger.close()


async def test_message_media_handler_calls_new_ledger_before_download(tmp_path):
    from app.tg_handler import _on_topic_media
    store,ledger=env(tmp_path)
    client=mock_client({"message":{"id":"max77"}})
    client.upload_photo=AsyncMock(return_value={"_type":"PHOTO","photoId":77})
    update=_make_update(text=None,thread_id=10)
    update.message.caption="подпись"
    update.message.caption_entities=[]
    update.message.photo=[SimpleNamespace(file_id="photo-A",file_size=5)]
    update.message.voice=None
    update.message.audio=None
    update.message.document=None
    update.message.video=None
    ctx=_make_context(max_client=client,topic_store=store)
    ctx.bot_data[SUPERGROUP_KEY]=-10088
    ctx.bot_data[TG_OUTBOUND_KEY]=ledger
    ctx.bot=tg_bot()
    await _on_topic_media(update,ctx)
    assert ledger.counts()=={"SENT":1}
    ctx.bot.get_file.assert_awaited_once_with("photo-A")
    client.send_message.assert_awaited_once()
    await _on_topic_media(update,ctx)
    ctx.bot.get_file.assert_awaited_once()
    ledger.close()


def test_upgrade_existing_text_only_db_adds_prepared_column(tmp_path):
    path=tmp_path/"tg-outbound.sqlite3"
    con=sqlite3.connect(path)
    con.executescript("""
       CREATE TABLE outbound (
        telegram_chat TEXT NOT NULL,telegram_id INTEGER NOT NULL,
        max_chat TEXT NOT NULL,payload TEXT NOT NULL,
        fingerprint TEXT NOT NULL,status TEXT NOT NULL,
        max_id TEXT,created REAL NOT NULL,updated REAL NOT NULL,
        PRIMARY KEY(telegram_chat,telegram_id));
       INSERT INTO outbound VALUES('-100',1,'42','{}','hash','RECEIVED',
                                   NULL,1,1);
    """)
    con.close()
    ledger=TelegramOutbound(path)
    columns={row["name"] for row in ledger.db.execute("PRAGMA table_info(outbound)")}
    assert "prepared" in columns
    assert ledger.row(-100,1)["status"]=="RECEIVED"
    ledger.close()
