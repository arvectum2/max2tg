import asyncio
import io
import zipfile
from types import SimpleNamespace
import logging
from html import escape
from html.parser import HTMLParser

from telegram import Bot, InputFile, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut
from telegram.request import HTTPXRequest

from app.topics import TopicStore
from app.outbox import current_delivery

log = logging.getLogger(__name__)

TG_MAX_LENGTH = 4096
TG_CAPTION_MAX = 1024
TG_TOPIC_NAME_MAX = 128
MAX_RETRIES = 3
# Telegram validates text length AFTER parsing HTML entities; UTF-16 units
# are the safest bound (including astral emoji). Leave margin for entities.
TG_TEXT_PART_MAX = 3500
TG_CAPTION_PART_MAX = 900


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _take_part(value: str, capacity: int) -> int:
    """Choose a Unicode-safe cut, preferring paragraphs / word boundaries."""
    size = 0
    end = 0
    for index, char in enumerate(value):
        units = 2 if ord(char) > 0xFFFF else 1
        if size + units > capacity:
            break
        end = index + 1
        size += units
    if end == len(value):
        return end
    # Keep exact characters, including spaces and newlines, across splits.
    piece = value[:end]
    for mark in ("\n", " "):
        cut = piece.rfind(mark)
        if cut >= end // 2 and cut > 0:
            return cut + 1
    return end


class _HTMLChunker(HTMLParser):
    """Split Telegram HTML with balanced markup and no lost visible text."""

    def __init__(self, max_units: int):
        super().__init__(convert_charrefs=True)
        self.max_units = max_units
        self.stack: list[tuple[str, str]] = []
        self.buffer: list[str] = []
        self.units = 0
        self.parts: list[str] = []

    def _flush(self):
        if self.units == 0:
            return
        suffix = "".join(f"</{tag}>" for tag, _ in reversed(self.stack))
        self.parts.append("".join(self.buffer) + suffix)
        self.buffer = [opening for _, opening in self.stack]
        self.units = 0

    def handle_starttag(self, tag, attrs):
        opening = self.get_starttag_text()
        self.stack.append((tag, opening))
        self.buffer.append(opening)

    def handle_endtag(self, tag):
        # HTML sent by the bridge is balanced. Preserve well-formed nesting.
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
            self.buffer.append(f"</{tag}>")

    def handle_data(self, data):
        while data:
            if self.units >= self.max_units:
                self._flush()
            count = _take_part(data, self.max_units - self.units)
            if count <= 0:
                raise ValueError("Cannot split Telegram message at a Unicode boundary")
            portion, data = data[:count], data[count:]
            self.buffer.append(escape(portion, quote=False))
            self.units += _utf16_length(portion)
            if data:
                self._flush()

    def handle_startendtag(self, tag, attrs):
        self.buffer.append(self.get_starttag_text())


def split_telegram_html(text: str, max_units: int = TG_TEXT_PART_MAX) -> list[str]:
    """Return valid HTML parts; each displays <= max_units UTF-16 units."""
    if not text:
        return []
    if max_units < 2:
        raise ValueError("max_units must be >= 2")
    # Preserve short inputs literally; avoid unnecessary HTML normalization.
    if _utf16_length(text) <= max_units:
        return [text]
    parser = _HTMLChunker(max_units)
    parser.feed(text)
    parser.close()
    parser._flush()
    return parser.parts



def _looks_numeric(title: str) -> bool:
    """A title is 'placeholder' when it carries no human-readable name yet."""
    title = (title or "").strip()
    return not title or title.isdigit() or title.startswith("DM:")


