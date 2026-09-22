from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.max_client import MaxClient
from app.max_listener import import_recent_history


async def test_import_recent_history_orders_oldest_first_and_keeps_self_messages():
    client = MaxClient(token="tok", device_id="dev")
    client._my_id = 100
    client.fetch_history = AsyncMock(return_value={
        "messages": [
            {"id": "2", "sender": 200, "time": 1700000060000, "text": "second"},
            {"id": "1", "sender": 100, "time": 1700000000000, "text": "first"},
        ]
    })

    resolver = MagicMock()
    resolver.resolve_user = AsyncMock(return_value="Alice")
    resolver.is_dm.return_value = False
    resolver.chat_name.return_value = "Test chat"

    store = MagicMock()
    store.tg_for_max_message.return_value = None

    sender = MagicMock()
    sender.topic_store = store
    sender.send = AsyncMock(side_effect=[
        SimpleNamespace(message_id=501),
        SimpleNamespace(message_id=502),
    ])

    imported = await import_recent_history(
        client, sender, resolver, -123, thread_id=77, count=20,
    )

    assert imported == 2
    assert sender.send.await_count == 2
    first_text = sender.send.await_args_list[0].args[0]
    second_text = sender.send.await_args_list[1].args[0]
    assert "first" in first_text
    assert "Вы" in first_text
    assert "second" in second_text
    assert "Alice" in second_text
    assert sender.send.await_args_list[0].kwargs["message_thread_id"] == 77
    assert store.set_message.call_args_list[0].args == (-123, "1", 501)
    assert store.set_message.call_args_list[1].args == (-123, "2", 502)


async def test_import_recent_history_skips_already_mapped_message():
    client = MaxClient(token="tok", device_id="dev")
    client.fetch_history = AsyncMock(return_value={
        "messages": [
            {"id": "1", "sender": 200, "time": 1700000000000, "text": "old"},
            {"id": "2", "sender": 200, "time": 1700000060000, "text": "new"},
        ]
    })

    resolver = MagicMock()
    resolver.resolve_user = AsyncMock(return_value="Alice")
    resolver.is_dm.return_value = False
    resolver.chat_name.return_value = "Test chat"

    store = MagicMock()
    store.tg_for_max_message.side_effect = lambda chat_id, msg_id: 400 if str(msg_id) == "1" else None

    sender = MagicMock()
    sender.topic_store = store
    sender.send = AsyncMock(return_value=SimpleNamespace(message_id=502))

    imported = await import_recent_history(
        client, sender, resolver, -123, thread_id=77, count=20,
    )

    assert imported == 1
    sender.send.assert_awaited_once()
    assert "new" in sender.send.await_args.args[0]


async def test_import_recent_history_zero_does_not_call_max():
    client = MaxClient(token="tok", device_id="dev")
    client.fetch_history = AsyncMock()
    sender = MagicMock()
    resolver = MagicMock()

    imported = await import_recent_history(
        client, sender, resolver, -123, thread_id=77, count=0,
    )

    assert imported == 0
    client.fetch_history.assert_not_awaited()
