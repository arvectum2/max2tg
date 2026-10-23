"""Per-chat MAX2TG settings UI. All changes require Telegram admin rights."""
from __future__ import annotations

import asyncio
import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.chat_preferences import get_chat_prefs,set_chat_pref,HISTORY_CHOICES

log=logging.getLogger(__name__)


def _scope(context):
    from app.tg_handler import TOPIC_STORE_KEY,MAX_CLIENT_KEY,SUPERGROUP_KEY
    return (context.bot_data.get(TOPIC_STORE_KEY),
            context.bot_data.get(MAX_CLIENT_KEY),
            context.bot_data.get(SUPERGROUP_KEY))


def _is_admin(update,context):
    from app.tg_handler import _is_admin_user
    return _is_admin_user(update,context)


def _format_settings(store,chat_id):
    prefs=get_chat_prefs(store,chat_id)
    title=store.get_title(chat_id) or "MAX-чат"
    history=str(prefs.history_depth) if prefs.history_depth is not None else "общая"
    return (
        f"⚙️ <b>Настройки чата: {escape(str(title)[:100])}</b>\n"
        f"Синхронизация: {'✅ включена' if prefs.sync else '⏸️ пауза'}\n"
        f"Уведомления: {'🔕 без звука' if prefs.mute else '🔔 со звуком'}\n"
        f"Медиа: {'✅ файлы разрешены' if prefs.media else '🚫 только текст'}\n"
        f"История новых тем: {history}\n"
        f"Архив: {'🗃 в архиве' if prefs.archived else '📂 активный'}\n"
        f"Имя темы из MAX: {'✅ синхронизировать' if prefs.title_sync else '⏹️ вручную'}\n\n"
        "Пауза не удаляет переписку и не подтверждает пропущенные сообщения.\n"
        "Архив скрывает чат из общего списка, но не удаляет Telegram-тему.\n"
        "Имя синхронизируется только после включения настройки; "
        "аватар можно обновить командой /intro."
    )


def _markup(store,chat_id):
    p=get_chat_prefs(store,chat_id)
    depth=p.history_depth
    labels=[
        ("sync","⏸️ Пауза" if p.sync else "▶️ Возобновить"),
        ("mute","🔔 Со звуком" if p.mute else "🔕 Без звука"),
        ("media","🚫 Отключить медиа" if p.media else "📎 Разрешить медиа"),
        ("archived","🗃 В архив" if not p.archived else "📂 Из архива"),
        ("title_sync","🔄 Синхр. имя" if not p.title_sync else "✋ Имя вручную"),
    ]
    rows=[[InlineKeyboardButton(label,
             callback_data=f"chatpref:toggle:{chat_id}:{key}")]
          for key,label in labels]
    rows.append([InlineKeyboardButton(
        f"История: {'общая' if depth is None else depth} → изменить",
        callback_data=f"chatpref:history:{chat_id}",
    )])
    rows.append([InlineKeyboardButton(
        "← Мои чаты",callback_data="menu:list:0",
    )])
    return InlineKeyboardMarkup(rows)


def _history_markup(chat_id):
    choices=[(None,"Общая настройка"),(0,"0 сообщений"),
             (20,"20 сообщений"),(50,"50 сообщений"),(100,"100 сообщений")]
    rows=[[InlineKeyboardButton(name,callback_data=
                f"chatpref:depth:{chat_id}:{'default' if value is None else value}")]
          for value,name in choices]
    rows.append([InlineKeyboardButton(
        "← Настройки чата", callback_data=f"chatpref:show:{chat_id}",
    )])
    return InlineKeyboardMarkup(rows)


async def cmd_chatsettings(update,context):
    msg=update.message
    if msg is None or not _is_admin(update,context):
        return
    store,client,group=_scope(context)
    if store is None or group is None or int(msg.chat_id)!=int(group):
        return
    cid=(store.chat_for_topic(msg.message_thread_id)
         if msg.message_thread_id is not None and msg.is_topic_message
         else None)
    if cid is None:
        await msg.reply_text(
            "Открой тему MAX-чата и введи /chatsettings либо выбери "
            "«Мои чаты» → «Настройки чата».")
        return
    await msg.reply_text(_format_settings(store,cid),
                         parse_mode="HTML",reply_markup=_markup(store,cid))


async def callback_chatsettings(update,context):
    q=update.callback_query
    if q is None or not q.data or not _is_admin(update,context):
        return
    store,client,group=_scope(context)
    if (store is None or group is None or q.message is None
        or int(q.message.chat.id)!=int(group)):
        return
    parts=q.data.split(":")
    if len(parts)<3 or parts[0]!="chatpref":
        return
    action,chat_id=parts[1:3]
    if store.get_topic(chat_id) is None:
        await q.answer("Тема удалена или не подключена.",show_alert=True)
        return
    if action in ("toggle","depth") and len(parts)!=4:
        return
    if action not in ("show","history","toggle","depth"):
        return
    await q.answer()
    if action=="history":
        await q.message.reply_text(
            "Выбери глубину автоматически импортируемой истории "
            "для этого MAX-чата при создании темы. "
            "Ручная /history по-прежнему загружает по 20.",
            reply_markup=_history_markup(chat_id))
        return
    if action=="toggle":
        key=parts[3]
        if key not in ("sync","mute","media","archived","title_sync"):
            return
        previous=get_chat_prefs(store,chat_id)
        wanted=not getattr(previous,key)
        if key=="sync":
            # Use live event lock: pause cannot race a currently delivering
            # message. On resume try catch-up from its durable cursor.
            lock=getattr(client,"_history_locks",{}).setdefault(
                str(chat_id),asyncio.Lock()) if client else asyncio.Lock()
            async with lock:
                set_chat_pref(store,chat_id,key,wanted)
            if wanted and client and getattr(client,"_authorized",False):
                sender=getattr(client,"_tg_sender",None)
                resolver=getattr(client,"resolver",None)
                if sender and resolver:
                    try:
                        from app.max_listener import catch_up_chat_history
                        async with lock:
                            await catch_up_chat_history(
                                client,sender,resolver,chat_id,
                                store.get_topic(chat_id))
                    except Exception:
                        log.exception("Catch-up after resuming chat=%s",chat_id)
                        await q.message.reply_text(
                            "⚠️ Синхронизация включена, но восстановить "
                            "историю сейчас не удалось. Проверь /status.")
        elif key=="title_sync" and wanted and client:
            resolver=getattr(client,"resolver",None)
            latest=resolver.chat_name(int(chat_id)) if resolver else None
            if latest and latest!=str(chat_id) and latest!=store.get_title(chat_id):
                try:
                    await context.bot.edit_forum_topic(
                        chat_id=int(group),
                        message_thread_id=store.get_topic(chat_id),
                        name=str(latest)[:128],
                    )
                    store.update_title(chat_id,str(latest)[:128])
                except Exception:
                    log.exception("Cannot synchronize topic name chat=%s",chat_id)
                    await q.message.reply_text(
                        "⚠️ Не удалось переименовать Telegram-тему. "
                        "Проверь права бота «Управление темами».")
                    return
            set_chat_pref(store,chat_id,key,wanted)
        else:
            set_chat_pref(store,chat_id,key,wanted)
    elif action=="depth":
        raw=parts[3]
        if raw=="default":
            depth=None
        else:
            try:depth=int(raw)
            except ValueError:return
            if depth not in HISTORY_CHOICES:return
        set_chat_pref(store,chat_id,"history_depth",depth)
    await q.message.reply_text(
        _format_settings(store,chat_id),parse_mode="HTML",
        reply_markup=_markup(store,chat_id),
    )
