"""Tests for db.py — SQLite repository layer."""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
import aiosqlite

from db import (
    DEFAULT_THREAD_ID,
    ResolvedConfig,
    ThreadConfig,
    get_db,
    init_db,
    get_thread_config,
    upsert_thread_config,
    list_all_threads,
    list_distinct_project_dirs,
    resolve_thread_config,
    update_last_active,
)


@pytest.fixture
def temp_db_path():
    """Provide a temporary DB file path, cleaned up after test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test_remoclaw.db")


class TestGetDb:
    """Async context manager for SQLite connections."""

    async def test_connection_succeeds(self, temp_db_path):
        async with get_db(temp_db_path) as db:
            assert db is not None

    async def test_wal_mode_enabled(self, temp_db_path):
        async with get_db(temp_db_path) as db:
            cursor = await db.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            assert row[0].lower() == "wal"

    async def test_busy_timeout_set(self, temp_db_path):
        async with get_db(temp_db_path) as db:
            cursor = await db.execute("PRAGMA busy_timeout")
            row = await cursor.fetchone()
            assert row[0] == 5000

    async def test_row_factory_returns_dict_like(self, temp_db_path):
        async with get_db(temp_db_path) as db:
            await db.execute("CREATE TABLE t (a INTEGER, b TEXT)")
            await db.execute("INSERT INTO t VALUES (1, 'x')")
            cursor = await db.execute("SELECT * FROM t")
            row = await cursor.fetchone()
            # aiosqlite.Row supports dict-like access
            assert row["a"] == 1
            assert row["b"] == "x"

    async def test_auto_commit_on_success(self, temp_db_path):
        async with get_db(temp_db_path) as db:
            await db.execute("CREATE TABLE t (a INTEGER)")
            await db.execute("INSERT INTO t VALUES (42)")

        # Verify data persisted (new connection)
        async with get_db(temp_db_path) as db:
            cursor = await db.execute("SELECT a FROM t")
            row = await cursor.fetchone()
            assert row["a"] == 42


class TestInitDb:
    """Schema migration on startup."""

    async def test_creates_thread_config_table(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/test")
        async with get_db(temp_db_path) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='thread_config'"
            )
            row = await cursor.fetchone()
            assert row is not None

    async def test_creates_default_row(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/myproj")
        async with get_db(temp_db_path) as db:
            cursor = await db.execute(
                "SELECT project_dir FROM thread_config WHERE thread_id = ?",
                (DEFAULT_THREAD_ID,),
            )
            row = await cursor.fetchone()
            assert row["project_dir"] == "/tmp/myproj"

    async def test_idempotent_safe_to_call_multiple_times(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/a")
        # Second call should not crash
        await init_db(temp_db_path, default_project_dir="/tmp/a")

        # Should still have exactly 1 row
        async with get_db(temp_db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) as c FROM thread_config")
            row = await cursor.fetchone()
            assert row["c"] == 1

    async def test_preserves_existing_db(self, temp_db_path):
        # First init + custom data
        await init_db(temp_db_path, default_project_dir="/tmp/original")
        await upsert_thread_config(42, project_dir="/tmp/custom", path=temp_db_path)

        # Re-init should NOT destroy data
        await init_db(temp_db_path, default_project_dir="/tmp/different")

        config = await get_thread_config(42, path=temp_db_path)
        assert config is not None
        assert config.project_dir == "/tmp/custom"


class TestGetThreadConfig:
    async def test_returns_none_for_missing_thread(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        config = await get_thread_config(999, path=temp_db_path)
        assert config is None

    async def test_returns_default_thread(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert config is not None
        assert config.project_dir == "/tmp/default"
        assert config.thread_id == DEFAULT_THREAD_ID

    async def test_returns_thread_config_dataclass(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        config = await get_thread_config(DEFAULT_THREAD_ID, path=temp_db_path)
        assert isinstance(config, ThreadConfig)


class TestUpsertThreadConfig:
    async def test_insert_new_thread(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(
            100,
            project_dir="/home/user/app",
            cli_provider="claude",
            model="opus",
            path=temp_db_path,
        )
        config = await get_thread_config(100, path=temp_db_path)
        assert config.project_dir == "/home/user/app"
        assert config.cli_provider == "claude"
        assert config.model == "opus"

    async def test_update_existing_thread(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(100, project_dir="/a", cli_provider="kiro", path=temp_db_path)
        await upsert_thread_config(100, cli_provider="claude", path=temp_db_path)

        config = await get_thread_config(100, path=temp_db_path)
        # project_dir unchanged, provider updated
        assert config.project_dir == "/a"
        assert config.cli_provider == "claude"

    async def test_partial_update_preserves_other_fields(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(
            100, project_dir="/a", cli_provider="kiro", model="sonnet", timeout_seconds=600,
            path=temp_db_path,
        )
        await upsert_thread_config(100, timeout_seconds=900, path=temp_db_path)

        config = await get_thread_config(100, path=temp_db_path)
        assert config.project_dir == "/a"
        assert config.cli_provider == "kiro"
        assert config.model == "sonnet"
        assert config.timeout_seconds == 900


class TestListAllThreads:
    async def test_empty_list_for_fresh_db_except_default(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        threads = await list_all_threads(path=temp_db_path)
        # Only the default thread should exist
        assert len(threads) == 1
        assert threads[0].thread_id == DEFAULT_THREAD_ID

    async def test_returns_all_threads(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(1, project_dir="/a", path=temp_db_path)
        await upsert_thread_config(2, project_dir="/b", path=temp_db_path)
        await upsert_thread_config(3, project_dir="/c", path=temp_db_path)

        threads = await list_all_threads(path=temp_db_path)
        assert len(threads) == 4  # default + 3 custom
        thread_ids = {t.thread_id for t in threads}
        assert thread_ids == {DEFAULT_THREAD_ID, 1, 2, 3}


class TestUpdateLastActive:
    async def test_sets_timestamp(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(100, project_dir="/a", path=temp_db_path)

        # Before update: last_active_at should be None
        config = await get_thread_config(100, path=temp_db_path)
        assert config.last_active_at is None

        await update_last_active(100, path=temp_db_path)
        config = await get_thread_config(100, path=temp_db_path)
        assert config.last_active_at is not None

    async def test_noop_for_missing_thread(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        # Should not raise
        await update_last_active(99999, path=temp_db_path)


class TestConcurrentAccess:
    """Verify WAL mode + busy_timeout handles concurrent writes."""

    async def test_concurrent_writes_succeed(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")

        async def write_thread(i: int):
            await upsert_thread_config(
                i, project_dir=f"/thread/{i}", cli_provider="kiro", path=temp_db_path
            )

        # 5 concurrent writes
        await asyncio.gather(*(write_thread(i) for i in range(1, 6)))

        # Verify all writes succeeded
        threads = await list_all_threads(path=temp_db_path)
        assert len(threads) == 6  # default + 5 new
        ids = {t.thread_id for t in threads}
        assert ids == {DEFAULT_THREAD_ID, 1, 2, 3, 4, 5}

    async def test_concurrent_reads_dont_block(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(42, project_dir="/test", path=temp_db_path)

        # 10 concurrent reads
        results = await asyncio.gather(
            *(get_thread_config(42, path=temp_db_path) for _ in range(10))
        )
        assert all(r is not None and r.project_dir == "/test" for r in results)


class TestListDistinctProjectDirs:
    async def test_returns_empty_for_fresh_db(self, temp_db_path):
        # Don't call init_db — empty DB
        async with get_db(temp_db_path) as db:
            await db.execute("""
                CREATE TABLE thread_config (
                    thread_id INTEGER PRIMARY KEY, project_dir TEXT NOT NULL,
                    cli_provider TEXT, model TEXT, timeout_seconds INTEGER,
                    last_active_at TEXT, created_at TEXT, updated_at TEXT
                )
            """)
        dirs = await list_distinct_project_dirs(path=temp_db_path)
        assert dirs == []

    async def test_returns_unique_dirs(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(1, project_dir="/proj/a", path=temp_db_path)
        await upsert_thread_config(2, project_dir="/proj/b", path=temp_db_path)
        await upsert_thread_config(3, project_dir="/proj/a", path=temp_db_path)  # duplicate

        dirs = await list_distinct_project_dirs(path=temp_db_path)
        # default + 2 unique (/proj/a, /proj/b)
        assert len(dirs) == 3
        assert "/proj/a" in dirs
        assert "/proj/b" in dirs
        assert "/tmp/default" in dirs

    async def test_orders_by_most_recently_active(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(1, project_dir="/proj/old", path=temp_db_path)
        await asyncio.sleep(1.1)  # ensure different timestamps (SQLite datetime() has 1s resolution)
        await upsert_thread_config(2, project_dir="/proj/new", path=temp_db_path)
        await update_last_active(2, path=temp_db_path)

        dirs = await list_distinct_project_dirs(path=temp_db_path)
        # /proj/new should be first (most recent activity)
        assert dirs[0] == "/proj/new"


class TestResolveThreadConfig:
    """3-layer fallback chain: thread → env → provider default."""

    async def test_missing_thread_falls_back_to_env(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")

        resolved = await resolve_thread_config(
            thread_id=99999,  # no row
            env_project_dir="/env/default",
            env_cli_provider="kiro",
            env_model="sonnet",
            env_timeout_seconds=600,
            path=temp_db_path,
        )

        assert isinstance(resolved, ResolvedConfig)
        assert resolved.thread_id == 99999
        assert resolved.project_dir == "/env/default"
        assert resolved.cli_provider == "kiro"
        assert resolved.model == "sonnet"
        assert resolved.timeout_seconds == 600

    async def test_thread_override_wins_over_env(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(
            100, project_dir="/a", cli_provider="claude", model="opus",
            timeout_seconds=900, path=temp_db_path,
        )

        resolved = await resolve_thread_config(
            thread_id=100,
            env_project_dir="/env/default",
            env_cli_provider="kiro",
            env_model="sonnet",
            env_timeout_seconds=600,
            path=temp_db_path,
        )

        assert resolved.project_dir == "/a"
        assert resolved.cli_provider == "claude"
        assert resolved.model == "opus"
        assert resolved.timeout_seconds == 900

    async def test_null_fields_fall_back_to_env(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        # Row with only project_dir set; other fields NULL
        await upsert_thread_config(100, project_dir="/a", path=temp_db_path)

        resolved = await resolve_thread_config(
            thread_id=100,
            env_project_dir="/env/default",
            env_cli_provider="kiro",
            env_model="sonnet",
            env_timeout_seconds=600,
            path=temp_db_path,
        )

        assert resolved.project_dir == "/a"  # thread wins
        assert resolved.cli_provider == "kiro"  # env
        assert resolved.model == "sonnet"  # env
        assert resolved.timeout_seconds == 600  # env

    async def test_partial_thread_override(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        # Only provider set, model & timeout NULL
        await upsert_thread_config(
            100, project_dir="/a", cli_provider="claude", path=temp_db_path,
        )

        resolved = await resolve_thread_config(
            thread_id=100,
            env_project_dir="/env/default",
            env_cli_provider="kiro",
            env_model="sonnet",
            env_timeout_seconds=600,
            path=temp_db_path,
        )

        assert resolved.cli_provider == "claude"  # thread
        assert resolved.model == "sonnet"  # env
        assert resolved.timeout_seconds == 600  # env

    async def test_model_none_when_no_env_default(self, temp_db_path):
        await init_db(temp_db_path, default_project_dir="/tmp/default")
        await upsert_thread_config(100, project_dir="/a", path=temp_db_path)

        resolved = await resolve_thread_config(
            thread_id=100,
            env_project_dir="/env/default",
            env_cli_provider="kiro",
            env_model=None,  # no model default
            env_timeout_seconds=600,
            path=temp_db_path,
        )

        # Provider's build_args() will handle None model (omit --model flag)
        assert resolved.model is None

    async def test_default_thread_id(self, temp_db_path):
        """Verify the default thread row resolves correctly."""
        await init_db(temp_db_path, default_project_dir="/tmp/default")

        resolved = await resolve_thread_config(
            thread_id=DEFAULT_THREAD_ID,
            env_project_dir="/env/ignored",  # SQLite has project_dir, env ignored
            env_cli_provider="kiro",
            env_timeout_seconds=600,
            path=temp_db_path,
        )

        assert resolved.thread_id == DEFAULT_THREAD_ID
        assert resolved.project_dir == "/tmp/default"
        assert resolved.cli_provider == "kiro"
        assert resolved.timeout_seconds == 600
