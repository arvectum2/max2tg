#!/usr/bin/env python3
"""Render a held Compose JSON plan; creates no files or containers."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.deployment_plan import build_plan
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ('deployment','image','data','keys','backup'):p.add_argument('--'+n,required=True)
    p.add_argument('--sid',required=True,action='append');a=p.parse_args()
    print(json.dumps(build_plan(a.deployment,a.image,a.sid,data=a.data,keys=a.keys,backup=a.backup),indent=2))
