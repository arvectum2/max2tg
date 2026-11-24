from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.service_events import is_membership_event
from app.max_listener import import_recent_history
from app.max_client import MaxMessage
from app.history_nav import import_next_page,history_position
from tests.test_outbox import fixture

@pytest.mark.parametrize('kind',['joinByLink','leave','remove','new','rename','newKind'])
def test_membership_only(kind):
    a=[{'_type':'CONTROL','event':kind}]
    assert is_membership_event(a)
    assert not is_membership_event(a,'text')
    assert not is_membership_event(a+[{'_type':'PHOTO'}])
    assert not is_membership_event(a,link={'type':'FORWARD'})
    assert not is_membership_event([{'_type':'CONTROL','event':'system','message':'Useful notification'}])

def raw(mid,attaches,text=''):
    return dict(id=str(mid),time=1700000000000+mid*1000,sender=200,text=text,attaches=attaches)

@pytest.mark.asyncio
async def test_manual_page_skips_service_without_fake_receipts(tmp_path):
    client,sender,store,db=fixture(tmp_path)
    client.fetch_history=AsyncMock(return_value={'messages':[raw(1,[{'_type':'CONTROL','event':'leave'}]),raw(2,[],'real')]})
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=801))
    report={}
    assert await import_next_page(client,sender,client.resolver,42,777,report=report)==(1,2,True)
    assert report['service_skipped']==1 and history_position(store,42)==0
    assert db.job(db.job_key(42,'1')) is None
    assert store.tg_for_max_message(42,'1') is None
    sender._bot.send_message.assert_awaited_once()
    db.close()

@pytest.mark.asyncio
async def test_live_membership_does_not_enqueue(tmp_path):
    client,sender,store,db=fixture(tmp_path)
    await client._on_message_cb(MaxMessage(chat_id=42,message_id='1',attaches=[{'_type':'CONTROL','event':'joinByLink'}]))
    assert not db.counts()
    sender._bot.send_message.assert_not_called()
    db.close()

@pytest.mark.asyncio
async def test_history_uses_bound_title_if_resolver_missing(tmp_path):
    client,sender,store,db=fixture(tmp_path)
    store.set_topic(-42,778,'Тестовая группа');client.resolver.chats={};client.resolver.chat_types={}
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=801))
    assert await import_recent_history(client,sender,client.resolver,'-42',778,1,raw_messages=[raw(2,[],'real')])==1
    assert 'Тестовая группа' in sender._bot.send_message.await_args.kwargs['text']
    db.close()

def test_resolver_normalizes_id_without_truncation():
    from app.resolver import ContactResolver
    r=ContactResolver();r.chats[-12345678901234]='Тестовая группа';r.chat_types[42]='DIALOG'
    assert r.chat_name('-12345678901234')=='Тестовая группа'
    assert r.is_dm('42')


@pytest.mark.asyncio
async def test_create_control_does_not_enqueue(tmp_path):
    client,sender,store,db=fixture(tmp_path)
    await client._on_message_cb(MaxMessage(chat_id=42,message_id='new',is_self=True,attaches=[{'_type':'CONTROL','event':'new','title':'test'}]))
    assert not db.counts()
    sender._bot.send_message.assert_not_called()
    db.close()


def test_system_notice_rendered_without_placeholder():
    from app.max_listener import _message_body
    msg=MaxMessage(attaches=[{'_type':'CONTROL','event':'system','message':'Notice <example>'}])
    assert _message_body(msg)=='Notice &lt;example&gt;'
    msg.text='Notice <example>'
    assert _message_body(msg).count('Notice')==1
