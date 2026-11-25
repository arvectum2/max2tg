"""Bounded derived receipt index, durable terminal evidence unchanged."""
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
import pytest
from app.capsule_egress_outbox import CapsuleEgressOutbox
from app.capsule_shared_listener import SharedCapsuleTextListener
from app.topics import TopicStore

SID='a'*24

def open_queue(root):
    return CapsuleEgressOutbox(root/'intents.sqlite3',SID)

def seed(q,n,chat='42'):
    q.db.execute('BEGIN IMMEDIATE')
    try:
        q.db.executemany("INSERT INTO worker_telegram_intents(source_key,text,fingerprint,status,telegram_id,updated_at,max_chat_id,max_message_id,submission_attempted) VALUES(?,'synthetic','synthetic','SENT',?,0,?,?,1)",((f'{chat}:{i}',i+100,chat,str(i)) for i in range(1,n+1)))
        q.db.execute('COMMIT')
    except BaseException:q.db.execute('ROLLBACK');raise

def listener(q,t):return SharedCapsuleTextListener(None,t,None,q,NS(sid=SID))


def test_bounded_history_no_repeated_eviction_and_restart(tmp_path):
    tmp_path.chmod(0o700);q=open_queue(tmp_path);t=TopicStore(str(tmp_path/'topics.json'),strict=True);t.set_topic(42,77,'test');seed(q,2500);a=listener(q,t)
    try:
        with patch.object(t,'_save',wraps=t._save) as save:
            assert a.repair_receipts()==1000;assert save.call_count==1
            for _ in range(3):assert a.repair_receipts()==0
            assert save.call_count==1
        assert t.tg_for_max_message(42,'1500') is None and t.tg_for_max_message(42,'1501')==1601 and t.tg_for_max_message(42,'2500')==2600
        assert q.counts()=={'SENT':2500};assert q.row('42:1')['telegram_id']==101
        q.close();q=open_queue(tmp_path);fresh=TopicStore(str(tmp_path/'topics.json'),strict=True);a=listener(q,fresh)
        with patch.object(fresh,'_save',wraps=fresh._save) as save:
            assert a.repair_receipts()==0;assert save.call_count==0
        # Restored empty index is rebuilt from the most recent confirmed rows.
        empty=TopicStore(str(tmp_path/'new.json'),strict=True);empty.set_topic(42,77,'test');a.topics=empty
        assert a.repair_receipts()==1000;assert empty.tg_for_max_message(42,'2500')==2600
        assert 'worker_receipt_chat' in ' '.join(str(tuple(r)) for r in q.db.execute("EXPLAIN QUERY PLAN SELECT max_chat_id,max_message_id,telegram_id FROM worker_telegram_intents WHERE status='SENT' AND receipt_seq>0 AND max_chat_id=? AND receipt_seq>? ORDER BY receipt_seq DESC LIMIT ?",('42',0,1000)))
    finally:q.close()


@pytest.mark.asyncio
async def test_old_pending_confirmation_and_late_binding(tmp_path):
    tmp_path.chmod(0o700);q=open_queue(tmp_path);t=TopicStore(str(tmp_path/'topics.json'),strict=True);t.set_topic(42,77,'test');a=listener(q,t)
    try:
        q.stage('early','early',max_chat_id=42,max_message_id='early');seed(q,10);assert a.repair_receipts()==10
        old=q.receipt_position();c=NS(status_verified=AsyncMock(return_value={'status':'SENT','telegram_id':10000}))
        assert await q.reconcile_one(c,'early')=='SENT';assert q.receipt_position()==old+1
        assert a.repair_receipts()==1;assert t.tg_for_max_message(42,'early')==10000
        seed(q,1,'43');assert a.repair_receipts()==0;t.set_topic(43,78,'new binding');assert a.repair_receipts()==1;assert t.tg_for_max_message(43,'1')==101
        pos=q.receipt_position();q.stage('uncertain','uncertain',max_chat_id=42,max_message_id='uncertain');c.status_verified.return_value={'status':'UNKNOWN'};assert await q.reconcile_one(c,'uncertain')=='UNKNOWN';assert q.receipt_position()==pos and a.repair_receipts()==0
    finally:q.close()


def test_legacy_migration_and_conflict_fail_closed(tmp_path):
    tmp_path.chmod(0o700);db=tmp_path/'intents.sqlite3';c=sqlite3.connect(db)
    c.executescript("""CREATE TABLE worker_telegram_intents(source_key TEXT PRIMARY KEY,text TEXT NOT NULL,fingerprint TEXT NOT NULL,status TEXT NOT NULL,telegram_id INTEGER,updated_at REAL NOT NULL,max_chat_id TEXT,max_message_id TEXT,submission_attempted INTEGER NOT NULL DEFAULT 1);INSERT INTO worker_telegram_intents VALUES('old','x','x','SENT',55,0,'42','old',1);INSERT INTO worker_telegram_intents VALUES('unknown','x','x','UNKNOWN',NULL,0,'42','unknown',1);""");c.close();db.chmod(0o600)
    q=open_queue(tmp_path);t=TopicStore(str(tmp_path/'topics.json'),strict=True);t.set_topic(42,77,'test');t.set_message(42,'old',99);a=listener(q,t)
    try:
        assert q.receipt_position()==1;assert q.row('old')['receipt_seq']==1 and q.row('unknown')['receipt_seq']==0
        with pytest.raises(RuntimeError,match='Conflicting'):a.repair_receipts()
        assert t.tg_for_max_message(42,'old')==99 and q.row('old')['telegram_id']==55
        t=TopicStore(str(tmp_path/'fresh.json'),strict=True);t.set_topic(42,77,'test');a.topics=t
        with patch.object(t,'_save',side_effect=lambda:setattr(t,'persist_failed',True)):
            with pytest.raises(RuntimeError,match='persistence failed'):a.repair_receipts()
        assert a._receipt_position==0
    finally:q.close()
