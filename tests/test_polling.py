"""P0.4: Telegram pending updates must wait until MAX is connected."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.polling import bind_polling_to_max
from app.max_client import MaxClient
from app.health import HealthSupervisor
from app.outbox import OutboxStore
from app.topics import TopicStore


def setup():
    client=MaxClient("token","device")
    app=SimpleNamespace(updater=SimpleNamespace(
        running=False,
        start_polling=AsyncMock(),
        stop=AsyncMock(),
    ))
    old_ready=AsyncMock()
    old_disconnect=AsyncMock()
    client.on_ready(old_ready)
    client.on_disconnect(old_disconnect)
    bind_polling_to_max(client,app)
    return client,app,old_ready,old_disconnect


async def test_no_polling_before_max_authorization():
    client,app,old_ready,old_disconnect=setup()
    assert client._polling_should_run is False
    await client._on_ready_cb({"profile":{}})
    app.updater.start_polling.assert_not_awaited()
    assert client._polling_should_run is False


async def test_ready_enables_replaying_pending_updates_without_dropping():
    client,app,old_ready,old_disconnect=setup()
    client._authorized=True
    await client._on_ready_cb({"profile":{}})
    old_ready.assert_awaited_once()
    app.updater.start_polling.assert_awaited_once()
    args=app.updater.start_polling.await_args.kwargs
    assert args["drop_pending_updates"] is False
    assert client._polling_should_run is True


async def test_disconnect_pauses_and_reauth_resumes():
    client,app,old_ready,old_disconnect=setup()
    client._authorized=True
    await client._on_ready_cb({})
    app.updater.running=True
    await client._on_disconnect_cb()
    app.updater.stop.assert_awaited_once()
    old_disconnect.assert_awaited_once()
    assert client._polling_should_run is False
    app.updater.running=False
    await client._on_ready_cb({})
    assert app.updater.start_polling.await_count==2


async def test_ready_when_polling_already_active_does_not_start_again():
    client,app,old_ready,old_disconnect=setup()
    app.updater.running=True
    client._authorized=True
    await client._on_ready_cb({})
    app.updater.start_polling.assert_not_awaited()


async def test_failed_start_does_not_block_next_health_repair(tmp_path):
    client,app,old_ready,old_disconnect=setup()
    client._authorized=True
    app.updater.start_polling.side_effect=RuntimeError("temporary network failure")
    await client._on_ready_cb({})
    assert client._polling_should_run is True
    assert app.updater.start_polling.await_count==1
    state=TopicStore(str(tmp_path/"topic.json"))
    db=OutboxStore(str(tmp_path/"outbox.sqlite3"))
    bot_sender=MagicMock()
    bot_sender.chat_id="-100"
    bot_sender.bot.send_message=AsyncMock()
    app.running=True
    client._ws=SimpleNamespace(closed=False)
    client._last_rx_monotonic=__import__('time').monotonic()
    client._authorized=True
    health=HealthSupervisor(client,bot_sender,db,state,app,str(tmp_path))
    assert "TELEGRAM_POLLING" in health.snapshot()["issues"]
    app.updater.start_polling.side_effect=None
    await health.repair(health.snapshot())
    assert app.updater.start_polling.await_count==2
    db.close()


def test_health_paused_state_is_not_false_alarm(tmp_path):
    client,app,old_ready,old_disconnect=setup()
    from app.health import status_text
    state=TopicStore(str(tmp_path/"topic.json"))
    db=OutboxStore(str(tmp_path/"outbox.sqlite3"))
    sender=SimpleNamespace(chat_id="-100",bot=MagicMock())
    app.running=True
    health=HealthSupervisor(client,sender,db,state,app,str(tmp_path))
    report=health.snapshot()
    assert report["telegram"]["paused_expected"]
    assert "TELEGRAM_POLLING" not in report["issues"]
    assert "ожидает MAX" in status_text(report)
    db.close()
