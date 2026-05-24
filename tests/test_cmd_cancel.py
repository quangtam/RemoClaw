"""Tests for /cancel command — Story 2.7 bug fix.

Verifies /cancel:
1. Cancels the running asyncio task (releases per-thread lock)
2. Kills the PTY session
3. Resets thread message counter
4. Clears pending_decision flag
"""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from session_manager import PtySession, PtyState


class TestCmdCancel:
    """Tests for /cancel handler."""

    async def test_cancel_with_no_session_and_no_task(
        self, telegram_update_factory
    ):
        """Empty state → 'No active CLI process to cancel'."""
        from remoclaw import cmd_cancel

        update = telegram_update_factory(text="/cancel", message_thread_id=1)
        ctx = AsyncMock(); ctx.user_data = {}; ctx.bot_data = {}

        await cmd_cancel(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "No active" in reply

    async def test_cancel_kills_session_and_resets_counter(
        self, telegram_update_factory
    ):
        """With session, no task → kill session + reset counter + ack."""
        from remoclaw import cmd_cancel
        import remoclaw

        update = telegram_update_factory(text="/cancel", message_thread_id=5)
        ctx = AsyncMock(); ctx.user_data = {}; ctx.bot_data = {}

        # Seed a fake session (fd=-1 keeps kill() safe)
        s = PtySession(thread_id=5, pid=999999, fd=-1, state=PtyState.STREAMING)
        remoclaw.runner._session_mgr._sessions[5] = s
        remoclaw._thread_sessions[5] = 7

        await cmd_cancel(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Cancelled" in reply
        assert 5 not in remoclaw.runner._session_mgr._sessions
        assert remoclaw._thread_sessions[5] == 0

    async def test_cancel_aborts_registered_task(
        self, telegram_update_factory
    ):
        """With running task → task is cancelled, lock released."""
        from remoclaw import cmd_cancel
        import remoclaw

        update = telegram_update_factory(text="/cancel", message_thread_id=9)
        ctx = AsyncMock(); ctx.user_data = {}; ctx.bot_data = {}

        lock = remoclaw.runner._get_lock(9)

        # Spawn a task that holds the lock (simulating a running stream)
        task_was_cancelled = asyncio.Event()

        async def fake_stream_task():
            try:
                async with lock:
                    await asyncio.sleep(10)  # would block forever
            except asyncio.CancelledError:
                task_was_cancelled.set()
                raise

        task = asyncio.create_task(fake_stream_task())
        await asyncio.sleep(0.05)  # let it grab the lock
        assert lock.locked()
        remoclaw._thread_tasks[9] = task

        # /cancel should cancel the task
        await cmd_cancel(update, ctx)

        # Give the event loop a moment to process cancellation
        try:
            await asyncio.wait_for(task_was_cancelled.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pytest.fail("Task was not cancelled by /cancel")

        # Task should be done, lock released, registry cleared
        assert task.done()
        assert not lock.locked()
        assert 9 not in remoclaw._thread_tasks

        reply = update.message.reply_text.call_args[0][0]
        assert "Cancelled" in reply

    async def test_cancel_clears_pending_decision_flag(
        self, telegram_update_factory
    ):
        """Cancel during WAITING_FOR_USER → pending_decision flag cleared."""
        from remoclaw import cmd_cancel
        import remoclaw

        update = telegram_update_factory(text="/cancel", message_thread_id=13)
        ctx = AsyncMock()
        ctx.user_data = {}
        ctx.bot_data = {"thread:13:pending_decision": True}

        s = PtySession(thread_id=13, pid=999999, fd=-1, state=PtyState.WAITING_FOR_USER)
        remoclaw.runner._session_mgr._sessions[13] = s

        await cmd_cancel(update, ctx)

        assert "thread:13:pending_decision" not in ctx.bot_data
        assert 13 not in remoclaw.runner._session_mgr._sessions

    async def test_cancel_only_affects_its_own_thread(
        self, telegram_update_factory
    ):
        """Cancelling thread A must not affect thread B's session or task."""
        from remoclaw import cmd_cancel
        import remoclaw

        update_a = telegram_update_factory(text="/cancel", message_thread_id=100)
        ctx = AsyncMock(); ctx.user_data = {}; ctx.bot_data = {}

        # Seed both threads
        s_a = PtySession(thread_id=100, pid=999997, fd=-1, state=PtyState.STREAMING)
        s_b = PtySession(thread_id=200, pid=999998, fd=-1, state=PtyState.STREAMING)
        remoclaw.runner._session_mgr._sessions[100] = s_a
        remoclaw.runner._session_mgr._sessions[200] = s_b
        remoclaw._thread_sessions[100] = 5
        remoclaw._thread_sessions[200] = 3

        # Background task for thread B (should survive)
        lock_b = remoclaw.runner._get_lock(200)
        b_cancelled = asyncio.Event()

        async def b_task():
            try:
                async with lock_b:
                    await asyncio.sleep(10)
            except asyncio.CancelledError:
                b_cancelled.set()
                raise

        task_b = asyncio.create_task(b_task())
        await asyncio.sleep(0.05)
        remoclaw._thread_tasks[200] = task_b

        # Cancel thread A
        await cmd_cancel(update_a, ctx)

        # Thread A: session gone, counter reset
        assert 100 not in remoclaw.runner._session_mgr._sessions
        assert remoclaw._thread_sessions[100] == 0

        # Thread B: still alive
        assert 200 in remoclaw.runner._session_mgr._sessions
        assert remoclaw._thread_sessions[200] == 3
        assert 200 in remoclaw._thread_tasks
        assert not task_b.done()

        # Cleanup
        task_b.cancel()
        try:
            await task_b
        except asyncio.CancelledError:
            pass

    async def test_cancel_after_task_already_done_is_safe(
        self, telegram_update_factory
    ):
        """If registered task already finished, cancel doesn't error."""
        from remoclaw import cmd_cancel
        import remoclaw

        update = telegram_update_factory(text="/cancel", message_thread_id=77)
        ctx = AsyncMock(); ctx.user_data = {}; ctx.bot_data = {}

        async def finished_task():
            return "done"

        task = asyncio.create_task(finished_task())
        await task  # ensure it's done
        remoclaw._thread_tasks[77] = task

        # Also seed a session so cancel returns the "cancelled" ack
        s = PtySession(thread_id=77, pid=999999, fd=-1, state=PtyState.IDLE)
        remoclaw.runner._session_mgr._sessions[77] = s

        await cmd_cancel(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Cancelled" in reply
        assert 77 not in remoclaw._thread_tasks  # cleaned up
