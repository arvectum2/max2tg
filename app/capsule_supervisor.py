"""P2.1 isolated supervisor, not used by the existing personal launchd job.

Process per capsule. Each starts app.capsule_worker with private STATE_DIR,
LOG_DIR, credentials and a single-owner lock. No shared TG polling token.
A single supervisor holds a flock so duplicate supervisors cannot launch
competing worker trees. Workers never receive the existing personal .env.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Callable

from app.capsule_registry import CapsuleRegistry

ENV_WHITELIST = frozenset((
    "HOME","PATH","TMPDIR","LANG","LC_ALL","SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE","PYTHONUNBUFFERED","PYTHONDONTWRITEBYTECODE",
))


def build_worker_env(registry: CapsuleRegistry, sid: str,
                     parent: dict[str,str] | None = None) -> dict[str,str]:
    """Fail-closed worker environment; never inherit the personal bot token."""
    trusted=registry.credentials(sid)
    parent=os.environ if parent is None else parent
    env={k:v for k,v in parent.items() if k in ENV_WHITELIST}
    layout=registry.capsule_dir(sid)
    env.update(trusted)
    env.update({
        "STATE_DIR":str(layout/"state"),"LOG_DIR":str(layout/"logs"),
        "MAX2TG_CAPSULE_LOCK":str(layout/"worker.lock"),
        "MAX2TG_BROWSER_PROFILE":str(layout/"browser-profile"),
        "MAX2TG_NO_DOTENV":"1",
        "REPLY_ENABLED":"true",
        "DEBUG":"false",
        "PYTHONUNBUFFERED":"1",
    })
    return env


def lock_held(path: Path) -> bool:
    """Probe an OS lock; unlike a PID file this survives stale processes."""
    if path.is_symlink():
        raise ValueError("Symlinked worker lock")
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            # The FD is always closed in outer finally.
            pass
        fcntl.flock(fd,fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class CapsuleSupervisor:
    """Synchronous orchestration; call tick() or run_forever()."""

    def __init__(self, registry: CapsuleRegistry, repo_root: str | Path,
                 *, worker_module: str = "app.capsule_worker",
                 clock: Callable[[],float] = time.time):
        self.registry=registry
        self.repo_root=Path(repo_root).absolute()
        if not (self.repo_root/"app"/"main.py").is_file():
            raise ValueError("Not a MAX2TG project checkout")
        self.clock=clock
        self.worker_module=worker_module
        self._children={}
        self._logs={}
        self._attempts={}
        self._retry_after={}
        self._lock_file=registry.root/"supervisor.lock"
        self._lock_fd=os.open(self._lock_file,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(self._lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_fd)
            raise RuntimeError("Session supervisor already running")
        os.chmod(self._lock_file,0o600)
        self._stopping=False

    def _launch(self, sid):
        base=self.registry.capsule_dir(sid)
        if lock_held(base/"worker.lock"):
            self.registry.set_status(sid,"RECONNECTING", "WORKER_ALREADY_RUNNING")
            return
        env=build_worker_env(self.registry,sid)
        log_path=base/"logs"/"worker.log"
        if log_path.is_symlink():
            raise ValueError("Refusing symlinked worker log")
        log_fd=os.open(log_path,os.O_APPEND|os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        output=os.fdopen(log_fd,"ab",buffering=0)
        try:
            child=subprocess.Popen(
                [sys.executable,"-m",self.worker_module],
                cwd=self.repo_root,env=env,stdin=subprocess.DEVNULL,
                stdout=output,stderr=subprocess.STDOUT,close_fds=True,
                start_new_session=True)
        except Exception:
            output.close()
            raise
        self._children[sid]=child
        self._logs[sid]=output
        self.registry.set_status(sid,"STARTING",
                                 restart=self._attempts.get(sid,0)>0)

    def _cleanup(self,sid):
        self._children.pop(sid,None)
        handle=self._logs.pop(sid,None)
        if handle:
            handle.close()

    def _observe_health(self,sid):
        p=self.registry.capsule_dir(sid)/"state"/"health.json"
        if not p.is_file() or self.clock()-p.stat().st_mtime>90:
            # Do not kill worker: a stuck Telegram API call may be UNKNOWN,
            # and prematurely restarting could compound ambiguity.
            self.registry.set_status(sid,"RECONNECTING","HEARTBEAT_STALE")
            return
        try:
            h=json.loads(p.read_text(encoding="utf-8"))
            state=h.get("max",{}).get("state")
            polling=h.get("telegram",{}).get("polling") is True
        except (ValueError,TypeError,OSError,AttributeError):
            self.registry.set_status(sid,"ERROR","BAD_HEALTH_SNAPSHOT")
            return
        if state=="ONLINE" and polling:
            self.registry.set_status(sid,"ONLINE")
            self._attempts.pop(sid,None)
            return
        if state in ("REAUTH_REQUIRED","AUTH_REQUIRED"):
            self.registry.set_status(sid,"AUTH_REQUIRED")
        elif state=="ONLINE":
            self.registry.set_status(sid,"RECONNECTING","TELEGRAM_NOT_POLLING")
        else:
            self.registry.set_status(sid,"RECONNECTING")

    def tick(self):
        active=set()
        for row in self.registry.list_capsules():
            sid=row["session_id"]
            if not row["desired_active"]:
                if sid in self._children:
                    self.stop_capsule(sid)
                continue
            active.add(sid)
            child=self._children.get(sid)
            if child is not None and child.poll() is None:
                self._observe_health(sid)
                continue
            if child is not None:
                self._cleanup(sid)
                count=self._attempts.get(sid,0)+1
                self._attempts[sid]=count
                self._retry_after[sid]=self.clock()+min(300,5*2**min(6,count-1))
                self.registry.set_status(sid,"CRASHED","WORKER_EXITED")
                continue
            if self.clock()<self._retry_after.get(sid,0):
                continue
            try:
                self._launch(sid)
            except Exception:
                self.registry.set_status(sid,"ERROR","WORKER_START_FAILED")
                self._retry_after[sid]=self.clock()+60
        # If a session was removed/deactivated, retire only owned workers.
        for sid in list(self._children):
            if sid not in active:
                self.stop_capsule(sid)

    def stop_capsule(self,sid, timeout=5):
        child=self._children.get(sid)
        if child is None:
            return
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        self._cleanup(sid)
        self.registry.set_status(sid,"STOPPED")

    def close(self):
        if self._stopping:
            return
        self._stopping=True
        for sid in list(self._children):
            self.stop_capsule(sid)
        fcntl.flock(self._lock_fd,fcntl.LOCK_UN)
        os.close(self._lock_fd)

    def run_forever(self,interval=10):
        # Respect service-manager SIGTERM, so child processes can terminate
        # normally and release Telegram polling and per-capsule flocks.
        previous=signal.getsignal(signal.SIGTERM)
        def request_stop(_signum,_frame):
            self._stopping_requested=True
        self._stopping_requested=False
        signal.signal(signal.SIGTERM,request_stop)
        try:
            while not self._stopping_requested:
                self.tick()
                time.sleep(interval)
        except KeyboardInterrupt:
            return
        finally:
            signal.signal(signal.SIGTERM,previous)
            self.close()


def observe_resources(pids: list[int]) -> list[dict]:
    """Read-only ps RSS/CPU sampling for isolated capsule workers."""
    if not pids:
        return []
    ids=[str(int(pid)) for pid in pids if int(pid)>0]
    if not ids:
        return []
    p=subprocess.run(["ps","-p",",".join(ids),"-o","pid=,rss=,%cpu="],
                     capture_output=True,text=True,timeout=8,check=False)
    result=[]
    for line in p.stdout.splitlines():
        fields=line.split()
        if len(fields)==3:
            try:
                result.append({"pid":int(fields[0]),
                               "rss_mib":round(int(fields[1])/1024,2),
                               "cpu_percent":float(fields[2])})
            except ValueError:
                continue
    return result
