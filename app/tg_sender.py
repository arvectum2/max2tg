import asyncio
import io
import logging
from html import escape
from html.parser import HTMLParser

from telegram import Bot, InputFile
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut
from telegram.request import HTTPXRequest

from app.topics import TopicStore

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
                                      message_thread_id: int | None) -> None:
        if media_result is None:
            return
        media_id = getattr(media_result, "message_id", None)
        for part in parts[1:]:
            result = await self.send(
                part, message_thread_id=message_thread_id,
                reply_to_message_id=media_id,
            )
            if result is None:
                log.error("Failed to forward remainder of media caption")
                break

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

    # ── send methods ───────────────────────────────────────────────

    async def send(self, text: str, message_thread_id: int | None = None,
                   reply_to_message_id: int | None = None):
        if not text:
            return None

        first_result = None
        for part in split_telegram_html(text):
            result = await self._retry(
                lambda: self._bot.send_message(
                    chat_id=self._chat_id,
                    text=part,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=message_thread_id,
                    **self._reply_kwargs(
                        reply_to_message_id if first_result is None
                        else getattr(first_result, "message_id", None)
                    ),
                )
            )
            if result is None:
                log.error("Long Telegram message part failed; not marking complete")
                return None
            if first_result is None:
                first_result = result
        return first_result

    async def send_photo(self, data: bytes, caption: str = "", filename: str = "photo.jpg",
                         message_thread_id: int | None = None,
                         reply_to_message_id: int | None = None):
        caption_parts = self._caption_parts(caption)
        caption = caption_parts[0] if caption_parts else ""
        result = await self._retry(
            lambda: self._bot.send_photo(
                chat_id=self._chat_id,
                photo=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
                **self._reply_kwargs(reply_to_message_id),
            )
        )
        await self._send_caption_remainder(caption_parts, result, message_thread_id)
        return result

    async def send_document(self, data: bytes, caption: str = "", filename: str = "file",
                            message_thread_id: int | None = None,
                            reply_to_message_id: int | None = None):
        caption_parts = self._caption_parts(caption)
        caption = caption_parts[0] if caption_parts else ""
        result = await self._retry(
            lambda: self._bot.send_document(
                chat_id=self._chat_id,
                document=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
                **self._reply_kwargs(reply_to_message_id),
            )
        )
        await self._send_caption_remainder(caption_parts, result, message_thread_id)
        return result

    async def send_video(self, data: bytes, caption: str = "", filename: str = "video.mp4",
                         message_thread_id: int | None = None,
                         reply_to_message_id: int | None = None):
        caption_parts = self._caption_parts(caption)
        caption = caption_parts[0] if caption_parts else ""
        result = await self._retry(
            lambda: self._bot.send_video(
                chat_id=self._chat_id,
                video=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
                **self._reply_kwargs(reply_to_message_id),
            )
        )
        await self._send_caption_remainder(caption_parts, result, message_thread_id)
        return result

    async def send_voice(self, data: bytes, caption: str = "",
                         message_thread_id: int | None = None,
                         reply_to_message_id: int | None = None):
        caption_parts = self._caption_parts(caption)
        caption = caption_parts[0] if caption_parts else ""
        result = await self._retry(
            lambda: self._bot.send_voice(
                chat_id=self._chat_id,
                voice=InputFile(io.BytesIO(data), filename="voice.ogg"),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
                **self._reply_kwargs(reply_to_message_id),
            )
        )
        if result is None:
            log.info("send_voice failed, falling back to send_audio")
            result = await self._retry(
                lambda: self._bot.send_audio(
                    chat_id=self._chat_id,
                    audio=InputFile(io.BytesIO(data), filename="audio.m4a"),
                    caption=caption or None,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=message_thread_id,
                    **self._reply_kwargs(reply_to_message_id),
                )
            )
        await self._send_caption_remainder(caption_parts, result, message_thread_id)
        return result

    async def send_sticker(self, data: bytes, message_thread_id: int | None = None,
                           reply_to_message_id: int | None = None):
        return await self._retry(
            lambda: self._bot.send_sticker(
                chat_id=self._chat_id,
                sticker=InputFile(io.BytesIO(data), filename="sticker.webp"),
                message_thread_id=message_thread_id,
                **self._reply_kwargs(reply_to_message_id),
            )
        )
