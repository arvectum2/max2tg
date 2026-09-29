"""Tests for app/max_listener.py — pure helper functions."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.max_listener import (
    _guess_media_kind,
    _human_size,
    _is_max_service_dialog,
    _reconcile_snapshot_chats,
    _refresh_chat_metadata,
)
from app.resolver import ContactResolver


# ---------------------------------------------------------------------------
# _human_size
# ---------------------------------------------------------------------------

class TestHumanSize:
    """Tests for the _human_size byte-formatter."""

    # Byte range (< 1024)
    def test_zero_bytes(self):
        assert _human_size(0) == "0 Б"

    def test_single_byte(self):
        assert _human_size(1) == "1 Б"

    def test_max_bytes(self):
        assert _human_size(1023) == "1023 Б"

    # Kilobyte range (1024 – 1024²-1)
    def test_exact_one_kb(self):
        assert _human_size(1024) == "1.0 КБ"

    def test_fractional_kb(self):
        assert _human_size(1536) == "1.5 КБ"

    def test_large_kb(self):
        assert _human_size(1023 * 1024) == "1023.0 КБ"

    # Megabyte range
    def test_exact_one_mb(self):
        assert _human_size(1024 ** 2) == "1.0 МБ"

    def test_fractional_mb(self):
        assert _human_size(int(2.5 * 1024 ** 2)) == "2.5 МБ"

    def test_large_mb(self):
        assert _human_size(500 * 1024 ** 2) == "500.0 МБ"

    # Gigabyte range
    def test_exact_one_gb(self):
        assert _human_size(1024 ** 3) == "1.0 ГБ"

    def test_fractional_gb(self):
        assert _human_size(int(1.5 * 1024 ** 3)) == "1.5 ГБ"

    # Terabyte range (overflow past ГБ loop)
    def test_terabyte(self):
        result = _human_size(1024 ** 4)
        assert "ТБ" in result

    def test_large_terabyte(self):
        result = _human_size(5 * 1024 ** 4)
        assert result.startswith("5")
        assert "ТБ" in result

    # Return type
    def test_returns_string(self):
        assert isinstance(_human_size(42), str)


# ---------------------------------------------------------------------------
# _guess_media_kind
# ---------------------------------------------------------------------------

class TestGuessMediaKind:
    """Tests for the filename-to-media-kind classifier."""

    # Photo extensions
    def test_jpg_is_photo(self):
        assert _guess_media_kind("image.jpg") == "photo"

    def test_jpeg_is_photo(self):
        assert _guess_media_kind("photo.jpeg") == "photo"

    def test_png_is_photo(self):
        assert _guess_media_kind("screenshot.png") == "photo"

    def test_gif_is_photo(self):
        assert _guess_media_kind("anim.gif") == "photo"

    def test_webp_is_photo(self):
        assert _guess_media_kind("sticker.webp") == "photo"

    def test_bmp_is_photo(self):
        assert _guess_media_kind("old.bmp") == "photo"

    # Video extensions
    def test_mp4_is_video(self):
        assert _guess_media_kind("clip.mp4") == "video"

    def test_mov_is_video(self):
        assert _guess_media_kind("recording.mov") == "video"

    def test_avi_is_video(self):
        assert _guess_media_kind("video.avi") == "video"

    def test_mkv_is_video(self):
        assert _guess_media_kind("movie.mkv") == "video"

    def test_webm_is_video(self):
        assert _guess_media_kind("stream.webm") == "video"

    # Document / unknown extensions
    def test_pdf_is_document(self):
        assert _guess_media_kind("report.pdf") == "document"

    def test_zip_is_document(self):
        assert _guess_media_kind("archive.zip") == "document"

    def test_docx_is_document(self):
        assert _guess_media_kind("contract.docx") == "document"

    def test_txt_is_document(self):
        assert _guess_media_kind("notes.txt") == "document"

    def test_no_extension_is_document(self):
        assert _guess_media_kind("README") == "document"

    def test_empty_string_is_document(self):
        assert _guess_media_kind("") == "document"

    # Case-insensitivity
    def test_uppercase_jpg_is_photo(self):
        assert _guess_media_kind("PHOTO.JPG") == "photo"

    def test_mixed_case_mp4_is_video(self):
        assert _guess_media_kind("Video.MP4") == "video"

    def test_mixed_case_png_is_photo(self):
        assert _guess_media_kind("Image.PNG") == "photo"

    # Paths with directories
    def test_full_path_jpg(self):
        assert _guess_media_kind("/tmp/uploads/img.jpg") == "photo"

    def test_full_path_mp4(self):
        assert _guess_media_kind("/home/user/videos/clip.mp4") == "video"

    # Extension appearing in the middle of filename should not trigger false match
    def test_mp4_in_name_not_extension_is_document(self):
        assert _guess_media_kind("mp4_notes.txt") == "document"


class TestChatReconciliation:
    def _resolver(self):
        resolver = ContactResolver()
        resolver._my_id = 100
        resolver.chats_raw = {
            1: {"id": 1, "type": "CHAT", "status": "ACTIVE", "title": "Old"},
            2: {"id": 2, "type": "DIALOG", "status": "ACTIVE",
                "participants": {"100": 1, "55": 2}},
        }
        resolver.chat_types = {1: "CHAT", 2: "DIALOG"}
        resolver.chats = {1: "Old", 2: "DM:55"}
        resolver.users = {55: "Alice"}
        return resolver

    async def test_reconcile_creates_only_unseen_chat(self):
        resolver = self._resolver()
        sender = MagicMock()
        sender.topic_store.get_ui.return_value = ["1"]
        sender.topic_store.set_ui = MagicMock()
        client = MagicMock()

        with patch("app.max_listener._bootstrap_chat_topic",
                   new=AsyncMock(return_value=77)) as bootstrap:
            created = await _reconcile_snapshot_chats(client, sender, resolver)

        assert created == 1
        bootstrap.assert_awaited_once_with(client, sender, resolver, 2)
        saved = sender.topic_store.set_ui.call_args.args[1]
        assert set(saved) == {"1", "2"}

    async def test_reconcile_initializes_baseline_without_creating(self):
        resolver = self._resolver()
        sender = MagicMock()
        sender.topic_store.get_ui.return_value = None
        sender.topic_store.set_ui = MagicMock()
        client = MagicMock()

        with patch("app.max_listener._bootstrap_chat_topic",
                   new=AsyncMock()) as bootstrap:
            created = await _reconcile_snapshot_chats(client, sender, resolver)

        assert created == 0
        bootstrap.assert_not_awaited()
        assert set(sender.topic_store.set_ui.call_args.args[1]) == {"1", "2"}

    def test_max_service_dialog_is_excluded(self):
        resolver = ContactResolver()
        resolver._my_id = 100
        resolver.chats_raw[9] = {
            "id": 9, "type": "DIALOG", "participants": {"100": 1, "543835": 2},
        }
        resolver.chat_types[9] = "DIALOG"
        resolver.users[543835] = "MAX"
        assert _is_max_service_dialog(resolver, 9) is True

    async def test_refresh_unknown_chat_uses_chat_get_and_resolves_peer(self):
        resolver = ContactResolver()
        resolver._my_id = 100
        resolver.resolve_users_batch = AsyncMock()
        client = MagicMock()
        client.fetch_chats = AsyncMock(return_value={"chats": [{
            "id": 2, "type": "DIALOG", "status": "ACTIVE",
            "participants": {"100": 1, "55": 2},
        }]})

        await _refresh_chat_metadata(client, resolver, 2)

        client.fetch_chats.assert_awaited_once_with([2])
        assert resolver.chat_types[2] == "DIALOG"
        assert resolver.chats[2] == "DM:55"
        resolver.resolve_users_batch.assert_awaited_once()
