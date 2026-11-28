"""Durable, single-use retirement of an explicitly owned delegated cohort.

This primitive is restricted to the acceptance service and its fixed fresh
subtree. It does not create processes, discover targets, adopt after restart,
or grant launch authority. Trusted caller excludes concurrent migration.
"""
import json
import os
from pathlib import Path
import re
import time

from app.cgroup_readback import identity, readback, _read, _mount_id
from app.encrypted_storage import _read as read_private, _write
from app.generation_ledger import require
from app.recovery_manifest import _private_dir
from app.runtime_lock import RuntimeLock

UNIT=re.compile(r'/system.slice/max2tg-acceptance-[0-9]+-[0-9]+\.service')
LIMIT=8192


def membership():
    lines=Path('/proc/self/cgroup').read_text().splitlines()
    require(len(lines)==1 and lines[0].startswith('0::/'))
    return lines[0][3:]


class RetirementJournal:
    def __init__(self,folder):
        self.root=_private_dir(folder)
        self.path=self.root/'retirement.json'
        self.lock=RuntimeLock(self.root/'retirement.lock')
        self.record=None

    @staticmethod
    def validate(v):
        require(type(v) is dict and set(v)=={'version','state','parent','target','unit'}
                and type(v['version']) is int and v['version']==1
                and v['state'] in ('ARMED','STOPPING','RETIRED')
                and type(v['unit']) is str and UNIT.fullmatch(v['unit']))
        for k in ('parent','target'):
            b=v[k]
            require(type(b) is dict and set(b)=={'boot','mount','device','inode'}
                    and type(b['boot']) is str and re.fullmatch('[0-9a-f-]{36}',b['boot'])
                    and all(type(b[x]) is int and b[x]>0 for x in ('mount','device','inode')))
        require(v['parent']!=v['target'] and all(v['parent'][k]==v['target'][k]
                for k in ('boot','mount','device')))
        return v

    def _load(self):
        require(self.lock.fd is not None)
        def unique(pairs):
            result={}
            for k,v in pairs:
                require(k not in result)
                result[k]=v
            return result
        return self.validate(json.loads(read_private(self.path,LIMIT),object_pairs_hook=unique))

    def _save(self,value,replace):
        require(self.lock.fd is not None)
        self.validate(value)
        _write(self.path,json.dumps(value,sort_keys=True,separators=(',',':')).encode(),replace=replace)
        self.record=value

    def arm(self,parent,target,unit):
        require(self.record is None and not self.path.exists() and not self.path.is_symlink()
                and {p.name for p in self.root.iterdir()}=={'retirement.lock'})
        self._save({'version':1,'state':'ARMED','parent':parent,'target':target,'unit':unit},False)

    def advance(self,state):
        require(self.record is not None and self._load()==self.record)
        require((self.record['state'],state) in (('ARMED','STOPPING'),('STOPPING','RETIRED')))
        self._save(dict(self.record,state=state),True)

    def inspect(self):
        return dict(self._load(),automatic_retry=False,live_launch_authorized=False)

    def close(self):
        self.record=None;self.lock.close()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


class DelegatedRetirement:
    """Held exact service subtree; require durable intent before retirement."""
    def __init__(self,journal,*,parent_fd,target_fd):
        require(type(journal) is RetirementJournal and os.getuid()!=0)
        self.journal=journal
        self.parent_fd=parent_fd
        self.target_fd=target_fd
        self.unit=membership()
        require(UNIT.fullmatch(self.unit))
        self.parent=identity(parent_fd)
        self.target=identity(target_fd)
        self._used=False
        self._verified=False
        self._check()
        require(readback(target_fd,expected=self.target)['recursively_empty'])
        journal.arm(self.parent,self.target,self.unit)

    def _check(self):
        require(membership()==self.unit and identity(self.parent_fd)==self.parent
                and identity(self.target_fd)==self.target and self.parent!=self.target)
        require(os.fstat(self.parent_fd).st_uid==os.getuid())
        parent=os.open('/sys/fs/cgroup'+self.unit,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:require(identity(parent)==self.parent)
        finally:os.close(parent)
        target=os.open('max2tg-fixture',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,
                       dir_fd=self.parent_fd)
        try:require(identity(target)==self.target)
        finally:os.close(target)
        require(not _read(self.target_fd,'cgroup.procs').strip())

    def retire(self,*,operator_approved,verify_owned):
        require(not self._used)
        self._used=True
        require(operator_approved is True)
        self._check()
        require(verify_owned() is True)
        self.journal.advance('STOPPING')
        self._check()
        require(verify_owned() is True)
        fd=os.open('cgroup.kill',os.O_WRONLY|os.O_NOFOLLOW,dir_fd=self.target_fd)
        try:
            require(_mount_id(fd)==self.target['mount'])
            self._check()
            require(os.write(fd,b'1')==1)
        finally:os.close(fd)
        deadline=time.monotonic()+3
        while not readback(self.target_fd,expected=self.target)['recursively_empty']:
            require(time.monotonic()<deadline)
            time.sleep(.05)
        self._check()
        self.journal.advance('RETIRED')
        self._verified=True
        return self.journal.inspect()

    def stopped(self):
        """Live evidence only; historical RETIRED alone cannot authorize recovery."""
        if not self._verified:return False
        self._check()
        evidence=self.journal.inspect()
        return (evidence['state']=='RETIRED' and evidence['parent']==self.parent
                and evidence['target']==self.target and evidence['unit']==self.unit
                and readback(self.target_fd,expected=self.target)['recursively_empty'])
