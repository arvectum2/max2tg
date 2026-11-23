"""Offline two-tenant tests for central text-only Telegram egress."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.capsule_registry import CapsuleRegistry
from app.telegram_control_plane import TelegramControlPlane
from app.telegram_egress import TelegramEgress


def setup(tmp_path):
    registry = CapsuleRegistry(tmp_path / "private")
    plane = TelegramControlPlane(registry)
    sessions = []
    for actor, chat in ((111, -100001), (222, -100002)):
        owner = registry.create_account()
        sid = registry.create_capsule(owner)
        registry.db.execute(
            "INSERT INTO tg_owners VALUES(?,?)", (str(actor), owner),
        )
        registry.db.execute(
            "UPDATE capsules SET desired_active=1 WHERE session_id=?",
            (sid,),
        )
        registry.db.execute(
            "INSERT INTO tg_routes VALUES(?,?,?,?,?)",
            (str(chat), sid, owner, "ACTIVE", 1.0),
        )
        sessions.append((sid, actor, chat))
    ledger = TelegramEgress(registry.root / "telegram-egress.sqlite3", plane)
    return registry, plane, ledger, sessions


def close(registry, ledger):
    ledger.close()
    registry.close()


@pytest.mark.asyncio
async def test_two_tenants_have_disjoint_egress_targets(tmp_path):
    reg, plane, ledger, ((a, _, ca), (b, _, cb)) = setup(tmp_path)
    assert ledger.enqueue(a, "max-msg-1", "Текст A") == "QUEUED"
    assert ledger.enqueue(b, "max-msg-1", "Текст B") == "QUEUED"
    bot = SimpleNamespace(send_message=AsyncMock(
        side_effect=[SimpleNamespace(message_id=501),
                     SimpleNamespace(message_id=502)]
    ))
    assert await ledger.drain(bot) == {"SENT": 2}
    calls = bot.send_message.await_args_list
    assert [call.kwargs["chat_id"] for call in calls] == [ca, cb]
    assert [call.kwargs["text"] for call in calls] == ["Текст A", "Текст B"]
    assert all(call.kwargs["parse_mode"] is None for call in calls)
    assert ledger.row(a, "max-msg-1")["telegram_id"] == 501
    assert ledger.row(b, "max-msg-1")["telegram_id"] == 502
    assert ledger.counts() == {"SENT": 2}
    close(reg, ledger)


def test_immutable_event_dedup_and_mutation_block(tmp_path):
    reg, _, ledger, ((a, _, _), _) = setup(tmp_path)
    assert ledger.enqueue(a, "origin:42", "hello") == "QUEUED"
    assert ledger.enqueue(a, "origin:42", "hello") == "QUEUED"
    assert ledger.ready() == [(a, "origin:42")]
    with pytest.raises(ValueError, match="changed"):
        ledger.enqueue(a, "origin:42", "altered")
    assert ledger.ready() == []
    assert ledger.row(a, "origin:42")["status"] == "UNKNOWN"
    close(reg, ledger)


@pytest.mark.asyncio
async def test_revoked_forum_blocks_previously_queued_message(tmp_path):
    reg, plane, ledger, ((sid, _, chat), _) = setup(tmp_path)
    assert ledger.enqueue(sid, "msg-10", "not sent") == "QUEUED"
    plane.disable_forum(chat)
    bot = SimpleNamespace(send_message=AsyncMock())
    assert await ledger.send_one(bot, sid, "msg-10") == "BLOCKED"
    bot.send_message.assert_not_awaited()
    assert ledger.ready() == []
    close(reg, ledger)


@pytest.mark.asyncio
async def test_ambiguous_bot_failure_never_retries(tmp_path):
    reg, _, ledger, ((sid, _, _), _) = setup(tmp_path)
    ledger.enqueue(sid, "msg-11", "possible send")
    bot = SimpleNamespace(send_message=AsyncMock(
        side_effect=TimeoutError("private error: don't log")
    ))
    assert await ledger.send_one(bot, sid, "msg-11") == "UNKNOWN"
    assert ledger.ready() == []
    assert await ledger.send_one(bot, sid, "msg-11") == "UNKNOWN"
    assert bot.send_message.await_count == 1
    close(reg, ledger)


@pytest.mark.asyncio
async def test_stale_sending_is_unknown_after_restart(tmp_path):
    reg, plane, ledger, ((sid, _, _), _) = setup(tmp_path)
    ledger.enqueue(sid, "msg-12", "lost ack")
    ledger.db.execute(
        "UPDATE telegram_egress SET status='SENDING' "
        "WHERE session_id=? AND source_key=?",
        (sid, "msg-12"),
    )
    ledger.close()
    again = TelegramEgress(reg.root / "telegram-egress.sqlite3", plane)
    assert again.row(sid, "msg-12")["status"] == "UNKNOWN"
    assert again.ready() == []
    close(reg, again)


@pytest.mark.asyncio
async def test_concurrent_claims_send_exactly_once(tmp_path):
    reg, _, ledger, ((sid, _, _), _) = setup(tmp_path)
    ledger.enqueue(sid, "m:13", "first attempt")
    async def slow_send(**_kwargs):
        await asyncio.sleep(0)
        return SimpleNamespace(message_id=130)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=slow_send))
    results = await asyncio.gather(
        ledger.send_one(bot, sid, "m:13"),
        ledger.send_one(bot, sid, "m:13"),
    )
    assert results == ["SENT", "SENT"]
    bot.send_message.assert_awaited_once()
    close(reg, ledger)


@pytest.mark.asyncio
async def test_cancellation_makes_delivery_unknown(tmp_path):
    reg, _, ledger, ((sid, _, _), _) = setup(tmp_path)
    ledger.enqueue(sid, "m:14", "maybe")
    bot = SimpleNamespace(send_message=AsyncMock(
        side_effect=asyncio.CancelledError
    ))
    with pytest.raises(asyncio.CancelledError):
        await ledger.send_one(bot, sid, "m:14")
    assert ledger.row(sid, "m:14")["status"] == "UNKNOWN"
    assert ledger.ready() == []
    close(reg, ledger)


@pytest.mark.asyncio
async def test_missing_bot_ack_never_claims_sent(tmp_path):
    reg, _, ledger, ((sid, _, _), _) = setup(tmp_path)
    ledger.enqueue(sid, "m:15", "no acknowledgment")
    bot = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=None)
    ))
    assert await ledger.send_one(bot, sid, "m:15") == "UNKNOWN"
    assert ledger.row(sid, "m:15")["telegram_id"] is None
    close(reg, ledger)


@pytest.mark.parametrize("key,text", [
    ("bad key", "hello"), ("", "hello"), ("msg", ""),
    ("msg", "\x00bad"), ("msg", "🧩" * 2049),
])
def test_invalid_source_or_utf16_payload_denied(tmp_path, key, text):
    reg, _, ledger, ((sid, _, _), _) = setup(tmp_path)
    with pytest.raises(ValueError):
        ledger.enqueue(sid, key, text)
    assert ledger.ready() == []
    close(reg, ledger)


def test_foreign_session_and_inactive_route_denied(tmp_path):
    reg, plane, ledger, ((sid, _, _), (other, _, _)) = setup(tmp_path)
    with pytest.raises(ValueError):
        ledger.enqueue("other-session", "a", "text")
    reg.set_active(other, False)
    with pytest.raises(PermissionError):
        ledger.enqueue(other, "m:16", "other")
    ledger.enqueue(sid, "m:16", "valid")
    assert ledger.ready() == [(sid, "m:16")]
    close(reg, ledger)


@pytest.mark.asyncio
async def test_no_owner_bound_means_no_egress_delivery(tmp_path):
    reg, plane, ledger, ((sid, actor, _), _) = setup(tmp_path)
    ledger.enqueue(sid, "m:17", "hello")
    reg.db.execute("DELETE FROM tg_owners WHERE tg_user_id=?", (str(actor),))
    bot = SimpleNamespace(send_message=AsyncMock())
    assert await ledger.send_one(bot, sid, "m:17") == "BLOCKED"
    bot.send_message.assert_not_awaited()
    close(reg, ledger)


@pytest.mark.asyncio
async def test_reopen_preserves_sent_without_redispatch(tmp_path):
    reg, plane, ledger, ((sid, _, _), _) = setup(tmp_path)
    ledger.enqueue(sid, "m:18", "persisted")
    bot = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=99)
    ))
    assert await ledger.send_one(bot, sid, "m:18") == "SENT"
    ledger.close()
    reloaded = TelegramEgress(reg.root / "telegram-egress.sqlite3", plane)
    assert reloaded.row(sid, "m:18")["telegram_id"] == 99
    assert reloaded.ready() == []
    bot.send_message.reset_mock()
    assert await reloaded.send_one(bot, sid, "m:18") == "SENT"
    bot.send_message.assert_not_awaited()
    close(reg, reloaded)


def test_private_path_restricts_symlink_and_external_storage(tmp_path):
    reg, plane, ledger, ((sid, _, _), _) = setup(tmp_path)
    assert ledger.path.stat().st_mode & 0o077 == 0
    ledger.close()
    with pytest.raises(ValueError, match="private registry root"):
        TelegramEgress(tmp_path / "outside.sqlite3", plane)
    link = reg.root / "linked-egress.sqlite3"
    link.symlink_to(reg.root / "telegram-egress.sqlite3")
    with pytest.raises(ValueError, match="Unsafe"):
        TelegramEgress(link, plane)
    assert Path(reg.root).stat().st_mode & 0o077 == 0
    reg.close()


@pytest.mark.asyncio
async def test_gateway_connects_to_central_egress_without_bot_polling(tmp_path):
    from app.telegram_gateway import TelegramGatewayEngine
    from app.telegram_inbox import TelegramInbox
    reg, plane, ledger, ((sid, _, chat), _) = setup(tmp_path)
    inbox = TelegramInbox(reg.root / "updates.sqlite3", plane)
    gateway = TelegramGatewayEngine(plane, inbox, egress=ledger)
    assert gateway.enqueue_egress_text(sid, "max:18", "from MAX") == "QUEUED"
    bot = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=201)
    ))
    assert await gateway.drain_egress(bot) == {"SENT": 1}
    bot.send_message.assert_awaited_once_with(
        chat_id=chat, text="from MAX", parse_mode=None
    )
    assert gateway.enqueue_egress_text(sid, "max:18", "from MAX") == "SENT"
    inbox.close()
    close(reg, ledger)


@pytest.mark.asyncio
async def test_gateway_fails_closed_without_explicit_egress(tmp_path):
    from app.telegram_gateway import TelegramGatewayEngine
    from app.telegram_inbox import TelegramInbox
    reg, plane, ledger, ((sid, _, _), _) = setup(tmp_path)
    inbox = TelegramInbox(reg.root / "updates.sqlite3", plane)
    gateway = TelegramGatewayEngine(plane, inbox)
    with pytest.raises(RuntimeError, match="not configured"):
        gateway.enqueue_egress_text(sid, "m:20", "ignored")
    with pytest.raises(RuntimeError, match="not configured"):
        await gateway.drain_egress(SimpleNamespace())
    assert ledger.counts() == {}
    inbox.close()
    close(reg, ledger)


@pytest.mark.asyncio
async def test_two_egress_instances_cannot_double_send(tmp_path):
    reg, plane, first, ((sid, _, _), _) = setup(tmp_path)
    second = TelegramEgress(reg.root / "telegram-egress.sqlite3", plane)
    first.enqueue(sid, "m:99", "unique")
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def send(**_kwargs):
        entered.set()
        await resume.wait()
        return SimpleNamespace(message_id=99)

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    task = asyncio.create_task(first.send_one(bot, sid, "m:99"))
    await asyncio.wait_for(entered.wait(), 1)
    # The other process sees SENDING, never issues a second Bot API call.
    assert await second.send_one(bot, sid, "m:99") == "SENDING"
    resume.set()
    assert await task == "SENT"
    bot.send_message.assert_awaited_once()
    assert first.row(sid, "m:99")["status"] == "SENT"
    second.close()
    close(reg, first)


@pytest.mark.asyncio
async def test_no_replay_on_timeout_after_recovery(tmp_path):
    reg, plane, ledger, ((sid, _, _), _) = setup(tmp_path)
    ledger.enqueue(sid, "evt:timeout", "hello")
    bot = SimpleNamespace(send_message=AsyncMock(
        side_effect=TimeoutError("ambiguous")
    ))
    assert await ledger.drain(bot) == {"UNKNOWN": 1}
    ledger.close()
    restored = TelegramEgress(reg.root / "telegram-egress.sqlite3", plane)
    assert await restored.drain(bot) == {}
    bot.send_message.assert_awaited_once()
    close(reg, restored)


@pytest.mark.asyncio
async def test_cannot_route_to_disabled_tenant_via_other_session(tmp_path):
    reg, plane, ledger, ((first, _, chat_a), (second, _, chat_b)) = setup(tmp_path)
    ledger.enqueue(first, "m:100", "A")
    ledger.enqueue(second, "m:100", "B")
    plane.disable_forum(chat_b)
    bot = SimpleNamespace(send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=22)
    ))
    result = await ledger.drain(bot)
    assert result == {"SENT": 1, "BLOCKED": 1}
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == chat_a
    assert ledger.row(second, "m:100")["status"] == "BLOCKED"
    close(reg, ledger)


def test_ready_batch_limit_and_utf16_boundary(tmp_path):
    reg, _, ledger, ((sid, _, _), _) = setup(tmp_path)
    assert ledger.enqueue(sid, "m:101", "🧩" * 2048) == "QUEUED"
    with pytest.raises(ValueError, match="too long"):
        ledger.enqueue(sid, "m:102", "🧩" * 2049)
    with pytest.raises(ValueError):
        ledger.ready(limit=0)
    with pytest.raises(ValueError):
        ledger.ready(limit=True)
    assert ledger.ready(limit=1) == [(sid, "m:101")]
    close(reg, ledger)
