"""Bounded authenticated envelopes with separately retained scoped keys.

Trusted single-host provisioning; keys are not KMS or hardware-backed.
Never stores plaintext in an envelope staging file. Key rotation replaces
one envelope atomically; callers must retain both keys until verified.
"""
import base64,json,os,re,secrets,stat,tempfile
from pathlib import Path
from dataclasses import dataclass,field
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from app.recovery_manifest import _private_dir
from app.runtime_lock import RuntimeLock

SCOPE=re.compile(r'(?:max:[0-9a-f]{24}|telegram:[1-9][0-9]{0,18}|backup:[0-9a-f]{32})')
KID=re.compile(r'[0-9a-f]{32}')
MAX_BYTES=64*1024*1024
MAGIC=b'M2TG-AEAD-1\x00'

@dataclass(frozen=True)
class StorageKey:
    scope:str
    kid:str
    secret:bytes=field(repr=False)
    def __post_init__(self):
        if not isinstance(self.scope,str) or not SCOPE.fullmatch(self.scope) or not isinstance(self.kid,str) or not KID.fullmatch(self.kid) or type(self.secret) is not bytes or len(self.secret)!=32:raise ValueError('Invalid storage key')


def _path(value):
    p=Path(value);root=_private_dir(p.parent)
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',p.name):raise ValueError('Invalid storage filename')
    return root/p.name


def _read(path,limit):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as f:
        st=os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077 or st.st_nlink!=1 or st.st_size>limit:raise PermissionError('Unsafe storage file')
        result=f.read(limit+1)
        if len(result)>limit:raise ValueError('Storage limit exceeded')
        return result


def _write(path,data,*,replace=False):
    fd,temp=tempfile.mkstemp(prefix='.sealed-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        if replace:os.replace(temp,path)
        else:
            os.link(temp,path,follow_symlinks=False);os.unlink(temp)
        dfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(dfd)
        finally:os.close(dfd)
    finally:Path(temp).unlink(missing_ok=True)


def create_key(path,scope):
    p=_path(path);key=StorageKey(scope,secrets.token_hex(16),AESGCM.generate_key(bit_length=256))
    _write(p,json.dumps({'scope':key.scope,'kid':key.kid,'key':base64.b64encode(key.secret).decode()},separators=(',',':')).encode())
    return key.kid


def load_key(path,scope):
    try:
        d=json.loads(_read(_path(path),2048))
        if type(d) is not dict or set(d)!={'scope','kid','key'} or d['scope']!=scope:raise ValueError('Storage key scope mismatch')
        return StorageKey(scope,d['kid'],base64.b64decode(d['key'],validate=True))
    except (ValueError,TypeError,UnicodeError):raise ValueError('Invalid scoped storage key') from None


def _header(key,scope,record):
    if key.scope!=scope or not isinstance(record,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}',record):raise ValueError('Storage record identity mismatch')
    return MAGIC+json.dumps([scope,record,key.kid],separators=(',',':')).encode()+b'\x00'


def seal(data,key,scope,record):
    if type(data) is not bytes or len(data)>MAX_BYTES:raise ValueError('Envelope size limit')
    header=_header(key,scope,record);nonce=secrets.token_bytes(12)
    return header+nonce+AESGCM(key.secret).encrypt(nonce,data,header)


def unseal(data,key,scope,record):
    header=_header(key,scope,record)
    if type(data) is not bytes or not data.startswith(header) or not len(header)+28<=len(data)<=len(header)+28+MAX_BYTES:raise ValueError('Envelope identity or length mismatch')
    try:return AESGCM(key.secret).decrypt(data[len(header):len(header)+12],data[len(header)+12:],header)
    except InvalidTag:raise ValueError('Envelope authentication failed') from None


def install_envelope(path,data,key_path,scope,record):
    p=_path(path);kp=_path(key_path)
    if p.parent==kp.parent or p.parent.is_relative_to(kp.parent) or kp.parent.is_relative_to(p.parent):raise PermissionError('Key retention must be separate')
    key=load_key(kp,scope);_write(p,seal(data,key,scope,record))


def open_envelope(path,key_path,scope,record):
    p=_path(path);kp=_path(key_path)
    if p.parent==kp.parent or p.parent.is_relative_to(kp.parent) or kp.parent.is_relative_to(p.parent):raise PermissionError('Key retention must be separate')
    return unseal(_read(p,MAX_BYTES+1024),load_key(kp,scope),scope,record)


def rotate_envelope(path,old_key_path,new_key_path,scope,record):
    p=_path(path);lock=RuntimeLock(p.parent/(p.name+'.lock'))
    try:
        data=open_envelope(p,old_key_path,scope,record);newpath=_path(new_key_path);old=load_key(old_key_path,scope);new=load_key(newpath,scope)
        if new.kid==old.kid or new.secret==old.secret:raise ValueError('Distinct rotation key required')
        if p.parent==newpath.parent or p.parent.is_relative_to(newpath.parent) or newpath.parent.is_relative_to(p.parent):raise PermissionError('Key retention must be separate')
        blob=seal(data,new,scope,record)
        if unseal(blob,new,scope,record)!=data:raise RuntimeError('Rotation self-check failed')
        _write(p,blob,replace=True)
        if open_envelope(p,newpath,scope,record)!=data:raise RuntimeError('Rotation readback failed')
        return new.kid
    finally:lock.close()
