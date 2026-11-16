"""Telegram updates must survive loss of the in-memory queue."""
import json
from unittest.mock import MagicMock
from telegram import Bot, Update
from app.tg_ingress import DurableIngressQueue
from app.tg_outbox import TelegramOutbound
from app.topics import TopicStore
from app.tg_handler import build_tg_app

GROUP = -100123456
USER = 100

def event(message_id=5, update_id=99, thread_id=10, user_id=USER, text="exact text"):
    raw = {"update_id": update_id, "message": {
        "message_id": message_id, "date": 1794812400,
        "chat": {"id": GROUP, "type": "supergroup", "title": "Bridge"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Owner"},
        "message_thread_id": thread_id, "is_topic_message": True,
        "text": text,
    }}
    return Update.de_json(raw, Bot("123456:AAABBBCCC"))

def env(tmp_path):
    topics = TopicStore(str(tmp_path/"topics.json"))
    topics.set_topic(42, 10, "Sergey")
    ledger = TelegramOutbound(tmp_path/"tg-outbound.sqlite3", topics)
    queue = DurableIngressQueue(ledger, topics, GROUP, {USER})
    return topics, ledger, queue

async def test_update_is_durable_before_polling_can_acknowledge(tmp_path):
    topics, ledger, queue = env(tmp_path)
    await queue.put(event())
    assert queue.pending_count() == 1
    raw = ledger.db.execute("SELECT payload FROM inbound_updates").fetchone()[0]
    assert json.loads(raw)["message"]["text"] == "exact text"
    ledger.close()
    restored = TelegramOutbound(tmp_path/"tg-outbound.sqlite3", topics)
    recovered = DurableIngressQueue(restored, topics, GROUP, {USER})
    assert recovered.replay(Bot("123456:AAABBBCCC")) == 1
    update = recovered.get_nowait()
    assert update.message.text == "exact text"
    assert update.message.message_id == 5
    recovered.finish(update)
    assert recovered.pending_count() == 1  # Not yet accepted by the send ledger.
    restored.enqueue(GROUP, 5, 42, {"text": "exact text", "elements": [], "link": None})
    recovered.finish(update)
    assert recovered.pending_count() == 0
    assert restored.row(GROUP, 5)["status"] == "RECEIVED"
    restored.close()

async def test_replayed_update_already_in_outbound_ledger_is_not_sent_twice(tmp_path):
    topics, ledger, queue = env(tmp_path)
    await queue.put(event())
    ledger.enqueue(GROUP, 5, 42, {"text":"exact text","elements":[],"link":None})
    ledger.db.execute("UPDATE outbound SET status='SENT', max_id='confirmed'")
    ledger.close()
    restored = TelegramOutbound(tmp_path/"tg-outbound.sqlite3", topics)
    recovered = DurableIngressQueue(restored, topics, GROUP, {USER})
    assert recovered.replay(Bot("123456:AAABBBCCC")) == 0
    assert recovered.pending_count() == 0
    assert recovered.empty()
    restored.close()

async def test_intake_rejects_wrong_topic_and_unauthorized_user(tmp_path):
    _, ledger, queue = env(tmp_path)
    await queue.put(event(thread_id=99))
    await queue.put(event(update_id=100, user_id=200))
    assert queue.pending_count() == 0
    ledger.close()

async def test_queued_update_cannot_be_routed_into_reassigned_topic(tmp_path):
    topics, ledger, queue = env(tmp_path)
    update = event()
    await queue.put(update)
    topics.set_topic(43, 10, "Other")
    # Remove the old mapping if the TopicStore still retains it.
    assert str(topics.chat_for_topic(10)) == "43"
    assert not queue.may_process(update)
    assert queue.pending_count() == 1
    ledger.close()

def test_production_builder_installs_durable_queue(tmp_path):
    topics, ledger, _ = env(tmp_path)
    app = build_tg_app("123456:AAABBBCCC", MagicMock(), str(GROUP), topics,
                       allowed_user_id=USER, outbound_ledger=ledger)
    assert isinstance(app.update_queue, DurableIngressQueue)
    assert app.updater.update_queue is app.update_queue
    ledger.close()
