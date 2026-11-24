import json,time,sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.tg_outbox import TelegramOutbound
from app.tg_handler import _on_topic_media,_max_reply_link,TG_OUTBOUND_KEY,SUPERGROUP_KEY
from tests.test_tg_outbox import env,media_payload,mock_client,tg_bot
from tests.test_tg_handler import _make_update,_make_context

G=-10088
A=('album-1',100,10)

def part(i,caption=''):
    p=media_payload();p['text']=caption;p['media']['file_id']=f'photo-{i}';return p

def setup_group(ledger,n=3):
    for i in range(n):assert ledger.enqueue(G,100+i,42,part(i,'caption' if i==0 else ''),album=A)=='RECEIVED'

def age(ledger,seconds=4):
    ledger.db.execute('UPDATE outbound SET created=? WHERE album_pending=1',(time.time()-seconds,))

def client(n=3):
    c=mock_client({'message':{'id':'12345','attaches':[{'_type':'PHOTO','photoId':i} for i in range(n)]}})
    c.upload_photo=AsyncMock(side_effect=[{'_type':'PHOTO','photoToken':str(i)} for i in range(20)]);return c

async def drain(ledger,c,**kw):
    return await ledger.drain(c,is_allowed=lambda _:True,tg_bot=tg_bot(),**kw)

async def test_album_waits_then_one_rpc_three_receipts_and_reply(tmp_path):
    store,l=env(tmp_path);setup_group(l);c=client()
    assert l.counts()=={'RECEIVED':3};assert l.pending()==[]
    assert await drain(l,c)=={};c.send_message.assert_not_awaited()
    age(l);assert await drain(l,c)=={'SENT':3};c.send_message.assert_awaited_once()
    assert c.send_message.await_args.args==(42,'caption')
    assert len(c.send_message.await_args.kwargs['attaches'])==3
    assert [l.row(G,i)['max_id'] for i in range(100,103)]==['12345']*3
    assert store.tg_for_max_message(42,'12345')==100
    reply=SimpleNamespace(chat_id=G,reply_to_message=SimpleNamespace(message_id=102))
    assert _max_reply_link(reply,store,42,l)=={'type':'REPLY','messageId':'12345'}
    assert await drain(l,c)=={};assert l.enqueue(G,101,42,part(1),album=A)=='SENT';c.send_message.assert_awaited_once();l.close()

async def test_pending_group_survives_restart_and_legacy_worker_never_sees_ready(tmp_path):
    store,l=env(tmp_path);setup_group(l);age(l);l.close()
    l=TelegramOutbound(tmp_path/'tg-outbound.sqlite3',store);assert l.db.execute("SELECT count(*) FROM outbound WHERE status='RECEIVED'").fetchone()[0]==0
    c=client();assert await drain(l,c)=={'SENT':3};c.send_message.assert_awaited_once();l.close()

async def test_group_unknown_never_replays_and_late_part_blocks(tmp_path):
    store,l=env(tmp_path);setup_group(l);age(l);c=client();c.send_message.side_effect=TimeoutError()
    with pytest.raises(TimeoutError):await drain(l,c)
    assert l.counts()=={'UNKNOWN':3};l.close();l=TelegramOutbound(tmp_path/'tg-outbound.sqlite3',store)
    assert l.enqueue(G,103,42,part(3),album=A)=='BLOCKED'
    assert await drain(l,c)=={};c.send_message.assert_awaited_once();assert l.counts()=={'UNKNOWN':3,'BLOCKED':1};l.close()

@pytest.mark.parametrize('response',[{'message':{'id':'12345'}},{'message':{'id':'12345','attaches':[{'_type':'PHOTO'}]}},{'_max_error':{'error':'rejected'}}])
async def test_incomplete_ack_not_sent(tmp_path,response):
    store,l=env(tmp_path);setup_group(l);age(l);c=client();c.send_message.return_value=response
    state='FAILED' if '_max_error' in response else 'UNKNOWN';assert await drain(l,c)=={state:3};assert l.counts()=={state:3};assert await drain(l,c)=={};l.close()

async def test_album_waits_for_staged_intake_and_no_cross_topic(tmp_path):
    store,l=env(tmp_path);setup_group(l);age(l);c=client()
    l.db.execute('CREATE TABLE inbound_updates(telegram_chat TEXT,payload TEXT)');l.db.execute('INSERT INTO inbound_updates VALUES(?,?)',(str(G),json.dumps({'message':{'media_group_id':A[0]}})))
    assert await drain(l,c)=={};c.upload_photo.assert_not_awaited();l.db.execute('DELETE FROM inbound_updates')
    assert l.enqueue(G,103,43,part(3),album=(A[0],100,11))=='BLOCKED';assert await drain(l,c)=={};c.send_message.assert_not_awaited();assert l.counts()=={'BLOCKED':4};l.close()

async def test_album_paused_and_late_part_after_success(tmp_path):
    store,l=env(tmp_path);setup_group(l);age(l);c=client()
    assert await drain(l,c,is_media_allowed=lambda _:False)=={};c.upload_photo.assert_not_awaited()
    assert await drain(l,c)=={'SENT':3};assert l.enqueue(G,103,42,part(3),album=A)=='BLOCKED'
    assert await drain(l,c)=={};c.send_message.assert_awaited_once();l.close()

async def test_group_new_part_during_preparation_prevents_partial_send(tmp_path):
    store,l=env(tmp_path);setup_group(l,2);age(l);c=client()
    async def upload(*args,**kw):
        l.enqueue(G,102,42,part(2),album=A);return {'_type':'PHOTO','photoToken':str(c.upload_photo.await_count)}
    c.upload_photo=AsyncMock(side_effect=upload);assert await drain(l,c)=={};c.send_message.assert_not_awaited();assert l.counts()=={'RECEIVED':3};l.close()

async def test_handler_stages_album_without_immediate_send(tmp_path):
    store,l=env(tmp_path);c=client();ctx=_make_context(max_client=c,topic_store=store);ctx.bot_data[SUPERGROUP_KEY]=G;ctx.bot_data[TG_OUTBOUND_KEY]=l;ctx.bot=tg_bot()
    for i in range(3):
        u=_make_update(text=None,thread_id=10);m=u.message;m.message_id=100+i;m.media_group_id=A[0];m.caption='caption' if i==0 else '';m.caption_entities=[];m.photo=[SimpleNamespace(file_id=f'photo-{i}',file_size=5)];m.voice=m.audio=m.document=m.video=None
        await _on_topic_media(u,ctx)
    assert l.counts()=={'RECEIVED':3};c.send_message.assert_not_awaited();age(l);assert await drain(l,c)=={'SENT':3};l.close()

async def test_multiple_captions_preserved_with_numbered_positions(tmp_path):
    store,l=env(tmp_path);l.enqueue(G,100,42,part(0,'one'),album=A);l.enqueue(G,101,42,part(1,'two'),album=A);age(l);c=client();c.send_message.return_value={'message':{'id':'12345','attaches':[{'_type':'PHOTO'},{'_type':'PHOTO'}]}};assert await drain(l,c)=={'SENT':2};assert c.send_message.await_args.args==(42,'[1] one\n\n[2] two');l.close()
