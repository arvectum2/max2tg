"""Single-use operator rotation grant, bound to a held Linux network namespace.

No namespace creation, discovery, live launch, DNS, credentials or general command
interface. Trusted operator supplies the exact namespace keeper PID and pre-reviewed bundles.
The broker must be launched by the trusted caller into the held namespace.
"""
import hashlib
import json
import os
import re
import secrets
import select
import subprocess
import sys
import time

from app.egress_policy import EgressDenied
from app.linux_pin_rotation import rotate_quiesced_policy, rotation_batch

MAX_FRAME=1024


def _deny(message):
    raise EgressDenied(message)


def _identity(stat):
    return stat.st_dev, stat.st_ino


def _start(pid):
    # comm may contain spaces and right parentheses; field 22 is index 19
    # after the final closing parenthesis and process state.
    with open(f'/proc/{pid}/stat', encoding='ascii') as stream:
        tail=stream.read().rsplit(')',1)[1].split()
    if tail[0] in ('Z','X','x'):
        _deny('Namespace keeper process exited')
    return int(tail[19])


class NamespaceHandle:
    """Keep the namespace fd open; never enter by a mutable PID at mutation time."""
    def __init__(self,pid,*,expected):
        if sys.platform!='linux' or type(pid) is not int or pid<=1:
            _deny('Exact Linux namespace keeper PID required')
        self.pid=pid;self.fd=None
        self.started=_start(pid)
        self.fd=os.open(f'/proc/{pid}/ns/net',os.O_RDONLY|os.O_CLOEXEC)
        try:
            self.identity=_identity(os.fstat(self.fd))
            if self.identity!=expected or self.identity==_identity(os.stat('/proc/self/ns/net')):
                _deny('Foreign or operator network namespace')
            self.check()
        except BaseException:
            self.close();raise

    def check(self):
        if (self.fd is None or _start(self.pid)!=self.started
                or _identity(os.fstat(self.fd))!=self.identity
                or _identity(os.stat(f'/proc/{self.pid}/ns/net'))!=self.identity):
            _deny('Held namespace identity changed')

    def binding(self):
        self.check()
        with open('/proc/sys/kernel/random/boot_id',encoding='ascii') as stream:
            boot=stream.read().strip()
        if not re.fullmatch('[0-9a-f-]{36}',boot):_deny('Invalid boot identity')
        return {'boot':boot,'pid':self.pid,'start':self.started,'namespace':list(self.identity)}

    def retired(self):
        """Read-only snapshot: loopback is the only active interface; no nonlocal routes."""
        self.check()
        def ip(*args):
            result=subprocess.run(
                ['/usr/bin/nsenter',f'--net=/proc/self/fd/{self.fd}','--',
                 '/usr/sbin/ip','-j',*args],pass_fds=(self.fd,),
                text=True,capture_output=True,check=True,timeout=4)
            return json.loads(result.stdout)
        links=ip('link')
        routes=ip('-4','route','show','table','all')+ip('-6','route','show','table','all')
        self.check()
        return (type(links) is list and
                {i['ifname'] for i in links if 'UP' in i['flags']}=={'lo'}
                and all(r.get('dev')=='lo' and r.get('table')=='local'
                        and r.get('type') in ('local','broadcast')
                        and 'gateway' not in r and 'nexthops' not in r for r in routes))

    def _nft(self,args,*,data=None):
        self.check()
        result=subprocess.run(
            ['/usr/bin/nsenter',f'--net=/proc/self/fd/{self.fd}','--',
             '/usr/sbin/nft',*args],pass_fds=(self.fd,),input=data,
            text=True,capture_output=True,check=True,timeout=4)
        self.check()
        return result.stdout

    def observe(self):
        return json.loads(self._nft(['-j','list','ruleset']))

    def apply_reviewed(self,current,replacement,batch):
        # The exact address set is the only permitted mutation. Re-rendering
        # is time-sensitive; compare the grammar and each approved address,
        # using the TTL from this batch and a bounded current remaining time.
        match=re.fullmatch(r'flush set inet m2tg_egress api_ipv4\nadd element inet m2tg_egress api_ipv4 \{ (.+) \}\n',batch)
        if not match:_deny('Unexpected rotation transaction')
        items=match[1].split(', ')
        expected=replacement.max_text+replacement.telegram
        if len(items)!=len(expected):_deny('Unexpected pin count')
        ttls=[]
        for item,ip in zip(items,expected):
            m=re.fullmatch(re.escape(ip)+r' timeout ([1-9][0-9]{0,3})s',item)
            if not m:_deny('Unexpected pin transaction')
            ttls.append(int(m[1]))
        remaining=replacement.remaining()
        if len(set(ttls))!=1 or ttls[0]>remaining or remaining-ttls[0]>1:
            _deny('Unexpected rotation lifetime')
        rotation_batch(current,replacement,now=time.time())
        self._nft(['-f','-'],data=batch)

    def close(self):
        if self.fd is not None:
            os.close(self.fd);self.fd=None


