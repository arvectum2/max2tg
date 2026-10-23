"""P1.2 media resilience, albums, exact receipts and safe fallbacks."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import json
import sqlite3

import pytest
from telegram.error import RetryAfter, TimedOut

from app.max_media import (
    photo_url, file_url, video_url, video_preview_url,
    media_filename, media_size,
)
from app.max_listener import _send_attach
from app.max_client import MaxMessage
from tests.test_outbox import fixture, event


def message(mid, attaches, text=""):
    m=event(mid,text)
    m.attaches=attaches
    return m


@pytest.mark.parametrize("obj,expected", [
    ({"url":"https://cdn.oneme.ru/image","baseUrl":"https://i.oneme.ru/hi"},
      "https://i.oneme.ru/hi"),
    ({"fileUrl":"https://cdn.oneme.ru/old"}, "https://cdn.oneme.ru/old"),
    ({"thumbnail":{"url":"https://i.oneme.ru/thumb"}},
      None),
])
def test_photo_url_fields(obj,expected):
    assert photo_url(obj)==expected


def test_files_from_max_history_keep_true_filename_and_url():
    attach={"_type":"FILE","fileName":"docs/report.pdf",
            "fileUrl":"https://file-ms.oneme.ru/abc","fileSize":1200}
    assert file_url(attach)=="https://file-ms.oneme.ru/abc"
    assert media_filename(attach)=="report.pdf"
    assert media_size(attach)==1200


def test_video_original_over_preview_without_accepting_playlist_or_external_page():
    assert video_url({"mp4Url":"https://cdn.oneme.ru/a.mp4",
                      "thumbnail":"https://cdn.oneme.ru/cover.jpg"})==(
        "https://cdn.oneme.ru/a.mp4")
    assert video_preview_url({"thumbnail":{"url":"https://cdn.oneme.ru/preview"}})==(
        "https://cdn.oneme.ru/preview")
    assert video_url({"url":"https://cdn.oneme.ru/playlist.m3u8"}) is None
    assert video_url({"url":"https://m.ok.ru/video/17"}) is None


async def test_single_video_uploads_original_not_preview(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=message("vid", [{"_type":"VIDEO",
        "mp4Url":"https://cdn.oneme.ru/clip.mp4",
        "thumbnail":"https://i.oneme.ru/cover.jpg"}])
    client.download_file=AsyncMock(return_value=b"fake-mp4")
    sender._bot.send_video=AsyncMock(return_value=SimpleNamespace(message_id=40))
    sender._bot.send_photo=AsyncMock()
    await client._on_message_cb(msg)
    sender._bot.send_video.assert_awaited_once()
    sender._bot.send_photo.assert_not_awaited()
    assert state.tg_for_max_message(42,"vid")==40
    assert db.job(db.job_key(42,"vid"))["status"]=="SENT"
    db.close()


async def test_unavailable_video_download_uses_explicit_preview(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=message("vid-prev",[{"_type":"VIDEO",
        "mp4Url":"https://cdn.oneme.ru/video.mp4",
        "thumbnail":{"url":"https://i.oneme.ru/prev.jpg"}}])
    client.download_file=AsyncMock(side_effect=[None,b"preview-bytes"])
    sender._bot.send_photo=AsyncMock(return_value=SimpleNamespace(message_id=44))
    await client._on_message_cb(msg)
    caption=sender._bot.send_photo.await_args.kwargs["caption"]
    assert "только превью" in caption and "оригинал недоступен" in caption
    assert state.tg_for_max_message(42,"vid-prev")==44
    db.close()


async def test_file_history_field_fileurl_and_name_are_preserved(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=message("pdf",[{"_type":"FILE","fileName":"museum.pdf",
        "fileUrl":"https://file-ms.oneme.ru/file","fileSize":888}])
    client.download_file=AsyncMock(return_value=b"%PDF-test")
    sender._bot.send_document=AsyncMock(return_value=SimpleNamespace(message_id=10))
    await client._on_message_cb(msg)
    assert sender._bot.send_document.await_args.kwargs["document"].filename=="museum.pdf"
    assert db.job(db.job_key(42,"pdf"))["status"]=="SENT"
    db.close()


async def test_audio_id_is_resolved_with_real_chat_and_token(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=message("voiceid",[{"_type":"AUDIO","audioId":123,
        "token":"signed","duration":45000}])
    client.download_audio_url=AsyncMock(return_value="https://au.oneme.ru/file.ogg")
    client.download_file=AsyncMock(return_value=b"OggS" + b"x"*20)
    sender._bot.send_voice=AsyncMock(return_value=SimpleNamespace(message_id=17))
    await client._on_message_cb(msg)
    client.download_audio_url.assert_awaited_once_with(
        123,42,"voiceid",token="signed")
    sender._bot.send_voice.assert_awaited_once()
    db.close()


async def test_photo_missing_url_produces_explicit_message_not_silent_drop(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=message("missing-photo",[{"_type":"PHOTO","id":14}])
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=99))
    await client._on_message_cb(msg)
    assert "ссылка недоступна" in sender._bot.send_message.await_args.kwargs["text"]
    assert state.tg_for_max_message(42,"missing-photo")==99
    db.close()


async def test_photo_album_sends_one_media_group_and_saves_all_receipts(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=message("album-3",[
        {"_type":"PHOTO","url":f"https://i.oneme.ru/{i}"}
        for i in range(3)
    ],"Наши фото")
    client.download_file=AsyncMock(
        side_effect=[b"photo-A",b"photo-B",b"photo-C"])
    sender._bot.send_media_group=AsyncMock(return_value=[
        SimpleNamespace(message_id=i) for i in (101,102,103)])
    await client._on_message_cb(msg)
    sender._bot.send_media_group.assert_awaited_once()
    kwargs=sender._bot.send_media_group.await_args.kwargs
    assert len(kwargs["media"])==3
    assert "Наши фото" in kwargs["media"][0].caption
    assert kwargs["media"][1].caption is None
    row=db._part(db.job_key(42,"album-3"),0)
    assert row["state"]=="SENT" and row["tg_id"]==101
    assert json.loads(row["tg_ids"])==[101,102,103]
    assert state.tg_for_max_message(42,"album-3")==101
    assert db.job(db.job_key(42,"album-3"))["status"]=="SENT"
    await client._on_message_cb(msg)
    assert sender._bot.send_media_group.await_count==1
    db.close()


async def test_media_group_rate_limit_persists_files_then_recovers_without_redownload(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=message("album-retry",[
        {"_type":"PHOTO","url":"https://i.oneme.ru/1"},
        {"_type":"PHOTO","url":"https://i.oneme.ru/2"}])
    client.download_file=AsyncMock(side_effect=[b"1",b"2"])
    sender._bot.send_media_group=AsyncMock(side_effect=RetryAfter(1))
    await client._on_message_cb(msg)
    key=db.job_key(42,"album-retry")
    assert db.job(key)["status"]=="RETRY"
    assert db._part(key,0)["media"] is not None
    db.db.execute("UPDATE jobs SET next_attempt=0 WHERE key=?",(key,))
    client.download_file.reset_mock()
    client.download_file.side_effect=RuntimeError("expired CDN link")
    sender._bot.send_media_group=AsyncMock(return_value=[
        SimpleNamespace(message_id=41),SimpleNamespace(message_id=42)])
    await client._on_message_cb(msg)
    client.download_file.assert_not_awaited()
    assert db.job(key)["status"]=="SENT"
    assert state.tg_for_max_message(42,"album-retry")==41
    db.close()


async def test_album_network_timeout_unknown_no_auto_repeat(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=message("album-timeout",[
        {"_type":"PHOTO","url":"https://i.oneme.ru/1"},
        {"_type":"PHOTO","url":"https://i.oneme.ru/2"}])
    client.download_file=AsyncMock(return_value=b"photo")
    sender._bot.send_media_group=AsyncMock(side_effect=TimedOut())
    await client._on_message_cb(msg)
    key=db.job_key(42,"album-timeout")
    assert db.job(key)["status"]=="UNKNOWN"
    sender._bot.send_media_group=AsyncMock()
    await client._on_message_cb(msg)
    sender._bot.send_media_group.assert_not_awaited()
    db.close()


async def test_album_remainder_after_photo_group_retries_without_resending_group(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=message("album-long-caption",[
        {"_type":"PHOTO","url":"https://i.oneme.ru/1"},
        {"_type":"PHOTO","url":"https://i.oneme.ru/2"}],"слово "*350)
    client.download_file=AsyncMock(return_value=b"photo")
    sender._bot.send_media_group=AsyncMock(return_value=[
        SimpleNamespace(message_id=77),SimpleNamespace(message_id=78)])
    sender._bot.send_message=AsyncMock(side_effect=RetryAfter(1))
    await client._on_message_cb(msg)
    key=db.job_key(42,"album-long-caption")
    assert db.job(key)["status"]=="RETRY"
    assert db._part(key,0)["state"]=="SENT"
    db.db.execute("UPDATE jobs SET next_attempt=0 WHERE key=?",(key,))
    client.download_file.reset_mock()
    client.download_file.side_effect=RuntimeError("expired link")
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=79))
    await client._on_message_cb(msg)
    sender._bot.send_media_group.assert_awaited_once()
    client.download_file.assert_not_awaited()
    assert db.job(key)["status"]=="SENT"
    db.close()


async def test_album_all_photo_ids_deleted_from_one_max_delete(tmp_path):
    from tests.test_message_sync import bootstrap
    from app.message_sync import MessageSync
    client,sender,state,db=fixture(tmp_path)
    msg=message("album-delete",[
        {"_type":"PHOTO","url":"https://i.oneme.ru/1"},
        {"_type":"PHOTO","url":"https://i.oneme.ru/2"}])
    client.download_file=AsyncMock(return_value=b"test")
    sender._bot.send_media_group=AsyncMock(return_value=[
        SimpleNamespace(message_id=801), SimpleNamespace(message_id=802)])
    await client._on_message_cb(msg)
    sender._bot.delete_message=AsyncMock(return_value=True)
    sync=MessageSync(db,state,sender._bot,"-100")
    assert await sync.accept(142,{"chatId":42,"messageId":"album-delete"})
    ids=[x.kwargs["message_id"]
         for x in sender._bot.delete_message.await_args_list]
    assert ids==[802,801]
    db.close()


async def test_later_failed_attachment_does_not_mark_parent_delivered(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=message("partial-media",[
        {"_type":"PHOTO","url":"https://i.oneme.ru/1"},
        {"_type":"PHOTO","url":"https://i.oneme.ru/2"},
        {"_type":"FILE","url":"https://file-ms.oneme.ru/3","name":"doc.pdf"}])
    client.download_file=AsyncMock(return_value=b"media")
    sender._bot.send_media_group=AsyncMock(return_value=[
        SimpleNamespace(message_id=1),SimpleNamespace(message_id=2)])
    sender._bot.send_document=AsyncMock(side_effect=RetryAfter(1))
    await client._on_message_cb(msg)
    key=db.job_key(42,"partial-media")
    assert db.job(key)["status"]=="RETRY"
    assert db._part(key,0)["state"]=="SENT"
    assert state.tg_for_max_message(42,"partial-media") is None
    db.db.execute("UPDATE jobs SET next_attempt=0 WHERE key=?",(key,))
    sender._bot.send_document=AsyncMock(return_value=SimpleNamespace(message_id=3))
    await client._on_message_cb(msg)
    assert db.job(key)["status"]=="SENT"
    assert sender._bot.send_media_group.await_count==1
    assert state.tg_for_max_message(42,"partial-media")==1
    db.close()


def test_albums_are_bounded_by_telegram_group_size():
    from app.tg_sender import TelegramSender
    with pytest.raises(ValueError):
        # Input validation before touching runtime context
        import asyncio
        asyncio.run(TelegramSender.send_photo_album(
            MagicMock(), [b"photo"]*11))


async def test_eleven_photos_are_group_ten_plus_one_and_all_recorded(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=message("eleven",[
        {"_type":"PHOTO","url":f"https://i.oneme.ru/{i}"}
        for i in range(11)
    ])
    client.download_file=AsyncMock(return_value=b"small-photo")
    sender._bot.send_media_group=AsyncMock(return_value=[
        SimpleNamespace(message_id=i) for i in range(100,110)
    ])
    sender._bot.send_photo=AsyncMock(return_value=SimpleNamespace(message_id=110))
    await client._on_message_cb(msg)
    assert sender._bot.send_media_group.await_count==1
    assert sender._bot.send_photo.await_count==1
    assert db.job(db.job_key(42,"eleven"))["status"]=="SENT"
    parts=db.db.execute(
        "SELECT method,tg_id FROM parts WHERE job_key=? ORDER BY ordinal",
        (db.job_key(42,"eleven"),)
    ).fetchall()
    assert [(r["method"],r["tg_id"]) for r in parts]==[
        ("send_media_group",100),("send_photo",110)
    ]
    db.close()


async def test_album_stored_ambiguous_result_remains_unknown_after_reopen(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=message("group-ambig",[
        {"_type":"PHOTO","url":"https://i.oneme.ru/1"},
        {"_type":"PHOTO","url":"https://i.oneme.ru/2"}])
    client.download_file=AsyncMock(return_value=b"data")
    sender._bot.send_media_group=AsyncMock(side_effect=TimedOut())
    await client._on_message_cb(msg)
    assert db.counts()=={"UNKNOWN":1}
    db.close()
    from app.outbox import OutboxStore
    reopened=OutboxStore(str(tmp_path/"outbox.sqlite3"))
    assert reopened.counts()=={"UNKNOWN":1}
    assert not reopened.recoverable()
    reopened.close()


@pytest.mark.parametrize("filename,method",[
    ("music.mp3","send_audio"),
    ("voice.m4a","send_audio"),
    ("picture.webp","send_document"),
    ("photo.gif","send_document"),
    ("picture.bmp","send_document"),
    ("movie.mkv","send_document"),
    ("video.mov","send_document"),
    ("normal.jpg","send_photo"),
    ("video.mp4","send_video"),
])
async def test_file_extension_keeps_usable_file_kind(tmp_path,filename,method):
    client,sender,state,db=fixture(tmp_path)
    msg=message("file-kind-"+filename,[{
        "_type":"FILE","fileName":filename,
        "fileUrl":"https://file-ms.oneme.ru/media"
    }])
    client.download_file=AsyncMock(return_value=b"file-bytes")
    target=AsyncMock(return_value=SimpleNamespace(message_id=65))
    setattr(sender._bot,method,target)
    await client._on_message_cb(msg)
    target.assert_awaited_once()
    assert db.job(db.job_key(42,"file-kind-"+filename))["status"]=="SENT"
    db.close()


async def test_file_without_download_url_explains_unavailability(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    sender._bot.send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=3))
    await client._on_message_cb(message("no-file-url",[
        {"_type":"FILE","fileName":"report.pdf","fileSize":2024}
    ]))
    body=sender._bot.send_message.await_args.kwargs["text"]
    assert "report.pdf" in body
    assert "недоступен" in body
    db.close()


async def test_location_with_zero_coordinates_is_not_dropped(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=22))
    await client._on_message_cb(message("location-zero",[
        {"_type":"LOCATION","lat":0,"lon":0}
    ]))
    assert "0, 0" in sender._bot.send_message.await_args.kwargs["text"]
    db.close()
