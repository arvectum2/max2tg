"""P0.3: edits, precise deletes and reactions; no destructive MAX delete RPC."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import ReactionTypeEmoji
from telegram.error import TimedOut

from app.max_client import MaxClient, OpCode
from app.message_sync import MessageSync, parse_mutation
from app.outbox import OutboxStore
from app.topics import TopicStore
from app.tg_handler import (
    _on_edited_topic_message, _on_message_reaction,
    TOPIC_STORE_KEY, MAX_CLIENT_KEY, SUPERGROUP_KEY, ALLOWED_USERS_KEY,
    _on_topic_message,
)

def bootstrap(tmp_path):
    topics=TopicStore(str(tmp_path/"topics.json"))
    topics.set_topic(42,777,"Test")
    ledger=OutboxStore(str(tmp_path/"outbox.sqlite3"))
    bot=MagicMock()
    bot.edit_message_text=AsyncMock(return_value=True)
    bot.edit_message_caption=AsyncMock(return_value=True)
    bot.delete_message=AsyncMock(return_value=True)
    bot.set_message_reaction=AsyncMock(return_value=True)
    sync=MessageSync(ledger,topics,bot,"-100222")
    return sync,topics,ledger,bot


def mark_bot_authored(sync,topics,ledger,mid, parts=None):
    key=ledger.job_key(42,mid)
    now=100000
    ledger.db.execute(
        "INSERT INTO jobs(key,chat_id,max_id,event,status,created,updated,tg_primary)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (key,"42",str(mid),"{}", "SENT",now,now,500),
    )
    for idx,(method,tgid) in enumerate(parts or [("send_message",500)]):
        ledger._insert_part(key,idx,method,str(idx),{},None)
        ledger._update_part(key,idx,"SENT",tgid)
    topics.set_message(42,mid,500)


async def test_max_edit_updates_original_tg_message_not_duplicate(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"777")
    packet={"chatId":42,"message":{"id":"777","status":"EDITED",
                                    "updateTime":1792,"text":"новый текст"}}
    assert await sync.accept(128,packet)
    bot.edit_message_text.assert_awaited_once()
    kwargs=bot.edit_message_text.await_args.kwargs
    assert kwargs["message_id"]==500
    assert "новый текст" in kwargs["text"]
    assert await sync.accept(128,packet) is False
    assert bot.edit_message_text.await_count==1
    ledger.close()


async def test_max_photo_caption_edited_without_resending_media(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"777",
                      parts=[("send_photo",500)])
    packet={"chatId":42,"message":{"id":"777","updateTime":27,
                                    "text":"подпись","status":"EDITED"}}
    assert await sync.accept(128,packet)
    bot.edit_message_caption.assert_awaited_once()
    bot.edit_message_text.assert_not_awaited()
    ledger.close()


async def test_max_delete_affects_only_messages_created_by_bridge(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"777",
                      parts=[("send_photo",500),("send_message",501)])
    assert await sync.accept(142,{"chatId":42,"messageIds":["777"]})
    ids=[c.kwargs["message_id"] for c in bot.delete_message.await_args_list]
    assert ids==[501,500]
    assert await sync.accept(142,{"chatId":42,"messageIds":["777"]}) is False
    assert bot.delete_message.await_count==2
    ledger.close()


async def test_delete_does_not_touch_tg_originated_user_message(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    topics.set_message(42,"111",555)
    assert not await sync.accept(142,{"chatId":42,"messageId":"111"})
    bot.delete_message.assert_not_awaited()
    ledger.close()


async def test_range_delete_must_not_mirror_mass_deletions(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"777")
    assert not await sync.accept(140,{"chatId":42,"messageIds":[777]})
    assert not await sync.accept(142,{"chatId":42,"messageIds":[777,778]})
    assert not await sync.accept(142,{"chatId":42,"messageIds":[777],"forMe":True})
    bot.delete_message.assert_not_awaited()
    ledger.close()


async def test_timedout_delete_is_not_retried(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"777")
    bot.delete_message.side_effect=TimedOut()
    assert not await sync.accept(142,{"chatId":42,"messageId":"777"})
    await sync.recover_pending()
    assert bot.delete_message.await_count==1
    row=ledger.db.execute("SELECT state FROM message_mutations").fetchone()
    assert row["state"]=="UNKNOWN"
    ledger.close()


async def test_reaction_from_own_max_profile_mirrored_only_for_known_emoji(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"777")
    payload={"chatId":42,"messageId":"777",
             "reactionInfo":{"yourReaction":"👍","totalCount":2}}
    assert await sync.accept(156,payload)
    assert bot.set_message_reaction.await_args.kwargs["reaction"][0].emoji=="👍"
    assert not await sync.accept(156,payload)
    # Other people's aggregate reactions must not be impersonated by the bot
    payload["reactionInfo"]={"totalCount":3}
    assert not await sync.accept(156,payload)
    assert bot.set_message_reaction.await_count==1
    ledger.close()


async def test_max_reaction_removed_when_own_reaction_empty(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"777")
    assert await sync.accept(155,{
        "chatId":42,"messageId":"777",
        "reactionInfo":{"yourReaction":"","totalCount":0},
    })
    assert bot.set_message_reaction.await_args.kwargs["reaction"]==[]
    ledger.close()


async def test_dispatch_edited_never_becomes_new_message(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"777")
    c=MaxClient("token","device")
    c._on_message_cb=AsyncMock()
    c._on_mutation_cb=AsyncMock(side_effect=sync.accept)
    await c._handle({"opcode":128,"cmd":0,"payload":{
        "chatId":42,"message":{"id":"777","text":"исправлено",
                                "status":"EDITED","updateTime":113}}})
    import asyncio
    await asyncio.sleep(0.02)
    c._on_message_cb.assert_not_awaited()
    c._on_mutation_cb.assert_awaited_once()
    ledger.close()


async def test_max_edit_rpc_has_required_wire_fields():
    c=MaxClient("token","device")
    c.cmd=AsyncMock(return_value={"message":{"id":"555","status":"EDITED"}})
    result=await c.edit_message(42,"555","новое",[{"type":"STRONG","from":0,"length":5}])
    assert result is not None
    args=c.cmd.await_args.args
    assert args[0]==67
    assert args[1]["attachments"]==[]
    assert args[1]["elements"][0]["type"]=="STRONG"
    assert args[1]["messageId"]=="555"


async def test_tg_to_max_edit_uses_mapping_and_never_creates_new_message(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    topics.set_message(42,"999",42)
    update=MagicMock()
    update.message=None
    edited=MagicMock()
    update.edited_message=edited
    edited.message_thread_id=777
    edited.is_topic_message=True
    edited.message_id=42
    edited.text="Исправлено"
    edited.entities=[]
    update.effective_user.id=111
    ctx=MagicMock()
    client=MagicMock()
    client.edit_message=AsyncMock(return_value={"message":{"id":"999"}})
    client.send_message=AsyncMock()
    ctx.bot_data={TOPIC_STORE_KEY:topics,MAX_CLIENT_KEY:client,
                  ALLOWED_USERS_KEY:frozenset({111})}
    await _on_edited_topic_message(update,ctx)
    client.edit_message.assert_awaited_once_with(42,"999","Исправлено",[])
    client.send_message.assert_not_awaited()
    ledger.close()


async def test_unmapped_tg_edit_is_ignored(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    update=MagicMock()
    update.message=None
    update.edited_message.message_thread_id=777
    update.edited_message.is_topic_message=True
    update.edited_message.message_id=15
    update.edited_message.text="unmapped"
    update.effective_user.id=111
    ctx=MagicMock()
    client=MagicMock()
    client.edit_message=AsyncMock()
    ctx.bot_data={TOPIC_STORE_KEY:topics,MAX_CLIENT_KEY:client,
                  ALLOWED_USERS_KEY:frozenset({111})}
    await _on_edited_topic_message(update,ctx)
    client.edit_message.assert_not_awaited()
    ledger.close()


async def test_tg_reaction_maps_to_max_id_not_topic_id(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    topics.set_message(42,"999",501)
    update=MagicMock()
    update.message_reaction.chat.id=-100222
    update.message_reaction.message_id=501
    update.message_reaction.user.id=111
    update.message_reaction.new_reaction=[ReactionTypeEmoji("👍")]
    ctx=MagicMock()
    client=MagicMock()
    client.react_to_message=AsyncMock(return_value={"reactionInfo":{}})
    ctx.bot_data={TOPIC_STORE_KEY:topics,MAX_CLIENT_KEY:client,
                  SUPERGROUP_KEY:-100222,ALLOWED_USERS_KEY:frozenset({111})}
    from app.tg_handler import _on_message_reaction
    await _on_message_reaction(update,ctx)
    client.react_to_message.assert_awaited_once_with("42","999","👍")
    ledger.close()


async def test_unauthorized_tg_reaction_is_ignored(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    topics.set_message(42,"999",501)
    update=MagicMock()
    update.message_reaction.chat.id=-100222
    update.message_reaction.message_id=501
    update.message_reaction.user.id=222
    update.message_reaction.new_reaction=[ReactionTypeEmoji("👍")]
    ctx=MagicMock()
    client=MagicMock()
    client.react_to_message=AsyncMock()
    ctx.bot_data={TOPIC_STORE_KEY:topics,MAX_CLIENT_KEY:client,
                  SUPERGROUP_KEY:-100222,ALLOWED_USERS_KEY:frozenset({111})}
    from app.tg_handler import _on_message_reaction
    await _on_message_reaction(update,ctx)
    client.react_to_message.assert_not_awaited()
    ledger.close()


async def test_late_old_edit_cannot_undo_a_newer_version(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"777")
    new={"chatId":42,"message":{"id":"777","status":"EDITED",
        "updateTime":300,"text":"новейший"}}
    old={"chatId":42,"message":{"id":"777","status":"EDITED",
        "updateTime":200,"text":"старый"}}
    assert await sync.accept(128,new)
    assert not await sync.accept(128,old)
    bot.edit_message_text.assert_awaited_once()
    ledger.close()


async def test_reconnect_catches_edit_of_existing_mapped_message(tmp_path):
    from app.max_listener import catch_up_chat_history
    from tests.test_self_forward import bridge
    client,sender,state=bridge(tmp_path)
    client._my_id=100
    sync,_,ledger,bot=bootstrap(tmp_path/"mut")
    # Mutation subsystem sees same TopicStore as catch-up.
    sync.topics=state
    client.message_sync=sync
    state.set_message(42,"1",500)
    state.set_history_cursor(42,1000)
    # Bot-authored outbox ledger entry is required for safe editing.
    mark_bot_authored(sync,state,ledger,"1")
    client.fetch_history=AsyncMock(return_value={"messages":[{
        "id":"1","time":1000,"sender":200,
        "text":"исправленное",
        "status":"EDITED","updateTime":4000,
    }]})
    assert await catch_up_chat_history(client,sender,client.resolver,42,777)==0
    bot.edit_message_text.assert_awaited_once()
    sender.send.assert_not_awaited()
    ledger.close()


async def test_explicit_max_delete_outbound_is_not_available():
    c=MaxClient("t","d")
    assert not hasattr(c,"delete_message")
    assert "DELETE" not in OpCode.__members__


async def test_separate_telegram_handler_detects_reaction_only_in_right_group():
    from app.tg_handler import build_tg_app
    from telegram.ext import MessageReactionHandler
    app=build_tg_app("12345:ABCDEFGH",MaxClient("t","d"),"-100123",
                     TopicStore("/tmp/max2tg_p03_handler_probe_nonexistent.json"),
                     allowed_user_id=111)
    assert any(isinstance(h,MessageReactionHandler)
               for handlers in app.handlers.values() for h in handlers)

async def test_edit_of_previous_multipart_message_replaces_stale_parts(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"777",
                      parts=[("send_message",500),("send_message",501)])
    assert await sync.accept(128,{"chatId":42,"message":{
        "id":"777","status":"EDITED","updateTime":19,"text":"short",
    }})
    assert bot.edit_message_text.await_count==2
    assert ledger.db.execute(
        "SELECT state FROM message_mutations").fetchone()["state"]=="APPLIED"
    ledger.close()


async def test_max_edit_multipart_text_updates_all_bot_owned_parts(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"multi",
        parts=[("send_message",500),("send_message",501)])
    packet={"chatId":42,"message":{"id":"multi","status":"EDITED",
        "updateTime":123,"text":"Исправлено в MAX"}}
    assert await sync.accept(128,packet) is True
    assert [v.kwargs["message_id"] for v in
            bot.edit_message_text.await_args_list]==[500,501]
    assert "продолжение изменено" in (
        bot.edit_message_text.await_args_list[1].kwargs["text"])
    ledger.close()


async def test_max_edit_multipart_media_stays_blocked(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"multi",
        parts=[("send_photo",500),("send_message",501)])
    assert not await sync.accept(128,{"chatId":42,"message":{
        "id":"multi","status":"EDITED","updateTime":123,
        "text":"Новая подпись"}})
    bot.edit_message_text.assert_not_awaited()
    ledger.close()


async def test_max_multipart_edit_refuses_more_parts_than_previously_exist(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"multi",
        parts=[("send_message",500),("send_message",501)])
    assert not await sync.accept(128,{"chatId":42,"message":{
        "id":"multi","status":"EDITED","updateTime":124,
        "text":"Хлеб "*3500}})
    bot.edit_message_text.assert_not_awaited()
    ledger.close()
