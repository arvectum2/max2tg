"""Read-only cgroup v2 evidence, never a process-control or launch authority.

Caller supplies a held, reviewed directory FD and its exact identity.
A recursive empty snapshot is not a fence against subsequent migration.
"""
import os
import re
import stat
import sys

from app.generation_ledger import require

LIMIT=65536


def _mount_id(fd):
    with open(f'/proc/self/fdinfo/{fd}',encoding='ascii') as f:
        info=dict(line.split(':',1) for line in f.read(4096).splitlines())
    return int(info['mnt_id'])


def identity(fd):
    require(sys.platform=='linux' and type(fd) is int and fd>2)
    st=os.fstat(fd)
    require(stat.S_ISDIR(st.st_mode) and st.st_nlink>0)
    mid=_mount_id(fd)
    with open('/proc/self/mountinfo',encoding='ascii') as f:
        mounts=[line for line in f if line.split(' ',1)[0]==str(mid)]
    require(len(mounts)==1 and mounts[0].split(' - ',1)[1].split()[0]=='cgroup2')
    with open('/proc/sys/kernel/random/boot_id',encoding='ascii') as f:boot=f.read().strip()
    return {'boot':boot,'mount':mid,'device':st.st_dev,'inode':st.st_ino}


def _read(fd,name):
    member=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    with os.fdopen(member,'rb') as f:
        require(stat.S_ISREG(os.fstat(f.fileno()).st_mode) and _mount_id(member)==_mount_id(fd))
        raw=f.read(LIMIT+1)
    require(len(raw)<=LIMIT)
    return raw.decode('ascii')


def parse_events(raw):
    events={}
    for line in raw.splitlines():
        fields=line.split()
        require(len(fields)==2 and fields[0] in ('populated','frozen')
                and fields[0] not in events and fields[1] in ('0','1'))
        events[fields[0]]=fields[1]=='1'
    require(set(events)=={'populated','frozen'})
    return events


def readback(fd,*,expected):
    require(identity(fd)==expected)
    require(_read(fd,'cgroup.type').strip()=='domain')
    first=parse_events(_read(fd,'cgroup.events'))
    direct=_read(fd,'cgroup.procs').splitlines()
    # PID zero denotes a task outside the observing PID namespace.
    require(all(re.fullmatch('0|[1-9][0-9]*',p) for p in direct))
    second=parse_events(_read(fd,'cgroup.events'))
    require(first==second and (first['populated'] or not direct))
    require(identity(fd)==expected)
    return dict(first,direct_process_count=len(direct),
                recursively_empty=not first['populated'] and not direct,
                live_launch_authorized=False)
