"""Normalize playable voice data to Telegram OGG/Opus without truncation."""
import asyncio,json,logging,math,tempfile
from pathlib import Path
from app.video_compress import _run,FFMPEG,FFPROBE,UPLOAD_CAP
log=logging.getLogger(__name__)

async def _audio_info(path):
    raw=await _run([FFPROBE,'-v','error','-protocol_whitelist','file,pipe','-show_entries','format=duration:stream=codec_type,codec_name','-of','json',str(path)],15)
    if not raw:return None
    info=json.loads(raw);seconds=float(info.get('format',{}).get('duration') or 0)
    streams=info.get('streams') or []
    if not math.isfinite(seconds) or not 0<seconds<=1800 or not any(s.get('codec_type')=='audio' for s in streams):return None
    if any(s.get('codec_type')=='video' for s in streams):return None
    return seconds,streams

async def voice_ogg(data):
    if not isinstance(data,bytes) or not 16<=len(data)<=UPLOAD_CAP:return None
    if data.startswith(b'OggS') and b'OpusHead' in data[:256]:return data
    try:
        with tempfile.TemporaryDirectory(prefix='max2tg-voice-') as d:
            src=Path(d)/'input';dst=Path(d)/'voice.ogg';src.write_bytes(data);src.chmod(0o600)
            source=await _audio_info(src)
            if not source:return None
            result=await _run([FFMPEG,'-nostdin','-hide_banner','-loglevel','error','-y','-protocol_whitelist','file,pipe','-threads','2','-i',str(src),'-map','0:a:0','-map_metadata','-1','-c:a','libopus','-b:a','48k','-ac','1',str(dst)],90)
            if result is None or not dst.exists() or dst.stat().st_size>UPLOAD_CAP:return None
            converted=await _audio_info(dst)
            if not converted or abs(converted[0]-source[0])>max(0.2,min(1.0,source[0]*0.02)):return None
            if not any(s.get('codec_name')=='opus' for s in converted[1]):return None
            result=dst.read_bytes()
            return result if result.startswith(b'OggS') and b'OpusHead' in result[:256] else None
    except (OSError,ValueError,TypeError,asyncio.TimeoutError) as exc:
        log.warning('Voice conversion stopped: %s',type(exc).__name__)
        return None
