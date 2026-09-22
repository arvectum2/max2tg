import asyncio
import io
import logging
import re
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, ReplyKeyboardMarkup, Update
from telegram.constants import MessageEntityType
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.max_client import MaxClient
from app.topics import TopicStore

log = logging.getLogger(__name__)

MAX_CLIENT_KEY = "max_client"
TOPIC_STORE_KEY = "topic_store"
ALLOWED_USER_KEY = "allowed_user_id"  # legacy single-user key
ALLOWED_USERS_KEY = "allowed_user_ids"
ADMIN_USER_KEY = "admin_user_id"
SUPERGROUP_KEY = "supergroup_id"

_MAX_URL_RE = re.compile(r"https?://(?:web\.)?max\.ru/(-?\d+)")
_MENU_PAGE_SIZE = 8
_MENU_ACTIVE_TYPES = {"DIALOG", "CHAT", "CHANNEL"}
_SEARCH_LIMIT = 16
_SEARCH_WAITING_KEY = "awaiting_max_search"
_SEARCH_RESULTS_KEY = "max_search_results"
_SEARCH_QUERY_KEY = "max_search_query"
_QUICK_MENU_TEXT = "☰ Меню"
_QUICK_MENU_MESSAGE_KEY = "quick_menu_message_id"

# Telegram entity type → MAX element type. The MAX names match what the
# existing codebase used (STRONG) and what MAX renders for the formatting
# styles surfaced in its UI (bold/italic/strike/underline/code).
_TG_TO_MAX_ELEMENT_TYPE = {
    MessageEntityType.BOLD: "STRONG",
    MessageEntityType.ITALIC: "EMPHASIZED",
    MessageEntityType.STRIKETHROUGH: "STRIKETHROUGH",
    MessageEntityType.UNDERLINE: "UNDERLINE",
    MessageEntityType.CODE: "MONOSPACED",
    # MAX's WebSocket protocol exposes a smaller element enum than its bot
    # HTTP API: BLOCKQUOTE / CODE_BLOCK / HIGHLIGHTED / HEADING are all
    # rejected with "No enum constant". Best fallback for multi-line code
    # is the same monospace style as inline code. Telegram blockquotes
    # have no MAX counterpart at all — let them through as plain text
    # rather than fail the whole send_message.
    MessageEntityType.PRE: "MONOSPACED",
}


def _utf16_to_char_offset(text: str, utf16_offset: int) -> int:
    """Convert a UTF-16 code-units offset (what Telegram uses for entity
    positions) into a Python codepoint index (which MAX appears to use)."""
    if utf16_offset <= 0 or not text:
        return 0
    encoded = text.encode("utf-16-le")
    truncated = encoded[: utf16_offset * 2]
    return len(truncated.decode("utf-16-le", errors="ignore"))


def _entities_to_max_elements(text: str, entities) -> list:
    """Map Telegram message entities to MAX `elements` descriptors so basic
    inline formatting (bold/italic/strike/underline/code/link) survives the
    Telegram → MAX bridge."""
    if not entities:
        return []
    elements: list[dict] = []
    for e in entities:
        start = _utf16_to_char_offset(text, e.offset)
        end = _utf16_to_char_offset(text, e.offset + e.length)
        length = end - start
        if length <= 0:
            continue
        max_type = _TG_TO_MAX_ELEMENT_TYPE.get(e.type)
        if max_type:
            elements.append({"type": max_type, "from": start, "length": length})
        elif e.type == MessageEntityType.TEXT_LINK and getattr(e, "url", None):
            elements.append({
                "type": "LINK",
                "from": start,
                "length": length,
                "attributes": {"url": e.url},
            })
    return elements


def _effective_user_id(update: Update) -> int | None:
    user = update.effective_user
    return int(user.id) if user is not None else None


def _is_allowed_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = _effective_user_id(update)
    allowed = context.bot_data.get(ALLOWED_USERS_KEY)
    if allowed:
        return user_id in allowed
    legacy = context.bot_data.get(ALLOWED_USER_KEY)
    return not legacy or user_id == legacy


def _is_admin_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = _effective_user_id(update)
    admin = context.bot_data.get(ADMIN_USER_KEY)
    if admin:
        return user_id == admin
    return _is_allowed_user(update, context)


def _parse_max_chat_id(s: str) -> int | None:
    """Accept either a raw chat id or a web.max.ru URL."""
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        pass
    m = _MAX_URL_RE.match(s)
    if m:
        return int(m.group(1))
    return None


def _peer_id_in_dm(resolver, chat_id) -> int | None:
    """Return the other participant of a DIALOG chat (i.e., not us)."""
    chat = resolver.chats_raw.get(chat_id) or {}
    my_id = resolver.my_id
    for uid_str in chat.get("participants") or {}:
        try:
            uid = int(uid_str)
        except (TypeError, ValueError):
            continue
        if uid != my_id:
            return uid
    return None


def _resolve_topic_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Common prelude for topic handlers. Returns (message, max_chat_id, max_client)
    if the message should be routed, or None to drop silently."""
    message = update.message
    if message is None:
        return None
    thread_id = message.message_thread_id
    if thread_id is None or not message.is_topic_message:
        return None
    topic_store: TopicStore | None = context.bot_data.get(TOPIC_STORE_KEY)
    max_chat_id = topic_store.chat_for_topic(thread_id) if topic_store else None
    if max_chat_id is None:
        return None
    if not _is_allowed_user(update, context):
        return None
    max_client: MaxClient | None = context.bot_data.get(MAX_CLIENT_KEY)
    return message, max_chat_id, max_client


async def _surface_send_result(message, resp) -> None:
    """Translate a Max send_message response into a Telegram reaction or warning."""
    err = (resp or {}).get("_max_error")
    if err:
        desc = (err.get("localizedMessage") or err.get("message")
                or err.get("error") or "не удалось отправить сообщение")
        await message.reply_text(f"⚠️ MAX: {desc}")
        return
    if not resp:
        await message.reply_text("⚠️ Таймаут от MAX — сообщение не подтверждено.")
        return
    try:
        await message.set_reaction("👀")
    except Exception:
        log.debug("Could not set reaction on confirmed message", exc_info=True)


def _max_reply_link(message, topic_store: TopicStore, max_chat_id) -> dict | None:
    """Translate a Telegram reply into a MAX REPLY link when possible."""
    replied = getattr(message, "reply_to_message", None)
    if replied is None:
        return None
    max_message_id = topic_store.max_for_tg_message(
        max_chat_id, replied.message_id,
    )
    if not max_message_id:
        return None
    return {"type": "REPLY", "messageId": str(max_message_id)}


def _remember_outbound_message(
    topic_store: TopicStore, max_chat_id, tg_message_id: int, resp: dict | None,
) -> None:
    if not resp or resp.get("_max_error"):
        return
    max_message = resp.get("message") or {}
    max_message_id = max_message.get("id")
    if max_message_id:
        topic_store.set_message(max_chat_id, max_message_id, tg_message_id)


async def _on_topic_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a text message typed in a forum topic back to the matching Max chat."""
    target = _resolve_topic_target(update, context)
    if not target:
        return
    message, max_chat_id, max_client = target
    if not message.text:
        return

    if not max_client:
        await message.reply_text("⚠️ MAX не подключён.")
        return

    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    elements = _entities_to_max_elements(message.text, message.entities)
    link = _max_reply_link(message, topic_store, max_chat_id)
    send_kwargs = {"elements": elements}
    if link:
        send_kwargs["link"] = link
    try:
        resp = await max_client.send_message(
            max_chat_id, message.text, **send_kwargs,
        )
    except Exception:
        log.exception("Failed to send reply to Max chat %s", max_chat_id)
        await message.reply_text("⚠️ Ошибка при отправке в Max.")
        return

    _remember_outbound_message(
        topic_store, max_chat_id, message.message_id, resp,
    )
    await _surface_send_result(message, resp)


async def _download_tg_file(file_obj) -> bytes | None:
    """Pull bytes from a Telegram File object via the Bot API."""
    try:
        tg_file = await file_obj.get_file()
        return bytes(await tg_file.download_as_bytearray())
    except Exception:
        log.exception("Failed to download Telegram file")
        return None


