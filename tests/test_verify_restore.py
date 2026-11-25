"""Real temporary restore and corruption checks without live state writes."""
import sqlite3
from pathlib import Path
import pytest

from app.topics import TopicStore
from app.outbox import OutboxStore
from app.tg_outbox import TelegramOutbound
from scripts.verify_backup_restore import verify_restore


def test_restore_entire_personal_state(tmp_path):
    state=TopicStore(str(tmp_path/"topics.json"))
    state.set_topic(42,777,"Chat")
    state.set_message(42,"max001",1001)
    state.set_history_cursor(42,1792000000000)
    outbox=OutboxStore(str(tmp_path/"outbox.sqlite3"))
    ledger=TelegramOutbound(tmp_path/"tg-outbound.sqlite3",state)
    ledger.enqueue(-100123,1002,42,
                   {"text":"text","elements":[],"link":None})
    outbox.close()
    ledger.close()
    before=(tmp_path/"topics.json").read_bytes()
    report=verify_restore(tmp_path/"topics.json",tmp_path/"outbox.sqlite3",
                          tmp_path/"tg-outbound.sqlite3")
    assert report["passed"] is True
    assert report["topics"]==1
    assert report["mappings"]==1
    assert report["cursors"]==1
    assert report["tg_outbound_status_counts"]=={"RECEIVED":1}
    assert (tmp_path/"topics.json").read_bytes()==before


def test_corrupt_sqlite_is_refused(tmp_path):
    topics=TopicStore(str(tmp_path/"topics.json"))
    topics.set_topic(42,11,"Test")
    bad=tmp_path/"bad.sqlite3"
    bad.write_bytes(b"not-sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        verify_restore(tmp_path/"topics.json",bad)


def test_refuse_symlink_to_private_topics(tmp_path):
    topics=TopicStore(str(tmp_path/"topics.json"))
    topics.set_topic(42,11,"Test")
    db=OutboxStore(str(tmp_path/"good.sqlite3"))
    db.close()
    alias=tmp_path/"alias.json"
    alias.symlink_to(tmp_path/"topics.json")
    with pytest.raises(ValueError):
        verify_restore(alias,tmp_path/"good.sqlite3")


def test_restore_album_members_and_mutations_without_replaying(tmp_path):
    from app.tg_mutation_outbox import TgMutationOutbox
    from tests.test_tg_albums import setup_group
    t=TopicStore(str(tmp_path/'topics.json'));t.set_topic(42,10,'Test')
    o=OutboxStore(str(tmp_path/'outbox.sqlite3'));o.close()
    l=TelegramOutbound(tmp_path/'tg-outbound.sqlite3',t);setup_group(l)
    l.db.execute('CREATE TABLE inbound_updates(update_id INTEGER PRIMARY KEY,payload TEXT)');l.db.execute('INSERT INTO inbound_updates VALUES(1,?)',('{}',));l.close()
    m=TgMutationOutbox(tmp_path/'tg-mutations.sqlite3');k=m.enqueue('reaction',42,'123',{'emoji':'👍'});m.db.execute("UPDATE tg_mutations SET status='SENDING' WHERE key=?",(k,));m.close()
    r=verify_restore(tmp_path/'topics.json',tmp_path/'outbox.sqlite3',tmp_path/'tg-outbound.sqlite3',tmp_path/'tg-mutations.sqlite3')
    assert r['pending_album_members']==3 and r['pending_ingress']==1
    assert r['tg_outbound_status_counts']=={'RECEIVED':3}
    assert r['tg_mutation_status_counts']=={'UNKNOWN':1}
    with sqlite3.connect(tmp_path/'tg-mutations.sqlite3') as c:assert c.execute('SELECT status FROM tg_mutations').fetchone()[0]=='SENDING'


def test_explicit_missing_restore_source_fails_closed(tmp_path):
    t=TopicStore(str(tmp_path/'topics.json'));t.set_topic(42,10,'Test')
    o=OutboxStore(str(tmp_path/'outbox.sqlite3'));o.close()
    with pytest.raises(ValueError):verify_restore(tmp_path/'topics.json',tmp_path/'outbox.sqlite3',tmp_path/'missing.sqlite3')
