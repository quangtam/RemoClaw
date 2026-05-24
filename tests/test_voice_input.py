"""Tests for voice input (Story 6.1) — transcription + confirmation UX."""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── VoiceTranscriber tests (voice.py module) ───────────────────────────────


class TestVoiceTranscriber:
    """Tests for the VoiceTranscriber wrapper around OpenAI Whisper."""

    @pytest.fixture
    def audio_path(self, tmp_path):
        """Create a tiny fake audio file on disk."""
        p = tmp_path / "sample.ogg"
        p.write_bytes(b"fake-ogg-bytes")
        return str(p)

    def _make_transcriber_with_mock_client(self, api_key="sk-test", **kwargs):
        """Create a VoiceTranscriber with a pre-wired mock OpenAI client."""
        from voice import VoiceTranscriber
        t = VoiceTranscriber(api_key=api_key, **kwargs)
        mock_client = MagicMock()
        t._openai_client = mock_client  # inject before lazy-init fires
        return t, mock_client

    async def test_transcribe_returns_text_on_success(self, audio_path):
        """Successful transcription returns the trimmed text string."""
        t, mock_client = self._make_transcriber_with_mock_client(
            model="gpt-4o-mini-transcribe", timeout=10
        )
        fake_create = AsyncMock(return_value="  hello world  ")
        mock_client.audio.transcriptions.create = fake_create

        result = await t.transcribe(audio_path)
        assert result == "hello world"
        fake_create.assert_awaited_once()

    async def test_transcribe_handles_object_response(self, audio_path):
        """Transcribe handles object responses that have a .text attribute."""
        t, mock_client = self._make_transcriber_with_mock_client()

        class _Resp:
            text = " hi there "

        mock_client.audio.transcriptions.create = AsyncMock(return_value=_Resp())
        result = await t.transcribe(audio_path)
        assert result == "hi there"

    async def test_transcribe_returns_none_on_empty_text(self, audio_path):
        """Empty transcription result collapses to None."""
        t, mock_client = self._make_transcriber_with_mock_client()
        mock_client.audio.transcriptions.create = AsyncMock(return_value="   ")
        assert await t.transcribe(audio_path) is None

    async def test_transcribe_returns_none_on_timeout(self, audio_path):
        """Transcription timeout yields None (graceful degradation)."""
        t, mock_client = self._make_transcriber_with_mock_client(timeout=1)

        async def slow(*args, **kwargs):
            await asyncio.sleep(5)

        mock_client.audio.transcriptions.create = slow
        result = await t.transcribe(audio_path)
        assert result is None

    async def test_transcribe_returns_none_on_api_error(self, audio_path):
        """Any API exception yields None (graceful degradation)."""
        t, mock_client = self._make_transcriber_with_mock_client()
        mock_client.audio.transcriptions.create = AsyncMock(
            side_effect=RuntimeError("API boom")
        )
        assert await t.transcribe(audio_path) is None

    async def test_transcribe_missing_file_returns_none(self, tmp_path):
        """Missing audio file returns None rather than raising."""
        from voice import VoiceTranscriber

        t = VoiceTranscriber(api_key="sk-test")
        result = await t.transcribe(str(tmp_path / "nope.ogg"))
        assert result is None


# ─── handle_voice_message (remoclaw.py) tests ──────────────────────────────────


def _make_voice_update(tg_factory, *, thread_id=None):
    """Build a telegram Update with a .voice attribute suitable for handler."""
    update = tg_factory(message_thread_id=thread_id)
    update.message.voice = MagicMock()
    update.message.voice.file_id = "fake-file-id"
    update.message.reply_photo = AsyncMock()
    update.message.reply_document = AsyncMock()
    return update


def _make_context(*, bot=None):
    """Build a ContextTypes.DEFAULT_TYPE-like mock with bot + bot_data."""
    ctx = MagicMock()
    ctx.bot_data = {}
    ctx.user_data = {}
    ctx.bot = bot or MagicMock()
    return ctx