async def _on_topic_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a media message (photo / voice / document / audio / video) from a
    forum topic to the matching Max chat. Caption, if any, becomes the
    accompanying text."""
    target = _resolve_topic_target(update, context)
    if not target:
        return
    message, max_chat_id, max_client = target

    if not max_client:
        await message.reply_text("⚠️ MAX не подключён.")
        return

    caption = message.caption or ""

    # ── pick the right uploader for the attached media ────────────
    attach = None
    if message.photo:
        # message.photo is a list of progressively larger PhotoSize objects;
        # the last one is the highest resolution.
        photo = message.photo[-1]
        data = await _download_tg_file(photo)
        if data is None:
            await message.reply_text("⚠️ Не удалось скачать фото из Telegram.")
            return
        attach = await max_client.upload_photo(data, chat_id=max_chat_id)

    elif message.voice:
        data = await _download_tg_file(message.voice)
        if data is None:
            await message.reply_text("⚠️ Не удалось скачать голосовое из Telegram.")
            return
        attach = await max_client.upload_audio(
            data, chat_id=max_chat_id,
            filename="voice.ogg",
            mimetype="audio/ogg",
        )

    elif message.audio:
        data = await _download_tg_file(message.audio)
        if data is None:
            await message.reply_text("⚠️ Не удалось скачать аудио из Telegram.")
            return
        attach = await max_client.upload_file(
            data, chat_id=max_chat_id,
            filename=message.audio.file_name or "audio",
            mimetype=message.audio.mime_type or "audio/mpeg",
        )

    elif message.document:
        data = await _download_tg_file(message.document)
        if data is None:
            await message.reply_text("⚠️ Не удалось скачать файл из Telegram.")
            return
        attach = await max_client.upload_file(
            data, chat_id=max_chat_id,
            filename=message.document.file_name or "file",
            mimetype=message.document.mime_type or "application/octet-stream",
        )

    elif message.video:
        data = await _download_tg_file(message.video)
        if data is None:
            await message.reply_text("⚠️ Не удалось скачать видео из Telegram.")
            return
        attach = await max_client.upload_file(
            data, chat_id=max_chat_id,
            filename=message.video.file_name or "video.mp4",
            mimetype=message.video.mime_type or "video/mp4",
        )

    else:
        return  # unsupported media kind

    if not attach:
        await message.reply_text("⚠️ Не удалось загрузить файл в MAX.")
        return

    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    elements = _entities_to_max_elements(caption, message.caption_entities)
    link = _max_reply_link(message, topic_store, max_chat_id)
    send_kwargs = {
        "text": caption,
        "elements": elements,
        "attaches": [attach],
    }
    if link:
        send_kwargs["link"] = link
    try:
        resp = await max_client.send_message(max_chat_id, **send_kwargs)
    except Exception:
        log.exception("Failed to send media reply to Max chat %s", max_chat_id)
        await message.reply_text("⚠️ Ошибка при отправке в Max.")
        return

    _remember_outbound_message(
        topic_store, max_chat_id, message.message_id, resp,
    )
    await _surface_send_result(message, resp)


async def post_topic_intro(bot, supergroup_id, max_client: MaxClient,
                            max_chat_id, thread_id: int, *,
                            pin: bool = True) -> None:
    """Publish a profile/info card as the first message of a topic, then pin
    it. Called when a topic is freshly created (either auto on first
    incoming message or manually via /bind)."""
    resolver = getattr(max_client, "resolver", None)
    if resolver is None:
        return

    is_dm = resolver.is_dm(max_chat_id)

    if is_dm:
        peer_id = _peer_id_in_dm(resolver, max_chat_id)
        if peer_id is None:
            return
        contact = resolver.contacts_raw.get(peer_id)
        if contact is None:
            try:
                await max_client.fetch_contacts([peer_id])
            except Exception:
                log.exception("post_topic_intro: fetch_contacts failed")
            contact = resolver.contacts_raw.get(peer_id)

        name = resolver.user_name(peer_id)
        phone = (contact or {}).get("phone") or ""
        about = ((contact or {}).get("description")
                 or (contact or {}).get("about")
                 or (contact or {}).get("status") or "")
        username = (contact or {}).get("link") or (contact or {}).get("username") or ""

        lines = [f"<b>{escape(str(name))}</b>",
                 f"id: <code>{peer_id}</code>"]
        if phone:
            lines.append(f"📞 <code>{escape(str(phone))}</code>")
        if username:
            lines.append(f"🔗 {escape(str(username))}")
        if about:
            lines.append(f"\n{escape(str(about))}")
        body = "\n".join(lines)

        photo_url = None
        photo_obj = (contact or {}).get("photo") or (contact or {}).get("avatar")
        if isinstance(photo_obj, dict):
            photo_url = (photo_obj.get("baseUrl") or photo_obj.get("url")
                         or photo_obj.get("photoUrl"))
        photo_url = (photo_url
                     or (contact or {}).get("baseUrl")
                     or (contact or {}).get("baseRawUrl")
                     or (contact or {}).get("photoUrl")
                     or (contact or {}).get("baseRawIconUrl"))
    else:
        chat = resolver.chats_raw.get(max_chat_id) or {}
        title = chat.get("title") or resolver.chat_name(max_chat_id)
        ctype = chat.get("type") or "?"
        participants = chat.get("participants") or {}
        descr = chat.get("description") or ""
        link = chat.get("link") or ""

        lines = [f"<b>{escape(str(title))}</b> · {escape(str(ctype))}",
                 f"id: <code>{max_chat_id}</code>",
                 f"Участников: <b>{len(participants)}</b>"]
        if descr:
            lines.append(f"\n{escape(str(descr))}")
        if link:
            lines.append(f"\n🔗 {escape(str(link))}")
        body = "\n".join(lines)
        photo_url = chat.get("baseRawIconUrl") or chat.get("baseUrl")

    sent = None
    if photo_url:
        data = await max_client.download_file(photo_url)
        if data:
            try:
                sent = await bot.send_photo(
                    chat_id=int(supergroup_id),
                    photo=InputFile(io.BytesIO(data), filename="profile.jpg"),
                    caption=body, parse_mode="HTML",
                    message_thread_id=thread_id,
                    reply_markup=_management_keyboard(max_chat_id, thread_id),
                )
            except Exception:
                log.exception("post_topic_intro: send_photo failed")
                sent = None

    if sent is None:
        try:
            sent = await bot.send_message(
                chat_id=int(supergroup_id), text=body, parse_mode="HTML",
                message_thread_id=thread_id,
                reply_markup=_management_keyboard(max_chat_id, thread_id),
            )
        except Exception:
            log.exception("post_topic_intro: send_message failed")
            return

    if pin and sent is not None:
        try:
            await bot.pin_chat_message(
                chat_id=int(supergroup_id),
                message_id=sent.message_id,
                disable_notification=True,
            )
        except Exception:
            log.exception("post_topic_intro: pin_chat_message failed")


async def _cmd_bind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a forum topic bound to a specific Max chat id.

    Usage: `/bind <chat_id-or-url> [optional-title]` — typed anywhere in the
    supergroup. The bot creates a new forum topic (or reports an existing
    binding) and stores the mapping so messages typed there get forwarded
    to the Max chat.
    """
    message = update.message
    if message is None:
        return

    if not _is_admin_user(update, context):
        return

    args = context.args or []
    if not args:
        await message.reply_text(
            "Использование: <code>/bind &lt;chat_id или https://web.max.ru/-...&gt; "
            "[название]</code>",
            parse_mode="HTML",
        )
        return

    max_chat_id = _parse_max_chat_id(args[0])
    if max_chat_id is None:
        await message.reply_text(
            "Не понял chat_id. Пример: <code>/bind -75107924425434</code>",
            parse_mode="HTML",
        )
        return

    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    existing = topic_store.get_topic(max_chat_id)
    if existing is not None:
        # A forum topic can be deleted manually by a Telegram admin while our
        # local mapping survives. Probe the stored thread and self-heal only
        # when Telegram explicitly says the thread no longer exists.
        stored_title = topic_store.get_title(max_chat_id) or str(max_chat_id)
        try:
            await context.bot.edit_forum_topic(
                chat_id=int(context.bot_data[SUPERGROUP_KEY]),
                message_thread_id=existing,
                name=stored_title[:128],
            )
        except BadRequest as exc:
            text = str(exc).lower()
            if "message thread not found" in text or "message_thread_not_found" in text:
                log.warning(
                    "/bind: stale mapping for MAX chat %s → thread %s; recreating",
                    max_chat_id, existing,
                )
                topic_store.remove(max_chat_id)
                existing = None
            else:
                # "Topic not modified" and similar errors still prove that the
                # topic exists, so keep the mapping.
                await message.reply_text(
                    f"Этот чат MAX уже привязан к топику "
                    f"(thread_id=<code>{existing}</code>).",
                    parse_mode="HTML",
                )
                return
        except Exception:
            log.exception("/bind: could not verify existing forum topic")
            await message.reply_text(
                f"Этот чат MAX уже привязан к топику "
                f"(thread_id=<code>{existing}</code>).",
                parse_mode="HTML",
            )
            return
        else:
            await message.reply_text(
                f"Этот чат MAX уже привязан к топику "
                f"(thread_id=<code>{existing}</code>).",
                parse_mode="HTML",
            )
            return

    max_client: MaxClient = context.bot_data[MAX_CLIENT_KEY]
    resolver = getattr(max_client, "resolver", None)

    # Build a topic title: explicit second arg → known chat title → chat id.
    if len(args) > 1:
        title = " ".join(args[1:]).strip()
    elif resolver and resolver.chat_name(max_chat_id) != str(max_chat_id):
        title = resolver.chat_name(max_chat_id)
    else:
        title = str(max_chat_id)
    title = title[:128]

    supergroup_id = context.bot_data[SUPERGROUP_KEY]
    try:
        topic = await context.bot.create_forum_topic(
            chat_id=int(supergroup_id), name=title,
        )
    except Exception as exc:
        log.exception("Failed to create forum topic for %s", max_chat_id)
        await message.reply_text(f"Не удалось создать топик: {exc}")
        return

    thread_id = topic.message_thread_id
    topic_store.set_topic(max_chat_id, thread_id, title)
    await message.reply_text(
        f"Готово: <b>{escape(title)}</b> ↔ MAX <code>{max_chat_id}</code> "
        f"(thread_id=<code>{thread_id}</code>). Пиши в новом топике — улетит в MAX.",
        parse_mode="HTML",
    )
    # Post & pin a profile card in the freshly-created topic.
    supergroup_id = context.bot_data[SUPERGROUP_KEY]
    asyncio.create_task(
        post_topic_intro(context.bot, supergroup_id, max_client,
                          max_chat_id, thread_id)
    )


