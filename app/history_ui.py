"""Telegram commands for scoped MAX history search and explicit imports.

The global /search command continues to search users/chats. /find searches
messages without sending them, /history explicitly imports older messages.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
import secrets
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from app.history_nav import (
    SEARCH_MAX_CHATS, parse_search, search_page, history_position,
    set_history_position, import_next_page, timestamp_ms, HISTORY_PAGE,
)

log=logging.getLogger(__name__)
_SESSION="max_message_search_session"


def _from_context(context):
    # Delay import to avoid a cycle with tg_handler's handlers.
    from app.tg_handler import (MAX_CLIENT_KEY, TOPIC_STORE_KEY,
                                SUPERGROUP_KEY)
    return (context.bot_data.get(MAX_CLIENT_KEY),
            context.bot_data.get(TOPIC_STORE_KEY),
            context.bot_data.get(SUPERGROUP_KEY))


def _is_admin(update,context):
    from app.tg_handler import _is_admin_user
    return _is_admin_user(update,context)


def _topic_scope(message,store):
    if (message is None or not getattr(message,"is_topic_message",False)
        or message.message_thread_id is None):
        return None
    return store.chat_for_topic(message.message_thread_id)


def _all_bound_chats(store):
    return [str(x) for x in sorted(store._chats,
                                     key=str) if store.get_topic(x) is not None]


def _find_markup(has_more: bool, token: str = ""):
    rows=[]
    if has_more:
        rows.append([InlineKeyboardButton(
            "🔎 Искать дальше (следующая страница)",
            callback_data=f"find:more:{token}")])
    rows.append([InlineKeyboardButton(
        "🔄 Новый запрос: /find",callback_data="find:help")])
    return InlineKeyboardMarkup(rows)


def _history_markup(chat_id, done: bool):
    if done:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📜 Просмотреть сначала",
                                 callback_data=f"history:reset:{chat_id}"),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📥 Загрузить до {HISTORY_PAGE} сообщений",
                             callback_data=f"history:load:{chat_id}"),
    ]])


def _html_result(page, spec, store, supergroup_id):
    parts=[
        f"🔎 <b>Поиск сообщений MAX</b> · {escape(spec.phrase or 'по датам')}",
        f"Просмотрено сообщений в этом проходе: {page.scanned}. "
        f"Совпадений на странице: {len(page.matches)}.",
    ]
    for chat_id,item in page.matches:
        title=store.get_title(chat_id) or "MAX-чат"
        label=datetime.fromtimestamp(timestamp_ms(item)/1000,ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")
        text=item.get("text") or "[вложение]"
        if not isinstance(text,str):
            text="[вложение]"
        preview=text.strip().replace("\n"," ")
        if len(preview)>160:
            preview=preview[:157]+"…"
        max_id=str(item["id"])
        tg_id=store.tg_for_max_message(chat_id,max_id)
        prefix=f"• <b>{escape(str(title)[:45])}</b> · {label}"
        if tg_id and str(supergroup_id).startswith("-100"):
            slug=str(supergroup_id)[4:]
            prefix+=f' · <a href="https://t.me/c/{slug}/{int(tg_id)}">Открыть</a>'
        else:
            prefix+=" · <i>ещё не импортировано</i>"
        parts.append(prefix+"\n"+escape(preview))
    if not page.matches:
        parts.append("На этой странице совпадений нет.")
    if page.exhausted:
        parts.append("Это конец доступной истории в проверенных чатах.")
    else:
        parts.append("Есть более старая история. Нажми «Искать дальше».")
    return "\n\n".join(parts)


async def cmd_find(update,context, *, all_chats=False):
    message=update.message
    if message is None or not _is_admin(update,context):
        return
    client,store,supergroup=_from_context(context)
    if client is None or store is None:
        await message.reply_text("⚠️ Мост MAX сейчас недоступен.")
        return
    scope=_topic_scope(message,store)
    chats=_all_bound_chats(store) if all_chats or scope is None else [str(scope)]
    if len(chats)>SEARCH_MAX_CHATS:
        await message.reply_text(
            f"Поиск ограничен {SEARCH_MAX_CHATS} чатами за проход. "
            "Перейди в нужную тему и используй /find.")
        return
    if not chats:
        await message.reply_text("Нет подключённых MAX-чатов.")
        return
    raw=" ".join(context.args or []).strip()
    if not raw:
        await message.reply_text(
            "🔎 <b>Поиск сообщений MAX</b>\n"
            "В теме: <code>/find хлеб</code>\n"
            "Только по дате: <code>/find с:2026-10-01 по:2026-10-23</code>\n"
            "С диапазоном дат (время Москвы): "
            "<code>/find договор с:2026-10-01 по:2026-10-23</code>\n"
            "По всем подключённым чатам: <code>/findall договор</code>\n"
            "Поиск читает историю MAX, ничего не пересылает.",
            parse_mode="HTML")
        return
    try:
        spec=parse_search(raw)
        result=await search_page(client,chats,spec)
    except (ValueError,RuntimeError) as exc:
        await message.reply_text(f"⚠️ {escape(str(exc))}",parse_mode="HTML")
        return
    except Exception:
        log.exception("Message history search failed")
        await message.reply_text("⚠️ Не удалось прочитать историю MAX.")
        return
    shown=result.matches[:8]
    pending=result.matches[8:]
    context.user_data[_SESSION]={
        "raw":raw,"chats":chats,"cursors":result.cursors,
        "token":secrets.token_hex(4),
        "pending":pending,
        "seen":{(chat,str(item["id"])) for chat,item in result.matches},
        "exhausted":result.exhausted,
    }
    from app.history_nav import SearchPage
    view=SearchPage(shown,result.cursors,result.exhausted and not pending,result.scanned)
    await message.reply_text(
        _html_result(view,spec,store,supergroup),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_find_markup(
            bool(pending) or not result.exhausted,
            context.user_data[_SESSION]["token"]),
    )


async def callback_find(update,context):
    query=update.callback_query
    if query is None or not _is_admin(update,context):
        return
    await query.answer()
    client,store,supergroup=_from_context(context)
    if (supergroup is None or query.message is None or
        int(query.message.chat.id)!=int(supergroup)):
        return
    if query.data=="find:help":
        await query.message.reply_text(
            "🔎 Для поиска по сообщениям используй "
            "/find слово или /findall слово; "
            "фильтры: с:ГГГГ-ММ-ДД по:ГГГГ-ММ-ДД.")
        return
    if not query.data.startswith("find:more:"):
        return
    session=context.user_data.get(_SESSION)
    if (not session or query.data!=f"find:more:{session['token']}"
        or not client or not store):
        # An old inline button must never advance or mutate a newer search.
        return
    try:
        spec=parse_search(session["raw"])
        if not session.get("pending") and not session["exhausted"]:
            result=await search_page(
                client,session["chats"],spec,session["cursors"])
            session["cursors"]=result.cursors
            session["exhausted"]=result.exhausted
            fresh=[(chat,item) for chat,item in result.matches
                   if (chat,str(item["id"])) not in session["seen"]]
            session["seen"].update((chat,str(item["id"])) for chat,item in fresh)
            session["pending"].extend(fresh)
            scanned=result.scanned
        else:
            scanned=0
    except (ValueError,RuntimeError) as exc:
        await query.message.reply_text(
            f"⚠️ {escape(str(exc))}",parse_mode="HTML")
        return
    except Exception:
        log.exception("Search continuation failed")
        await query.message.reply_text("⚠️ Не удалось продолжить поиск MAX.")
        return
    if context.user_data.get(_SESSION) is not session:
        return
    from app.history_nav import SearchPage
    shown=session["pending"][:8]
    del session["pending"][:8]
    has_more=bool(session["pending"]) or not session["exhausted"]
    page=SearchPage(shown,session["cursors"],not has_more,scanned)
    await query.message.reply_text(
        _html_result(page,spec,store,supergroup),
        parse_mode="HTML",disable_web_page_preview=True,
        reply_markup=_find_markup(has_more,session["token"]),
    )


async def cmd_history(update,context):
    message=update.message
    if message is None or not _is_admin(update,context):
        return
    client,store,_=_from_context(context)
    chat_id=_topic_scope(message,store) if store else None
    if chat_id is None or client is None:
        await message.reply_text(
            "📜 Открой Telegram-тему нужного MAX-чата и введи /history.")
        return
    if (context.args or []) == ["reset"]:
        set_history_position(store,chat_id,None)
        await message.reply_text(
            "Позиция просмотра сброшена. Уже импортированные сообщения "
            "повторно не отправляются.")
    cursor=history_position(store,chat_id)
    await message.reply_text(
        "<b>История MAX</b>\n"
        "Загружай старые сообщения порциями по 20. Уже переданные "
        "сообщения пропускаются. Позиция сохраняется после перезапуска.\n"
        + ("Архив полностью просмотрен." if cursor==0
           else "Следующая порция будет импортирована только после нажатия."),
        parse_mode="HTML",
        reply_markup=_history_markup(chat_id, cursor==0),
    )


async def callback_history(update,context):
    query=update.callback_query
    if query is None or not query.data or not _is_admin(update,context):
        return
    await query.answer()
    client,store,supergroup=_from_context(context)
    if (supergroup is None or query.message is None or
        int(query.message.chat.id)!=int(supergroup)):
        return
    tokens=query.data.split(":")
    if len(tokens)!=3 or tokens[0]!="history" or tokens[1] not in (
        "show","load","reset"):
        return
    chat_id=tokens[2]
    if client is None or store is None:
        return
    thread_id=store.get_topic(chat_id)
    # No arbitrary chat_id: button must be physically in the correct topic.
    if (thread_id is None or
        not getattr(query.message,"is_topic_message",False) or
        query.message.message_thread_id!=thread_id):
        return
    action=tokens[1]
    if action=="reset":
        set_history_position(store,chat_id,None)
        await query.message.reply_text(
            "Позиция истории сброшена. Ранее импортированные ID не дублируются.",
            reply_markup=_history_markup(chat_id,False))
        return
    if action=="show":
        position=history_position(store,chat_id)
        await query.message.reply_text(
            "📜 Старые сообщения MAX можно догружать по 20 за нажатие. "
            "Отправляются только ещё не импортированные.",
            reply_markup=_history_markup(chat_id,position==0))
        return
    # Import is a deliberate destructive-to-TG-volume user action.
    sender=getattr(client,"_tg_sender",None)
    resolver=getattr(client,"resolver",None)
    if sender is None or resolver is None:
        await query.message.reply_text("⚠️ Мост ещё не готов к импорту.")
        return
    try:
        imported,scanned,done=await import_next_page(
            client,sender,resolver,chat_id,thread_id)
    except RuntimeError as exc:
        await query.message.reply_text(
            "⚠️ "+str(exc)+" Повтори загрузку; подтверждённые ID "
            "не будут отправлены ещё раз.")
        return
    except Exception:
        log.exception("Manual history import failed chat=%s",chat_id)
        await query.message.reply_text(
            "⚠️ Не удалось загрузить историю. Позиция не изменена.")
        return
    await query.message.reply_text(
        f"📜 Проверено {scanned} сообщений, добавлено {imported}. "
        + ("Достигнуто начало истории." if done
           else "Можно загрузить следующую порцию."),
        reply_markup=_history_markup(chat_id,done))
