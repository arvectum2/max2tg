"""P0.5 isolated failure/kill acceptance; no real MAX/TG traffic or production state."""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import TimedOut, RetryAfter

from app.max_client import MaxMessage
from app.max_listener import catch_up_chat_history
from app.outbox import OutboxStore
from app.topics import TopicStore
from tests.test_outbox import fixture, event
from tests.test_catch_up import feed, row


def _child(code, dbpath):
    """An actual separate Python process writes SQLite state and exits abruptly."""
    proc = subprocess.run(
        [sys.executable, "-c", code, str(dbpath)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=12,
    )
    assert proc.returncode == 83, (proc.returncode, proc.stderr)
    return proc


def test_process_killed_during_telegram_send_quarantines_unknown(tmp_path):
    dbpath=tmp_path/"outbox.sqlite3"
    _child("""
import os,sys
from app.outbox import OutboxStore
from app.max_client import MaxMessage
s=OutboxStore(sys.argv[1])
key=s.enqueue(MaxMessage(chat_id=42,message_id='inflight',text='top secret'))
s._insert_part(key,0,'send_message','digest',{'text':'private'},None)
s._update_part(key,0,'SENDING')
s._set_job(key,'SENDING')
os._exit(83)
""",dbpath)
    ledger=OutboxStore(str(dbpath))
    key=ledger.job_key(42,"inflight")
    assert ledger.job(key)["status"]=="UNKNOWN"
    assert ledger._part(key,0)["state"]=="UNKNOWN"
    assert not ledger.recoverable()
    assert ledger.db.execute("PRAGMA quick_check").fetchone()[0]=="ok"
    ledger.close()


def test_process_killed_before_network_request_recovers_safely(tmp_path):
    dbpath=tmp_path/"outbox.sqlite3"
    _child("""
import os,sys
from app.outbox import OutboxStore
from app.max_client import MaxMessage
s=OutboxStore(sys.argv[1])
key=s.enqueue(MaxMessage(chat_id=42,message_id='before-wire',text='private'))
s._set_job(key,'SENDING')
os._exit(83)
""",dbpath)
    ledger=OutboxStore(str(dbpath))
    key=ledger.job_key(42,"before-wire")
    assert ledger.job(key)["status"]=="RETRY"
    tasks=ledger.recoverable()
    assert len(tasks)==1 and tasks[0].message_id=="before-wire"
    ledger.close()


def test_process_killed_after_first_part_ack_preserves_receipt(tmp_path):
    dbpath=tmp_path/"outbox.sqlite3"
    _child("""
import os,sys
from app.outbox import OutboxStore
from app.max_client import MaxMessage
s=OutboxStore(sys.argv[1])
key=s.enqueue(MaxMessage(chat_id=42,message_id='partial',text='part one part two'))
s._insert_part(key,0,'send_message','fingerprint',{'part':1},None)
s._update_part(key,0,'SENT',101)
s._insert_part(key,1,'send_message','fingerprint2',{'part':2},None)
s._update_part(key,1,'SENDING')
s._set_job(key,'SENDING')
os._exit(83)
""",dbpath)
    ledger=OutboxStore(str(dbpath))
    key=ledger.job_key(42,"partial")
    assert ledger.job(key)["status"]=="UNKNOWN"
    assert ledger._part(key,0)["tg_id"]==101
    assert ledger._part(key,0)["state"]=="SENT"
    assert ledger._part(key,1)["state"]=="UNKNOWN"
    assert not ledger.recoverable()
    ledger.close()


async def test_retry_after_reconnect_resumes_without_repeating_confirmed_parts(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    msg=event("rate", "💬"*5500)
    sender._bot.send_message=AsyncMock(side_effect=[
        SimpleNamespace(message_id=71),RetryAfter(1)])
    await client._on_message_cb(msg)
    key=db.job_key(42,"rate")
    assert db.job(key)["status"]=="RETRY"
    db.close()
    new=OutboxStore(str(tmp_path/"outbox.sqlite3"))
    sender.outbox=new
    new.db.execute("UPDATE jobs SET next_attempt=0 WHERE key=?",(key,))
    sender._bot.send_message=AsyncMock(side_effect=[
        SimpleNamespace(message_id=72), SimpleNamespace(message_id=73),
        SimpleNamespace(message_id=74)])
    for item in new.recoverable():
        await client._on_message_cb(item)
    assert new.job(key)["status"]=="SENT"
    assert new._part(key,0)["tg_id"]==71
    assert state.tg_for_max_message(42,"rate")==71
    assert sender._bot.send_message.await_count>=1
    new.close()


async def test_timeout_never_automatically_retries_after_restart(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    sender._bot.send_message=AsyncMock(side_effect=TimedOut())
    await client._on_message_cb(event("timeout-42"))
    assert db.counts()=={"UNKNOWN":1}
    db.close()
    reopened=OutboxStore(str(tmp_path/"outbox.sqlite3"))
    assert reopened.counts()=={"UNKNOWN":1}
    assert not reopened.recoverable()
    reopened.close()


async def test_cursor_does_not_advance_when_history_window_exceeds_budget(tmp_path):
    client,sender,state=fixture(tmp_path)[:3]
    state.set_history_cursor(42,1000)
    client.fetch_history=feed([row(i) for i in range(1,24)])
    with patch("app.max_listener.CATCH_UP_BATCH",4):
        assert await catch_up_chat_history(
            client,sender,client.resolver,42,777,max_pages=2)==0
    assert state.get_history_cursor(42)==1000
    sender._bot.send_message.assert_not_called()


async def test_same_timestamp_at_pagination_boundary_is_not_silently_lost(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    state.set_history_cursor(42,1000)
    # MAX returns 3 most-recent records, but an omitted fourth has identical
    # timestamp to the page boundary; from_time=min_ts-1 skips that record.
    def datum(i,ts):
        return {"id":str(i),"time":ts,"sender":200,"text":f"same {i}"}
    items=[datum(1,1000),datum(2,2000),datum(3,2000),
           datum(4,2000),datum(5,3000)]
    client.fetch_history=feed(items)
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=700))
    with patch("app.max_listener.CATCH_UP_BATCH",3):
        result=await catch_up_chat_history(
            client,sender,client.resolver,42,777,max_pages=5)
    # Never claim all 4 new events were delivered if pagination lost a tie.
    assert state.get_history_cursor(42)!=3000 or all(
        state.tg_for_max_message(42,str(i)) is not None for i in (2,3,4,5))
    db.close()


async def test_failed_history_window_raises_persistent_health_flag(tmp_path):
    from app.health import HealthSupervisor, status_text
    client,sender,state,db=fixture(tmp_path)
    state.set_history_cursor(42,1000)
    client.fetch_history=feed([row(i) for i in range(1,18)])
    with patch("app.max_listener.CATCH_UP_BATCH",3):
        assert await catch_up_chat_history(
            client,sender,client.resolver,42,777,max_pages=1)==0
    warnings=state.get_ui("catch_up_warnings",{})
    assert warnings["42"]["reason"]=="BUDGET_EXCEEDED"
    restarted=TopicStore(str(tmp_path/"topics.json"))
    assert "42" in restarted.get_ui("catch_up_warnings")
    app=SimpleNamespace(running=True,updater=SimpleNamespace(running=True))
    supervisor=HealthSupervisor(client,sender,db,restarted,app,str(tmp_path))
    snapshot=supervisor.snapshot()
    assert snapshot["history_gaps"]==1
    assert "HISTORY_GAP" in snapshot["issues"]
    assert "Проблемы догрузки: 1" in status_text(snapshot)
    # A subsequent complete reconciliation clears the same diagnostic.
    client.fetch_history=feed([row(1),row(2)])
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=300))
    assert await catch_up_chat_history(client,sender,client.resolver,42,777,max_pages=10)==1
    assert state.get_ui("catch_up_warnings")=={}
    db.close()


async def test_sqlite_write_lock_prevents_network_send_without_data_loss(tmp_path):
    import sqlite3
    client,sender,state,db=fixture(tmp_path)
    lock=sqlite3.connect(str(tmp_path/"outbox.sqlite3"),isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    db.db.execute("PRAGMA busy_timeout=25")
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=555))
    try:
        with pytest.raises(sqlite3.OperationalError):
            await client._on_message_cb(event("disk-locked"))
    finally:
        lock.rollback()
        lock.close()
    sender._bot.send_message.assert_not_awaited()
    assert not db.db.execute("SELECT 1 FROM jobs WHERE max_id='disk-locked'").fetchone()
    await client._on_message_cb(event("disk-locked"))
    assert state.tg_for_max_message(42,"disk-locked")==555
    sender._bot.send_message.assert_awaited_once()
    db.close()
