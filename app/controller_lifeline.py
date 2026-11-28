"""Dedicated private pipe: fail closed when its sole writer disappears.

Trusted launcher passes the read end and retains the sole write end in
the controlling process (no duplicates in descendants). No heartbeat or
hang detection, no process discovery, no authority to restart.
"""
import asyncio
import fcntl
import os
import stat

from app.egress_policy import EgressDenied


class ControllerLifeline:
    """Own a dedicated inherited read-only pipe for one broker lifetime."""
    def __init__(self,fd,stop):
        try:
            valid=(type(fd) is int and fd>2 and
                   stat.S_ISFIFO(os.fstat(fd).st_mode) and
                   fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE==os.O_RDONLY)
        except OSError:
            valid=False
        if not valid:
            stop.set()
            raise EgressDenied('Dedicated read-only control pipe required')
        self.fd=fd
        self.stop=stop
        self.loop=asyncio.get_running_loop()
        self.reason=None
        try:
            os.set_blocking(fd,False)
            self.loop.add_reader(fd,self._probe)
            # A writer may have exited before registration.
            self._probe()
        except BaseException:
            self.close()
            raise

    def _probe(self):
        if self.fd is None or self.reason is not None:return
        try:
            data=os.read(self.fd,1)
        except BlockingIOError:
            return
        except OSError:
            self.reason='IO_ERROR'
        else:
            self.reason='UNEXPECTED_DATA' if data else 'EOF'
        self.stop.set()
        self.loop.remove_reader(self.fd)

    def close(self):
        if self.fd is not None:
            self.stop.set()
            try:self.loop.remove_reader(self.fd)
            finally:
                os.close(self.fd)
                self.fd=None

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