_MAX_LINK_RE = re.compile(r"https?://max\.ru/[A-Za-z0-9_\-/]+")


def _extract_chat_id_from_open(resp: dict) -> int | None:
    """Pick a chat id out of the various shapes opcode 57 returns."""
    if not isinstance(resp, dict):
        return None
    # Direct fields seen in practice.
    for key in ("chatId", "conversationId"):
        v = resp.get(key)
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                pass
    # Nested chat object.
    chat = resp.get("chat")
    if isinstance(chat, dict):
        cid = chat.get("id")
        if isinstance(cid, int):
            return cid
        if isinstance(cid, str):
            try:
                return int(cid)
            except ValueError:
                pass
    return None


async def _cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open a max.ru link (user or chat invite) and bind it to a new topic.

    Usage: `/add https://max.ru/u/<token>` or `/add https://max.ru/join/<token>`.
    """
    message = update.message
    if message is None:
        return

    if not _is_admin_user(update, context):
        return

    args = context.args or []
    link = args[0] if args else ""
    # Try to extract a max.ru link from anywhere in the message text too,
    # so `/add` works if the link was just pasted alongside the command.
    if not link.startswith(("http://", "https://")) and message.text:
        m = _MAX_LINK_RE.search(message.text)
        if m:
            link = m.group(0)

    if not link.startswith(("http://", "https://")) or "max.ru/" not in link:
        await message.reply_text(
            "Использование: <code>/add https://max.ru/u/...</code> или "
            "<code>/add https://max.ru/join/...</code>",
            parse_mode="HTML",
        )
        return

    max_client: MaxClient = context.bot_data[MAX_CLIENT_KEY]
    try:
        resp = await max_client.open_by_link(link)
    except Exception as exc:
        log.exception("open_by_link failed")
        await message.reply_text(f"⚠️ Ошибка при обращении к MAX: {exc}")
        return

    err = (resp or {}).get("_max_error")
    if err:
        desc = (err.get("localizedMessage") or err.get("message")
                or err.get("error") or "MAX отказал")
        await message.reply_text(f"⚠️ MAX: {desc}")
        return
    if not resp:
        await message.reply_text("⚠️ Таймаут от MAX, ссылка не открылась.")
        return

    chat_id = _extract_chat_id_from_open(resp)
    if chat_id is None:
        log.warning("/add: cannot extract chat_id from response: %s", resp)
        await message.reply_text(
            "MAX принял ссылку, но не вернул chat_id, который я понимаю. "
            "Лог: <code>" + escape(str(resp)[:300]) + "</code>",
            parse_mode="HTML",
        )
        return

    # Let the resolver pick up the freshly arrived chat metadata, if any.
    resolver = getattr(max_client, "resolver", None)
    if resolver is not None and isinstance(resp.get("chat"), dict):
        chat_obj = resp["chat"]
        resolver.chats_raw[chat_id] = chat_obj
        if chat_obj.get("type"):
            resolver.chat_types[chat_id] = chat_obj["type"]
        if chat_obj.get("title"):
            resolver.chats[chat_id] = chat_obj["title"]

    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    existing = topic_store.get_topic(chat_id)
    if existing is not None:
        await message.reply_text(
            f"Этот чат MAX уже привязан к топику (thread_id=<code>{existing}</code>).",
            parse_mode="HTML",
        )
        return

    # Pick a title — prefer chat title or peer name from resolver.
    title = None
    if resolver is not None:
        title = resolver.chat_name(chat_id)
        if title == str(chat_id) and resolver.is_dm(chat_id):
            peer_id = _peer_id_in_dm(resolver, chat_id)
            if peer_id is not None:
                # Best-effort fetch contact name now.
                try:
                    await max_client.fetch_contacts([peer_id])
                except Exception:
                    pass
                title = resolver.user_name(peer_id)
    if not title or title == str(chat_id):
        title = str(chat_id)
    title = title[:128]

    supergroup_id = context.bot_data[SUPERGROUP_KEY]
    try:
        topic = await context.bot.create_forum_topic(
            chat_id=int(supergroup_id), name=title,
        )
    except Exception as exc:
        log.exception("/add: create_forum_topic failed")
        await message.reply_text(f"Не удалось создать топик: {exc}")
        return

    thread_id = topic.message_thread_id
    topic_store.set_topic(chat_id, thread_id, title)
    await message.reply_text(
        f"Готово: <b>{escape(title)}</b> ↔ MAX <code>{chat_id}</code> "
        f"(thread_id=<code>{thread_id}</code>).",
        parse_mode="HTML",
    )
    asyncio.create_task(
        post_topic_intro(context.bot, supergroup_id, max_client,
                          chat_id, thread_id)
    )


def _menu_chat_title(resolver, chat_id) -> str:
    title = resolver.chat_name(chat_id)
    if resolver.is_dm(chat_id) and str(title).startswith("DM:"):
        peer_id = _peer_id_in_dm(resolver, chat_id)
        if peer_id is not None:
            resolved = resolver.user_name(peer_id)
            if resolved and resolved != str(peer_id):
                title = resolved
    return str(title)


def _menu_entries(resolver, topic_store: TopicStore) -> list[tuple]:
    entries = []
    chat_ids = set(resolver.chats) | set(resolver.chats_raw)
    for chat_id in chat_ids:
        ctype = resolver.chat_types.get(chat_id)
        raw = resolver.chats_raw.get(chat_id) or {}
        if ctype not in _MENU_ACTIVE_TYPES:
            continue
        if raw.get("status") in ("LEFT", "CLOSED"):
            continue
        title = _menu_chat_title(resolver, chat_id)
        entries.append((title.casefold(), chat_id, title, ctype,
                        topic_store.get_topic(chat_id)))
    entries.sort(key=lambda item: (item[0], str(item[1])))
    return entries


def _menu_list_markup(resolver, topic_store: TopicStore, page: int = 0):
    entries = _menu_entries(resolver, topic_store)
    page_count = max(1, (len(entries) + _MENU_PAGE_SIZE - 1) // _MENU_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * _MENU_PAGE_SIZE
    rows = []
    icons = {"DIALOG": "👤", "CHAT": "💬", "CHANNEL": "📢"}
    for _, chat_id, title, ctype, thread_id in entries[start:start + _MENU_PAGE_SIZE]:
        linked = "✅ " if thread_id else ""
        rows.append([InlineKeyboardButton(
            f"{linked}{icons.get(ctype, '💬')} {title[:42]}",
            callback_data=f"menu:chat:{chat_id}:{page}",
        )])
    if page_count > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("←", callback_data=f"menu:list:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{page_count}", callback_data="menu:noop"))
        if page + 1 < page_count:
            nav.append(InlineKeyboardButton("→", callback_data=f"menu:list:{page + 1}"))
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("🔎 Поиск в MAX", callback_data="menu:search"),
        InlineKeyboardButton("🔄 Обновить", callback_data=f"menu:list:{page}"),
    ])
    return InlineKeyboardMarkup(rows), page, page_count, len(entries)


def _contact_display_name(contact: dict) -> str:
    names = contact.get("names")
    if isinstance(names, list) and names:
        first = names[0].get("firstName", "")
        last = names[0].get("lastName", "")
        name = f"{first} {last}".strip() or names[0].get("name", "")
        if name:
            return str(name)
    first = contact.get("firstName") or contact.get("first_name") or ""
    last = contact.get("lastName") or contact.get("last_name") or ""
    return (f"{first} {last}".strip()
            or str(contact.get("friendly") or contact.get("displayName")
                   or contact.get("name") or contact.get("link") or ""))


def _normalize_global_search_results(resp: dict | None,
                                     max_client: MaxClient) -> list[dict]:
    if not isinstance(resp, dict):
        return []
    records: list[dict] = []
    seen: set[int] = set()
    for item in resp.get("result") or []:
        if not isinstance(item, dict):
            continue

        chat = item.get("chat")
        if isinstance(chat, dict):
            chat_id = chat.get("id")
            try:
                chat_id = int(chat_id)
            except (TypeError, ValueError):
                continue
            ctype = chat.get("type")
            if ctype not in ("CHAT", "CHANNEL"):
                continue
            if chat_id in seen:
                continue
            title = str(chat.get("title") or chat.get("name") or chat_id)
            records.append({
                "chat_id": chat_id,
                "type": ctype,
                "title": title,
                "link": chat.get("link") or "",
                "raw": chat,
            })
            seen.add(chat_id)
            continue

        wrapper = item.get("contact")
        if not isinstance(wrapper, dict):
            continue
        contact = wrapper.get("contact")
        if not isinstance(contact, dict):
            contact = wrapper
        contact_id = contact.get("id") or contact.get("userId")
        try:
            contact_id = int(contact_id)
        except (TypeError, ValueError):
            continue
        if max_client._my_id is not None and contact_id == int(max_client._my_id):
            continue
        try:
            chat_id = max_client.dialog_chat_id(contact_id)
        except (RuntimeError, TypeError, ValueError):
            continue
        if chat_id in seen:
            continue
        title = _contact_display_name(contact) or str(contact_id)
        records.append({
            "chat_id": chat_id,
            "contact_id": contact_id,
            "type": "DIALOG",
            "title": title,
            "link": contact.get("link") or "",
            "raw": contact,
        })
        seen.add(chat_id)

    return records


def _search_results_markup(records: list[dict], topic_store: TopicStore) -> InlineKeyboardMarkup:
    rows = []
    icons = {"DIALOG": "👤", "CHAT": "💬", "CHANNEL": "📢"}
    for record in records[:_SEARCH_LIMIT]:
        chat_id = record["chat_id"]
        linked = "✅ " if topic_store.get_topic(chat_id) else ""
        label = f"{linked}{icons.get(record['type'], '💬')} {record['title']}"[:55]
        rows.append([InlineKeyboardButton(
            label, callback_data=f"search:open:{chat_id}",
        )])
    rows.append([
        InlineKeyboardButton("🔎 Новый поиск", callback_data="search:new"),
        InlineKeyboardButton("← Мои чаты", callback_data="menu:list:0"),
    ])
    return InlineKeyboardMarkup(rows)


def _cache_search_record_in_resolver(max_client: MaxClient, record: dict) -> None:
    resolver = getattr(max_client, "resolver", None)
    if resolver is None:
        return
    chat_id = record["chat_id"]
    ctype = record["type"]
    title = record["title"]

    if ctype == "DIALOG":
        contact_id = record.get("contact_id")
        contact = record.get("raw") or {}
        if contact_id is not None:
            resolver.contacts_raw[contact_id] = contact
            resolver.users[contact_id] = title
        participants = {}
        if resolver.my_id is not None:
            participants[str(resolver.my_id)] = {}
        if contact_id is not None:
            participants[str(contact_id)] = {}
        resolver.chats_raw[chat_id] = {
            "id": chat_id,
            "type": "DIALOG",
            "status": "ACTIVE",
            "participants": participants,
        }
    else:
        resolver.chats_raw[chat_id] = dict(record.get("raw") or {})
    resolver.chat_types[chat_id] = ctype
    resolver.chats[chat_id] = title


def _management_keyboard(max_chat_id, thread_id: int, *, is_dm: bool = False,
                         include_back: bool = False, back_page: int = 0):
    rows = []
    if thread_id > 0:
        rows.append([InlineKeyboardButton(
            "✅ Telegram-топик подключён",
            callback_data="menu:noop",
        )])
    else:
        rows.append([InlineKeyboardButton(
            "➕ Создать Telegram-топик",
            callback_data=f"menu:bind:{max_chat_id}:{back_page}",
        )])
    if not is_dm:
        rows.append([InlineKeyboardButton(
            "🚪 Выйти из MAX",
            callback_data=f"leave:ask:{thread_id}:{max_chat_id}",
        )])
    if thread_id > 0:
        rows.append([InlineKeyboardButton(
            "🗑 Удалить только Telegram-топик",
            callback_data=f"del:ask:{thread_id}:{max_chat_id}",
        )])
    if include_back:
        rows.append([InlineKeyboardButton(
            "← К списку", callback_data=f"menu:list:{back_page}",
        )])
    return InlineKeyboardMarkup(rows)


def _general_management_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Мои чаты", callback_data="menu:list:0")],
        [InlineKeyboardButton("🔎 Поиск в MAX", callback_data="menu:search")],
    ])


def _quick_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[_QUICK_MENU_TEXT]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="MAX ↔ Telegram",
    )


async def ensure_quick_menu(bot, supergroup_id, topic_store: TopicStore) -> int | None:
    """Install the persistent bottom menu button once for this chat."""
    existing = topic_store.get_ui(_QUICK_MENU_MESSAGE_KEY)
    if existing:
        return int(existing)
    try:
        sent = await bot.send_message(
            chat_id=int(supergroup_id),
            text=(
                "☰ Быстрое меню включено. Кнопка «Меню» теперь всегда доступна "
                "у поля ввода."
            ),
            reply_markup=_quick_menu_keyboard(),
            disable_notification=True,
        )
    except Exception:
        log.exception("Failed to install persistent quick menu")
        return None
    topic_store.set_ui(_QUICK_MENU_MESSAGE_KEY, sent.message_id)
    log.info("Persistent quick menu installed: message_id=%s", sent.message_id)
    return sent.message_id


async def _edit_callback_content(query, text: str, **kwargs) -> None:
    """Edit callback message whether the button lives on text or media.

    Topic intro cards are often photos with captions. Telegram rejects
    edit_message_text for those with "There is no text in the message to edit".
    Fall back to editing the caption so the same management buttons work on
    text-only and photo cards.
    """
    try:
        await query.edit_message_text(text, **kwargs)
        return
    except BadRequest as exc:
        if "there is no text in the message to edit" not in str(exc).lower():
            raise
    await query.edit_message_caption(caption=text, **kwargs)


async def _send_general_notice(context, text: str, *, reply_markup=None) -> None:
    """Send an administrative lifecycle confirmation to General."""
    supergroup_id = context.bot_data[SUPERGROUP_KEY]
    await context.bot.send_message(
        chat_id=int(supergroup_id),
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def ensure_management_panel(bot, supergroup_id, topic_store: TopicStore) -> int | None:
    """Ensure a visible, pinned management panel exists in General."""
    chat_id = int(supergroup_id)
    message_id = topic_store.get_ui("management_panel_message_id")
    markup = _general_management_keyboard()

    panel_text = (
        "<b>MAX ↔ Telegram</b>\n"
        "Здесь можно открыть свои чаты, найти человека/группу/канал "
        "и управлять Telegram-топиками без команд и chat_id."
    )

    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=panel_text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return int(message_id)
        except BadRequest as exc:
            text = str(exc).lower()
            if "message is not modified" in text:
                return int(message_id)
            log.info("Management panel needs recreation: %s", exc)
        except Exception:
            log.exception("Could not verify management panel")

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=panel_text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except Exception:
        log.exception("Failed to create management panel")
        return None

    topic_store.set_ui("management_panel_message_id", sent.message_id)
    try:
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception:
        log.exception("Could not pin management panel")

    log.info("Management panel ready: message_id=%s", sent.message_id)
    return sent.message_id


async def _cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open a button-based management menu.

    Inside a bound topic the menu targets that MAX chat directly. In General
    it lists active MAX dialogs, groups and channels without numeric IDs.
    """
    message = update.message
    if message is None or not _is_admin_user(update, context):
        return

    context.user_data[_SEARCH_WAITING_KEY] = False
    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    max_client: MaxClient | None = context.bot_data.get(MAX_CLIENT_KEY)
    if max_client is None:
        await message.reply_text("⚠️ MAX не подключён.", do_quote=False)
        return

    thread_id = message.message_thread_id
    if thread_id is not None and message.is_topic_message:
        max_chat_id = topic_store.chat_for_topic(thread_id)
        if max_chat_id is not None:
            resolver = getattr(max_client, "resolver", None)
            title = resolver.chat_name(max_chat_id) if resolver else str(max_chat_id)
            await message.reply_text(
                f"<b>{escape(title)}</b>\nВыбери действие:",
                parse_mode="HTML",
                reply_markup=_management_keyboard(
                    max_chat_id, thread_id,
                    is_dm=bool(resolver and resolver.is_dm(max_chat_id)),
                ),
                do_quote=False,
            )
            return

    resolver = getattr(max_client, "resolver", None)
    if resolver is None:
        await message.reply_text("⚠️ Список MAX-чатов пока недоступен.", do_quote=False)
        return

    markup, page, page_count, total = _menu_list_markup(
        resolver, topic_store, page=0,
    )
    if total == 0:
        await message.reply_text("Не вижу активных чатов MAX.", do_quote=False)
        return

    await message.reply_text(
        f"<b>MAX-чаты</b> · {total}\n"
        f"Страница {page + 1}/{page_count}. ✅ — Telegram-топик уже создан.",
        parse_mode="HTML",
        reply_markup=markup,
        do_quote=False,
    )



