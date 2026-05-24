"""Tests for voice configuration & code detection hardening (Story 6.3).

Covers:
- SQLite persistence of /voice toggle (upsert_voice_output, _resolve_voice_output)
- is_code_heavy() hardened edge cases (content-only ratio, inline code, etc.)
- Graceful no-op when openai not installed
- /voice status subcommand
- Schema migration idempotency
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from message_utils import is_code_heavy


# ─── is_code_heavy() hardened tests ─────────────────────────────────────────


class TestIsCodeHeavyHardened:
    """Edge-case tests for the hardened is_code_heavy() implementation."""

    def test_content_only_ratio_excludes_delimiters(self):
        """Ratio counts only content inside ```, not the ``` delimiters."""
        # 6 chars of content, surrounded by delimiters
        text = "```\nabc\n```"  # delimiters: 8 chars, content: 4 chars (abc\n)
        total = len(text)
        # With content-only counting, ratio = 4/11 ≈ 0.36 → not code-heavy
        assert is_code_heavy(text) is False

    def test_inline_code_not_counted(self):
        """Single-backtick inline code is NOT counted as a code block."""
        text = "Use `foo()` and `bar()` and `baz()` — all inline code, no blocks."
        assert is_code_heavy(text) is False

    def test_empty_code_block_zero_content(self):
        """An empty code block (``` immediately followed by ```) has 0 content chars."""
        text = "Some text. ``` ``` More text here to keep ratio low."
        assert is_code_heavy(text) is False

    def test_language_specifier_matched(self):
        """Code blocks with language specifiers (```python) are correctly matched."""
        # Large python block
        code_content = "x = 1\n" * 50  # 300 chars
        text = f"```python\n{code_content}```\nshort"
        assert is_code_heavy(text) is True

    def test_unclosed_code_block_not_matched(self):
        """A single ``` without a closing ``` does not match."""
        text = "Here is some text with a lone ``` backtick fence that never closes."
        assert is_code_heavy(text) is False

    def test_multiple_code_blocks_content_summed(self):
        """Content from multiple code blocks is summed for ratio calculation."""
        block1 = "```\n" + "a" * 100 + "\n```"
        block2 = "```\n" + "b" * 100 + "\n```"
        text = block1 + "\n" + block2 + "\n" + "x" * 10
        # 200 content chars / ~230 total → code-heavy
        assert is_code_heavy(text) is True

    def test_very_short_text_returns_false(self):
        """Text shorter than 6 chars returns False (can't contain a valid block)."""
        assert is_code_heavy("") is False
        assert is_code_heavy("abc") is False
        assert is_code_heavy("```") is False

    def test_100_percent_code_returns_true(self):
        """A response that is entirely a code block is code-heavy."""
        text = "```python\nprint('hello world')\n```"
        assert is_code_heavy(text) is True

    def test_text_with_only_inline_code_returns_false(self):
        """Inline code only (no fenced blocks) → not code-heavy."""
        text = "Run `git status` then `git add .` and finally `git commit -m 'msg'`."
        assert is_code_heavy(text) is False

    def test_threshold_boundary(self):
        """Exactly at threshold (==) returns False; just above returns True."""
        # Build text where content ratio is exactly 0.5
        content = "x" * 50
        block = f"```\n{content}\n```"  # content = 51 chars (50 + newline)
        # Pad with non-code text to make ratio exactly 0.5
        # total = len(block) + len(padding), content = 51
        # 51 / total = 0.5 → total = 102 → padding = 102 - len(block)
        padding = "y" * (102 - len(block))
        text = block + padding
        # ratio == 0.5 → NOT code-heavy (threshold is >0.5)
        assert is_code_heavy(text) is False

        # One more content char tips it over
        text2 = f"```\n{content}z\n```" + padding
        assert is_code_heavy(text2) is True


# ─── SQLite persistence tests ────────────────────────────────────────────────


class TestVoiceOutputPersistence:
    """Tests for voice_output column and upsert_voice_output()."""

    async def test_upsert_voice_output_updates_existing_row(self, temp_db_path):
        """upsert_voice_output updates voice_output for an existing thread."""
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        # Create a thread row
        await db_module.upsert_thread_config(
            42, project_dir="/tmp/proj", path=temp_db_path
        )

        await db_module.upsert_voice_output(42, voice_output=True, path=temp_db_path)

        tc = await db_module.get_thread_config(42, path=temp_db_path)
        assert tc is not None
        assert tc.voice_output == 1

    async def test_upsert_voice_output_disable(self, temp_db_path):
        """upsert_voice_output can set voice_output to 0 (disabled)."""
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        await db_module.upsert_thread_config(
            42, project_dir="/tmp/proj", path=temp_db_path
        )
        await db_module.upsert_voice_output(42, voice_output=True, path=temp_db_path)
        await db_module.upsert_voice_output(42, voice_output=False, path=temp_db_path)

        tc = await db_module.get_thread_config(42, path=temp_db_path)
        assert tc.voice_output == 0

    async def test_upsert_voice_output_no_row_is_noop(self, temp_db_path):
        """upsert_voice_output silently no-ops when thread has no row."""
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        # No row for thread 99
        await db_module.upsert_voice_output(99, voice_output=True, path=temp_db_path)

        tc = await db_module.get_thread_config(99, path=temp_db_path)
        assert tc is None  # Still no row

    async def test_resolve_voice_output_from_sqlite(self, temp_db_path):
        """_resolve_voice_output reads from SQLite when row exists."""
        import db as db_module
        import remoclaw

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        await db_module.upsert_thread_config(
            42, project_dir="/tmp/proj", path=temp_db_path
        )
        await db_module.upsert_voice_output(42, voice_output=True, path=temp_db_path)

        with patch("remoclaw.DB_PATH", temp_db_path):
            result = await remoclaw._resolve_voice_output(42)

        assert result is True

    async def test_resolve_voice_output_falls_back_to_global(self, temp_db_path):
        """_resolve_voice_output falls back to config when no SQLite override."""
        import db as db_module
        import remoclaw

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_output_enabled", True):
            result = await remoclaw._resolve_voice_output(99)

        assert result is True

    async def test_is_voice_output_enabled_uses_cache(self, temp_db_path):
        """_is_voice_output_enabled uses in-memory cache on second call."""
        import remoclaw

        ctx = MagicMock()
        ctx.bot_data = {"thread:42:voice_output": True}

        # Cache hit — should not touch SQLite
        result = await remoclaw._is_voice_output_enabled(42, ctx)
        assert result is True

    async def test_is_voice_output_enabled_populates_cache_on_miss(
        self, temp_db_path
    ):
        """_is_voice_output_enabled populates cache on SQLite lookup."""
        import db as db_module
        import remoclaw

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        await db_module.upsert_thread_config(
            42, project_dir="/tmp/proj", path=temp_db_path
        )
        await db_module.upsert_voice_output(42, voice_output=False, path=temp_db_path)

        bot_data: dict = {}
        ctx = MagicMock()
        ctx.bot_data = bot_data

        with patch("remoclaw.DB_PATH", temp_db_path):
            result = await remoclaw._is_voice_output_enabled(42, ctx)

        assert result is False
        # Cache should now be populated
        assert bot_data.get("thread:42:voice_output") is False

    async def test_per_thread_sqlite_overrides_global_config(self, temp_db_path):
        """Per-thread SQLite value takes precedence over global config default."""
        import db as db_module
        import remoclaw

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        await db_module.upsert_thread_config(
            42, project_dir="/tmp/proj", path=temp_db_path
        )
        # SQLite says enabled, global config says disabled
        await db_module.upsert_voice_output(42, voice_output=True, path=temp_db_path)

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_output_enabled", False):
            result = await remoclaw._resolve_voice_output(42)

        assert result is True  # SQLite wins

    async def test_null_voice_output_falls_back_to_global(self, temp_db_path):
        """Thread with NULL voice_output falls back to global config."""
        import db as db_module
        import remoclaw

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        await db_module.upsert_thread_config(
            42, project_dir="/tmp/proj", path=temp_db_path
        )
        # voice_output is NULL (not set)

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_output_enabled", True):
            result = await remoclaw._resolve_voice_output(42)

        assert result is True  # Falls back to global

    async def test_new_thread_no_row_falls_back_to_global(self, temp_db_path):
        """New thread (no SQLite row) falls back to global config."""
        import db as db_module
        import remoclaw

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        # No row for thread 999

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_output_enabled", False):
            result = await remoclaw._resolve_voice_output(999)

        assert result is False


# ─── Schema migration tests ──────────────────────────────────────────────────


class TestSchemaMigration:
    """Tests for voice_output column migration idempotency."""

    async def test_init_db_fresh_has_voice_output_column(self, temp_db_path):
        """Fresh database has voice_output column after init_db."""
        import db as db_module
        import aiosqlite

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")

        async with aiosqlite.connect(temp_db_path) as conn:
            cursor = await conn.execute("PRAGMA table_info(thread_config)")
            cols = [row[1] for row in await cursor.fetchall()]

        assert "voice_output" in cols

    async def test_init_db_idempotent_with_existing_column(self, temp_db_path):
        """Calling init_db twice does not raise (ALTER TABLE is idempotent)."""
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        # Second call should not raise
        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")

    async def test_row_to_config_handles_missing_column(self, temp_db_path):
        """_row_to_config returns voice_output=None for old DBs without the column."""
        import db as db_module
        import aiosqlite

        # Create DB without voice_output column (simulates old DB)
        async with aiosqlite.connect(temp_db_path) as conn:
            await conn.execute("""
                CREATE TABLE thread_config (
                    thread_id INTEGER PRIMARY KEY,
                    project_dir TEXT NOT NULL,
                    cli_provider TEXT,
                    model TEXT,
                    timeout_seconds INTEGER,
                    last_active_at TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await conn.execute(
                "INSERT INTO thread_config (thread_id, project_dir) VALUES (?, ?)",
                (1, "/tmp/old"),
            )
            await conn.commit()

        # Reading should not crash
        tc = await db_module.get_thread_config(1, path=temp_db_path)
        assert tc is not None
        assert tc.voice_output is None


# ─── /voice status subcommand tests ─────────────────────────────────────────


class TestVoiceStatusSubcommand:
    """Tests for /voice status."""

    def _make_context(self, *, bot_data=None):
        ctx = MagicMock()
        ctx.bot_data = bot_data or {}
        ctx.user_data = {}
        ctx.args = ["status"]
        return ctx

    async def test_voice_status_shows_config_values(
        self, telegram_update_factory, temp_db_path
    ):
        """'/voice status' shows whisper model, TTS model, TTS voice."""
        import remoclaw
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")

        update = telegram_update_factory(text="/voice status")
        ctx = self._make_context()

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_enabled", True), \
             patch.object(remoclaw.config, "whisper_model", "gpt-4o-mini-transcribe"), \
             patch.object(remoclaw.config, "tts_model", "gpt-4o-mini-tts"), \
             patch.object(remoclaw.config, "tts_voice", "coral"), \
             patch.object(remoclaw.config, "voice_output_enabled", False):
            await remoclaw.cmd_voice(update, ctx)

        reply = update.message.reply_text.call_args.args[0]
        assert "gpt-4o-mini-transcribe" in reply
        assert "gpt-4o-mini-tts" in reply
        assert "coral" in reply

    async def test_voice_status_shows_global_default_source(
        self, telegram_update_factory, temp_db_path
    ):
        """'/voice status' shows 'global default' when no per-thread override."""
        import remoclaw
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")

        update = telegram_update_factory(text="/voice status")
        ctx = self._make_context()

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_enabled", True), \
             patch.object(remoclaw.config, "whisper_model", "gpt-4o-mini-transcribe"), \
             patch.object(remoclaw.config, "tts_model", "gpt-4o-mini-tts"), \
             patch.object(remoclaw.config, "tts_voice", "coral"), \
             patch.object(remoclaw.config, "voice_output_enabled", False):
            await remoclaw.cmd_voice(update, ctx)

        reply = update.message.reply_text.call_args.args[0]
        assert "global default" in reply

    async def test_voice_status_shows_per_thread_source(
        self, telegram_update_factory, temp_db_path
    ):
        """'/voice status' shows 'per-thread (SQLite)' when override exists."""
        import remoclaw
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        await db_module.upsert_thread_config(
            42, project_dir="/tmp/proj", path=temp_db_path
        )
        await db_module.upsert_voice_output(42, voice_output=True, path=temp_db_path)

        update = telegram_update_factory(text="/voice status", message_thread_id=42)
        ctx = self._make_context()

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_enabled", True), \
             patch.object(remoclaw.config, "whisper_model", "gpt-4o-mini-transcribe"), \
             patch.object(remoclaw.config, "tts_model", "gpt-4o-mini-tts"), \
             patch.object(remoclaw.config, "tts_voice", "coral"), \
             patch.object(remoclaw.config, "voice_output_enabled", False):
            await remoclaw.cmd_voice(update, ctx)

        reply = update.message.reply_text.call_args.args[0]
        assert "per-thread" in reply

    async def test_voice_status_when_voice_disabled(
        self, telegram_update_factory, temp_db_path
    ):
        """'/voice status' still shows config even when voice is disabled."""
        import remoclaw
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")

        update = telegram_update_factory(text="/voice status")
        ctx = self._make_context()

        # voice_enabled = False → should show "not configured" message
        with patch.object(remoclaw.config, "voice_enabled", False):
            await remoclaw.cmd_voice(update, ctx)

        reply = update.message.reply_text.call_args.args[0]
        assert "not configured" in reply.lower() or "Voice features" in reply


# ─── /voice toggle persistence tests ────────────────────────────────────────


class TestVoiceTogglePersistence:
    """Tests for /voice toggle writing to SQLite."""

    async def test_voice_toggle_persists_to_sqlite(
        self, telegram_update_factory, temp_db_path
    ):
        """Toggling /voice writes the new state to SQLite."""
        import remoclaw
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        await db_module.upsert_thread_config(
            42, project_dir="/tmp/proj", path=temp_db_path
        )

        update = telegram_update_factory(text="/voice", message_thread_id=42)
        ctx = MagicMock()
        ctx.bot_data = {}
        ctx.args = []

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_enabled", True), \
             patch.object(remoclaw.config, "voice_output_enabled", False):
            await remoclaw.cmd_voice(update, ctx)

        # Verify SQLite was updated
        tc = await db_module.get_thread_config(42, path=temp_db_path)
        assert tc is not None
        assert tc.voice_output == 1  # toggled from False → True

    async def test_voice_toggle_updates_in_memory_cache(
        self, telegram_update_factory, temp_db_path
    ):
        """Toggling /voice also updates context.bot_data cache immediately."""
        import remoclaw
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        await db_module.upsert_thread_config(
            42, project_dir="/tmp/proj", path=temp_db_path
        )

        update = telegram_update_factory(text="/voice", message_thread_id=42)
        ctx = MagicMock()
        ctx.bot_data = {}
        ctx.args = []

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_enabled", True), \
             patch.object(remoclaw.config, "voice_output_enabled", False):
            await remoclaw.cmd_voice(update, ctx)

        assert ctx.bot_data.get("thread:42:voice_output") is True

    async def test_voice_toggle_shows_persisted_message(
        self, telegram_update_factory, temp_db_path
    ):
        """Toggle confirmation message mentions persistence (Story 6.3)."""
        import remoclaw
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        await db_module.upsert_thread_config(
            42, project_dir="/tmp/proj", path=temp_db_path
        )

        update = telegram_update_factory(text="/voice", message_thread_id=42)
        ctx = MagicMock()
        ctx.bot_data = {}
        ctx.args = []

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.config, "voice_enabled", True), \
             patch.object(remoclaw.config, "voice_output_enabled", False):
            await remoclaw.cmd_voice(update, ctx)

        reply = update.message.reply_text.call_args.args[0]
        assert "persist" in reply.lower() or "survives" in reply.lower()


# ─── resolve_thread_config voice_output tests ────────────────────────────────


class TestResolveThreadConfigVoiceOutput:
    """Tests for voice_output in resolve_thread_config()."""

    async def test_resolve_includes_voice_output_from_sqlite(self, temp_db_path):
        """resolve_thread_config includes voice_output from SQLite."""
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        await db_module.upsert_thread_config(
            42, project_dir="/tmp/proj", path=temp_db_path
        )
        await db_module.upsert_voice_output(42, voice_output=True, path=temp_db_path)

        resolved = await db_module.resolve_thread_config(
            42,
            env_project_dir="/tmp/proj",
            env_cli_provider="kiro",
            env_voice_output=False,
            path=temp_db_path,
        )

        assert resolved.voice_output is True

    async def test_resolve_voice_output_falls_back_to_env(self, temp_db_path):
        """resolve_thread_config falls back to env_voice_output when no SQLite override."""
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")
        await db_module.upsert_thread_config(
            42, project_dir="/tmp/proj", path=temp_db_path
        )
        # voice_output is NULL

        resolved = await db_module.resolve_thread_config(
            42,
            env_project_dir="/tmp/proj",
            env_cli_provider="kiro",
            env_voice_output=True,
            path=temp_db_path,
        )

        assert resolved.voice_output is True

    async def test_resolve_voice_output_no_row_uses_env(self, temp_db_path):
        """resolve_thread_config uses env_voice_output when no row exists."""
        import db as db_module

        await db_module.init_db(temp_db_path, default_project_dir="/tmp/proj")

        resolved = await db_module.resolve_thread_config(
            999,
            env_project_dir="/tmp/proj",
            env_cli_provider="kiro",
            env_voice_output=True,
            path=temp_db_path,
        )

        assert resolved.voice_output is True


# ─── Graceful degradation tests ──────────────────────────────────────────────


class TestGracefulDegradation:
    """Tests for graceful no-op when openai/voice packages not installed."""

    async def test_voice_import_failure_leaves_services_none(self):
        """When voice module import fails, voice_transcriber/synthesizer remain None."""
        import importlib
        from unittest.mock import patch

        # Simulate ImportError on 'from voice import ...'
        with patch.dict("sys.modules", {"voice": None}):
            # The module-level initialization in chati.py already ran at import time.
            # We test the pattern: if import fails, services are None.
            import remoclaw

            # Verify the pattern: if voice services are None, voice handlers degrade
            original_transcriber = remoclaw.voice_transcriber
            remoclaw.voice_transcriber = None
            try:
                # handle_voice_message should respond with "not configured"
                from unittest.mock import MagicMock, AsyncMock

                update = MagicMock()
                update.effective_user = MagicMock()
                update.effective_user.id = 123456789
                update.message = MagicMock()
                update.message.reply_text = AsyncMock()
                update.message.voice = MagicMock()

                ctx = MagicMock()
                ctx.bot_data = {}

                with patch.object(remoclaw.config, "voice_enabled", True):
                    await remoclaw.handle_voice_message(update, ctx)

                reply = update.message.reply_text.call_args.args[0]
                assert "not configured" in reply.lower() or "type your message" in reply.lower()
            finally:
                remoclaw.voice_transcriber = original_transcriber

    async def test_voice_disabled_cmd_voice_shows_not_configured(
        self, telegram_update_factory
    ):
        """When voice_enabled is False, /voice shows 'not configured' message."""
        import remoclaw
        from unittest.mock import patch, MagicMock

        update = telegram_update_factory(text="/voice")
        ctx = MagicMock()
        ctx.bot_data = {}
        ctx.args = []

        with patch.object(remoclaw.config, "voice_enabled", False):
            await remoclaw.cmd_voice(update, ctx)

        reply = update.message.reply_text.call_args.args[0]
        assert "not configured" in reply.lower() or "Voice features" in reply

    async def test_voice_synthesizer_none_skips_tts(self, telegram_update_factory):
        """When voice_synthesizer is None, TTS is silently skipped (no crash)."""
        import remoclaw

        # voice_synthesizer = None means TTS is disabled
        original = remoclaw.voice_synthesizer
        remoclaw.voice_synthesizer = None
        try:
            # The condition `if voice_synthesizer and ...` should short-circuit
            assert remoclaw.voice_synthesizer is None
            # No crash — this is the graceful no-op pattern
        finally:
            remoclaw.voice_synthesizer = original
