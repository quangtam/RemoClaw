"""Tests for /project command handler."""

import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from db import DEFAULT_THREAD_ID, get_thread_config, init_db


@pytest.fixture
def valid_project_dir():
    """Provide a real directory path for valid-path tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


class TestCmdProject:
    """Tests for /project command handler."""

    async def test_valid_path_binds_thread(
        self, telegram_update_factory, temp_db_path, valid_project_dir
    ):
        from remoclaw import cmd_project

        await init_db(temp_db_path, default_project_dir="/tmp/default")
        update = telegram_update_factory(text=f"/project {valid_project_dir}")

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_project(update, AsyncMock())

        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config is not None
        assert config.project_dir == valid_project_dir

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "✅" in reply
        assert valid_project_dir in reply

    async def test_nonexistent_path_rejected(
        self, telegram_update_factory, temp_db_path
    ):
        from remoclaw import cmd_project

        await init_db(temp_db_path, default_project_dir="/tmp/default")
        update = telegram_update_factory(text="/project /nonexistent/path/xyz")

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_project(update, AsyncMock())

        # Binding should NOT change — default row still has original path
        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config.project_dir == "/tmp/default"

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "⚠️" in reply or "not found" in reply.lower()

    async def test_path_with_spaces(
        self, telegram_update_factory, temp_db_path
    ):
        from remoclaw import cmd_project

        # Create a dir with spaces in name
        with tempfile.TemporaryDirectory() as tmpdir:
            path_with_spaces = os.path.join(tmpdir, "my project dir")
            os.makedirs(path_with_spaces)

            await init_db(temp_db_path, default_project_dir="/tmp/default")
            update = telegram_update_factory(text=f"/project {path_with_spaces}")

            with patch("remoclaw.DB_PATH", temp_db_path):
                await cmd_project(update, AsyncMock())

            config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
            assert config.project_dir == path_with_spaces

    async def test_no_path_argument(
        self, telegram_update_factory, temp_db_path
    ):
        from remoclaw import cmd_project

        await init_db(temp_db_path, default_project_dir="/tmp/default")
        update = telegram_update_factory(text="/project")

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_project(update, AsyncMock())

        # No binding change
        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config.project_dir == "/tmp/default"

        # Usage hint shown
        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "/project" in reply.lower() or "usage" in reply.lower()

    async def test_thread_id_mapping(
        self, telegram_update_factory, temp_db_path, valid_project_dir
    ):
        """Verify message_thread_id is used correctly (not defaulted to 0)."""
        from remoclaw import cmd_project

        await init_db(temp_db_path, default_project_dir="/tmp/default")
        update = telegram_update_factory(
            text=f"/project {valid_project_dir}",
            message_thread_id=42,
        )

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_project(update, AsyncMock())

        # Should write to thread 42, NOT default thread 0
        config_42 = await get_thread_config(42, path=temp_db_path)
        config_default = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)

        assert config_42 is not None
        assert config_42.project_dir == valid_project_dir
        # Default thread unchanged
        assert config_default.project_dir == "/tmp/default"

    async def test_persistence_across_restarts(
        self, telegram_update_factory, temp_db_path, valid_project_dir
    ):
        """Binding persists across DB reconnections (simulates restart)."""
        from remoclaw import cmd_project

        await init_db(temp_db_path, default_project_dir="/tmp/default")
        update = telegram_update_factory(text=f"/project {valid_project_dir}")

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_project(update, AsyncMock())

        # Simulate restart — new init_db call (idempotent)
        await init_db(temp_db_path, default_project_dir="/tmp/different")

        # Binding from first call should still be there
        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config.project_dir == valid_project_dir
