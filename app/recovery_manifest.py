"""Offline recovery verification; never restores or enables a running bridge.

Trusted operator must stop all writers and hold their exclusive locks before
capture/verification. Anchor must be retained independently of restored volumes.
A matching manifest is structural evidence, not evidence of external delivery.
"""
import hashlib,json,os,re,secrets,sqlite3,stat,tempfile
from pathlib import Path

NAME=re.compile(r'[a-z0-9][a-z0-9_.-]{0,127}')
DIGEST=re.compile(r'[0-9a-f]{64}')


def _private_dir(value):
    p=Path(value);st=p.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077:raise PermissionError('Private directory required')
    return p.resolve()


def _file(root,name):
    if not isinstance(name,str) or not NAME.fullmatch(name):raise ValueError('Invalid snapshot member')
    p=root/name;st=p.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077 or st.st_nlink!=1:raise PermissionError('Unsafe snapshot member')
    return p


def _digest(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def _publish(path,data):
    fd,tmp=tempfile.mkstemp(prefix='.recovery-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:json.dump(data,f,sort_keys=True,separators=(',',':'));f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        d=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(d)
        finally:os.close(d)
    finally:Path(tmp).unlink(missing_ok=True)


def _members(root,expected):
    if not expected or len(expected)>10000 or any(not isinstance(n,str) or not NAME.fullmatch(n) or n.endswith(('-wal','-shm','-journal')) for n in expected):raise ValueError('Invalid recovery member set')
    actual={p.name for p in root.iterdir()}
    if actual!=set(expected):raise ValueError('Incomplete or unexpected snapshot members')
    result={}
    for name in sorted(expected):
        p=_file(root,name)
        if name.endswith('.sqlite3'):
            # immutable only on a stopped, standalone snapshot; no WAL allowed.
            with sqlite3.connect(p.as_uri()+'?mode=ro&immutable=1',uri=True) as db:
                if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('Invalid SQLite snapshot')
        result[name]=_digest(p)
    return result


class RecoveryAnchor:
    """Independent latest-snapshot anchor. All restore decisions are fail closed.

    Single trusted offline operator; no concurrency or external attestation.
    A dirty anchor forbids every snapshot, including the most recent one.
    Mark dirty BEFORE permitting any external side effect after a checkpoint.
    """
    def __init__(self,folder):self.root=_private_dir(folder);self.path=self.root/'anchor.json'
    def _load(self):
        p=_file(self.root,'anchor.json');v=json.loads(p.read_text())
        if type(v) is not dict or set(v)!={'epoch','generation','dirty','manifest'} or type(v['epoch']) is not str or not re.fullmatch(r'[0-9a-f]{32}',v['epoch']) or type(v['generation']) is not int or v['generation']<1 or type(v['dirty']) is not bool or type(v['manifest']) is not dict or any(not NAME.fullmatch(k) or not isinstance(d,str) or not DIGEST.fullmatch(d) for k,d in v['manifest'].items()):raise ValueError('Invalid recovery anchor')
        return v
    def capture(self,folder,expected,*,quiescent,reconciled=False):
        if quiescent is not True:raise PermissionError('All writers must be stopped')
        root=_private_dir(folder)
        if root==self.root or root.is_relative_to(self.root) or self.root.is_relative_to(root):raise PermissionError('Anchor must be independent')
        old=self._load() if self.path.exists() else None
        if old and old['dirty'] and reconciled is not True:raise PermissionError('Dirty epoch requires explicit trusted reconciliation')
        if old and set(old['manifest'])!=set(expected):raise PermissionError('Snapshot membership changed; explicit re-provisioning required')
        files=_members(root,expected);record={'epoch':old['epoch'] if old else secrets.token_hex(16),'generation':old['generation']+1 if old else 1,'dirty':False,'manifest':files}
        _publish(self.path,record);return record
    def mark_dirty(self):
        v=self._load();v['dirty']=True;_publish(self.path,v)
    def verify(self,folder,*,quiescent):
        if quiescent is not True:raise PermissionError('All writers must be stopped')
        root=_private_dir(folder)
        if root==self.root or root.is_relative_to(self.root) or self.root.is_relative_to(root):raise PermissionError('Anchor must be independent')
        v=self._load()
        if v['dirty']:raise PermissionError('External effects may postdate checkpoint; manual reconciliation required')
        if _members(root,v['manifest'])!=v['manifest']:raise PermissionError('Snapshot differs from latest trusted checkpoint')
        return {'epoch':v['epoch'],'generation':v['generation'],'members':len(v['manifest']),'structural_only':True}
