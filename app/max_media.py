"""Extract media links from MAX *user WebSocket* attachments.

Do not call the bot REST API with a user-session token: these are distinct
authentication modes. Download URLs are validated by MaxClient.download_file.
"""
from __future__ import annotations

from urllib.parse import urlsplit


def _candidate(value):
    if isinstance(value, dict):
        return _candidate(value.get("url") or value.get("fileUrl")
                          or value.get("baseUrl"))
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return value
    return None


def first_media_url(attach: dict, *fields: str) -> str | None:
    if not isinstance(attach, dict):
        return None
    for field in fields:
        found = _candidate(attach.get(field))
        if found:
            return found
    return None


def photo_url(attach: dict) -> str | None:
    return first_media_url(attach, "baseUrl", "url", "fileUrl",
                           "baseRawUrl", "previewUrl")


def file_url(attach: dict) -> str | None:
    return first_media_url(attach,"url","fileUrl","downloadUrl")


def video_url(attach: dict) -> str | None:
    # Only direct files. Never download HLS playlists or external web players
    # and pass them to Telegram as binary videos.
    url=first_media_url(attach, "mp4Url", "videoUrl", "fileUrl",
                        "url", "downloadUrl", "mp4_720",
                        "mp4_480", "mp4_360")
    if url and (urlsplit(url).path.lower().endswith((".m3u8", ".mpd"))
                or "m.ok.ru/video/" in url):
        return None
    return url


def video_preview_url(attach: dict) -> str | None:
    return first_media_url(attach, "thumbnail","previewUrl",
                           "preview", "poster")


def media_filename(attach: dict, default: str="file") -> str:
    raw=(attach.get("name") or attach.get("fileName")
         or attach.get("filename") or default)
    if not isinstance(raw,str) or not raw.strip():
        return default
    name=raw.replace("\\","/").split("/")[-1].strip()
    # Telegram's displayed name must not contain controls.
    name="".join(ch for ch in name if ord(ch)>=32 and ord(ch)!=127)
    return name[:180] or default


def media_size(attach: dict) -> int:
    raw=attach.get("size",attach.get("fileSize",0))
    try:
        return max(0,int(raw))
    except (ValueError,TypeError,OverflowError):
        return 0


def duration_seconds(attach: dict) -> int:
    try:
        return max(0,int(attach.get("duration") or 0)//1000)
    except (TypeError,ValueError,OverflowError):
        return 0
