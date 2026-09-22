"""Tests for app/tg_handler.py — topic-based reply routing."""

from unittest.mock import AsyncMock, MagicMock

from telegram.error import BadRequest

from app.tg_handler import (
    ADMIN_USER_KEY,
    ALLOWED_USER_KEY,
    ALLOWED_USERS_KEY,
    MAX_CLIENT_KEY,
    SUPERGROUP_KEY,
    TOPIC_STORE_KEY,
    _cmd_bind,
    _cmd_leave,
    _cmd_menu,
    _cmd_search,
    _history_import_limit,
    _normalize_global_search_results,
    _on_quick_menu_button,
    _on_history_setting_input,
    _on_del_callback,
    _on_leave_callback,
    _on_menu_callback,
    _on_settings_callback,
    _on_search_callback,
    _on_search_input,
    _on_topic_message,
    build_tg_app,
    ensure_quick_menu,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_topic_store(mapping: dict | None = None):
    """A TopicStore stand-in: chat_for_topic(thread_id) → max_chat_id."""
    mapping = {10: 42} if mapping is None else mapping
    store = MagicMock()
    reverse = {chat_id: thread_id for thread_id, chat_id in mapping.items()}
    store.chat_for_topic = MagicMock(side_effect=lambda tid: mapping.get(tid))
    store.get_topic = MagicMock(side_effect=lambda chat_id: reverse.get(chat_id))
    store.max_for_tg_message = MagicMock(return_value=None)
    return store


def _make_update(text="Hello", thread_id=10, is_topic_message=True, user_id=100):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.message_id = 500
    update.message.entities = []
    update.message.message_thread_id = thread_id
    update.message.is_topic_message = is_topic_message
    update.message.reply_to_message = None
    update.message.reply_text = AsyncMock()
    update.message.set_reaction = AsyncMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def _make_context(max_client=None, topic_store=None, allowed_user_id=None, allowed_user_ids=None, admin_user_id=None):
    ctx = MagicMock()
    ctx.args = []
    bot_data = {ALLOWED_USER_KEY: allowed_user_id, ALLOWED_USERS_KEY: frozenset(allowed_user_ids or []), ADMIN_USER_KEY: admin_user_id}
    if max_client is not None:
        bot_data[MAX_CLIENT_KEY] = max_client
    if topic_store is not None:
        bot_data[TOPIC_STORE_KEY] = topic_store
    ctx.bot_data = bot_data
    ctx.user_data = {}
    return ctx


# ---------------------------------------------------------------------------
# _on_topic_message
# ---------------------------------------------------------------------------

class TestOnTopicMessage:
    async def test_routes_topic_text_to_max(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_update("Hello", thread_id=10)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_called_once_with(42, "Hello", elements=[])

    async def test_reacts_on_success(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.set_reaction.assert_called_once()

    async def test_ignores_general_topic(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_update(thread_id=None, is_topic_message=False)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_ignores_unknown_topic(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_update(thread_id=999)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_ignores_empty_text(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_update(text=None)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_respects_allowed_user_id(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_update(user_id=555)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store(),
                            allowed_user_id=100)

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_allows_matching_user_id(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_update(user_id=100)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store(),
                            allowed_user_id=100)

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_called_once()

    async def test_allows_each_member_of_multi_user_acl(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store(),
                            allowed_user_ids={100, 200, 300})

        for user_id in (100, 200, 300):
            max_client.send_message.reset_mock()
            await _on_topic_message(_make_update(user_id=user_id), ctx)
            max_client.send_message.assert_called_once()

    async def test_rejects_user_outside_multi_user_acl(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store(),
                            allowed_user_ids={100, 200, 300})

        await _on_topic_message(_make_update(user_id=999), ctx)

        max_client.send_message.assert_not_called()

    async def test_warns_when_max_client_missing(self):
        update = _make_update()
        ctx = _make_context(topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args[0][0]

    async def test_warns_on_send_failure(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value=None)

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args[0][0]

    async def test_warns_on_exception(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(side_effect=RuntimeError("boom"))

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args[0][0]

    async def test_native_reply_is_sent_as_max_reply(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(
            return_value={"message": {"id": "max-new"}}
        )
        store = _make_topic_store({10: 42})
        store.max_for_tg_message.return_value = "max-old"

        update = _make_update("Ответ", thread_id=10)
        update.message.reply_to_message = MagicMock()
        update.message.reply_to_message.message_id = 77
        ctx = _make_context(max_client=max_client, topic_store=store)

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_called_once_with(
            42,
            "Ответ",
            elements=[],
            link={"type": "REPLY", "messageId": "max-old"},
        )
        store.set_message.assert_called_once_with(42, "max-new", 500)


# ---------------------------------------------------------------------------
# /leave
# ---------------------------------------------------------------------------

class TestLeave:
    async def test_admin_gets_leave_confirmation(self):
        max_client = MagicMock()
        resolver = MagicMock()
        resolver.is_dm.return_value = False
        resolver.chat_name.return_value = "Test group"
        max_client.resolver = resolver

        store = _make_topic_store({10: -123})
        update = _make_update(thread_id=10, user_id=100)
        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )

        await _cmd_leave(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "Выйти из" in update.message.reply_text.call_args[0][0]

    async def test_leave_by_chat_id_works_from_general(self):
        max_client = MagicMock()
        resolver = MagicMock()
        resolver.is_dm.return_value = False
        resolver.chat_name.return_value = "Test channel"
        max_client.resolver = resolver

        store = _make_topic_store({})
        store.get_topic.return_value = None
        update = _make_update(thread_id=None, is_topic_message=False, user_id=100)
        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )
        ctx.args = ["-123"]

        await _cmd_leave(update, ctx)

        update.message.reply_text.assert_called_once()
        markup = update.message.reply_text.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].callback_data == "leave:ok:0:-123"

    async def test_empty_payload_is_successful_leave(self):
        max_client = MagicMock()
        max_client.leave_chat = AsyncMock(return_value={})
        resolver = MagicMock()
        resolver.chats = {-123: "Test channel"}
        resolver.chat_types = {-123: "CHANNEL"}
        resolver.chats_raw = {-123: {"type": "CHANNEL"}}
        max_client.resolver = resolver

        store = MagicMock()
        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = "leave:ok:0:-123"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 100

        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )
        ctx.bot_data[SUPERGROUP_KEY] = -100999
        ctx.bot = MagicMock()
        ctx.bot.delete_forum_topic = AsyncMock()

        await _on_leave_callback(update, ctx)

        max_client.leave_chat.assert_awaited_once_with(-123)
        store.remove.assert_called_once_with(-123)
        ctx.bot.delete_forum_topic.assert_not_awaited()
        success_text = update.callback_query.edit_message_text.call_args.args[0]
        assert "Вышли из" in success_text

    async def test_leave_button_on_photo_card_edits_caption(self):
        max_client = MagicMock()
        resolver = MagicMock()
        resolver.chat_name.return_value = "Photo channel"
        max_client.resolver = resolver

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = "leave:ask:10:-123"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=BadRequest("There is no text in the message to edit")
        )
        update.callback_query.edit_message_caption = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 100

        ctx = _make_context(
            max_client=max_client,
            topic_store=MagicMock(),
            allowed_user_ids={100},
            admin_user_id=100,
        )

        await _on_leave_callback(update, ctx)

        update.callback_query.edit_message_caption.assert_awaited_once()
        kwargs = update.callback_query.edit_message_caption.call_args.kwargs
        assert "Выйти из" in kwargs["caption"]
        callbacks = [
            button.callback_data
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        assert "leave:ok:10:-123" in callbacks

    async def test_confirmed_leave_calls_max_then_removes_topic(self):
        max_client = MagicMock()
        max_client.leave_chat = AsyncMock(
            return_value={"message": {"attaches": [{"event": "leave"}]}}
        )
        resolver = MagicMock()
        resolver.chats = {-123: "Test group"}
        resolver.chat_types = {-123: "CHAT"}
        resolver.chats_raw = {-123: {"type": "CHAT"}}
        max_client.resolver = resolver

        store = MagicMock()
        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = "leave:ok:10:-123"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 100

        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )
        ctx.bot_data[SUPERGROUP_KEY] = -100999
        ctx.bot = MagicMock()
        ctx.bot.delete_forum_topic = AsyncMock()
        ctx.bot.send_message = AsyncMock()

        await _on_leave_callback(update, ctx)

        max_client.leave_chat.assert_awaited_once_with(-123)
        store.remove.assert_called_once_with(-123)
        ctx.bot.delete_forum_topic.assert_awaited_once_with(
            chat_id=-100999,
            message_thread_id=10,
        )
        ctx.bot.send_message.assert_awaited_once()
        assert "Telegram-топик удалён" in ctx.bot.send_message.call_args.kwargs["text"]


# ---------------------------------------------------------------------------
# delete topic
# ---------------------------------------------------------------------------

class TestDeleteTopic:
    async def test_delete_button_on_photo_card_edits_caption(self):
        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = "del:ask:10:-123"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=BadRequest("There is no text in the message to edit")
        )
        update.callback_query.edit_message_caption = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 100

        ctx = _make_context(
            topic_store=MagicMock(),
            allowed_user_ids={100},
            admin_user_id=100,
        )

        await _on_del_callback(update, ctx)

        update.callback_query.edit_message_caption.assert_awaited_once()
        kwargs = update.callback_query.edit_message_caption.call_args.kwargs
        assert "Удалить Telegram-топик" in kwargs["caption"]
        callbacks = [
            button.callback_data
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        assert "del:ok:10:-123" in callbacks


# ---------------------------------------------------------------------------
# history import settings
# ---------------------------------------------------------------------------

class TestHistorySettings:
    def test_default_is_20_and_value_is_clamped(self):
        store = MagicMock()
        store.get_ui.return_value = None
        assert _history_import_limit(store) == 20
        store.get_ui.return_value = 150
        assert _history_import_limit(store) == 100
        store.get_ui.return_value = -5
        assert _history_import_limit(store) == 0

    async def test_settings_open_shows_current_value(self):
        store = MagicMock()
        store.get_ui.return_value = 35
        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = "settings:open"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 100
        ctx = _make_context(
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )

        await _on_settings_callback(update, ctx)

        text = update.callback_query.edit_message_text.await_args.args[0]
        assert "35" in text
        assert "0" in text and "100" in text

    async def test_valid_history_limit_is_saved(self):
        store = _make_topic_store({})
        update = _make_update(text="42", thread_id=None,
                              is_topic_message=False, user_id=100)
        update.message.delete = AsyncMock()
        ctx = _make_context(
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )
        ctx.user_data["awaiting_history_limit"] = True

        await _on_history_setting_input(update, ctx)

        store.set_ui.assert_called_once_with("history_import_limit", 42)
        assert ctx.user_data["awaiting_history_limit"] is False
        assert "42" in update.message.reply_text.await_args.args[0]
        update.message.delete.assert_awaited_once()

    async def test_invalid_history_limit_is_rejected(self):
        store = _make_topic_store({})
        update = _make_update(text="101", thread_id=None,
                              is_topic_message=False, user_id=100)
        ctx = _make_context(
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )
        ctx.user_data["awaiting_history_limit"] = True

        await _on_history_setting_input(update, ctx)

        store.set_ui.assert_not_called()
        assert ctx.user_data["awaiting_history_limit"] is True
        assert "0 до 100" in update.message.reply_text.await_args.args[0]


# ---------------------------------------------------------------------------
# persistent quick menu
# ---------------------------------------------------------------------------

class TestQuickMenu:
    async def test_installs_persistent_keyboard_once(self):
        bot = MagicMock()
        sent = MagicMock()
        sent.message_id = 91
        bot.send_message = AsyncMock(return_value=sent)
        store = MagicMock()
        store.get_ui.return_value = None

        result = await ensure_quick_menu(bot, -100999, store)

        assert result == 91
        kwargs = bot.send_message.call_args.kwargs
        markup = kwargs["reply_markup"]
        assert markup.is_persistent is True
        assert markup.resize_keyboard is True
        assert markup.keyboard[0][0].text == "☰ Меню"
        store.set_ui.assert_called_once_with("quick_menu_message_id", 91)

    async def test_does_not_post_duplicate_installer(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        store = MagicMock()
        store.get_ui.return_value = 91

        result = await ensure_quick_menu(bot, -100999, store)

        assert result == 91
        bot.send_message.assert_not_awaited()

    async def test_button_opens_topic_menu_and_deletes_trigger(self):
        max_client = MagicMock()
        resolver = MagicMock()
        resolver.chat_name.return_value = "Новости"
        resolver.is_dm.return_value = False
        max_client.resolver = resolver
        store = _make_topic_store({10: -123})
        update = _make_update(text="☰ Меню", thread_id=10, user_id=100)
        update.message.delete = AsyncMock()
        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )

        await _on_quick_menu_button(update, ctx)

        update.message.reply_text.assert_awaited_once()
        markup = update.message.reply_text.call_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        assert "leave:ask:10:-123" in callbacks
        update.message.delete.assert_awaited_once()


# ---------------------------------------------------------------------------
# button menu
# ---------------------------------------------------------------------------

class TestMenu:
    async def test_topic_menu_offers_leave_without_chat_id(self):
        max_client = MagicMock()
        resolver = MagicMock()
        resolver.chat_name.return_value = "Пчёлки"
        resolver.is_dm.return_value = False
        max_client.resolver = resolver
        store = _make_topic_store({10: -123})

        update = _make_update(thread_id=10, user_id=100)
        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )

        await _cmd_menu(update, ctx)

        markup = update.message.reply_text.call_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        assert "leave:ask:10:-123" in callbacks

    async def test_general_menu_lists_max_chats_by_name(self):
        max_client = MagicMock()
        resolver = MagicMock()
        resolver.chats = {-123: "Канал", -456: "Группа", 789: "Личный"}
        resolver.chat_types = {-123: "CHANNEL", -456: "CHAT", 789: "DIALOG"}
        resolver.chats_raw = {
            -123: {"status": "ACTIVE"},
            -456: {"status": "ACTIVE"},
            789: {"status": "ACTIVE"},
        }
        resolver.chat_name.side_effect = lambda cid: resolver.chats.get(cid, str(cid))
        resolver.is_dm.side_effect = lambda cid: resolver.chat_types.get(cid) == "DIALOG"
        resolver.user_name.side_effect = lambda uid: str(uid)
        max_client.resolver = resolver
        store = _make_topic_store({})

        update = _make_update(thread_id=None, is_topic_message=False, user_id=100)
        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )

        await _cmd_menu(update, ctx)

        markup = update.message.reply_text.call_args.kwargs["reply_markup"]
        labels = [row[0].text for row in markup.inline_keyboard]
        assert any("Канал" in label for label in labels)
        assert any("Группа" in label for label in labels)
        assert any("Личный" in label for label in labels)
        callbacks = [
            button.callback_data for row in markup.inline_keyboard for button in row
        ]
        assert "menu:search" in callbacks

    async def test_refresh_ignores_telegram_not_modified(self):
        max_client = MagicMock()
        resolver = MagicMock()
        resolver.chats = {-123: "Канал"}
        resolver.chat_types = {-123: "CHANNEL"}
        resolver.chats_raw = {-123: {"status": "ACTIVE"}}
        resolver.chat_name.return_value = "Канал"
        resolver.is_dm.return_value = False
        max_client.resolver = resolver
        store = _make_topic_store({})

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = "menu:list:0"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message is not modified")
        )
        update.effective_user = MagicMock()
        update.effective_user.id = 100

        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )

        await _on_menu_callback(update, ctx)

        update.callback_query.edit_message_text.assert_awaited_once()

    async def test_dm_card_has_create_topic_but_no_leave(self):
        max_client = MagicMock()
        resolver = MagicMock()
        resolver.chats = {789: "Личный"}
        resolver.chat_types = {789: "DIALOG"}
        resolver.chats_raw = {789: {"status": "ACTIVE", "type": "DIALOG"}}
        resolver.chat_name.return_value = "Личный"
        resolver.is_dm.return_value = True
        max_client.resolver = resolver
        store = _make_topic_store({})

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = "menu:chat:789:0"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 100

        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )

        await _on_menu_callback(update, ctx)

        markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        assert "menu:bind:789:0" in callbacks
        assert not any(callback.startswith("leave:") for callback in callbacks)

    async def test_menu_can_create_topic_for_unbound_chat(self):
        max_client = MagicMock()
        resolver = MagicMock()
        resolver.chats = {-456: "Группа"}
        resolver.chat_types = {-456: "CHAT"}
        resolver.chats_raw = {-456: {"status": "ACTIVE", "type": "CHAT", "title": "Группа"}}
        resolver.chat_name.return_value = "Группа"
        resolver.is_dm.return_value = False
        max_client.resolver = resolver
        store = _make_topic_store({})

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = "menu:bind:-456:0"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 100

        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )
        ctx.bot_data[SUPERGROUP_KEY] = -100999
        ctx.bot = MagicMock()
        topic = MagicMock()
        topic.message_thread_id = 77
        ctx.bot.create_forum_topic = AsyncMock(return_value=topic)
        ctx.bot.send_message = AsyncMock()
        ctx.bot.pin_chat_message = AsyncMock()

        await _on_menu_callback(update, ctx)

        ctx.bot.create_forum_topic.assert_awaited_once_with(
            chat_id=-100999, name="Группа",
        )
        store.set_topic.assert_called_once_with(-456, 77, "Группа")
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "Telegram-топик создан" in text


