"""Shared test fixtures for RemoClaw v2.0 TDD workflow.

Provides fixtures:
- in_memory_db: Migrated SQLite :memory: for DB tests
- mock_provider: Configurable mock CliProvider
- pty_process: Real PTY with cat, force-kill cleanup
- session_context: Seeded thread_config data
- telegram_update_factory: Factory for fake Telegram Update objects
- patch_allowed_users: Auto-applied auth bypass for test user IDs
- clean_state: Auto-applied per-thread task/session/counter reset
- temp_db_path: Fresh SQLite file per test
"""

import asyncio
import os
import pty as pty_module
import signal
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import aiosqlite

# Add project root to path for imports
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cli_providers.base import CliProvider, CliProviderConfig


# ─── Auth bypass (auto-applied) ──────────────────────────────────────────────


class _ConfigShim:
    """Test-only shim that exposes a wider allowed_user_ids frozenset.

    The real Config is a frozen dataclass (immutable). We swap the whole
    remoclaw.config object for this shim during tests so the @authorized
    decorator accepts our fake test user IDs.
    """

    def __init__(self, orig):
        self._orig = orig
        self.allowed_user_ids = frozenset({123456789, 999, 888})

    def __getattr__(self, name):
        return getattr(self._orig, name)


@pytest.fixture(autouse=True)
def patch_allowed_users():
    """Allow test user IDs through the @authorized decorator. Auto-applied."""
    import remoclaw
    original = remoclaw.config
    remoclaw.config = _ConfigShim(original)
    yield
    remoclaw.config = original


# ─── Runtime state cleanup (auto-applied) ────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_state():
    """Reset per-thread task/session/counter dicts between tests.

    Auto-applied because every test that touches chati handlers can
    accidentally leak state into the next test via module-level dicts.
    """
    import remoclaw
    # Save pre-test state (defensive — shouldn't normally have any)
    remoclaw._thread_tasks.clear()
    remoclaw._thread_sessions.clear()
    remoclaw.runner._session_mgr._sessions.clear()
    yield
    # Always clean up after test
    remoclaw._thread_tasks.clear()
    remoclaw._thread_sessions.clear()
    remoclaw.runner._session_mgr._sessions.clear()


# ─── Temp SQLite path ────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path():
    """Fresh on-disk SQLite path, cleaned up after the test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test_remoclaw.db")


# ─── in_memory_db ────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def in_memory_db():
    """Provides migrated in-memory SQLite for testing."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("""
        CREATE TABLE thread_config (
            thread_id       INTEGER PRIMARY KEY,
            project_dir     TEXT NOT NULL,
            cli_provider    TEXT,
            model           TEXT,
            timeout_seconds INTEGER,
            last_active_at  TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    await db.commit()
    yield db
    await db.close()


# ─── mock_provider ───────────────────────────────────────────────────────────


class MockCliProvider(CliProvider):
    """Configurable mock CliProvider for testing."""

    provider_id = "mock"
    name = "Mock CLI"
    default_cli_path = "echo"

    def __init__(self, responses=None):
        config = CliProviderConfig(cli_path="echo", api_key="", trust_all_tools=True)
        super().__init__(config)
        self.responses = responses or ["mock response"]
        self.calls: list[tuple] = []

    def build_args(self, prompt, *, model=None, resume=False):
        self.calls.append(("build_args", prompt, model, resume))
        return ["echo", prompt]

    def build_env(self, base_env):
        return base_env.copy()


@pytest.fixture
def mock_provider():
    """Factory fixture returning configurable MockCliProvider instances."""
    def _factory(responses=None):
        return MockCliProvider(responses=responses)
    return _factory


# ─── pty_process ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def pty_process():
    """Real PTY process for integration tests. Force-kills on cleanup."""
    pid, fd = pty_module.fork()
    if pid == 0:
        # Child: run cat (echoes input back)
        os.execvp("cat", ["cat"])

    yield {"pid": pid, "fd": fd}

    # CLEANUP: Force kill within 5s — NEVER leave zombies
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    # Brief wait for graceful termination
    await asyncio.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    # Reap zombie
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


# ─── session_context ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session_context(in_memory_db):
    """Provides seeded thread_config data for session tests."""
    await in_memory_db.execute(
        "INSERT INTO thread_config (thread_id, project_dir, cli_provider, model) VALUES (?, ?, ?, ?)",
        (1001, "/home/test/project-a", "kiro", "sonnet"),
    )
    await in_memory_db.execute(
        "INSERT INTO thread_config (thread_id, project_dir) VALUES (?, ?)",
        (1002, "/home/test/project-b"),
    )
    await in_memory_db.commit()
    yield {"db": in_memory_db, "thread_ids": [1001, 1002]}


# ─── telegram_update_factory ─────────────────────────────────────────────────


@pytest.fixture
def telegram_update_factory():
    """Factory for fake Telegram Update objects.

    All async Telegram methods likely called by handlers are pre-wired as
    AsyncMock so tests can `await update.message.reply_chat_action(...)`
    or `await update.message.reply_text(...)` without extra setup.
    """
    def _create(
        user_id=123456789,
        chat_id=123456789,
        text="/start",
        message_thread_id=None,
    ):
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = user_id
        update.effective_chat = MagicMock()
        update.effective_chat.id = chat_id
        update.message = MagicMock()
        update.message.text = text
        update.message.message_thread_id = message_thread_id
        # Pre-wire all async Telegram methods handlers might call
        update.message.reply_text = AsyncMock()
        update.message.reply_chat_action = AsyncMock()
        return update

    return _create
