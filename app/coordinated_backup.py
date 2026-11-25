"""Stopped-runtime backup of shared delivery state; no secrets or live restore.

Trusted operator must suspend provisioning/other registry writers. The exact
runtime locks protect gateway and isolated workers; all copies are made while
held. Restore materialization creates only a fresh flat private directory.
"""
from contextlib import ExitStack,closing
import json,os,shutil,sqlite3,tempfile
from pathlib import Path
from app.recovery_manifest import RecoveryAnchor,_private_dir,_file,_members
from app.runtime_lock import RuntimeLock
from app.capsule_registry import TENANT_ID

GATEWAY=('capsules.sqlite3','inbox.sqlite3','telegram-egress.sqlite3')
WORKER=('tg-outbound.sqlite3','to-telegram.sqlite3','topics.json','worker-acl.json')


def _disjoint(paths):
    for i,p in enumerate(paths):
        if any(p==q or p.is_relative_to(q) or q.is_relative_to(p) for q in paths[:i]):raise PermissionError('Overlapping backup roots')


def _lock(stack,root,name):
    # Never fabricate a lock for a missing runtime layout.
    path=_file(root,name)
    lock=RuntimeLock(path);stack.callback(lock.close)


def _copy(source,dest):
    if dest.name.endswith('.sqlite3'):
        fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(dest)) as dst:
            src.backup(dst);dst.execute('PRAGMA journal_mode=DELETE')
            if dst.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Invalid SQLite backup')
    else:
        fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as out,source.open('rb') as inp:shutil.copyfileobj(inp,out);out.flush();os.fsync(out.fileno())
    with dest.open('rb') as f:os.fsync(f.fileno())


def _sync_dir(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def _publish_directory(parent,name,files):
    if type(name) is not str or not name.startswith('snapshot-') or '/' in name or len(name)>100:raise ValueError('Invalid snapshot name')
    target=parent/name
    if target.exists() or target.is_symlink():raise FileExistsError('Snapshot already exists')
    stage=Path(tempfile.mkdtemp(prefix='.backup-',dir=parent))
    try:
        for output,source in files.items():_copy(source,stage/output)
        _members(stage,files);_sync_dir(stage)
        # Trusted single operator; no concurrent destination writers.
        if target.exists() or target.is_symlink():raise FileExistsError('Snapshot already exists')
        stage.rename(target);_sync_dir(parent);return target
    finally:
        if stage.exists():shutil.rmtree(stage)


def capture_delivery_state(registry,workers,output,anchor:RecoveryAnchor,*,name,provisioning_stopped,reconciled=False):
    if provisioning_stopped is not True:raise PermissionError('Provisioning must be stopped')
    root=_private_dir(registry);parent=_private_dir(output)
    if type(workers) is not dict or not workers or any(type(sid) is not str or not TENANT_ID.fullmatch(sid) for sid in workers):raise ValueError('Explicit worker set required')
    states={sid:_private_dir(path) for sid,path in workers.items()};_disjoint([root,parent,anchor.root,*states.values()])
    with ExitStack() as stack:
        a=RuntimeLock(anchor.root/'backup.lock');stack.callback(a.close)
        _lock(stack,root,'shared-gateway.lock')
        for sid in sorted(states):_lock(stack,states[sid],'worker.lock')
        with closing(sqlite3.connect(_file(root,'capsules.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
            rows=db.execute('SELECT session_id,telegram_mode,credential_mode,tg_chat_id FROM capsules').fetchall()
            if not rows or any(mode!='shared' or cred!='external' for _,mode,cred,_ in rows) or {r[0] for r in rows}!=set(states):raise PermissionError('Snapshot must cover every external capsule')
            for sid,_,_,forum in rows:
                acl=json.loads(_file(states[sid],'worker-acl.json').read_text());owner=db.execute('SELECT o.tg_user_id FROM tg_owners o JOIN capsules c ON c.user_id=o.user_id WHERE c.session_id=?',(sid,)).fetchone()
                if not owner or acl!={'sid':sid,'actor':int(owner[0]),'forum':int(forum)}:raise PermissionError('Worker ACL identity mismatch')
        files={f'gateway-{n}':_file(root,n) for n in GATEWAY}
        for sid,state in states.items():
            files.update({f'{sid}-{n}':_file(state,n) for n in WORKER})
        if anchor.path.exists():
            previous=anchor._load()
            if previous['dirty'] and reconciled is not True:raise PermissionError('Dirty epoch requires explicit trusted reconciliation')
            if set(previous['manifest'])!=set(files):raise PermissionError('Snapshot membership changed')
        target=_publish_directory(parent,name,files)
        # A complete unpublished-to-anchor copy is harmless if anchor update fails;
        # it cannot pass verification against the existing trusted anchor.
        record=anchor.capture(target,set(files),quiescent=True,reconciled=reconciled)
        return target,record


def materialize_verified(snapshot,output,anchor:RecoveryAnchor,*,name):
    root=_private_dir(snapshot);parent=_private_dir(output);_disjoint([root,parent,anchor.root])
    lock=RuntimeLock(anchor.root/'backup.lock')
    try:
        evidence=anchor.verify(root,quiescent=True);record=anchor._load();files={n:_file(root,n) for n in record['manifest']}
        # Byte-copy already standalone SQLite snapshots: preserve manifest hashes.
        if type(name) is not str or not name.startswith('snapshot-') or '/' in name or len(name)>100:raise ValueError('Invalid snapshot name')
        target=parent/name
        if target.exists() or target.is_symlink():raise FileExistsError('Restore destination exists')
        stage=Path(tempfile.mkdtemp(prefix='.restore-',dir=parent))
        try:
            for n,source in files.items():
                fd=os.open(stage/n,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as out,source.open('rb') as inp:shutil.copyfileobj(inp,out);out.flush();os.fsync(out.fileno())
            anchor.verify(stage,quiescent=True);_sync_dir(stage)
            if target.exists() or target.is_symlink():raise FileExistsError('Restore destination exists')
            stage.rename(target);_sync_dir(parent);return target,evidence
        finally:
            if stage.exists():shutil.rmtree(stage)
    finally:lock.close()
