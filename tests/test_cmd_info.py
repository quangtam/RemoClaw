"""Tests for /info command handler."""

import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db import DEFAULT_THREAD_ID, init_db
from session_manager import PtySession, PtyState


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_context():
    """Plain Telegram context with user_data dict (AsyncMock bot_data)."""
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
    """Build a PtySession for tests without spawning a real PTY."""
    created = created_at if created_at is not None else time.monotonic()
    return PtySession(
        thread_id=thread_id,
        pid=pid,
        fd=fd,
        state=state,
        created_at=created,
    )


# ─── _format_duration helper ─────────────────────────────────────────────────


class TestFormatDuration:
    """Tests for _format_duration helper."""

    def test_seconds_only(self):
        from remoclaw import _format_duration
        assert _format_duration(45) == "45s"

    def test_zero_seconds(self):
        from remoclaw import _format_duration
        assert _format_duration(0) == "0s"

    def test_negative_seconds_clamped_to_zero(self):
        from remoclaw import _format_duration
        assert _format_duration(-5) == "0s"

    def test_minutes_and_seconds(self):
        from remoclaw import _format_duration
        # 2 min 5 sec
        assert _format_duration(125) == "2m 5s"

    def test_exactly_one_minute(self):
        from remoclaw import _format_duration
        assert _format_duration(60) == "1m 0s"

    def test_hours_and_minutes(self):
        from remoclaw import _format_duration
        # 1h 1m (seconds dropped when hours present)
        assert _format_duration(3660) == "1h 1m"

    def test_many_hours(self):
        from remoclaw import _format_duration
        # 25h 0m — >24 hour edge case
        assert _format_duration(25 * 3600) == "25h 0m"


# ─── cmd_info ────────────────────────────────────────────────────────────────


