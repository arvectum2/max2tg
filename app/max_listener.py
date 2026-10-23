import asyncio
import logging
import time
from datetime import datetime
from html import escape

from app.max_client import MaxClient, MaxMessage
from app.resolver import ContactResolver
from app.tg_sender import TelegramSender
from app.outbox import OutboxStore, current_delivery
from app.max_formatting import format_max_html
from app.max_media import (
    photo_url, file_url, video_url, video_preview_url, first_media_url,
    media_filename, media_size, duration_seconds,
)

log = logging.getLogger(__name__)

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_ACTIVE_CHAT_TYPES = {"DIALOG", "CHAT", "CHANNEL"}
_KNOWN_MAX_CHATS_UI_KEY = "known_max_chat_ids"
CATCH_UP_BATCH = 100
CATCH_UP_MAX_PAGES = 10  # 1000 events max per chat per reconciliation
_MAX_SERVICE_PEER_IDS = {543835}


def _header(msg: MaxMessage, sender_label: str, chat_label: str, is_dm: bool) -> str:
    if is_dm:
        return f"✉ <b>{sender_label}</b>"
    if not sender_label:
        return f"📢 <b>{chat_label}</b>"
    return f"💬 <b>{chat_label}</b> | {sender_label}"


def _extract_photo_url(attach: dict) -> str | None:
    return photo_url(attach)


def _extract_file_url(attach: dict) -> str | None:
    return file_url(attach)


async def _get_media(client: MaxClient, url: str,
                     methods: tuple[str, ...]) -> bytes | None:
    """Prefer durable media bytes from an unfinished Telegram outbox part."""
    scope = current_delivery()
    if scope is not None:
        cached = scope.cached_media(methods)
        if cached is not None:
            return cached
    return await client.download_file(url)


def _guess_media_kind(filename: str) -> str:
    name_lower = filename.lower()
    for ext in PHOTO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "photo"
    for ext in VIDEO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "video"
    return "document"


