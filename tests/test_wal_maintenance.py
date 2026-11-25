import sqlite3
from pathlib import Path
import pytest
from app.wal_maintenance import WalMaintenance


def setup(path):
    db=sqlite3.connect(path,isolation_level=None);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL');db.execute('PRAGMA busy_timeout=1000');db.execute('CREATE TABLE receipts(id INTEGER PRIMARY KEY,status TEXT,payload TEXT)');return db


def write(db,n):
    for i in range(n):db.execute('INSERT INTO receipts(status,payload) VALUES(?,?)',('UNKNOWN' if i%11==0 else 'SENT','x'*2000))


def test_checkpoint_keeps_all_receipts_and_full_sync(tmp_path):
    path=tmp_path/'queue.sqlite3';db=setup(path);now=[0];m=WalMaintenance([db],clock=lambda:now[0]);wal=Path(str(path)+'-wal')
    try:
        write(db,100);before=db.execute('SELECT * FROM receipts ORDER BY id').fetchall();assert wal.stat().st_size>262144
        m.idle();assert wal.stat().st_size==0
        assert db.execute('PRAGMA synchronous').fetchone()[0]==2 and db.execute('PRAGMA busy_timeout').fetchone()[0]==1000
        assert db.execute('SELECT * FROM receipts ORDER BY id').fetchall()==before
        assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    finally:db.close()
    with sqlite3.connect(path) as reopened:assert reopened.execute('SELECT * FROM receipts ORDER BY id').fetchall()==before


def test_busy_reader_defers_then_preserves_snapshot(tmp_path):
    path=tmp_path/'queue.sqlite3';db=setup(path);now=[0];m=WalMaintenance([db],clock=lambda:now[0]);reader=sqlite3.connect(path,isolation_level=None);wal=Path(str(path)+'-wal')
    try:
        reader.execute('BEGIN');assert reader.execute('SELECT count(*) FROM receipts').fetchone()[0]==0
        write(db,100);before=wal.stat().st_size;assert before>262144
        m.idle();assert wal.stat().st_size==before and reader.execute('SELECT count(*) FROM receipts').fetchone()[0]==0
        assert db.execute('PRAGMA busy_timeout').fetchone()[0]==1000
        reader.execute('COMMIT');now[0]=14;m.idle();assert wal.stat().st_size==before
        now[0]=15;m.idle();assert wal.stat().st_size==0 and db.execute('SELECT count(*) FROM receipts').fetchone()[0]==100
        db.execute('BEGIN');db.execute("INSERT INTO receipts(status,payload) VALUES('UNKNOWN','uncommitted')");now[0]=30;m.idle();assert db.in_transaction;db.execute('ROLLBACK')
    finally:reader.close();db.close()


def test_refuse_nondurable_or_nonwal(tmp_path):
    db=setup(tmp_path/'test.sqlite3')
    try:
        db.execute('PRAGMA synchronous=NORMAL')
        with pytest.raises(ValueError):WalMaintenance([db])
    finally:db.close()
    db=sqlite3.connect(':memory:')
    try:
        with pytest.raises(ValueError):WalMaintenance([db])
    finally:db.close()
