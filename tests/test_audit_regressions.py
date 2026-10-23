"""Regression coverage for failures found during the October bridge audit."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.max_client import MaxClient, MaxMessage
from app.max_listener import import_recent_history
from app.topics import TopicStore
from tests.test_self_forward import bridge


def test_chat_filter_is_instance_local():
    a = MaxClient("token", "device", chat_ids="1,2")
    b = MaxClient("token", "device", chat_ids="3")
    c = MaxClient("token", "device")
    assert a.chat_ids == [1, 2]
    assert b.chat_ids == [3]
    assert c.chat_ids == []


def test_null_message_id_does_not_become_literal_none():
    msg = MaxClient("token", "device")._parse_message({
        "chatId": 42, "message": {"text": "hello", "id": None},
    })
    assert msg.message_id == ""


def test_state_write_failure_does_not_interrupt_forwarding(tmp_path):
    path = str(tmp_path / "store.json")
    store = TopicStore(path)
    store.set_topic(42, 777, "Test")
    with patch("app.topics.tempfile.mkstemp", side_effect=PermissionError("disk locked")):
        store.set_message(42, "MAX-1", 900)
        assert store.tg_for_max_message(42, "MAX-1") == 900
    assert TopicStore(path).tg_for_max_message(42, "MAX-1") is None
    store.set_message(42, "MAX-2", 901)
    recovered = TopicStore(path)
    assert recovered.tg_for_max_message(42, "MAX-1") == 900
    assert recovered.tg_for_max_message(42, "MAX-2") == 901


async def test_forward_without_comment_stored_and_not_duplicated(tmp_path):
    client, sender, store = bridge(tmp_path)
    msg = MaxMessage(
        chat_id=42, sender_id=200, message_id="fwd-1", text="",
        link={"type": "FORWARD",
              "message": {"sender": 200, "text": "Пересланное содержание"}},
    )
    await client._on_message_cb(msg)
    sender.send.assert_awaited_once()
    assert "Пересланное содержание" in sender.send.await_args.args[0]
    assert store.tg_for_max_message(42, "fwd-1") == 555
    await client._on_message_cb(msg)
    sender.send.assert_awaited_once()


async def test_forward_preview_does_not_repeat_email(tmp_path):
    client, sender, store = bridge(tmp_path)
    msg = MaxMessage(
        chat_id=42, sender_id=200, message_id="fwd-email",
        link={"type": "FORWARD",
              "message": {"sender": 200,
                          "text": "Почта user@example.org",
                          "attaches": [{"_type": "SHARE",
                                        "url": "mailto:user@example.org"}]}},
    )
    await client._on_message_cb(msg)
    sender.send.assert_awaited_once()
    assert sender.send.await_args.args[0].count("user@example.org") == 1
    assert store.tg_for_max_message(42, "fwd-email") == 555


async def test_forward_without_comment_in_history_maps_id(tmp_path):
    client, sender, store = bridge(tmp_path)
    client.fetch_history = AsyncMock(return_value={
        "messages": [{"id": "history-fwd", "time": 1700000000000,
                      "sender": 200,
                      "link": {"type": "FORWARD",
                               "message": {"sender": 200, "text": "Содержимое"}}}],
    })
    count = await import_recent_history(
        client, sender, client.resolver, 42, thread_id=777, count=20,
    )
    assert count == 1
    assert store.tg_for_max_message(42, "history-fwd") == 555
    count = await import_recent_history(
        client, sender, client.resolver, 42, thread_id=777, count=20,
    )
    assert count == 0
    sender.send.assert_awaited_once()


@pytest.mark.parametrize("url", [
    "http://i.oneme.ru/i?a=x",
    "https://127.0.0.1/x",
    "https://localhost/x",
    "https://i.oneme.ru.evil.example/x",
    "https://i.oneme.ru:8080/x",
    "https://user@i.oneme.ru/x",
    "ftp://i.oneme.ru/x",
    "https://[::1]/x",
    "not a URL",
    "",
])
def test_media_url_rejects_untrusted_sources(url):
    assert not MaxClient._safe_media_url(url)


@pytest.mark.parametrize("url", [
    "https://i.oneme.ru/i?r=token",
    "https://cdn.oneme.ru/file",
    "https://a.b.max.ru/file",
    "https://sun1.userapi.com/x",
])
def test_media_url_accepts_known_cdn_hosts(url):
    assert MaxClient._safe_media_url(url)


class FakeResp:
    def __init__(self, status=200, data=b"ok", headers=None, content_length=None):
        self.status = status
        self.data = data
        self.headers = headers or {}
        self.content_length = content_length
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def iter_chunked(self, size):
        for idx in range(0, len(self.data), size):
            yield self.data[idx:idx+size]


async def test_media_private_redirect_blocked():
    c = MaxClient("token", "device")
    session = MagicMock()
    session.closed = False
    session.get = MagicMock(return_value=FakeResp(
        status=302, headers={"Location": "http://127.0.0.1:8000/admin"}))
    c._session = session
    result = await c.download_file("https://i.oneme.ru/a")
    assert result is None
    session.get.assert_called_once()


async def test_media_oversized_response_rejected():
    c = MaxClient("token", "device")
    session = MagicMock()
    session.closed = False
    session.get = MagicMock(return_value=FakeResp(data=b"z" * 80))
    c._session = session
    with patch("app.max_client.MAX_MEDIA_BYTES", 40):
        result = await c.download_file("https://i.oneme.ru/a")
    assert result is None


async def test_media_allowed_response_streamed():
    c = MaxClient("token", "device")
    session = MagicMock()
    session.closed = False
    session.get = MagicMock(return_value=FakeResp(data=b"x" * 256))
    c._session = session
    assert await c.download_file("https://i.oneme.ru/a") == b"x" * 256


async def test_concurrent_live_callbacks_for_one_message_are_serialized(tmp_path):
    client, sender, store = bridge(tmp_path)
    sending = asyncio.Event()
    continue_sending = asyncio.Event()

    async def delayed_send(*args, **kwargs):
        sending.set()
        await continue_sending.wait()
        return SimpleNamespace(message_id=555)

    sender.send = AsyncMock(side_effect=delayed_send)
    msg = MaxMessage(chat_id=42, sender_id=200,
                     message_id="racing-1", text="Only once")
    first = asyncio.create_task(client._on_message_cb(msg))
    await sending.wait()
    second = asyncio.create_task(client._on_message_cb(msg))
    await asyncio.sleep(0)
    assert sender.send.await_count == 1
    continue_sending.set()
    await asyncio.gather(first, second)
    assert sender.send.await_count == 1
    assert store.tg_for_max_message(42, "racing-1") == 555


async def test_live_message_waits_for_initial_snapshot_import(tmp_path):
    client, sender, store = bridge(tmp_path, existing=False)
    from app.max_listener import _KNOWN_MAX_CHATS_UI_KEY
    store.set_ui(_KNOWN_MAX_CHATS_UI_KEY, [])
    resolver = client.resolver
    resolver.load_snapshot = MagicMock(return_value=[])
    started = asyncio.Event()
    release = asyncio.Event()

    async def bootstrap(client_arg, sender_arg, resolver_arg, chat_id):
        store.set_topic(chat_id, 777, "Получатель")
        started.set()
        await release.wait()
        store.set_message(chat_id, "new-1", 999)
        return 777

    with patch("app.max_listener._bootstrap_chat_topic", new=bootstrap):
        ready = asyncio.create_task(client._on_ready_cb({}))
        await asyncio.wait_for(started.wait(), timeout=1)
        msg = MaxMessage(chat_id=42, sender_id=200,
                         message_id="new-1", text="Import started")
        live = asyncio.create_task(client._on_message_cb(msg))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(ready, live)
    assert store.tg_for_max_message(42, "new-1") == 999
    assert not any("Import started" in c.args[0] for c in sender.send.await_args_list)


async def test_disconnected_max_command_does_not_wait_ten_seconds():
    client = MaxClient("token", "device")
    assert await client.cmd(49, {"chatId": 1}) == {}
    assert await client.cmd(49, {"chatId": 1}, none_on_timeout=True) is None


def test_null_message_text_is_normalized():
    c = MaxClient("token", "device")
    m = c._parse_message({"chatId": 1, "message": {"id": 42, "text": None}})
    assert m.text == ""


async def test_malformed_forward_inner_does_not_crash(tmp_path):
    client, sender, store = bridge(tmp_path)
    m = MaxMessage(chat_id=42, sender_id=200, message_id="fwd-broken",
                   link={"type": "FORWARD", "message": "not a dict",
                         "text": "Still recoverable"})
    await client._on_message_cb(m)
    sender.send.assert_awaited_once()
    assert store.tg_for_max_message(42, "fwd-broken") == 555