class TestHandleVoiceMessage:
    """Integration tests for the voice message handler in remoclaw.py."""

    async def test_voice_disabled_sends_fallback_message(self, telegram_update_factory):
        """When voice_enabled=False, handler sends the 'not configured' message."""
        import remoclaw

        update = _make_voice_update(telegram_update_factory)
        ctx = _make_context()

        with patch.object(remoclaw.config, "voice_enabled", False, create=True):
            await remoclaw.handle_voice_message(update, ctx)

        update.message.reply_text.assert_awaited_once()
        (args, _) = update.message.reply_text.call_args
        assert "Voice features not configured" in args[0]

    async def test_transcription_success_shows_inline_keyboard(
        self, telegram_update_factory
    ):
        """On successful transcription with auto_send=False, handler shows the confirm keyboard."""
        import remoclaw

        update = _make_voice_update(telegram_update_factory, thread_id=42)
        ctx = _make_context()

        voice_file = MagicMock()
        voice_file.download_to_drive = AsyncMock()
        ctx.bot.get_file = AsyncMock(return_value=voice_file)

        transcriber = MagicMock()
        transcriber.transcribe = AsyncMock(return_value="hello world")

        with patch.object(remoclaw.config, "voice_enabled", True, create=True), \
             patch.object(remoclaw.config, "voice_auto_send", False, create=True), \
             patch.object(remoclaw, "voice_transcriber", transcriber, create=True):
            await remoclaw.handle_voice_message(update, ctx)

        # The reply_text call should include the transcribed text
        update.message.reply_text.assert_awaited()
        last_call = update.message.reply_text.call_args
        assert "hello world" in last_call.args[0]
        # Inline keyboard should have been provided
        assert last_call.kwargs.get("reply_markup") is not None

        # Per-thread state should hold the transcription
        assert ctx.bot_data[f"thread:42:voice_transcription"] == "hello world"

    async def test_transcription_success_auto_send(
        self, telegram_update_factory, monkeypatch
    ):
        """On successful transcription with auto_send=True, handler forwards immediately."""
        import remoclaw

        update = _make_voice_update(telegram_update_factory, thread_id=42)
        ctx = _make_context()

        voice_file = MagicMock()
        voice_file.download_to_drive = AsyncMock()
        ctx.bot.get_file = AsyncMock(return_value=voice_file)

        transcriber = MagicMock()
        transcriber.transcribe = AsyncMock(return_value="hello world")

        captured: dict = {}

        async def fake_execute(upd, c, prompt):
            captured["prompt"] = prompt

        monkeypatch.setattr(remoclaw, "_execute_and_reply", fake_execute)

        with patch.object(remoclaw.config, "voice_enabled", True, create=True), \
             patch.object(remoclaw.config, "voice_auto_send", True, create=True), \
             patch.object(remoclaw, "voice_transcriber", transcriber, create=True):
            await remoclaw.handle_voice_message(update, ctx)

        # Should have forwarded directly without keyboard
        assert captured.get("prompt") == "hello world"
        last_call = update.message.reply_text.call_args
        assert last_call.kwargs.get("reply_markup") is None

    async def test_transcription_failure_sends_error(self, telegram_update_factory):
        """When transcription returns None, handler sends the error message."""
        import remoclaw

        update = _make_voice_update(telegram_update_factory)
        ctx = _make_context()

        voice_file = MagicMock()
        voice_file.download_to_drive = AsyncMock()
        ctx.bot.get_file = AsyncMock(return_value=voice_file)

        transcriber = MagicMock()
        transcriber.transcribe = AsyncMock(return_value=None)

        with patch.object(remoclaw.config, "voice_enabled", True, create=True), patch.object(
            remoclaw, "voice_transcriber", transcriber, create=True
        ):
            await remoclaw.handle_voice_message(update, ctx)

        update.message.reply_text.assert_awaited()
        last_call = update.message.reply_text.call_args
        assert "temporarily unavailable" in last_call.args[0]

    async def test_temp_file_is_cleaned_up(self, telegram_update_factory):
        """The downloaded temp file must be deleted after processing."""
        import remoclaw

        update = _make_voice_update(telegram_update_factory)
        ctx = _make_context()

        captured_path: dict = {}

        async def fake_download(path):
            captured_path["path"] = path
            # Simulate actual file creation — NamedTemporaryFile already
            # created the file, but this is here for realism.
            with open(path, "wb") as f:
                f.write(b"ogg")

        voice_file = MagicMock()
        voice_file.download_to_drive = fake_download
        ctx.bot.get_file = AsyncMock(return_value=voice_file)

        transcriber = MagicMock()
        transcriber.transcribe = AsyncMock(return_value="x")

        with patch.object(remoclaw.config, "voice_enabled", True, create=True), \
             patch.object(remoclaw.config, "voice_auto_send", False, create=True), \
             patch.object(remoclaw, "voice_transcriber", transcriber, create=True):
            await remoclaw.handle_voice_message(update, ctx)

        assert "path" in captured_path
        assert not os.path.exists(captured_path["path"])  # cleaned up

    async def test_temp_file_cleaned_up_on_transcription_failure(
        self, telegram_update_factory
    ):
        """Temp file cleanup must happen even when transcription fails."""
        import remoclaw

        update = _make_voice_update(telegram_update_factory)
        ctx = _make_context()

        captured_path: dict = {}

        async def fake_download(path):
            captured_path["path"] = path
            with open(path, "wb") as f:
                f.write(b"ogg")

        voice_file = MagicMock()
        voice_file.download_to_drive = fake_download
        ctx.bot.get_file = AsyncMock(return_value=voice_file)

        transcriber = MagicMock()
        transcriber.transcribe = AsyncMock(side_effect=RuntimeError("whisper down"))

        with patch.object(remoclaw.config, "voice_enabled", True, create=True), \
             patch.object(remoclaw.config, "voice_auto_send", False, create=True), \
             patch.object(remoclaw, "voice_transcriber", transcriber, create=True):
            # Handler should NOT raise — it should log and continue.
            await remoclaw.handle_voice_message(update, ctx)

        assert "path" in captured_path
        assert not os.path.exists(captured_path["path"])


