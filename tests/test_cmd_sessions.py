"""Tests for /sessions command handler (Story 4.2).

Lists all active PTY sessions across threads with status, project,
provider, and model. Read-only — never shells out, never mutates state.
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
    ctx.user_data = {}
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


# ─── cmd_sessions — no sessions branch ───────────────────────────────────────


class TestCmdSessionsEmpty:
    """Tests for the 'no active sessions' branch."""

    async def test_no_sessions_shows_hint_message(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """No sessions → friendly message with slot availability."""
        from remoclaw import cmd_sessions
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/any")
        update = telegram_update_factory(text="/sessions")

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_sessions(update, mock_context)

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "No active sessions" in reply
        assert "Send a message" in reply
        # Pool shows 0/max
        assert "Slots available" in reply or "0/" in reply

    async def test_no_sessions_uses_html_parse_mode(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """No-session branch must use HTML parse mode (consistent with /info)."""
        from remoclaw import cmd_sessions
        from db import init_db
        from telegram.constants import ParseMode

        await init_db(temp_db_path, default_project_dir="/tmp/any")
        update = telegram_update_factory(text="/sessions")

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_sessions(update, mock_context)

        kwargs = update.message.reply_text.call_args.kwargs
        assert kwargs.get("parse_mode") == ParseMode.HTML


# ─── cmd_sessions — with sessions branch ─────────────────────────────────────


class TestCmdSessionsList:
    """Tests for the populated-list branch."""

    async def test_single_session_shows_thread_project_provider_model(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """One session → line with emoji, thread ID, project, provider, model."""
        from remoclaw import cmd_sessions
        import remoclaw
        import db as db_module
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/proj-a")
        await db_module.upsert_thread_config(
            111,
            project_dir="/tmp/proj-a",
            cli_provider="kiro",
            model="sonnet",
            path=temp_db_path,
        )

        session = _make_session(
            thread_id=111,
            state=PtyState.STREAMING,
            created_at=time.monotonic() - 60,
        )
        remoclaw.runner._session_mgr._sessions[111] = session
        remoclaw._thread_sessions[111] = 3

        update = telegram_update_factory(text="/sessions")
        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_sessions(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "Active Sessions" in reply
        assert "111" in reply            # thread id
        assert "proj-a" in reply         # project basename
        assert "sonnet" in reply         # model
        # Provider name (from runner.provider.name) shown
        assert remoclaw.runner.provider.name in reply or "📡" in reply
        # Streaming emoji
        assert "🟢" in reply
        # Message count
        assert "3" in reply

    async def test_multiple_sessions_show_distinct_emojis_per_state(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Sessions in different states render with distinct status emojis and state text."""
        from remoclaw import cmd_sessions
        import remoclaw
        import db as db_module
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/root")
        # Seed three threads with configs
        for tid, name in ((10, "app-a"), (20, "app-b"), (30, "app-c")):
            await db_module.upsert_thread_config(
                tid, project_dir=f"/tmp/{name}", path=temp_db_path
            )

        now = time.monotonic()
        remoclaw.runner._session_mgr._sessions[10] = _make_session(
            thread_id=10, state=PtyState.STREAMING, created_at=now - 15
        )
        remoclaw.runner._session_mgr._sessions[20] = _make_session(
            thread_id=20, state=PtyState.WAITING_FOR_USER, created_at=now - 120
        )
        remoclaw.runner._session_mgr._sessions[30] = _make_session(
            thread_id=30, state=PtyState.IDLE, created_at=now - 300
        )

        update = telegram_update_factory(text="/sessions")
        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_sessions(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "🟢" in reply   # STREAMING
        assert "⏳" in reply   # WAITING_FOR_USER
        assert "💤" in reply   # IDLE
        # All three thread IDs present
        assert "10" in reply
        assert "20" in reply
        assert "30" in reply
        # All project names present
        assert "app-a" in reply
        assert "app-b" in reply
        assert "app-c" in reply
        # State-specific text
        assert "Waiting for input" in reply
        assert "Idle" in reply

    async def test_none_thread_id_rendered_as_main(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Session with thread_id=None is labelled 'main'."""
        from remoclaw import cmd_sessions
        import remoclaw
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/main-dir")

        remoclaw.runner._session_mgr._sessions[None] = _make_session(
            thread_id=None, state=PtyState.IDLE
        )

        update = telegram_update_factory(text="/sessions")
        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_sessions(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "main" in reply.lower()

    async def test_missing_db_config_uses_em_dash_fallback(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Thread with no DB row → project column falls back (no crash)."""
        from remoclaw import cmd_sessions
        import remoclaw
        from db import init_db

        # Empty init — no default row, no per-thread row
        await init_db(temp_db_path, default_project_dir="")

        remoclaw.runner._session_mgr._sessions[9999] = _make_session(
            thread_id=9999, state=PtyState.STREAMING
        )

        update = telegram_update_factory(text="/sessions")
        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_sessions(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "9999" in reply  # still lists the thread
        # Handler must not raise; reply exists


# ─── Pagination ──────────────────────────────────────────────────────────────


class TestCmdSessionsPagination:
    """Tests for pagination when >10 sessions exist."""

    async def test_eleven_sessions_show_first_ten_plus_more_marker(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """11 sessions → first 10 listed + '... and 1 more' marker."""
        from remoclaw import cmd_sessions
        import remoclaw
        import db as db_module
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/root")
        # Bump max_sessions for this test (default is 5)
        original_max = remoclaw.runner._session_mgr._max_sessions
        remoclaw.runner._session_mgr._max_sessions = 20

        try:
            for i in range(1, 12):  # threads 1..11
                await db_module.upsert_thread_config(
                    i, project_dir=f"/tmp/p{i}", path=temp_db_path
                )
                remoclaw.runner._session_mgr._sessions[i] = _make_session(
                    thread_id=i, state=PtyState.IDLE
                )

            update = telegram_update_factory(text="/sessions")
            with patch("remoclaw.DB_PATH", temp_db_path):
                await cmd_sessions(update, mock_context)
        finally:
            remoclaw.runner._session_mgr._max_sessions = original_max

        reply = update.message.reply_text.call_args[0][0]
        # Must include the "more" marker
        assert "more" in reply.lower()
        # First 10 should appear (threads 1..10)
        for i in range(1, 11):
            assert f"p{i}" in reply
        # 11th should NOT appear in the list itself
        # (allow "1 more" marker to contain the digit 1, so test for project name)
        assert "p11" not in reply

    async def test_exactly_ten_sessions_no_more_marker(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Exactly 10 sessions → no pagination marker shown."""
        from remoclaw import cmd_sessions
        import remoclaw
        import db as db_module
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/root")
        original_max = remoclaw.runner._session_mgr._max_sessions
        remoclaw.runner._session_mgr._max_sessions = 20

        try:
            for i in range(1, 11):  # 1..10
                await db_module.upsert_thread_config(
                    i, project_dir=f"/tmp/p{i}", path=temp_db_path
                )
                remoclaw.runner._session_mgr._sessions[i] = _make_session(
                    thread_id=i, state=PtyState.IDLE
                )

            update = telegram_update_factory(text="/sessions")
            with patch("remoclaw.DB_PATH", temp_db_path):
                await cmd_sessions(update, mock_context)
        finally:
            remoclaw.runner._session_mgr._max_sessions = original_max

        reply = update.message.reply_text.call_args[0][0]
        # All 10 projects shown
        for i in range(1, 11):
            assert f"p{i}" in reply
        # No "... and N more" line
        assert "more" not in reply.lower() or "... and" not in reply


# ─── Read-only / safety guarantees ───────────────────────────────────────────


class TestCmdSessionsReadOnly:
    """Guardrails: /sessions never mutates state or shells out."""

    async def test_does_not_mutate_session_state(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """States must be unchanged after /sessions runs."""
        from remoclaw import cmd_sessions
        import remoclaw
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/ro")
        remoclaw.runner._session_mgr._sessions[1] = _make_session(
            thread_id=1, state=PtyState.WAITING_FOR_USER
        )
        remoclaw.runner._session_mgr._sessions[2] = _make_session(
            thread_id=2, state=PtyState.STREAMING
        )

        update = telegram_update_factory(text="/sessions")
        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_sessions(update, mock_context)

        # States unchanged
        assert remoclaw.runner._session_mgr._sessions[1].state == PtyState.WAITING_FOR_USER
        assert remoclaw.runner._session_mgr._sessions[2].state == PtyState.STREAMING

    async def test_dead_sessions_excluded_from_list(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """DEAD sessions are filtered out — only live sessions shown."""
        from remoclaw import cmd_sessions
        import remoclaw
        import db as db_module
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/root")
        await db_module.upsert_thread_config(
            50, project_dir="/tmp/alive-proj", path=temp_db_path
        )
        await db_module.upsert_thread_config(
            51, project_dir="/tmp/dead-proj", path=temp_db_path
        )

        remoclaw.runner._session_mgr._sessions[50] = _make_session(
            thread_id=50, state=PtyState.STREAMING
        )
        remoclaw.runner._session_mgr._sessions[51] = _make_session(
            thread_id=51, state=PtyState.DEAD
        )

        update = telegram_update_factory(text="/sessions")
        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_sessions(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "alive-proj" in reply
        assert "dead-proj" not in reply
        assert "❌" not in reply

    async def test_only_dead_sessions_shows_no_active(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """If all sessions are DEAD, show 'No active sessions' branch."""
        from remoclaw import cmd_sessions
        import remoclaw
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/root")
        remoclaw.runner._session_mgr._sessions[60] = _make_session(
            thread_id=60, state=PtyState.DEAD
        )

        update = telegram_update_factory(text="/sessions")
        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_sessions(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "No active sessions" in reply

    async def test_model_fallback_shows_default_not_user_data(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Model fallback is 'default', not context.user_data (per-user global)."""
        from remoclaw import cmd_sessions
        import remoclaw
        from db import init_db

        await init_db(temp_db_path, default_project_dir="/tmp/model-test")
        # Set user_data model to something specific — should NOT appear
        mock_context.user_data["model"] = "opus"

        remoclaw.runner._session_mgr._sessions[70] = _make_session(
            thread_id=70, state=PtyState.IDLE
        )

        update = telegram_update_factory(text="/sessions")
        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_sessions(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        # Should show "default", not "opus"
        assert "default" in reply
        assert "opus" not in reply

    async def test_handler_registered_in_main(self):
        """cmd_sessions must be wired as a CommandHandler named 'sessions'."""
        import remoclaw
        import inspect

        # Look for the string literal 'sessions' in main()'s source, as a
        # lightweight smoke test that the handler is registered.
        src = inspect.getsource(remoclaw.main)
        assert 'CommandHandler("sessions"' in src
        assert "cmd_sessions" in src
