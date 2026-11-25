"""Trusted worker-local preparation, no registry access or live authentication."""
import json,os,secrets,stat,hmac
from pathlib import Path
from app.capsule_registry import CapsuleRegistry
from app.worker_secrets import load_worker_secret
from app.runtime_lock import RuntimeLock

def _folders(state,channel,secret):
    paths=[]
    for value in (state,channel,secret):
        p=Path(value).absolute()
        if p.is_symlink() or not p.is_dir():raise PermissionError('Missing private worker directory')
        st=p.stat()
        if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700:raise PermissionError('Unsafe worker directory')
        p=p.resolve()
        if any(p==q or p.is_relative_to(q) or q.is_relative_to(p) for q in paths):raise ValueError('Overlapping worker directories')
        paths.append(p)
    return paths

def _expected(sid,actor,forum,key):
    CapsuleRegistry._check_id(sid)
    if type(actor) is not int or not 0<actor<2**63 or type(forum) is not int or not -(2**63)<=forum<0 or type(key) is not bytes or len(key)!=32:raise ValueError('Invalid provisioning identity')
    return {'key':key,'worker-acl.json':json.dumps({'sid':sid,'actor':actor,'forum':forum},sort_keys=True).encode()}

def _read(path,limit):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as f:
        st=os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077 or st.st_nlink!=1 or st.st_size>limit:raise PermissionError('Unsafe provisioning file')
        return f.read(limit+1)

def _ensure(path,data):
    if path.exists() or path.is_symlink():
        if not hmac.compare_digest(_read(path,len(data)),data):raise PermissionError('Provisioning file conflict')
        return
    temp=path.parent/('.provision-'+secrets.token_hex(12));fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.link(temp,path,follow_symlinks=False)
    finally:temp.unlink()
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(directory)
    finally:os.close(directory)

def prepare_worker(state,channel,secret,sid,actor,forum,key):
    state,channel,secret=_folders(state,channel,secret);expected=_expected(sid,actor,forum,key)
    lock=RuntimeLock(state/'worker.lock')
    try:
        load_worker_secret(secret,sid)
        for name,data in expected.items():_ensure(state/name,data)
        return check_worker(state,channel,secret,sid,actor,forum,key)
    finally:lock.close()

def check_worker(state,channel,secret,sid,actor,forum,key):
    state,channel,secret=_folders(state,channel,secret);expected=_expected(sid,actor,forum,key)
    load_worker_secret(secret,sid)
    return all(hmac.compare_digest(_read(state/name,len(data)),data) for name,data in expected.items())
