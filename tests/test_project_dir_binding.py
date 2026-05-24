"""Tests for per-thread project_dir binding (Story 1.6 bug fix).

Verifies that `cwd` passed to PTY/subprocess spawn respects the per-thread
project_dir binding, not just .env PROJECT_DIR.
"""

import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from db import DEFAULT_THREAD_ID, init_db, upsert_thread_config


# ─── Direct CliRunner tests: cwd propagation ────────────────────────────────


class TestExecuteStreamCwdPropagation:
    """Verify project_dir is passed through execute_stream → spawn."""

    async def test_interactive_spawn_uses_per_thread_project_dir(self):
        """execute_stream(project_dir=X) → _spawn_pty receives cwd=X."""
        from cli_runner import CliRunner

        # Capture what cwd _spawn_pty is called with
        captured: dict = {}

        def _fake_spawn(args, env, cwd):
            captured["args"] = args
            captured["cwd"] = cwd
            # Raise — we only care about the cwd that was passed
            raise RuntimeError("stop after cwd capture")

        import remoclaw
        with tempfile.TemporaryDirectory() as real_dir, patch.object(
            CliRunner, "_spawn_pty", staticmethod(_fake_spawn)
        ):
            remoclaw.runner._session_mgr._sessions.clear()
            try:
                gen = remoclaw.runner.execute_stream(
                    "hello",
                    thread_id=111,
                    project_dir=real_dir,
                )
                async for _ in gen:
                    pass
            except RuntimeError:
                pass  # expected from _fake_spawn

            assert captured.get("cwd") == real_dir

    async def test_interactive_spawn_falls_back_to_env_default(self):
        """execute_stream(project_dir=None) → cwd = config.project_dir."""
        from cli_runner import CliRunner

        captured: dict = {}

        def _fake_spawn(args, env, cwd):
            captured["cwd"] = cwd
            raise RuntimeError("stop")

        import remoclaw
        with patch.object(CliRunner, "_spawn_pty", staticmethod(_fake_spawn)):
            remoclaw.runner._session_mgr._sessions.clear()
            try:
                gen = remoclaw.runner.execute_stream(
                    "hi",
                    thread_id=222,
                    project_dir=None,
                )
                async for _ in gen:
                    pass
            except RuntimeError:
                pass

        assert captured.get("cwd") == remoclaw.runner._config.project_dir

    async def test_non_interactive_spawn_uses_per_thread_project_dir(self):
        """_stream_non_interactive honors project_dir when no interactive args."""
        from cli_runner import CliRunner

        captured: dict = {}

        async def _fake_create_subprocess_exec(*args, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            captured["args"] = args
            # Return a mock process that exits immediately with no output
            mock_proc = AsyncMock()
            mock_proc.stdout.readline = AsyncMock(return_value=b"")
            mock_proc.stderr.read = AsyncMock(return_value=b"")
            mock_proc.wait = AsyncMock(return_value=0)
            mock_proc.returncode = 0
            mock_proc.kill = lambda: None
            return mock_proc

        import remoclaw
        # Force the non-interactive path by making build_interactive_args return None
        with tempfile.TemporaryDirectory() as real_dir, patch.object(
            remoclaw.runner._provider.__class__,
            "build_interactive_args",
            lambda self, model=None: None,
        ), patch(
            "asyncio.create_subprocess_exec",
            new=_fake_create_subprocess_exec,
        ):
            remoclaw.runner._session_mgr._sessions.clear()
            gen = remoclaw.runner.execute_stream(
                "hi",
                thread_id=333,
                project_dir=real_dir,
            )
            async for _ in gen:
                pass

            assert captured.get("cwd") == real_dir

    async def test_spawn_rejected_when_cwd_does_not_exist(self):
        """Non-existent cwd → spawn returns None early (no PTY started)."""
        from cli_runner import CliRunner

        import remoclaw
        spawn_called = False

        def _fake_spawn(args, env, cwd):
            nonlocal spawn_called
            spawn_called = True
            return (99999, 99)

        with patch.object(CliRunner, "_spawn_pty", staticmethod(_fake_spawn)):
            remoclaw.runner._session_mgr._sessions.clear()
            # Path that definitely doesn't exist
            bogus = "/tmp/definitely-not-a-real-dir-" + os.urandom(4).hex()

            gen = remoclaw.runner.execute_stream(
                "hi", thread_id=444, project_dir=bogus,
            )
            async for _ in gen:
                pass

        # isdir() guard should have prevented the spawn
        assert not spawn_called, "spawn must be blocked when cwd is invalid"


# ─── End-to-end: handle_message uses resolved project_dir ───────────────────


class TestHandleMessageBindingResolved:
    """Verify _execute_and_reply_inner passes per-thread project_dir."""

    async def test_two_threads_resolve_different_project_dirs(
        self, telegram_update_factory, temp_db_path
    ):
        """Thread A and Thread B with distinct bindings → distinct resolved.project_dir."""
        from remoclaw import _execute_and_reply_inner
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/env-default")
        await upsert_thread_config(
            10, project_dir="/tmp/project-A", path=temp_db_path
        )
        await upsert_thread_config(
            20, project_dir="/tmp/project-B", path=temp_db_path
        )

        captured_cwds: list[str] = []

        async def _fake_execute_stream(*args, **kwargs):
            captured_cwds.append(kwargs.get("project_dir"))
            # Yield nothing so the handler completes
            return
            yield  # unreachable — makes this an async generator

        ctx_a = AsyncMock(); ctx_a.user_data = {}; ctx_a.bot_data = {}
        ctx_b = AsyncMock(); ctx_b.user_data = {}; ctx_b.bot_data = {}

        update_a = telegram_update_factory(text="hi A", message_thread_id=10)
        update_b = telegram_update_factory(text="hi B", message_thread_id=20)

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.runner, "execute_stream", new=_fake_execute_stream):
            await _execute_and_reply_inner(update_a, ctx_a, "hi A", 10)
            await _execute_and_reply_inner(update_b, ctx_b, "hi B", 20)

        assert captured_cwds == ["/tmp/project-A", "/tmp/project-B"], (
            f"Expected per-thread project_dirs to be resolved, got {captured_cwds}"
        )

    async def test_thread_without_binding_falls_back_to_env_default(
        self, telegram_update_factory, temp_db_path
    ):
        """Thread with no DB row → resolved.project_dir = .env default."""
        from remoclaw import _execute_and_reply_inner
        import remoclaw

        await init_db(temp_db_path, default_project_dir="/tmp/fallback-env")

        captured_cwd: list[str] = []

        async def _fake_execute_stream(*args, **kwargs):
            captured_cwd.append(kwargs.get("project_dir"))
            return
            yield

        ctx = AsyncMock(); ctx.user_data = {}; ctx.bot_data = {}
        update = telegram_update_factory(text="hi", message_thread_id=9999)

        with patch("remoclaw.DB_PATH", temp_db_path), \
             patch.object(remoclaw.runner, "execute_stream", new=_fake_execute_stream):
            await _execute_and_reply_inner(update, ctx, "hi", 9999)

        # Resolver returns .env fallback when no row exists
        assert captured_cwd == [remoclaw.config.project_dir]
