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
                   tg_outbound_path: Path|None=None) -> dict:
    """Runs a no-network restore drill, including a TopicStore reload."""
    from app.topics import TopicStore
    from app.outbox import OutboxStore
    from app.tg_outbox import TelegramOutbound
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
        if tg_outbound_path and tg_outbound_path.exists():
            tg_stats=sqlite_copy_and_verify(
                tg_outbound_path,location/"tg-outbound.sqlite3")
        store=TopicStore(str(restored_topics))
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
        if tg_stats:
            tg_store=TelegramOutbound(location/"tg-outbound.sqlite3",store)
            try:
                tg_counts=tg_store.counts()
            finally:
                tg_store.close()
        else:
            tg_counts={}
        return {
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
    args=arg.parse_args()
    result=verify_restore(args.topics,args.outbox,args.tg_outbound)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))


if __name__=="__main__":
    main()
