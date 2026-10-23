"""P0.1: durable checkpoints, reconnect replay and pagination."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.max_listener import catch_up_chat_history, catch_up_known_chats
from app.topics import TopicStore
from tests.test_self_forward import bridge


def row(n):
    return {"id": str(n), "time": n * 1000, "sender": 200, "text": f"msg {n}"}


def feed(messages):
    messages = sorted(messages, key=lambda m: m["time"], reverse=True)
    async def fetch(chat_id, count=100, from_time=None):
        return {"messages": [m for m in messages if m["time"] < from_time][:count]}
    return AsyncMock(side_effect=fetch)


async def test_recovery_only_sends_missing_in_order(tmp_path):
    client, sender, state = bridge(tmp_path)
    state.set_message(42, "1", 900)
    client.fetch_history = feed([row(i) for i in range(1, 5)])
    assert await catch_up_chat_history(client,sender,client.resolver,42,777)==3
    out = [call.args[0] for call in sender.send.await_args_list]
    assert all(f"msg {i}" in out[i-2] for i in range(2,5))
    assert state.get_history_cursor(42)==4000
    sender.send.reset_mock()
    assert await catch_up_chat_history(client,sender,client.resolver,42,777)==0
    sender.send.assert_not_awaited()


async def test_cursor_does_not_stop_at_newer_tg_origin(tmp_path):
    client, sender, state = bridge(tmp_path)
    state.set_history_cursor(42, 1000)
    state.set_message(42, "3", 902)
    client.fetch_history = feed([row(i) for i in range(1, 4)])
    assert await catch_up_chat_history(client,sender,client.resolver,42,777)==1
    assert "msg 2" in sender.send.await_args.args[0]
    assert state.get_history_cursor(42)==3000


async def test_unanchored_chat_baselines_without_replaying_old_messages(tmp_path):
    client, sender, state = bridge(tmp_path)
    client.fetch_history = feed([row(1),row(2)])
    assert await catch_up_chat_history(client,sender,client.resolver,42,777)==0
    assert state.get_history_cursor(42)==2000
    sender.send.assert_not_awaited()
    client.fetch_history = feed([row(1),row(2),row(3)])
    assert await catch_up_chat_history(client,sender,client.resolver,42,777)==1
    assert "msg 3" in sender.send.await_args.args[0]


async def test_pagination_persists_cursor_across_restart(tmp_path):
    client, sender, state = bridge(tmp_path)
    state.set_history_cursor(42,1000)
    client.fetch_history = feed([row(i) for i in range(1,9)])
    with patch("app.max_listener.CATCH_UP_BATCH",3):
        assert await catch_up_chat_history(client,sender,client.resolver,42,777,max_pages=5)==7
    assert client.fetch_history.await_count==4  # timestamp overlap across pages
    disk=TopicStore(str(tmp_path/"topics.json"))
    assert disk.get_history_cursor(42)==8000
    assert disk.tg_for_max_message(42,"8")==555


async def test_failed_send_does_not_advance_checkpoint(tmp_path):
    client, sender, state = bridge(tmp_path)
    state.set_history_cursor(42,1000)
    client.fetch_history = feed([row(1),row(2),row(3)])
    sender.send=AsyncMock(side_effect=[SimpleNamespace(message_id=551), None])
    await catch_up_chat_history(client,sender,client.resolver,42,777)
    assert state.get_history_cursor(42)==1000
    sender.send=AsyncMock(return_value=SimpleNamespace(message_id=552))
    assert await catch_up_chat_history(client,sender,client.resolver,42,777)==1
    assert state.get_history_cursor(42)==3000


async def test_failed_rpc_and_insufficient_history_leave_cursor_unchanged(tmp_path):
    client, sender, state = bridge(tmp_path)
    state.set_history_cursor(42,1000)
    client.fetch_history=AsyncMock(return_value=None)
    assert await catch_up_chat_history(client,sender,client.resolver,42,777)==0
    assert state.get_history_cursor(42)==1000
    client.fetch_history=feed([row(i) for i in range(1,11)])
    with patch("app.max_listener.CATCH_UP_BATCH",3):
        assert await catch_up_chat_history(client,sender,client.resolver,42,777,max_pages=2)==0
    assert state.get_history_cursor(42)==1000
    sender.send.assert_not_awaited()


async def test_known_chat_only_recovers_preexisting_topic(tmp_path):
    client, sender, state = bridge(tmp_path)
    state.set_history_cursor(42,1000)
    client.fetch_history=feed([row(1),row(2)])
    assert await catch_up_known_chats(client,sender,client.resolver,{},set())==0
    client.fetch_history.assert_not_awaited()
    assert await catch_up_known_chats(client,sender,client.resolver,{},{"42"})==1


def test_invalid_cursor_does_not_reset_topics(tmp_path):
    path=tmp_path/"topics.json"
    state=TopicStore(str(path))
    state.set_topic(42,777,"Test")
    data=json.loads(path.read_text())
    data["sync"]={"42":"broken"}
    path.write_text(json.dumps(data))
    restored=TopicStore(str(path))
    assert restored.get_topic(42)==777
    assert restored.get_history_cursor(42) is None
    restored.set_history_cursor(42,6000)
    restored.remove(42)
    assert restored.get_history_cursor(42) is None
