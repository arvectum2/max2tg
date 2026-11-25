from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock
import pytest
from app.max_client import MaxClient
from app import native_media

URL='https://maxvd1.okcdn.ru/upload?token=a%2Fb'
SLOT={'info':[{'url':URL,'videoId':123,'token':'fixture'}]}

@pytest.mark.asyncio
async def test_video_waits_for_matching_processing_ack(monkeypatch):
    client=MaxClient('synthetic','synthetic');client.cmd=AsyncMock(return_value=SLOT)
    monkeypatch.setattr(native_media,'validate_video',AsyncMock(return_value=True))
    async def post(*args):
        assert 123 in client._video_pending
        await client._handle({'opcode':136,'cmd':0,'seq':0,'payload':{'fileId':123}})
        assert not client._video_pending[123].done()
        await client._handle({'opcode':136,'cmd':0,'seq':0,'payload':{'videoId':123}})
        return True
    client._upload_native_bytes=AsyncMock(side_effect=post)
    assert await client.upload_video(b'fixture')=={'_type':'VIDEO','videoId':123,'token':'fixture'}
    assert not client._video_pending
    client.cmd.assert_awaited_once_with(82,{'count':1},none_on_timeout=True)

@pytest.mark.asyncio
@pytest.mark.parametrize('error',[True,False])
async def test_video_error_or_no_ack_not_sent(monkeypatch,error):
    c=MaxClient('synthetic','synthetic');c.cmd=AsyncMock(return_value=SLOT)
    monkeypatch.setattr(native_media,'validate_video',AsyncMock(return_value=True))
    async def post(*args):
        if error:await c._handle({'opcode':136,'cmd':0,'seq':0,'payload':{'videoId':123,'error':'processing failed'}})
        return True
    c._upload_native_bytes=AsyncMock(side_effect=post)
    assert await c.upload_video(b'fixture',timeout=.001) is None
    assert not c._video_pending

@pytest.mark.asyncio
async def test_voice_uses_confirmed_processed_route_only():
    c=MaxClient('synthetic','synthetic');c.upload_processed_audio=AsyncMock(return_value={'_type':'AUDIO','audioId':123,'duration':3000,'wave':[]});c.upload_file=AsyncMock()
    assert (await c.upload_audio(b'fixture'))['_type']=='AUDIO'
    c.upload_processed_audio.assert_awaited_once_with(b'fixture',timeout=60.,uploader_type=0)
    c.upload_file.assert_not_called()
    c.upload_processed_audio.return_value=None
    assert await c.upload_audio(b'fixture') is None
    c.upload_file.assert_not_called()

@pytest.mark.asyncio
@pytest.mark.parametrize('status,headers,body,expected',[(200,{},b'{}',True),(201,{'X-Last-Known-Byte':'2'},b'',True),(201,{},b'',False),(200,{'X-Last-Known-Byte':'1'},b'',False),(200,{},b'{"error_code":7}',False),(302,{},b'',False),(200,{'X-Last-Known-Byte':'2'},b'non-JSON receipt',True),(200,{},b'non-JSON receipt',False),(200,{'X-Last-Known-Byte':'1'},b'non-JSON receipt',False)])
async def test_raw_upload_receipt(status,headers,body,expected):
    c=MaxClient('synthetic','synthetic');c._session=MagicMock();c._session.closed=False
    async def chunks(*a):yield body
    resp=SimpleNamespace(status=status,headers=headers,content=SimpleNamespace(iter_chunked=chunks))
    c._session.post.return_value.__aenter__=AsyncMock(return_value=resp);c._session.post.return_value.__aexit__=AsyncMock(return_value=False)
    assert await c._upload_native_bytes(URL,b'123','test.mp4','video/mp4') is expected
    args=c._session.post.call_args
    assert str(args.args[0])==URL and args.kwargs['allow_redirects'] is False
    assert args.kwargs['headers']['Content-Range']=='bytes 0-2/3'

