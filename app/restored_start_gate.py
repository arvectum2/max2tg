"""Explicit trusted restored-cohort startup, not the ordinary crash restart path.

The independent anchor must be outside all restored volumes. The orchestrator
must exclusively control launches and low-level registry writers. No untrusted
callback or public API; no live deployment entrypoint.
"""
from contextlib import ExitStack
import hmac,json,sqlite3
from contextlib import closing
from app.coordinated_backup import GATEWAY,WORKER,_disjoint,_lock
from app.recovery_manifest import _private_dir,_file,_digest,RecoveryAnchor
from app.runtime_lock import RuntimeLock
from app.capsule_registry import TENANT_ID
from app.worker_secrets import load_worker_secret
from app.worker_provision import _read


async def launch_verified_restore(anchor:RecoveryAnchor,registry,workers,*,start,secret_keys=None):
    """Check exact restored data, separate retained keys, then burn clean anchor.

    `workers` maps sid to (state, channel, secret). `start` is trusted async
    orchestration, not a user-supplied function. Any exception after the dirty
    write requires reconciliation; never reset dirty here. Runtime locks are
    released for the launch handoff while the independent backup lock is held.
    """
    root=_private_dir(registry)
    if type(workers) is not dict or not workers or any(type(sid) is not str or not TENANT_ID.fullmatch(sid) for sid in workers):raise ValueError('Explicit worker set required')
    paths={sid:tuple(_private_dir(p) for p in parts) for sid,parts in workers.items()}
    if any(len(parts)!=3 for parts in paths.values()):raise ValueError('Invalid worker layout')
    keys={} if secret_keys is None else secret_keys
    if type(keys) is not dict or not set(keys)<=set(paths):raise ValueError("Invalid worker secret key set")
    from pathlib import Path
    key_roots=[_private_dir(Path(p).parent) for p in keys.values()]
    _disjoint([anchor.root,root,*[p for parts in paths.values() for p in parts],*key_roots])
    outer=RuntimeLock(anchor.root/'backup.lock')
    try:
        with ExitStack() as stack:
            for n in ('shared-gateway.lock','shared-provisioning.lock'):_lock(stack,root,n)
            for sid in sorted(paths):_lock(stack,paths[sid][0],'worker.lock')
            record=anchor._load()
            if record['dirty']:raise PermissionError('Dirty anchor: reconciliation required')
            files={f'gateway-{n}':_file(root,n) for n in GATEWAY}
            for sid,(state,channel,secret) in paths.items():
                files.update({f'{sid}-{n}':_file(state,n) for n in WORKER})
            if set(files)!=set(record['manifest']):raise PermissionError('Incomplete restored cohort')
            for name,p in files.items():
                if p.name.endswith('.sqlite3') and any(p.with_name(p.name+s).exists() or p.with_name(p.name+s).is_symlink() for s in ('-wal','-shm','-journal')):raise PermissionError('Unexpected database sidecar')
                if not hmac.compare_digest(_digest(p),record['manifest'][name]):raise PermissionError('Restored delivery state differs from checkpoint')
            _private_dir(root/'tenants')
            with closing(sqlite3.connect((root/'capsules.sqlite3').as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
                rows=db.execute('SELECT session_id,telegram_mode,credential_mode,tg_chat_id FROM capsules').fetchall()
                if {r[0] for r in rows}!=set(paths) or any(m!='shared' or c!='external' for _,m,c,_ in rows):raise PermissionError('Restored tenant set mismatch')
                for sid,_,_,forum in rows:
                    state,channel,secret=paths[sid];owner=db.execute('SELECT o.tg_user_id FROM tg_owners o JOIN capsules c ON c.user_id=o.user_id WHERE c.session_id=?',(sid,)).fetchone()
                    if not owner or json.loads(_file(state,'worker-acl.json').read_text())!={'sid':sid,'actor':int(owner[0]),'forum':int(forum)}:raise PermissionError('Restored ACL mismatch')
                    # Keys are not in delivery backup. Require retained matching
                    # keys, never silently regenerate or use a central MAX secret.
                    capsule=_private_dir(root/'tenants'/sid)
                    key=_read(capsule/'telegram-ipc.key',32);local=_read(state/'key',32)
                    if len(key)!=32 or not hmac.compare_digest(key,local):raise PermissionError('Retained IPC key mismatch')
                    if (capsule/'credentials.json').exists() or (capsule/'credentials.json').is_symlink():raise PermissionError('Legacy secret prohibited')
                    load_worker_secret(secret,sid,key_path=keys.get(sid))
            anchor.mark_dirty()
        # Dirty marker is fsynced before any runtime constructor, network or write.
        # Trusted exclusive host orchestrator controls the handoff to runtime locks.
        return await start()
    finally:outer.close()
