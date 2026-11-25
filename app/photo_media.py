"""Normalize MAX WebP photos to real JPEGs before Telegram dispatch."""
import asyncio,json,tempfile
from pathlib import Path
from app.video_compress import FFMPEG,FFPROBE,_run

async def telegram_photo(data:bytes)->bytes|None:
    if not isinstance(data,bytes) or not data:return None
    if not (data[:4]==b'RIFF' and data[8:12]==b'WEBP'):return data
    if len(data)>25*1024*1024:return None
    try:
        with tempfile.TemporaryDirectory(prefix='max2tg-photo-') as d:
            src=Path(d)/'source.webp';dst=Path(d)/'photo.jpg';src.write_bytes(data);src.chmod(0o600)
            async def info(p):
                raw=await _run([FFPROBE,'-v','error','-protocol_whitelist','file,pipe','-select_streams','v:0','-show_entries','stream=codec_name,width,height','-of','json',str(p)],10)
                if not raw:return None
                streams=json.loads(raw).get('streams') or []
                return streams[0] if len(streams)==1 else None
            original=await info(src)
            if not original or original.get('codec_name')!='webp':return None
            w,h=original.get('width',0),original.get('height',0)
            if not 0<w<=10000 or not 0<h<=10000 or w*h>40_000_000:return None
            if await _run([FFMPEG,'-nostdin','-hide_banner','-loglevel','error','-y','-protocol_whitelist','file,pipe','-threads','1','-i',str(src),'-map','0:v:0','-map_metadata','-1','-frames:v','1','-c:v','mjpeg','-q:v','2','-threads','1',str(dst)],20) is None:return None
            if not dst.exists() or not 0<dst.stat().st_size<=10*1024*1024:return None
            result=await info(dst)
            if not result or result.get('codec_name')!='mjpeg' or (result.get('width'),result.get('height'))!=(w,h):return None
            content=dst.read_bytes()
            return content if content.startswith(b'\xff\xd8') and content.endswith(b'\xff\xd9') else None
    except (OSError,ValueError,TypeError,asyncio.TimeoutError):return None