async def _on_quick_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open context-aware management from the persistent reply-keyboard button."""
    message = update.message
    if message is None or not _is_admin_user(update, context):
        return
    await _cmd_menu(update, context)
    try:
        await message.delete()
    except Exception:
        log.debug("Could not delete quick-menu trigger message", exc_info=True)


def _search_prompt_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("← Мои чаты", callback_data="menu:list:0"),
    ]])


async def _run_global_search(context: ContextTypes.DEFAULT_TYPE,
                             query_text: str) -> tuple[list[dict], int, str | None]:
    max_client: MaxClient | None = context.bot_data.get(MAX_CLIENT_KEY)
    if max_client is None:
        return [], 0, "MAX не подключён."
    try:
        resp = await max_client.search_global(query_text, count=_SEARCH_LIMIT)
    except Exception:
        log.exception("MAX global search failed")
        return [], 0, "Не удалось выполнить поиск в MAX. Попробуй ещё раз."
    if resp is None:
        return [], 0, "MAX не ответил вовремя."
    err = resp.get("_max_error") if isinstance(resp, dict) else None
    if err:
        desc = (err.get("localizedMessage") or err.get("message")
                or err.get("error") or "MAX отклонил поиск")
        return [], 0, str(desc)

    records = _normalize_global_search_results(resp, max_client)
    total = resp.get("total", len(records)) if isinstance(resp, dict) else len(records)
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = len(records)
    context.user_data[_SEARCH_RESULTS_KEY] = {
        str(record["chat_id"]): record for record in records
    }
    context.user_data[_SEARCH_QUERY_KEY] = query_text
    return records, total, None


async def _reply_search_results(message, context: ContextTypes.DEFAULT_TYPE,
                                query_text: str) -> None:
    records, total, error = await _run_global_search(context, query_text)
    if error:
        await message.reply_text(
            f"⚠️ {escape(error)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔎 Попробовать ещё", callback_data="search:new"),
            ]]),
        )
        return
    if not records:
        await message.reply_text(
            f"По запросу <b>{escape(query_text)}</b> ничего не нашёл.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔎 Новый поиск", callback_data="search:new"),
                InlineKeyboardButton("← Мои чаты", callback_data="menu:list:0"),
            ]]),
        )
        return

    shown = min(len(records), _SEARCH_LIMIT)
    total_hint = f" из {total}" if total > shown else ""
    await message.reply_text(
        f"<b>Поиск MAX:</b> {escape(query_text)}\n"
        f"Показано {shown}{total_hint}. Выбери результат:",
        parse_mode="HTML",
        reply_markup=_search_results_markup(records, context.bot_data[TOPIC_STORE_KEY]),
        disable_web_page_preview=True,
    )


async def _cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not _is_admin_user(update, context):
        return
    query_text = " ".join(context.args or []).strip()
    if not query_text:
        context.user_data[_SEARCH_WAITING_KEY] = True
        await message.reply_text(
            "<b>Поиск в MAX</b>\n"
            "Напиши в General имя человека, название группы/канала "
            "или @username.",
            parse_mode="HTML",
            reply_markup=_search_prompt_markup(),
        )
        return
    context.user_data[_SEARCH_WAITING_KEY] = False
    await _reply_search_results(message, context, query_text)


async def _on_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not _is_admin_user(update, context):
        return
    if not context.user_data.get(_SEARCH_WAITING_KEY):
        return
    # General can itself carry a forum message_thread_id. Only reject text
    # from a thread that is actually mapped to a MAX chat.
    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    if (
        message.is_topic_message
        and message.message_thread_id is not None
        and topic_store.chat_for_topic(message.message_thread_id) is not None
    ):
        return
    query_text = (message.text or "").strip()
    if not query_text:
        return
    context.user_data[_SEARCH_WAITING_KEY] = False
    await _reply_search_results(message, context, query_text)


def _search_record_keyboard(record: dict, topic_store: TopicStore,
                            resolver=None) -> InlineKeyboardMarkup:
    chat_id = record["chat_id"]
    thread_id = topic_store.get_topic(chat_id)
    rows = []
    if thread_id:
        rows.append([InlineKeyboardButton(
            "✅ Telegram-топик подключён", callback_data="menu:noop",
        )])
    else:
        active = bool(
            resolver
            and chat_id in resolver.chats_raw
            and (resolver.chats_raw.get(chat_id) or {}).get("status")
            not in ("LEFT", "CLOSED")
        )
        if record["type"] == "DIALOG" or active:
            action = "➕ Создать Telegram-топик"
        elif record["type"] == "CHANNEL":
            action = "➕ Подписаться и создать топик"
        else:
            action = "➕ Вступить и создать топик"
        rows.append([InlineKeyboardButton(
            action, callback_data=f"search:connect:{chat_id}",
        )])
    rows.append([
        InlineKeyboardButton("← К результатам", callback_data="search:results"),
        InlineKeyboardButton("🔎 Новый поиск", callback_data="search:new"),
    ])
    return InlineKeyboardMarkup(rows)


async def _on_search_callback(update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    await query.answer()
    if not _is_admin_user(update, context):
        return

    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    max_client: MaxClient | None = context.bot_data.get(MAX_CLIENT_KEY)
    resolver = getattr(max_client, "resolver", None) if max_client else None

    if query.data == "search:new":
        context.user_data[_SEARCH_WAITING_KEY] = True
        await query.edit_message_text(
            "<b>Поиск в MAX</b>\n"
            "Напиши в General имя человека, название группы/канала "
            "или @username.",
            parse_mode="HTML",
            reply_markup=_search_prompt_markup(),
        )
        return

    records_by_id = context.user_data.get(_SEARCH_RESULTS_KEY) or {}
    if query.data == "search:results":
        records = list(records_by_id.values())
        query_text = context.user_data.get(_SEARCH_QUERY_KEY) or ""
        if not records:
            await query.edit_message_text(
                "Результаты поиска устарели. Запусти новый поиск.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔎 Новый поиск", callback_data="search:new"),
                ]]),
            )
            return
        await query.edit_message_text(
            f"<b>Поиск MAX:</b> {escape(str(query_text))}\n"
            f"Найдено: {len(records)}. Выбери результат:",
            parse_mode="HTML",
            reply_markup=_search_results_markup(records, topic_store),
        )
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "search" or parts[1] not in ("open", "connect"):
        return
    record = records_by_id.get(parts[2])
    if not record:
        await query.edit_message_text(
            "Результат поиска устарел. Запусти поиск ещё раз.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔎 Новый поиск", callback_data="search:new"),
            ]]),
        )
        return

    chat_id = record["chat_id"]
    if parts[1] == "open":
        type_label = {
            "DIALOG": "Человек",
            "CHAT": "Группа",
            "CHANNEL": "Канал",
        }.get(record["type"], record["type"])
        lines = [
            f"<b>{escape(str(record['title']))}</b>",
            escape(str(type_label)),
        ]
        if record.get("link"):
            lines.append(f"🔗 {escape(str(record['link']))}")
        if topic_store.get_topic(chat_id):
            lines.append("✅ Telegram-топик уже подключён")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=_search_record_keyboard(record, topic_store, resolver),
            disable_web_page_preview=True,
        )
        return

    if max_client is None:
        await _edit_callback_content(query, "⚠️ MAX не подключён.")
        return

    # Public groups/channels found outside the current snapshot first need to
    # be opened/joined in MAX. For DIALOG the chat id is deterministic and
    # sending the first message will create/use the conversation directly.
    active = bool(
        resolver
        and chat_id in resolver.chats_raw
        and (resolver.chats_raw.get(chat_id) or {}).get("status")
        not in ("LEFT", "CLOSED")
    )
    if record["type"] != "DIALOG" and not active:
        link = record.get("link")
        if not link:
            await query.edit_message_text(
                "⚠️ У этого результата MAX не отдал публичную ссылку, "
                "поэтому автоматически вступить не могу.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("← К результатам", callback_data="search:results"),
                ]]),
            )
            return
        try:
            resp = await max_client.open_by_link(link)
        except Exception:
            log.exception("search connect: open_by_link failed")
            await query.edit_message_text(
                "⚠️ Не удалось подключить этот чат/канал в MAX. Попробуй ещё раз."
            )
            return
        err = (resp or {}).get("_max_error")
        if err:
            desc = (err.get("localizedMessage") or err.get("message")
                    or err.get("error") or "MAX отказал")
            await query.edit_message_text(f"⚠️ MAX: {escape(str(desc))}",
                                          parse_mode="HTML")
            return
        if not resp:
            await query.edit_message_text(
                "⚠️ MAX не подтвердил подключение. Попробуй ещё раз."
            )
            return
        if isinstance(resp.get("chat"), dict):
            returned_chat = resp["chat"]
            record["raw"] = returned_chat
            record["title"] = str(
                returned_chat.get("title") or record["title"]
            )
            record["link"] = returned_chat.get("link") or record.get("link") or ""

    _cache_search_record_in_resolver(max_client, record)
    try:
        thread_id, title, created = await _create_topic_from_menu(
            context, max_client, topic_store, chat_id,
            title_override=record["title"],
        )
    except Exception:
        log.exception("search connect: create_forum_topic failed")
        await query.edit_message_text(
            "⚠️ Не удалось создать Telegram-топик. Попробуй ещё раз."
        )
        return

    prefix = ("✅ Подключено. Telegram-топик создан."
              if created else "✅ Этот Telegram-топик уже подключён.")
    await query.edit_message_text(
        f"{prefix}\n\n<b>{escape(title)}</b>\n"
        "Теперь переписка доступна через созданный Telegram-топик.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("← К результатам", callback_data="search:results"),
            InlineKeyboardButton("💬 Мои чаты", callback_data="menu:list:0"),
        ]]),
    )


async def _create_topic_from_menu(context: ContextTypes.DEFAULT_TYPE,
                                  max_client: MaxClient,
                                  topic_store: TopicStore,
                                  max_chat_id,
                                  title_override: str | None = None) -> tuple[int, str, bool]:
    resolver = getattr(max_client, "resolver", None)
    title = title_override
    if not title:
        title = _menu_chat_title(resolver, max_chat_id) if resolver else str(max_chat_id)
    title = (title or str(max_chat_id))[:128]
    supergroup_id = int(context.bot_data[SUPERGROUP_KEY])

    existing = topic_store.get_topic(max_chat_id)
    if existing:
        try:
            await context.bot.edit_forum_topic(
                chat_id=supergroup_id,
                message_thread_id=existing,
                name=title,
            )
            return existing, title, False
        except BadRequest as exc:
            error = str(exc).lower()
            if "message thread not found" not in error and "message_thread_not_found" not in error:
                return existing, title, False
            topic_store.remove(max_chat_id)
        except Exception:
            log.exception("menu bind: could not verify existing topic")
            return existing, title, False

    topic = await context.bot.create_forum_topic(
        chat_id=supergroup_id,
        name=title,
    )
    thread_id = topic.message_thread_id
    topic_store.set_topic(max_chat_id, thread_id, title)
    asyncio.create_task(post_topic_intro(
        context.bot, supergroup_id, max_client, max_chat_id, thread_id,
    ))
    return thread_id, title, True


async def _on_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    await query.answer()
    if not _is_admin_user(update, context):
        return

    max_client: MaxClient | None = context.bot_data.get(MAX_CLIENT_KEY)
    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    resolver = getattr(max_client, "resolver", None) if max_client else None

    if query.data == "menu:noop":
        return

    if query.data == "menu:search":
        context.user_data[_SEARCH_WAITING_KEY] = True
        await query.edit_message_text(
            "<b>Поиск в MAX</b>\n"
            "Напиши в General имя человека, название группы/канала "
            "или @username.",
            parse_mode="HTML",
            reply_markup=_search_prompt_markup(),
        )
        return

    if query.data == "menu:list" or query.data.startswith("menu:list:"):
        context.user_data[_SEARCH_WAITING_KEY] = False
        if resolver is None:
            await query.edit_message_text("⚠️ Список MAX-чатов пока недоступен.")
            return
        try:
            page = int(query.data.split(":")[2]) if query.data.count(":") == 2 else 0
        except ValueError:
            page = 0
        markup, page, page_count, total = _menu_list_markup(
            resolver, topic_store, page=page,
        )
        if total == 0:
            await query.edit_message_text("Не вижу активных чатов MAX.")
            return
        try:
            await query.edit_message_text(
                f"<b>MAX-чаты</b> · {total}\n"
                f"Страница {page + 1}/{page_count}. ✅ — Telegram-топик уже создан.",
                parse_mode="HTML",
                reply_markup=markup,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        return

    parts = query.data.split(":")
    if len(parts) != 4 or parts[0] != "menu" or parts[1] not in ("chat", "bind"):
        return
    try:
        max_chat_id = int(parts[2])
        back_page = int(parts[3])
    except ValueError:
        return

    if resolver is None:
        await query.edit_message_text("⚠️ Список MAX-чатов пока недоступен.")
        return

    if parts[1] == "bind":
        if max_client is None:
            await _edit_callback_content(query, "⚠️ MAX не подключён.")
            return
        try:
            thread_id, title, created = await _create_topic_from_menu(
                context, max_client, topic_store, max_chat_id,
            )
        except Exception:
            log.exception("menu bind: create_forum_topic failed")
            await query.edit_message_text(
                f"⚠️ Не удалось создать Telegram-топик: {escape(str(exc))}",
                parse_mode="HTML",
            )
            return
        prefix = "✅ Telegram-топик создан." if created else "✅ Telegram-топик уже подключён."
    else:
        title = _menu_chat_title(resolver, max_chat_id)
        thread_id = topic_store.get_topic(max_chat_id) or 0
        prefix = None

    ctype = resolver.chat_types.get(max_chat_id) or "?"
    type_label = {
        "DIALOG": "Личный диалог",
        "CHAT": "Группа",
        "CHANNEL": "Канал",
    }.get(ctype, str(ctype))
    topic_status = "✅ Telegram-топик подключён" if thread_id else "Telegram-топик не создан"
    header = f"{prefix}\n\n" if prefix else ""
    await query.edit_message_text(
        header
        + f"<b>{escape(title)}</b> · {escape(type_label)}\n"
        + topic_status
        + "\n\nВыбери действие:",
        parse_mode="HTML",
        reply_markup=_management_keyboard(
            max_chat_id, thread_id,
            is_dm=resolver.is_dm(max_chat_id),
            include_back=True,
            back_page=back_page,
        ),
    )


HELP_TEXT = (
    "<b>max2tg — мост MAX ↔ Telegram</b>\n\n"
    "Команды в супергруппе:\n"
    "• <code>/menu</code> — открыть кнопочное меню управления текущим "
    "MAX-чатом или выбрать чат по названию из General.\n"
    "• <code>/search [запрос]</code> — найти в MAX человека, группу или "
    "канал; без запроса включит режим поиска через General.\n"
    "• <code>/bind &lt;chat_id или URL&gt; [название]</code> — привязать "
    "новый топик к чату MAX.\n"
    "• <code>/add &lt;https://max.ru/join/...&gt;</code> — открыть "
    "групповую/канальную ссылку MAX, создать топик и поставить карточку.\n"
    "• <code>/profile</code> — внутри топика: показать профиль собеседника "
    "из MAX (имя, id, аватар).\n"
    "• <code>/intro</code> — перепостить и закрепить карточку профиля "
    "в текущем топике (полезно после смены аватара).\n"
    "• <code>/del</code> — удалить только Telegram-топик и локальную связь "
    "(в MAX останешься).\n"
    "• <code>/leave [chat_id]</code> — выйти из группы/канала MAX; "
    "без аргумента работает внутри топика, с chat_id — даже из General.\n"
    "• <code>/help</code> — эта справка.\n\n"
    "Просто пиши в любом привязанном топике — сообщение уйдёт в "
    "соответствующий чат MAX. Поддерживается жирный/курсив/зачёркнутый/"
    "подчёркнутый текст, моноширинный код, цитаты и ссылки. Фото, "
    "документы и видео тоже передаются. Голосовые приходят как .ogg "
    "файл (пока MAX не вернул нам опкод нативной загрузки).\n\n"
    "Если кто-то новый пишет тебе в MAX — топик создастся автоматически "
    "и в нём сразу появится карточка собеседника."
)


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    await message.reply_text(HELP_TEXT, parse_mode="HTML",
                              disable_web_page_preview=True)


async def _cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask the user to confirm deletion of the current topic. The actual
    deletion happens in ``_on_del_callback`` when the inline button is
    pressed."""
    message = update.message
    if message is None:
        return

    if not _is_admin_user(update, context):
        return

    target = _resolve_topic_target(update, context)
    if not target:
        await message.reply_text(
            "Команда работает только внутри топика, связанного с MAX-чатом."
        )
        return
    _, max_chat_id, _ = target
    thread_id = message.message_thread_id

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑 Удалить топик",
                                 callback_data=f"del:ok:{thread_id}:{max_chat_id}"),
            InlineKeyboardButton("Отмена", callback_data="del:cancel"),
        ]
    ])
    await message.reply_text(
        "Удалить этот топик вместе со всеми сообщениями и снять связь "
        f"с MAX-чатом <code>{max_chat_id}</code>?\n\n"
        "Восстановить нельзя. Новый топик создастся, если собеседник снова "
        "тебе напишет.",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def _on_del_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    await query.answer()

    if not _is_admin_user(update, context):
        return

    parts = query.data.split(":")
    if parts[:2] == ["del", "cancel"]:
        try:
            await _edit_callback_content(query, "Отменено.")
        except Exception:
            pass
        return

    if len(parts) != 4 or parts[0] != "del":
        return
    try:
        thread_id = int(parts[2])
    except ValueError:
        return
    try:
        max_chat_id: int | str = int(parts[3])
    except ValueError:
        max_chat_id = parts[3]

    if parts[1] == "ask":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🗑 Да, удалить топик",
                callback_data=f"del:ok:{thread_id}:{max_chat_id}",
            ),
            InlineKeyboardButton("Отмена", callback_data="del:cancel"),
        ]])
        await _edit_callback_content(
            query,
            "Удалить Telegram-топик и локальную связь?\n\n"
            "Из MAX-чата/канала ты при этом не выйдешь.",
            reply_markup=kb,
        )
        return

    if parts[1] != "ok":
        return

    supergroup_id = context.bot_data[SUPERGROUP_KEY]
    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]

    # Remove mapping first — even if delete_forum_topic fails the stale link
    # is gone, and a fresh topic can be made via /bind.
    topic_store.remove(max_chat_id)

    try:
        await context.bot.delete_forum_topic(
            chat_id=int(supergroup_id), message_thread_id=thread_id,
        )
    except Exception:
        log.exception("/del: delete_forum_topic failed")
        try:
            await _edit_callback_content(
                query,
                "⚠️ Связь с MAX снята, но Telegram не удалил топик. "
                "Можно создать новый топик через «Мои чаты»."
            )
        except Exception:
            pass
        return

    log.info("/del: removed topic thread=%s for max_chat_id=%s",
             thread_id, max_chat_id)
    try:
        await _send_general_notice(
            context,
            "✅ Telegram-топик удалён. В MAX чат/канал остался подключён.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 Мои чаты", callback_data="menu:list:0"),
            ]]),
        )
    except Exception:
        log.exception("/del: topic deleted, but General confirmation failed")


