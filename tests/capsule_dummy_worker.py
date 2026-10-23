"""Offline integration probe; never connects to MAX or Telegram."""
import fcntl
import json
import os
from pathlib import Path
import signal
import time

active=True

def stop(*_):
    global active
    active=False

def main():
    lock=Path(os.environ["MAX2TG_CAPSULE_LOCK"])
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    signal.signal(signal.SIGTERM,stop)
    state=Path(os.environ["STATE_DIR"])
    data={
        "max":{"state":"ONLINE"},
        "telegram":{"polling":True},
        "issues":[],
    }
    inspection={
        "state_dir":str(state),
        "log_dir":os.environ["LOG_DIR"],
        "profile_dir":os.environ["MAX2TG_BROWSER_PROFILE"],
        "tg_chat_id":os.environ["TG_CHAT_ID"],
        "personal_token_inherited":"PERSONAL_TOKEN_SENTINEL" in repr(os.environ),
        "parent_env_inherited":"MAX2TG_PARENT_SECRET" in os.environ,
        "skip_personal_dotenv":os.environ.get("MAX2TG_NO_DOTENV"),
        "pid":os.getpid(),
    }
    (state/"inspection.json").write_text(json.dumps(inspection))
    while active:
        (state/"health.json").write_text(json.dumps(data))
        time.sleep(0.05)
    os.close(fd)

if __name__=="__main__":
    main()