class TelegramSender:
    def __init__(self, token: str, chat_id: str, topic_store: TopicStore,
                 proxy_url: str | None = None):
        if proxy_url:
            request = HTTPXRequest(proxy=proxy_url)
            self._bot = Bot(token=token, request=request)
        else:
            self._bot = Bot(token=token)
        self._chat_id = chat_id
        self._topics = topic_store
        self._topic_lock = asyncio.Lock()

    @property
    def bot(self) -> Bot:
        return self._bot

    @property
    def chat_id(self) -> str:
        return self._chat_id

    @property
    def topic_store(self) -> TopicStore:
        return self._topics

    async def start(self):
        await self._bot.initialize()
        me = await self._bot.get_me()
        log.info("Telegram bot ready: @%s", me.username)

    async def stop(self):
        await self._bot.shutdown()

    # ── forum topics ───────────────────────────────────────────────

    async def ensure_topic(self, max_chat_id, title: str) -> int | None:
        """Return the Telegram forum topic (thread) ID for a Max chat.

        Creates the topic on first use. If a previously created topic still
        carries a placeholder (numeric) name and a real name is now known,
        the topic is renamed. Returns None if topic creation fails — callers
        then fall back to the General topic.
        """
        title = (title or str(max_chat_id)).strip()[:TG_TOPIC_NAME_MAX]

        existing = self._topics.get_topic(max_chat_id)
        if existing is not None:
            stored = self._topics.get_title(max_chat_id) or ""
            stored_is_placeholder = _looks_numeric(stored) or stored.strip().lower() in {"none", "unknown"}
            if title and title != stored and stored_is_placeholder and not _looks_numeric(title):
                try:
                    await self._bot.edit_forum_topic(
                        chat_id=self._chat_id, message_thread_id=existing, name=title
                    )
                    self._topics.update_title(max_chat_id, title)
                    log.info("Renamed forum topic %s → %r", existing, title)
                except Exception:
                    log.exception("Failed to rename forum topic %s", existing)
            return existing

        async with self._topic_lock:
            existing = self._topics.get_topic(max_chat_id)
            if existing is not None:
                return existing
            try:
                topic = await self._bot.create_forum_topic(
                    chat_id=self._chat_id, name=title
                )
            except Exception:
                log.exception(
                    "Failed to create forum topic for Max chat %s — is the supergroup "
                    "a forum and is the bot an admin with 'Manage Topics'?",
                    max_chat_id,
                )
                return None
            thread_id = topic.message_thread_id
            self._topics.set_topic(max_chat_id, thread_id, title)
            log.info("Created forum topic %r (thread=%s) for Max chat %s",
                     title, thread_id, max_chat_id)
            return thread_id

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _caption_parts(caption: str) -> list[str]:
        return split_telegram_html(caption, TG_CAPTION_PART_MAX)

    async def _send_caption_remainder(self, parts: list[str], media_result,
                                      message_thread_id: int | None) -> bool:
        if media_result is None:
            return False
        media_id = getattr(media_result, "message_id", None)
        for part in parts[1:]:
            result = await self.send(
                part, message_thread_id=message_thread_id,
                reply_to_message_id=media_id,
            )
            if result is None:
                log.error("Failed to forward remainder of media caption")
                return False
        return True

    @staticmethod
    def _reply_kwargs(reply_to_message_id: int | None) -> dict:
        if reply_to_message_id is None:
            return {}
        return {
            "reply_to_message_id": int(reply_to_message_id),
            "allow_sending_without_reply": True,
        }

    async def _retry(self, coro_factory):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except RetryAfter as e:
                log.warning("Telegram rate limit, retry after %ss", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except TimedOut:
                log.warning("Telegram timeout (attempt %d/%d)", attempt, MAX_RETRIES)
                await asyncio.sleep(2 * attempt)
            except Exception:
                log.exception("Failed to send to Telegram (attempt %d/%d)", attempt, MAX_RETRIES)
                if attempt == MAX_RETRIES:
                    return None
                await asyncio.sleep(2 * attempt)
        return None

    async def _send_api(self, method: str, kwargs: dict, *,
                        file_field: str | None = None,
                        data: bytes | None = None,
                        filename: str | None = None):
        """One durable Telegram API call under an outbox delivery scope.

        Outside that scope keep the old behaviour for control/management
        messages. Inside, every part is durably recorded before calling TG.
        """
        async def call():
            actual = dict(kwargs)
            if file_field:
                actual[file_field] = InputFile(
                    io.BytesIO(data), filename=filename,
                )
            return await getattr(self._bot, method)(**actual)

        current = current_delivery()
        if current is not None:
            meta = {
                "kwargs": kwargs,
                "file_field": file_field,
                "filename": filename,
            }
            return await current.send_part(method, meta, call, media=data)
        return await self._retry(call)

    # ── send methods ───────────────────────────────────────────────

    async def send(self, text: str, message_thread_id: int | None = None,
                   reply_to_message_id: int | None = None):
        if not text:
            return None
        first_result = None
        for part in split_telegram_html(text):
            kwargs = dict(
                chat_id=self._chat_id,
                text=part,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
                **self._reply_kwargs(
                    reply_to_message_id if first_result is None
                    else getattr(first_result, "message_id", None),
                ),
            )
            result = await self._send_api("send_message", kwargs)
            if result is None:
                log.error("Long Telegram message part failed; not marking complete")
                return None
            if first_result is None:
                first_result = result
        return first_result

    async def send_photo(self, data: bytes, caption: str = "", filename: str = "photo.jpg",
                         message_thread_id: int | None = None,
                         reply_to_message_id: int | None = None):
        parts = self._caption_parts(caption)
        result = await self._send_api("send_photo", dict(
            chat_id=self._chat_id, caption=parts[0] if parts else None,
            parse_mode=ParseMode.HTML, message_thread_id=message_thread_id,
            **self._reply_kwargs(reply_to_message_id)),
            file_field="photo", data=data, filename=filename)
        if not await self._send_caption_remainder(
            parts,result,message_thread_id):
            return None
        return result

    @staticmethod
    def _pack_album(photos: list[bytes]) -> bytes:
        output=io.BytesIO()
        with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_STORED) as pack:
            for i,content in enumerate(photos):
                item=zipfile.ZipInfo(f"{i:02d}.jpg")
                pack.writestr(item,content)
        return output.getvalue()

    @staticmethod
    def _unpack_album(data: bytes) -> list[bytes]:
        with zipfile.ZipFile(io.BytesIO(data),"r") as pack:
            names=pack.namelist()
            if not 2<=len(names)<=10 or any(
                name!=f"{i:02d}.jpg" for i,name in enumerate(names)
            ):
                raise ValueError("Invalid stored Telegram photo album")
            return [pack.read(name) for name in names]

    async def send_photo_album(
        self, photos: list[bytes], caption: str = "",
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ):
        """Send 2..10 photos as one idempotency-tracked Telegram album.

        The one API operation returns multiple messages; all Telegram IDs are
        durably stored in one SQLite part for deletion and restart recovery.
        """
        if not 2<=len(photos)<=10:
            raise ValueError("A Telegram album must contain 2-10 photos")
        parts=self._caption_parts(caption)
        scope=current_delivery()
        cached=scope.cached_media(("send_media_group",)) if scope else None
        packed=cached if cached is not None else self._pack_album(photos)
        saved_photos=self._unpack_album(packed)
        options=dict(chat_id=self._chat_id,message_thread_id=message_thread_id,
                     **self._reply_kwargs(reply_to_message_id))

        async def call():
            media=[
                InputMediaPhoto(
                    media=InputFile(io.BytesIO(data),filename=f"photo-{i+1}.jpg"),
                    caption=parts[0] if i==0 and parts else None,
                    parse_mode=ParseMode.HTML if i==0 and parts else None,
                )
                for i,data in enumerate(saved_photos)
            ]
            return await self._bot.send_media_group(media=media,**options)

        if scope is not None:
            # Persist photo bytes before the first API call. On RetryAfter,
            # recover the packed album even if MAX CDN links have expired.
            result=await scope.send_part("send_media_group",{
                "kwargs":options,
                "caption":parts[0] if parts else None,
                "count":len(saved_photos),
            },call,media=packed)
        else:
            result=await self._retry(call)
            if isinstance(result,(list,tuple)) and result:
                result=SimpleNamespace(
                    message_id=result[0].message_id,
                    media_group_ids=[m.message_id for m in result],
                )

        if not await self._send_caption_remainder(
            parts,result,message_thread_id):
            return None
        return result

    async def send_document(self, data: bytes, caption: str = "", filename: str = "file",
                            message_thread_id: int | None = None,
                            reply_to_message_id: int | None = None):
        parts = self._caption_parts(caption)
        result = await self._send_api("send_document", dict(
            chat_id=self._chat_id, caption=parts[0] if parts else None,
            parse_mode=ParseMode.HTML, message_thread_id=message_thread_id,
            **self._reply_kwargs(reply_to_message_id)),
            file_field="document", data=data, filename=filename)
        if not await self._send_caption_remainder(
            parts,result,message_thread_id):
            return None
        return result

    async def send_video(self, data: bytes, caption: str = "", filename: str = "video.mp4",
                         message_thread_id: int | None = None,
                         reply_to_message_id: int | None = None):
        parts = self._caption_parts(caption)
        result = await self._send_api("send_video", dict(
            chat_id=self._chat_id, caption=parts[0] if parts else None,
            parse_mode=ParseMode.HTML, message_thread_id=message_thread_id,
            **self._reply_kwargs(reply_to_message_id)),
            file_field="video", data=data, filename=filename)
        if not await self._send_caption_remainder(
            parts,result,message_thread_id):
            return None
        return result

    async def send_audio(self,data: bytes,caption: str = "",
                         filename: str = "audio.mp3",
                         message_thread_id: int | None = None,
                         reply_to_message_id: int | None = None):
        """Send a playable audio file, not an OGG/Opus voice note."""
        parts=self._caption_parts(caption)
        result=await self._send_api("send_audio",dict(
            chat_id=self._chat_id,caption=parts[0] if parts else None,
            parse_mode=ParseMode.HTML,message_thread_id=message_thread_id,
            **self._reply_kwargs(reply_to_message_id)),
            file_field="audio",data=data,filename=filename)
        if not await self._send_caption_remainder(
            parts,result,message_thread_id):
            return None
        return result

    async def send_voice(self, data: bytes, caption: str = "",
                         message_thread_id: int | None = None,
                         reply_to_message_id: int | None = None):
        parts = self._caption_parts(caption)
        opts = dict(
            chat_id=self._chat_id, caption=parts[0] if parts else None,
            parse_mode=ParseMode.HTML, message_thread_id=message_thread_id,
            **self._reply_kwargs(reply_to_message_id),
        )
        result = await self._send_api(
            "send_voice", opts, file_field="voice",
            data=data, filename="voice.ogg")
        if result is None:
            log.info("send_voice failed, falling back to send_audio")
            result = await self._send_api(
                "send_audio", opts, file_field="audio",
                data=data, filename="audio.m4a")
        if not await self._send_caption_remainder(
            parts,result,message_thread_id):
            return None
        return result

    async def send_sticker(self, data: bytes, message_thread_id: int | None = None,
                           reply_to_message_id: int | None = None):
        return await self._send_api(
            "send_sticker", dict(
                chat_id=self._chat_id, message_thread_id=message_thread_id,
                **self._reply_kwargs(reply_to_message_id)),
            file_field="sticker", data=data, filename="sticker.webp")
