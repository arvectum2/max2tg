"""Bounded authenticated archive of a verified delivery snapshot.

No plaintext archive file. Input snapshot already exists in private storage;
materialization intentionally produces a new private plaintext directory.
"""
import base64,hashlib,json,os,shutil,tempfile
from pathlib import Path
from app.encrypted_storage import install_envelope,open_envelope,rotate_envelope,_read
from app.recovery_manifest import _private_dir,_file,RecoveryAnchor
from app.coordinated_backup import _disjoint,_sync_dir
from app.runtime_lock import RuntimeLock

MAX_SNAPSHOT=32*1024*1024

def _identity(anchor):
    v=anchor._load()
    if v['dirty']:raise PermissionError('Dirty anchor: reconciliation required')
    return v,'backup:'+v['epoch'],'generation-'+str(v['generation'])

def encrypt_snapshot(snapshot,destination,key_path,anchor:RecoveryAnchor):
    src=_private_dir(snapshot);dst=Path(destination);_disjoint([src,_private_dir(dst.parent),anchor.root,_private_dir(Path(key_path).parent)])
    lock=RuntimeLock(anchor.root/'backup.lock')
    try:
        anchor.verify(src,quiescent=True);v,scope,record=_identity(anchor);files={};total=0
        for n in sorted(v['manifest']):
            p=_file(src,n);data=_read(p,MAX_SNAPSHOT-total)
            if hashlib.sha256(data).hexdigest()!=v['manifest'][n]:raise PermissionError('Snapshot changed during encryption')
            total+=len(data);files[n]=base64.b64encode(data).decode('ascii')
        install_envelope(dst,json.dumps(files,separators=(',',':')).encode(),key_path,scope,record)
        return {'generation':v['generation'],'members':len(files),'bytes':total}
    finally:lock.close()


def materialize_encrypted(archive,key_path,output,anchor:RecoveryAnchor,*,name):
    parent=_private_dir(output);_disjoint([parent,_private_dir(Path(archive).parent),anchor.root,_private_dir(Path(key_path).parent)])
    if type(name) is not str or not name.startswith('snapshot-') or '/' in name or len(name)>100:raise ValueError('Invalid snapshot name')
    target=parent/name;lock=RuntimeLock(anchor.root/'backup.lock');stage=None
    try:
        if target.exists() or target.is_symlink():raise FileExistsError('Restore destination exists')
        v,scope,record=_identity(anchor);raw=open_envelope(archive,key_path,scope,record)
        files=json.loads(raw)
        if type(files) is not dict or set(files)!=set(v['manifest']):raise PermissionError('Incomplete encrypted snapshot')
        stage=Path(tempfile.mkdtemp(prefix='.decrypt-',dir=parent));total=0
        for n in sorted(v['manifest']):
            data=base64.b64decode(files[n],validate=True);total+=len(data)
            if total>MAX_SNAPSHOT:raise ValueError('Snapshot size limit')
            fd=os.open(stage/n,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        evidence=anchor.verify(stage,quiescent=True);_sync_dir(stage)
        if target.exists() or target.is_symlink():raise FileExistsError('Restore destination exists')
        stage.rename(target);_sync_dir(parent);return target,evidence
    finally:
        if stage is not None and stage.exists():shutil.rmtree(stage)
        lock.close()


def rotate_snapshot(archive,old,new,anchor:RecoveryAnchor):
    lock=RuntimeLock(anchor.root/'backup.lock')
    try:
        _,scope,record=_identity(anchor)
        return rotate_envelope(archive,old,new,scope,record)
    finally:lock.close()
