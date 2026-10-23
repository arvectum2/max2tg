"""P1.4 per-chat delivery preferences: store, routing, UI, restart safety."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chat_preferences import (get_chat_prefs,set_chat_pref,remove_chat_prefs,
                                  ChatPreferences,HISTORY_CHOICES)
from app.chat_settings_ui import (cmd_chatsettings,callback_chatsettings,
                                  _format_settings,_markup)
from app.topics import TopicStore
from app.max_client import MaxMessage
from app.max_listener import _send_message_content, import_recent_history
from app.tg_handler import (_on_topic_message,_on_topic_media,
                            _on_message_reaction,_menu_entries,
                            _menu_list_markup, MAX_CLIENT_KEY,TOPIC_STORE_KEY,
                            SUPERGROUP_KEY,ALLOWED_USERS_KEY,ADMIN_USER_KEY)
from tests.test_outbox import fixture as bridge_fixture, event


def setup(tmp_path, user=111, chat=42):
    client,sender,store,db=bridge_fixture(tmp_path)
    store.set_topic(42,777,"Хлеб")
    store.set_topic(99,999,"Пекарня")
    client._history_locks={}
    client._authorized=False
    client.resolver.chat_name=lambda cid: {42:"Хлеб",99:"Пекарня"}.get(int(cid),str(cid))
    update=MagicMock()
    update.message=MagicMock()
    update.message.message_thread_id=777
    update.message.is_topic_message=True
    update.message.chat_id=-100222
    update.message.reply_text=AsyncMock()
    update.message.text="/chatsettings"
    update.effective_user.id=user
    update.callback_query=None
    context=MagicMock()
    context.bot_data={
        MAX_CLIENT_KEY:client,TOPIC_STORE_KEY:store,SUPERGROUP_KEY:-100222,
        ALLOWED_USERS_KEY:frozenset({111,222}),ADMIN_USER_KEY:111
    }
    context.user_data={}
    context.args=[]
    context.bot=MagicMock()
    context.bot.edit_forum_topic=AsyncMock()
    return update,context,client,sender,store,db


def cb(update, action, chat_id=42):
    query=MagicMock()
    query.data=(f"chatpref:{action}" if ":" in action
                else f"chatpref:{action}:{chat_id}")
    query.message=MagicMock()
    query.message.chat.id=-100222
    query.message.reply_text=AsyncMock()
    query.answer=AsyncMock()
    update.callback_query=query
    update.message=None
    return query


def test_defaults_do_not_change_existing_chats(tmp_path):
    store=TopicStore(str(tmp_path/"topics.json"))
    store.set_topic(42,777,"Хлеб")
    assert get_chat_prefs(store,42)==ChatPreferences()
    assert get_chat_prefs(store,99)==ChatPreferences()
    assert get_chat_prefs(store,42).effective_history(50)==50


@pytest.mark.parametrize("flag",["sync","media","mute","archived","title_sync"])
def test_boolean_preferences_persist_by_chat_only(tmp_path,flag):
    path=str(tmp_path/"state.json")
    store=TopicStore(path)
    set_chat_pref(store,42,flag,not getattr(ChatPreferences(),flag))
    assert getattr(get_chat_prefs(TopicStore(path),42),flag)==(
        not getattr(ChatPreferences(),flag))
    assert get_chat_prefs(store,99)==ChatPreferences()


@pytest.mark.parametrize("depth", [0,20,50,100,None])
def test_history_depth_persists_and_inherits_global(tmp_path,depth):
    path=str(tmp_path/"state.json")
    store=TopicStore(path)
    set_chat_pref(store,42,"history_depth",depth)
    restored=get_chat_prefs(TopicStore(path),42)
    assert restored.history_depth==depth
    assert restored.effective_history(50)==(50 if depth is None else depth)


@pytest.mark.parametrize("field,value",[
    ("sync",1),("mute","False"),("archived",None),
    ("history_depth",500),("history_depth",True),("history_depth","50"),
    ("token","whatever"),
])
def test_bad_preference_value_rejected(tmp_path,field,value):
    with pytest.raises(ValueError):
        set_chat_pref(TopicStore(str(tmp_path/"p.json")),42,field,value)


def test_corrupted_prefs_fall_back_to_defaults(tmp_path):
    s=TopicStore(str(tmp_path/"t.json"))
    s.set_ui("chat_preferences",{"42":{
        "sync":"yes","mute":1,"media":None,"history_depth":1000}})
    assert get_chat_prefs(s,42)==ChatPreferences()


def test_delete_topic_clears_own_preferences_but_not_other_chats(tmp_path):
    path=str(tmp_path/"p.json")
    s=TopicStore(path)
    s.set_topic(42,777,"Первый")
    s.set_topic(99,999,"Второй")
    set_chat_pref(s,42,"mute",True)
    set_chat_pref(s,99,"mute",True)
    s.remove(42)
    r=TopicStore(path)
    assert get_chat_prefs(r,42).mute is False
    assert get_chat_prefs(r,99).mute is True
    assert r.get_topic(99)==999


async def test_per_chat_pause_does_not_forward_live_max_message(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    set_chat_pref(store,42,"sync",False)
    sender._bot.send_message=AsyncMock()
    await client._on_message_cb(event("paused","Привет"))
    sender._bot.send_message.assert_not_awaited()
    assert db.counts()=={}
    assert store.tg_for_max_message(42,"paused") is None
    db.close()


async def test_per_chat_pause_does_not_block_other_max_chat(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    set_chat_pref(store,42,"sync",False)
    other=event("otherchat","Текст")
    other.chat_id=99
    sender._bot.send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=335))
    await client._on_message_cb(other)
    sender._bot.send_message.assert_awaited()
    assert store.tg_for_max_message(99,"otherchat")==335
    db.close()


async def test_paused_tg_message_is_rejected_with_feedback(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    set_chat_pref(store,42,"sync",False)
    update.message.text="Привет"
    update.message.message_id=909
    update.message.entities=[]
    update.message.reply_to_message=None
    client.send_message=AsyncMock()
    await _on_topic_message(update,ctx)
    client.send_message.assert_not_awaited()
    assert "приостановлена" in update.message.reply_text.await_args.args[0]
    db.close()


async def test_paused_tg_media_does_not_trigger_upload(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    set_chat_pref(store,42,"sync",False)
    update.message.photo=[MagicMock()]
    client.upload_photo=AsyncMock()
    await _on_topic_media(update,ctx)
    client.upload_photo.assert_not_awaited()
    assert "приостановлена" in update.message.reply_text.await_args.args[0]
    db.close()


async def test_disabled_media_still_delivers_text_marker_no_download(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    set_chat_pref(store,42,"media",False)
    msg=event("media-disabled","Описание")
    msg.attaches=[{"_type":"PHOTO","url":"https://i.oneme.ru/secret"}]
    client.download_file=AsyncMock()
    sender._bot.send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=32))
    await client._on_message_cb(msg)
    client.download_file.assert_not_awaited()
    text=sender._bot.send_message.await_args.kwargs["text"]
    assert "Описание" in text
    assert "вложения отключены" in text
    assert "secret" not in text
    assert store.tg_for_max_message(42,"media-disabled")==32
    db.close()


async def test_disabled_media_in_tg_is_rejected_before_upload(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    set_chat_pref(store,42,"media",False)
    update.message.photo=[MagicMock()]
    client.upload_photo=AsyncMock()
    await _on_topic_media(update,ctx)
    client.upload_photo.assert_not_awaited()
    assert "Медиа отключены" in update.message.reply_text.await_args.args[0]
    db.close()


async def test_mute_applies_only_one_topic_and_includes_albums(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    set_chat_pref(store,42,"mute",True)
    sender._bot.send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=1))
    sender._bot.send_media_group=AsyncMock(return_value=[
        SimpleNamespace(message_id=2),SimpleNamespace(message_id=3)])
    await sender.send("<b>Тихо</b>",message_thread_id=777)
    assert sender._bot.send_message.await_args.kwargs["disable_notification"] is True
    await sender.send("Обычно",message_thread_id=999)
    assert "disable_notification" not in sender._bot.send_message.await_args.kwargs
    await sender.send_photo_album([b"p1",b"p2"],message_thread_id=777)
    assert sender._bot.send_media_group.await_args.kwargs["disable_notification"] is True
    db.close()


def test_archived_chats_hidden_but_not_deleted(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    resolver=MagicMock()
    resolver.chats_raw={42:{"status":"ACTIVE"},99:{"status":"ACTIVE"}}
    resolver.chats={42:"Хлеб",99:"Пекарня"}
    resolver.chat_types={42:"CHAT",99:"CHAT"}
    resolver.is_dm=lambda cid:False
    resolver.chat_name=lambda cid:{42:"Хлеб",99:"Пекарня"}[cid]
    set_chat_pref(store,42,"archived",True)
    active=_menu_entries(resolver,store)
    archive=_menu_entries(resolver,store,archived=True)
    assert [row[1] for row in active]==[99]
    assert [row[1] for row in archive]==[42]
    markup,*_= _menu_list_markup(resolver,store,archived=True)
    assert any(button.callback_data=="menu:chat:42:0"
               for row in markup.inline_keyboard for button in row)
    assert store.get_topic(42)==777
    db.close()


async def test_per_chat_history_depth_applies_to_auto_history(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    set_chat_pref(store,42,"history_depth",0)
    client.fetch_history=AsyncMock()
    result=await import_recent_history(client,sender,client.resolver,
                                        42,777,get_chat_prefs(store,42).effective_history(20))
    assert result==0
    client.fetch_history.assert_not_awaited()
    assert get_chat_prefs(store,99).effective_history(20)==20
    db.close()


async def test_settings_command_shows_only_own_bound_topic(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    await cmd_chatsettings(update,ctx)
    assert "Настройки чата" in update.message.reply_text.await_args.args[0]
    assert "Хлеб" in update.message.reply_text.await_args.args[0]
    update.message.reply_text.reset_mock()
    update.message.message_thread_id=None
    await cmd_chatsettings(update,ctx)
    assert "Открой тему" in update.message.reply_text.await_args.args[0]
    db.close()


async def test_settings_command_requires_admin(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path,user=222)
    await cmd_chatsettings(update,ctx)
    update.message.reply_text.assert_not_awaited()
    db.close()


async def test_settings_callback_persists_toggle_and_respects_admin(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    q=cb(update,"toggle:42:mute")
    await callback_chatsettings(update,ctx)
    assert get_chat_prefs(store,42).mute is True
    assert get_chat_prefs(TopicStore(str(tmp_path/"topics.json")),42).mute is True
    update.effective_user.id=222
    q=cb(update,"toggle:42:media")
    await callback_chatsettings(update,ctx)
    assert get_chat_prefs(store,42).media is True
    db.close()


async def test_settings_history_callback_zero_and_restore_default(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    cb(update,"depth:42:0")
    await callback_chatsettings(update,ctx)
    assert get_chat_prefs(store,42).history_depth==0
    cb(update,"depth:42:default")
    await callback_chatsettings(update,ctx)
    assert get_chat_prefs(store,42).history_depth is None
    db.close()


async def test_settings_callback_foreign_group_is_ignored(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    q=cb(update,"toggle:42:sync")
    q.message.chat.id=-100333
    await callback_chatsettings(update,ctx)
    assert get_chat_prefs(store,42).sync is True
    db.close()


async def test_settings_invalid_chat_does_not_change_preferences(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    q=cb(update,"toggle:999999:mute")
    await callback_chatsettings(update,ctx)
    assert get_chat_prefs(store,999999)==ChatPreferences()
    db.close()


async def test_topic_name_auto_sync_optin_and_manual_default(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    sender._bot.edit_forum_topic=AsyncMock(return_value=True)
    await sender.ensure_topic(42,"Новый заголовок MAX")
    sender._bot.edit_forum_topic.assert_not_awaited()
    set_chat_pref(store,42,"title_sync",True)
    await sender.ensure_topic(42,"Новый заголовок MAX")
    sender._bot.edit_forum_topic.assert_awaited_once()
    assert store.get_title(42)=="Новый заголовок MAX"
    db.close()


async def test_paused_max_mutations_not_recorded_or_sent(tmp_path):
    from app.message_sync import MessageSync
    update,ctx,client,sender,store,db=setup(tmp_path)
    sync=MessageSync(db,store,sender._bot,-100222)
    set_chat_pref(store,42,"sync",False)
    sender._bot.edit_message_text=AsyncMock()
    result=await sync.accept(128,{"chatId":42,"message":{
        "id":"ed","text":"новое","status":"EDITED","updateTime":4}})
    assert result is False
    assert db.db.execute("select count(*) from message_mutations").fetchone()[0]==0
    sender._bot.edit_message_text.assert_not_awaited()
    db.close()


async def test_paused_history_load_does_not_fetch_or_change_cursor(tmp_path):
    from app.history_ui import callback_history
    update,ctx,client,sender,store,db=setup(tmp_path)
    set_chat_pref(store,42,"sync",False)
    query=MagicMock()
    query.data="history:load:42"
    query.answer=AsyncMock()
    query.message.chat.id=-100222
    query.message.is_topic_message=True
    query.message.message_thread_id=777
    query.message.reply_text=AsyncMock()
    update.callback_query=query
    update.message=None
    client.fetch_history=AsyncMock()
    await callback_history(update,ctx)
    assert "приостановлена" in query.message.reply_text.await_args.args[0]
    client.fetch_history.assert_not_awaited()
    db.close()


async def test_resume_sync_retains_checkpoint_and_enables_recovery(tmp_path):
    update,ctx,client,sender,store,db=setup(tmp_path)
    store.set_history_cursor(42,1700000000000)
    set_chat_pref(store,42,"sync",False)
    query=cb(update,"toggle:42:sync")
    await callback_chatsettings(update,ctx)
    assert get_chat_prefs(store,42).sync is True
    assert store.get_history_cursor(42)==1700000000000
    db.close()


async def test_paused_pending_mutation_does_not_recover_until_resume(tmp_path):
    from app.message_sync import MessageSync
    update,ctx,client,sender,store,db=setup(tmp_path)
    sync=MessageSync(db,store,sender._bot,-100222)
    db.db.execute("""INSERT INTO message_mutations
        (key,chat_id,max_id,kind,payload,state,updated)
        VALUES('p','42','edit','edit','{"message":{"text":"ed"}}','PENDING',1)""")
    set_chat_pref(store,42,"sync",False)
    sender._bot.edit_message_text=AsyncMock()
    await sync.recover_pending()
    assert db.db.execute("select state from message_mutations where key='p'").fetchone()[0]=="PENDING"
    sender._bot.edit_message_text.assert_not_awaited()
    db.close()