# ─── handle_voice_callback (remoclaw.py) tests ─────────────────────────────────


class TestHandleVoiceCallback:
    """Tests for the inline keyboard callback handler."""

    def _make_callback_update(self, action: str, thread_id="42"):
        update = MagicMock()
        update.effective_user = MagicMock(id=123456789)
        update.effective_chat = MagicMock(id=1)
        query = MagicMock()
        query.from_user = MagicMock(id=123456789)
        query.data = f"voice:{action}:{thread_id}"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        # query.message is the bot's own message (inline keyboard message)
        query.message = MagicMock()
        query.message.reply_text = AsyncMock()
        query.message.reply_chat_action = AsyncMock()
        update.callback_query = query
        update.message = None  # Callback queries have no update.message
        return update

    async def test_send_forwards_transcription_to_cli(self, monkeypatch):
        import remoclaw

        update = self._make_callback_update("send")
        ctx = _make_context()
        ctx.bot_data["thread:42:voice_transcription"] = "build feature X"

        captured: dict = {}

        async def fake_execute(upd, c, prompt):
            captured["prompt"] = prompt

        monkeypatch.setattr(remoclaw, "_execute_and_reply", fake_execute)

        await remoclaw.handle_voice_callback(update, ctx)

        assert captured.get("prompt") == "build feature X"
        # Transcription cleared from bot_data
        assert "thread:42:voice_transcription" not in ctx.bot_data

    async def test_edit_sets_edit_mode_flag(self, monkeypatch):
        import remoclaw

        update = self._make_callback_update("edit")
        ctx = _make_context()
        ctx.bot_data["thread:42:voice_transcription"] = "original text"

        await remoclaw.handle_voice_callback(update, ctx)

        assert ctx.bot_data.get("thread:42:voice_edit_mode") is True
        # Original transcription should still be available until replaced
        assert ctx.bot_data.get("thread:42:voice_transcription") == "original text"
        update.callback_query.edit_message_text.assert_awaited()

    async def test_cancel_discards_transcription(self):
        import remoclaw

        update = self._make_callback_update("cancel")
        ctx = _make_context()
        ctx.bot_data["thread:42:voice_transcription"] = "something"

        await remoclaw.handle_voice_callback(update, ctx)

        assert "thread:42:voice_transcription" not in ctx.bot_data
        update.callback_query.edit_message_text.assert_awaited()

    async def test_malformed_callback_data_is_ignored(self):
        import remoclaw

        update = MagicMock()
        update.effective_user = MagicMock(id=123456789)
        query = MagicMock()
        query.from_user = MagicMock(id=123456789)
        query.data = "voice:bogus"  # wrong number of segments
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        update.message = None
        ctx = _make_context()

        # Should not raise
        await remoclaw.handle_voice_callback(update, ctx)

    async def test_send_with_expired_transcription_shows_error(self, monkeypatch):
        """If transcription is missing from bot_data (bot restart), show error."""
        import remoclaw

        update = self._make_callback_update("send")
        ctx = _make_context()
        # No transcription stored — simulates bot restart

        await remoclaw.handle_voice_callback(update, ctx)

        # Should show expiration message, NOT forward to CLI
        update.callback_query.edit_message_text.assert_awaited()
        call_args = update.callback_query.edit_message_text.call_args
        assert "expired" in call_args.args[0].lower()

    async def test_send_sets_update_message_from_callback_query(self, monkeypatch):
        """The 'send' action must set update.message = query.message for _execute_and_reply."""
        import remoclaw

        update = self._make_callback_update("send")
        ctx = _make_context()
        ctx.bot_data["thread:42:voice_transcription"] = "test prompt"

        captured: dict = {}

        async def fake_execute(upd, c, prompt):
            captured["prompt"] = prompt
            captured["has_message"] = upd.message is not None

        monkeypatch.setattr(remoclaw, "_execute_and_reply", fake_execute)

        await remoclaw.handle_voice_callback(update, ctx)

        assert captured.get("prompt") == "test prompt"
        assert captured.get("has_message") is True  # update.message was set


