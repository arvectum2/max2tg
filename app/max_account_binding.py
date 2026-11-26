"""Offline, trusted worker-local MAX account continuity, opt-in.
No SMS collection or user-supplied account ID. The trusted probe must obtain
viewer identity from the exact candidate's authenticated MAX session.
"""
import asyncio,json
from pathlib import Path
from app.capsule_registry import CapsuleRegistry
from app.encrypted_credentials import load_encrypted_worker,_worker_path
from app.encrypted_storage import install_envelope,open_envelope,load_key,seal,unseal,_path,_read,_write
from app.worker_secrets import _validate
from app.runtime_lock import RuntimeLock
from app.recovery_manifest import _private_dir
from app.coordinated_backup import _disjoint


def _viewer(value):
    if type(value) is not int or not 0<value<2**63:raise PermissionError('Authenticated MAX identity unavailable')
    return value


def bound_viewer(secret,sid,key_path,*,required=False):
    CapsuleRegistry._check_id(sid);p=Path(secret)/'account.enc'
    if not (p.exists() or p.is_symlink()):
        if required:raise PermissionError('MAX account binding required')
        return None
    return _viewer(json.loads(open_envelope(p,key_path,'max:'+sid,'account-binding')))


async def _prove(probe,values):
    return _viewer(await asyncio.wait_for(probe(dict(values)),20))


async def bind_initial(state,secret,sid,key_path,*,probe):
    """Only a fresh provisioned worker with no existing delivery state."""
    state=_private_dir(state);secret=_private_dir(secret);_disjoint([state,secret,_private_dir(Path(key_path).parent)])
    lock=RuntimeLock(state/'worker.lock')
    try:
        if set(p.name for p in state.iterdir())-{'worker.lock','key','worker-acl.json','test-ca.pem'}:raise PermissionError('Existing worker history requires separate verified migration')
        values=load_encrypted_worker(secret,sid,key_path);viewer=await _prove(probe,values)
        current=bound_viewer(secret,sid,key_path)
        if current is None:install_envelope(secret/'account.enc',json.dumps(viewer).encode(),key_path,'max:'+sid,'account-binding')
        elif current!=viewer:raise PermissionError('MAX account identity mismatch')
        return viewer
    finally:lock.close()


async def replace_bound_credentials(state,secret,sid,key_path,candidate,*,probe):
    """Stopped worker only. Keep account pin immutable; atomically replace token."""
    state=_private_dir(state);secret=_private_dir(secret);_disjoint([state,secret,_private_dir(Path(key_path).parent)])
    candidate=dict(_validate(sid,candidate));lock=RuntimeLock(state/'worker.lock')
    try:
        viewer=bound_viewer(secret,sid,key_path,required=True);path=_path(_worker_path(secret));old=_read(path,65536)
        load_encrypted_worker(secret,sid,key_path)
        if await _prove(probe,candidate)!=viewer:raise PermissionError('MAX account identity mismatch')
        if _read(path,65536)!=old or bound_viewer(secret,sid,key_path,required=True)!=viewer:raise PermissionError('Credential evidence changed during verification')
        data=json.dumps(candidate,separators=(',',':')).encode();key=load_key(key_path,'max:'+sid);blob=seal(data,key,'max:'+sid,'credentials')
        if unseal(blob,key,'max:'+sid,'credentials')!=data:raise RuntimeError('Credential self-check failed')
        _write(path,blob,replace=True)
        if load_encrypted_worker(secret,sid,key_path)!=candidate:raise RuntimeError('Credential readback failed')
        return viewer
    finally:lock.close()
