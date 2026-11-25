"""Opt-in WAL maintenance for existing owner-held SQLite connections.
No unlink, VACUUM, row mutation or new connection; FULL durability unchanged.
The size threshold is a maintenance trigger, not a hard storage quota.
"""
import sqlite3,time
from pathlib import Path

class WalMaintenance:
    def __init__(self,connections,*,clock=time.monotonic):
        self.clock=clock;self.next=clock();self.connections=[]
        for db in connections:
            if db.in_transaction or db.execute('PRAGMA journal_mode').fetchone()[0].lower()!='wal' or db.execute('PRAGMA synchronous').fetchone()[0]<2:raise ValueError('Durable WAL connection required')
            main=[r[2] for r in db.execute('PRAGMA database_list') if r[1]=='main']
            if len(main)!=1 or not main[0]:raise ValueError('File-backed database required')
            self.connections.append((db,Path(main[0]+'-wal')))
        for db,_ in self.connections:
            db.execute('PRAGMA wal_autocheckpoint=128')
            db.execute('PRAGMA journal_size_limit=262144')

    def idle(self):
        """Attempt at most once per 15s, without waiting for SQLite locks.

        Disk I/O itself is synchronous and not time-bounded. BUSY is benign;
        all other storage errors propagate to existing fail-closed runtime.
        Call only from the connection's event loop outside a transaction.
        """
        at=self.clock()
        if at<self.next:return
        self.next=at+15
        for db,wal in self.connections:
            if db.in_transaction:continue
            try:size=wal.stat().st_size
            except FileNotFoundError:continue
            if size<=262144:continue
            timeout=db.execute('PRAGMA busy_timeout').fetchone()[0]
            try:
                db.execute('PRAGMA busy_timeout=0')
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            except sqlite3.OperationalError as exc:
                if getattr(exc,'sqlite_errorcode',0)&255 not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):raise
            finally:db.execute(f'PRAGMA busy_timeout={int(timeout)}')
