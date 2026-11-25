import asyncio
import json
import logging
import os
import random
import time
from urllib.parse import urljoin, urlsplit, quote
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import aiohttp
from yarl import URL

from app.max_media import media_filename
from app.max_wire import encode, decode, profile_contact

log = logging.getLogger(__name__)

DEBUG_DIR = "debug"
MAX_MEDIA_BYTES = 50 * 1024 * 1024  # Telegram Bot API file upload ceiling
MEDIA_HOST_SUFFIXES = ("oneme.ru", "max.ru", "vk.com", "vk-cdn.net",
                       "userapi.com", "okcdn.ru")


def _log_task_exception(task: "asyncio.Task") -> None:
    """Done-callback that logs any exception raised by a fire-and-forget task."""
    if not task.cancelled() and task.exception() is not None:
        log.exception("Unhandled exception in background task", exc_info=task.exception())

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_BROWSER_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": '"Chromium";v="131", "Google Chrome";v="131", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

_WS_HEADERS = {
    **_BROWSER_HEADERS,
    "Origin": "https://web.max.ru",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_HTTP_HEADERS = {
    **_BROWSER_HEADERS,
    "Origin": "https://web.max.ru",
    "Referer": "https://web.max.ru/",
    "Accept": "*/*",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    # Override the browser default to avoid brotli/zstd: aiohttp ships without
    # these codecs by default, and the MAX upload server returns its JSON body
    # brotli-encoded when "br" is advertised.
    "Accept-Encoding": "gzip, deflate",
}


class OpCode(IntEnum):
    HEARTBEAT_PING = 1
    HANDSHAKE = 6
    AUTH_SNAPSHOT = 19
    LOGOUT = 20
    STICKER_STORE = 27
    ASSET_GET = 28
    FAVORITE_STICKER = 29
    CONTACT_GET = 32
    CONTACT_PRESENCE = 35
    CHAT_GET = 48
    CHAT_HISTORY = 49
    CHAT_OPEN_LINK = 57
    CHAT_LEAVE = 58
    GLOBAL_SEARCH = 60
    SEND_MESSAGE = 64
    ATTACH_TYPING = 65        # "I'm uploading <type> in this chat"
    EDIT_MESSAGE = 67
    # Deletion opcode 66 intentionally NOT exposed for sends: known cascade risk.
    REACTION_SET = 178
    REACTION_REMOVE = 179
    DELETION_PUSH = 142
    REACTION_PUSH = 156
    REACTION_PUSH_ALTERNATE = 155
    VIDEO_PLAY = 83           # verified web.max.ru request schema 2026-11-24
    PHOTO_UPLOAD_URL = 80     # get URL for photo upload
    VIDEO_UPLOAD_URL = 82    # verified native video/audio upload
    AUDIO_UPLOAD_URL = 86     # legacy constant; not used for audio uploads
    FILE_UPLOAD_URL = 87      # get URL for file upload
    FILE_DOWNLOAD_URL = 88    # verified web.max.ru request schema
    AUDIO_PLAY = 301          # verified web.max.ru request schema
    DISPATCH = 128
    UPLOAD_READY = 136        # server says an uploaded file/video is processed


@dataclass
class MaxMessage:
    chat_id: Any = None
    sender_id: Any = None
    text: str = ""
    timestamp: Any = None
    message_id: str = ""
    is_self: bool = False
    attaches: list = field(default_factory=list)
    link: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


class MaxClient:
    WS_URL = "wss://api.oneme.ru/websocket"
    APP_VERSION = "26.11.9"
    HEARTBEAT_SEC = 30
    RECONNECT_SEC = 5
    def __init__(self, token: str, device_id: str, chat_ids: str | None = None, debug: bool = False, *, session_factory=None):
        self._session_factory = session_factory
        self.token = token
        self.device_id = device_id
        self.chat_ids: list[int] = []
        self.debug = debug
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._seq = 0
        self._binary_transport = False  # switched on by the live connection loop
        self._my_id = None
        self._authorized = False
        self._auth_required = False
        self._last_rx_monotonic = None
        self._on_ready_cb = None
        self._on_message_cb = None
        self._on_mutation_cb = None
        self._pending_previews = {}
        self._active_previews = set()
        self._telegram_outbound = {}  # cid -> Telegram-originated pending send
        self._heartbeat_task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._dispatch_counter = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._file_pending: dict[int, asyncio.Future] = {}
        self._video_pending: dict[int, asyncio.Future] = {}
        self._audio_pending: dict[int, asyncio.Future] = {}
        self._on_disconnect_cb = None
        if chat_ids:
            self.chat_ids += map(int, map(str.strip, chat_ids.split(',')))

    def _new_session(self, **kwargs):
        factory = getattr(self, '_session_factory', None) or aiohttp.ClientSession
        return factory(**kwargs)

    # ── decorator API ──────────────────────────────────────────────

    def on_ready(self, func):
        self._on_ready_cb = func
        return func

    def on_message(self, func):
        self._on_message_cb = func
        return func

    def on_mutation(self, func):
        self._on_mutation_cb = func
        return func

    def on_disconnect(self, func):
        self._on_disconnect_cb = func
        return func

    # ── transport ──────────────────────────────────────────────────

    async def _send(self, opcode: int, payload: dict, *, pending=None) -> int:
        if not self._ws or self._ws.closed:
            return -1
        seq = self._seq
        if self._binary_transport and seq >= 32767:
            # The web protocol uses signed 16-bit sequence numbers. Reconnect
            # before exhausting the sequence space; do not reuse pending IDs.
            await self._ws.close()
            return -1
        pkt = {
            "ver": 11,
            "cmd": 0,
            "seq": seq,
            "opcode": opcode,
            "payload": payload,
        }
        self._seq += 1
        raw = encode(pkt) if self._binary_transport else json.dumps(pkt, ensure_ascii=False)
        log.debug(">>> SEND op=%d seq=%d bytes=%d", opcode, seq, len(raw))
        # Register before awaiting the transport: an immediate response can
        # arrive before send_str() returns, especially on faster runtimes.
        if pending is not None:
            self._pending[seq] = pending
        try:
            if self._binary_transport:
                await self._ws.send_bytes(raw)
            else:
                await self._ws.send_str(raw)
        except BaseException:
            if self._pending.get(seq) is pending:
                self._pending.pop(seq, None)
            raise
        return seq

    async def cmd(
        self, opcode: int, payload: dict, timeout: float = 10,
        *, none_on_timeout: bool = False,
    ) -> dict | None:
        """Send a request and wait for the response (cmd=1 with same seq).

        By default timeouts keep the legacy empty-dict result. Callers that
        need to distinguish a valid empty MAX response from a timeout can set
        none_on_timeout=True.
        """
        if self._ws is None or self._ws.closed:
            log.warning("MAX RPC unavailable while WebSocket is disconnected: op=%d", opcode)
            return None if none_on_timeout else {}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        seq = await self._send(opcode, payload, pending=fut)
        if seq < 0:
            return None if none_on_timeout else {}
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result if result is not None else (None if none_on_timeout else {})
        except asyncio.TimeoutError:
            log.warning("cmd timeout: op=%d seq=%d", opcode, seq)
            return None if none_on_timeout else {}
        finally:
            # A reconnect can reuse seq with a different future.
            if self._pending.get(seq) is fut:
                self._pending.pop(seq, None)

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(self.HEARTBEAT_SEC)
            try:
                if self._ws and not self._ws.closed:
                    await self._send(OpCode.HEARTBEAT_PING, {"interactive": False})
                else:
                    break
            except Exception:
                log.exception("Heartbeat error, stopping heartbeat loop")
                break

    # ── main loop ──────────────────────────────────────────────────

    async def run(self):
        if self.debug:
            os.makedirs(DEBUG_DIR, exist_ok=True)

        self._binary_transport = True
        async with self._new_session(
            headers=_BROWSER_HEADERS, timeout=aiohttp.ClientTimeout(total=60, connect=10, sock_connect=10)
        ) as session:
            self._session = session
            while True:
                try:
                    log.info("Connecting to %s ...", self.WS_URL)
                    async with session.ws_connect(
                        self.WS_URL, headers=_WS_HEADERS, max_msg_size=32*1024*1024
                    ) as ws:
                        self._ws = ws
                        self._authorized = False
                        self._last_rx_monotonic = time.monotonic()
                        self._seq = 0
                        self._pending.clear()

                        log.info("Connected. Sending handshake...")
                        await self._send(
                            OpCode.HANDSHAKE,
                            {
                                "deviceId": self.device_id,
                                "clientSessionId": int(time.time() * 1000),
                                "userAgent": {
                                    "deviceType": "WEB",
                                    "deviceName": "Google Chrome 131.0.0.0",
                                    "headerUserAgent": _USER_AGENT,
                                    "osVersion": "Windows",
                                    "appVersion": self.APP_VERSION,
                                    "locale": "ru", "deviceLocale": "ru",
                                    "pushDeviceType": "WEBPUSH",
                                },
                            },
                        )

                        self._heartbeat_task = asyncio.create_task(
                            self._heartbeat_loop()
                        )

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                await self._handle(decode(msg.data))
                            elif msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle(json.loads(msg.data))
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                log.warning("WebSocket closed/error: %s", msg.type)
                                break

                except Exception:
                    log.exception("Connection error")

                finally:
                    if self._heartbeat_task:
                        self._heartbeat_task.cancel()
                    self._authorized = False
                    self._ws = None
                    for fut in self._pending.values():
                        if not fut.done():
                            # Disconnect is an uncertain RPC outcome, NOT task
                            # cancellation (which could kill Telegram intake).
                            fut.set_result(None)
                    self._pending.clear()
                    for fut in self._file_pending.values():
                        if not fut.done():
                            fut.set_result(None)
                    self._file_pending.clear()
                    for fut in self._video_pending.values():
                        if not fut.done():fut.set_result(None)
                    self._video_pending.clear()
                    for fut in self._audio_pending.values():
                        if not fut.done():fut.set_result(None)
                    self._audio_pending.clear()

                if self._on_disconnect_cb:
                    try:
                        await self._on_disconnect_cb()
                    except Exception:
                        log.exception("on_disconnect callback error")

                log.info("Reconnecting in %ds...", self.RECONNECT_SEC)
                await asyncio.sleep(self.RECONNECT_SEC)

    # ── event dispatcher ───────────────────────────────────────────

    async def _handle(self, data: dict):
        self._last_rx_monotonic = time.monotonic()
        op = data.get("opcode")
        cmd = data.get("cmd")
        seq = data.get("seq")
        payload = data.get("payload", {})

        # cmd=1 is a response to our request — resolve the pending future
        if cmd == 1 and seq in self._pending:
            fut = self._pending.pop(seq)
            if not fut.done():
                fut.set_result(payload)
            if op not in (OpCode.HANDSHAKE, OpCode.AUTH_SNAPSHOT):
                log.debug("<<< RESP  op=%-4s seq=%s", op, seq)

        # cmd=3 is an error response
        elif cmd == 3 and seq in self._pending:
            fut = self._pending.pop(seq)
            if not fut.done():
                fut.set_result({"_max_error": payload})
            log.warning("<<< ERROR op=%-4s seq=%s | %s", op, seq, payload)

        # server-initiated events — not a reply to one of our requests
        else:
            payload_preview = json.dumps(payload, ensure_ascii=False, default=str)
            if len(payload_preview) > 3000:
                payload_preview = payload_preview[:3000] + "…"

            if op == OpCode.HANDSHAKE and cmd == 1:
                log.info("Handshake OK → sending auth token...")
                await self._send(
                    OpCode.AUTH_SNAPSHOT,
                    {
                        "chatsCount": 10,
                        "interactive": True,
                        "token": self.token,
                    },
                )

            elif op == OpCode.AUTH_SNAPSHOT and cmd == 1:
                viewer = profile_contact(payload).get("id")
                if type(viewer) is not int or viewer <= 0:
                    self._authorized = False
                    log.error("MAX auth response has no valid viewer; refusing unsafe readiness")
                    if self._ws is not None:
                        await self._ws.close()
                    return
                self._my_id = viewer
                self._authorized = True
                self._auth_required = False
                log.info("Authorized! my_id=%s", self._my_id)
                if self.debug:
                    self._dump_json("snapshot.json", payload)

                if self._on_ready_cb:
                    # Do not await user callbacks inside the WebSocket receive
                    # loop: ready handlers may issue RPCs (for example CONTACT_GET),
                    # whose responses can only be consumed by this same loop.
                    task = asyncio.create_task(self._on_ready_cb(payload))
                    task.add_done_callback(_log_task_exception)

            elif op == OpCode.AUTH_SNAPSHOT and cmd == 3:
                self._authorized = False
                self._auth_required = True
                log.warning("MAX authentication rejected; interactive re-login required")

            elif op == OpCode.LOGOUT and cmd == 0:
                self._authorized = False
                self._auth_required = True
                log.warning("MAX logout event received; interactive re-login required")

            elif op == OpCode.DISPATCH:
                body = payload.get("message") if isinstance(payload,dict) else None
                if isinstance(body,dict) and (
                    body.get("status")=="EDITED" or body.get("updateTime")
                ):
                    if self._on_mutation_cb:
                        task = asyncio.create_task(self._on_mutation_cb(int(op),payload))
                        task.add_done_callback(_log_task_exception)
                    return
                self._dispatch_counter += 1
                if self.debug and self._dispatch_counter <= 20:
                    self._dump_json(
                        f"dispatch_{self._dispatch_counter:04d}.json", payload
                    )

                if self._on_message_cb:
                    msg = self._parse_message(payload)
                    if msg is not None and ((not self.chat_ids) or (msg.chat_id in self.chat_ids)):
                        if msg.chat_id is not None and msg.message_id:
                            key = (str(msg.chat_id), msg.message_id)
                            self._pending_previews[key] = msg
                            if key in self._active_previews:
                                task = None
                            else:
                                self._active_previews.add(key)
                                task = asyncio.create_task(self._deliver_preview(key))
                        else:
                            task = asyncio.create_task(self._on_message_cb(msg))
                        if task is not None:
                            task.add_done_callback(_log_task_exception)

            elif op in (OpCode.DELETION_PUSH, OpCode.REACTION_PUSH,
                        OpCode.REACTION_PUSH_ALTERNATE) and cmd == 0:
                if self._on_mutation_cb:
                    task = asyncio.create_task(self._on_mutation_cb(int(op),payload))
                    task.add_done_callback(_log_task_exception)

            elif op == OpCode.UPLOAD_READY:
                # Server confirms an uploaded file/video finished server-side processing.
                for field, pending in (("fileId",self._file_pending),("videoId",self._video_pending),("audioId",self._audio_pending)):
                    ident=payload.get(field)
                    if ident is not None:
                        try:fut=pending.pop(int(ident),None)
                        except (ValueError,TypeError):continue
                        if fut is not None and not fut.done():
                            fut.set_result(None if payload.get("error") else payload)
                log.debug("UPLOAD_READY op=136 fields=%s",list(payload))

            elif op in (OpCode.HEARTBEAT_PING,):
                log.debug("Heartbeat op=%s", op)

            elif cmd not in (1, 3):
                log.info("<<< EVENT op=%s cmd=%s keys=%s",
                         op, cmd, list(payload)[:12] if isinstance(payload, dict) else [])
                log.debug("<<< EVENT payload=%s", payload_preview[:500])

    async def _deliver_preview(self, key):
        """Merge rapid MAX updates sharing one message ID before forwarding."""
        try:
            await asyncio.sleep(0.15)
            while key in self._pending_previews:
                latest = self._pending_previews.pop(key)
                await self._on_message_cb(latest)
        finally:
            self._active_previews.discard(key)

    def _prune_telegram_outbound(self):
        now = time.monotonic()
        for cid, rec in list(self._telegram_outbound.items()):
            if now - rec["created"] > 60:
                self._telegram_outbound.pop(cid, None)

    async def telegram_echo_tg_id(self, msg: MaxMessage) -> int | None:
        """Suppress only verified TG->MAX echoes, including pre-ACK DISPATCH."""
        if not msg.message_id:
            return None
        self._prune_telegram_outbound()
        raw = msg.raw.get("message") or {}
        observed_cid = str(raw["cid"]) if raw.get("cid") is not None else None
        has_media = any(
            isinstance(a, dict) and a.get("_type") not in
            ("SHARE", "CONTROL", "WIDGET", "INLINE_KEYBOARD")
            for a in msg.attaches
        )
        for rec in list(self._telegram_outbound.values()):
            if rec["chat"] != str(msg.chat_id):
                continue
            matched_id = rec["max_id"] == msg.message_id
            matched_cid = observed_cid is not None and observed_cid == rec["cid"]
            pending = (not rec["done"].is_set() and
                       rec["text"] == (msg.text or "") and rec["media"] == has_media)
            if not (matched_id or matched_cid or pending):
                continue
            if pending and not (matched_id or matched_cid):
                try:
                    await asyncio.wait_for(rec["done"].wait(), timeout=11)
                except asyncio.TimeoutError:
                    return None
            if rec["max_id"] == msg.message_id or (
                observed_cid is not None and rec["cid"] == observed_cid
            ):
                return rec["tg_id"]
        return None

    # ── WebSocket RPC: fetch contacts ──────────────────────────────

    async def fetch_contacts(self, contact_ids: list[int]) -> dict:
        """Fetch contact info via WS opcode 32. Returns raw response payload."""
        if not contact_ids:
            return {}
        resp = await self.cmd(OpCode.CONTACT_GET, {"contactIds": contact_ids})
        if self.debug:
            self._dump_json("contacts_response.json", resp)
        log.info("fetch_contacts(%s) → keys: %s", contact_ids, list(resp.keys()))
        return resp

    async def send_message(self, chat_id, text: str = "", elements=None,
                            attaches=None, link: dict | None = None,
                            telegram_message_id: int | None = None) -> dict:
        """Send a message to a Max chat. Returns the server response.

        Both ``elements`` (text formatting) and ``attaches`` (photos, files,
        voice, ...) are optional. Pass an empty ``text`` together with an
        attach to send a media-only message.
        """
        if elements is None:
            elements = []
        if attaches is None:
            attaches = []
        cid = int(time.time() * 1000) * 1000 + random.randint(0, 999)
        message = {"text": text, "cid": cid, "elements": elements}
        if attaches:
            message["attaches"] = attaches
        if link:
            message["link"] = link
        record = None
        if telegram_message_id is not None:
            self._prune_telegram_outbound()
            record = {
                "chat": str(chat_id), "text": text or "",
                "media": bool(attaches), "cid": str(cid),
                "max_id": None, "tg_id": int(telegram_message_id),
                "created": time.monotonic(), "done": asyncio.Event(),
            }
            self._telegram_outbound[cid] = record
        resp = None
        try:
            resp = await self.cmd(
                OpCode.SEND_MESSAGE,
                {"chatId": chat_id, "message": message, "notify": True},
            )
        finally:
            if record is not None:
                body = (resp or {}).get("message") or {}
                if isinstance(body, dict) and body.get("id") is not None:
                    record["max_id"] = str(body["id"])
                record["done"].set()
        ok = bool(resp) and "_max_error" not in resp
        log.info("send_message(chat=%s, attaches=%d) → %s",
                 chat_id, len(attaches), "OK" if ok else "FAIL")
        return resp

    async def edit_message(self, chat_id, max_message_id, text: str,
                           elements: list | None = None) -> dict | None:
        """Edit owned MAX text (no delete opcodes, no automatic retry)."""
        if not max_message_id or not isinstance(text,str):
            return None
        result = await self.cmd(OpCode.EDIT_MESSAGE, {
            "chatId":chat_id, "messageId":str(max_message_id),
            "text":text, "elements":elements if elements is not None else [],
            "attachments":[],
        },none_on_timeout=True)
        if not isinstance(result,dict) or result.get("_max_error"):
            return result
        msg=result.get("message") or {}
        if str(msg.get("id"))!=str(max_message_id):
            return None
        return result

    async def react_to_message(self, chat_id, max_message_id,
                               emoji: str | None) -> dict | None:
        """Only explicit reaction emojis; remove an existing reaction with None."""
        if not str(max_message_id).isdigit():
            return None
        fields={"chatId":chat_id,"messageId":int(max_message_id)}
        if emoji:
            fields["reaction"]={"reactionType":"EMOJI","id":emoji}
            op=OpCode.REACTION_SET
        else:
            op=OpCode.REACTION_REMOVE
        result=await self.cmd(op,fields,none_on_timeout=True)
        if result is None or (isinstance(result,dict) and result.get("_max_error")):
            return None
        return result

    async def fetch_chats(self, chat_ids: list) -> dict | None:
        """Fetch current metadata for one or more MAX chats via opcode 48."""
        ids = list(dict.fromkeys(chat_ids))
        if not ids:
            return {"chats": []}
        resp = await self.cmd(
            OpCode.CHAT_GET,
            {"chatIds": ids},
            none_on_timeout=True,
        )
        log.info(
            "fetch_chats(%s) → %s chats",
            ids,
            len((resp or {}).get("chats") or []) if isinstance(resp, dict) else 0,
        )
        return resp

    async def fetch_history(self, chat_id, count: int = 20,
                            from_time: int | None = None) -> dict | None:
        """Fetch the newest MAX history window via opcode 49.

        ``from_time`` is a millisecond timestamp marker. Current time returns
        the newest messages; the Personal UX limits one import to 100.
        """
        count = max(1, min(int(count), 100))
        if from_time is None:
            from_time = int(time.time() * 1000)
        resp = await self.cmd(
            OpCode.CHAT_HISTORY,
            {
                "chatId": chat_id,
                "from": int(from_time),
                "forward": 0,
                "backward": count,
                "getMessages": True,
            },
            none_on_timeout=True,
        )
        log.info(
            "fetch_history(chat=%s, count=%s) → %s messages",
            chat_id,
            count,
            len((resp or {}).get("messages") or []) if isinstance(resp, dict) else 0,
        )
        return resp

    async def search_global(self, query: str, count: int = 20) -> dict | None:
        """Search MAX globally for people, groups and channels."""
        query = query.strip()
        if not query:
            return {"result": [], "total": 0}
        resp = await self.cmd(
            OpCode.GLOBAL_SEARCH,
            {"query": query, "count": count, "type": "ALL"},
            none_on_timeout=True,
        )
        log.info(
            "search_global(%r) → %s results",
            query,
            len((resp or {}).get("result") or []) if isinstance(resp, dict) else 0,
        )
        return resp

    def dialog_chat_id(self, contact_id: int) -> int:
        """Return MAX's deterministic DIALOG id: viewer_id XOR contact_id."""
        if self._my_id is None:
            raise RuntimeError("MAX viewer id is not available yet")
        return int(self._my_id) ^ int(contact_id)

    async def leave_chat(self, chat_id) -> dict | None:
        """Leave a MAX group/channel via opcode 58.

        MAX may confirm a successful leave with an empty payload {}.
        None is reserved for an actual RPC timeout.
        """
        resp = await self.cmd(
            OpCode.CHAT_LEAVE, {"chatId": chat_id}, none_on_timeout=True,
        )
        ok = resp is not None and "_max_error" not in resp
        log.info("leave_chat(chat=%s) → %s", chat_id, "OK" if ok else "FAIL")
        return resp

    # ── media upload ───────────────────────────────────────────────

    async def upload_photo(self, data: bytes, chat_id=None,
                            filename: str = "image.jpg",
                            mimetype: str = "image/jpeg") -> dict | None:
        """Upload a photo and return an attach dict for send_message.

        Protocol (reverse-engineered, see nsdkinx/vkmax):
          1. opcode 80 → response.payload.url
          2. (optional) opcode 65 → 'I'm uploading PHOTO' indicator
          3. multipart POST file to URL → JSON {'photos': {key: {'token': ...}}}
          4. token → {"_type": "PHOTO", "photoToken": token}
        """
        resp = await self.cmd(OpCode.PHOTO_UPLOAD_URL, {"count": 1})
        if not resp or "_max_error" in resp:
            log.error("Photo upload URL request failed: %s", resp)
            return None
        url = resp.get("url")
        if not url:
            log.error("Photo upload response missing 'url': %s", resp)
            return None

        if chat_id is not None:
            await self.cmd(OpCode.ATTACH_TYPING,
                           {"chatId": chat_id, "type": "PHOTO"})

        body = await self._http_upload(url, data, filename, mimetype,
                                        expect_json=True)
        if not isinstance(body, dict):
            return None

        photos = body.get("photos") or {}
        if not photos:
            log.error("Photo upload OK but no 'photos' in response: %s", body)
            return None
        first = next(iter(photos.values()))
        token = first.get("token") if isinstance(first, dict) else None
        if not token:
            log.error("Photo upload response missing token: %s", body)
            return None
        return {"_type": "PHOTO", "photoToken": token}

    async def open_by_link(self, link: str) -> dict | None:
        """Resolve a max.ru invite link via opcode 57.

        Works for ``/join/<token>`` (group / channel invite — chat namespace).
        ``/u/<token>`` (user share link) needs a different, undocumented
        opcode that we haven't reverse-engineered yet — server returns
        ``not.found`` for chat-namespace lookup.
        """
        resp = await self.cmd(
            OpCode.CHAT_OPEN_LINK, {"link": link}, none_on_timeout=True,
        )
        log.info("open_by_link(%s) → %s",
                 link[:60], str(resp)[:300] if resp else resp)
        return resp

    async def download_document_url(self, file_id, chat_id, message_id) -> str | None:
        """Resolve an ordinary file; do not bypass MAX's unsafe-file flag."""
        if file_id is None or chat_id is None or not message_id:
            return None
        from app.max_wire import normalize_ids
        try:
            payload=normalize_ids(dict(fileId=file_id,chatId=chat_id,messageId=message_id,itemType="REGULAR"))
        except (TypeError,ValueError):
            return None
        result=await self.cmd(OpCode.FILE_DOWNLOAD_URL,payload,none_on_timeout=True)
        if not isinstance(result,dict) or result.get('_max_error') or result.get('unsafe') is not False:
            return None
        url=result.get('url')
        return url if self._safe_media_url(url) else None

    async def download_audio_url(self, audio_id, chat_id, message_id,
                                  token: str | None = None) -> str | None:
        """Use the web client's AUDIO_PLAY RPC, not guessed CDN paths."""
        if audio_id is None or chat_id is None or not message_id:
            return None
        from app.max_wire import normalize_ids
        try:
            payload=normalize_ids(dict(audioId=audio_id,chatId=chat_id,messageId=message_id,token=token or ''))
        except (TypeError,ValueError):
            return None
        result=await self.cmd(OpCode.AUDIO_PLAY,payload,none_on_timeout=True)
        if not isinstance(result,dict) or result.get('_max_error'):
            return None
        for key in ('opus','m4a','mp3'):
            url=result.get(key)
            if self._safe_media_url(url):return url
        return None

    async def _probe_audio_url(self, url: str) -> bool:
        """HEAD-probe a candidate URL — accept if HTTP 200 with non-HTML body."""
        session = getattr(self, "_session", None)
        close_after = False
        if session is None or session.closed:
            session = self._new_session(headers=_BROWSER_HEADERS)
            close_after = True
        try:
            async with session.get(
                url, headers=_HTTP_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                ct = resp.headers.get("Content-Type", "")
                log.info("probe %s → HTTP %d, Content-Type=%s",
                          url[:80], resp.status, ct)
                if resp.status != 200:
                    return False
                # Reject obviously-HTML responses (error/redirect pages).
                if "text/html" in ct.lower():
                    return False
                return True
        except Exception:
            log.exception("probe error for %s", url[:80])
            return False
        finally:
            if close_after:
                await session.close()

    async def _native_upload_slot(self, *, voice=False, uploader_type=1):
        payload={"count":1}
        if voice:payload.update(type=2,uploaderType=uploader_type)
        result=await self.cmd(OpCode.VIDEO_UPLOAD_URL,payload,none_on_timeout=True)
        if not isinstance(result,dict) or result.get('_max_error'):return None
        if voice and result.get('uploaderType',uploader_type)!=uploader_type:return None
        info=result.get('info')
        if not isinstance(info,list) or len(info)!=1 or not isinstance(info[0],dict):return None
        item=info[0];url=item.get('url');token=item.get('token')
        try:ident=int(item.get('videoId'))
        except (TypeError,ValueError):return None
        if not 0<ident<2**63 or not isinstance(token,str) or not token or not self._safe_media_url(url):return None
        return ident,token,url

    async def _upload_native_bytes(self,url,data,filename,mimetype):
        """One bounded raw POST, no redirects or ambiguous retries."""
        if not self._safe_media_url(url) or not 0<len(data)<=25*1024*1024:return False
        session=self._session;close_after=session is None or session.closed
        if close_after:session=self._new_session(headers=_BROWSER_HEADERS)
        headers={**_HTTP_HEADERS,'Content-Type':mimetype,
                 'Content-Disposition':'attachment; filename='+quote(media_filename({'name':filename}),safe=''),
                 'Content-Range':f'bytes 0-{len(data)-1}/{len(data)}'}
        try:
            async with session.post(URL(url,encoded=True),data=data,headers=headers,allow_redirects=False,timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status not in (200,201):
                    log.warning('Native upload rejected: HTTP %d',resp.status);return False
                last=resp.headers.get('X-Last-Known-Byte')
                if last is None and resp.headers.get('Range'):
                    import re
                    match=re.fullmatch(r'(?:bytes[= ])?0-(\d+)(?:/\d+)?',resp.headers['Range'])
                    if match is None:return False
                    last=match[1]
                if (last is None and resp.status==201) or (last is not None and int(last)+1!=len(data)):return False
                body=bytearray()
                async for chunk in resp.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body)>65536:return False
                if body.strip():
                    try:result=json.loads(body)
                    except (ValueError,UnicodeError):
                        # Observed MAX video uploader returns HTTP 200 with a
                        # non-JSON body and a full-byte receipt. Require both;
                        # upload_video still requires matching processing ACK.
                        return mimetype in ('video/mp4','audio/mp4') and resp.status == 200 and last is not None and int(last)+1 == len(data)
                    if not isinstance(result,dict) or 'error_code' in result or result.get('error'):return False
                return True
        except Exception as exc:
            log.warning('Native upload stopped: %s',type(exc).__name__);return False
        finally:
            if close_after:await session.close()

    async def upload_audio(self, data: bytes, chat_id=None,
                           filename: str = "voice.ogg", mimetype: str = "audio/ogg",
                           timeout: float = 60.0) -> dict | None:
        # The live test confirmed the server-processing route, not direct OGG.
        return await self.upload_processed_audio(data,timeout=timeout,uploader_type=0)

    async def upload_processed_audio(self,data:bytes,timeout=25.,uploader_type=0):
        from app.native_media import prepare_processed_voice
        prepared=await prepare_processed_voice(data)
        if prepared is None:return None
        audio,duration=prepared
        slot=await self._native_upload_slot(voice=True,uploader_type=uploader_type)
        if slot is None:return None
        ident,token,url=slot
        if ident in self._audio_pending:return None
        future=asyncio.get_running_loop().create_future();self._audio_pending[ident]=future
        try:
            if not await self._upload_native_bytes(url,audio,'voice.m4a','audio/mp4'):return None
            ack=await asyncio.wait_for(future,timeout)
            if not isinstance(ack,dict) or ack.get('error') or str(ack.get('audioId'))!=str(ident):return None
            return {'_type':'AUDIO','audioId':ident,'token':token,'duration':duration,'wave':[]}
        except asyncio.TimeoutError:return None
        finally:
            if self._audio_pending.get(ident) is future:self._audio_pending.pop(ident,None)
            if not future.done():future.cancel()

    async def upload_video(self,data:bytes,chat_id=None,filename='video.mp4',mimetype='video/mp4',timeout=120.0):
        from app.native_media import validate_video
        if not await validate_video(data):return None
        slot=await self._native_upload_slot()
        if slot is None:return None
        ident,token,url=slot
        if ident in self._video_pending:return None
        future=asyncio.get_running_loop().create_future();self._video_pending[ident]=future
        try:
            if not await self._upload_native_bytes(url,data,filename,'video/mp4'):return None
            ack=await asyncio.wait_for(future,timeout)
            if not isinstance(ack,dict) or ack.get('error') or str(ack.get('videoId'))!=str(ident):return None
            return {'_type':'VIDEO','videoId':ident,'token':token}
        except asyncio.TimeoutError:return None
        finally:
            if self._video_pending.get(ident) is future:self._video_pending.pop(ident,None)
            if not future.done():future.cancel()

    async def upload_file(self, data: bytes, chat_id=None,
                           filename: str = "file.bin",
                           mimetype: str = "application/octet-stream",
                           attach_type: str = "FILE",
                           timeout: float = 60.0) -> dict | None:
        """Upload a generic file (used for voice, audio, documents, video).

        Unlike photos, the server processes the file asynchronously and only
        then sends an opcode-136 UPLOAD_READY event. We wait for that event
        before returning the attach dict — otherwise the SEND_MESSAGE that
        follows would reference a file the server hasn't finished ingesting.
        """
        resp = await self.cmd(OpCode.FILE_UPLOAD_URL, {"count": 1})
        if not resp or "_max_error" in resp:
            log.error("File upload URL request failed: %s", resp)
            return None
        info_list = resp.get("info") or []
        if not info_list:
            log.error("File upload response missing 'info': %s", resp)
            return None
        info = info_list[0]
        url = info.get("url")
        file_id = info.get("fileId")
        if not url or file_id is None:
            log.error("File upload info missing url/fileId: %s", info)
            return None

        if chat_id is not None:
            await self.cmd(OpCode.ATTACH_TYPING,
                           {"chatId": chat_id, "type": attach_type})

        # Register the pending future BEFORE the POST so the server's reply
        # (which may arrive before our POST coroutine resumes) is not lost.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._file_pending[int(file_id)] = fut

        ok = await self._http_upload(url, data, filename, mimetype,
                                      expect_json=False)
        if not ok:
            self._file_pending.pop(int(file_id), None)
            return None

        try:
            if await asyncio.wait_for(fut, timeout=timeout) is None:
                return None
        except asyncio.TimeoutError:
            self._file_pending.pop(int(file_id), None)
            log.warning("File upload server processing timed out (fileId=%s)",
                        file_id)
            return None

        return {"_type": attach_type, "fileId": file_id}

    async def _http_upload(self, url: str, data: bytes, filename: str,
                            mimetype: str, expect_json: bool):
        """POST multipart bytes to an upload URL. Returns parsed JSON if
        ``expect_json`` is True, otherwise True on HTTP 200, or None on error.
        """
        session = getattr(self, "_session", None)
        close_after = False
        if session is None or session.closed:
            session = self._new_session(headers=_BROWSER_HEADERS)
            close_after = True
        try:
            # aiohttp's default percent-encodes non-ASCII filenames in
            # Content-Disposition, which MAX persists as _D0_... garbage.
            # Send a safe NFC-normalized UTF-8 filename directly instead.
            form = aiohttp.FormData(quote_fields=False)
            form.add_field(
                "file", data, filename=media_filename({"name": filename}),
                content_type=mimetype,
            )
            async with session.post(
                url, headers=_HTTP_HEADERS, data=form,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:500]
                    log.error("Upload POST failed: HTTP %d — %s",
                              resp.status, body)
                    return None
                if expect_json:
                    try:
                        return await resp.json()
                    except Exception:
                        log.exception("Upload response is not JSON")
                        return None
                # consume body to release connection
                await resp.read()
                return True
        except Exception:
            log.exception("Upload POST error: %s", url[:120])
            return None
        finally:
            if close_after:
                await session.close()

    @staticmethod
    def _safe_media_url(url: str) -> bool:
        """Disallow fetching local services or arbitrary attacker-supplied URLs.

        Attachment and profile URLs arrive through MAX message payloads,
        not from our own code. Limit downloads to trusted MAX media domains.
        """
        if not isinstance(url, str):
            return False
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
                return False
            if parsed.port not in (None, 443):
                return False
            host = (parsed.hostname or "").lower().rstrip(".")
            return any(
                host == suffix or host.endswith("." + suffix)
                for suffix in MEDIA_HOST_SUFFIXES
            )
        except (ValueError, TypeError):
            return False

    async def download_file(self, url: str, *, preserve_url: bool = False, max_bytes: int | None = None) -> bytes | None:
        """Bounded download; only the video compressor may request a larger source."""
        if max_bytes is None:max_bytes=MAX_MEDIA_BYTES
        if type(max_bytes) is not int or not 0 < max_bytes <= 150*1024*1024:
            raise ValueError("Invalid media download cap")
        if not self._safe_media_url(url):
            log.warning("Refusing untrusted MAX media URL")
            return None
        session = getattr(self, "_session", None)
        close_after = False
        if session is None or session.closed:
            session = self._new_session(headers=_BROWSER_HEADERS)
            close_after = True
        try:
            for _ in range(4):
                if not self._safe_media_url(url):
                    log.warning("Blocked redirect to untrusted MAX media URL")
                    return None
                # The CDN signature may cover the exact percent-encoded path
                # and query. aiohttp/yarl normally requotes these components.
                request_url = URL(url, encoded=True) if preserve_url else url
                if preserve_url:
                    log.info("MAX signed media request host=%s default_requotes=%s",
                             urlsplit(url).hostname, str(URL(url)) != url)
                async with session.get(
                    request_url, headers=_HTTP_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=120),
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        target = resp.headers.get("Location")
                        if not target:
                            return None
                        url = urljoin(url, target)
                        continue
                    if resp.status != 200:
                        log.warning("MAX media download failed: HTTP %d host=%s exact_url=%s",
                                    resp.status,urlsplit(url).hostname,preserve_url)
                        return None
                    if resp.content_length and resp.content_length > max_bytes:
                        log.warning("MAX media exceeds %d bytes", max_bytes)
                        return None
                    result = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if len(result) + len(chunk) > max_bytes:
                            log.warning("MAX media exceeded size limit while downloading")
                            return None
                        result.extend(chunk)
                    log.info("Downloaded media from %s (%d bytes)",
                             urlsplit(url).hostname, len(result))
                    return bytes(result)
            log.warning("MAX media has too many redirects")
        except Exception as exc:
            # Never log a signed CDN URL, even through exception formatting.
            log.warning("MAX media download failed: %s",type(exc).__name__)
        finally:
            if close_after:
                await session.close()
        return None

    # ── message parsing ────────────────────────────────────────────

    def _parse_message(self, payload: dict) -> MaxMessage | None:
        msg_body = payload.get("message")
        if not msg_body or not isinstance(msg_body, dict):
            return None

        msg = MaxMessage(
            chat_id=payload.get("chatId"),
            sender_id=msg_body.get("sender"),
            text=msg_body.get("text") or "",
            timestamp=msg_body.get("time"),
            message_id=str(msg_body.get("id") or ""),
            attaches=msg_body.get("attaches") or [],
            link=msg_body.get("link") or {},
            raw=payload,
        )

        if self._my_id is not None and str(msg.sender_id) == str(self._my_id):
            msg.is_self = True

        return msg

    # ── debug helpers ──────────────────────────────────────────────

    @staticmethod
    def _dump_json(filename: str, data: dict) -> None:
        path = os.path.join(DEBUG_DIR, filename)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            log.info("Dumped %s (%d bytes)", path, os.path.getsize(path))
        except Exception:
            log.exception("Failed to dump %s", path)
