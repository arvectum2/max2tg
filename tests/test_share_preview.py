"""Regression for duplicate MAX text / SHARE preview dispatches."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.max_client import MaxClient, MaxMessage, OpCode
from app.max_listener import _message_body, _send_message_content


def test_email_preview_does_not_duplicate_address():
    m = MaxMessage(text="моя почта sereychik@musbread.ru",
                   attaches=[{"_type": "SHARE", "url": "mailto:sereychik@musbread.ru",
                              "title": "sereychik@musbread.ru"}])
    assert _message_body(m) == "моя почта sereychik@musbread.ru"


def test_url_preview_does_not_duplicate_url():
    m = MaxMessage(text="Ссылка https://example.org",
                   attaches=[{"_type": "SHARE", "url": "https://example.org"}])
    assert _message_body(m) == "Ссылка https://example.org"


def test_distinct_link_is_preserved_once():
    m = MaxMessage(text="Посмотри",
                   attaches=[{"_type": "SHARE", "url": "https://example.org"}])
    assert _message_body(m) == "Посмотри\nhttps://example.org"


def test_standalone_share_keeps_link():
    m = MaxMessage(attaches=[{"_type": "SHARE", "url": "https://example.org",
                              "title": "Пример"}])
    assert "https://example.org" in _message_body(m)


async def test_one_telegram_message_for_email_preview():
    m = MaxMessage(text="моя почта sereychik@musbread.ru",
                   attaches=[{"_type": "SHARE", "url": "mailto:sereychik@musbread.ru"}])
    sender = MagicMock()
    sender.send = AsyncMock(return_value=SimpleNamespace(message_id=5))
    sent = await _send_message_content(m, "Сергей", MagicMock(), sender, 9)
    sender.send.assert_awaited_once()
    assert sent.message_id == 5
    assert sender.send.await_args.args[0].count("sereychik@musbread.ru") == 1


async def test_two_dispatch_updates_coalesce_by_message_id():
    client = MaxClient(token="t", device_id="d")
    seen = []
    @client.on_message
    async def callback(msg):
        seen.append(msg)
    base = {"chatId": 7, "message": {"id": "m-1", "sender": 18,
                                    "text": "моя почта someone@example.org"}}
    updated = {"chatId": 7, "message": {**base["message"],
                "attaches": [{"_type": "SHARE", "url": "mailto:someone@example.org"}]}}
    for payload in (base, updated):
        await client._handle({"opcode": OpCode.DISPATCH, "cmd": 0, "payload": payload})
    await asyncio.sleep(0.25)
    assert len(seen) == 1
    assert seen[0].attaches == updated["message"]["attaches"]


async def test_distinct_message_ids_still_delivered():
    client = MaxClient(token="t", device_id="d")
    seen = []
    @client.on_message
    async def callback(msg):
        seen.append(msg.message_id)
    for mid in ("m-1", "m-2"):
        await client._handle({"opcode": OpCode.DISPATCH, "cmd": 0,
                              "payload": {"chatId": 7, "message": {"id": mid, "text": mid}}})
    await asyncio.sleep(0.25)
    assert set(seen) == {"m-1", "m-2"}