@pytest.mark.asyncio
async def test_native_slot_rejects_nontrusted_host(monkeypatch):
    c=MaxClient('synthetic','synthetic');c.cmd=AsyncMock(return_value={'info':[{'url':'https://evil.example/upload','videoId':123,'token':'fixture'}]})
    monkeypatch.setattr(native_media,'validate_video',AsyncMock(return_value=True))
    c._upload_native_bytes=AsyncMock()
    assert await c.upload_video(b'fixture') is None
    c._upload_native_bytes.assert_not_called()

@pytest.mark.asyncio
async def test_outbound_video_ledger_uses_native_video(tmp_path):
    from tests.test_tg_outbox import env,media_payload,mock_client,tg_bot
    store,ledger=env(tmp_path);ledger.enqueue(-10088,81,42,media_payload('video'));c=mock_client({'message':{'id':'video81'}})
    c.upload_video=AsyncMock(return_value={'_type':'VIDEO','videoId':123,'token':'fixture'});c.upload_file=AsyncMock()
    assert await ledger.deliver_one(c,-10088,81,tg_bot=tg_bot(b'video'))=='SENT'
    assert await ledger.deliver_one(c,-10088,81,tg_bot=tg_bot(b'video'))=='SENT'
    c.upload_video.assert_awaited_once();c.upload_file.assert_not_called();c.send_message.assert_awaited_once()
    ledger.close()


def test_audio_wave_survives_json_ledger_and_binary_wire():
    import json
    from app.max_wire import encode,decode
    a=json.loads(json.dumps({'_type':'AUDIO','audioId':123,'token':'fixture','duration':3000,'wave':[]}))
    p={'cmd':0,'seq':1,'opcode':64,'payload':{'message':{'attaches':[a]}}}
    assert decode(encode(p))['payload']['message']['attaches'][0]['wave']==b''
    a['wave']=[0,127,255]
    assert decode(encode(p))['payload']['message']['attaches'][0]['wave']==bytes([0,127,255])
    a['wave']=[256]
    with pytest.raises(ValueError):encode(p)


@pytest.mark.asyncio
@pytest.mark.parametrize('ack',[{'audioId':123},{'audioId':123,'error':'invalid'},{'videoId':123},None])
async def test_processed_voice_requires_own_audio_ack(monkeypatch,ack):
    c=MaxClient('synthetic','synthetic');c.cmd=AsyncMock(return_value={**SLOT,'uploaderType':0})
    monkeypatch.setattr(native_media,'prepare_processed_voice',AsyncMock(return_value=(b'm4a',3000)))
    async def post(*args):
        assert 123 in c._audio_pending
        if ack:await c._handle({'opcode':136,'cmd':0,'seq':0,'payload':ack})
        return True
    c._upload_native_bytes=AsyncMock(side_effect=post)
    result=await c.upload_processed_audio(b'fixture',timeout=.001)
    assert bool(result)==(ack=={'audioId':123})
    if result:assert result['_type']=='AUDIO' and result['audioId']==123
    assert not c._audio_pending
    c.cmd.assert_awaited_once_with(82,{'count':1,'type':2,'uploaderType':0},none_on_timeout=True)
    c._upload_native_bytes.assert_awaited_once_with(URL,b'm4a','voice.m4a','audio/mp4')


@pytest.mark.asyncio
async def test_native_voice_ledger_keeps_wave_and_suppresses_resend(tmp_path):
    from tests.test_tg_outbox import env,media_payload,mock_client,tg_bot
    store,ledger=env(tmp_path);ledger.enqueue(-10088,82,42,media_payload('voice'));c=mock_client({'message':{'id':'audio82'}})
    attach={'_type':'AUDIO','audioId':123,'duration':3000,'token':'fixture','wave':[]}
    c.upload_audio=AsyncMock(return_value=attach);c.upload_file=AsyncMock()
    assert await ledger.deliver_one(c,-10088,82,tg_bot=tg_bot(b'voice'))=='SENT'
    assert await ledger.deliver_one(c,-10088,82,tg_bot=tg_bot(b'voice'))=='SENT'
    c.upload_audio.assert_awaited_once();c.upload_file.assert_not_called();c.send_message.assert_awaited_once()
    assert c.send_message.await_args.kwargs['attaches']==[attach]
    ledger.close()
