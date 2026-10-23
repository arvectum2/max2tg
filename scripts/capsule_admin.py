#!/usr/bin/env python3
"""P2.1 local operator CLI; no network and never prints secrets.

Example: .venv/bin/python scripts/capsule_admin.py --root ~/capsules create-user
Then create-session, configure from a private 0600 JSON file, and enable.
Do not reuse the current personal MAX session or Telegram bot token.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.capsule_registry import CapsuleRegistry
from app.capsule_supervisor import CapsuleSupervisor


def main():
    parser=argparse.ArgumentParser(description="Offline MAX2TG Session Capsule operator")
    parser.add_argument("--root",default=str(Path.home()/
        "Library"/"Application Support"/"Arvectum"/"MAX2TG-SaaS"),
        help="Private SaaS root (separate from personal MAX2TG state)")
    sub=parser.add_subparsers(dest="operation",required=True)
    sub.add_parser("create-user")
    mk=sub.add_parser("create-session");mk.add_argument("user_id")
    cfg=sub.add_parser("configure");cfg.add_argument("session_id")
    cfg.add_argument("--credentials-file",required=True,
        help="Path to chmod 600 JSON, read only (never shell arguments)")
    active=sub.add_parser("activate");active.add_argument("session_id")
    inactive=sub.add_parser("deactivate");inactive.add_argument("session_id")
    sub.add_parser("list")
    serve=sub.add_parser("supervise")
    serve.add_argument("--interval",type=int,default=10)
    args=parser.parse_args()
    registry=CapsuleRegistry(args.root)
    try:
        if args.operation=="create-user":
            print("USER_ID",registry.create_account())
        elif args.operation=="create-session":
            print("SESSION_ID",registry.create_capsule(args.user_id))
        elif args.operation=="configure":
            path=Path(args.credentials_file)
            if (path.is_symlink() or not path.is_file() or
                path.stat().st_mode & 0o077):
                parser.error("Credentials file must be ordinary and chmod 600")
            data=json.loads(path.read_text(encoding="utf-8"))
            registry.configure(args.session_id,data)
            print("CONFIGURED",args.session_id)
        elif args.operation=="activate":
            registry.set_active(args.session_id,True)
            print("ACTIVATED",args.session_id)
        elif args.operation=="deactivate":
            registry.set_active(args.session_id,False)
            print("DEACTIVATED",args.session_id)
        elif args.operation=="list":
            for row in registry.list_capsules():
                print(row["session_id"],row["user_id"],
                      row["status"],"enabled" if row["desired_active"] else "disabled",
                      "tg-configured" if row["tg_chat_id"] else "no-telegram")
        elif args.operation=="supervise":
            if not 1<=args.interval<=120:
                parser.error("--interval must be 1..120")
            supervisor=CapsuleSupervisor(registry,Path(__file__).resolve().parents[1])
            supervisor.run_forever(interval=args.interval)
    finally:
        registry.close()


if __name__=="__main__":
    main()
