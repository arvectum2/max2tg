#!/usr/bin/env python3
"""Render reviewed numeric pins to nftables text; never applies any rules."""
import argparse,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.linux_egress_policy import read_bundle

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pins',required=True);a=p.parse_args()
    try:
        with open(a.pins,encoding='utf-8') as f:bundle=read_bundle(f.read(8193))
        print(bundle.render(),end='')
    except (OSError,ValueError) as exc:
        print('Policy refused: '+type(exc).__name__,file=sys.stderr);sys.exit(1)
