"""Validate the immutable runtime before connecting to either live messenger."""
import importlib.metadata as metadata
from pathlib import Path
import sys
import sqlite3

ROOT=Path(__file__).resolve().parents[1]
if sys.version_info[:3] != (3,12,14):
    raise SystemExit("Unexpected MAX2TG Python runtime")
for line in (ROOT/"requirements.lock").read_text().splitlines():
    name,version=line.strip().split("==",1)
    if metadata.version(name)!=version:
        raise SystemExit("Dependency version mismatch: "+name)
# Native extensions and TLS must load; versions alone do not prove ABI health.
import ssl, aiohttp, telegram, httpx, dotenv, msgpack, lz4.block
from app.tg_ingress import DurableIngressQueue
from app.main import main
assert sqlite3.connect(":memory:").execute("pragma quick_check").fetchone()[0]=="ok"
print("MAX2TG runtime preflight OK",flush=True)
