from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.photo_history import import_photos, photo_count
from app.max_client import MaxMessage
from tests.test_outbox import fixture


def row(i, *types):
    return dict(id=str(i),time=1700000000000+i*1000,sender=200,text='',attaches=[dict(_type=t,baseUrl='https://i.oneme.ru/photo') for t in types])


@pytest.mark.asyncio
async def test_photo_backfill_skip_text_unknown_and_repeat(tmp_path):
    client,sender,store,db=fixture(tmp_path)
    unknown=MaxMessage(chat_id=42,message_id='2',timestamp=1700000002000,attaches=[dict(_type='PHOTO')])
    key=db.enqueue(unknown);db._set_job(key,'UNKNOWN')
    client.fetch_history=AsyncMock(return_value={'messages':[row(1,'CONTROL'),row(2,'PHOTO'),row(3,'PHOTO')]})
    client.download_file=AsyncMock(return_value=b'image')
    sender._bot.send_photo=AsyncMock(return_value=SimpleNamespace(message_id=789))
    s=await import_photos(client,sender,client.resolver,42,777)
    assert s==dict(scanned=3,candidates=2,delivered=1,existing=0,unresolved=1)
    assert db.job(key)['status']=='UNKNOWN'
    s=await import_photos(client,sender,client.resolver,42,777)
    assert s['existing']==1 and s['delivered']==0
    sender._bot.send_photo.assert_awaited_once()
    sender._bot.send_message.assert_not_called()
    db.close()


@pytest.mark.asyncio
async def test_photo_placeholder_not_counted(tmp_path):
    client,sender,store,db=fixture(tmp_path)
    client.fetch_history=AsyncMock(return_value={'messages':[row(1,'PHOTO')]})
    client.download_file=AsyncMock(return_value=None)
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=123))
    s=await import_photos(client,sender,client.resolver,42,777)
    assert s['delivered']==0 and s['unresolved']==1
    db.close()


def test_photo_type_filter():
    assert photo_count(row(1,'PHOTO','PHOTO'))==2
    assert photo_count(row(1,'PHOTO','VIDEO'))==0
    assert photo_count(row(1,'CONTROL'))==0

@pytest.mark.asyncio
async def test_album_ack_counts_every_photo(tmp_path):
    client,sender,store,db=fixture(tmp_path)
    client.fetch_history=AsyncMock(return_value={'messages':[row(1,'PHOTO','PHOTO')]})
    client.download_file=AsyncMock(return_value=b'image')
    sender._bot.send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=5),SimpleNamespace(message_id=6)])
    s=await import_photos(client,sender,client.resolver,42,777)
    assert s['delivered']==2
    sender._bot.send_media_group.assert_awaited_once()
    assert (await import_photos(client,sender,client.resolver,42,777))['existing']==2
    db.close()

@pytest.mark.asyncio
async def test_mismatched_topic_denied(tmp_path):
    client,sender,store,db=fixture(tmp_path)
    client.fetch_history=AsyncMock()
    with pytest.raises(RuntimeError,match='chat binding'):
        await import_photos(client,sender,client.resolver,42,999)
    client.fetch_history.assert_not_called()
    db.close()

@pytest.mark.asyncio
async def test_page_scan_reaches_older_photo(tmp_path):
    client,sender,store,db=fixture(tmp_path)
    client.fetch_history=AsyncMock(side_effect=[{'messages':[row(i,'CONTROL') for i in range(101,201)]},{'messages':[row(1,'PHOTO')]}])
    client.download_file=AsyncMock(return_value=b'image')
    sender._bot.send_photo=AsyncMock(return_value=SimpleNamespace(message_id=789))
    s=await import_photos(client,sender,client.resolver,42,777)
    assert s['scanned']==101 and s['delivered']==1
    assert client.fetch_history.await_args_list[1].kwargs['from_time'] is not None
    db.close()

@pytest.mark.asyncio
async def test_forwarded_photo_is_delivered_once_with_original_outer_id(tmp_path):
    client,sender,store,db=fixture(tmp_path)
    outer=row(20,'CONTROL'); outer['attaches']=[]
    outer['link']={'type':'FORWARD','message':row(7,'PHOTO')}
    outer['link']['message']['text']='Фото из канала'
    client.fetch_history=AsyncMock(return_value={'messages':[outer]})
    client.download_file=AsyncMock(return_value=b'image')
    sender._bot.send_photo=AsyncMock(return_value=SimpleNamespace(message_id=901))
    assert photo_count(outer)==1
    assert (await import_photos(client,sender,client.resolver,42,777))['delivered']==1
    assert db.job(db.job_key(42,'20'))['status']=='SENT'
    assert db.job(db.job_key(42,'7')) is None
    assert (await import_photos(client,sender,client.resolver,42,777))['existing']==1
    sender._bot.send_photo.assert_awaited_once()
    db.close()


def test_forward_photo_count_uses_nested_not_outer_metadata():
    outer=row(20,'PHOTO'); outer['link']={'type':'FORWARD','message':row(7,'PHOTO','PHOTO')}
    assert photo_count(outer)==2
    outer['link']['message']=row(7,'PHOTO','VIDEO')
    assert photo_count(outer)==0
    outer['link']={'type':'REPLY','message':row(7,'PHOTO')};outer['attaches']=[]
    assert photo_count(outer)==0
