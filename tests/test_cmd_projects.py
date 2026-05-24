"""Tests for /projects command handler and callback."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db import DEFAULT_THREAD_ID, get_thread_config, init_db, upsert_thread_config


def _make_context():
    """Create a fake telegram context with chat_data dict."""
    ctx = MagicMock()
    ctx.chat_data = {}
    return ctx


class TestCmdProjects:
    """Tests for /projects command."""

    async def test_empty_list_shows_hint(
        self, telegram_update_factory, temp_db_path
    ):
        from remoclaw import cmd_projects

        # Fresh DB with no projects at all — don't call init_db
        # Create schema manually to avoid default row
        import db as db_module
        async with db_module.get_db(temp_db_path) as conn:
            await conn.execute("""
                CREATE TABLE thread_config (
                    thread_id INTEGER PRIMARY KEY, project_dir TEXT NOT NULL,
                    cli_provider TEXT, model TEXT, timeout_seconds INTEGER,
                    last_active_at TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)

        update = telegram_update_factory(text="/projects")
        ctx = _make_context()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_projects(update, ctx)

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "No previous projects" in reply

    async def test_shows_inline_keyboard_with_projects(
        self, telegram_update_factory, temp_db_path
    ):
        from remoclaw import cmd_projects

        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(1, project_dir="/proj/a", path=temp_db_path)
        await upsert_thread_config(2, project_dir="/proj/b", path=temp_db_path)

        update = telegram_update_factory(text="/projects")
        ctx = _make_context()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_projects(update, ctx)

        # chat_data should store the list
        assert "_projects_list" in ctx.chat_data
        assert len(ctx.chat_data["_projects_list"]) == 3  # default + 2

        update.message.reply_text.assert_called_once()
        kwargs = update.message.reply_text.call_args.kwargs
        keyboard = kwargs["reply_markup"].inline_keyboard
        assert len(keyboard) == 3  # 3 buttons

        # Each button should have callback_data = project:<index>
        for idx, row in enumerate(keyboard):
            assert row[0].callback_data == f"project:{idx}"

    async def test_marks_current_thread_binding(
        self, telegram_update_factory, temp_db_path
    ):
        from remoclaw import cmd_projects

        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(1, project_dir="/proj/a", path=temp_db_path)
        # Bind default thread to /proj/a so it matches
        await upsert_thread_config(DEFAULT_THREAD_ID, project_dir="/proj/a", path=temp_db_path)

        update = telegram_update_factory(text="/projects")
        ctx = _make_context()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_projects(update, ctx)

        kwargs = update.message.reply_text.call_args.kwargs
        keyboard = kwargs["reply_markup"].inline_keyboard
        # The /proj/a button should have the ✓ marker
        labels = [row[0].text for row in keyboard]
        assert any(label.startswith("✓") and "/proj/a" in label for label in labels)

    async def test_long_paths_truncated_in_display(
        self, telegram_update_factory, temp_db_path
    ):
        from remoclaw import cmd_projects

        long_path = "/very/long/path/that/exceeds/sixty/characters/for/testing/display/truncation/here"
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(1, project_dir=long_path, path=temp_db_path)

        update = telegram_update_factory(text="/projects")
        ctx = _make_context()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_projects(update, ctx)

        kwargs = update.message.reply_text.call_args.kwargs
        keyboard = kwargs["reply_markup"].inline_keyboard
        # Find the button for long_path
        for row in keyboard:
            if row[0].callback_data == "project:0" or row[0].callback_data == "project:1":
                text = row[0].text
                # If this is our long path (ends with "here"), it should be truncated
                if "here" in text:
                    assert text.startswith("...")
                    assert len(text) <= 60


class TestHandleProjectsCallback:
    """Tests for project selection callback."""

    async def test_valid_selection_binds_thread(
        self, telegram_update_factory, temp_db_path
    ):
        from remoclaw import handle_projects_callback

        await init_db(temp_db_path, default_project_dir="/tmp/default")

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.data = "project:1"
        update.effective_user = MagicMock()
        update.effective_user.id = 999
        update.effective_chat = MagicMock()
        update.effective_chat.id = 999
        update.message = MagicMock()
        update.message.message_thread_id = None
        update.callback_query.message = update.message

        ctx = MagicMock()
        ctx.chat_data = {"_projects_list": ["/proj/a", "/proj/b", "/proj/c"]}

        with patch("remoclaw.DB_PATH", temp_db_path):
            await handle_projects_callback(update, ctx)

        # Should have bound default thread to /proj/b (index 1)
        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config.project_dir == "/proj/b"

        update.callback_query.edit_message_text.assert_called_once()
        reply = update.callback_query.edit_message_text.call_args[0][0]
        assert "✅" in reply
        assert "/proj/b" in reply

    async def test_invalid_index_shows_error(
        self, telegram_update_factory, temp_db_path
    ):
        from remoclaw import handle_projects_callback

        await init_db(temp_db_path, default_project_dir="/tmp/default")

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.data = "project:99"
        update.effective_user = MagicMock()
        update.effective_user.id = 999
        update.effective_chat = MagicMock()
        update.effective_chat.id = 999
        update.message = MagicMock()
        update.message.message_thread_id = None
        update.callback_query.message = update.message

        ctx = MagicMock()
        ctx.chat_data = {"_projects_list": ["/proj/a"]}  # only 1 item

        with patch("remoclaw.DB_PATH", temp_db_path):
            await handle_projects_callback(update, ctx)

        reply = update.callback_query.edit_message_text.call_args[0][0]
        assert "⚠️" in reply or "Invalid" in reply

    async def test_missing_chat_data_shows_error(
        self, telegram_update_factory, temp_db_path
    ):
        from remoclaw import handle_projects_callback

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.data = "project:0"
        update.effective_user = MagicMock()
        update.effective_user.id = 999
        update.effective_chat = MagicMock()
        update.effective_chat.id = 999
        update.message = MagicMock()
        update.message.message_thread_id = None
        update.callback_query.message = update.message

        ctx = MagicMock()
        ctx.chat_data = {}  # no _projects_list

        with patch("remoclaw.DB_PATH", temp_db_path):
            await handle_projects_callback(update, ctx)

        reply = update.callback_query.edit_message_text.call_args[0][0]
        assert "⚠️" in reply or "Invalid" in reply

    async def test_malformed_callback_data(
        self, telegram_update_factory, temp_db_path
    ):
        from remoclaw import handle_projects_callback

        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.data = "project:notanumber"
        update.effective_user = MagicMock()
        update.effective_user.id = 999
        update.effective_chat = MagicMock()
        update.effective_chat.id = 999
        update.message = MagicMock()
        update.message.message_thread_id = None
        update.callback_query.message = update.message

        ctx = MagicMock()
        ctx.chat_data = {"_projects_list": ["/proj/a"]}

        with patch("remoclaw.DB_PATH", temp_db_path):
            await handle_projects_callback(update, ctx)

        reply = update.callback_query.edit_message_text.call_args[0][0]
        assert "⚠️" in reply or "Invalid" in reply
