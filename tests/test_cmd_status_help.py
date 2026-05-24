"""Tests for enhanced /status and /help commands (Story 4.3).

/status: CLI health + session count + thread state + project/timeout.
/help: Complete v2 command reference in English, categorized.
"""

import os
import tempfile
import time
from unittest.mock import AsyncMock, patch

import pytest

from session_manager import PtySession, PtyState


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_context():
    """Plain Telegram context with user_data / bot_data dicts."""
    ctx = AsyncMock()
    ctx.user_data = {"model": "sonnet"}
    ctx.bot_data = {}
    return ctx


def _make_session(
    *,
    thread_id=None,
    state=PtyState.STREAMING,
    created_at=None,
    pid=99999,
    fd=99,
) -> PtySession:
    """Build a PtySession without spawning a real PTY."""
    created = created_at if created_at is not None else time.monotonic()
    return PtySession(
        thread_id=thread_id,
        pid=pid,
        fd=fd,
        state=state,
        created_at=created,
    )


@pytest.fixture(autouse=True)
def clear_session_pool():
    """Ensure a clean session pool before/after each test."""
    import remoclaw

    remoclaw.runner._session_mgr._sessions.clear()
    remoclaw._thread_sessions.clear()
    yield
    remoclaw.runner._session_mgr._sessions.clear()
    remoclaw._thread_sessions.clear()


# ─── /status tests ───────────────────────────────────────────────────────────


class TestCmdStatusEnhanced:
    """Tests for the enhanced /status command."""

    async def test_status_shows_session_count(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Response includes active session count (e.g., '2/5 active')."""
        from remoclaw import cmd_status
        import remoclaw
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/proj")
        remoclaw.runner._session_mgr._sessions[10] = _make_session(
            thread_id=10, state=PtyState.STREAMING
        )
        remoclaw.runner._session_mgr._sessions[20] = _make_session(
            thread_id=20, state=PtyState.IDLE
        )

        update = telegram_update_factory(text="/status")
        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.runner, "check_status", new_callable=AsyncMock, return_value="✅ Kiro CLI ready"):
            await cmd_status(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "2/5" in reply
        assert "active" in reply.lower()

    async def test_status_shows_thread_state_when_session_exists(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Current thread's session state is shown with emoji."""
        from remoclaw import cmd_status
        import remoclaw
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/proj")
        remoclaw.runner._session_mgr._sessions[42] = _make_session(
            thread_id=42, state=PtyState.WAITING_FOR_USER
        )

        update = telegram_update_factory(text="/status", message_thread_id=42)
        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.runner, "check_status", new_callable=AsyncMock, return_value="✅ Kiro CLI ready"):
            await cmd_status(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "⏳" in reply  # WAITING_FOR_USER emoji
        assert "waiting_for_user" in reply.lower()

    async def test_status_shows_no_session_when_none(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """No session in current thread → shows 'No session'."""
        from remoclaw import cmd_status
        import remoclaw
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/proj")

        update = telegram_update_factory(text="/status", message_thread_id=99)
        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.runner, "check_status", new_callable=AsyncMock, return_value="✅ Kiro CLI ready"):
            await cmd_status(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "No session" in reply

    async def test_status_shows_cli_not_found(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """CLI not found → shows error message."""
        from remoclaw import cmd_status
        import remoclaw
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/proj")

        update = telegram_update_factory(text="/status")
        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.runner, "check_status", new_callable=AsyncMock, return_value="❌ CLI not found: kiro-cli"):
            await cmd_status(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "❌" in reply
        assert "not found" in reply.lower()

    async def test_status_shows_project_and_timeout(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Response includes project name and timeout."""
        from remoclaw import cmd_status
        import remoclaw
        import db as db_module
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/my-app")
        await db_module.upsert_thread_config(
            55, project_dir="/tmp/my-app", timeout_seconds=900, path=temp_db_path
        )

        update = telegram_update_factory(text="/status", message_thread_id=55)
        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.runner, "check_status", new_callable=AsyncMock, return_value="✅ Kiro CLI ready"):
            await cmd_status(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "my-app" in reply
        assert "900" in reply

    async def test_status_uses_html_parse_mode(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Response uses HTML parse mode."""
        from remoclaw import cmd_status
        import remoclaw
        from db import init_db
        from telegram.constants import ParseMode

        await init_db(temp_db_path, default_project_dir="/tmp/proj")

        update = telegram_update_factory(text="/status")
        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.runner, "check_status", new_callable=AsyncMock, return_value="✅ Kiro CLI ready"):
            await cmd_status(update, mock_context)

        kwargs = update.message.reply_text.call_args.kwargs
        assert kwargs.get("parse_mode") == ParseMode.HTML

    async def test_status_shows_model(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Response includes current model."""
        from remoclaw import cmd_status
        import remoclaw
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/proj")
        mock_context.user_data["model"] = "opus"

        update = telegram_update_factory(text="/status")
        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.runner, "check_status", new_callable=AsyncMock, return_value="✅ Kiro CLI ready"):
            await cmd_status(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "opus" in reply


# ─── /help tests ─────────────────────────────────────────────────────────────


class TestCmdHelpEnhanced:
    """Tests for the rewritten /help command."""

    async def test_help_contains_all_v2_commands(
        self, telegram_update_factory, mock_context
    ):
        """All v2 commands are listed."""
        from remoclaw import cmd_help

        update = telegram_update_factory(text="/help")
        await cmd_help(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        required_commands = [
            "/project", "/projects", "/provider", "/info", "/sessions",
            "/model", "/new", "/resume", "/cancel", "/status",
            "/skills", "/help", "/start",
        ]
        for cmd in required_commands:
            assert cmd in reply, f"Missing command: {cmd}"

    async def test_help_uses_html_parse_mode(
        self, telegram_update_factory, mock_context
    ):
        """Response uses HTML parse mode."""
        from remoclaw import cmd_help
        from telegram.constants import ParseMode

        update = telegram_update_factory(text="/help")
        await cmd_help(update, mock_context)

        kwargs = update.message.reply_text.call_args.kwargs
        assert kwargs.get("parse_mode") == ParseMode.HTML

    async def test_help_shows_current_provider_and_model(
        self, telegram_update_factory, mock_context
    ):
        """Response includes current provider name and model."""
        from remoclaw import cmd_help
        import remoclaw

        mock_context.user_data["model"] = "haiku"

        update = telegram_update_factory(text="/help")
        await cmd_help(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert remoclaw.runner.provider.name in reply
        assert "haiku" in reply

    async def test_help_is_categorized(
        self, telegram_update_factory, mock_context
    ):
        """Help text is organized by category."""
        from remoclaw import cmd_help

        update = telegram_update_factory(text="/help")
        await cmd_help(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        # Check category headers exist
        assert "Session" in reply
        assert "Configuration" in reply or "Config" in reply
        assert "Status" in reply

    async def test_help_mentions_decision_prompt_flow(
        self, telegram_update_factory, mock_context
    ):
        """Help mentions the decision prompt reply flow."""
        from remoclaw import cmd_help

        update = telegram_update_factory(text="/help")
        await cmd_help(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "decision" in reply.lower() or "reply" in reply.lower()
