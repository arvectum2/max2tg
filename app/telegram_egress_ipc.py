"""Authenticated reverse Unix IPC: capsule worker -> central Telegram egress.

This module never starts polling or sends to Telegram. Gateway commits text
to its durable outbox before returning QUEUED; the single-bot sender drains
separately. Lost IPC replies can be retried with the same immutable event key.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re

from app.capsule_registry import CapsuleRegistry
from app.telegram_egress import TelegramEgress
from app.telegram_ipc import (
    _read_frame,
    _signed,
    _verify,
    _write_frame,
    ensure_ipc_key,
)

_NONCE = re.compile(r"^[a-f0-9]{32}$")
_PENDING_STATES = frozenset({"QUEUED", "SENDING"})
_RESOLVED_STATES = frozenset({"SENT", "UNKNOWN", "BLOCKED"})
_RESPONSES = _PENDING_STATES | _RESOLVED_STATES
_SOCKET_NAME = "central-egress.sock"


def egress_ipc_path(root: Path) -> Path:
    root = Path(root)
    folder = root / "s"
    if folder.is_symlink() or (
        folder.exists() and (not folder.is_dir() or folder.stat().st_mode & 0o077)
    ):
        raise PermissionError("Unsafe shared IPC directory")
    if not folder.exists():
        folder.mkdir(mode=0o700)
    if len(os.fsencode(folder / _SOCKET_NAME)) >= 103:
        raise ValueError("Central IPC socket path exceeds macOS limit")
    return folder / _SOCKET_NAME


def _derive(raw: bytes, direction: bytes) -> bytes:
    """Direction-separated keys prevent request/response reflection."""
    return hmac.new(raw, b"max2tg-egress-v1:" + direction, hashlib.sha256).digest()


def _read_private_key(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PermissionError("Missing private IPC key")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("Unsafe IPC key ownership or mode")
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("Invalid IPC key length")
    return key


class GatewayEgressIpcServer:
    """Gateway-only listener which authenticates each tenant request."""

    def __init__(self, registry: CapsuleRegistry, egress: TelegramEgress, *, topic_scoped=False):
        if egress.plane.capsules is not registry:
            raise ValueError("Gateway IPC must share the exact control plane")
        self.registry = registry
        self.egress = egress
        self.topic_scoped=topic_scoped
        self.path = egress_ipc_path(registry.root)
        self._server = None

    async def _receive(self, reader, writer):
        try:
            frame = await asyncio.wait_for(_read_frame(reader), timeout=6)
            # Untrusted outer envelope is inspected only to locate the key.
            envelope = json.loads(frame)
            if type(envelope) is not dict or set(envelope) != {
                "payload", "signature"
            }:
                raise ValueError("Invalid IPC envelope")
            raw = envelope["payload"]
            if type(raw) is not dict:
                raise ValueError("Invalid IPC payload")
            sid = raw.get("session_id")
            self.registry._check_id(sid)
            self.registry.get(sid)
            keypath = ensure_ipc_key(self.registry, sid)
            key = _read_private_key(keypath)
            body = _verify(frame, _derive(key, b"request"))
            if (
                set(body) not in ({"session_id", "nonce", "action", "source_key", "text"},
                                  {"session_id", "nonce", "action", "source_key", "text", "max_chat_id"})
                or body["session_id"] != sid
                or type(body["nonce"]) is not str
                or not _NONCE.fullmatch(body["nonce"])
            ):
                raise ValueError("Invalid signed egress request")
            if self.topic_scoped and body.get('max_chat_id') is None:raise PermissionError('Scoped gateway requires MAX chat')
            if body["action"] == "enqueue":
                state = self.egress.enqueue(
                    sid, body["source_key"], body["text"], max_chat_id=body.get("max_chat_id")
                )
                result = {"status": state}
            elif body["action"] == "status" and body["text"] is None:
                row = self.egress.row(sid, body["source_key"])
                requested=body.get('max_chat_id')
                if requested is not None:
                    requested=self.egress.plane.max_chat_key(requested)
                    if row and row['max_chat_id']!=requested:raise ValueError('Source route mismatch')
                elif row and row['max_chat_id'] is not None:raise ValueError('Scoped status requires source route')
                result = {"status": row["status"] if row else "MISSING"}
                if row and row["status"] == "SENT":
                    result["telegram_id"] = row["telegram_id"]
            else:
                raise ValueError("Invalid egress IPC action")

            response = {
                "session_id": sid,
                "nonce": body["nonce"],
                "result": result,
            }
            await _write_frame(writer, _signed(
                response, _derive(key, b"response")
            ))
        except Exception:
            # A failed authorization receives no signed acknowledgement and
            # never discloses raw text, tokens, keys or private routing data.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def start(self):
        if self.path.exists() or self.path.is_symlink():
            raise RuntimeError("Central egress socket exists; no double owner")
        self._server = await asyncio.start_unix_server(
            self._receive, path=str(self.path)
        )
        os.chmod(self.path, 0o600)

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            if self.path.is_socket():
                self.path.unlink()


class GatewayEgressIpcClient:
    """A worker-owned client with no Telegram bot token or arbitrary chat ID.

    The caller provides only a per-capsule private HMAC file and central
    socket path; the gateway binds it to the registered session identity.
    """

    def __init__(self, sid: str, socket: Path, key_file: Path):
        CapsuleRegistry._check_id(sid)
        self.sid = sid
        self.socket = Path(socket)
        self.key_file = Path(key_file)

    async def _call(self, action: str, source_key: str, text, *, max_chat_id=None) -> dict:
        import secrets
        key = _read_private_key(self.key_file)
        nonce = secrets.token_hex(16)
        request = {
            "session_id": self.sid,
            "nonce": nonce,
            "action": action,
            "source_key": source_key,
            "text": text,
        }
        if max_chat_id is not None:request["max_chat_id"]=max_chat_id
        frame = _signed(request, _derive(key, b"request"))
        # A disconnected socket means the gateway did not receive a request.
        reader, writer = await asyncio.open_unix_connection(str(self.socket))
        try:
            await asyncio.wait_for(_write_frame(writer, frame), timeout=6)
            reply = await asyncio.wait_for(_read_frame(reader), timeout=12)
            response = _verify(reply, _derive(key, b"response"))
            if (type(response) is not dict or
                set(response) != {"session_id", "nonce", "result"} or
                response["session_id"] != self.sid or
                response["nonce"] != nonce or
                type(response["result"]) is not dict):
                raise ValueError("Unauthenticated egress acknowledgement")
            result = response["result"]
            status = result.get("status")
            if status not in _RESPONSES | {"MISSING"}:
                raise ValueError("Unexpected gateway outcome")
            if status == "SENT":
                msg_id = result.get("telegram_id")
                if type(msg_id) is not int or msg_id <= 0:
                    raise ValueError("Missing durable Telegram message ID")
            elif set(result) != {"status"}:
                raise ValueError("Unexpected gateway result fields")
            return result
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def enqueue(self, source_key: str, text: str, *, max_chat_id=None) -> dict:
        return await self._call("enqueue", source_key, text,max_chat_id=max_chat_id)

    async def status(self, source_key: str, *, max_chat_id=None) -> dict:
        return await self._call("status", source_key, None,max_chat_id=max_chat_id)
