from unittest.mock import AsyncMock
from types import SimpleNamespace
import pytest
from app.video_resolver import candidate_urls,download_resolved_video,is_mp4

HI='https://maxvd1.okcdn.ru/video?id=1'
LO='https://maxvd2.okcdn.ru/video?id=2'
MP4=b'\x00\x00\x00\x10ftypisom\x00\x00\x00\x00'

@pytest.mark.parametrize('payload', [{'MP4_720':HI,'MP4_240':LO},{'urls':{'mp4_720':HI,'mp4_240':LO}}])
def test_order(payload):
    assert candidate_urls(payload)==[HI,LO]

@pytest.mark.parametrize('url',['http://okcdn.ru/video','https://127.0.0.1/file','https://okcdn.ru.evil.example/file','https://okcdn.ru/file.m3u8','https://u:p@okcdn.ru/file','https://okcdn.ru:8080/file'])
def test_block_unsafe_or_manifest(url):
    assert candidate_urls({'MP4_720':url})==[]

@pytest.mark.parametrize('data',[b'<html>oops</html>',b'\xff\xd8\xff'+b'x'*30,b'#EXTM3U',None,b'\xff\xff\xff\xffftypisom'+b'x'*10])
def test_reject_non_mp4(data):
    assert not is_mp4(data)

@pytest.mark.asyncio
async def test_fallback_resolution_not_preview():
    client=SimpleNamespace(download_file=AsyncMock(side_effect=[b'<html>error</html>',MP4]))
    assert await download_resolved_video(client,{'MP4_720':HI,'MP4_240':LO})==MP4
    assert client.download_file.await_count==2

@pytest.mark.asyncio
async def test_no_download_on_unsafe_response():
    client=SimpleNamespace(download_file=AsyncMock())
    assert await download_resolved_video(client,{'MP4_720':'https://evil.example/file'}) is None
    client.download_file.assert_not_called()

@pytest.mark.asyncio
async def test_verified_video_rpc_shape():
    from app.video_resolver import resolve_video
    client=SimpleNamespace(cmd=AsyncMock(return_value={'MP4_720':HI}),download_file=AsyncMock(return_value=MP4))
    assert await resolve_video(client,{'videoId':'123','token':'test-token'},'-42','789')==MP4
    client.cmd.assert_awaited_once_with(83,{'videoId':123,'chatId':-42,'messageId':789,'token':'test-token'},none_on_timeout=True)

@pytest.mark.asyncio
async def test_invalid_identifier_never_calls_rpc():
    from app.video_resolver import resolve_video
    client=SimpleNamespace(cmd=AsyncMock())
    assert await resolve_video(client,{'videoId':'invalid'},42,'789') is None
    client.cmd.assert_not_called()

@pytest.mark.asyncio
async def test_live_video_resolved_and_acknowledged(tmp_path):
    from tests.test_outbox import fixture,event
    client,sender,store,db=fixture(tmp_path)
    m=event('123','video');m.attaches=[{'_type':'VIDEO','videoId':456,'token':'test-token','thumbnail':'https://i.oneme.ru/preview'}]
    client.cmd=AsyncMock(return_value={'MP4_720':HI});client.download_file=AsyncMock(return_value=MP4)
    sender._bot.send_video=AsyncMock(return_value=SimpleNamespace(message_id=801))
    await client._on_message_cb(m)
    assert db.job(db.job_key(42,'123'))['status']=='SENT'
    assert store.tg_for_max_message(42,'123')==801
    sender._bot.send_video.assert_awaited_once();sender._bot.send_photo.assert_not_called()
    await client._on_message_cb(m);sender._bot.send_video.assert_awaited_once()
    db.close()

@pytest.mark.asyncio
async def test_unresolved_video_no_false_receipt(tmp_path):
    from tests.test_outbox import fixture,event
    client,sender,store,db=fixture(tmp_path)
    m=event('124','');m.attaches=[{'_type':'VIDEO','videoId':456,'thumbnail':'https://i.oneme.ru/preview'}]
    client.cmd=AsyncMock(return_value={'HLS':'https://okcdn.ru/a.m3u8'});client.download_file=AsyncMock()
    await client._on_message_cb(m)
    key=db.job_key(42,'124');assert db.job(key)['status']=='RETRY'
    assert store.tg_for_max_message(42,'124') is None
    sender._bot.send_photo.assert_not_called();sender._bot.send_video.assert_not_called();sender._bot.send_message.assert_not_called()
    db.db.execute('update jobs set attempts=2,next_attempt=0 where key=?',(key,))
    await client._on_message_cb(m)
    assert db.job(key)['status']=='FAILED'
    db.close()

@pytest.mark.asyncio
async def test_resolver_requests_exact_signed_url():
    signed='https://maxvd1.okcdn.ru/video?sig=a%2Bb%2Fc%3D&path=%2Fone%2Ftwo'
    client=SimpleNamespace(download_file=AsyncMock(return_value=MP4))
    assert await download_resolved_video(client,{'MP4_720':signed})==MP4
    client.download_file.assert_awaited_once_with(signed,preserve_url=True)

@pytest.mark.asyncio
async def test_downloader_preserves_encoded_url_and_rejects_redirect():
    from app.max_client import MaxClient
    from unittest.mock import MagicMock
    from yarl import URL
    signed='https://maxvd1.okcdn.ru/video?sig=a%2Bb%2Fc%3D&path=%2Fone%2Ftwo'
    client=MaxClient('synthetic','synthetic')
    client._session=MagicMock();client._session.closed=False
    response=SimpleNamespace(status=302,headers={'Location':'https://evil.example/video'})
    client._session.get.return_value.__aenter__=AsyncMock(return_value=response)
    client._session.get.return_value.__aexit__=AsyncMock(return_value=False)
    assert str(URL(signed))!=signed
    assert await client.download_file(signed,preserve_url=True) is None
    client._session.get.assert_called_once()
    assert str(client._session.get.call_args.args[0])==signed

@pytest.mark.asyncio
async def test_http_400_logs_no_signed_query(caplog):
    from app.max_client import MaxClient
    from unittest.mock import MagicMock
    signed='https://maxvd1.okcdn.ru/video?secret=TOP_SECRET%2Fabc'
    client=MaxClient('synthetic','synthetic');client._session=MagicMock();client._session.closed=False
    client._session.get.return_value.__aenter__=AsyncMock(return_value=SimpleNamespace(status=400,headers={}))
    client._session.get.return_value.__aexit__=AsyncMock(return_value=False)
    assert await client.download_file(signed,preserve_url=True) is None
    assert 'HTTP 400' in caplog.text and 'TOP_SECRET' not in caplog.text

@pytest.mark.asyncio
async def test_large_video_fallback_after_smaller_variant(monkeypatch):
    from app import video_resolver as v
    client=SimpleNamespace(download_file=AsyncMock(side_effect=[None,None,MP4]))
    monkeypatch.setattr(v,'UPLOAD_CAP',15)
    compress=AsyncMock(return_value=b'compressed');monkeypatch.setattr(v,'compress_mp4',compress)
    assert await v.download_resolved_video(client,{'MP4_720':HI,'MP4_240':LO})==b'compressed'
    client.download_file.assert_awaited_with(LO,preserve_url=True,max_bytes=v.SOURCE_CAP)
    compress.assert_awaited_once_with(MP4)
