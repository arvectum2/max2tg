"""Bounded progress lease on an inherited private pipe, same host clock.

The actual controlling process renews only after reviewed progress. No
background renewal, no restart authority; expiry is terminal. The broker
must still have an independently enforced network policy and a live event
loop. A stalled broker itself cannot be stopped by this in-process monitor.
"""
import asyncio
import fcntl
import os
import stat
import struct
import time

from app.egress_policy import EgressDenied

FRAME=struct.Struct('!Q')
MIN_SECONDS=1
MAX_SECONDS=30


def renew(fd):
    """One nonblocking atomic frame; no retry or queued renewal."""
    if (type(fd) is not int or fd<=2 or
            not stat.S_ISFIFO(os.fstat(fd).st_mode) or
            fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE!=os.O_WRONLY):
        raise EgressDenied('Private lease writer required')
    os.set_blocking(fd,False)
    if os.write(fd,FRAME.pack(time.monotonic_ns()))!=FRAME.size:
        raise EgressDenied('Incomplete lease renewal')


class ControllerLease:
    def __init__(self,fd,stop,*,seconds):
        try:
            valid=(type(seconds) is int and MIN_SECONDS<=seconds<=MAX_SECONDS and
                   type(fd) is int and fd>2 and stat.S_ISFIFO(os.fstat(fd).st_mode) and
                   fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE==os.O_RDONLY)
        except OSError:
            valid=False
        if not valid:
            stop.set()
            raise EgressDenied('Invalid controller lease')
        self.fd=fd
        self.stop=stop
        self.loop=asyncio.get_running_loop()
        self.ready=asyncio.Event()
        self.reason=None
        self._ttl=seconds*1_000_000_000
        self._deadline=time.monotonic_ns()+self._ttl
        self._last=None
        self._buffer=b''
        self._timer=None
        try:
            os.set_blocking(fd,False)
            self.loop.add_reader(fd,self._probe)
            self._schedule()
            self._probe()
        except BaseException:
            self.close()
            raise

    def _fail(self,reason):
        if self.reason is not None:return
        self.reason=reason
        self.stop.set()
        self.ready.set()
        self.loop.remove_reader(self.fd)
        if self._timer is not None:self._timer.cancel()

    def _schedule(self):
        if self._timer is not None:self._timer.cancel()
        self._timer=self.loop.call_later(max(0,(self._deadline-time.monotonic_ns())/1e9),self._expire)

    def _expire(self):
        if self.reason is not None:return
        if time.monotonic_ns()>=self._deadline:self._fail('LEASE_EXPIRED')
        else:self._schedule()

    def _probe(self):
        if self.fd is None or self.reason is not None:return
        now=time.monotonic_ns()
        if now>=self._deadline:
            self._fail('LEASE_EXPIRED');return
        try:data=os.read(self.fd,FRAME.size*16)
        except BlockingIOError:return
        except OSError:
            self._fail('IO_ERROR');return
        if not data:
            self._fail('EOF');return
        self._buffer+=data
        # Bounded work per callback; incomplete frames never extend a lease.
        while len(self._buffer)>=FRAME.size:
            sent=FRAME.unpack(self._buffer[:FRAME.size])[0]
            self._buffer=self._buffer[FRAME.size:]
            now=time.monotonic_ns()
            if now>=self._deadline:
                self._fail('LEASE_EXPIRED');return
            if (sent>now or sent+self._ttl<=now or
                    self._last is not None and sent<=self._last):
                self._fail('INVALID_RENEWAL');return
            self._last=sent
            self._deadline=sent+self._ttl
            self.ready.set()
        self._schedule()

    def valid(self):
        # Check synchronously before admission, not just after timer dispatch.
        if self.fd is None:return False
        if self.reason is None and time.monotonic_ns()>=self._deadline:
            self._fail('LEASE_EXPIRED')
        return self.reason is None and self._last is not None and not self.stop.is_set()

    def close(self):
        if self.fd is not None:
            self.stop.set()
            self.ready.set()
            if self._timer is not None:self._timer.cancel()
            try:self.loop.remove_reader(self.fd)
            finally:
                os.close(self.fd)
                self.fd=None

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
