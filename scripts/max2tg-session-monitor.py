"""Read-only session readiness monitor; no browser credential extraction.

The existing Chrome authorization is managed by the account owner.
A denied directory access is reported, not bypassed.
"""
import json
import os
from pathlib import Path
import tempfile
import time

ROOT=Path("/Volumes/ArvectumSSD/Arvectum/arvectum-max-bridge")
STATE=Path.home()/".local/state/max2tg"
CHROME=Path.home()/"Library/Application Support/Google/Chrome"

def read_json(p):
    try:
        return json.loads(p.read_text())
    except (OSError,ValueError):
        return {}

def check():
    if not (ROOT/".env").is_file():
        return "WAIT_REPO"
    try:
        profiles=read_json(CHROME/"Local State").get("profile",{}).get("info_cache",{})
        profile=next((k for k,v in profiles.items()
                      if str(v.get("name","")).strip().upper()=="MAX"),"Profile 4")
        if Path(profile).name!=profile:
            return "PROFILE_INVALID"
        # Only check access to the directory, never read browser session contents.
        with os.scandir(CHROME/profile/"Local Storage/leveldb") as entries:
            next(entries,None)
    except PermissionError:
        return "ACCESS_DENIED"
    except OSError:
        return "PROFILE_UNAVAILABLE"
    if read_json(STATE/"health.json").get("max",{}).get("state")=="REAUTH_REQUIRED":
        return "REAUTH_REQUIRED"
    return "MANUAL_REAUTH_ONLY"

def main():
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=STATE/"session-watch.json"
    code=check()
    if read_json(path).get("state")!=code:
        print("MAX session monitor:",code,flush=True)
    fd,tmp=tempfile.mkstemp(prefix=".session-monitor-",dir=STATE)
    with os.fdopen(fd,"w") as f:
        json.dump({"state":code,"time":time.time()},f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp,path)

if __name__=="__main__":
    main()
