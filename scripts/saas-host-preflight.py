#!/usr/bin/env python3
"""Read-only local storage evidence. No credential reads or launch."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.host_storage_preflight import inspect_storage

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',required=True);p.add_argument('--backup',required=True);p.add_argument('--keys',required=True);a=p.parse_args()
    try:print(json.dumps(inspect_storage(a.data,a.backup,a.keys),indent=2))
    except (OSError,ValueError) as exc:
        print(json.dumps({'local_storage_checks':'BLOCKED','live_launch_authorized':False,'reason':str(exc)}));sys.exit(1)
