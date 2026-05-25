"""Driver — long-running task that pumps an AutoRunner until it pauses or finishes.

When the user sends /auto or /yolo, the command handler:
  1. Builds an AutoRunner + AutoExecutor
  2. Calls start_run() here, which schedules a background task
  3. The task loops `await runner.step()` and persists state after each call
  4. When the runner pauses (gate, fail), the task exits — the state remains
     in SQLite so /auto resume can pick it back up

Only one driver task per thread_id at a time, tracked in `_active_drivers`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from auto.runner import AutoRunner, RunStatus

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# thread_id → asyncio.Task running the loop
_active_drivers: dict[int, asyncio.Task] = {}


def is_driver_active(thread_id: int) -> bool:
    task = _active_drivers.get(thread_id)
    return task is not None and not task.done()


async def stop_driver(thread_id: int) -> bool:
    """Cancel the driver task for `thread_id`. Returns True if there was one."""
    task = _active_drivers.pop(thread_id, None)
    if task is None or task.done():
        return False
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    return True


async def start_run(
    runner: AutoRunner,
    *,
    persist,           # async callable: (state_row) -> None
    on_pause=None,     # async callable: (state) -> None — called when paused
    on_done=None,      # async callable: (state) -> None — called on completion
    on_error=None,     # async callable: (exc) -> None — called on unexpected exc
) -> asyncio.Task:
    """Schedule the runner loop as a background task. Returns the Task.

    If a driver is already active for this thread_id, raises RuntimeError —
    callers should /auto abort or wait first.
    """
    thread_id = runner.state.thread_id
    if is_driver_active(thread_id):
        raise RuntimeError(f"driver already active for thread_id={thread_id}")

    task = asyncio.create_task(
        _run_loop(runner, persist, on_pause, on_done, on_error),
        name=f"auto-driver-{thread_id}",
    )
    _active_drivers[thread_id] = task
    return task


async def _run_loop(
    runner: AutoRunner,
    persist,
    on_pause,
    on_done,
    on_error,
) -> None:
    """Inner loop — drives runner.step() until pause/done/abort."""
    thread_id = runner.state.thread_id
    try:
        while True:
            # Persist before each step so a crash doesn't lose progress
            runner.state.last_active_at = datetime.now(timezone.utc).isoformat()
            await persist(runner.state.to_db_row())

            status = await runner.step()

            await persist(runner.state.to_db_row())

            if status == RunStatus.DONE:
                logger.info("[auto] run done [thread=%s]", thread_id)
                if on_done:
                    await on_done(runner.state)
                return

            if status == RunStatus.ABORTED:
                logger.info("[auto] run aborted [thread=%s]", thread_id)
                return

            if status == RunStatus.PAUSED:
                logger.info("[auto] run paused [thread=%s] reason=%s",
                            thread_id, runner.state.status)
                if on_pause:
                    await on_pause(runner.state)
                return

            # status == CONTINUE — loop again. Yield to event loop briefly
            # so we don't starve other handlers.
            await asyncio.sleep(0)

    except asyncio.CancelledError:
        logger.info("[auto] driver cancelled [thread=%s]", thread_id)
        raise
    except Exception as exc:
        logger.exception("[auto] driver crashed [thread=%s]", thread_id)
        if on_error:
            try:
                await on_error(exc)
            except Exception:
                logger.exception("[auto] on_error handler also crashed")
    finally:
        # Clean up registry — but only if we're still the registered task
        current = _active_drivers.get(thread_id)
        if current is not None and current.done():
            _active_drivers.pop(thread_id, None)
