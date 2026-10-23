"""Regression: browser MAX outgoing mirrors to Telegram; TG-origin is not echoed."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.max_client import MaxClient, MaxMessage
from app.max_listener import create_max_client
from app.topics import TopicStore


def bridge(tmp_path, *, existing=True):
    store = TopicStore(str(tmp_path / "topics.json"))
    if existing:
        store.set_topic(42, 777, "Получатель")
    sender = MagicMock()
    sender.topic_store = store
    sender.send = AsyncMock(return_value=SimpleNamespace(message_id=555))
    sender.ensure_topic = AsyncMock(return_value=777)
    client = create_max_client("token", "device", sender)
    client._my_id = 100
    resolver = client.resolver
    resolver.chats_raw = {42: {"id": 42, "type": "DIALOG", "status": "ACTIVE",
                               "participants": {"100": 1, "200": 2}}}
    resolver.chats = {42: "DM:200"}
    resolver.chat_types = {42: "DIALOG"}
    resolver._my_id = 100
    resolver.users[200] = "Получатель"
    resolver.resolve_user = AsyncMock(return_value="Получатель")
    return client, sender, store


async def test_self_from_max_browser_forwarded_once(tmp_path):
    client, sender, store = bridge(tmp_path)
    msg = MaxMessage(chat_id=42, sender_id=100, message_id="browser-1",
                     is_self=True, text="Сообщение из MAX")
    await client._on_message_cb(msg)
    sender.send.assert_awaited_once()
    text = sender.send.await_args.args[0]
    assert "Вы" in text and "Сообщение из MAX" in text
    assert sender.send.await_args.kwargs["message_thread_id"] == 777
    assert store.tg_for_max_message(42, "browser-1") == 555
    sender.send.reset_mock()
    await client._on_message_cb(msg)
    sender.send.assert_not_awaited()


async def test_incoming_still_forwarded(tmp_path):
    client, sender, store = bridge(tmp_path)
    msg = MaxMessage(chat_id=42, sender_id=200, message_id="incoming-1",
                     text="Входящее")
    await client._on_message_cb(msg)
    sender.send.assert_awaited_once()
    assert "Получатель" in sender.send.await_args.args[0]


async def test_existing_tg_mapping_blocks_echo(tmp_path):
    client, sender, store = bridge(tmp_path)
    store.set_message(42, "tg-origin-1", 900)
    msg = MaxMessage(chat_id=42, sender_id=100, message_id="tg-origin-1",
                     is_self=True, text="Telegram")
    await client._on_message_cb(msg)
    sender.send.assert_not_awaited()


async def test_tg_outgoing_echo_is_detected_by_actual_max_id(tmp_path):
    client, sender, store = bridge(tmp_path)
    client.cmd = AsyncMock(return_value={"message": {"id": "max-200"}})
    response = await client.send_message(42, "Текст из Telegram",
                                         telegram_message_id=901)
    assert response["message"]["id"] == "max-200"
    assert await client.telegram_echo_tg_id(MaxMessage(
        chat_id=42, message_id="max-200", text="Текст из Telegram",
        raw={"message": {"text": "Текст из Telegram"}},
    )) == 901
    await client._on_message_cb(MaxMessage(
        chat_id=42, sender_id=100, message_id="max-200",
        is_self=True, text="Текст из Telegram",
    ))
    sender.send.assert_not_awaited()
    assert store.tg_for_max_message(42, "max-200") == 901


async def test_tg_echo_before_ack_does_not_create_duplicate(tmp_path):
    client, sender, store = bridge(tmp_path)
    request_started = asyncio.Event()
    respond = asyncio.Event()

    async def rpc(opcode, payload):
        request_started.set()
        await respond.wait()
        return {"message": {"id": "late-ack"}}

    client.cmd = AsyncMock(side_effect=rpc)
    outgoing = asyncio.create_task(client.send_message(
        42, "Сначала DISPATCH", telegram_message_id=910))
    await request_started.wait()
    echo = asyncio.create_task(client._on_message_cb(MaxMessage(
        chat_id=42, sender_id=100, message_id="late-ack",
        is_self=True, text="Сначала DISPATCH",
    )))
    await asyncio.sleep(0)
    respond.set()
    await outgoing
    await echo
    sender.send.assert_not_awaited()
    assert store.tg_for_max_message(42, "late-ack") == 910


async def test_other_browser_message_is_not_suppressed_while_tg_pending(tmp_path):
    client, sender, _ = bridge(tmp_path)
    request_started = asyncio.Event()
    respond = asyncio.Event()

    async def rpc(opcode, payload):
        request_started.set()
        await respond.wait()
        return {"message": {"id": "tg-only"}}

    client.cmd = AsyncMock(side_effect=rpc)
    task = asyncio.create_task(client.send_message(
        42, "Одинаковый текст", telegram_message_id=920))
    await request_started.wait()
    browser = asyncio.create_task(client._on_message_cb(MaxMessage(
        chat_id=42, sender_id=100, is_self=True,
        message_id="browser-other", text="Одинаковый текст",
    )))
    await asyncio.sleep(0)
    respond.set()
    await asyncio.gather(task, browser)
    sender.send.assert_awaited_once()
    assert "Одинаковый текст" in sender.send.await_args.args[0]


async def test_new_chat_history_import_prevents_self_duplicate(tmp_path):
    client, sender, store = bridge(tmp_path, existing=False)

    async def bootstrap(c, s, r, chat_id):
        store.set_topic(chat_id, 777, "Получатель")
        store.set_message(chat_id, "bootstrap-own", 700)
        return 777

    with patch("app.max_listener._bootstrap_chat_topic", new=bootstrap):
        await client._on_message_cb(MaxMessage(
            chat_id=42, sender_id=100, is_self=True,
            message_id="bootstrap-own", text="В истории"))
    sender.send.assert_not_awaited()


def test_my_id_string_matches_numeric_sender():
    client = MaxClient("token", "device")
    client._my_id = "100"
    assert client._parse_message({"chatId": 42,
                                  "message": {"id": "one", "sender": 100}}).is_self
