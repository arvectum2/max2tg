"""Read-only Linux host storage checks; never authorizes a live launch.

Trust the host kernel and privileged operator, not a supplied readiness JSON.
Local checks cannot prove off-host key custody, backup retention or firewall.
"""
from dataclasses import dataclass
import os,re,stat,sys
from pathlib import Path

class StorageDenied(PermissionError):pass

@dataclass(frozen=True)
class Mount:
    dev:str
    root:str
    point:str
    options:frozenset
    fstype:str
    source:str
    super_options:frozenset


def _unescape(value):
    return re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),value)


def parse_mountinfo(text):
    rows=[]
    for line in text.splitlines():
        a,sep,b=line.partition(' - ');left=a.split();right=b.split()
        if not sep or len(left)<6 or len(right)!=3 or not re.fullmatch(r'[0-9]+:[0-9]+',left[2]):raise StorageDenied('Malformed mountinfo')
        root,point=map(_unescape,left[3:5])
        if not root.startswith('/') or not point.startswith('/'):raise StorageDenied('Malformed mount path')
        rows.append(Mount(left[2],root,point,frozenset(left[5].split(',')),right[0],_unescape(right[1]),frozenset(right[2].split(','))))
    if not rows:raise StorageDenied('Mount table unavailable')
    return rows


def _mount(rows,path):
    matches=[r for r in rows if r.point==str(path)]
    if len(matches)!=1:raise StorageDenied('Dedicated unambiguous mount required')
    m=matches[0]
    if any(r.dev==m.dev and r.point!=m.point for r in rows):raise StorageDenied('Aliased mount device prohibited')
    if m.root!='/' or any(r.point!=m.point and Path(r.point).is_relative_to(path) for r in rows):raise StorageDenied('Bind subroot or nested mount prohibited')
    if not {'nodev','nosuid','noexec'}<=m.options or 'rw' not in m.options or 'ro' in m.super_options:raise StorageDenied('Required mount restrictions missing')
    return m


def _directory(value):
    p=Path(value)
    if not p.is_absolute() or p.resolve()!=p or p==Path('/') or any(q.is_symlink() for q in (p,*p.parents)):raise StorageDenied('Canonical absolute private mount path required')
    s=p.stat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode&0o077:raise StorageDenied('Root-owned private mount required')
    return p,s


def inspect_storage(data,backup,keys,*,mountinfo=None,sys_block=None,swaps=None,check_dir=None):
    """Test injection is internal only; CLI always reads real kernel evidence."""
    if mountinfo is None:
        if sys.platform!='linux' or os.geteuid()!=0:raise StorageDenied('Run on trusted Linux host as root, not inside a worker')
        mountinfo=Path('/proc/self/mountinfo').read_text();sys_block=Path('/sys/dev/block');swaps=Path('/proc/swaps').read_text()
    check_dir=check_dir or _directory
    paths=[];rows=parse_mountinfo(mountinfo);mounts=[]
    for value in (data,backup,keys):
        p,s=check_dir(value)
        if any(p==q or p.is_relative_to(q) or q.is_relative_to(p) for q in paths):raise StorageDenied('Overlapping storage roots')
        m=_mount(rows,p)
        if m.dev!=f'{os.major(s.st_dev)}:{os.minor(s.st_dev)}':raise StorageDenied('Mount device changed or mismatch')
        paths.append(p);mounts.append(m)
    d,b,k=mounts
    if len({m.dev for m in mounts})!=3:raise StorageDenied('Separate storage devices required')
    luks_ids=set()
    for m in (d,b):
        if m.fstype not in ('ext4','xfs'):raise StorageDenied('Dedicated ext4/xfs filesystem required')
        try:uuid=(sys_block/m.dev/'dm'/'uuid').read_text().strip()
        except OSError:raise StorageDenied('Direct LUKS device mapper not established') from None
        match=re.fullmatch(r'CRYPT-LUKS[12]-([0-9a-fA-F]{32})-.+',uuid)
        if not match:raise StorageDenied('Direct LUKS device mapper not established')
        if match[1].lower() in luks_ids:raise StorageDenied('Duplicate LUKS identity')
        luks_ids.add(match[1].lower())
    if k.fstype!='tmpfs' or k.source!='tmpfs':raise StorageDenied('Key staging requires dedicated tmpfs')
    if not swaps or len(swaps.splitlines())!=1 or not swaps.splitlines()[0].startswith('Filename'):raise StorageDenied('Active or unverified swap; keys may persist')
    return {'local_storage_checks':'PASS','data':'direct LUKS mapper','backup':'separate direct LUKS mapper','key_staging':'tmpfs; no active swap','live_launch_authorized':False,'manual_gates':['independent off-host key custody and anchor retention','host firewall and restricted egress verification','trusted restore/start orchestration and deployment boundary','isolated-account acceptance']}