# ─── Voice edit-mode in handle_message ──────────────────────────────────────


class TestVoiceEditModeInHandleMessage:
    """Verify edit mode intercepts the next text message for the thread."""

    async def test_edit_mode_forwards_corrected_text(
        self, telegram_update_factory, monkeypatch
    ):
        import remoclaw

        update = telegram_update_factory(text="corrected prompt", message_thread_id=42)
        ctx = _make_context()
        ctx.bot_data["thread:42:voice_edit_mode"] = True
        ctx.bot_data["thread:42:voice_transcription"] = "original noise"

        captured: dict = {}

        async def fake_execute(upd, c, prompt):
            captured["prompt"] = prompt

        monkeypatch.setattr(remoclaw, "_execute_and_reply", fake_execute)

        await remoclaw.handle_message(update, ctx)

        assert captured.get("prompt") == "corrected prompt"
        # Flag and stored transcription cleared after one-shot intercept
        assert "thread:42:voice_edit_mode" not in ctx.bot_data
        assert "thread:42:voice_transcription" not in ctx.bot_data

    async def test_edit_mode_does_not_affect_other_threads(
        self, telegram_update_factory, monkeypatch
    ):
        import remoclaw

        # Thread 42 has edit mode set, but message comes from thread 99
        update = telegram_update_factory(text="normal msg", message_thread_id=99)
        ctx = _make_context()
        ctx.bot_data["thread:42:voice_edit_mode"] = True

        captured: dict = {}

        async def fake_execute(upd, c, prompt):
            captured["prompt"] = prompt

        monkeypatch.setattr(remoclaw, "_execute_and_reply", fake_execute)

        await remoclaw.handle_message(update, ctx)

        # Thread 42 flag still set (untouched)
        assert ctx.bot_data.get("thread:42:voice_edit_mode") is True
        # Message was routed normally
        assert captured.get("prompt") == "normal msg"
