"""Personal bridge's operator-only, non-destructive incident triage."""
from unittest.mock import AsyncMock,MagicMock
from app.operator_issues import get_issues,format_issues,acknowledge_blocked_mutation
from app.message_sync import MessageSync
from app.outbox import OutboxStore
from app.topics import TopicStore
from app.tg_handler import (_cmd_issues,_cmd_issue_ack,MAX_CLIENT_KEY,
    ADMIN_USER_KEY,TG_OUTBOUND_KEY)
import pytest


def fixture(tmp_path):
    topics=TopicStore(str(tmp_path/"topics.json"))
    outbox=OutboxStore(str(tmp_path/"outbox.sqlite3"))
    sync=MessageSync(outbox,topics,MagicMock(),"-1001")
    key="abcd12345678"*5+"abcd"
    outbox.db.execute(
        "INSERT INTO message_mutations(key,chat_id,max_id,kind,payload,state,updated)"
        " VALUES(?,?,?,?,?,?,?)",
        (key,"42","max-one","edit",
         '{"message":{"text":"secret message","id":"max-one"}}',
         "BLOCKED",100))
    return topics,outbox,sync,key


def test_diagnostics_never_expose_text_or_private_ids(tmp_path):
    topics,outbox,sync,key=fixture(tmp_path)
    entries=get_issues(outbox)
    body=format_issues(entries)
    assert "secret message" not in body
    assert "max-one" not in body
    assert key not in body
    assert key[:12] in body
    assert "Нет подтверждённого сообщения" in body
    outbox.close()


def test_ack_marks_reviewed_not_applied(tmp_path):
    topics,outbox,sync,key=fixture(tmp_path)
    assert acknowledge_blocked_mutation(outbox,key[:12]) is True
    assert outbox.db.execute(
        "SELECT state FROM message_mutations WHERE key=?",(key,)
    ).fetchone()["state"]=="REVIEWED"
    assert get_issues(outbox)==[]
    with pytest.raises(ValueError):
        acknowledge_blocked_mutation(outbox,key[:12])
    outbox.close()


def test_invalid_ack_does_not_change_state(tmp_path):
    topics,outbox,sync,key=fixture(tmp_path)
    with pytest.raises(ValueError):
        acknowledge_blocked_mutation(outbox,"anything")
    with pytest.raises(ValueError):
        acknowledge_blocked_mutation(outbox,"abcd")
    assert outbox.db.execute(
        "SELECT state FROM message_mutations WHERE key=?",(key,)
    ).fetchone()["state"]=="BLOCKED"
    outbox.close()


@pytest.mark.asyncio
async def test_only_admin_can_review_blocked(tmp_path):
    topics,outbox,sync,key=fixture(tmp_path)
    message=MagicMock()
    message.reply_text=AsyncMock()
    update=MagicMock()
    update.message=message
    update.effective_user.id=111
    context=MagicMock()
    context.bot_data={
        ADMIN_USER_KEY:222,
        MAX_CLIENT_KEY:MagicMock(message_sync=sync),
    }
    context.args=[key[:12]]
    await _cmd_issue_ack(update,context)
    assert message.reply_text.await_count==0
    assert outbox.db.execute(
        "SELECT state FROM message_mutations"
    ).fetchone()["state"]=="BLOCKED"
    update.effective_user.id=222
    context.args=[]
    await _cmd_issues(update,context)
    assert key[:12] in message.reply_text.await_args.args[0]
    context.args=[key[:12]]
    await _cmd_issue_ack(update,context)
    assert outbox.db.execute(
        "SELECT state FROM message_mutations"
    ).fetchone()["state"]=="REVIEWED"
    outbox.close()


def test_delivery_codes_are_unique_and_all_categories_paginate(tmp_path):
    from app.max_client import MaxMessage
    from app.tg_outbox import TelegramOutbound
    topics,outbox,sync,key=fixture(tmp_path)
    reverse=TelegramOutbound(tmp_path/'tg-outbound.sqlite3',topics)
    reverse.enqueue(-100,815,42,{'text':'private','elements':[],'link':None});reverse._mark(-100,815,'FAILED')
    for i in range(20):
        k=outbox.enqueue(MaxMessage(chat_id=42,message_id=str(123400+i),text='private'))
        outbox._set_job(k,'UNKNOWN','Telegram delivery outcome uncertain')
        if i==0:
            outbox._insert_part(k,0,'send_media_group','digest',{},None);outbox._update_part(k,0,'SENT',123,[123,124,125])
    rows=[]
    for off in range(0,24,8):rows+=get_issues(outbox,reverse,limit=8,offset=off)
    assert len(rows)==22 and len({r['ref'] for r in rows})==22
    assert any(r['category']=='Telegram→MAX' for r in rows)
    assert any('подтверждённых частей: 3' in r['cause'] for r in rows)
    body=format_issues(rows[:8],has_next=True)
    assert '/issues 2' in body and 'private' not in body and '123400' not in body
    outbox.close();reverse.close()


@pytest.mark.asyncio
async def test_issue_command_second_page_and_bad_page(tmp_path):
    from app.max_client import MaxMessage
    topics,outbox,sync,key=fixture(tmp_path)
    for i in range(9):outbox._set_job(outbox.enqueue(MaxMessage(chat_id=42,message_id=str(i),text='x')),'UNKNOWN')
    u=MagicMock();u.effective_user.id=222;u.message.reply_text=AsyncMock();c=MagicMock();c.bot_data={ADMIN_USER_KEY:222,MAX_CLIENT_KEY:MagicMock(message_sync=sync)};c.args=['2']
    await _cmd_issues(u,c);assert 'Страница 2' in u.message.reply_text.await_args.args[0] and key[:12] in u.message.reply_text.await_args.args[0]
    c.args=['-1'];await _cmd_issues(u,c);assert 'НОМЕР_СТРАНИЦЫ' in u.message.reply_text.await_args.args[0];outbox.close()
