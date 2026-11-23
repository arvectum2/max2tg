"""Contract checks for extracted Telegram handler modules.

No live MAX/Telegram requests, credentials, or persistent storage are used.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import MessageEntity
from telegram.constants import MessageEntityType

from app import tg_handler
from app import max_search, telegram_entities, tg_help


def test_legacy_reexports_keep_callable_identity():
    assert tg_handler._utf16_to_char_offset is telegram_entities._utf16_to_char_offset
    assert tg_handler._entities_to_max_elements is telegram_entities._entities_to_max_elements
    assert tg_handler._normalize_global_search_results is max_search._normalize_global_search_results
    assert tg_handler._cmd_help is tg_help._cmd_help
    assert tg_handler.HELP_TEXT == tg_help.HELP_TEXT


def test_telegram_utf16_entity_offsets_and_link_attributes():
    text = "A🧩Б"
    entities = [
        MessageEntity(type=MessageEntityType.BOLD, offset=3, length=1),
        MessageEntity(type=MessageEntityType.TEXT_LINK, offset=1, length=2,
                      url="https://example.org/"),
        MessageEntity(type=MessageEntityType.BOT_COMMAND, offset=0, length=1),
    ]
    assert telegram_entities._entities_to_max_elements(text, entities) == [
        {"type": "STRONG", "from": 2, "length": 1},
        {"type": "LINK", "from": 1, "length": 1,
         "attributes": {"url": "https://example.org/"}},
    ]


@pytest.mark.parametrize("entities", [None, [], ()])
def test_empty_entities_do_not_emit_max_styles(entities):
    assert telegram_entities._entities_to_max_elements("Hi", entities) == []


def test_search_normalizes_chat_and_contact_and_skips_own_user():
    client = SimpleNamespace(_my_id=7, dialog_chat_id=lambda cid: 7 ^ cid)
    response = {"result": [
        {"chat": {"id": "42", "type": "CHAT", "title": "Клуб"}},
        {"chat": {"id": 42, "type": "CHAT", "title": "Дубликат"}},
        {"chat": {"id": "bad", "type": "CHANNEL"}},
        {"contact": {"contact": {"id": "12",
                                 "names": [{"firstName": "Анна", "lastName": "П"}]}}},
        {"contact": {"contact": {"id": 7, "name": "Self"}}},
    ]}
    items = max_search._normalize_global_search_results(response, client)
    assert [(item["chat_id"], item["type"], item["title"]) for item in items] == [
        (42, "CHAT", "Клуб"), (11, "DIALOG", "Анна П")
    ]


def test_search_rejects_non_map_and_unknown_payloads():
    client = SimpleNamespace(_my_id=None, dialog_chat_id=lambda cid: cid + 1)
    assert max_search._normalize_global_search_results(None, client) == []
    assert max_search._normalize_global_search_results(
        {"result": [{"chat": {"id": 1, "type": "UNSUPPORTED"}}, 42]},
        client
    ) == []


@pytest.mark.asyncio
async def test_menu_topic_creation_failure_fails_closed(monkeypatch):
    """A failed remote create cannot crash the callback or leak exception data."""
    update = MagicMock()
    update.effective_user.id = 100
    update.callback_query.data = "menu:bind:42:0"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {
        tg_handler.MAX_CLIENT_KEY: SimpleNamespace(resolver=MagicMock()),
        tg_handler.TOPIC_STORE_KEY: MagicMock(),
        tg_handler.ADMIN_USER_KEY: 100,
    }
    monkeypatch.setattr(
        tg_handler, "_create_topic_from_menu",
        AsyncMock(side_effect=RuntimeError("SECRET_REDACTED_IN_TELEGRAM")),
    )
    await tg_handler._on_menu_callback(update, context)
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once()
    sent = update.callback_query.edit_message_text.await_args.args[0]
    assert "Не удалось создать Telegram-топик" in sent
    assert "SECRET_REDACTED_IN_TELEGRAM" not in sent


@pytest.mark.asyncio
async def test_help_handler_response_is_unchanged():
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message)
    await tg_help._cmd_help(update, None)
    message.reply_text.assert_awaited_once_with(
        tg_help.HELP_TEXT,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await tg_help._cmd_help(SimpleNamespace(message=None), None)
    assert message.reply_text.await_count == 1