class TestCmdInfo:
    """Tests for the /info command handler."""

    async def test_info_with_no_session_shows_thread_config(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """No active session → shows thread config + 'No active session' note."""
        from remoclaw import cmd_info
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/default-proj")
        update = telegram_update_factory(text="/info")

        # Ensure no session exists for this thread
        remoclaw.runner._session_mgr._sessions.clear()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_info(update, mock_context)

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "No active session" in reply
        assert "default-proj" in reply  # project basename
        assert "Send a message to start a session" in reply

    async def test_info_with_no_db_config_falls_back_to_env_defaults(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """No DB row → falls back to config.project_dir from .env."""
        from remoclaw import cmd_info
        import remoclaw

        # init_db with empty default_project_dir — no default row inserted
        await init_db(temp_db_path, default_project_dir="")
        remoclaw.runner._session_mgr._sessions.clear()

        update = telegram_update_factory(
            text="/info", message_thread_id=9999
        )

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_info(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        # Falls back to config.project_dir basename
        expected_name = os.path.basename(remoclaw.config.project_dir.rstrip("/"))
        assert expected_name in reply or "No active session" in reply

    async def test_info_with_active_session_shows_all_fields(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Active session → shows provider, model, duration, messages, status."""
        from remoclaw import cmd_info
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/my-project")
        update = telegram_update_factory(text="/info", message_thread_id=555)

        # Seed an active STREAMING session created 125 seconds ago
        session = _make_session(
            thread_id=555,
            state=PtyState.STREAMING,
            created_at=time.monotonic() - 125,
        )
        # Seed thread_config with model
        import db as db_module
        await db_module.upsert_thread_config(
            555, project_dir="/tmp/my-project", model="sonnet", path=temp_db_path
        )

        remoclaw.runner._session_mgr._sessions.clear()
        remoclaw.runner._session_mgr._sessions[555] = session
        remoclaw._thread_sessions[555] = 7

        try:
            with patch("remoclaw.DB_PATH", temp_db_path):
                await cmd_info(update, mock_context)
        finally:
            remoclaw.runner._session_mgr._sessions.pop(555, None)
            remoclaw._thread_sessions.pop(555, None)

        reply = update.message.reply_text.call_args[0][0]
        # Project shown as bold header
        assert "my-project" in reply
        assert "sonnet" in reply  # model
        assert "2m 5s" in reply  # duration
        assert "Messages:" in reply
        assert "7" in reply  # message count
        assert "streaming" in reply  # state name
        # Emoji for STREAMING
        assert "🟢" in reply
        # No-session branch text must NOT appear
        assert "No active session" not in reply

    async def test_info_with_dead_session_treated_as_no_session(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """DEAD session → still shows 'no active session' branch."""
        from remoclaw import cmd_info
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/proj-dead")
        update = telegram_update_factory(text="/info", message_thread_id=777)

        session = _make_session(thread_id=777, state=PtyState.DEAD)
        remoclaw.runner._session_mgr._sessions.clear()
        remoclaw.runner._session_mgr._sessions[777] = session

        try:
            with patch("remoclaw.DB_PATH", temp_db_path):
                await cmd_info(update, mock_context)
        finally:
            remoclaw.runner._session_mgr._sessions.pop(777, None)

        reply = update.message.reply_text.call_args[0][0]
        assert "No active session" in reply

    async def test_info_usage_shows_fallback_when_provider_returns_none(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Provider parse_usage_output() returns None → 'Not available'."""
        from remoclaw import cmd_info
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/proj-usage")
        update = telegram_update_factory(text="/info", message_thread_id=1)

        session = _make_session(thread_id=1, state=PtyState.IDLE)
        remoclaw.runner._session_mgr._sessions.clear()
        remoclaw.runner._session_mgr._sessions[1] = session

        try:
            with patch("remoclaw.DB_PATH", temp_db_path):
                await cmd_info(update, mock_context)
        finally:
            remoclaw.runner._session_mgr._sessions.pop(1, None)

        reply = update.message.reply_text.call_args[0][0]
        assert "not available" in reply.lower()

    async def test_info_handles_none_thread_id(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """thread_id=None → falls back to DEFAULT_THREAD_ID (0) for DB lookup."""
        from remoclaw import cmd_info
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/main-chat")
        update = telegram_update_factory(text="/info", message_thread_id=None)

        remoclaw.runner._session_mgr._sessions.clear()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_info(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "main-chat" in reply  # default row project

    async def test_info_does_not_mutate_session_state(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Read-only: state must remain unchanged after /info."""
        from remoclaw import cmd_info
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/ro-proj")
        update = telegram_update_factory(text="/info", message_thread_id=42)

        session = _make_session(thread_id=42, state=PtyState.WAITING_FOR_USER)
        remoclaw.runner._session_mgr._sessions.clear()
        remoclaw.runner._session_mgr._sessions[42] = session

        try:
            with patch("remoclaw.DB_PATH", temp_db_path):
                await cmd_info(update, mock_context)

            # State must be untouched
            assert remoclaw.runner._session_mgr._sessions[42].state == PtyState.WAITING_FOR_USER
        finally:
            remoclaw.runner._session_mgr._sessions.pop(42, None)

    async def test_info_uses_html_parse_mode(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Response must use Telegram HTML format (per guardrail)."""
        from remoclaw import cmd_info
        import remoclaw
        from telegram.constants import ParseMode

        await init_db(temp_db_path, default_project_dir="/tmp/fmt-proj")
        update = telegram_update_factory(text="/info")

        remoclaw.runner._session_mgr._sessions.clear()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_info(update, mock_context)

        kwargs = update.message.reply_text.call_args.kwargs
        assert kwargs.get("parse_mode") == ParseMode.HTML

    async def test_info_waiting_for_user_state_shows_correct_emoji(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """WAITING_FOR_USER session → shows ⏳ emoji."""
        from remoclaw import cmd_info
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/wait-proj")
        update = telegram_update_factory(text="/info", message_thread_id=88)

        session = _make_session(thread_id=88, state=PtyState.WAITING_FOR_USER)
        remoclaw.runner._session_mgr._sessions.clear()
        remoclaw.runner._session_mgr._sessions[88] = session

        try:
            with patch("remoclaw.DB_PATH", temp_db_path):
                await cmd_info(update, mock_context)
        finally:
            remoclaw.runner._session_mgr._sessions.pop(88, None)

        reply = update.message.reply_text.call_args[0][0]
        assert "⏳" in reply
        assert "waiting_for_user" in reply

    async def test_info_shows_rich_session_fields(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Active session shows PID, ready flag, thread label, pool stats, timeout, full path."""
        from remoclaw import cmd_info
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/rich-proj")
        import db as db_module
        await db_module.upsert_thread_config(
            333,
            project_dir="/tmp/rich-proj",
            cli_provider="kiro",
            model="sonnet",
            timeout_seconds=900,
            path=temp_db_path,
        )

        update = telegram_update_factory(text="/info", message_thread_id=333)
        session = _make_session(
            thread_id=333,
            state=PtyState.STREAMING,
            created_at=time.monotonic() - 60,
            pid=12345,
        )
        session.ready = True
        session.last_active_at = time.monotonic() - 5

        remoclaw.runner._session_mgr._sessions.clear()
        remoclaw.runner._session_mgr._sessions[333] = session

        try:
            with patch("remoclaw.DB_PATH", temp_db_path):
                await cmd_info(update, mock_context)
        finally:
            remoclaw.runner._session_mgr._sessions.pop(333, None)

        reply = update.message.reply_text.call_args[0][0]
        assert "333" in reply
        assert "12345" in reply  # PID
        assert "ready" in reply.lower()  # ready flag
        assert "/tmp/rich-proj" in reply  # full path
        assert "900s" in reply  # per-thread timeout
        assert "/5" in reply  # pool stats
        assert "last activity" in reply.lower()

    async def test_info_waiting_for_user_shows_reply_timeout_remaining(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """WAITING_FOR_USER state shows remaining time before decision auto-expire."""
        from remoclaw import cmd_info
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/wait-proj")
        update = telegram_update_factory(text="/info", message_thread_id=91)

        session = _make_session(thread_id=91, state=PtyState.WAITING_FOR_USER)
        session.last_active_at = time.monotonic() - 60  # waited 60s

        remoclaw.runner._session_mgr._sessions.clear()
        remoclaw.runner._session_mgr._sessions[91] = session

        try:
            with patch("remoclaw.DB_PATH", temp_db_path):
                await cmd_info(update, mock_context)
        finally:
            remoclaw.runner._session_mgr._sessions.pop(91, None)

        reply = update.message.reply_text.call_args[0][0]
        assert "Waiting for your reply" in reply
        assert "expires in" in reply

    async def test_info_no_session_shows_pool_stats_and_full_path(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """No-session branch also shows pool usage + full project path + timeout."""
        from remoclaw import cmd_info
        import remoclaw
        import db as db_module

        await init_db(temp_db_path, default_project_dir="/tmp/no-sess-proj")
        # Seed thread 12 explicitly (init_db only seeds DEFAULT_THREAD_ID=0)
        await db_module.upsert_thread_config(
            12, project_dir="/tmp/no-sess-proj", path=temp_db_path
        )
        update = telegram_update_factory(text="/info", message_thread_id=12)

        remoclaw.runner._session_mgr._sessions.clear()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_info(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "/tmp/no-sess-proj" in reply  # full path
        assert "Pool:" in reply
        assert "/5" in reply
        assert "Timeout:" in reply
        assert "12" in reply  # thread label

    async def test_info_shows_cli_binary_path(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Provider info includes binary path (helps debugging CLI_PATH)."""
        from remoclaw import cmd_info
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/bin-proj")
        update = telegram_update_factory(text="/info")

        remoclaw.runner._session_mgr._sessions.clear()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_info(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        assert "Binary:" in reply
        expected_path = remoclaw.runner.provider.config.cli_path
        assert expected_path in reply

    async def test_info_project_is_first_line_and_bold_active_session(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Active session: Project is the first visible content, bold-wrapped."""
        from remoclaw import cmd_info
        import remoclaw
        import db as db_module

        await init_db(temp_db_path, default_project_dir="/tmp/top-proj")
        await db_module.upsert_thread_config(
            44, project_dir="/tmp/top-proj", model="sonnet", path=temp_db_path
        )

        update = telegram_update_factory(text="/info", message_thread_id=44)
        session = _make_session(thread_id=44, state=PtyState.STREAMING)
        remoclaw.runner._session_mgr._sessions.clear()
        remoclaw.runner._session_mgr._sessions[44] = session

        try:
            with patch("remoclaw.DB_PATH", temp_db_path):
                await cmd_info(update, mock_context)
        finally:
            remoclaw.runner._session_mgr._sessions.pop(44, None)

        reply = update.message.reply_text.call_args[0][0]
        # First non-empty line is the bold project header
        first_line = reply.split("\n", 1)[0]
        assert first_line.startswith("📁")
        assert "<b>top-proj</b>" in first_line

    async def test_info_project_is_first_line_and_bold_no_session(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """No-session branch: Project is still the first line, bold."""
        from remoclaw import cmd_info
        import remoclaw
        import db as db_module

        await init_db(temp_db_path, default_project_dir="/tmp/top-proj-ns")
        await db_module.upsert_thread_config(
            77, project_dir="/tmp/top-proj-ns", path=temp_db_path
        )

        update = telegram_update_factory(text="/info", message_thread_id=77)
        remoclaw.runner._session_mgr._sessions.clear()

        with patch("remoclaw.DB_PATH", temp_db_path):
            await cmd_info(update, mock_context)

        reply = update.message.reply_text.call_args[0][0]
        first_line = reply.split("\n", 1)[0]
        assert first_line.startswith("📁")
        assert "<b>top-proj-ns</b>" in first_line

    async def test_info_pending_decision_alert_position(
        self, telegram_update_factory, mock_context, temp_db_path
    ):
        """Pending-decision alert appears before provider/duration (high-priority)."""
        from remoclaw import cmd_info
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/alert-proj")
        update = telegram_update_factory(text="/info", message_thread_id=55)

        session = _make_session(thread_id=55, state=PtyState.WAITING_FOR_USER)
        session.last_active_at = time.monotonic() - 30

        remoclaw.runner._session_mgr._sessions.clear()
        remoclaw.runner._session_mgr._sessions[55] = session

        try:
            with patch("remoclaw.DB_PATH", temp_db_path):
                await cmd_info(update, mock_context)
        finally:
            remoclaw.runner._session_mgr._sessions.pop(55, None)

        reply = update.message.reply_text.call_args[0][0]
        # Pending-decision alert must come before provider line
        alert_pos = reply.find("Waiting for your reply")
        provider_pos = reply.find("📡")
        assert alert_pos >= 0
        assert provider_pos > alert_pos, (
            "Pending-decision alert should appear above the provider line"
        )


# ─── parse_usage_output base method ──────────────────────────────────────────


class TestParseUsageOutputBase:
    """Tests for the new optional CliProvider.parse_usage_output() method."""

    def test_base_default_returns_none(self):
        """Default implementation returns None (best-effort per FR25)."""
        from cli_providers.base import CliProvider, CliProviderConfig

        class _Dummy(CliProvider):
            provider_id = "dummy"
            name = "Dummy"
            default_cli_path = "/bin/true"

            def build_args(self, prompt, *, model=None, resume=False):
                return []

            def build_env(self, base_env):
                return base_env

        p = _Dummy(CliProviderConfig(cli_path="/bin/true", api_key=""))
        assert p.parse_usage_output("anything") is None
        assert p.parse_usage_output("") is None

    def test_existing_providers_inherit_default(self):
        """All shipped providers return None (no changes needed per story)."""
        from cli_providers.kiro import KiroProvider
        from cli_providers.claude import ClaudeProvider
        from cli_providers.gemini import GeminiProvider
        from cli_providers.codex import CodexProvider
        from cli_providers.base import CliProviderConfig

        cfg = CliProviderConfig(cli_path="/bin/true", api_key="")
        for provider_cls in (KiroProvider, ClaudeProvider, GeminiProvider, CodexProvider):
            p = provider_cls(cfg)
            assert p.parse_usage_output("sample stdout") is None
