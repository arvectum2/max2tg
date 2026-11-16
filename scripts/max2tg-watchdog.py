"""Independent launchd supervisor. Standard library only; no project imports."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import urllib.request

STATE=Path(os.environ.get("MAX2TG_STATE_DIR",str(Path.home()/".local/state/max2tg")))
REPO=Path(os.environ.get("MAX2TG_REPO","/Volumes/ArvectumSSD/Arvectum/arvectum-max-bridge"))
LABEL="com.arvectum.max2tg"
STALE=150
START_GRACE=180
RESTART_COOLDOWN=300
REMINDER_COOLDOWN=86400


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError,ValueError):
        return {}


def write_json(path,obj):
    tmp=path.with_suffix(".tmp")
    fd=os.open(tmp,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
    with os.fdopen(fd,"w") as f:
        json.dump(obj,f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp,path)


def mtime_age(path,now):
    try:
        return max(0,now-path.stat().st_mtime)
    except OSError:
        return float("inf")


def evaluate(pid,health,health_age,launcher,launcher_age):
    # An old ONLINE file must never make a new process look healthy.
    if not pid:
        return "WORKER_MISSING",False
    if launcher in {"WAIT_REPO","WAIT_PROXY"} and launcher_age<30:
        return launcher,False
    if launcher in {"RUNTIME_MISSING","RUNTIME_INVALID"}:
        return launcher,False
    if health.get("pid")==pid and health_age<=STALE:
        return "RUNNING",False
    if launcher_age<START_GRACE:
        return "STARTING",False
    return "HEARTBEAT_STALE",True


def notify(code):
    # Separate network client: it does not depend on python-telegram-bot.
    # Credentials never enter command line arguments / logs.
    try:
        env={}
        for line in (REPO/".env").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k,v=line.split("=",1)
                env[k.strip()]=v.strip().strip('"').strip("'")
        proxy=env.get("TG_PROXY")
        handlers=[urllib.request.ProxyHandler({"http":proxy,"https":proxy})] if proxy else []
        opener=urllib.request.build_opener(*handlers)
        body=urllib.parse.urlencode({
            "chat_id":env["TG_CHAT_ID"],
            "text":"⚠️ MAX2TG: независимый watchdog — "+code+
                   ". Не считай отправку подтверждённой без 👀.",
            "disable_notification":"true",
        }).encode()
        req=urllib.request.Request("https://api.telegram.org/bot"+env["TG_BOT_TOKEN"]+"/sendMessage",
                                   data=body,method="POST")
        with opener.open(req,timeout=8) as response:
            return bool(json.load(response).get("ok"))
    except Exception as e:
        print("Watchdog notification unavailable:",type(e).__name__,flush=True)
        return False


def main():
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    now=time.time()
    rows=subprocess.run(["/bin/launchctl","list"],capture_output=True,text=True,timeout=8,check=True).stdout
    row=next((line.split() for line in rows.splitlines() if line.split()[-1:]==[LABEL]),None)
    pid=int(row[0]) if row and row[0].isdigit() else None
    hp=STATE/"health.json"
    lp=STATE/"launcher.status"
    launcher=""
    try:
        launcher=lp.read_text().split()[0]
    except (OSError,IndexError):
        pass
    code,restart=evaluate(pid,read_json(hp),mtime_age(hp,now),launcher,mtime_age(lp,now))
    previous=read_json(STATE/"watchdog.json")
    since=previous.get("since",now) if previous.get("state")==code else now
    last_restart=previous.get("last_restart",0)
    last_alert=previous.get("last_alert",0) if previous.get("state")==code else 0
    bad_since=previous.get("bad_since",now) if previous.get("state")!="RUNNING" else now
    if code=="RUNNING":
        since=now
        bad_since=now
    else:
        if restart and now-last_restart>=RESTART_COOLDOWN:
            last_restart=now
            print("Watchdog: restarting stale worker",flush=True)
            subprocess.run(["/bin/launchctl","kickstart","-k",f"gui/{os.getuid()}/{LABEL}"],
                           timeout=15,check=False)
        if now-bad_since>=START_GRACE and now-last_alert>=REMINDER_COOLDOWN:
            # Record only successful alerts; failed delivery is retried with
            # a separate short cooldown below.
            last_attempt=previous.get("last_attempt",0)
            if now-last_attempt>=RESTART_COOLDOWN:
                previous["last_attempt"]=now
                print("Watchdog:",code,flush=True)
                if notify(code):
                    last_alert=now
    write_json(STATE/"watchdog.json",{
        "time":datetime.now(timezone.utc).isoformat(),"state":code,"pid":pid,
        "since":since,"bad_since":bad_since,"last_restart":last_restart,"last_alert":last_alert,
        "last_attempt":previous.get("last_attempt",0)})
    return 0

if __name__=="__main__":
    sys.exit(main())
