"""Tests for voice output / TTS (Story 6.2).

Covers:
- VoiceSynthesizer (voice.py)
- is_code_heavy() (message_utils.py)
- TTS integration in response handlers (remoclaw.py)
- /voice toggle command (remoclaw.py)
"""

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from message_utils import is_code_heavy


# ─── is_code_heavy() tests (pure function) ──────────────────────────────────


class TestIsCodeHeavy:
    """Tests for the code-heavy detection function."""

    def test_all_code_returns_true(self):
        text = "```python\nprint('hello')\n```"
        assert is_code_heavy(text) is True

    def test_no_code_returns_false(self):
        text = "This is a plain text response with no code blocks at all."
        assert is_code_heavy(text) is False

    def test_exactly_50_percent_returns_false(self):
        """Threshold is >0.5, not >=0.5."""
        # 10 chars code block content + delimiters, 10 chars text
        code = "```\n12345\n```"  # 13 chars
        text_part = "1234567890123"  # 13 chars — exactly 50/50
        full = code + text_part
        # This should be False because ratio == 0.5, not > 0.5
        assert is_code_heavy(full) is False

    def test_above_50_percent_returns_true(self):
        code = "```python\nprint('hello world this is a long line of code')\n```"
        text_part = "short"
        full = code + text_part
        assert is_code_heavy(full) is True

    def test_empty_string_returns_false(self):
        assert is_code_heavy("") is False

    def test_multiple_code_blocks(self):
        text = (
            "Here's some explanation.\n\n"
            "```python\ndef foo():\n    pass\n```\n\n"
            "And more:\n\n"
            "```javascript\nconst x = 1;\n```\n\n"
            "End."
        )
        # Verify the function returns a consistent bool result
        result = is_code_heavy(text)
        assert isinstance(result, bool)

    def test_backticks_not_code_blocks(self):
        """Single backticks (inline code) should NOT count as code blocks."""
        text = "Use `foo()` and `bar()` in your code. This is mostly text."
        assert is_code_heavy(text) is False

    def test_custom_threshold(self):
        code = "```\ncode\n```"  # 14 chars
        text_part = "text here is longer than code"  # 29 chars
        full = code + text_part
        # With default 0.5 threshold, this is not code-heavy
        assert is_code_heavy(full) is False
        # With a very low threshold, it becomes code-heavy
        assert is_code_heavy(full, threshold=0.1) is True


# ─── VoiceSynthesizer tests (voice.py) ──────────────────────────────────────


