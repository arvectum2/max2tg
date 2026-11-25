import asyncio,struct,subprocess
from pathlib import Path
import pytest
from app.video_compress import compress_mp4,_info,FFMPEG,FFPROBE,UPLOAD_CAP

@pytest.mark.asyncio
async def test_invalid_mp4_rejected():
    assert await compress_mp4(b'not a video') is None
    assert await compress_mp4(b'\x00\x00\x00\x10ftypisom\x00\x00\x00\x00') is None

@pytest.mark.asyncio
@pytest.mark.skipif(not Path(FFMPEG).is_file() or not Path(FFPROBE).is_file(),reason='FFmpeg missing')
async def test_real_oversize_container_compresses_without_truncation(tmp_path):
    src=tmp_path/'source.mp4'
    subprocess.run([FFMPEG,'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc2=s=320x240:r=15','-f','lavfi','-i','sine=frequency=440','-t','6','-c:v','libx264','-c:a','aac','-movflags','+faststart',str(src)],check=True,timeout=20)
    # Valid ISO-BMFF free atom exercises >50 MiB input without encoding
    # minutes of video in the regression test.
    pad=51*1024*1024
    with src.open('ab') as f:f.write(struct.pack('>I4s',pad,b'free'));f.write(b'\0'*(pad-8))
    assert src.stat().st_size>UPLOAD_CAP
    before=await _info(src)
    data=await compress_mp4(src.read_bytes(),target=450*1024)
    assert data and len(data)<=450*1024
    dst=tmp_path/'output.mp4';dst.write_bytes(data);after=await _info(dst)
    assert abs(before[0]-after[0])<0.3 and before[2] and after[2]
    assert after[1]['codec_name']=='h264'
    print('source bytes',src.stat().st_size,'output bytes',len(data),'duration',after[0])
