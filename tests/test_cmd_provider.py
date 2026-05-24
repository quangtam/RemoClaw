"""Tests for /provider command handler and /model SQLite persistence."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db import DEFAULT_THREAD_ID, get_thread_config, init_db, upsert_thread_config


@pytest.fixture
def clean_runner():
    """Ensure runner has no active sessions during test."""
    import remoclaw
    original_sessions = remoclaw.runner._sessions.copy()
    remoclaw.runner._sessions.clear()
    yield remoclaw.runner
    remoclaw.runner._sessions = original_sessions


class TestCmdProvider:
    """Tests for /provider command."""

    async def test_valid_switch_persists(
        self, telegram_update_factory, temp_db_path, clean_runner
    ):
        from remoclaw import cmd_provider

        await init_db(temp_db_path, default_project_dir="/tmp/default")
        update = telegram_update_factory(text="/provider claude")
        ctx = MagicMock()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_provider(update, ctx)

        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config.cli_provider == "claude"

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "✅" in reply
        assert "claude" in reply.lower()

    async def test_no_argument_shows_available(
        self, telegram_update_factory, temp_db_path, clean_runner
    ):
        from remoclaw import cmd_provider

        await init_db(temp_db_path, default_project_dir="/tmp/default")
        update = telegram_update_factory(text="/provider")
        ctx = MagicMock()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_provider(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply or "⚠️" in reply
        assert "kiro" in reply.lower() or "claude" in reply.lower()

    async def test_invalid_provider_rejected(
        self, telegram_update_factory, temp_db_path, clean_runner
    ):
        from remoclaw import cmd_provider

        await init_db(temp_db_path, default_project_dir="/tmp/default")
        update = telegram_update_factory(text="/provider notreal")
        ctx = MagicMock()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_provider(update, ctx)

        # Provider should NOT have been changed
        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config.cli_provider is None

        reply = update.message.reply_text.call_args[0][0]
        assert "Unknown" in reply or "⚠️" in reply

    async def test_active_session_killed_on_switch(
        self, telegram_update_factory, temp_db_path, clean_runner
    ):
        from remoclaw import cmd_provider

        await init_db(temp_db_path, default_project_dir="/tmp/default")

        # Simulate active session on default thread
        fake_session = MagicMock()
        fake_session.alive = True
        clean_runner._sessions[DEFAULT_THREAD_ID] = fake_session

        update = telegram_update_factory(text="/provider claude")
        ctx = MagicMock()

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(clean_runner, "_kill_session") as mock_kill:
            await cmd_provider(update, ctx)

        # Provider SHOULD have changed (auto-kill behavior)
        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config.cli_provider == "claude"

        # Session should have been killed
        mock_kill.assert_called_with(DEFAULT_THREAD_ID)

        reply = update.message.reply_text.call_args[0][0]
        assert "Claude Code" in reply

    async def test_dead_session_allows_switch(
        self, telegram_update_factory, temp_db_path, clean_runner
    ):
        from remoclaw import cmd_provider

        await init_db(temp_db_path, default_project_dir="/tmp/default")

        # Dead session should not block
        fake_session = MagicMock()
        fake_session.alive = False
        clean_runner._sessions[DEFAULT_THREAD_ID] = fake_session

        update = telegram_update_factory(text="/provider gemini")
        ctx = MagicMock()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_provider(update, ctx)

        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config.cli_provider == "gemini"

    async def test_persists_across_restarts(
        self, telegram_update_factory, temp_db_path, clean_runner
    ):
        from remoclaw import cmd_provider

        await init_db(temp_db_path, default_project_dir="/tmp/default")
        update = telegram_update_factory(text="/provider codex")
        ctx = MagicMock()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_provider(update, ctx)

        # Simulate restart (new init_db is idempotent)
        await init_db(temp_db_path, default_project_dir="/tmp/default")

        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config.cli_provider == "codex"

    async def test_case_insensitive_provider_name(
        self, telegram_update_factory, temp_db_path, clean_runner
    ):
        from remoclaw import cmd_provider

        await init_db(temp_db_path, default_project_dir="/tmp/default")
        update = telegram_update_factory(text="/provider CLAUDE")
        ctx = MagicMock()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_provider(update, ctx)

        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config.cli_provider == "claude"


class TestHandleModelCallbackPersistence:
    """Tests that /model selection persists to SQLite."""

    async def test_model_selection_persists(
        self, telegram_update_factory, temp_db_path
    ):
        from remoclaw import handle_model_callback

        await init_db(temp_db_path, default_project_dir="/tmp/default")

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.data = "model:sonnet-4"
        update.effective_user = MagicMock()
        update.effective_user.id = 999
        update.effective_chat = MagicMock()
        update.effective_chat.id = 999
        update.message = MagicMock()
        update.message.message_thread_id = None
        update.callback_query.message = update.message

        ctx = MagicMock()
        ctx.user_data = {}

        with patch("remoclaw.DB_PATH", temp_db_path):
            await handle_model_callback(update, ctx)

        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config.model == "sonnet-4"

        # Also in user_data (backward compat)
        assert ctx.user_data["model"] == "sonnet-4"

    async def test_model_selection_without_project_binding_graceful(
        self, telegram_update_factory, temp_db_path
    ):
        """If thread has no row yet, model is stored in user_data only (no crash)."""
        from remoclaw import handle_model_callback

        # DB with schema but no default row
        async with __import__("db").get_db(temp_db_path) as conn:
            await conn.execute("""
                CREATE TABLE thread_config (
                    thread_id INTEGER PRIMARY KEY, project_dir TEXT NOT NULL,
                    cli_provider TEXT, model TEXT, timeout_seconds INTEGER,
                    last_active_at TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.data = "model:opus"
        update.effective_user = MagicMock()
        update.effective_user.id = 999
        update.effective_chat = MagicMock()
        update.effective_chat.id = 999
        update.message = MagicMock()
        update.message.message_thread_id = 42  # new thread, no row
        update.callback_query.message = update.message

        ctx = MagicMock()
        ctx.user_data = {}

        with patch("remoclaw.DB_PATH", temp_db_path):
            await handle_model_callback(update, ctx)

        # No crash, user_data set
        assert ctx.user_data["model"] == "opus"
        update.callback_query.edit_message_text.assert_called_once()
