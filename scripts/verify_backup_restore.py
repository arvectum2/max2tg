#!/usr/bin/env python3
"""Verify a consistent, isolated restoration of MAX2TG personal state.

Never overwrites a live file. Copies sqlite databases via sqlite backup API
and checks their consistency + topic/mapping counts in a temporary folder.
All temporary files are removed on exit, including private message mappings.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def sqlite_copy_and_verify(source: Path,target: Path) -> dict:
    if source.is_symlink() or not source.is_file():
        raise ValueError("Source database is not a regular file")
    src=sqlite3.connect(f"file:{source}?mode=ro",uri=True)
    try:
        if src.execute("PRAGMA quick_check").fetchone()[0]!="ok":
            raise ValueError("Source SQLite database is corrupt")
        with sqlite3.connect(target) as out:
            src.backup(out)
    finally:
        src.close()
    with sqlite3.connect(target) as out:
        if out.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
            raise ValueError("Restored SQLite database is corrupt")
        schema=out.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {"tables":len(schema),"bytes":target.stat().st_size}


def verify_restore(topics_path: Path,outbox_path: Path,
                   tg_outbound_path: Path|None=None,
                   tg_mutations_path: Path|None=None) -> dict:
    """Runs a no-network restore drill, including a TopicStore reload."""
    from app.topics import TopicStore
    from app.outbox import OutboxStore
    from app.tg_outbox import TelegramOutbound
    from app.tg_mutation_outbox import TgMutationOutbox
    if topics_path.is_symlink() or not topics_path.is_file():
        raise ValueError("Source topic state is not an ordinary file")
    original=json.loads(topics_path.read_text(encoding="utf-8"))
    if not isinstance(original,dict):
        raise ValueError("Invalid topics JSON")
    with tempfile.TemporaryDirectory(prefix="max2tg-restore-") as directory:
        location=Path(directory)
        location.chmod(0o700)
        restored_topics=location/"topics.json"
        shutil.copyfile(topics_path,restored_topics)
        restored_topics.chmod(0o600)
        outbox_stats=sqlite_copy_and_verify(outbox_path,location/"outbox.sqlite3")
        tg_stats=None
        if tg_outbound_path is not None:
            tg_stats=sqlite_copy_and_verify(
                tg_outbound_path,location/"tg-outbound.sqlite3")
        mutation_stats=None
        if tg_mutations_path is not None:
            mutation_stats=sqlite_copy_and_verify(tg_mutations_path,location/'tg-mutations.sqlite3')
        store=TopicStore(str(restored_topics),strict=True)
        if len(store._chats)!=len(original.get("chats",{})):
            raise ValueError("Topic count changed on restore")
        if store._messages!=original.get("messages",{}):
            raise ValueError("Message mapping mismatch")
        if store._sync!={str(k):int(v) for k,v in
                         (original.get("sync") or {}).items() if int(v)>0}:
            raise ValueError("History cursor mismatch")
        ledger=OutboxStore(str(location/"outbox.sqlite3"))
        try:
            mappings_repaired=ledger.reconcile_mappings(store)
            outbox_counts=ledger.counts()
        finally:
            ledger.close()
        pending_albums=0;ingress_count=0
        if tg_stats:
            tg_store=TelegramOutbound(location/"tg-outbound.sqlite3",store)
            try:
                tg_counts=tg_store.counts()
                pending_albums=tg_store.db.execute('SELECT count(*) FROM outbound WHERE album_pending=1').fetchone()[0]
                if tg_store.db.execute("SELECT 1 FROM sqlite_master WHERE name='inbound_updates'").fetchone():
                    ingress_count=tg_store.db.execute('SELECT count(*) FROM inbound_updates').fetchone()[0]
            finally:
                tg_store.close()
        else:
            tg_counts={}
        mutation_counts={}
        if mutation_stats:
            mutation_store=TgMutationOutbox(location/'tg-mutations.sqlite3')
            try:mutation_counts=mutation_store.counts()
            finally:mutation_store.close()
        return {
            "tg_mutation_tables":mutation_stats['tables'] if mutation_stats else 0,
            "tg_mutation_status_counts":mutation_counts,
            "pending_album_members":pending_albums,
            "pending_ingress":ingress_count,
            "topics":len(store._chats),
            "mappings":sum(map(len,store._messages.values())),
            "cursors":len(store._sync),
            "outbox_tables":outbox_stats["tables"],
            "tg_outbound_tables":tg_stats["tables"] if tg_stats else 0,
            "outbox_status_counts":outbox_counts,
            "tg_outbound_status_counts":tg_counts,
            "mappings_repaired":mappings_repaired,
            "passed":True,
        }


def main():
    arg=argparse.ArgumentParser()
    arg.add_argument("--topics",type=Path,required=True)
    arg.add_argument("--outbox",type=Path,required=True)
    arg.add_argument("--tg-outbound",type=Path)
    arg.add_argument("--tg-mutations",type=Path)
    args=arg.parse_args()
    result=verify_restore(args.topics,args.outbox,args.tg_outbound,args.tg_mutations)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))


if __name__=="__main__":
    main()
