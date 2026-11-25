"""Bounded local transcoding of MAX MP4 files; never upload partial output."""
import asyncio,json,logging,math,tempfile,shutil
from pathlib import Path

log=logging.getLogger(__name__)
UPLOAD_CAP=50*1024*1024
SOURCE_CAP=150*1024*1024
TARGET=45*1024*1024
FFMPEG=shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
FFPROBE=shutil.which('ffprobe') or '/opt/homebrew/bin/ffprobe'

async def _run(args,timeout):
    proc=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        out,_=await asyncio.wait_for(proc.communicate(),timeout)
        return out if proc.returncode==0 else None
    finally:
        if proc.returncode is None:
            proc.kill();await proc.wait()

async def _info(path):
    raw=await _run([FFPROBE,'-v','error','-protocol_whitelist','file,pipe','-show_entries','format=duration:stream=codec_type,codec_name,width,height','-of','json',str(path)],15)
    if not raw:return None
    j=json.loads(raw);duration=float(j.get('format',{}).get('duration') or 0)
    streams=j.get('streams',[]);video=next((s for s in streams if s.get('codec_type')=='video'),None)
    if not video or not math.isfinite(duration) or not 0<duration<=1800:return None
    return duration,video,any(s.get('codec_type')=='audio' for s in streams)

async def compress_mp4(data,*,target=TARGET):
    if not isinstance(data,bytes) or not 16<=len(data)<=SOURCE_CAP or data[4:8]!=b'ftyp' or not 65536<=target<=TARGET:return None
    try:
        with tempfile.TemporaryDirectory(prefix='max2tg-transcode-') as d:
            src=Path(d)/'source.mp4';dst=Path(d)/'output.mp4';src.write_bytes(data);src.chmod(0o600)
            info=await _info(src)
            if not info:return None
            duration,video,audio=info
            # Budget for mux overhead and audio; preserve full duration, never -fs.
            audio_rate=64000 if audio else 0
            rate=min(4000000,int(target*8*0.90/duration)-audio_rate)
            if rate<80000:return None
            for factor in (1.0,0.65):
                bitrate=int(rate*factor)
                if bitrate<60000:return None
                args=[FFMPEG,'-nostdin','-hide_banner','-loglevel','error','-y','-protocol_whitelist','file,pipe','-threads','2','-i',str(src),'-map','0:v:0','-map','0:a:0?','-map_metadata','-1','-map_chapters','-1','-vf',"scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",'-filter_threads','1','-c:v','libx264','-threads','2','-preset','fast','-pix_fmt','yuv420p','-b:v',str(bitrate),'-maxrate',str(bitrate),'-bufsize',str(bitrate*2),'-c:a','aac','-b:a','64k','-movflags','+faststart',str(dst)]
                if await _run(args,120) is None or not dst.exists():continue
                if dst.stat().st_size>target:continue
                result=await _info(dst)
                if not result:continue
                secs,v,has_audio=result
                if abs(secs-duration)>max(0.3,min(1.0,duration*0.02)) or v.get('codec_name')!='h264' or audio!=has_audio:continue
                output=dst.read_bytes()
                if output[4:8]==b'ftyp':
                    log.info('MAX video compressed: %d -> %d bytes',len(data),len(output));return output
    except (OSError,ValueError,TypeError,asyncio.TimeoutError) as exc:
        log.warning('MAX video compression stopped: %s',type(exc).__name__)
    return None
