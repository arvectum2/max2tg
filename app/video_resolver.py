"""Resolve original video using the current MAX web-client VIDEO_PLAY RPC.
Source verified 2026-11-24: web.max.ru immutable chunk D0OzoqHp.js.
"""
import asyncio
import logging
from urllib.parse import urlsplit
from app.max_client import MaxClient, OpCode
from app.max_wire import normalize_ids
from app.video_compress import compress_mp4, SOURCE_CAP, UPLOAD_CAP

log = logging.getLogger(__name__)

_large_video_lock = asyncio.Lock()

RESOLUTIONS = (1080,720,480,360,240,144)


def candidate_urls(payload):
    if not isinstance(payload,dict) or payload.get('_max_error'):
        return []
    found=[]
    for block in (payload,payload.get('urls'),payload.get('videoUrls')):
        if not isinstance(block,dict):
            continue
        for height in RESOLUTIONS:
            for key in (f'MP4_{height}',f'mp4_{height}'):
                value=block.get(key)
                if isinstance(value,dict): value=value.get('url')
                if not isinstance(value,str) or not MaxClient._safe_media_url(value): continue
                if urlsplit(value).path.lower().endswith(('.m3u8','.mpd')): continue
                if value not in found: found.append(value)
    return found


def is_mp4(data):
    # ISO BMFF ftyp header, not an HTML error, JPEG preview or manifest.
    return isinstance(data,bytes) and len(data)>=16 and data[4:8]==b'ftyp' and 8<=int.from_bytes(data[:4],'big')<=len(data)


async def download_resolved_video(client,payload):
    urls = candidate_urls(payload)
    log.info("MAX video source lookup: response=%s mp4_candidates=%d",
             "ok" if isinstance(payload,dict) and not payload.get('_max_error') else "unavailable",len(urls))
    for url in urls:
        data=await client.download_file(url,preserve_url=True)
        if is_mp4(data) and len(data)<=UPLOAD_CAP: return data
    # Lowest available quality first for the bounded large-file path.
    # At most one large download and transcode at a time across chats.
    if urls:
        async with _large_video_lock:
            data=await client.download_file(urls[-1],preserve_url=True,max_bytes=SOURCE_CAP)
            if is_mp4(data):
                if len(data)<=UPLOAD_CAP:return data
                return await compress_mp4(data)
    return None


async def resolve_video(client, attach, chat_id, message_id):
    video_id = attach.get('videoId')
    if video_id is None or chat_id is None or not message_id:
        return None
    token = attach.get('token') or ''
    if not isinstance(token,str):
        return None
    try:
        payload = normalize_ids(dict(videoId=video_id,chatId=chat_id,messageId=message_id,token=token))
    except (ValueError,TypeError):
        return None
    response = await client.cmd(OpCode.VIDEO_PLAY,payload,none_on_timeout=True)
    return await download_resolved_video(client,response)