async def _cmd_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for confirmation before leaving a MAX group/channel.

    Usage:
      /leave                  — inside a bound topic
      /leave <chat_id-or-url> — anywhere in the bridge supergroup
    """
    message = update.message
    if message is None or not _is_admin_user(update, context):
        return

    max_client: MaxClient | None = context.bot_data.get(MAX_CLIENT_KEY)
    if not max_client:
        await message.reply_text("⚠️ MAX не подключён.")
        return

    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    args = context.args or []

    if args:
        max_chat_id = _parse_max_chat_id(args[0])
        if max_chat_id is None:
            await message.reply_text(
                "Не понял chat_id. Пример: <code>/leave -75107924425434</code>",
                parse_mode="HTML",
            )
            return
        thread_id = topic_store.get_topic(max_chat_id) or 0
    else:
        target = _resolve_topic_target(update, context)
        if not target:
            await message.reply_text(
                "Запусти <code>/leave</code> внутри топика или "
                "<code>/leave &lt;chat_id&gt;</code> из General.",
                parse_mode="HTML",
            )
            return
        _, max_chat_id, _ = target
        thread_id = message.message_thread_id or topic_store.get_topic(max_chat_id) or 0

    resolver = getattr(max_client, "resolver", None)
    if resolver is not None and resolver.is_dm(max_chat_id):
        await message.reply_text("Из личного диалога выйти нельзя — используй /del.")
        return

    title = resolver.chat_name(max_chat_id) if resolver is not None else str(max_chat_id)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚪 Выйти из MAX",
                callback_data=f"leave:ok:{thread_id}:{max_chat_id}",
            ),
            InlineKeyboardButton("Отмена", callback_data="leave:cancel"),
        ]
    ])
    await message.reply_text(
        f"Выйти из <b>{escape(title)}</b> в MAX и удалить этот Telegram-топик?\n\n"
        "Это уже реальный выход из группы/канала MAX.",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def _on_leave_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    await query.answer()

    if not _is_admin_user(update, context):
        return

    parts = query.data.split(":")
    if parts[:2] == ["leave", "cancel"]:
        try:
            await _edit_callback_content(query, "Отменено.")
        except Exception:
            pass
        return

    if len(parts) != 4 or parts[0] != "leave":
        return

    try:
        thread_id = int(parts[2])
        max_chat_id: int | str = int(parts[3])
    except ValueError:
        return

    if parts[1] == "ask":
        max_client: MaxClient | None = context.bot_data.get(MAX_CLIENT_KEY)
        resolver = getattr(max_client, "resolver", None) if max_client else None
        title = resolver.chat_name(max_chat_id) if resolver else str(max_chat_id)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🚪 Да, выйти из MAX",
                callback_data=f"leave:ok:{thread_id}:{max_chat_id}",
            ),
            InlineKeyboardButton("Отмена", callback_data="leave:cancel"),
        ]])
        await _edit_callback_content(
            query,
            f"Выйти из <b>{escape(title)}</b> в MAX"
            + (" и удалить Telegram-топик?" if thread_id > 0 else "?"),
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    if parts[1] != "ok":
        return

    max_client: MaxClient | None = context.bot_data.get(MAX_CLIENT_KEY)
    if max_client is None:
        try:
            await _edit_callback_content(query, "⚠️ MAX не подключён.")
        except Exception:
            pass
        return

    try:
        resp = await max_client.leave_chat(max_chat_id)
    except Exception:
        log.exception("/leave: MAX leave failed")
        try:
            await _edit_callback_content(query, "⚠️ Не удалось выйти из MAX. Попробуй ещё раз.")
        except Exception:
            pass
        return

    if resp is None:
        try:
            await _edit_callback_content(
                query,
                "⚠️ MAX не ответил вовремя. Выход не подтверждён — обнови «Мои чаты» перед повторной попыткой."
            )
        except Exception:
            pass
        return

    err = resp.get("_max_error")
    if err:
        desc = (
            err.get("localizedMessage")
            or err.get("message")
            or err.get("error")
            or "MAX отклонил выход"
        )
        try:
            await _edit_callback_content(query, f"⚠️ MAX: {escape(str(desc))}", parse_mode="HTML")
        except Exception:
            pass
        return

    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    resolver = getattr(max_client, "resolver", None)
    title = resolver.chat_name(max_chat_id) if resolver is not None else str(max_chat_id)
    topic_store.remove(max_chat_id)

    if resolver is not None:
        resolver.chats.pop(max_chat_id, None)
        resolver.chat_types.pop(max_chat_id, None)
        resolver.chats_raw.pop(max_chat_id, None)

    supergroup_id = context.bot_data[SUPERGROUP_KEY]
    if thread_id > 0:
        try:
            await context.bot.delete_forum_topic(
                chat_id=int(supergroup_id),
                message_thread_id=thread_id,
            )
        except BadRequest as exc:
            if "message thread not found" not in str(exc).lower():
                log.exception("/leave: MAX left, but Telegram topic deletion failed")
        except Exception:
            log.exception("/leave: MAX left, but Telegram topic deletion failed")

    log.info("/leave: left MAX chat=%s and removed topic thread=%s",
             max_chat_id, thread_id or None)
    success_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Мои чаты", callback_data="menu:list:0"),
    ]])
    try:
        if thread_id > 0:
            await _send_general_notice(
                context,
                f"✅ Вышли из <b>{escape(str(title))}</b> в MAX. Telegram-топик удалён.",
                reply_markup=success_markup,
            )
        else:
            await _edit_callback_content(
                query,
                f"✅ Вышли из <b>{escape(str(title))}</b> в MAX.",
                parse_mode="HTML",
                reply_markup=success_markup,
            )
    except Exception:
        log.exception("/leave: left successfully, but confirmation failed")


async def _cmd_intro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-publish the pinned profile card in the current topic.

    Useful for topics that were created before this feature existed, or to
    refresh the card after the contact updated their photo/name.
    """
    target = _resolve_topic_target(update, context)
    message = update.message
    if message is None:
        return
    if not target:
        await message.reply_text(
            "Команда работает только внутри топика, связанного с чатом MAX."
        )
        return
    _, max_chat_id, max_client = target
    if not max_client:
        await message.reply_text("⚠️ MAX не подключён.")
        return
    supergroup_id = context.bot_data[SUPERGROUP_KEY]
    await post_topic_intro(
        context.bot, supergroup_id, max_client, max_chat_id,
        message.message_thread_id,
    )


