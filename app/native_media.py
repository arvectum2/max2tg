"""Validate bounded media before reserving native MAX upload slots."""
import asyncio,tempfile
from pathlib import Path
from app.voice_media import voice_ogg,_audio_info
from app.video_compress import _info
CAP=25*1024*1024

async def prepare_voice(data):
    if not isinstance(data,bytes) or not 16<=len(data)<=CAP:return None
    try:
        result=await voice_ogg(data)
        if not result or len(result)>CAP:return None
        with tempfile.TemporaryDirectory(prefix='max2tg-native-voice-') as d:
            p=Path(d)/'voice.ogg';p.write_bytes(result);info=await _audio_info(p)
            if not info or not any(s.get('codec_name')=='opus' for s in info[1]):return None
            return result,round(info[0]*1000)
    except (OSError,ValueError,TypeError,asyncio.TimeoutError):return None

async def validate_video(data):
    if not isinstance(data,bytes) or not 16<=len(data)<=CAP or data[4:8]!=b'ftyp':return False
    try:
        with tempfile.TemporaryDirectory(prefix='max2tg-native-video-') as d:
            p=Path(d)/'video.mp4';p.write_bytes(data);return bool(await _info(p))
    except (OSError,ValueError,TypeError,asyncio.TimeoutError):return False


async def prepare_processed_voice(data):
    """Use AAC/M4A for the server-processing audio path."""
    from app.video_compress import FFMPEG,_run
    if not isinstance(data,bytes) or not 16<=len(data)<=CAP:return None
    try:
        with tempfile.TemporaryDirectory(prefix='max2tg-processed-voice-') as d:
            src=Path(d)/'input';dst=Path(d)/'voice.m4a';src.write_bytes(data);src.chmod(0o600)
            source=await _audio_info(src)
            if not source:return None
            result=await _run([FFMPEG,'-nostdin','-hide_banner','-loglevel','error','-y','-protocol_whitelist','file,pipe','-threads','2','-i',str(src),'-map','0:a:0','-map_metadata','-1','-c:a','aac','-b:a','64k','-ac','1','-movflags','+faststart',str(dst)],90)
            if result is None or not dst.exists() or not 0<dst.stat().st_size<=CAP:return None
            converted=await _audio_info(dst)
            if not converted or abs(converted[0]-source[0])>max(.2,min(1.,source[0]*.02)):return None
            if not any(x.get('codec_name')=='aac' for x in converted[1]):return None
            return dst.read_bytes(),round(converted[0]*1000)
    except (OSError,ValueError,TypeError,asyncio.TimeoutError):return None
