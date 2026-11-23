"""Fail-closed immutable release check before accessing either messenger."""
import importlib
import importlib.metadata as metadata
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
# Direct Python execution starts from scripts/; include the checkout root
# explicitly instead of relying on PYTHONPATH or a launcher-specific -c mode.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RELEASE_PYTHON = (3, 15, 2)
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.!+-]*)$")


def locked_runtime_packages(path: Path) -> dict[str, str]:
    """Parse compiled uv runtime pins; comments are metadata, not dependencies."""
    packages = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = PIN.fullmatch(line)
        if match is None:
            raise SystemExit("Unexpected MAX2TG runtime lock entry")
        name, version = match.groups()
        if name in packages and packages[name] != version:
            raise SystemExit("Conflicting MAX2TG dependency pin")
        packages[name] = version
    if not packages:
        raise SystemExit("Empty MAX2TG runtime lock")
    return packages


def verify_runtime() -> None:
    if sys.version_info[:3] != RELEASE_PYTHON:
        raise SystemExit("Unexpected MAX2TG Python runtime")
    for name, version in locked_runtime_packages(ROOT / "requirements.lock").items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            raise SystemExit("Missing MAX2TG package: " + name) from None
        if actual != version:
            raise SystemExit("Dependency version mismatch: " + name)

    # ABI, TLS and imports must work before connecting to MAX or Telegram.
    for module in (
        "ssl", "aiohttp", "telegram", "httpx", "dotenv", "msgpack",
        "lz4.block", "app.tg_ingress", "app.main",
    ):
        importlib.import_module(module)

    assert sqlite3.connect(":memory:").execute("pragma quick_check").fetchone()[0] == "ok"
    print("MAX2TG runtime preflight OK", flush=True)


if __name__ == "__main__":
    verify_runtime()
