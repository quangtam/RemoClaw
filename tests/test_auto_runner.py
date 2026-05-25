"""Tests for AutoRunner — state machine, retries, gates, party-mode divert.

These tests use a FakeExecutor so we can simulate skill outcomes deterministically
without touching CLI subprocesses or Telegram.
"""

from pathlib import Path

import pytest

from auto.flow import (
    Flow,
    FlowPhase,
    FlowStep,
    GateType,
    LoopKind,
    RunMode,
    load_flow,
)
from auto.runner import AutoRunner, RunStatus, StepResult
from auto.state import (
    STATUS_ABORTED,
    STATUS_DONE,
    STATUS_PAUSED_FAIL,
    STATUS_PAUSED_GATE,
    STATUS_RUNNING,
    AutoState,
)


# ── Fake executor ────────────────────────────────────────────────────


class FakeExecutor:
    """Programmable executor for tests.

    By default every step succeeds with no findings. Override per-step via
    `outcomes[step_id]` to return a custom StepResult, or `should_fail[step_id]`
    to inject failures (each call decrements the counter; -1 = fail forever).
    """

    def __init__(self):
        self.outcomes: dict[str, StepResult] = {}
        self.skill_calls: list[dict] = []
        self.builtin_calls: list[dict] = []
        self.party_mode_calls: list[dict] = []
        self.ask_once_calls: list[dict] = []
        self.ask_once_responses: dict[str, bool] = {}  # step_id → answer

    async def run_skill(self, *, thread_id, skill, model, new_session, prompt_override=None):
        self.skill_calls.append({
            "thread_id": thread_id, "skill": skill,
            "model": model, "new_session": new_session,
        })
        # Look up outcome by skill (since steps share skill names in tests)
        # Caller can override via `outcomes['<step_id_or_skill>'] = result`
        return self.outcomes.get(skill, StepResult(success=True))

    async def run_builtin(self, *, thread_id, builtin, state):
        self.builtin_calls.append({"thread_id": thread_id, "builtin": builtin})
        return self.outcomes.get(builtin, StepResult(success=True))

    async def ask_human_review(self, *, thread_id, step):
        # Default: approve. Tests can override.
        return True

    async def run_party_mode(self, *, thread_id, context, min_rounds):
        self.party_mode_calls.append({
            "thread_id": thread_id, "context": context, "min_rounds": min_rounds,
        })
        return min_rounds

    async def ask_once(self, *, thread_id, step, prompt):
        self.ask_once_calls.append({
            "thread_id": thread_id, "step_id": step.id, "prompt": prompt,
        })
        return self.ask_once_responses.get(step.id, True)

    async def list_pending_stories(self, *, thread_id):
        return []


# ── Fixtures ─────────────────────────────────────────────────────────


def _make_flow(steps_def: list[dict]) -> Flow:
    """Build a single-phase flow from a list of step kwargs."""
    steps = tuple(
        FlowStep(phase_id="p1", **s) for s in steps_def
    )
    phase = FlowPhase(id="p1", description="", steps=steps)
    return Flow(
        id="test", name="Test", description="",
        default_model_strategy="balanced",
        phases=(phase,),
    )


def _make_state(mode: RunMode = RunMode.AUTO) -> AutoState:
    return AutoState(thread_id=1, flow_id="test", mode=mode)


@pytest.fixture
def fake_executor():
    return FakeExecutor()


# ── Tests: basic forward progression ─────────────────────────────────


class TestBasicProgression:
    @pytest.mark.asyncio
    async def test_single_step_success_advances_to_done(self, fake_executor):
        flow = _make_flow([{"id": "a", "skill": "x", "gate": GateType.AUTO}])
        runner = AutoRunner(flow, _make_state(), fake_executor)

        status = await runner.step()
        assert status == RunStatus.CONTINUE
        # Next step() finds no more steps and marks done
        status = await runner.step()
        assert status == RunStatus.DONE
        assert runner.state.status == STATUS_DONE

    @pytest.mark.asyncio
    async def test_two_steps_run_sequentially(self, fake_executor):
        flow = _make_flow([
            {"id": "a", "skill": "first"},
            {"id": "b", "skill": "second"},
        ])
        runner = AutoRunner(flow, _make_state(), fake_executor)

        await runner.step()  # runs a
        await runner.step()  # runs b
        await runner.step()  # done

        assert [c["skill"] for c in fake_executor.skill_calls] == ["first", "second"]
        assert runner.state.status == STATUS_DONE

    @pytest.mark.asyncio
    async def test_history_records_each_step(self, fake_executor):
        flow = _make_flow([
            {"id": "a", "skill": "x"},
            {"id": "b", "skill": "y"},
        ])
        runner = AutoRunner(flow, _make_state(), fake_executor)

        await runner.step()
        await runner.step()

        assert len(runner.state.history) == 2
        assert runner.state.history[0].step_id == "a"
        assert runner.state.history[1].step_id == "b"
        assert all(r.status == "success" for r in runner.state.history)


# ── Tests: gate behavior ─────────────────────────────────────────────


