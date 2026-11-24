from pathlib import Path
import subprocess
from unittest.mock import AsyncMock
import pytest
from app.voice_media import voice_ogg,FFMPEG,FFPROBE,_audio_info

@pytest.mark.asyncio
async def test_invalid_voice_rejected():
    assert await voice_ogg(b'not audio') is None
    assert await voice_ogg(b'<html>'+b'x'*30) is None

@pytest.mark.asyncio
@pytest.mark.skipif(not Path(FFMPEG).is_file() or not Path(FFPROBE).is_file(),reason='FFmpeg missing')
async def test_real_m4a_to_voice(tmp_path):
    src=tmp_path/'source.m4a'
    subprocess.run([FFMPEG,'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','sine=frequency=440','-t','3','-c:a','aac',str(src)],check=True,timeout=10)
    data=await voice_ogg(src.read_bytes());assert data and data.startswith(b'OggS') and b'OpusHead' in data[:256]
    p=tmp_path/'voice.ogg';p.write_bytes(data);seconds,streams=await _audio_info(p)
    assert abs(seconds-3)<0.2 and streams[0]['codec_name']=='opus'
    assert await voice_ogg(data)==data

@pytest.mark.asyncio
async def test_unresolved_audio_does_not_create_text_receipt(tmp_path):
    from tests.test_outbox import fixture,event
    c,s,store,db=fixture(tmp_path);m=event('123','');m.attaches=[{'_type':'AUDIO','audioId':123,'token':'fixture'}]
    c.download_audio_url=AsyncMock(return_value=None)
    await c._on_message_cb(m)
    assert db.job(db.job_key(42,'123'))['status']=='RETRY'
    s._bot.send_voice.assert_not_called();s._bot.send_message.assert_not_called()
    db.close()
