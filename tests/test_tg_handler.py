"""Tests for app/tg_handler.py — topic-based reply routing."""

from unittest.mock import AsyncMock, MagicMock

from app.tg_handler import (
    ADMIN_USER_KEY,
    ALLOWED_USER_KEY,
    ALLOWED_USERS_KEY,
    MAX_CLIENT_KEY,
    SUPERGROUP_KEY,
    TOPIC_STORE_KEY,
    _cmd_bind,
    _cmd_leave,
    _on_leave_callback,
    _on_topic_message,
    build_tg_app,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_topic_store(mapping: dict | None = None):
    """A TopicStore stand-in: chat_for_topic(thread_id) → max_chat_id."""
    mapping = mapping or {10: 42}
    store = MagicMock()
    store.chat_for_topic = MagicMock(side_effect=lambda tid: mapping.get(tid))
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
    bot_data = {ALLOWED_USER_KEY: allowed_user_id, ALLOWED_USERS_KEY: frozenset(allowed_user_ids or []), ADMIN_USER_KEY: admin_user_id}
    if max_client is not None:
        bot_data[MAX_CLIENT_KEY] = max_client
    if topic_store is not None:
        bot_data[TOPIC_STORE_KEY] = topic_store
    ctx.bot_data = bot_data
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
