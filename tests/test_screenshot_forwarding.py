"""Tests for screenshot detection and forwarding (Story 5.1)."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from message_utils import detect_screenshots


# ─── detect_screenshots() tests ─────────────────────────────────────────────


class TestDetectScreenshots:
    """Pure function tests — detection logic only (no I/O)."""

    def test_single_absolute_path(self):
        assert detect_screenshots("/tmp/screenshot.png") == ["/tmp/screenshot.png"]

    def test_path_with_surrounding_text(self):
        text = "Screenshot saved to /tmp/shot.png done"
        assert detect_screenshots(text) == ["/tmp/shot.png"]

    def test_multiple_paths_preserves_order(self):
        text = (
            "First: /home/user/a.png\n"
            "Second: /home/user/b.jpg\n"
            "Third: /home/user/c.webp\n"
        )
        assert detect_screenshots(text) == [
            "/home/user/a.png",
            "/home/user/b.jpg",
            "/home/user/c.webp",
        ]

    def test_all_supported_extensions(self):
        extensions = ["png", "jpg", "jpeg", "gif", "webp"]
        text = "\n".join(f"/tmp/shot.{ext}" for ext in extensions)
        paths = detect_screenshots(text)
        assert len(paths) == 5
        for ext in extensions:
            assert any(p.endswith(f".{ext}") for p in paths)

    def test_case_insensitive_extensions(self):
        text = "/tmp/A.PNG /tmp/B.Jpg /tmp/C.JPEG"
        paths = detect_screenshots(text)
        assert len(paths) == 3

    def test_relative_path_dot_slash(self):
        text = "Saved to ./screenshots/page.png"
        assert detect_screenshots(text) == ["./screenshots/page.png"]

    def test_no_paths_returns_empty(self):
        assert detect_screenshots("No images here, just text.") == []
        assert detect_screenshots("") == []

    def test_urls_not_detected(self):
        text = "Check https://example.com/image.png for reference"
        assert detect_screenshots(text) == []

    def test_http_urls_not_detected(self):
        text = "Download from http://foo.bar/photo.jpg"
        assert detect_screenshots(text) == []

    def test_duplicates_deduplicated(self):
        text = (
            "/tmp/shot.png\n"
            "See /tmp/shot.png again\n"
            "Final: /tmp/shot.png"
        )
        assert detect_screenshots(text) == ["/tmp/shot.png"]

    def test_non_image_extensions_ignored(self):
        text = "/tmp/code.py /tmp/readme.txt /tmp/doc.pdf"
        assert detect_screenshots(text) == []

    def test_path_in_quoted_string(self):
        text = 'File="/tmp/image.png"'
        assert "/tmp/image.png" in detect_screenshots(text)

    def test_path_with_colon_prefix(self):
        text = "Output: /tmp/result.jpg"
        assert "/tmp/result.jpg" in detect_screenshots(text)

    def test_path_with_ansi_codes_stripped(self):
        # ANSI codes should be stripped before detection
        text = "\x1b[32mSaved: /tmp/green.png\x1b[0m"
        assert "/tmp/green.png" in detect_screenshots(text)

    def test_path_with_deep_directory(self):
        text = "Saved to /home/user/project/.kiro/screenshots/page-01.png"
        assert detect_screenshots(text) == [
            "/home/user/project/.kiro/screenshots/page-01.png"
        ]

    def test_path_with_hyphens_and_underscores(self):
        text = "/tmp/my_image-v2.png /tmp/snap_shot-2026.jpg"
        paths = detect_screenshots(text)
        assert "/tmp/my_image-v2.png" in paths
        assert "/tmp/snap_shot-2026.jpg" in paths

    def test_mixed_absolute_and_relative(self):
        text = "Abs: /tmp/a.png, Rel: ./b.jpg"
        paths = detect_screenshots(text)
        assert "/tmp/a.png" in paths
        assert "./b.jpg" in paths

    def test_empty_string(self):
        assert detect_screenshots("") == []


# ─── _send_screenshots() tests ─────────────────────────────────────────────


class TestSendScreenshots:
    """Integration tests for the Telegram sending helper."""

    @pytest.fixture
    def mock_update(self):
        """Minimal mock Update for reply_photo/reply_document testing."""
        update = MagicMock()
        update.message = MagicMock()
        update.message.reply_photo = AsyncMock()
        update.message.reply_document = AsyncMock()
        return update

    @pytest.mark.asyncio
    async def test_file_exists_small_sends_photo(self, mock_update, tmp_path):
        # Create a small fake image (~100 bytes)
        img_path = tmp_path / "small.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)

        from remoclaw import _send_screenshots

        count = await _send_screenshots(mock_update, [str(img_path)])

        assert count == 1
        mock_update.message.reply_photo.assert_called_once()
        mock_update.message.reply_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_larger_than_10mb_sends_document(self, mock_update, tmp_path):
        # Create a >10MB file
        big_path = tmp_path / "big.png"
        big_path.write_bytes(b"x" * (11 * 1024 * 1024))  # 11MB

        from remoclaw import _send_screenshots

        count = await _send_screenshots(mock_update, [str(big_path)])

        assert count == 1
        mock_update.message.reply_document.assert_called_once()
        mock_update.message.reply_photo.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_not_exists_skipped(self, mock_update):
        from remoclaw import _send_screenshots

        count = await _send_screenshots(mock_update, ["/nonexistent/path.png"])

        assert count == 0
        mock_update.message.reply_photo.assert_not_called()
        mock_update.message.reply_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_files_some_missing(self, mock_update, tmp_path):
        good_path = tmp_path / "good.png"
        good_path.write_bytes(b"\x89PNG" + b"x" * 50)

        from remoclaw import _send_screenshots

        count = await _send_screenshots(
            mock_update,
            [str(good_path), "/nonexistent/bad.png"],
        )

        assert count == 1
        assert mock_update.message.reply_photo.call_count == 1

    @pytest.mark.asyncio
    async def test_relative_path_resolved_against_project_dir(
        self, mock_update, tmp_path
    ):
        # Create file at project_dir/shot.png
        img_path = tmp_path / "shot.png"
        img_path.write_bytes(b"\x89PNG" + b"x" * 50)

        from remoclaw import _send_screenshots

        count = await _send_screenshots(
            mock_update,
            ["./shot.png"],
            project_dir=str(tmp_path),
        )

        assert count == 1
        mock_update.message.reply_photo.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_photo_api_failure_graceful(self, mock_update, tmp_path):
        img_path = tmp_path / "shot.png"
        img_path.write_bytes(b"\x89PNG" + b"x" * 50)

        # reply_photo raises an exception
        mock_update.message.reply_photo = AsyncMock(
            side_effect=Exception("Telegram API error")
        )

        from remoclaw import _send_screenshots

        # Should not crash — logs warning, continues
        count = await _send_screenshots(mock_update, [str(img_path)])

        assert count == 0  # nothing successful

    @pytest.mark.asyncio
    async def test_empty_path_list(self, mock_update):
        from remoclaw import _send_screenshots

        count = await _send_screenshots(mock_update, [])

        assert count == 0
        mock_update.message.reply_photo.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_files_all_sent_in_order(self, mock_update, tmp_path):
        paths = []
        for i in range(3):
            p = tmp_path / f"img{i}.png"
            p.write_bytes(b"\x89PNG" + b"x" * 50)
            paths.append(str(p))

        from remoclaw import _send_screenshots

        count = await _send_screenshots(mock_update, paths)

        assert count == 3
        assert mock_update.message.reply_photo.call_count == 3

    @pytest.mark.asyncio
    async def test_isfile_oserror_skipped(self, mock_update):
        """OSError from os.path.isfile is caught and path is skipped."""
        from remoclaw import _send_screenshots

        with patch("os.path.isfile", side_effect=OSError("permission denied")):
            count = await _send_screenshots(mock_update, ["/some/path.png"])

        assert count == 0
        mock_update.message.reply_photo.assert_not_called()

    @pytest.mark.asyncio
    async def test_getsize_oserror_skipped(self, mock_update, tmp_path):
        """OSError from os.path.getsize is caught and path is skipped."""
        img_path = tmp_path / "shot.png"
        img_path.write_bytes(b"\x89PNG" + b"x" * 50)

        from remoclaw import _send_screenshots

        with patch("os.path.getsize", side_effect=OSError("permission denied")):
            count = await _send_screenshots(mock_update, [str(img_path)])

        assert count == 0
        mock_update.message.reply_photo.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_document_api_failure_graceful(self, mock_update, tmp_path):
        """reply_document raising an exception is caught and path is skipped."""
        big_path = tmp_path / "big.png"
        big_path.write_bytes(b"x" * (11 * 1024 * 1024))  # 11MB

        mock_update.message.reply_document = AsyncMock(
            side_effect=Exception("Telegram API error")
        )

        from remoclaw import _send_screenshots

        count = await _send_screenshots(mock_update, [str(big_path)])

        assert count == 0
        mock_update.message.reply_document.assert_called_once()
        mock_update.message.reply_photo.assert_not_called()