class TestVoiceSynthesizer:
    """Tests for the VoiceSynthesizer TTS wrapper."""

    def _make_synth_with_mock_client(self, api_key="sk-test", **kwargs):
        """Create a VoiceSynthesizer with a pre-wired mock OpenAI client."""
        from voice import VoiceSynthesizer
        synth = VoiceSynthesizer(api_key=api_key, **kwargs)
        mock_client = MagicMock()
        synth._openai_client = mock_client  # inject before lazy-init fires
        return synth, mock_client

    async def test_synthesize_returns_bytes_on_success(self):
        synth, mock_client = self._make_synth_with_mock_client(
            model="gpt-4o-mini-tts", voice="coral", timeout=10
        )
        fake_response = MagicMock()
        fake_response.content = b"fake-opus-audio-bytes"
        mock_client.audio.speech.create = AsyncMock(return_value=fake_response)

        result = await synth.synthesize("Hello world")
        assert result == b"fake-opus-audio-bytes"
        mock_client.audio.speech.create.assert_awaited_once()

    async def test_synthesize_returns_none_on_timeout(self):
        synth, mock_client = self._make_synth_with_mock_client(timeout=1)

        async def slow(*args, **kwargs):
            await asyncio.sleep(5)

        mock_client.audio.speech.create = slow
        result = await synth.synthesize("Hello")
        assert result is None

    async def test_synthesize_returns_none_on_api_error(self):
        synth, mock_client = self._make_synth_with_mock_client()
        mock_client.audio.speech.create = AsyncMock(side_effect=RuntimeError("API down"))
        assert await synth.synthesize("Hello") is None

    async def test_synthesize_returns_none_on_empty_content(self):
        synth, mock_client = self._make_synth_with_mock_client()
        fake_response = MagicMock()
        fake_response.content = b""
        mock_client.audio.speech.create = AsyncMock(return_value=fake_response)
        assert await synth.synthesize("Hello") is None

    async def test_synthesize_truncates_long_text(self):
        synth, mock_client = self._make_synth_with_mock_client()
        fake_response = MagicMock()
        fake_response.content = b"audio"
        mock_client.audio.speech.create = AsyncMock(return_value=fake_response)

        long_text = "x" * 5000
        await synth.synthesize(long_text)

        call_kwargs = mock_client.audio.speech.create.call_args.kwargs
        assert len(call_kwargs["input"]) == 4096
        assert call_kwargs["input"].endswith("...")

    async def test_synthesize_uses_opus_format(self):
        synth, mock_client = self._make_synth_with_mock_client()
        fake_response = MagicMock()
        fake_response.content = b"audio"
        mock_client.audio.speech.create = AsyncMock(return_value=fake_response)

        await synth.synthesize("Hello")
        call_kwargs = mock_client.audio.speech.create.call_args.kwargs
        assert call_kwargs["response_format"] == "opus"

    async def test_synthesize_returns_none_for_empty_input(self):
        from voice import VoiceSynthesizer
        synth = VoiceSynthesizer(api_key="sk-test")
        assert await synth.synthesize("") is None
        assert await synth.synthesize("   ") is None


# ─── TTS integration in remoclaw.py ────────────────────────────────────────────


def _make_context(*, bot_data=None):
    """Build a mock context with bot_data."""
    ctx = MagicMock()
    ctx.bot_data = bot_data if bot_data is not None else {}
    ctx.user_data = {}
    ctx.bot = MagicMock()
    return ctx


class TestVoiceOutputIntegration:
    """Tests for TTS integration in response handlers."""

    async def test_voice_enabled_non_code_sends_voice_message(
        self, telegram_update_factory, monkeypatch
    ):
        """When voice output is enabled and response is not code-heavy, voice is sent."""
        import remoclaw

        update = telegram_update_factory(text="hello", message_thread_id=42)
        update.message.reply_voice = AsyncMock()

        synth = MagicMock()
        synth.synthesize = AsyncMock(return_value=b"fake-audio")

        monkeypatch.setattr(remoclaw, "voice_synthesizer", synth)

        # Call the helper directly
        await remoclaw._send_voice_message(update, b"fake-audio")
        update.message.reply_voice.assert_awaited_once()

    async def test_is_voice_output_enabled_per_thread_override(self):
        """Per-thread override takes precedence over global config."""
        import remoclaw

        ctx = _make_context(bot_data={"thread:42:voice_output": True})
        assert await remoclaw._is_voice_output_enabled(42, ctx) is True

        ctx2 = _make_context(bot_data={"thread:42:voice_output": False})
        assert await remoclaw._is_voice_output_enabled(42, ctx2) is False

    async def test_is_voice_output_enabled_falls_back_to_global(self, temp_db_path):
        """Without per-thread override, falls back to global config."""
        import remoclaw
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")

        ctx = _make_context()
        # Global default is False (from config)
        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_output_enabled", False, create=True):
            assert await remoclaw._is_voice_output_enabled(42, ctx) is False

        ctx2 = _make_context()
        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_output_enabled", True, create=True):
            assert await remoclaw._is_voice_output_enabled(42, ctx2) is True

    async def test_strip_html_for_tts(self):
        """HTML tags and entities are stripped for TTS input."""
        import remoclaw

        html = "<b>Hello</b> &amp; <code>world</code>"
        result = remoclaw._strip_html_for_tts(html)
        assert result == "Hello & world"

    async def test_strip_html_for_tts_collapses_whitespace(self):
        import remoclaw

        html = "Hello   \n\n   world"
        result = remoclaw._strip_html_for_tts(html)
        assert result == "Hello world"

    async def test_send_voice_message_handles_failure_gracefully(
        self, telegram_update_factory
    ):
        """If reply_voice fails, it logs but doesn't raise."""
        import remoclaw

        update = telegram_update_factory(text="x")
        update.message.reply_voice = AsyncMock(side_effect=RuntimeError("Telegram error"))

        # Should not raise
        await remoclaw._send_voice_message(update, b"audio")


