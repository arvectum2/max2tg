import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from telegram.error import RetryAfter,TimedOut
from telegram.request._requestparameter import RequestParameter
from telegram.request._requestdata import RequestData
import pytest
from tests.test_tg_albums import G,A,part,age,client,drain
from tests.test_tg_outbox import env
from tests.test_media_v12 import message
from tests.test_outbox import fixture

PHOTO=(Path(__file__).parent/'fixtures/synthetic-max-album.webp').read_bytes()
VIDEO=(Path(__file__).parent/'fixtures/synthetic-mixed-album.mp4').read_bytes()

async def test_tg_mixed_native_one_rpc_and_caption_offsets(tmp_path):
    store,l=env(tmp_path);p=part(0,'🎞 abc');p['elements']=[{'type':'STRONG','from':2,'length':3}]
    q=part(1,'видео');q['media'].update(kind='video',filename='clip.mp4',mime='video/mp4');q['elements']=[{'type':'EMPHASIZED','from':0,'length':5}]
    for i,x in enumerate((p,q,part(2))):l.enqueue(G,100+i,42,x,album=A)
    assert l.album_wakeup.is_set() and l.has_pending_albums();age(l);c=client();c.upload_video=AsyncMock(return_value={'_type':'VIDEO','videoId':50,'token':'synthetic'})
    c.send_message.return_value={'message':{'id':'12345','attaches':[{'_type':k} for k in ('PHOTO','VIDEO','PHOTO')]}}
    assert await drain(l,c)=={'SENT':3};c.send_message.assert_awaited_once();c.upload_video.assert_awaited_once();c.upload_photo.assert_awaited()
    args=c.send_message.await_args;assert args.args==(42,'[1] 🎞 abc\n\n[2] видео');assert [a['_type'] for a in args.kwargs['attaches']]==['PHOTO','VIDEO','PHOTO']
    text=args.args[1];assert [text[e['from']:e['from']+e['length']] for e in args.kwargs['elements']]==['abc','видео'];assert not l.has_pending_albums();l.close()

async def test_tg_mixed_wrong_ack_types_quarantined(tmp_path):
    store,l=env(tmp_path);q=part(1);q['media']['kind']='video';l.enqueue(G,100,42,part(0),album=A);l.enqueue(G,101,42,q,album=A);age(l);c=client(2);c.upload_video=AsyncMock(return_value={'_type':'VIDEO','videoId':50,'token':'synthetic'})
    assert await drain(l,c)=={'UNKNOWN':2};assert await drain(l,c)=={};c.send_message.assert_awaited_once();l.close()

async def test_tg_mixed_video_failure_no_partial_send(tmp_path):
    store,l=env(tmp_path);q=part(1);q['media']['kind']='video';l.enqueue(G,100,42,part(0),album=A);l.enqueue(G,101,42,q,album=A);age(l);c=client(2);c.upload_video=AsyncMock(return_value=None)
    assert await drain(l,c)=={'FAILED':2};c.send_message.assert_not_awaited();l.close()


def max_msg(mid):
    return message(mid,[{'_type':'PHOTO','url':'https://i.oneme.ru/1'},{'_type':'VIDEO','mp4Url':'https://cdn.oneme.ru/clip.mp4'},{'_type':'PHOTO','url':'https://i.oneme.ru/2'}],'caption')

async def test_max_mixed_real_ptb_multipart_and_receipts(tmp_path):
    c,s,t,db=fixture(tmp_path);m=max_msg('mixed-wire');c.download_file=AsyncMock(side_effect=[PHOTO,VIDEO,PHOTO])
    async def send(**kw):
        rd=RequestData([RequestParameter.from_input('media',kw['media'])]);entries=json.loads(rd.json_parameters['media']);assert [e['type'] for e in entries]==['photo','video','photo'];names=[e['media'].removeprefix('attach://') for e in entries];assert len(set(names))==3 and set(names)==set(rd.multipart_data)
        assert rd.multipart_data[names[0]][1].startswith(b'\xff\xd8');assert rd.multipart_data[names[1]][1]==VIDEO;assert rd.multipart_data[names[1]][2]=='video/mp4';assert 'caption' in entries[0]['caption'];assert 'caption' not in entries[1]
        return [SimpleNamespace(message_id=i) for i in (101,102,103)]
    s._bot.send_media_group=AsyncMock(side_effect=send);await c._on_message_cb(m);assert db.job(db.job_key(42,'mixed-wire'))['status']=='SENT';assert json.loads(db._part(db.job_key(42,'mixed-wire'),0)['tg_ids'])==[101,102,103];await c._on_message_cb(m);s._bot.send_media_group.assert_awaited_once();db.close()

async def test_max_mixed_retry_reuses_bytes(tmp_path):
    c,s,t,db=fixture(tmp_path);m=max_msg('mixed-retry');c.download_file=AsyncMock(side_effect=[PHOTO,VIDEO,PHOTO]);s._bot.send_media_group=AsyncMock(side_effect=RetryAfter(1));await c._on_message_cb(m);k=db.job_key(42,'mixed-retry');assert db.job(k)['status']=='RETRY';db.db.execute('UPDATE jobs SET next_attempt=0 WHERE key=?',(k,));c.download_file.reset_mock();c.download_file.side_effect=RuntimeError('expired');s._bot.send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=i) for i in (11,12,13)]);await c._on_message_cb(m);c.download_file.assert_not_awaited();assert db.job(k)['status']=='SENT';db.close()

async def test_max_mixed_timeout_no_repeat(tmp_path):
    c,s,t,db=fixture(tmp_path);m=max_msg('mixed-timeout');c.download_file=AsyncMock(side_effect=[PHOTO,VIDEO,PHOTO]);s._bot.send_media_group=AsyncMock(side_effect=TimedOut());await c._on_message_cb(m);assert db.job(db.job_key(42,'mixed-timeout'))['status']=='UNKNOWN';await c._on_message_cb(m);s._bot.send_media_group.assert_awaited_once();db.close()

async def test_max_mixed_missing_video_never_sends_preview_or_partial(tmp_path):
    c,s,t,db=fixture(tmp_path);m=max_msg('mixed-missing');c.download_file=AsyncMock(side_effect=[PHOTO,b'preview']);s._bot.send_media_group=AsyncMock();s._bot.send_photo=AsyncMock();await c._on_message_cb(m);s._bot.send_media_group.assert_not_awaited();s._bot.send_photo.assert_not_awaited();assert db.job(db.job_key(42,'mixed-missing'))['status']!='SENT';db.close()
