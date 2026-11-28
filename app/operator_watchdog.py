"""Independent Linux watchdog for a single explicitly owned broker.

The trusted launcher passes an exact pidfd and a dedicated progress pipe,
starts the watchdog before admitting traffic, and retains the private
journal outside runtime state. No PID lookup, group kill, process discovery,
launch or automatic restart. Descendants require a separate cgroup boundary.
"""
import asyncio
import json
import os
import re
import signal
import sys

from app.controller_lease import ControllerLease
from app.egress_policy import EgressDenied
from app.encrypted_storage import _read, _write
from app.generation_ledger import require
from app.recovery_manifest import _private_dir
from app.runtime_lock import RuntimeLock

LIMIT=4096
STATES=('ARMED','STOPPING','STOPPED','EXITED','UNVERIFIED')
REASONS=('NONE','LEASE_EXPIRED','EOF','IO_ERROR','INVALID_RENEWAL','WATCHDOG_ERROR','TARGET_EXIT')


def pidfd_identity(fd,pid):
    require(sys.platform=='linux' and type(fd) is int and fd>2 and
            type(pid) is int and pid>1 and pid!=os.getpid())
    require(os.readlink(f'/proc/self/fd/{fd}')=='anon_inode:[pidfd]')
    with open(f'/proc/self/fdinfo/{fd}',encoding='ascii') as f:
        info=dict(line.split(':',1) for line in f.read(4096).splitlines())
    require(int(info['Pid'].strip())==pid)
    with open('/proc/sys/kernel/random/boot_id',encoding='ascii') as f:boot=f.read().strip()
    return {'pid':pid,'boot':boot,'inode':os.fstat(fd).st_ino}


class WatchdogJournal:
    def __init__(self,folder):
        self.root=_private_dir(folder)
        self.path=self.root/'watchdog.json'
        self.lock=RuntimeLock(self.root/'watchdog.lock')
        self.record=None

    def _validate(self,value):
        require(type(value) is dict and set(value)=={'version','target','state','reason'}
                and type(value['version']) is int and value['version']==1
                and value['state'] in STATES and value['reason'] in REASONS)
        target=value['target']
        require(type(target) is dict and set(target)=={'pid','boot','inode'}
                and type(target['pid']) is int and target['pid']>1
                and type(target['inode']) is int and target['inode']>0
                and type(target['boot']) is str and
                re.fullmatch('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',target['boot']))
        require((value['state']=='ARMED' and value['reason']=='NONE')
                or (value['state']=='EXITED' and value['reason']=='TARGET_EXIT')
                or (value['state'] in ('STOPPING','STOPPED','UNVERIFIED')
                    and value['reason'] not in ('NONE','TARGET_EXIT')))
        return value

    def _save(self,record,*,replace):
        require(self.lock.fd is not None)
        self._validate(record)
        _write(self.path,json.dumps(record,sort_keys=True,separators=(',',':')).encode(),replace=replace)
        self.record=record

    def inspect(self):
        require(self.lock.fd is not None)
        def unique(pairs):
            result={}
            for key,value in pairs:
                require(key not in result)
                result[key]=value
            return result
        value=self._validate(json.loads(_read(self.path,LIMIT),object_pairs_hook=unique))
        return dict(value,automatic_retry=False,live_launch_authorized=False)

    def arm(self,target):
        require(self.record is None and not self.path.exists() and not self.path.is_symlink()
                and {p.name for p in self.root.iterdir()}=={'watchdog.lock'})
        self._save({'version':1,'target':target,'state':'ARMED','reason':'NONE'},replace=False)

    def advance(self,state,reason):
        require(self.record is not None and state in STATES and reason in REASONS)
        allowed={'ARMED':('STOPPING','EXITED'),'STOPPING':('STOPPED','UNVERIFIED')}
        require(state in allowed.get(self.record['state'],()))
        evidence=self.inspect()
        require(all(evidence[k]==v for k,v in self.record.items()))
        if self.record['state']=='STOPPING':require(reason==self.record['reason'])
        self._save(dict(self.record,state=state,reason=reason),replace=True)

    def close(self):
        self.record=None;self.lock.close()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


async def watch(folder,*,pidfd,pid,progress_fd,seconds):
    """One guardian lifetime; terminal evidence never grants a new start."""
    require(pidfd!=progress_fd)
    target=pidfd_identity(pidfd,pid)
    loop=asyncio.get_running_loop()
    exited=asyncio.Event()
    stop=asyncio.Event()
    with WatchdogJournal(folder) as journal:
        journal.arm(target)
        loop.add_reader(pidfd,exited.set)
        reason='WATCHDOG_ERROR'
        try:
            try:
                with ControllerLease(progress_fd,stop,seconds=seconds) as lease:
                    a=asyncio.create_task(exited.wait())
                    b=asyncio.create_task(stop.wait())
                    try:
                        await asyncio.wait((a,b),return_when=asyncio.FIRST_COMPLETED)
                        reason=lease.reason or 'WATCHDOG_ERROR'
                    finally:
                        a.cancel();b.cancel()
                        await asyncio.gather(a,b,return_exceptions=True)
            except Exception:
                reason='WATCHDOG_ERROR'
            if exited.is_set():
                journal.advance('EXITED','TARGET_EXIT')
                return journal.inspect()
            # If durable intent fails, still attempt exact pidfd shutdown;
            # the retained ARMED/STOPPING record remains non-authorizing.
            try:journal.advance('STOPPING',reason)
            finally:
                try:signal.pidfd_send_signal(pidfd,signal.SIGKILL)
                except ProcessLookupError:pass
            try:await asyncio.wait_for(exited.wait(),3)
            except TimeoutError:
                journal.advance('UNVERIFIED',reason)
                raise EgressDenied('Owned broker exit not verified')
            journal.advance('STOPPED',reason)
            return journal.inspect()
        finally:loop.remove_reader(pidfd)
