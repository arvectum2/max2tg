"""P1.3 read-only MAX search and administrator-triggered history import."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.history_nav import (
    SearchSpec,parse_search,history_page,search_page,SearchPage,
    timestamp_ms,next_marker,history_position,import_next_page,
    set_history_position,SEARCH_MAX_CHATS,
)
from app.history_ui import (
    cmd_find,callback_find,cmd_history,callback_history,_html_result,
    _all_bound_chats,
)
from app.topics import TopicStore
from app.max_client import MaxClient
from tests.test_outbox import fixture as bridge_fixture


def raw(num,ts,text=None):
    # MAX returns Unix milliseconds, not seconds.
    if ts < 10_000_000_000:
        ts=1_700_000_000_000+ts
    return {"id":str(num),"time":ts,"sender":200,
            "text":text if text is not None else f"Message {num}"}


def source(items):
    items=sorted(items,key=lambda x:(x["time"],str(x["id"])),reverse=True)
    async def fetch(chat_id,count=100,from_time=None):
        return {"messages":[m for m in items if
                            m["time"] < from_time][:count]}
    return AsyncMock(side_effect=fetch)


def ui(tmp_path,*,admin=111,thread_id=777,group=-100222):
    client,sender,store,outbox=bridge_fixture(tmp_path)
    store.set_topic(42,thread_id,"Музей хлеба")
    event=MagicMock()
    event.message.reply_text=AsyncMock()
    event.message.message_thread_id=thread_id
    event.message.is_topic_message=True
    event.message.chat_id=group
    event.effective_user.id=admin
    event.callback_query=None
    from app.tg_handler import (
        MAX_CLIENT_KEY,TOPIC_STORE_KEY,SUPERGROUP_KEY,
        ADMIN_USER_KEY,ALLOWED_USERS_KEY,
    )
    context=MagicMock()
    context.user_data={}
    context.args=[]
    context.bot_data={
        MAX_CLIENT_KEY:client,
        TOPIC_STORE_KEY:store,
        SUPERGROUP_KEY:group,
        ADMIN_USER_KEY:111,
        ALLOWED_USERS_KEY:frozenset({111,222}),
    }
    return event,context,client,sender,store,outbox


def button(event,data):
    q=MagicMock()
    q.data=data
    q.answer=AsyncMock()
    q.message.reply_text=AsyncMock()
    q.message.is_topic_message=True
    q.message.message_thread_id=777
    q.message.chat.id=-100222
    event.callback_query=q
    event.message=None
    return q


def test_search_date_parser_inclusive_and_moscow_timezone():
    spec=parse_search("музей хлеб с:2026-10-21 по:2026-10-22")
    assert spec.phrase=="музей хлеб"
    assert spec.since_ms==int(datetime.fromisoformat("2026-10-21T00:00:00+03:00").timestamp()*1000)
    assert spec.until_ms==int(datetime.fromisoformat("2026-10-23T00:00:00+03:00").timestamp()*1000)
    assert spec.matches(raw(1,spec.since_ms,"музей ХЛЕБ"))
    assert spec.matches(raw(2,spec.until_ms-1,"Музей хлеб"))
    assert not spec.matches(raw(3,spec.until_ms,"Музей хлеб"))
    assert not spec.matches(raw(4,spec.since_ms-1,"музей хлеб"))


@pytest.mark.parametrize("bad",[
    "","a", "слово с:2026-13-04",
    "слово с:2026-10-22 по:2026-10-20",
    "слово с:2026-10-21 с:2026-10-22",
    "x"*501,
])
def test_bad_search_rejected_before_rpc(bad):
    with pytest.raises(ValueError):
        parse_search(bad)


async def test_search_one_page_filters_text_without_telegram_send(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    client.fetch_history=source([
        raw(1,1000,"Хлеб любим"),raw(2,2000,"Другая тема"),
        raw(3,3000,"Музей ХЛЕБ")])
    ctx.args=["хлеб"]
    await cmd_find(event,ctx)
    response=event.message.reply_text.await_args.args[0]
    assert "Музей хлеба" in response
    assert "Музей ХЛЕБ" in response
    assert "Хлеб любим" in response
    assert "Другая тема" not in response
    assert store.tg_for_max_message(42,3) is None
    sender._bot.send_message.assert_not_called()
    outbox.close()


async def test_search_across_chats_with_scope_and_result_links(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    store.set_topic(99,999,"Второй чат")
    store.set_message(42,1,530)
    client.fetch_history=AsyncMock(side_effect=lambda chat_id,**kw:{
        "messages":[raw(1,2000,"контракт")] if str(chat_id)=="42"
                   else [raw(2,3000,"контракт")]})
    ctx.args=["контракт"]
    await cmd_find(event,ctx,all_chats=True)
    result=event.message.reply_text.await_args.args[0]
    assert "Второй чат" in result
    assert "Музей хлеба" in result
    assert "https://t.me/c/222/530" in result
    assert "ещё не импортировано" in result
    outbox.close()


async def test_find_message_without_args_is_help_and_never_queries_max(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    client.fetch_history=AsyncMock()
    await cmd_find(event,ctx)
    assert "/findall" in event.message.reply_text.await_args.args[0]
    client.fetch_history.assert_not_awaited()
    outbox.close()


async def test_admin_only_search_does_not_disclose_content(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path,admin=222)
    client.fetch_history=AsyncMock()
    ctx.args=["секрет"]
    await cmd_find(event,ctx)
    event.message.reply_text.assert_not_awaited()
    client.fetch_history.assert_not_awaited()
    outbox.close()


async def test_search_result_overflow_kept_in_memory_not_dropped(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    client.fetch_history=source([raw(i,i*1000,"секрет")
                                 for i in range(1,26)])
    ctx.args=["секрет"]
    await cmd_find(event,ctx)
    session=ctx.user_data["max_message_search_session"]
    assert len(session["pending"])==17
    assert len(session["seen"])==25
    first=event.message.reply_text.await_args.args[0]
    assert first.count("секрет")>=8
    calls=client.fetch_history.await_count
    token=ctx.user_data["max_message_search_session"]["token"]
    q=button(event,f"find:more:{token}")
    await callback_find(event,ctx)
    assert client.fetch_history.await_count==calls
    second=q.message.reply_text.await_args.args[0]
    assert second.count("секрет")>=8
    assert len(session["pending"])==9
    await callback_find(event,ctx)
    assert len(session["pending"])==1
    await callback_find(event,ctx)
    assert session["pending"]==[]
    outbox.close()


async def test_search_next_page_deduplicates_overlapping_timestamp(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    # MAX search pages overlap 1 ms to avoid skipping timestamp ties.
    # 100 entries on page one, an overlap of 1 item on page two.
    client.fetch_history=source([
        raw(i,i*1000,"хлеб") for i in range(1,106)
    ])
    ctx.args=["хлеб"]
    await cmd_find(event,ctx)
    session=ctx.user_data["max_message_search_session"]
    assert len(session["seen"])==100
    # Drain 12 batches of 8, then 4 remaining.
    token=ctx.user_data["max_message_search_session"]["token"]
    q=button(event,f"find:more:{token}")
    for _ in range(12):
        await callback_find(event,ctx)
    await callback_find(event,ctx)
    assert client.fetch_history.await_count==2
    assert len(session["seen"])==105
    outbox.close()


async def test_search_ambiguous_same_timestamp_is_explicit_error(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    client.fetch_history=source([raw(i,1000,"хлеб") for i in range(105)])
    ctx.args=["хлеб"]
    await cmd_find(event,ctx)
    assert "Нельзя безопасно продолжить" in event.message.reply_text.await_args.args[0]
    assert "max_message_search_session" not in ctx.user_data
    outbox.close()


async def test_search_timeout_keeps_user_data_untouched(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    client.fetch_history=AsyncMock(return_value=None)
    ctx.args=["договор"]
    await cmd_find(event,ctx)
    assert "MAX не вернул" in event.message.reply_text.await_args.args[0]
    assert "max_message_search_session" not in ctx.user_data
    outbox.close()


async def test_history_explicit_click_imports_and_does_not_duplicate(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    client.fetch_history=source([
        raw(i,i*1000,"Старое сообщение") for i in range(1,24)
    ])
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=789))
    await cmd_history(event,ctx)
    assert "История MAX" in event.message.reply_text.await_args.args[0]
    client.fetch_history.assert_not_awaited()
    q=button(event,"history:load:42")
    await callback_history(event,ctx)
    assert "добавлено 20" in q.message.reply_text.await_args.args[0]
    assert history_position(store,42) is not None
    old_cursor=history_position(store,42)
    count=sender._bot.send_message.await_count
    # Replaying the older window sees one duplicated overlap but only sends new.
    await callback_history(event,ctx)
    assert history_position(store,42)==0
    assert sender._bot.send_message.await_count==23
    assert old_cursor > 0
    old_count=sender._bot.send_message.await_count
    await callback_history(event,ctx)
    assert sender._bot.send_message.await_count==old_count
    outbox.close()


async def test_history_cursor_survives_reload_and_reuses_same_position(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    set_history_position(store,42,5000)
    restored=TopicStore(str(tmp_path/"topics.json"))
    assert history_position(restored,42)==5000
    set_history_position(restored,42,0)
    assert history_position(TopicStore(str(tmp_path/"topics.json")),42)==0
    outbox.close()


async def test_history_failed_delivery_does_not_advance_cursor(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    client.fetch_history=source([raw(i,i*1000) for i in range(1,3)])
    sender._bot.send_message=AsyncMock(return_value=None)
    with pytest.raises(RuntimeError,match="Позиция истории сохранена"):
        await import_next_page(client,sender,client.resolver,42,777)
    assert history_position(store,42) is None
    assert outbox.counts()=={"UNKNOWN":1}
    outbox.close()


async def test_history_wrong_topic_or_unauthorized_button_cannot_import(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    client.fetch_history=AsyncMock()
    q=button(event,"history:load:99")
    await callback_history(event,ctx)
    client.fetch_history.assert_not_awaited()
    assert history_position(store,42) is None
    event.effective_user.id=222
    q=button(event,"history:load:42")
    await callback_history(event,ctx)
    client.fetch_history.assert_not_awaited()
    outbox.close()


async def test_history_reset_requires_admin_and_stays_in_correct_topic(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    set_history_position(store,42,12345)
    q=button(event,"history:reset:42")
    await callback_history(event,ctx)
    assert history_position(store,42) is None
    outbox.close()


def test_search_previews_escape_user_content_and_bound_size(tmp_path):
    store=TopicStore(str(tmp_path/"topics.json"))
    store.set_topic(42,777,"<private&chat>")
    spec=SearchSpec("xx")
    hits=[("42",raw(1,1000,'<script>alert("bad")</script>'+("X"*2000)))]
    page=SearchPage(hits,{"42":0},True,1)
    html=_html_result(page,spec,store,-100222)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;private&amp;chat&gt;" in html
    assert len(html)<1000


def test_at_most_twelve_chats_and_no_leak_to_overlapping_results():
    client=MaxClient("t","d")
    with pytest.raises(ValueError):
        asyncio.run(search_page(client,[str(i) for i in range(13)],SearchSpec("abc")))


def test_date_only_search_is_valid_and_filters_without_text():
    spec=parse_search("с:2026-10-01 по:2026-10-23")
    assert spec.phrase==""
    assert spec.matches(raw(1,spec.since_ms,"любое сообщение"))
    assert not spec.matches(raw(2,spec.until_ms,"любое сообщение"))


def test_malformed_date_prefix_is_rejected_not_treated_as_text():
    for word in ("с:2026-13-40","по:не-дата"):
        with pytest.raises(ValueError):
            parse_search(f"поиск {word}")


async def test_old_find_callback_cannot_advance_new_query(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    client.fetch_history=source([raw(i,i*1000,"архив")
                                 for i in range(120)])
    ctx.args=["архив"]
    await cmd_find(event,ctx)
    first=ctx.user_data["max_message_search_session"]["token"]
    ctx.args=["другой"]
    await cmd_find(event,ctx)
    current=ctx.user_data["max_message_search_session"]
    assert current["token"] != first
    q=button(event,f"find:more:{first}")
    fetched=client.fetch_history.await_count
    await callback_find(event,ctx)
    assert client.fetch_history.await_count==fetched
    q.message.reply_text.assert_not_awaited()
    assert ctx.user_data["max_message_search_session"] is current
    outbox.close()


async def test_removed_topic_clears_its_history_cursor(tmp_path):
    state=TopicStore(str(tmp_path/"topics.json"))
    state.set_topic(42,777,"Temporary")
    set_history_position(state,42,1700000000000)
    state.remove(42)
    assert history_position(TopicStore(str(tmp_path/"topics.json")),42) is None


async def test_search_keeps_only_truncated_text_not_media_tokens(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    msg=raw("token-test",1700000000000,"музей "+("X"*2000))
    msg["attaches"]=[{"url":"https://cdn.oneme.ru/?secret=private"}]
    client.fetch_history=AsyncMock(return_value={"messages":[msg]})
    ctx.args=["музей"]
    await cmd_find(event,ctx)
    session=ctx.user_data["max_message_search_session"]
    assert len(str(session))<1500
    assert "private" not in str(session)
    assert "attaches" not in str(session)
    outbox.close()


async def test_invalid_id_in_history_page_does_not_advance_cursor(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    client.fetch_history=AsyncMock(return_value={"messages":[
        {"time":1700000000000,"text":"broken message"},
    ]})
    with pytest.raises(RuntimeError,match="без ID"):
        await import_next_page(client,sender,client.resolver,42,777)
    assert history_position(store,42) is None
    sender._bot.send_message.assert_not_called()
    outbox.close()


async def test_callback_from_other_supergroup_cannot_search_or_import(tmp_path):
    event,ctx,client,sender,store,outbox=ui(tmp_path)
    client.fetch_history=AsyncMock()
    q=button(event,"history:load:42")
    q.message.chat.id=-100333
    await callback_history(event,ctx)
    client.fetch_history.assert_not_awaited()
    event.message=MagicMock()
    event.message.reply_text=AsyncMock()
    event.message.is_topic_message=True
    event.message.message_thread_id=777
    ctx.args=["архив"]
    client.fetch_history=source([raw(1,1700000000000,"архив")])
    await cmd_find(event,ctx)
    token=ctx.user_data["max_message_search_session"]["token"]
    q=button(event,f"find:more:{token}")
    q.message.chat.id=-100333
    count=client.fetch_history.await_count
    await callback_find(event,ctx)
    assert client.fetch_history.await_count==count
    outbox.close()
