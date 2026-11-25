"""Owner-only process lock; standard library only."""
import fcntl,os,stat

class RuntimeLock:
    def __init__(self,path):
        self.fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise PermissionError('Unsafe runtime lock')
            fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd);self.fd=None;raise
    def close(self):
        if self.fd is not None:os.close(self.fd);self.fd=None