# ---------------------------------------------------------------------------
# MAX global search UX
# ---------------------------------------------------------------------------

class TestSearch:
    def test_normalizes_public_chat_and_contact(self):
        max_client = MagicMock()
        max_client._my_id = 100
        max_client.dialog_chat_id.side_effect = lambda uid: 100 ^ uid

        records = _normalize_global_search_results({
            "result": [
                {
                    "chat": {
                        "id": -777,
                        "type": "CHANNEL",
                        "title": "Новости",
                        "link": "https://max.ru/news",
                    }
                },
                {
                    "contact": {
                        "contact": {
                            "id": 55,
                            "names": [{"firstName": "Иван", "lastName": "Иванов"}],
                            "link": "https://max.ru/id55",
                        }
                    }
                },
            ]
        }, max_client)

        assert records[0]["chat_id"] == -777
        assert records[0]["type"] == "CHANNEL"
        assert records[1]["chat_id"] == (100 ^ 55)
        assert records[1]["type"] == "DIALOG"
        assert records[1]["title"] == "Иван Иванов"

    async def test_search_input_calls_global_search_and_renders_results(self):
        max_client = MagicMock()
        max_client._my_id = 100
        max_client.search_global = AsyncMock(return_value={
            "result": [{
                "chat": {
                    "id": -777,
                    "type": "CHANNEL",
                    "title": "Новости",
                    "link": "https://max.ru/news",
                }
            }],
            "total": 1,
        })
        store = _make_topic_store({})
        update = _make_update(
            text="Новости", thread_id=1,
            is_topic_message=True, user_id=100,
        )
        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )
        ctx.user_data["awaiting_max_search"] = True

        await _on_search_input(update, ctx)

        max_client.search_global.assert_awaited_once_with("Новости", count=16)
        assert ctx.user_data["awaiting_max_search"] is False
        markup = update.message.reply_text.call_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data for row in markup.inline_keyboard for button in row
        ]
        assert "search:open:-777" in callbacks

    async def test_search_connect_dialog_creates_topic_without_open_by_link(self):
        max_client = MagicMock()
        max_client._my_id = 100
        max_client.open_by_link = AsyncMock()
        resolver = MagicMock()
        resolver.my_id = 100
        resolver.chats = {}
        resolver.chat_types = {}
        resolver.chats_raw = {}
        resolver.contacts_raw = {}
        resolver.users = {}
        resolver.is_dm.side_effect = lambda cid: resolver.chat_types.get(cid) == "DIALOG"
        resolver.chat_name.side_effect = lambda cid: resolver.chats.get(cid, str(cid))
        max_client.resolver = resolver

        chat_id = 100 ^ 55
        record = {
            "chat_id": chat_id,
            "contact_id": 55,
            "type": "DIALOG",
            "title": "Иван Иванов",
            "link": "https://max.ru/id55",
            "raw": {
                "id": 55,
                "names": [{"firstName": "Иван", "lastName": "Иванов"}],
            },
        }
        store = _make_topic_store({})
        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = f"search:connect:{chat_id}"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 100

        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )
        ctx.user_data["max_search_results"] = {str(chat_id): record}
        ctx.bot_data[SUPERGROUP_KEY] = -100999
        ctx.bot = MagicMock()
        topic = MagicMock()
        topic.message_thread_id = 88
        ctx.bot.create_forum_topic = AsyncMock(return_value=topic)
        ctx.bot.send_message = AsyncMock()
        ctx.bot.pin_chat_message = AsyncMock()

        await _on_search_callback(update, ctx)

        max_client.open_by_link.assert_not_awaited()
        ctx.bot.create_forum_topic.assert_awaited_once_with(
            chat_id=-100999, name="Иван Иванов",
        )
        store.set_topic.assert_called_once_with(chat_id, 88, "Иван Иванов")
        assert resolver.chat_types[chat_id] == "DIALOG"
        assert resolver.users[55] == "Иван Иванов"

    async def test_search_connect_continues_if_callback_ack_network_fails(self):
        max_client = MagicMock()
        resolver = MagicMock()
        resolver.my_id = 100
        resolver.chats = {}
        resolver.chat_types = {}
        resolver.chats_raw = {}
        resolver.contacts_raw = {}
        resolver.users = {}
        resolver.is_dm.return_value = False
        resolver.chat_name.side_effect = lambda cid: resolver.chats.get(cid, str(cid))
        max_client.resolver = resolver
        max_client.open_by_link = AsyncMock(return_value={
            "chat": {
                "id": -777,
                "type": "CHANNEL",
                "title": "Новости",
                "link": "https://max.ru/news",
                "status": "ACTIVE",
            }
        })
        store = _make_topic_store({})
        record = {
            "chat_id": -777,
            "type": "CHANNEL",
            "title": "Новости",
            "link": "https://max.ru/news",
            "raw": {"id": -777, "type": "CHANNEL", "title": "Новости"},
        }

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = "search:connect:-777"
        update.callback_query.answer = AsyncMock(
            side_effect=RuntimeError("temporary Telegram proxy failure")
        )
        update.callback_query.edit_message_text = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 100

        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )
        ctx.user_data["max_search_results"] = {"-777": record}
        ctx.bot_data[SUPERGROUP_KEY] = -100999
        ctx.bot = MagicMock()
        topic = MagicMock()
        topic.message_thread_id = 90
        ctx.bot.create_forum_topic = AsyncMock(return_value=topic)
        ctx.bot.send_message = AsyncMock()
        ctx.bot.pin_chat_message = AsyncMock()

        await _on_search_callback(update, ctx)

        max_client.open_by_link.assert_awaited_once_with("https://max.ru/news")
        ctx.bot.create_forum_topic.assert_awaited_once_with(
            chat_id=-100999, name="Новости",
        )
        store.set_topic.assert_called_once_with(-777, 90, "Новости")

    async def test_search_connect_public_channel_opens_link_first(self):
        max_client = MagicMock()
        resolver = MagicMock()
        resolver.my_id = 100
        resolver.chats = {}
        resolver.chat_types = {}
        resolver.chats_raw = {}
        resolver.contacts_raw = {}
        resolver.users = {}
        resolver.is_dm.return_value = False
        resolver.chat_name.side_effect = lambda cid: resolver.chats.get(cid, str(cid))
        max_client.resolver = resolver
        max_client.open_by_link = AsyncMock(return_value={
            "chat": {
                "id": -777,
                "type": "CHANNEL",
                "title": "Новости",
                "link": "https://max.ru/news",
                "status": "ACTIVE",
            }
        })
        store = _make_topic_store({})
        record = {
            "chat_id": -777,
            "type": "CHANNEL",
            "title": "Новости",
            "link": "https://max.ru/news",
            "raw": {
                "id": -777,
                "type": "CHANNEL",
                "title": "Новости",
            },
        }

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = "search:connect:-777"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 100

        ctx = _make_context(
            max_client=max_client,
            topic_store=store,
            allowed_user_ids={100},
            admin_user_id=100,
        )
        ctx.user_data["max_search_results"] = {"-777": record}
        ctx.bot_data[SUPERGROUP_KEY] = -100999
        ctx.bot = MagicMock()
        topic = MagicMock()
        topic.message_thread_id = 89
        ctx.bot.create_forum_topic = AsyncMock(return_value=topic)
        ctx.bot.send_message = AsyncMock()
        ctx.bot.pin_chat_message = AsyncMock()

        await _on_search_callback(update, ctx)

        max_client.open_by_link.assert_awaited_once_with("https://max.ru/news")
        ctx.bot.create_forum_topic.assert_awaited_once_with(
            chat_id=-100999, name="Новости",
        )
        assert resolver.chat_types[-777] == "CHANNEL"