class TestGates:
    @pytest.mark.asyncio
    async def test_human_review_gate_pauses_in_auto_mode(self, fake_executor):
        flow = _make_flow([
            {"id": "a", "skill": "x", "gate": GateType.HUMAN_REVIEW},
            {"id": "b", "skill": "y"},
        ])
        runner = AutoRunner(flow, _make_state(RunMode.AUTO), fake_executor)

        status = await runner.step()
        assert status == RunStatus.PAUSED
        assert runner.state.status == STATUS_PAUSED_GATE
        assert runner.state.pending_gate_step_id == "a"

    @pytest.mark.asyncio
    async def test_human_review_gate_skipped_in_yolo_mode(self, fake_executor):
        flow = _make_flow([
            {"id": "a", "skill": "x", "gate": GateType.HUMAN_REVIEW},
            {"id": "b", "skill": "y"},
        ])
        runner = AutoRunner(flow, _make_state(RunMode.YOLO), fake_executor)

        status = await runner.step()
        # YOLO doesn't pause for human-review
        assert status == RunStatus.CONTINUE
        assert runner.state.status == STATUS_RUNNING

    @pytest.mark.asyncio
    async def test_resume_after_human_review_continues(self, fake_executor):
        flow = _make_flow([
            {"id": "a", "skill": "x", "gate": GateType.HUMAN_REVIEW},
            {"id": "b", "skill": "y"},
        ])
        runner = AutoRunner(flow, _make_state(RunMode.AUTO), fake_executor)

        await runner.step()
        assert runner.state.status == STATUS_PAUSED_GATE

        await runner.resume(approve=True)
        assert runner.state.status == STATUS_RUNNING

        await runner.step()
        assert fake_executor.skill_calls[-1]["skill"] == "y"

    @pytest.mark.asyncio
    async def test_auto_on_pass_pauses_only_on_findings(self, fake_executor):
        flow = _make_flow([
            {"id": "a", "skill": "review", "gate": GateType.AUTO_ON_PASS},
            {"id": "b", "skill": "next"},
        ])
        # No findings → auto continue
        fake_executor.outcomes["review"] = StepResult(success=True, findings=False)
        runner = AutoRunner(flow, _make_state(), fake_executor)
        status = await runner.step()
        assert status == RunStatus.CONTINUE

    @pytest.mark.asyncio
    async def test_auto_on_pass_pauses_on_findings(self, fake_executor):
        flow = _make_flow([
            {"id": "a", "skill": "review", "gate": GateType.AUTO_ON_PASS},
        ])
        fake_executor.outcomes["review"] = StepResult(success=True, findings=True)
        runner = AutoRunner(flow, _make_state(), fake_executor)
        status = await runner.step()
        assert status == RunStatus.PAUSED


# ── Tests: retries ───────────────────────────────────────────────────


class CountingFailExecutor(FakeExecutor):
    """Fails the first N attempts on a given skill, then succeeds."""

    def __init__(self, fail_skill: str, fail_count: int):
        super().__init__()
        self.fail_skill = fail_skill
        self.remaining_failures = fail_count

    async def run_skill(self, *, thread_id, skill, model, new_session, prompt_override=None):
        self.skill_calls.append({"skill": skill, "model": model})
        if skill == self.fail_skill and self.remaining_failures > 0:
            self.remaining_failures -= 1
            return StepResult(success=False, error="simulated failure")
        return StepResult(success=True)


class TestRetries:
    @pytest.mark.asyncio
    async def test_retry_on_failure_within_limit(self):
        flow = _make_flow([{"id": "a", "skill": "flaky", "max_retries": 3}])
        ex = CountingFailExecutor("flaky", fail_count=2)
        runner = AutoRunner(flow, _make_state(), ex)

        # 1st call → fail (attempt 1)
        await runner.step()
        assert runner.state.status == STATUS_RUNNING
        # 2nd call → fail (attempt 2)
        await runner.step()
        assert runner.state.status == STATUS_RUNNING
        # 3rd call → success
        await runner.step()
        # 4th call → done
        await runner.step()
        assert runner.state.status == STATUS_DONE
        assert len(ex.skill_calls) == 3  # 2 fails + 1 success

    @pytest.mark.asyncio
    async def test_pause_on_exhausted_retries(self):
        flow = _make_flow([{"id": "a", "skill": "broken", "max_retries": 3}])
        ex = CountingFailExecutor("broken", fail_count=999)  # always fail
        runner = AutoRunner(flow, _make_state(), ex)

        # Three attempts then pause-fail
        await runner.step()
        await runner.step()
        await runner.step()
        assert runner.state.status == STATUS_PAUSED_FAIL
        assert runner.state.last_error == "simulated failure"


# ── Tests: party-mode divert ─────────────────────────────────────────