# ─── /voice toggle command ──────────────────────────────────────────────────


class TestCmdVoice:
    """Tests for the /voice toggle command."""

    async def test_voice_disabled_shows_not_configured(self, telegram_update_factory):
        import remoclaw

        update = telegram_update_factory(text="/voice")
        ctx = _make_context()

        with patch.object(remoclaw.config, "voice_enabled", False, create=True):
            await remoclaw.cmd_voice(update, ctx)

        update.message.reply_text.assert_awaited_once()
        msg = update.message.reply_text.call_args.args[0]
        assert "not configured" in msg

    async def test_voice_toggle_enables(self, telegram_update_factory, temp_db_path):
        import remoclaw
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")

        update = telegram_update_factory(text="/voice", message_thread_id=42)
        ctx = _make_context()

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_enabled", True, create=True), \
             patch.object(remoclaw.config, "voice_output_enabled", False, create=True):
            await remoclaw.cmd_voice(update, ctx)

        # Should have toggled from False → True
        assert ctx.bot_data.get("thread:42:voice_output") is True
        msg = update.message.reply_text.call_args.args[0]
        assert "enabled" in msg

    async def test_voice_toggle_disables(self, telegram_update_factory, temp_db_path):
        import remoclaw
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")

        update = telegram_update_factory(text="/voice", message_thread_id=42)
        ctx = _make_context(bot_data={"thread:42:voice_output": True})

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_enabled", True, create=True):
            await remoclaw.cmd_voice(update, ctx)

        assert ctx.bot_data.get("thread:42:voice_output") is False
        msg = update.message.reply_text.call_args.args[0]
        assert "disabled" in msg


# ─── Local backend tests ─────────────────────────────────────────────────────


class TestLocalBackends:
    """Tests for faster-whisper and edge-tts local backends."""

    async def test_transcriber_uses_local_when_no_api_key(self, tmp_path):
        """VoiceTranscriber without API key uses local faster-whisper backend."""
        from voice import VoiceTranscriber

        t = VoiceTranscriber(api_key="")  # no key → local backend
        assert t._use_openai is False

    async def test_synthesizer_uses_local_when_no_api_key(self):
        """VoiceSynthesizer without API key uses edge-tts backend."""
        from voice import VoiceSynthesizer

        s = VoiceSynthesizer(api_key="")  # no key → edge-tts
        assert s._use_openai is False

    async def test_transcriber_uses_openai_when_key_present(self):
        """VoiceTranscriber with API key uses OpenAI backend."""
        from voice import VoiceTranscriber

        t = VoiceTranscriber(api_key="sk-test")
        assert t._use_openai is True

    async def test_synthesizer_uses_openai_when_key_present(self):
        """VoiceSynthesizer with API key uses OpenAI backend."""
        from voice import VoiceSynthesizer

        s = VoiceSynthesizer(api_key="sk-test")
        assert s._use_openai is True

    async def test_local_transcription_returns_none_on_missing_file(self):
        """Local transcription returns None for missing audio file."""
        from voice import VoiceTranscriber

        t = VoiceTranscriber(api_key="")
        result = await t.transcribe("/nonexistent/path/audio.ogg")
        assert result is None

    async def test_edge_tts_synthesize_returns_none_on_error(self):
        """edge-tts failure returns None gracefully."""
        from voice import VoiceSynthesizer

        s = VoiceSynthesizer(api_key="")

        with patch("edge_tts.Communicate") as mock_comm:
            mock_comm.return_value.save = AsyncMock(side_effect=RuntimeError("network error"))
            result = await s.synthesize("Hello world")

        assert result is None