async def _cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show profile of the Max peer linked to the current topic."""
    target = _resolve_topic_target(update, context)
    message = update.message
    if message is None:
        return
    if not target:
        await message.reply_text(
            "Команда работает только внутри топика, связанного с чатом MAX."
        )
        return
    _, max_chat_id, max_client = target
    if not max_client:
        await message.reply_text("⚠️ MAX не подключён.")
        return

    resolver = getattr(max_client, "resolver", None)
    if resolver is None:
        await message.reply_text("⚠️ Кеш контактов недоступен.")
        return

    is_dm = resolver.is_dm(max_chat_id)
    if not is_dm:
        # Group / channel: show whatever the snapshot exposes.
        chat = resolver.chats_raw.get(max_chat_id) or {}
        title = chat.get("title") or resolver.chat_name(max_chat_id)
        ctype = chat.get("type") or "?"
        participants = chat.get("participants") or {}
        descr = chat.get("description") or ""
        link = chat.get("link") or ""
        parts = [f"<b>{escape(str(title))}</b> · {escape(str(ctype))}"]
        parts.append(f"Участников: <b>{len(participants)}</b>")
        if descr:
            parts.append(f"\n{escape(str(descr))}")
        if link:
            parts.append(f"\n🔗 {escape(str(link))}")
        await message.reply_text("\n".join(parts), parse_mode="HTML")
        return

    peer_id = _peer_id_in_dm(resolver, max_chat_id)
    if peer_id is None:
        await message.reply_text("Не нашёл собеседника в этом чате.")
        return

    contact = resolver.contacts_raw.get(peer_id)
    if contact is None:
        # Probe several payload shapes / opcodes so we can see in the log
        # what MAX is willing to return for a peer that's not in contacts.
        probes = [
            (32, {"contactIds": [peer_id]}),
            (32, {"contactIds": [str(peer_id)]}),
            (32, {"userIds": [peer_id]}),
            (32, {"userId": peer_id}),
            (35, {"contactIds": [peer_id]}),   # CONTACT_PRESENCE
            (33, {"contactIds": [peer_id]}),
            (36, {"contactIds": [peer_id]}),
        ]
        for op, payload in probes:
            try:
                resp = await max_client.cmd(op, payload)
            except Exception:
                log.exception("/profile probe op=%d failed", op)
                continue
            log.info("/profile probe op=%d payload=%s → %s",
                     op, payload, str(resp)[:600])
            if resp and "_max_error" not in resp:
                # Let the resolver opportunistically pick up new fields.
                resolver._parse_contacts_response(resp)
                if peer_id in resolver.contacts_raw:
                    break
        contact = resolver.contacts_raw.get(peer_id)

    if contact is None:
        # MAX won't share extended profile for users that aren't in your
        # contact list. Show whatever we already know.
        name = resolver.users.get(peer_id)
        if name:
            await message.reply_text(
                f"<b>{escape(str(name))}</b>\nid: <code>{peer_id}</code>\n\n"
                "<i>MAX не отдал расширенный профиль для этого собеседника "
                "(скорее всего, он не у тебя в контактах).</i>",
                parse_mode="HTML",
            )
            return
        await message.reply_text(
            f"Не удалось получить профиль из MAX. id: <code>{peer_id}</code>",
            parse_mode="HTML",
        )
        return

    log.info("/profile contact raw fields: %s", list(contact.keys()))

    name = resolver.user_name(peer_id)
    phone = contact.get("phone") or ""
    about = (contact.get("description") or contact.get("about")
             or contact.get("status") or "")
    username = contact.get("link") or contact.get("username") or ""

    lines = [f"<b>{escape(str(name))}</b>",
             f"id: <code>{peer_id}</code>"]
    if phone:
        lines.append(f"📞 <code>{escape(str(phone))}</code>")
    if username:
        lines.append(f"🔗 {escape(str(username))}")
    if about:
        lines.append(f"\n{escape(str(about))}")
    body = "\n".join(lines)

    # Find a photo if any. MAX puts the avatar URL at the top level of the
    # contact dict as `baseUrl` / `baseRawUrl`, sometimes also wrapped in a
    # nested photo/avatar dict.
    photo_url = None
    photo_obj = contact.get("photo") or contact.get("avatar")
    if isinstance(photo_obj, dict):
        photo_url = (photo_obj.get("baseUrl") or photo_obj.get("url")
                     or photo_obj.get("photoUrl"))
    photo_url = (photo_url
                 or contact.get("baseUrl")
                 or contact.get("baseRawUrl")
                 or contact.get("photoUrl")
                 or contact.get("baseRawIconUrl"))

    if photo_url:
        data = await max_client.download_file(photo_url)
        if data:
            try:
                await context.bot.send_photo(
                    chat_id=message.chat_id,
                    photo=data,
                    caption=body,
                    parse_mode="HTML",
                    message_thread_id=message.message_thread_id,
                )
                return
            except Exception:
                log.exception("send_photo failed in /profile")

    await message.reply_text(body, parse_mode="HTML")


def build_tg_app(token: str, max_client: MaxClient, supergroup_id: str,
                 topic_store: TopicStore, allowed_user_id: int | None = None,
                 allowed_user_ids: set[int] | frozenset[int] | None = None,
                 admin_user_id: int | None = None,
                 proxy_url: str | None = None) -> Application:
    """Build the Telegram Application that routes topic replies back to Max."""
    builder = Application.builder().token(token)
    if proxy_url:
        builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
    app = builder.build()
    app.bot_data[MAX_CLIENT_KEY] = max_client
    app.bot_data[TOPIC_STORE_KEY] = topic_store
    app.bot_data[ALLOWED_USER_KEY] = int(allowed_user_id) if allowed_user_id else None
    allowed = {int(uid) for uid in (allowed_user_ids or set())}
    if allowed_user_id:
        allowed.add(int(allowed_user_id))
    if admin_user_id:
        allowed.add(int(admin_user_id))
    app.bot_data[ALLOWED_USERS_KEY] = frozenset(allowed)
    app.bot_data[ADMIN_USER_KEY] = int(admin_user_id) if admin_user_id else None
    app.bot_data[SUPERGROUP_KEY] = int(supergroup_id)

    chat_filter = filters.Chat(chat_id=int(supergroup_id))
    quick_menu_filter = filters.Regex(r"^☰ Меню$")
    app.add_handler(CommandHandler("menu", _cmd_menu, filters=chat_filter))
    app.add_handler(CommandHandler("search", _cmd_search, filters=chat_filter))
    app.add_handler(CommandHandler("bind", _cmd_bind, filters=chat_filter))
    app.add_handler(CommandHandler("add", _cmd_add, filters=chat_filter))
    app.add_handler(CommandHandler("profile", _cmd_profile, filters=chat_filter))
    app.add_handler(CommandHandler("intro", _cmd_intro, filters=chat_filter))
    app.add_handler(CommandHandler("del", _cmd_del, filters=chat_filter))
    app.add_handler(CommandHandler("leave", _cmd_leave, filters=chat_filter))
    app.add_handler(CommandHandler("help", _cmd_help, filters=chat_filter))
    app.add_handler(CallbackQueryHandler(_on_menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(_on_search_callback, pattern=r"^search:"))
    app.add_handler(CallbackQueryHandler(_on_del_callback, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(_on_leave_callback, pattern=r"^leave:"))
    # The persistent menu button sends its label as a normal Telegram message.
    # Consume it before search/forwarding, then delete the trigger message.
    app.add_handler(
        MessageHandler(quick_menu_filter & chat_filter, _on_quick_menu_button),
        group=-1,
    )
    # Search capture runs in a separate earlier handler group so ordinary
    # topic messages still continue to the MAX forwarding handler in group 0.
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~quick_menu_filter & chat_filter,
            _on_search_input,
        ),
        group=-1,
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~quick_menu_filter & chat_filter,
            _on_topic_message,
        )
    )
    media_filter = (
        filters.PHOTO | filters.VOICE | filters.AUDIO
        | filters.Document.ALL | filters.VIDEO
    )
    app.add_handler(
        MessageHandler(media_filter & chat_filter, _on_topic_media)
    )

    return app