# ---------------------------------------------------------------------------
# family admin ACL
# ---------------------------------------------------------------------------

class TestFamilyAdminAcl:
    async def test_non_admin_cannot_bind(self):
        update = _make_update(user_id=200)
        ctx = _make_context(allowed_user_ids={100, 200, 300}, admin_user_id=100)
        ctx.args = ["-12345"]
        await _cmd_bind(update, ctx)
        update.message.reply_text.assert_not_called()

    async def test_admin_can_enter_bind_flow(self):
        update = _make_update(user_id=100)
        ctx = _make_context(allowed_user_ids={100, 200, 300}, admin_user_id=100)
        ctx.args = []
        await _cmd_bind(update, ctx)
        update.message.reply_text.assert_called_once()
        assert "/bind" in update.message.reply_text.call_args[0][0]


# ---------------------------------------------------------------------------
# build_tg_app
# ---------------------------------------------------------------------------

class TestBuildTgApp:
    def test_wires_bot_data(self):
        max_client = MagicMock()
        topic_store = _make_topic_store()

        app = build_tg_app("123456:AAABBBCCC", max_client, "-100123456",
                            topic_store, allowed_user_id=777)

        assert app.bot_data[MAX_CLIENT_KEY] is max_client
        assert app.bot_data[TOPIC_STORE_KEY] is topic_store
        assert app.bot_data[ALLOWED_USER_KEY] == 777

    def test_allowed_user_id_none_when_unset(self):
        app = build_tg_app("123456:AAABBBCCC", MagicMock(), "-100123456",
                            _make_topic_store())

        assert app.bot_data[ALLOWED_USER_KEY] is None

    def test_wires_family_acl_and_admin(self):
        app = build_tg_app("123456:AAABBBCCC", MagicMock(), "-100123456",
                           _make_topic_store(), allowed_user_ids={100, 200, 300},
                           admin_user_id=100)

        assert app.bot_data[ALLOWED_USERS_KEY] == frozenset({100, 200, 300})
        assert app.bot_data[ADMIN_USER_KEY] == 100

    def test_registers_message_handler(self):
        app = build_tg_app("123456:AAABBBCCC", MagicMock(), "-100123456",
                            _make_topic_store())

        assert app.handlers[0]
