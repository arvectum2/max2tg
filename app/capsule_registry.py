"""P2.1: fail-closed, per-tenant registry and private runtime layout.

No MAX/Telegram credentials are stored in SQLite or written to logs.
One capsule is one MAX account, dedicated Telegram bot and private forum
supergroup for this prototype. A shared multi-tenant bot requires a
separate centralized Telegram dispatcher (not implemented yet).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from typing import Mapping

TENANT_ID = re.compile(r"^[a-f0-9]{24}$")
CREDENTIAL_KEYS = frozenset({
    "MAX_TOKEN","MAX_DEVICE_ID","TG_BOT_TOKEN","TG_CHAT_ID",
    "TG_ADMIN_USER_ID","TG_ALLOWED_USER_IDS","TG_ALLOWED_USER_ID",
    "TG_PROXY","MAX_CHAT_IDS",
})
REQUIRED_KEYS = frozenset({
    "MAX_TOKEN","MAX_DEVICE_ID","TG_BOT_TOKEN","TG_CHAT_ID","TG_ADMIN_USER_ID",
})
PRIVATE_MODE = 0o700


def _private_folder(path: Path) -> None:
    # Never chmod an arbitrary pre-existing directory supplied via --root.
    # In particular, passing a home/SSD/repository mount must not change its
    # permissions. Existing 0700 capsule folders are safe to reuse.
    existed=path.exists() or path.is_symlink()
    if existed:
        if path.is_symlink() or not path.is_dir():
            raise ValueError("Capsule folder is not an ordinary directory")
        if stat.S_IMODE(path.stat().st_mode) != PRIVATE_MODE:
            raise PermissionError("Existing capsule root must already be chmod 700")
    else:
        path.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=False)
        if path.is_symlink() or not path.is_dir():
            raise ValueError("Capsule folder is not an ordinary directory")
        os.chmod(path, PRIVATE_MODE)


def _private_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Credentials must be an ordinary private file")
    if path.stat().st_mode & 0o077:
        raise PermissionError("Credential file is readable by group/others")


class CapsuleRegistry:
    """Synchronous SQLite registry; no subprocesses, network, or browser."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().absolute()
        _private_folder(self.root)
        self.tenants = self.root / "tenants"
        _private_folder(self.tenants)
        self.db_path = self.root / "capsules.sqlite3"
        if self.db_path.is_symlink():
            raise ValueError("Symlinked registry database is prohibited")
        self.db = sqlite3.connect(self.db_path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        os.chmod(self.db_path, 0o600)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=1000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS accounts (
            user_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL
          );
          CREATE TABLE IF NOT EXISTS capsules (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES accounts(user_id),
            tg_chat_id TEXT UNIQUE,
            bot_fingerprint TEXT UNIQUE,
            desired_active INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'UNCONFIGURED',
            restarts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at REAL NOT NULL,
            CHECK(desired_active IN(0,1))
          );
          CREATE INDEX IF NOT EXISTS capsules_owner ON capsules(user_id);
        """)
        for suffix in ("-wal", "-shm"):
            name = self.db_path.with_name(self.db_path.name + suffix)
            if name.exists():
                os.chmod(name, 0o600)

    @staticmethod
    def _check_id(value: str) -> str:
        if not isinstance(value, str) or not TENANT_ID.fullmatch(value):
            raise ValueError("Invalid opaque capsule/user ID")
        return value

    def create_account(self) -> str:
        user_id = secrets.token_hex(12)
        self.db.execute("INSERT INTO accounts VALUES(?,?)",(user_id,time.time()))
        return user_id

    def create_capsule(self, user_id: str) -> str:
        self._check_id(user_id)
        if not self.db.execute(
            "SELECT 1 FROM accounts WHERE user_id=?", (user_id,)
        ).fetchone():
            raise KeyError("Unknown owner")
        sid=secrets.token_hex(12)
        self._create_layout(sid)
        self.db.execute(
            "INSERT INTO capsules(session_id,user_id,updated_at) VALUES(?,?,?)",
            (sid,user_id,time.time()))
        return sid

    def _create_layout(self, sid: str) -> Path:
        base = self.tenants / self._check_id(sid)
        for path in (base,base/"state",base/"logs",base/"browser-profile"):
            _private_folder(path)
        return base

    def capsule_dir(self, sid: str) -> Path:
        return self.tenants / self._check_id(sid)

    def get(self, sid: str) -> dict:
        self._check_id(sid)
        row=self.db.execute("SELECT * FROM capsules WHERE session_id=?",
                            (sid,)).fetchone()
        if row is None:
            raise KeyError("Unknown capsule")
        return dict(row)

    def list_capsules(self) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM capsules ORDER BY session_id")]

    def configure(self, sid: str, credentials: Mapping[str,str]) -> None:
        """Install credentials from a trusted operator-supplied mapping.

        Two capsules cannot accidentally poll the same bot or share a
        Telegram forum. Existing credentials must be changed while inactive.
        """
        current=self.get(sid)
        if current["desired_active"]:
            raise ValueError("Deactivate capsule before changing credentials")
        if set(credentials) - CREDENTIAL_KEYS or REQUIRED_KEYS - set(credentials):
            raise ValueError("Unknown or missing credential fields")
        if any(not isinstance(v,str) or not v.strip() or len(v)>4096
               for v in credentials.values()):
            raise ValueError("Credentials must be nonempty strings")
        try:
            group_id=int(credentials["TG_CHAT_ID"])
            admin_id=int(credentials["TG_ADMIN_USER_ID"])
        except ValueError as exc:
            raise ValueError("Invalid Telegram IDs") from exc
        if group_id>=0 or admin_id<=0:
            raise ValueError("Use a private Telegram supergroup and admin ID")
        if credentials.get("TG_ALLOWED_USER_ID"):
            try:
                if int(credentials["TG_ALLOWED_USER_ID"])<=0:
                    raise ValueError("Invalid allowed user")
            except ValueError as exc:
                raise ValueError("Invalid allowed user") from exc
        fp=hashlib.sha256(credentials["TG_BOT_TOKEN"].encode()).hexdigest()
        conflict=self.db.execute(
            "SELECT session_id FROM capsules WHERE session_id<>? "
            "AND (tg_chat_id=? OR bot_fingerprint=?)",
            (sid,str(group_id),fp)).fetchone()
        if conflict:
            raise ValueError("A Telegram group or bot is already assigned")
        base=self._create_layout(sid)
        target=base/"credentials.json"
        if target.is_symlink():
            raise ValueError("Refusing symlinked credentials file")
        temp=base/(".credentials." + secrets.token_hex(8))
        try:
            fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,"w",encoding="utf-8") as stream:
                json.dump(dict(credentials),stream,ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp,target)
            os.chmod(target,0o600)
        finally:
            temp.unlink(missing_ok=True)
        # Persist metadata only after durable credentials replace.
        self.db.execute(
            "UPDATE capsules SET tg_chat_id=?,bot_fingerprint=?,"
            "status='STOPPED',last_error=NULL,updated_at=? WHERE session_id=?",
            (str(group_id),fp,time.time(),sid))

    def credentials(self, sid: str) -> dict[str,str]:
        self.get(sid)
        path=self.capsule_dir(sid)/"credentials.json"
        _private_file(path)
        # Ensure no untrusted paths/values are imported as environment knobs.
        with path.open(encoding="utf-8") as f:
            data=json.load(f)
        if not isinstance(data,dict) or REQUIRED_KEYS-set(data) or (
            set(data)-CREDENTIAL_KEYS):
            raise ValueError("Malformed credential file")
        if any(not isinstance(v,str) or not v.strip() for v in data.values()):
            raise ValueError("Malformed credential values")
        return data

    def set_active(self, sid: str, active: bool) -> None:
        row=self.get(sid)
        if active:
            self.credentials(sid)  # refuse activation without private secrets
        self.db.execute(
            "UPDATE capsules SET desired_active=?,updated_at=? WHERE session_id=?",
            (int(active),time.time(),sid))

    def set_status(self, sid: str, status: str, error: str | None = None,
                   *, restart: bool = False) -> None:
        allowed={
            "UNCONFIGURED","STOPPED","STARTING","ONLINE","AUTH_REQUIRED",
            "RECONNECTING","ERROR","PAUSED","CRASHED",
        }
        if status not in allowed:
            raise ValueError("Invalid capsule status")
        # No exception text, tokens, chat titles or user content in DB.
        if error is not None and not re.fullmatch(r"[A-Z_]{1,48}",error):
            raise ValueError("Only nonsecret error codes are permitted")
        self.get(sid)
        self.db.execute(
            "UPDATE capsules SET status=?,last_error=?,"
            "restarts=restarts+?,updated_at=? WHERE session_id=?",
            (status,error,int(restart),time.time(),sid))

    def close(self) -> None:
        self.db.close()