class TestPartyModeDivert:
    @pytest.mark.asyncio
    async def test_findings_trigger_party_mode_when_configured(self, fake_executor):
        flow = _make_flow([{
            "id": "review", "skill": "code-review",
            "gate": GateType.AUTO_ON_PASS,
            "on_findings": "party-mode",
        }])
        fake_executor.outcomes["code-review"] = StepResult(
            success=True, findings=True, notes="3 issues",
        )
        runner = AutoRunner(flow, _make_state(), fake_executor)
        await runner.step()

        assert len(fake_executor.party_mode_calls) == 1
        assert fake_executor.party_mode_calls[0]["min_rounds"] == 2
        # The auto-on-pass gate still pauses afterwards
        assert runner.state.status == STATUS_PAUSED_GATE

    @pytest.mark.asyncio
    async def test_no_findings_skips_party_mode(self, fake_executor):
        flow = _make_flow([{
            "id": "review", "skill": "code-review",
            "gate": GateType.AUTO_ON_PASS,
            "on_findings": "party-mode",
        }])
        # No findings
        fake_executor.outcomes["code-review"] = StepResult(success=True, findings=False)
        runner = AutoRunner(flow, _make_state(), fake_executor)
        await runner.step()

        assert fake_executor.party_mode_calls == []


# ── Tests: optional + ask_once ───────────────────────────────────────


class TestOptionalSteps:
    @pytest.mark.asyncio
    async def test_user_declines_skips_step(self, fake_executor):
        flow = _make_flow([
            {
                "id": "ux", "skill": "create-ux", "optional": True,
                "ask_once": "Has UI?",
            },
            {"id": "next", "skill": "n"},
        ])
        fake_executor.ask_once_responses["ux"] = False  # decline
        runner = AutoRunner(flow, _make_state(), fake_executor)

        await runner.step()  # asks, declines, skips
        await runner.step()  # runs next
        await runner.step()  # done

        assert [c["skill"] for c in fake_executor.skill_calls] == ["n"]
        assert runner.state.history[0].status == "skipped"

    @pytest.mark.asyncio
    async def test_user_accepts_runs_step(self, fake_executor):
        flow = _make_flow([{
            "id": "ux", "skill": "create-ux", "optional": True,
            "ask_once": "Has UI?",
        }])
        fake_executor.ask_once_responses["ux"] = True
        runner = AutoRunner(flow, _make_state(), fake_executor)
        await runner.step()
        assert any(c["skill"] == "create-ux" for c in fake_executor.skill_calls)

    @pytest.mark.asyncio
    async def test_ask_once_only_asked_once_after_resume(self, fake_executor):
        # If we pause and resume, we shouldn't re-ask the optional question.
        flow = _make_flow([{
            "id": "ux", "skill": "create-ux", "optional": True,
            "ask_once": "Has UI?",
            "gate": GateType.HUMAN_REVIEW,
        }])
        fake_executor.ask_once_responses["ux"] = True
        runner = AutoRunner(flow, _make_state(RunMode.AUTO), fake_executor)

        await runner.step()  # asks, runs, pauses at human-review gate
        assert len(fake_executor.ask_once_calls) == 1
        assert runner.state.status == STATUS_PAUSED_GATE

        # Resume — should NOT re-prompt
        await runner.resume(approve=True)
        await runner.step()  # done
        assert len(fake_executor.ask_once_calls) == 1  # still 1


# ── Tests: condition: previous_succeeded ─────────────────────────────


class TestPreviousSucceeded:
    @pytest.mark.asyncio
    async def test_skips_when_previous_failed(self):
        flow = _make_flow([
            {"id": "a", "skill": "first", "max_retries": 1},
            {"id": "b", "skill": "second", "condition": "previous_succeeded"},
        ])
        ex = CountingFailExecutor("first", fail_count=999)
        runner = AutoRunner(flow, _make_state(), ex)

        await runner.step()  # fails — exhausts retries (max_retries=1)
        assert runner.state.status == STATUS_PAUSED_FAIL


# ── Tests: abort ─────────────────────────────────────────────────────


class TestAbort:
    @pytest.mark.asyncio
    async def test_abort_sets_status(self, fake_executor):
        flow = _make_flow([{"id": "a", "skill": "x"}])
        runner = AutoRunner(flow, _make_state(), fake_executor)
        await runner.abort()
        assert runner.state.status == STATUS_ABORTED

        status = await runner.step()
        assert status == RunStatus.ABORTED

    @pytest.mark.asyncio
    async def test_resume_with_approve_false_aborts(self, fake_executor):
        flow = _make_flow([
            {"id": "a", "skill": "x", "gate": GateType.HUMAN_REVIEW},
        ])
        runner = AutoRunner(flow, _make_state(RunMode.AUTO), fake_executor)
        await runner.step()
        assert runner.state.status == STATUS_PAUSED_GATE

        status = await runner.resume(approve=False)
        assert status == RunStatus.ABORTED
        assert runner.state.status == STATUS_ABORTED


# ── Tests: builtin steps ─────────────────────────────────────────────


class TestBuiltinSteps:
    @pytest.mark.asyncio
    async def test_builtin_invokes_run_builtin(self, fake_executor):
        flow = _make_flow([
            {"id": "summary", "builtin": "present-summary"},
        ])
        runner = AutoRunner(flow, _make_state(), fake_executor)
        await runner.step()
        assert len(fake_executor.builtin_calls) == 1
        assert fake_executor.builtin_calls[0]["builtin"] == "present-summary"
        assert fake_executor.skill_calls == []
