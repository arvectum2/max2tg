"""P2.2 authenticated local IPC between one Telegram gateway and capsules.

Protocol intentionally transports only one update per request and returns
an explicit handler acknowledgement. A lost response is ambiguous and must
not be retried blindly. No network port is opened.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import struct

from app.telegram_inbox import MAX_UPDATE_BYTES,WorkerUnavailable,identity
from app.capsule_registry import CapsuleRegistry

FRAME_MAX=MAX_UPDATE_BYTES+4096


def ipc_socket_path(registry:CapsuleRegistry,sid:str) -> Path:
    """macOS AF_UNIX paths are short; put sockets in private root/s/."""
    sid=registry._check_id(sid)
    folder=registry.root/"s"
    if not folder.exists():
        folder.mkdir(mode=0o700)
    if folder.is_symlink() or folder.stat().st_mode & 0o077:
        raise PermissionError("Unsafe IPC socket directory")
    result=folder/f"{sid}.sock"
    if len(os.fsencode(result)) >= 103:
        raise ValueError("Unix socket path exceeds conservative macOS limit")
    return result


def ipc_key_path(registry:CapsuleRegistry,sid:str) -> Path:
    return registry.capsule_dir(sid)/"telegram-ipc.key"


def ensure_ipc_key(registry:CapsuleRegistry,sid:str) -> Path:
    """Create a per-capsule HMAC key atomically; never overwrite an old key."""
    registry.get(sid)
    dest=ipc_key_path(registry,sid)
    if dest.is_symlink():
        raise ValueError("Symlinked IPC secret")
    if not dest.exists():
        try:
            fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd,"wb") as output:
                output.write(secrets.token_bytes(32))
                output.flush()
                os.fsync(output.fileno())
    if dest.is_symlink() or (dest.stat().st_mode & 0o077):
        raise PermissionError("IPC key permissions unsafe")
    if len(dest.read_bytes())!=32:
        raise ValueError("Invalid IPC key")
    return dest


def _signed(body:dict,key:bytes) -> bytes:
    data=json.dumps(body,sort_keys=True,separators=(",",":"),
                    ensure_ascii=False).encode()
    signature=hmac.new(key,data,hashlib.sha256).hexdigest()
    return json.dumps({"payload":body,"signature":signature},
                      ensure_ascii=False,separators=(",",":")).encode()


def _verify(raw:bytes,key:bytes) -> dict:
    obj=json.loads(raw)
    data=obj["payload"]
    signed=_signed(data,key)
    expected=json.loads(signed)["signature"]
    if not hmac.compare_digest(expected,obj["signature"]):
        raise ValueError("Invalid IPC signature")
    if not isinstance(data,dict):
        raise ValueError("Bad IPC payload")
    return data


async def _read_frame(reader:asyncio.StreamReader) -> bytes:
    size=struct.unpack(">I",await reader.readexactly(4))[0]
    if size<1 or size>FRAME_MAX:
        raise ValueError("Invalid IPC frame length")
    return await reader.readexactly(size)


async def _write_frame(writer:asyncio.StreamWriter,payload:bytes):
    if not 0<len(payload)<=FRAME_MAX:
        raise ValueError("IPC payload too large")
    writer.write(struct.pack(">I",len(payload))+payload)
    await writer.drain()


class CapsuleIpcClient:
    def __init__(self,registry:CapsuleRegistry):
        self.registry=registry

    async def deliver(self,sid:str,update:dict) -> bool:
        self.registry.get(sid)
        socket_path=ipc_socket_path(self.registry,sid)
        key=ensure_ipc_key(self.registry,sid).read_bytes()
        data=_signed({"session_id":sid,"update":update},key)
        # If we cannot connect, worker definitely has not received a byte.
        try:
            reader,writer=await asyncio.open_unix_connection(str(socket_path))
        except (OSError,ConnectionError) as exc:
            raise WorkerUnavailable() from exc
        try:
            await _write_frame(writer,data)
            response=_verify(await asyncio.wait_for(_read_frame(reader),15),key)
            if (response.get("update_id")!=update.get("update_id")
                or response.get("session_id")!=sid):
                raise ValueError("Unexpected IPC response")
            return response.get("ack") is True
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


class CapsuleIpcServer:
    def __init__(self,registry:CapsuleRegistry,sid:str,handler):
        self.registry=registry
        self.sid=registry._check_id(sid)
        self.handler=handler
        self.path=ipc_socket_path(registry,sid)
        self.key=ensure_ipc_key(registry,sid).read_bytes()
        self._server=None

    async def _receive(self,reader,writer):
        try:
            request=_verify(await _read_frame(reader),self.key)
            if request.get("session_id")!=self.sid:
                raise PermissionError("Other capsule ID")
            update=request.get("update")
            if not isinstance(update,dict) or type(update.get("update_id")) is not int:
                raise ValueError("Missing Telegram update ID")
            # Trust boundary #2: enforce this worker's own Telegram ACL.
            # A signed event for somebody else's forum is still forbidden.
            chat_id,actor_id=identity(update)
            data=self.registry.credentials(self.sid)
            permitted={int(data["TG_ADMIN_USER_ID"])}
            permitted.update(int(x.strip()) for x in
                data.get("TG_ALLOWED_USER_IDS","").split(",")
                if x.strip().isdigit())
            if data.get("TG_ALLOWED_USER_ID","").isdigit():
                permitted.add(int(data["TG_ALLOWED_USER_ID"]))
            if (chat_id!=int(data["TG_CHAT_ID"]) or actor_id not in permitted):
                raise PermissionError("Foreign Telegram tenant event")
            result=await self.handler(update)
            response={"session_id":self.sid,
                      "update_id":update["update_id"],
                      "ack":result is True}
            await _write_frame(writer,_signed(response,self.key))
        except Exception:
            # No success ack on an unknown execution outcome.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def start(self):
        if self.path.exists() or self.path.is_symlink():
            raise RuntimeError("Existing IPC socket; do not steal another worker")
        self._server=await asyncio.start_unix_server(
            self._receive,path=str(self.path))
        os.chmod(self.path,0o600)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server=None
            if self.path.is_socket():
                self.path.unlink()
