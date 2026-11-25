"""Per-capsule MAX credentials. No central registry or secret values in args/logs."""
from __future__ import annotations
import json,os,stat,secrets
from pathlib import Path
from app.capsule_registry import CapsuleRegistry

MAX_SECRET_FIELDS=frozenset(('session_id','MAX_TOKEN','MAX_DEVICE_ID'))


def _validate(sid,values):
    CapsuleRegistry._check_id(sid)
    if type(values) is not dict or set(values)!=MAX_SECRET_FIELDS or values['session_id']!=sid:raise ValueError('Invalid worker secret identity or fields')
    if any(type(values[k]) is not str or not values[k].strip() or len(values[k])>4096 or '\x00' in values[k] for k in ('MAX_TOKEN','MAX_DEVICE_ID')):raise ValueError('Invalid MAX credentials')
    return values


def _directory(folder):
    p=Path(folder).absolute()
    if p.is_symlink() or not p.is_dir():raise PermissionError('Unsafe secret directory')
    st=p.stat()
    if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700:raise PermissionError('Secret directory must be private and owned')
    fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    info=os.fstat(fd)
    if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        os.close(fd);raise PermissionError("Unsafe opened secret directory")
    return fd


def install_worker_secret(folder,sid,values):
    """Trusted local provisioning only. Refuse overwrite; rotation is separate."""
    values=_validate(sid,values);dfd=_directory(folder)
    temp='.max-'+secrets.token_hex(12)
    try:
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=dfd)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                json.dump(values,stream);stream.flush();os.fsync(stream.fileno())
            # Atomic no-overwrite publication, including concurrent install.
            os.link(temp,'max.json',src_dir_fd=dfd,dst_dir_fd=dfd,follow_symlinks=False)
        finally:os.unlink(temp,dir_fd=dfd)
        os.fsync(dfd)
    finally:os.close(dfd)


def load_worker_secret(folder,sid,*,key_path=None):
    if key_path is not None:
        from app.encrypted_credentials import load_encrypted_worker
        return load_encrypted_worker(folder,sid,key_path)
    dfd=_directory(folder)
    try:
        fd=os.open('max.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=dfd)
        with os.fdopen(fd,'rb') as stream:
            st=os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077 or st.st_nlink!=1 or st.st_size>32768:raise PermissionError('Unsafe worker secret file')
            raw=stream.read(32769)
            if len(raw)>32768:raise ValueError('Worker secret exceeds limit')
        try:values=json.loads(raw)
        except (ValueError,UnicodeError):raise ValueError('Malformed worker secret') from None
        return _validate(sid,values)
    finally:os.close(dfd)


def build_max_client(folder,sid,*,factory=None,key_path=None):
    if factory is None:
        from app.max_client import MaxClient
        factory=MaxClient
    values=load_worker_secret(folder,sid,key_path=key_path)
    return factory(token=values['MAX_TOKEN'],device_id=values['MAX_DEVICE_ID'],debug=False)
