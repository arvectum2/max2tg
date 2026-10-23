"""Single session worker entry point.

A per-session OS file lock ensures that even if the supervisor crashes,
two processes cannot use the same Telegram bot / MAX state simultaneously.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import runpy
import sys


def main():
    raw=os.environ.get("MAX2TG_CAPSULE_LOCK")
    if not raw:
        raise SystemExit("Capsule worker requires a per-session lock path")
    lock_path=Path(raw).absolute()
    if lock_path.is_symlink() or not lock_path.parent.is_dir():
        raise SystemExit("Invalid capsule worker lock")
    fd=os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit("Capsule is already running")
    os.chmod(lock_path,0o600)
    # Keep fd open until this Python interpreter exits. An unexpected crash
    # releases the flock. This is not a token or session-store file.
    os.environ["MAX2TG_NO_DOTENV"]="1"
    runpy.run_module("app.main",run_name="__main__")


if __name__=="__main__":
    main()