async def _send_attach(
    attach: dict,
    client: MaxClient,
    sender: TelegramSender,
    header_text: str,
    thread_id: int | None = None,
    msg: MaxMessage | None = None,
    reply_to_message_id: int | None = None,
):
    """Process and send one attachment. Returns the Telegram Message, if sent."""
    atype = attach.get("_type", "")
    log.info("Processing attach _type=%s keys=%s", atype, list(attach.keys()))

    if atype in ("CONTROL", "WIDGET", "INLINE_KEYBOARD"):
        return None

    if atype == "UNSUPPORTED" and attach.get("audioId") is not None:
        audio_id = attach.get("audioId")
        token = attach.get("token")
        duration = attach.get("duration", 0)
        chat_id = msg.chat_id if msg else None
        message_id = msg.message_id if msg else None
        url = None
        if chat_id is not None and message_id:
            url = await client.download_audio_url(
                audio_id, chat_id, message_id, token=token,
            )
        if url:
            data = await _get_media(client, url, ("send_voice", "send_audio"))
            if data:
                return await sender.send_voice(
                    data, caption=header_text, message_thread_id=thread_id,
                    reply_to_message_id=reply_to_message_id,
                )
        dur_s = f" ({duration_seconds(attach)}с)" if duration else ""
        return await sender.send(
            f"{header_text}\n🎙 <i>[голосовое сообщение{dur_s} — не удалось скачать]</i>",
            message_thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    if atype == "PHOTO":
        url = _extract_photo_url(attach)
        if not url:
            log.warning("PHOTO attachment lacks a downloadable URL")
            return await sender.send(
                f"{header_text}\n<i>[фото — ссылка недоступна]</i>",
                message_thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
            )
        data = await _get_media(client, url, ("send_photo",))
        if data:
            return await sender.send_photo(
                data, caption=header_text, message_thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
            )
        return await sender.send(
            f"{header_text}\n<i>[фото — не удалось загрузить]</i>",
            message_thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    if atype == "VIDEO":
        original_url = video_url(attach)
        if original_url:
            data = await _get_media(
                client, original_url, ("send_video", "send_document"))
            if data:
                result = await sender.send_video(
                    data, caption=header_text, filename="video.mp4",
                    message_thread_id=thread_id,
                    reply_to_message_id=reply_to_message_id,
                )
                if result is not None:
                    return result
                # A failed/ambiguous API call must be handled by the outbox,
                # never disguised as a successful preview delivery.
                return None
        thumb = video_preview_url(attach)
        if thumb:
            data = await _get_media(client, thumb, ("send_photo",))
            if data:
                return await sender.send_photo(
                    data, caption=f"{header_text}\n<i>[видео — только превью; "
                                  "оригинал недоступен]</i>",
                    message_thread_id=thread_id,
                    reply_to_message_id=reply_to_message_id,
                )
        return await sender.send(
            f"{header_text}\n<i>[видео — исходный файл недоступен]</i>",
            message_thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    if atype == "FILE":
        name = media_filename(attach)
        size = media_size(attach)
        token_url = _extract_file_url(attach)
        if token_url:
            data = await _get_media(
                client, token_url, ("send_photo", "send_video", "send_document"))
            if data:
                kind = _guess_media_kind(name)
                # Telegram Bot API does not reliably accept GIF/WEBP/BMP
                # through sendPhoto or MOV/MKV/AVI through sendVideo.
                # Preserve original bytes and filename as a document instead.
                ext = name.lower().rsplit(".",1)[-1] if "." in name else ""
                if ext in ("mp3","m4a","wav","flac","aac","ogg","opus"):
                    return await sender.send_audio(
                        data,caption=header_text,filename=name,
                        message_thread_id=thread_id,
                        reply_to_message_id=reply_to_message_id,
                    )
                if kind == "photo" and ext in ("jpg","jpeg","png"):
                    return await sender.send_photo(
                        data, caption=header_text, filename=name,
                        message_thread_id=thread_id,
                        reply_to_message_id=reply_to_message_id,
                    )
                if kind == "video" and ext=="mp4":
                    return await sender.send_video(
                        data, caption=header_text, filename=name,
                        message_thread_id=thread_id,
                        reply_to_message_id=reply_to_message_id,
                    )
                return await sender.send_document(
                    data, caption=header_text, filename=name,
                    message_thread_id=thread_id,
                    reply_to_message_id=reply_to_message_id,
                )
        size_str = f" ({_human_size(size)})" if size else ""
        return await sender.send(
            f"{header_text}\n📎 <b>{escape(name)}</b>{size_str}"
            "\n<i>[исходный файл недоступен для скачивания]</i>",
            message_thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    if atype == "AUDIO":
        url = first_media_url(attach,"url","fileUrl")
        if not url and attach.get("audioId") is not None and msg is not None:
            url = await client.download_audio_url(
                attach["audioId"], msg.chat_id, msg.message_id,
                token=attach.get("token"))
        if url:
            data = await _get_media(client, url, ("send_voice", "send_audio"))
            if data:
                return await sender.send_voice(
                    data, caption=header_text, message_thread_id=thread_id,
                    reply_to_message_id=reply_to_message_id,
                )
        return await sender.send(
            f"{header_text}\n<i>[аудио]</i>", message_thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    if atype == "STICKER":
        url = attach.get("url")
        if url:
            data = await _get_media(client, url, ("send_sticker",))
            if data:
                return await sender.send_sticker(
                    data, message_thread_id=thread_id,
                    reply_to_message_id=reply_to_message_id,
                )
        return await sender.send(
            f"{header_text}\n<i>[стикер]</i>", message_thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    if atype == "SHARE":
        share_url = attach.get("url", "")
        title = attach.get("title", "")
        desc = attach.get("description", "")
        parts = [header_text]
        if title:
            parts.append(f"🔗 <b>{escape(title)}</b>")
        if share_url:
            parts.append(escape(share_url))
        if desc:
            parts.append(f"<i>{escape(desc[:200])}</i>")
        return await sender.send(
            "\n".join(parts), message_thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    if atype == "LOCATION":
        lat = attach.get("lat") if attach.get("lat") is not None else attach.get("latitude")
        lon = next((attach.get(k) for k in ("lon","lng","longitude")
                    if attach.get(k) is not None),None)
        text = f"{header_text}\n📍 {lat}, {lon}" if lat is not None and lon is not None else (
            f"{header_text}\n<i>[геолокация]</i>"
        )
        return await sender.send(
            text, message_thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    if atype == "CONTACT":
        name = attach.get("name", "")
        phone = attach.get("phone", "")
        text = f"{header_text}\n👤 {escape(name)}"
        if phone:
            text += f" — {escape(phone)}"
        return await sender.send(
            text, message_thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )

    log.info("Unknown attach type %s, sending as info", atype)
    return await sender.send(
        f"{header_text}\n<i>[вложение: {escape(atype or 'unknown')}]</i>",
        message_thread_id=thread_id,
        reply_to_message_id=reply_to_message_id,
    )


async def _handle_linked_message(
    link: dict,
    link_type: str,
    header_text: str,
    client: MaxClient,
    sender: TelegramSender,
    resolver: ContactResolver,
    thread_id: int | None = None,
    msg: MaxMessage | None = None,
):
    """Forward nested content, returning its first Telegram message for dedup."""
    inner = link.get("message") or link
    if not isinstance(inner, dict):
        inner = link
    fwd_sender_id = inner.get("sender") or link.get("sender")
    fwd_text = inner.get("text", "") or link.get("text", "")
    fwd_attaches = inner.get("attaches") or link.get("attaches") or []

    sender_label = ""
    if fwd_sender_id:
        sender_label = escape(await resolver.resolve_user(fwd_sender_id))

    if link_type == "FORWARD":
        prefix = "↩️ <b>Переслано</b>"
        if sender_label:
            prefix = f"↩️ <b>Переслано от {sender_label}</b>"
    else:
        prefix = "↩ <b>Ответ</b>"
        if sender_label:
            prefix = f"↩ <b>Ответ на {sender_label}</b>"

    nested = MaxMessage(
        chat_id=msg.chat_id if msg else None,
        message_id=msg.message_id if msg else "",
        text=fwd_text,
        attaches=fwd_attaches,
        raw={"message": inner},
    )
    return await _send_message_content(
        nested, f"{header_text}\n{prefix}", client, sender, thread_id,
    )


def _message_body(msg: MaxMessage) -> str:
    """Include SHARE link previews inside the original text once."""
    raw = msg.text or ""
    elements = ((msg.raw.get("message") or {}).get("elements")
                if isinstance(msg.raw, dict)
                and isinstance(msg.raw.get("message"), dict) else None)
    parts = [format_max_html(raw, elements)] if raw else []
    for att in msg.attaches:
        if not isinstance(att, dict) or att.get("_type") != "SHARE":
            continue
        title = str(att.get("title") or "").strip()
        url = str(att.get("url") or "").strip()
        if not raw and title:
            parts.append(f"🔗 {escape(title)}")
        if url:
            aliases = {url}
            for prefix in ("mailto:", "http://", "https://"):
                if url.lower().startswith(prefix):
                    aliases.add(url[len(prefix):])
            if any(x and x in raw for x in aliases):
                continue
            if not raw and title and title in url:
                continue
            parts.append(escape(url))
    return "\n".join(parts) if parts else "<i>[нетекстовое сообщение]</i>"


async def _send_message_content(
    msg: MaxMessage,
    header_text: str,
    client: MaxClient,
    sender: TelegramSender,
    thread_id: int | None,
    reply_to_message_id: int | None = None,
):
    """Forward the current MAX message and return its first Telegram message."""
    meaningful_attaches = [
        a for a in msg.attaches
        if isinstance(a, dict)
        and a.get("_type") not in ("CONTROL", "WIDGET", "INLINE_KEYBOARD", "SHARE", None)
    ]
    body = _message_body(msg)

    first_sent = None
    if meaningful_attaches:
        text_sent = False
        index = 0
        while index < len(meaningful_attaches):
            attach = meaningful_attaches[index]
            cap = (f"{header_text}\n{body}"
                   if not text_sent and body != "<i>[нетекстовое сообщение]</i>"
                   else header_text)

            # Consecutive PHOTO items from one MAX message become a Telegram
            # album, with each batch limited to Telegram's 2..10 rules.
            group = []
            for other in meaningful_attaches[index:index+10]:
                if other.get("_type") != "PHOTO":
                    break
                group.append(other)
            if len(group) >= 2:
                scope = current_delivery()
                cached = (scope.cached_media(("send_media_group",))
                          if scope else None)
                if cached is not None:
                    pictures = sender._unpack_album(cached)
                else:
                    pictures = []
                    for photo in group:
                        url = photo_url(photo)
                        picture = await client.download_file(url) if url else None
                        if not picture:
                            break
                        pictures.append(picture)
                # When one source image is missing, fall back to individual
                # photos and explicit text markers for the failed item.
                if (len(pictures) == len(group) and
                    sum(map(len,pictures)) <= 45 * 1024 * 1024):
                    result = await sender.send_photo_album(
                        pictures,caption=cap,message_thread_id=thread_id,
                        reply_to_message_id=(
                            reply_to_message_id if first_sent is None else None),
                    )
                    if result is None:
                        return None
                    if first_sent is None:
                        first_sent = result
                    if cap != header_text:
                        text_sent = True
                    index += len(group)
                    continue

            sent = await _send_attach(
                attach, client, sender, cap, thread_id=thread_id, msg=msg,
                reply_to_message_id=reply_to_message_id if first_sent is None else None,
            )
            # Do not mark the parent message SENT if any attachment failed,
            # even if an earlier attachment already reached Telegram.
            # Outbox will skip parts with confirmed IDs on retry.
            if sent is None:
                log.warning("MAX media item not delivered: type=%s",attach.get("_type"))
                return None
            if first_sent is None:
                first_sent = sent
            if cap != header_text:
                text_sent = True
            log.info("Forwarded attachment type=%s to TG",attach.get("_type"))
            index += 1

        if body != "<i>[нетекстовое сообщение]</i>" and not text_sent:
            sent = await sender.send(
                f"{header_text}\n{body}",
                message_thread_id=thread_id,
                reply_to_message_id=reply_to_message_id if first_sent is None else None,
            )
            if sent is None:
                return None
            if first_sent is None:
                first_sent = sent
    else:
        first_sent = await sender.send(
            f"{header_text}\n{body}",
            message_thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )
        log.info("Forwarded text to TG")

    return first_sent


def _remember_message(sender: TelegramSender, msg: MaxMessage, tg_message) -> None:
    tg_message_id = getattr(tg_message, "message_id", None)
    if msg.message_id and tg_message_id is not None:
        active = current_delivery()
        if active is not None:
            active.store.record_mapping(active.key, tg_message_id)
        sender.topic_store.set_message(msg.chat_id, msg.message_id, tg_message_id)


def _history_time_label(timestamp) -> str:
    try:
        value = float(timestamp)
        if value > 10_000_000_000:
            value /= 1000
        return datetime.fromtimestamp(value).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


async def _deliver_history_message(
    msg: MaxMessage, client: MaxClient, sender: TelegramSender,
    resolver: ContactResolver, chat_id, thread_id: int,
) -> bool:
    """Send one imported message, sharing the live path's durable ledger."""
    raw_sender = ""
    if msg.is_self:
        raw_sender = "Вы"
    elif msg.sender_id is not None:
        raw_sender = await resolver.resolve_user(msg.sender_id)
    is_dm = resolver.is_dm(chat_id)
    raw_chat = resolver.chat_name(chat_id)
    header_text = _header(msg, escape(raw_sender), escape(raw_chat), is_dm)
    label = _history_time_label(msg.timestamp)
    if label:
        header_text = f"🕘 <i>{escape(label)}</i>\n{header_text}"

    link = msg.link
    link_type = link.get("type") if isinstance(link, dict) else None
    if link_type == "REPLY":
        inner = link.get("message") or {}
        if not isinstance(inner, dict):
            inner = {}
        replied_max_id = inner.get("id") or link.get("messageId")
        replied_tg_id = (sender.topic_store.tg_for_max_message(chat_id, replied_max_id)
                         if replied_max_id else None)
        if replied_tg_id is not None:
            sent = await _send_message_content(
                msg, header_text, client, sender, thread_id,
                reply_to_message_id=replied_tg_id,
            )
            _remember_message(sender, msg, sent)
            return sent is not None

        source_sender_id = inner.get("sender")
        source_label = ""
        if source_sender_id is not None:
            if client._my_id and str(source_sender_id) == str(client._my_id):
                source_label = "Вы"
            else:
                source_label = escape(await resolver.resolve_user(source_sender_id))
        reply_header = f"{header_text}\n↩ <b>Ответ"
        if source_label:
            reply_header += f" на {source_label}"
        reply_header += "</b>"
        quoted = inner.get("text") or ""
        if quoted:
            reply_header += f"\n<blockquote>{escape(quoted[:700])}</blockquote>"
        elif inner.get("attaches"):
            reply_header += "\n<blockquote>[вложение]</blockquote>"
        sent = await _send_message_content(
            msg, reply_header, client, sender, thread_id,
        )
        _remember_message(sender, msg, sent)
        return sent is not None

    if link_type == "FORWARD":
        forwarded = await _handle_linked_message(
            link, link_type, header_text, client, sender, resolver,
            thread_id=thread_id, msg=msg,
        )
        comment = None
        if msg.text:
            comment = await sender.send(
                f"{header_text}\n{_message_body(msg)}",
                message_thread_id=thread_id,
            )
        _remember_message(sender, msg, comment or forwarded)
        return (comment or forwarded) is not None

    sent = await _send_message_content(
        msg, header_text, client, sender, thread_id,
    )
    _remember_message(sender, msg, sent)
    return sent is not None


async def import_recent_history(
    client: MaxClient,
    sender: TelegramSender,
    resolver: ContactResolver,
    chat_id,
    thread_id: int,
    count: int,
    *,
    raw_messages: list[dict] | None = None,
) -> int:
    """Import ordered history, storing deliveries before their first send."""
    if raw_messages is None:
        try:
            count = max(0, min(int(count), 100))
        except (TypeError, ValueError):
            count = 20
        if count == 0:
            return 0
        try:
            response = await client.fetch_history(chat_id, count=count)
        except Exception:
            log.exception("History fetch failed for chat=%s", chat_id)
            return 0
        if not isinstance(response, dict) or response.get("_max_error"):
            log.warning("History fetch failed for chat=%s", chat_id)
            return 0
        raw_messages = response.get("messages") or []
    raw_messages = [m for m in raw_messages if isinstance(m, dict)]
    raw_messages.sort(key=lambda item: item.get("time") or 0)
    imported = 0

    for body in raw_messages:
        msg = client._parse_message({"chatId": chat_id, "message": body})
        if msg is None or not msg.message_id:
            continue
        if sender.topic_store.tg_for_max_message(chat_id, msg.message_id) is not None:
            continue

        async def deliver():
            return await _deliver_history_message(
                msg, client, sender, resolver, chat_id, thread_id,
            )

        outbox = getattr(sender, "outbox", None)
        if isinstance(outbox, OutboxStore):
            completed = await outbox.process(msg, deliver)
        else:
            completed = await deliver()
        if completed:
            imported += 1

    log.info("Imported %d/%d history messages for chat=%s",
             imported, len(raw_messages), chat_id)
    return imported


def _human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def _peer_id_for_dialog(resolver: ContactResolver, chat_id):
    chat = resolver.chats_raw.get(chat_id) or {}
    participants = chat.get("participants") or {}
    for raw_id in participants:
        try:
            uid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if resolver.my_id is None or uid != int(resolver.my_id):
            return uid
    return None


def _is_max_service_dialog(resolver: ContactResolver, chat_id) -> bool:
    if not resolver.is_dm(chat_id):
        return False
    peer_id = _peer_id_for_dialog(resolver, chat_id)
    if peer_id in _MAX_SERVICE_PEER_IDS:
        return True
    if peer_id is None:
        return False
    return resolver.user_name(peer_id).strip().upper() == "MAX"


async def _topic_title_for_chat(resolver: ContactResolver, chat_id) -> str:
    if resolver.is_dm(chat_id):
        peer_id = _peer_id_for_dialog(resolver, chat_id)
        if peer_id is not None:
            title = await resolver.resolve_user(peer_id)
            if title and title != str(peer_id):
                return title
    title = resolver.chat_name(chat_id)
    return str(title or chat_id)


def _remember_known_chat(sender: TelegramSender, chat_id) -> None:
    raw = sender.topic_store.get_ui(_KNOWN_MAX_CHATS_UI_KEY, []) or []
    known = {str(value) for value in raw}
    key = str(chat_id)
    if key in known:
        return
    known.add(key)
    sender.topic_store.set_ui(_KNOWN_MAX_CHATS_UI_KEY, sorted(known))


async def _refresh_chat_metadata(client: MaxClient, resolver: ContactResolver,
                                 chat_id) -> None:
    if chat_id in resolver.chats_raw:
        return
    response = await client.fetch_chats([chat_id])
    chats = (response or {}).get("chats") or [] if isinstance(response, dict) else []
    participant_ids = resolver.upsert_chats(chats)
    if participant_ids:
        await resolver.resolve_users_batch(participant_ids)


async def _bootstrap_chat_topic(client: MaxClient, sender: TelegramSender,
                                resolver: ContactResolver, chat_id) -> int | None:
    """Create one missing Telegram topic and import its configured history."""
    if sender.topic_store.get_topic(chat_id) is not None:
        _remember_known_chat(sender, chat_id)
        return sender.topic_store.get_topic(chat_id)
    if _is_max_service_dialog(resolver, chat_id):
        _remember_known_chat(sender, chat_id)
        log.info("Skipping automatic topic for MAX service dialog chat=%s", chat_id)
        return None

    title = await _topic_title_for_chat(resolver, chat_id)
    thread_id = await sender.ensure_topic(chat_id, title)
    if thread_id is None:
        return None

    from app.tg_handler import post_topic_intro
    await post_topic_intro(
        sender.bot, sender.chat_id, client, chat_id, thread_id,
    )
    history_limit = sender.topic_store.get_ui("history_import_limit", 20)
    await import_recent_history(
        client, sender, resolver, chat_id, thread_id, history_limit,
    )
    _remember_known_chat(sender, chat_id)
    return thread_id


async def _reconcile_snapshot_chats(client: MaxClient, sender: TelegramSender,
                                    resolver: ContactResolver,
                                    chat_locks: dict[str, asyncio.Lock] | None = None) -> int:
    """Create topics for active MAX chats first seen since the previous snapshot."""
    current = {
        str(chat_id): chat_id
        for chat_id, raw in resolver.chats_raw.items()
        if resolver.chat_types.get(chat_id) in _ACTIVE_CHAT_TYPES
        and (raw or {}).get("status") not in ("LEFT", "CLOSED")
    }
    raw_known = sender.topic_store.get_ui(_KNOWN_MAX_CHATS_UI_KEY)
    if raw_known is None:
        sender.topic_store.set_ui(_KNOWN_MAX_CHATS_UI_KEY, sorted(current))
        log.info("MAX chat reconciliation baseline initialized: %d chats", len(current))
        return 0

    known = {str(value) for value in (raw_known or [])}
    new_ids = [current[key] for key in sorted(set(current) - known)]
    created = 0
    for chat_id in new_ids:
        # Snapshot imports and live events share the same per-chat lock.
        # Without it, the same MAX message may be imported and forwarded
        # concurrently before either path persists its Telegram mapping.
        if chat_locks is None:
            thread_id = await _bootstrap_chat_topic(client, sender, resolver, chat_id)
        else:
            async with chat_locks.setdefault(str(chat_id), asyncio.Lock()):
                thread_id = await _bootstrap_chat_topic(client, sender, resolver, chat_id)
        if thread_id is not None:
            created += 1

    # Keep a monotonic seen-set: a temporarily absent chat must not look new later.
    known.update(current)
    sender.topic_store.set_ui(_KNOWN_MAX_CHATS_UI_KEY, sorted(known))
    if new_ids:
        log.info(
            "MAX chat reconciliation: %d unseen, %d topics created",
            len(new_ids), created,
        )
    return created


_CATCH_UP_WARNINGS_KEY = "catch_up_warnings"


def _catch_up_warning(store, chat_id, reason: str | None) -> None:
    """Persist a diagnostic cursor gap without private message contents."""
    warnings = dict(store.get_ui(_CATCH_UP_WARNINGS_KEY, {}) or {})
    key = str(chat_id)
    if reason is None:
        warnings.pop(key, None)
    else:
        warnings[key] = {"reason": reason, "updated": int(time.time())}
    if warnings != (store.get_ui(_CATCH_UP_WARNINGS_KEY, {}) or {}):
        store.set_ui(_CATCH_UP_WARNINGS_KEY, warnings)


def _max_message_timestamp(message: dict) -> int:
    """MAX history timestamps are milliseconds; ignore invalid records."""
    try:
        return max(0, int(message.get("time") or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


async def catch_up_chat_history(
    client: MaxClient,
    sender: TelegramSender,
    resolver: ContactResolver,
    chat_id,
    thread_id: int,
    *,
    max_pages: int = CATCH_UP_MAX_PAGES,
) -> int:
    """Recover missed messages after reconnect, never bulk-replaying without an anchor.

    A stored reconciliation cursor or an already mapped MAX message is a
    safe anchor. Without either, establish a baseline rather than flood a
    Telegram topic with possibly duplicated old history.
    """
    store = sender.topic_store
    cursor = store.get_history_cursor(chat_id)
    older_than = int(time.time() * 1000)
    unseen = {}
    newest_ts = 0
    found_anchor = False
    reached_start = False
    exhausted_budget = False
    visited_ids = set()
    pages = max(1, int(max_pages))

    for page in range(pages):
        try:
            response = await client.fetch_history(
                chat_id, count=CATCH_UP_BATCH, from_time=older_than,
            )
        except Exception:
            log.exception("Catch-up RPC exception for chat=%s", chat_id)
            _catch_up_warning(store,chat_id,"RPC_ERROR")
            return 0
        if not isinstance(response, dict) or response.get("_max_error"):
            log.warning("Catch-up aborted for chat=%s: failed history RPC", chat_id)
            _catch_up_warning(store,chat_id,"RPC_ERROR")
            return 0
        entries = [entry for entry in (response.get("messages") or [])
                   if isinstance(entry, dict) and entry.get("id") is not None]
        if not entries:
            reached_start = True
            break
        ordered = sorted(entries,
                         key=lambda item: (_max_message_timestamp(item),
                                           str(item.get("id"))), reverse=True)
        timestamps = [_max_message_timestamp(x) for x in ordered]
        if not all(timestamps):
            log.warning("Catch-up for chat=%s has un-timestamped message", chat_id)
            _catch_up_warning(store,chat_id,"INVALID_TIMESTAMP")
            return 0
        newest_ts = max(newest_ts, max(timestamps))

        # A historical message may have been edited while the bridge was
        # offline. The original MAX ID is already mapped, so catch-up normally
        # skips re-delivery; still apply its latest edited snapshot in place.
        sync = getattr(client, "message_sync", None)
        if sync is not None:
            for entry in ordered:
                mid = str(entry["id"])
                if (entry.get("status") == "EDITED"
                    and sender.topic_store.tg_for_max_message(chat_id,mid)
                    is not None):
                    await sync.accept(128,{"chatId":chat_id,"message":entry})

        for entry in ordered:
            ts = _max_message_timestamp(entry)
            mid = str(entry["id"])
            if mid in visited_ids:
                continue
            visited_ids.add(mid)
            mapped = store.tg_for_max_message(chat_id, mid) is not None
            if cursor is not None and ts <= cursor:
                found_anchor = True
                break
            if cursor is None and mapped:
                found_anchor = True
                break
            if not mapped:
                unseen[mid] = entry
        if found_anchor:
            break
        if len(entries) < CATCH_UP_BATCH:
            reached_start = True
            break
        # MAX from-time is an exclusive upper boundary. Overlap the
        # oldest timestamp from the previous page so that multiple messages
        # sharing one millisecond cannot fall through a page boundary.
        # Deduplicate the overlap by exact MAX message ID above.
        next_marker = min(timestamps) + 1
        if next_marker >= older_than or next_marker <= 0:
            # A full page at one timestamp cannot be paginated safely using
            # a timestamp-only API. Leave cursor unchanged, never claim that
            # hidden messages were recovered.
            log.warning("Catch-up ambiguous timestamp pagination chat=%s; "
                        "cursor unchanged (manual reconciliation needed)", chat_id)
            _catch_up_warning(store,chat_id,"TIMESTAMP_TIE")
            return 0
        older_than = next_marker
    else:
        exhausted_budget = True

    if not newest_ts:
        return 0
    if exhausted_budget and not found_anchor and not reached_start:
        log.warning("Catch-up history window exceeded chat=%s; no cursor advance", chat_id)
        _catch_up_warning(store,chat_id,"BUDGET_EXCEEDED")
        return 0
    if not found_anchor and cursor is None:
        # New feature enabled for a pre-existing chat with no matching
        # historical Telegram ID. Do not blindly replay old conversations.
        store.set_history_cursor(chat_id, newest_ts)
        log.warning("Catch-up baseline initialized without mapped anchor chat=%s", chat_id)
        return 0
    candidates = sorted(unseen.values(), key=lambda item:
                        (_max_message_timestamp(item), str(item["id"])))
    imported = 0
    if candidates:
        imported = await import_recent_history(
            client, sender, resolver, chat_id, thread_id,
            count=len(candidates), raw_messages=candidates,
        )
        # The importer can encounter TG send failures; never advance the
        # durable cursor past missing messages.
        if any(store.tg_for_max_message(chat_id, entry["id"]) is None
               for entry in candidates):
            log.warning("Catch-up partially delivered chat=%s; cursor unchanged", chat_id)
            _catch_up_warning(store,chat_id,"DELIVERY_INCOMPLETE")
            return imported

    store.set_history_cursor(chat_id, newest_ts)
    _catch_up_warning(store,chat_id,None)
    log.info("Catch-up verified chat=%s imported=%d scanned=%d cursor=%s",
             chat_id, imported, len(visited_ids), newest_ts)
    return imported


async def catch_up_known_chats(
    client: MaxClient,
    sender: TelegramSender,
    resolver: ContactResolver,
    chat_locks: dict[str, asyncio.Lock],
    original_topics: set[str],
) -> int:
    """Run history reconciliation only for topics that predate this startup."""
    recovered = 0
    for chat_id, raw in resolver.chats_raw.items():
        if str(chat_id) not in original_topics:
            continue
        if resolver.chat_types.get(chat_id) not in _ACTIVE_CHAT_TYPES:
            continue
        if (raw or {}).get("status") in ("LEFT", "CLOSED"):
            continue
        if client.chat_ids and chat_id not in client.chat_ids:
            continue
        if _is_max_service_dialog(resolver, chat_id):
            continue
        topic_id = sender.topic_store.get_topic(chat_id)
        if topic_id is None:
            continue
        async with chat_locks.setdefault(str(chat_id), asyncio.Lock()):
            recovered += await catch_up_chat_history(
                client, sender, resolver, chat_id, topic_id,
            )
    return recovered


def create_max_client(
    max_token: str, max_device_id: str, sender: TelegramSender, max_chat_ids: str | None = None,
    debug: bool = False,
) -> MaxClient:
    client = MaxClient(token=max_token, device_id=max_device_id, debug=debug, chat_ids=max_chat_ids)
    resolver = ContactResolver(client=client)
    # Expose runtime helpers for Telegram-side management actions.
    client.resolver = resolver
    client._tg_sender = sender
    chat_locks: dict[str, asyncio.Lock] = {}
    background_outbox = None

    async def recover_outbox():
        outbox = getattr(sender, "outbox", None)
        if outbox is None:
            return
        for item in outbox.recoverable(limit=50):
            await handle_message(item)

    async def outbox_poller():
        while True:
            try:
                await recover_outbox()
                sync=getattr(client,"message_sync",None)
                if sync is not None:
                    await sync.recover_pending(limit=20)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Outbox recovery polling failed")
            await asyncio.sleep(15)

    _first_connect = True
    _notif_count = 0
    _last_notif_time: datetime | None = None

    def _can_notify() -> bool:
        if _last_notif_time is None:
            return True
        elapsed = (datetime.now() - _last_notif_time).total_seconds()
        if _notif_count == 1:
            return elapsed >= 3600    # 2-е: через 1 час
        if _notif_count == 2:
            return elapsed >= 10800   # 3-е: через 3 часа
        return elapsed >= 86400       # 4-е и далее: раз в сутки

    @client.on_ready
    async def handle_ready(snapshot: dict):
        nonlocal _first_connect
        participant_ids = resolver.load_snapshot(snapshot)

        if participant_ids:
            log.info("Batch-resolving %d participants...", len(participant_ids))
            await resolver.resolve_users_batch(participant_ids)
            log.info("Resolved %d users", len(resolver.users))

            log.info("Known chats: %d", len(resolver.chats))
            log.info("Known users: %d", len(resolver.users))

        original_topics = {
            str(chat_id) for chat_id in resolver.chats_raw
            if sender.topic_store.get_topic(chat_id) is not None
        }
        created = await _reconcile_snapshot_chats(
            client, sender, resolver, chat_locks=chat_locks,
        )
        restored = await catch_up_known_chats(
            client, sender, resolver, chat_locks, original_topics,
        )
        if restored:
            log.info("Reconnect recovered %d previously missed messages", restored)

        nonlocal background_outbox
        if isinstance(getattr(sender, "outbox", None), OutboxStore):
            if background_outbox is None or background_outbox.done():
                background_outbox = asyncio.create_task(outbox_poller())
            await recover_outbox()

        if not _first_connect:
            suffix = f" | новых топиков: {created}" if created else ""
            await sender.send(f"✅ <b>Max:</b> соединение восстановлено{suffix}")
        else:
            chat_count = len(resolver.chats)
            suffix = f" | новых топиков: {created}" if created else ""
            await sender.send(
                f"✅ <b>Max:</b> подключён | чатов: {chat_count}{suffix}"
            )
        _first_connect = False

    @client.on_disconnect
    async def handle_disconnect():
        nonlocal _notif_count, _last_notif_time
        if not _can_notify():
            log.info("Disconnect notification suppressed (throttle)")
            return
        _notif_count += 1
        _last_notif_time = datetime.now()
        await sender.send("⚠️ <b>Max:</b> соединение потеряно, переподключение...")

    @client.on_message
    async def handle_message(msg: MaxMessage):
        async with chat_locks.setdefault(str(msg.chat_id), asyncio.Lock()):
            async def deliver():
                await _handle_message_serial(msg)
                mapped_id = (sender.topic_store.tg_for_max_message(
                    msg.chat_id, msg.message_id,
                ) if msg.message_id else None)
                active = current_delivery()
                if active is not None and mapped_id is not None:
                    active.store.record_mapping(active.key, mapped_id)
                return mapped_id is not None
            outbox = getattr(sender, "outbox", None)
            if isinstance(outbox, OutboxStore):
                await outbox.process(msg, deliver)
            else:
                await deliver()

    async def _handle_message_serial(msg: MaxMessage):
        # Never log message content or private contact details.
        log.info(
            "New message: chat=%s sender=%s is_self=%s text_len=%d attaches=%d",
            msg.chat_id, msg.sender_id, msg.is_self,
            len(msg.text or ""), len(msg.attaches),
        )

        # A new chat can first appear as our own outgoing message. Refresh its
        # metadata before deciding whether to ignore the self-message, so the
        # Telegram topic is not lost until somebody replies.
        if msg.chat_id not in resolver.chats_raw:
            try:
                await _refresh_chat_metadata(client, resolver, msg.chat_id)
            except Exception:
                log.exception("Could not refresh metadata for new chat=%s", msg.chat_id)

        if msg.is_self:
            # Browser-originated messages must be mirrored as well. On a new
            # chat, history import may already include this exact message.
            if sender.topic_store.get_topic(msg.chat_id) is None:
                await _bootstrap_chat_topic(client, sender, resolver, msg.chat_id)
                if sender.topic_store.get_topic(msg.chat_id) is None:
                    return
            else:
                _remember_known_chat(sender, msg.chat_id)

        # A delayed SHARE preview must not be forwarded as a second message.
        if msg.message_id and sender.topic_store.tg_for_max_message(
            msg.chat_id, msg.message_id
        ) is not None:
            log.debug("Already forwarded message %s", msg.message_id)
            return

        if msg.is_self:
            echo_tg_id = await client.telegram_echo_tg_id(msg)
            if echo_tg_id is not None:
                sender.topic_store.set_message(msg.chat_id, msg.message_id, echo_tg_id)
                log.info("Suppressed Telegram-originated MAX echo max=%s", msg.message_id)
                return

        raw_sender = "Вы" if msg.is_self else ""
        if not msg.is_self and msg.sender_id is not None:
            raw_sender = await resolver.resolve_user(msg.sender_id)
        is_dm = resolver.is_dm(msg.chat_id)
        raw_chat = resolver.chat_name(msg.chat_id)

        # One forum topic per Max chat. Prefer a human title:
        # - DMs → the peer's name
        # - Groups/channels with a known title → the chat title
        # - Runtime-discovered chats with a sender → the sender's name
        # - Channel posts may have sender=None; use numeric chat ID temporarily
        #   so ensure_topic can rename it when channel metadata arrives.
        chat_title_known = raw_chat != str(msg.chat_id) and not raw_chat.startswith("DM:")
        if is_dm:
            topic_title = raw_sender or raw_chat
        elif chat_title_known:
            topic_title = raw_chat
        elif raw_sender:
            topic_title = raw_sender
        else:
            topic_title = str(msg.chat_id)

        existing_thread = sender.topic_store.get_topic(msg.chat_id)
        thread_id = await sender.ensure_topic(msg.chat_id, topic_title)
        _remember_known_chat(sender, msg.chat_id)

        # First time we touch this chat → publish context first, then import
        # the configured recent history before the live message. This keeps a
        # newly-created Telegram topic chronological.
        if existing_thread is None and thread_id is not None:
            from app.tg_handler import post_topic_intro
            await post_topic_intro(
                sender.bot, sender.chat_id, client, msg.chat_id, thread_id,
            )
            history_limit = sender.topic_store.get_ui("history_import_limit", 20)
            await import_recent_history(
                client, sender, resolver, msg.chat_id, thread_id, history_limit,
            )
            if (
                msg.message_id
                and sender.topic_store.tg_for_max_message(msg.chat_id, msg.message_id)
                is not None
            ):
                log.info("Live message already imported with initial history; skipping duplicate")
                return

        sender_label = escape(raw_sender)
        chat_label = escape(raw_chat)
        header_text = _header(msg, sender_label, chat_label, is_dm)

        link = msg.link
        link_type = link.get("type") if isinstance(link, dict) else None

        if link_type == "REPLY":
            inner = link.get("message") or {}
            replied_max_id = inner.get("id") or link.get("messageId")
            replied_tg_id = None
            if replied_max_id:
                replied_tg_id = sender.topic_store.tg_for_max_message(
                    msg.chat_id, replied_max_id,
                )

            if replied_tg_id is not None:
                sent = await _send_message_content(
                    msg, header_text, client, sender, thread_id,
                    reply_to_message_id=replied_tg_id,
                )
                _remember_message(sender, msg, sent)
                log.info(
                    "Forwarded native REPLY max=%s → tg=%s",
                    replied_max_id, replied_tg_id,
                )
                return

            # The referenced MAX message predates our mapping (e.g. bridge
            # was restarted before this feature). Keep it in one Telegram
            # message instead of emitting the quote and answer separately.
            source_sender_id = inner.get("sender")
            source_label = ""
            if source_sender_id is not None:
                source_label = escape(await resolver.resolve_user(source_sender_id))
            reply_header = f"{header_text}\n↩ <b>Ответ"
            if source_label:
                reply_header += f" на {source_label}"
            reply_header += "</b>"
            quoted = inner.get("text") or ""
            if quoted:
                reply_header += f"\n<blockquote>{escape(quoted[:700])}</blockquote>"
            elif inner.get("attaches"):
                reply_header += "\n<blockquote>[вложение]</blockquote>"

            sent = await _send_message_content(
                msg, reply_header, client, sender, thread_id,
            )
            _remember_message(sender, msg, sent)
            log.info("Forwarded REPLY fallback → TG")
            return

        if link_type == "FORWARD":
            forwarded = await _handle_linked_message(
                link, link_type, header_text, client, sender, resolver,
                thread_id=thread_id, msg=msg,
            )
            comment = None
            if msg.text:
                comment = await sender.send(
                    f"{header_text}\n{_message_body(msg)}",
                    message_thread_id=thread_id,
                )
            _remember_message(sender, msg, comment or forwarded)
            log.info("Forwarded link type=FORWARD → TG")
            return

        sent = await _send_message_content(
            msg, header_text, client, sender, thread_id,
        )
        _remember_message(sender, msg, sent)

    return client
