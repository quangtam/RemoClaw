"""Tests for auto.driver — background runner loop."""

import asyncio

import pytest

from auto.driver import is_driver_active, start_run, stop_driver
from auto.flow import (
    Flow,
    FlowPhase,
    FlowStep,
    GateType,
    RunMode,
)
from auto.runner import AutoRunner, StepResult
from auto.state import AutoState


class FakeExecutor:
    """Minimal executor — every skill succeeds immediately."""

    def __init__(self):
        self.calls: list[str] = []

    async def run_skill(self, *, thread_id, skill, model, new_session, prompt_override=None, timeout_seconds=None):
        self.calls.append(skill)
        return StepResult(success=True)

    async def run_builtin(self, *, thread_id, builtin, state):
        self.calls.append(f"builtin:{builtin}")
        return StepResult(success=True)

    async def ask_human_review(self, *, thread_id, step, timeout_seconds=None):
        return True

    async def run_party_mode(self, *, thread_id, context, min_rounds, timeout_seconds=None):
        from auto.party import PartyModeResult
        return PartyModeResult(rounds=min_rounds, consensus=False)

    async def ask_once(self, *, thread_id, step, prompt, timeout_seconds=None):
        return True

    async def list_pending_stories(self, *, thread_id):
        return []

    async def artifact_exists(self, *, thread_id, path):
        return False

    async def is_validation_passed(self, *, thread_id, skill):
        return False


def _make_flow(steps_def):
    steps = tuple(FlowStep(phase_id="p1", **s) for s in steps_def)
    phase = FlowPhase(id="p1", description="", steps=steps)
    return Flow(
        id="t", name="t", description="", default_model_strategy="balanced",
        phases=(phase,),
    )


@pytest.fixture
def captured_states():
    """Collects every state row passed to persist."""
    rows: list[dict] = []
    async def persist(row):
        rows.append(row)
    return rows, persist


class TestDriverHappyPath:
    @pytest.mark.asyncio
    async def test_runs_to_completion(self, captured_states):
        rows, persist = captured_states
        flow = _make_flow([
            {"id": "a", "skill": "x"},
            {"id": "b", "skill": "y"},
        ])
        state = AutoState(thread_id=10, flow_id="t", mode=RunMode.AUTO)
        ex = FakeExecutor()
        runner = AutoRunner(flow=flow, state=state, executor=ex)

        done_called = []
        async def on_done(s):
            done_called.append(s)

        task = await start_run(runner, persist=persist, on_done=on_done)
        await task

        assert ex.calls == ["x", "y"]
        assert state.status == "done"
        assert len(done_called) == 1
        # Persist called multiple times — at least once per step plus initial
        assert len(rows) >= 4

    @pytest.mark.asyncio
    async def test_pause_callback_fires(self, captured_states):
        rows, persist = captured_states
        flow = _make_flow([
            {"id": "a", "skill": "x", "gate": GateType.HUMAN_REVIEW},
            {"id": "b", "skill": "y"},
        ])
        state = AutoState(thread_id=11, flow_id="t", mode=RunMode.AUTO)
        runner = AutoRunner(flow=flow, state=state, executor=FakeExecutor())

        pause_called = []
        async def on_pause(s):
            pause_called.append(s.status)

        task = await start_run(runner, persist=persist, on_pause=on_pause)
        await task

        assert pause_called == ["paused-gate"]

    @pytest.mark.asyncio
    async def test_only_one_driver_per_thread(self, captured_states):
        rows, persist = captured_states
        flow = _make_flow([{"id": "a", "skill": "x", "gate": GateType.HUMAN_REVIEW}])
        state = AutoState(thread_id=12, flow_id="t", mode=RunMode.AUTO)
        runner = AutoRunner(flow=flow, state=state, executor=FakeExecutor())

        task1 = await start_run(runner, persist=persist)

        # Trying to start another for the same thread_id raises
        state2 = AutoState(thread_id=12, flow_id="t", mode=RunMode.AUTO)
        runner2 = AutoRunner(flow=flow, state=state2, executor=FakeExecutor())
        with pytest.raises(RuntimeError, match="already active"):
            await start_run(runner2, persist=persist)

        await task1
        # After completion, registry is cleaned up
        assert is_driver_active(12) is False


class TestStopDriver:
    @pytest.mark.asyncio
    async def test_stop_cancels_running_task(self, captured_states):
        rows, persist = captured_states

        # A flow with a step that hangs forever — to keep the driver running
        class HangingExecutor(FakeExecutor):
            async def run_skill(self, **kw):
                await asyncio.sleep(60)
                return StepResult(success=True)

        flow = _make_flow([{"id": "a", "skill": "x"}])
        state = AutoState(thread_id=20, flow_id="t", mode=RunMode.AUTO)
        runner = AutoRunner(flow=flow, state=state, executor=HangingExecutor())

        await start_run(runner, persist=persist)
        await asyncio.sleep(0.05)  # let it start
        assert is_driver_active(20) is True

        stopped = await stop_driver(20)
        assert stopped is True
        assert is_driver_active(20) is False

    @pytest.mark.asyncio
    async def test_stop_no_op_when_no_driver(self):
        stopped = await stop_driver(99999)
        assert stopped is False


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_executor_exception_is_caught_as_step_failure(self, captured_states):
        """When run_skill raises, AutoRunner converts it to a failed StepResult
        and follows the normal retry/pause-fail path. on_error is reserved for
        bugs in the driver itself, not skill failures."""
        rows, persist = captured_states

        class CrashingExecutor(FakeExecutor):
            async def run_skill(self, **kw):
                raise RuntimeError("boom")

        flow = _make_flow([{"id": "a", "skill": "x"}])
        state = AutoState(thread_id=30, flow_id="t", mode=RunMode.AUTO)
        runner = AutoRunner(flow=flow, state=state, executor=CrashingExecutor())

        async def on_pause(s):
            pass

        task = await start_run(runner, persist=persist, on_pause=on_pause)
        await task

        # 3 attempts → paused-fail
        assert state.status == "paused-fail"
        assert state.last_error is not None
