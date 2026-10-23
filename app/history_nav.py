"""Read-only MAX message search and explicit, page-wise history import.

All search operations query bounded slices of MAX opcode 49. No message
content or search results are persisted to disk. History page cursors
contain only timestamps, and import uses the regular durable outbox.
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
from datetime import date, datetime, time as dt_time, timedelta
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

MAX_PAGE = 100
HISTORY_PAGE = 20
SEARCH_MAX_CHATS = 12
SEARCH_DISPLAY = 8
HISTORY_UI_KEY = "manual_history_from"
DATE_TOKEN = re.compile(r"^(с|по):(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)


@dataclass(frozen=True)
class SearchSpec:
    phrase: str
    since_ms: int | None = None
    until_ms: int | None = None

    def matches(self, item: dict) -> bool:
        if not isinstance(item, dict):
            return False
        text = item.get("text") or ""
        if not isinstance(text, str):
            text = ""
        timestamp = timestamp_ms(item)
        if timestamp is None:
            return False
        if self.since_ms is not None and timestamp < self.since_ms:
            return False
        if self.until_ms is not None and timestamp >= self.until_ms:
            return False
        return (not self.phrase or self.phrase.casefold() in text.casefold())


def timestamp_ms(item: dict) -> int | None:
    if not isinstance(item, dict):
        return None
    raw = item.get("time")
    if type(raw) not in (int, float):
        return None
    if raw <= 0:
        return None
    # MAX opcode 49 always uses milliseconds; seconds conversion here would
    # cause page cursors to jump forward and repeat already-scanned entries.
    return int(raw)


def parse_search(raw: str, *, timezone_name: str = "Europe/Moscow") -> SearchSpec:
    """'ключевые слова с:2026-10-01 по:2026-10-23', dates inclusive."""
    if len(raw) > 500:
        raise ValueError("Слишком длинный запрос (до 500 символов).")
    words=[]
    bounds={}
    for word in raw.split():
        match=DATE_TOKEN.fullmatch(word)
        if not match:
            if word.lower().startswith(("с:","по:")):
                raise ValueError("Дата должна иметь формат ГГГГ-ММ-ДД.")
            words.append(word)
            continue
        part, label = match.groups()
        part=part.lower()
        if part in bounds:
            raise ValueError(f"Фильтр {part}: указан повторно.")
        try:
            local_date=date.fromisoformat(label)
            zone=ZoneInfo(timezone_name)
        except ValueError as exc:
            raise ValueError("Дата должна иметь формат ГГГГ-ММ-ДД.") from exc
        millis=int(datetime.combine(local_date,dt_time.min,tzinfo=zone).timestamp()*1000)
        if part=="по":
            millis=int(datetime.combine(local_date+timedelta(days=1),
                                        dt_time.min,tzinfo=zone).timestamp()*1000)
        bounds[part]=millis
    phrase=" ".join(words).strip()
    if not phrase and not bounds:
        raise ValueError("Укажи текст или хотя бы одну дату поиска.")
    if phrase and not 2 <= len(phrase) <= 180:
        raise ValueError("Укажи минимум 2 символа текста для поиска.")
    if bounds.get("с",0) >= bounds.get("по",float("inf")):
        raise ValueError("Начальная дата должна быть раньше конечной.")
    return SearchSpec(phrase,bounds.get("с"),bounds.get("по"))


def next_marker(entries: list[dict], previous: int) -> int | None:
    """Timestamp-only pagination, overlap by 1 ms to preserve ties.

    None means that we cannot safely move the marker: a full equal-timestamp
    page may hide more items, so the caller must not falsely say 'all loaded'.
    """
    timestamps=[timestamp_ms(x) for x in entries]
    if not timestamps or any(x is None for x in timestamps):
        return None
    value=min(timestamps)+1
    if not 0 < value < previous:
        return None
    return value


def _validate_response(response: Any) -> list[dict]:
    if not isinstance(response, dict) or response.get("_max_error"):
        raise RuntimeError("MAX не вернул страницу истории.")
    items=response.get("messages")
    if not isinstance(items,list):
        raise RuntimeError("Некорректный ответ истории MAX.")
    if any(not isinstance(r,dict) or r.get("id") in (None,"")
           for r in items):
        raise RuntimeError(
            "MAX вернул неполную страницу с сообщением без ID. "
            "Позиция истории не изменена.")
    return items


async def history_page(client, chat_id, marker: int | None, *,
                       count: int = HISTORY_PAGE) -> tuple[list[dict], int | None, bool]:
    """One bounded history request. Never invent offsets for MAX opcode 49."""
    marker=int(marker) if marker is not None else int(time.time()*1000)
    response=await client.fetch_history(chat_id,count=count,from_time=marker)
    records=_validate_response(response)
    if any(timestamp_ms(item) is None for item in records):
        raise RuntimeError("MAX вернул сообщения без временных меток.")
    if len(records)>count:
        raise RuntimeError("MAX превысил размер запрошенной страницы.")
    if len(records)==count and len({timestamp_ms(r) for r in records})==1:
        raise RuntimeError(
            "Нельзя безопасно продолжить: все сообщения страницы MAX "
            "имеют одинаковое время, часть истории может быть скрыта.")
    reached_end=len(records)<count
    nxt=next_marker(records,marker) if records else None
    if not reached_end and nxt is None:
        raise RuntimeError(
            "Нельзя безопасно продолжить: одинаковые временные метки "
            "на границе страницы. Курсор не изменён.")
    return records,nxt,reached_end


@dataclass(frozen=True)
class SearchPage:
    matches: list[tuple[str,dict]]
    cursors: dict[str,int | None]
    exhausted: bool
    scanned: int


async def search_page(client, chat_ids: list[str], spec: SearchSpec,
                      cursors: dict[str,int | None] | None = None) -> SearchPage:
    """Scan at most one MAX history page per chat, returning latest matches.

    Each click resumes the stored per-user cursor. Duplicate records at
    timestamp boundaries do not become duplicate displayed results.
    """
    if not 1<=len(chat_ids)<=SEARCH_MAX_CHATS:
        raise ValueError("Слишком много чатов для одного поиска.")
    cursors=dict(cursors or {})
    hits=[]
    next_cursors={}
    scanned=0
    for chat_id in chat_ids:
        cursor=cursors.get(str(chat_id))
        if cursor == 0:
            next_cursors[str(chat_id)]=0
            continue
        marker=cursor if cursor is not None else int(time.time()*1000)
        rows,next_time,at_start=await history_page(
            client,chat_id,marker,count=MAX_PAGE)
        scanned+=len(rows)
        for item in rows:
            if spec.matches(item):
                # Only keep fields needed by the UI. Source payload may
                # contain private attachment URLs/tokens and long text.
                hits.append((str(chat_id),{
                    "id":str(item["id"]),"time":item["time"],
                    "text":str(item.get("text") or "")[:200],
                }))
        # The prior marker is an inclusive overlap. When no progress is
        # possible, fail instead of silently losing timestamp-tied messages.
        if not rows or at_start:
            next_cursors[str(chat_id)]=0
        elif next_time is None:
            raise RuntimeError("Поиск остановлен из-за неоднозначной страницы MAX.")
        else:
            next_cursors[str(chat_id)]=next_time
    hits.sort(key=lambda pair: (timestamp_ms(pair[1]) or 0,
                                pair[0],str(pair[1]["id"])),reverse=True)
    return SearchPage(hits,next_cursors,
                      all(v==0 for v in next_cursors.values()),scanned)


def history_position(store,chat_id) -> int | None:
    positions=store.get_ui(HISTORY_UI_KEY,{}) or {}
    try:
        return int(positions[str(chat_id)]) if str(chat_id) in positions else None
    except (ValueError,TypeError):
        return None


def set_history_position(store,chat_id,marker: int | None):
    positions=dict(store.get_ui(HISTORY_UI_KEY,{}) or {})
    key=str(chat_id)
    if marker is None:
        positions.pop(key,None)
    else:
        positions[key]=int(marker)
    store.set_ui(HISTORY_UI_KEY,positions)


async def import_next_page(client,sender,resolver,chat_id,thread_id) -> tuple[int,int,bool]:
    """Explicitly import one page, persisting progress *after* all acks.

    On any skipped/ambiguous delivery, leave the timestamp cursor intact so
    the operator can safely retry without duplicating confirmed TG parts.
    """
    from app.max_listener import import_recent_history
    store=sender.topic_store
    lock=getattr(client,"_history_locks",{}).setdefault(str(chat_id),asyncio.Lock())
    async with lock:
        previous=history_position(store,chat_id)
        if previous==0:
            return 0,0,True
        rows,next_time,at_start=await history_page(client,chat_id,previous)
        if not rows:
            set_history_position(store,chat_id,0)
            return 0,0,True
        unique={str(r["id"]):r for r in rows}
        to_send=[r for r in unique.values() if
                 store.tg_for_max_message(chat_id,r["id"]) is None]
        if to_send:
            count=await import_recent_history(
                client,sender,resolver,chat_id,thread_id,len(to_send),
                raw_messages=to_send)
            if count!=len(to_send) or any(
                store.tg_for_max_message(chat_id,r["id"]) is None for r in to_send):
                raise RuntimeError(
                    "Некоторые сообщения не подтверждены Telegram. "
                    "Позиция истории сохранена для безопасного повтора.")
        # First empty/short page establishes the end, rather than inventing
        # more history. Previous marker remains on error.
        if at_start:
            set_history_position(store,chat_id,0)
        elif next_time is not None:
            set_history_position(store,chat_id,next_time)
        else:
            raise RuntimeError("MAX не дал безопасный курсор следующей страницы.")
        return len(to_send),len(rows),at_start