def digest(pins):
    return hashlib.sha256(json.dumps(pins.record(),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def frame(record):
    result=json.dumps(record,sort_keys=True,separators=(',',':')).encode('ascii')+b'\n'
    if len(result)>MAX_FRAME:_deny('Rotation frame too large')
    return result


def parse(raw):
    if type(raw) is not bytes or not raw.endswith(b'\n') or len(raw)>MAX_FRAME:
        _deny('Invalid rotation frame')
    def unique(pairs):
        value={}
        for k,v in pairs:
            if k in value:_deny('Duplicate rotation field')
            value[k]=v
        return value
    try:
        value=json.loads(raw.decode('ascii'),object_pairs_hook=unique)
    except (ValueError,UnicodeError):
        _deny('Malformed rotation frame')
    if type(value) is not dict:_deny('Expected rotation object')
    return value


def read_frame_fd(fd, *, timeout=4):
    """Bound both frame length and time, including incomplete/slow writes."""
    deadline=time.monotonic()+timeout
    data=bytearray()
    while len(data)<MAX_FRAME:
        remaining=deadline-time.monotonic()
        if remaining<=0 or not select.select([fd],[],[],remaining)[0]:
            _deny('Rotation frame timeout')
        chunk=os.read(fd,1)
        if not chunk:_deny('Rotation channel closed')
        data.extend(chunk)
        if chunk==b'\n':return bytes(data)
    _deny('Rotation frame too large')


def request(nonce,current,replacement):
    return {'version':1,'operation':'rotate','nonce':nonce,
            'current':digest(current),'replacement':digest(replacement)}


class RotationGrant:
    """Exactly one request on a private inherited channel; no caller-supplied commands."""
    def __init__(self,namespace,current,replacement):
        rotation_batch(current,replacement,now=time.time())
        self.namespace=namespace;self.current=current;self.replacement=replacement
        self.nonce=secrets.token_hex(24);self.used=False;self.state='HELD'

    def execute(self,raw):
        if self.used:_deny('Rotation grant already consumed')
        self.used=True;self.state='BLOCKED'
        value=parse(raw)
        expected=request(self.nonce,self.current,self.replacement)
        if value!=expected or type(value.get('version')) is not int:
            _deny('Rotation grant mismatch')
        self.namespace.check()
        result=rotate_quiesced_policy(
            self.current,self.replacement,observe=self.namespace.observe,
            apply=lambda batch:self.namespace.apply_reviewed(self.current,self.replacement,batch))
        self.namespace.check()
        self.state='VERIFIED'
        return frame({'grant':expected,'result':result})


def read_receipt(raw,nonce,current,replacement):
    value=parse(raw)
    if set(value)!={'grant','result'} or value['grant']!=request(nonce,current,replacement):
        _deny('Rotation receipt does not match grant')
    if type(value['grant'].get('version')) is not int:
        _deny('Invalid receipt version')
    return value['result']
