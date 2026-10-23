"""Offline safety checks for Telegram-originated MAX changes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.tg_mutation_outbox import TgMutationOutbox


def client():
    return SimpleNamespace(_authorized=True,
        edit_message=AsyncMock(return_value={"message":{"id":"123"}}),
        react_to_message=AsyncMock(return_value={"ok":True}))


async def test_edit_confirmation_and_duplicate(tmp_path):
    ledger=TgMutationOutbox(tmp_path/"changes.sqlite3")
    key=ledger.enqueue("edit",42,"123",{"text":"fixed","elements":[]})
    obj=client()
    assert await ledger.apply(key,obj)=="SENT"
    assert ledger.enqueue("edit",42,"123",{"text":"fixed","elements":[]})==key
    assert await ledger.apply(key,obj)=="SENT"
    assert obj.edit_message.await_count==1
    ledger.close()


async def test_reaction_and_duplicate(tmp_path):
    ledger=TgMutationOutbox(tmp_path/"changes.sqlite3")
    key=ledger.enqueue("reaction",42,"123",{"emoji":"👍"})
    obj=client()
    assert await ledger.apply(key,obj)=="SENT"
    assert await ledger.apply(key,obj)=="SENT"
    assert obj.react_to_message.await_count==1
    ledger.close()


async def test_timeout_is_unknown_and_cannot_retry(tmp_path):
    path=tmp_path/"changes.sqlite3"
    ledger=TgMutationOutbox(path)
    key=ledger.enqueue("edit",42,"123",{"text":"fixed","elements":[]})
    obj=client()
    obj.edit_message.side_effect=TimeoutError("unknown")
    with pytest.raises(TimeoutError):
        await ledger.apply(key,obj)
    ledger.close()
    again=TgMutationOutbox(path)
    obj.edit_message=AsyncMock()
    assert await again.apply(key,obj)=="UNKNOWN"
    obj.edit_message.assert_not_awaited()
    again.close()


def test_crash_during_max_rpc_becomes_unknown(tmp_path):
    path=tmp_path/"changes.sqlite3"
    ledger=TgMutationOutbox(path)
    key=ledger.enqueue("reaction",42,"123",{"emoji":None})
    ledger.db.execute("UPDATE tg_mutations SET status='SENDING'")
    ledger.close()
    again=TgMutationOutbox(path)
    assert again.db.execute("SELECT status FROM tg_mutations").fetchone()[0]=="UNKNOWN"
    again.close()


async def test_offline_is_not_an_attempt(tmp_path):
    ledger=TgMutationOutbox(tmp_path/"changes.sqlite3")
    key=ledger.enqueue("edit",42,"123",{"text":"later","elements":[]})
    obj=client()
    obj._authorized=False
    assert await ledger.apply(key,obj)=="RECEIVED"
    obj.edit_message.assert_not_awaited()
    ledger.close()


async def test_wrong_max_edit_ack_is_unknown(tmp_path):
    ledger=TgMutationOutbox(tmp_path/"changes.sqlite3")
    key=ledger.enqueue("edit",42,"123",{"text":"x","elements":[]})
    obj=client()
    obj.edit_message.return_value={"message":{"id":"wrong"}}
    assert await ledger.apply(key,obj)=="UNKNOWN"
    ledger.close()


def test_reject_bad_content(tmp_path):
    ledger=TgMutationOutbox(tmp_path/"changes.sqlite3")
    with pytest.raises(ValueError):
        ledger.enqueue("edit",42,123,{"text":"x"})
    with pytest.raises(ValueError):
        ledger.enqueue("delete",42,123,{})
    ledger.close()
