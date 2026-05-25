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
        # Sprint C: pending stories for loop expansion + artifact checks
        self.pending_stories: list[str] = []
        self.existing_artifacts: set[str] = set()
        self.validation_passed_for: set[str] = set()  # set of skill names
        self.cancel_calls: list[int] = []  # track session cancellations

    async def run_skill(
        self, *, thread_id, skill, model, new_session,
        prompt_override=None, timeout_seconds=None,
    ):
        if new_session:
            self.cancel_calls.append(thread_id)
        self.skill_calls.append({
            "thread_id": thread_id, "skill": skill,
            "model": model, "new_session": new_session,
            "timeout_seconds": timeout_seconds,
        })
        # Look up outcome by skill (since steps share skill names in tests)
        # Caller can override via `outcomes['<step_id_or_skill>'] = result`
        return self.outcomes.get(skill, StepResult(success=True))

    async def run_builtin(self, *, thread_id, builtin, state):
        self.builtin_calls.append({"thread_id": thread_id, "builtin": builtin})
        return self.outcomes.get(builtin, StepResult(success=True))

    async def ask_human_review(self, *, thread_id, step, timeout_seconds=None):
        # Default: approve. Tests can override.
        return True

    async def run_party_mode(self, *, thread_id, context, min_rounds, timeout_seconds=None):
        self.party_mode_calls.append({
            "thread_id": thread_id, "context": context, "min_rounds": min_rounds,
        })
        return min_rounds

    async def ask_once(self, *, thread_id, step, prompt, timeout_seconds=None):
        self.ask_once_calls.append({
            "thread_id": thread_id, "step_id": step.id, "prompt": prompt,
        })
        return self.ask_once_responses.get(step.id, True)

    async def list_pending_stories(self, *, thread_id):
        return list(self.pending_stories)

    async def artifact_exists(self, *, thread_id, path):
        return path in self.existing_artifacts

    async def is_validation_passed(self, *, thread_id, skill):
        return skill in self.validation_passed_for


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

    async def run_skill(self, *, thread_id, skill, model, new_session, prompt_override=None, timeout_seconds=None):
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


class TestInitialPrompt:
    """initial_prompt should be passed to the FIRST skill, then never again."""

    @pytest.mark.asyncio
    async def test_initial_prompt_passed_to_first_skill(self):
        flow = _make_flow([
            {"id": "a", "skill": "first"},
            {"id": "b", "skill": "second"},
        ])
        state = _make_state()
        state.initial_prompt = "Implement Sprint C"
        # Custom executor that captures prompt_override
        class CapturingExecutor(FakeExecutor):
            def __init__(self):
                super().__init__()
                self.prompt_overrides: list[str | None] = []

            async def run_skill(self, *, thread_id, skill, model, new_session, prompt_override=None, timeout_seconds=None):
                self.prompt_overrides.append(prompt_override)
                return await super().run_skill(
                    thread_id=thread_id, skill=skill, model=model,
                    new_session=new_session, prompt_override=prompt_override,
                )

        ex = CapturingExecutor()
        runner = AutoRunner(flow, state, ex)

        await runner.step()  # first
        await runner.step()  # second

        # First call: prompt passed; second call: no prompt
        assert ex.prompt_overrides == ["Implement Sprint C", None]

    @pytest.mark.asyncio
    async def test_no_initial_prompt_means_no_override(self, fake_executor):
        flow = _make_flow([{"id": "a", "skill": "x"}])
        state = _make_state()  # initial_prompt is None

        class CapturingExecutor(FakeExecutor):
            def __init__(self):
                super().__init__()
                self.prompt_overrides: list[str | None] = []

            async def run_skill(self, *, thread_id, skill, model, new_session, prompt_override=None, timeout_seconds=None):
                self.prompt_overrides.append(prompt_override)
                return await super().run_skill(
                    thread_id=thread_id, skill=skill, model=model,
                    new_session=new_session, prompt_override=prompt_override,
                )

        ex = CapturingExecutor()
        runner = AutoRunner(flow, state, ex)
        await runner.step()
        assert ex.prompt_overrides == [None]


class TestStepTimeouts:
    """Per-step timeout flows from FlowStep → run_skill kwargs."""

    @pytest.mark.asyncio
    async def test_step_timeout_override_used(self):
        flow = _make_flow([{"id": "a", "skill": "x", "timeout_seconds": 7200}])
        runner = AutoRunner(flow, _make_state(), FakeExecutor())
        await runner.step()
        assert runner.executor.skill_calls[0]["timeout_seconds"] == 7200

    @pytest.mark.asyncio
    async def test_flow_default_timeout_used_when_step_omits(self):
        # Step has no timeout_seconds → falls back to flow default
        flow = _make_flow([{"id": "a", "skill": "x"}])
        # Override the flow's default
        from dataclasses import replace
        flow = replace(flow, default_step_timeout_seconds=2700)
        runner = AutoRunner(flow, _make_state(), FakeExecutor())
        await runner.step()
        assert runner.executor.skill_calls[0]["timeout_seconds"] == 2700


# ── Sprint C: Per-story loop expansion ────────────────────────────────


def _make_loop_flow(*, new_session_each: bool = False) -> Flow:
    """Build a flow with a single per-story loop containing two substeps."""
    substeps = (
        FlowStep(
            id="dev", phase_id="impl", skill="bmad-dev-story",
            gate=GateType.AUTO,
        ),
        FlowStep(
            id="review", phase_id="impl", skill="bmad-code-review",
            gate=GateType.AUTO,
        ),
    )
    loop_step = FlowStep(
        id="story-loop", phase_id="impl",
        loop=LoopKind.PER_STORY,
        new_session_each=new_session_each,
        substeps=substeps,
    )
    phase = FlowPhase(id="impl", description="impl", steps=(loop_step,))
    return Flow(
        id="t", name="t", description="",
        default_model_strategy="balanced",
        phases=(phase,),
    )


class TestLoopExpansion:
    """Per-story loop expansion: snapshot stories on entry, iterate substeps
    once per story, advance past the loop when iterations are exhausted."""

    @pytest.mark.asyncio
    async def test_zero_pending_stories_skips_loop_entirely(self, fake_executor):
        flow = _make_loop_flow()
        fake_executor.pending_stories = []
        runner = AutoRunner(flow, _make_state(), fake_executor)

        # First step() should: notice empty stories, record skip on the loop
        # step, advance past it, and finish (no more steps in the phase).
        await runner.step()  # loop expansion → skipped, advances past
        await runner.step()  # nothing left, marks done

        assert fake_executor.skill_calls == []
        assert runner.state.status == STATUS_DONE
        assert any(
            r.step_id == "story-loop" and r.status == "skipped"
            for r in runner.state.history
        )

    @pytest.mark.asyncio
    async def test_one_story_runs_substeps_once(self, fake_executor):
        flow = _make_loop_flow()
        fake_executor.pending_stories = ["1-1-foo"]
        runner = AutoRunner(flow, _make_state(), fake_executor)

        await runner.step()  # expands loop (no skill call this iteration)
        await runner.step()  # dev
        await runner.step()  # review
        await runner.step()  # done

        skills = [c["skill"] for c in fake_executor.skill_calls]
        assert skills == ["bmad-dev-story", "bmad-code-review"]
        assert runner.state.status == STATUS_DONE

    @pytest.mark.asyncio
    async def test_three_stories_runs_substeps_three_times(self, fake_executor):
        flow = _make_loop_flow()
        fake_executor.pending_stories = ["a", "b", "c"]
        runner = AutoRunner(flow, _make_state(), fake_executor)

        # Pump until done
        for _ in range(20):
            status = await runner.step()
            if status == RunStatus.DONE:
                break

        skills = [c["skill"] for c in fake_executor.skill_calls]
        # Should be: dev, review, dev, review, dev, review (3 iterations × 2 substeps)
        assert skills == [
            "bmad-dev-story", "bmad-code-review",
            "bmad-dev-story", "bmad-code-review",
            "bmad-dev-story", "bmad-code-review",
        ]
        assert runner.state.status == STATUS_DONE

    @pytest.mark.asyncio
    async def test_loop_stories_persisted_in_state(self, fake_executor):
        flow = _make_loop_flow()
        fake_executor.pending_stories = ["s1", "s2"]
        runner = AutoRunner(flow, _make_state(), fake_executor)

        # First step expands the loop and snapshots stories
        await runner.step()
        assert runner.state.loop_stories == ["s1", "s2"]

        # Round-trip through SQLite serialization preserves the snapshot
        from auto.state import AutoState
        roundtripped = AutoState.from_db_row(runner.state.to_db_row())
        assert roundtripped.loop_stories == ["s1", "s2"]

    @pytest.mark.asyncio
    async def test_loop_clears_stories_after_all_iterations(self, fake_executor):
        flow = _make_loop_flow()
        fake_executor.pending_stories = ["only"]
        runner = AutoRunner(flow, _make_state(), fake_executor)

        # Pump to completion
        for _ in range(10):
            if (await runner.step()) == RunStatus.DONE:
                break

        assert runner.state.status == STATUS_DONE
        # After loop completion, snapshot is cleared so re-entry would
        # query the executor again.
        assert runner.state.loop_stories == []

    @pytest.mark.asyncio
    async def test_loop_iter_resets_substep_idx_each_iteration(self, fake_executor):
        flow = _make_loop_flow()
        fake_executor.pending_stories = ["s1", "s2"]
        runner = AutoRunner(flow, _make_state(), fake_executor)

        # First step expands the loop AND runs iter0/dev.
        # Second step runs iter0/review and advances to iter1/substep0.
        await runner.step()  # expand + iter0 dev
        await runner.step()  # iter0 review → finishes iter, bumps to iter1
        # At this point we should be at iter=1, substep_idx=0
        assert runner.state.current_loop_iter == 1
        assert runner.state.current_substep_idx == 0

    @pytest.mark.asyncio
    async def test_new_session_each_cancels_between_iterations(self, fake_executor):
        flow = _make_loop_flow(new_session_each=True)
        fake_executor.pending_stories = ["s1", "s2"]
        runner = AutoRunner(flow, _make_state(), fake_executor)

        # Pump to completion
        for _ in range(20):
            if (await runner.step()) == RunStatus.DONE:
                break

        # Each iteration's first substep (dev) should have been called with
        # new_session=True. The second substep (review) keeps the session.
        new_session_per_call = [c["new_session"] for c in fake_executor.skill_calls]
        assert new_session_per_call == [True, False, True, False]
        # Cancel was called exactly twice — once before each iteration.
        assert fake_executor.cancel_calls == [1, 1]

    @pytest.mark.asyncio
    async def test_no_new_session_each_keeps_single_session(self, fake_executor):
        flow = _make_loop_flow(new_session_each=False)
        fake_executor.pending_stories = ["s1", "s2"]
        runner = AutoRunner(flow, _make_state(), fake_executor)

        for _ in range(20):
            if (await runner.step()) == RunStatus.DONE:
                break

        # Without new_session_each, no substep gets a fresh session
        new_session_per_call = [c["new_session"] for c in fake_executor.skill_calls]
        assert new_session_per_call == [False, False, False, False]
        assert fake_executor.cancel_calls == []


# ── Sprint C: skip_if_validated ───────────────────────────────────────


class TestSkipIfValidated:
    @pytest.mark.asyncio
    async def test_skips_step_when_validation_passed(self, fake_executor):
        flow = _make_flow([
            {
                "id": "validate", "skill": "bmad-validate-prd",
                "skip_if_validated": True,
            },
            {"id": "next", "skill": "after"},
        ])
        fake_executor.validation_passed_for.add("bmad-validate-prd")

        runner = AutoRunner(flow, _make_state(), fake_executor)
        await runner.step()  # validate → skipped silently
        await runner.step()  # next → runs
        await runner.step()  # done

        skills = [c["skill"] for c in fake_executor.skill_calls]
        assert skills == ["after"]
        assert runner.state.history[0].status == "skipped"
        assert runner.state.history[0].step_id == "validate"
        assert "validation already passed" in (runner.state.history[0].notes or "")

    @pytest.mark.asyncio
    async def test_runs_step_when_validation_not_passed(self, fake_executor):
        flow = _make_flow([{
            "id": "validate", "skill": "bmad-validate-prd",
            "skip_if_validated": True,
        }])
        # validation_passed_for is empty by default → skill should run
        runner = AutoRunner(flow, _make_state(), fake_executor)
        await runner.step()
        skills = [c["skill"] for c in fake_executor.skill_calls]
        assert skills == ["bmad-validate-prd"]

    @pytest.mark.asyncio
    async def test_validation_check_crash_falls_back_to_running(self, fake_executor):
        """If is_validation_passed raises, we err on the side of running
        the validator (safer than skipping a needed check)."""
        flow = _make_flow([{
            "id": "validate", "skill": "bmad-validate-prd",
            "skip_if_validated": True,
        }])

        class CrashingExecutor(FakeExecutor):
            async def is_validation_passed(self, *, thread_id, skill):
                raise RuntimeError("disk read failed")

        ex = CrashingExecutor()
        runner = AutoRunner(flow, _make_state(), ex)
        await runner.step()
        assert [c["skill"] for c in ex.skill_calls] == ["bmad-validate-prd"]


# ── Sprint C: skip_if_artifact ────────────────────────────────────────


class TestSkipIfArtifact:
    @pytest.mark.asyncio
    async def test_yolo_skips_silently_when_artifact_exists(self, fake_executor):
        flow = _make_flow([{
            "id": "prd", "skill": "bmad-create-prd",
            "skip_if_artifact": "docs/planning-artifacts/prd.md",
            "ask_once": "Recreate?",
        }])
        fake_executor.existing_artifacts.add("docs/planning-artifacts/prd.md")

        runner = AutoRunner(flow, _make_state(RunMode.YOLO), fake_executor)
        await runner.step()

        assert fake_executor.skill_calls == []
        # No ask_once asked in yolo mode
        assert fake_executor.ask_once_calls == []
        assert runner.state.history[0].status == "skipped"

    @pytest.mark.asyncio
    async def test_auto_asks_user_when_artifact_exists(self, fake_executor):
        flow = _make_flow([{
            "id": "prd", "skill": "bmad-create-prd",
            "skip_if_artifact": "docs/planning-artifacts/prd.md",
            "ask_once": "PRD already exists. Recreate?",
        }])
        fake_executor.existing_artifacts.add("docs/planning-artifacts/prd.md")
        fake_executor.ask_once_responses["prd"] = False  # user says skip

        runner = AutoRunner(flow, _make_state(RunMode.AUTO), fake_executor)
        await runner.step()

        # User was asked exactly once
        assert len(fake_executor.ask_once_calls) == 1
        assert fake_executor.ask_once_calls[0]["step_id"] == "prd"
        assert "Recreate" in fake_executor.ask_once_calls[0]["prompt"]
        # And the skill was NOT run
        assert fake_executor.skill_calls == []
        assert runner.state.history[0].status == "skipped"

    @pytest.mark.asyncio
    async def test_auto_user_says_recreate_runs_step(self, fake_executor):
        flow = _make_flow([{
            "id": "prd", "skill": "bmad-create-prd",
            "skip_if_artifact": "docs/planning-artifacts/prd.md",
            "ask_once": "Recreate?",
        }])
        fake_executor.existing_artifacts.add("docs/planning-artifacts/prd.md")
        fake_executor.ask_once_responses["prd"] = True  # yes, recreate

        runner = AutoRunner(flow, _make_state(RunMode.AUTO), fake_executor)
        await runner.step()

        assert len(fake_executor.skill_calls) == 1
        assert fake_executor.skill_calls[0]["skill"] == "bmad-create-prd"

    @pytest.mark.asyncio
    async def test_no_artifact_runs_step_without_asking(self, fake_executor):
        flow = _make_flow([{
            "id": "prd", "skill": "bmad-create-prd",
            "skip_if_artifact": "docs/planning-artifacts/prd.md",
            "ask_once": "Recreate?",
        }])
        # existing_artifacts is empty — artifact doesn't exist

        runner = AutoRunner(flow, _make_state(RunMode.AUTO), fake_executor)
        await runner.step()

        # No prompt, skill ran
        assert fake_executor.ask_once_calls == []
        assert len(fake_executor.skill_calls) == 1

    @pytest.mark.asyncio
    async def test_auto_no_ask_once_skips_silently(self, fake_executor):
        """In auto mode without ask_once, skip_if_artifact still skips
        silently — same as yolo."""
        flow = _make_flow([{
            "id": "prd", "skill": "bmad-create-prd",
            "skip_if_artifact": "docs/planning-artifacts/prd.md",
        }])
        fake_executor.existing_artifacts.add("docs/planning-artifacts/prd.md")

        runner = AutoRunner(flow, _make_state(RunMode.AUTO), fake_executor)
        await runner.step()

        assert fake_executor.skill_calls == []
        assert fake_executor.ask_once_calls == []
        assert runner.state.history[0].status == "skipped"


# ── Sprint C: phase-aware previous_succeeded ──────────────────────────


class TestPreviousSucceededPhaseAware:
    """The dependency check should look up the immediately preceding step
    in the *current phase*, not just the last record in history."""

    @pytest.mark.asyncio
    async def test_first_step_in_phase_runs_when_history_is_unclean(self, fake_executor):
        """The phase-aware predecessor lookup should not be confused by
        records from earlier phases. The first step of a new phase has
        no predecessor in that phase, so condition is treated as satisfied.
        """
        from auto.flow import Flow, FlowPhase
        # p1: a is optional and user declines → skipped record ends up last
        steps_p1 = (
            FlowStep(
                id="a", phase_id="p1", skill="skip-me",
                optional=True, ask_once="?",
            ),
        )
        # p2: b is first step in p2, has condition: previous_succeeded.
        # The OLD impl would inspect history's last record (the "skipped"
        # for `a`) and refuse to run b. The NEW impl recognizes b has no
        # predecessor in p2 and runs.
        steps_p2 = (
            FlowStep(
                id="b", phase_id="p2", skill="should-run",
                condition="previous_succeeded",
            ),
        )
        flow = Flow(
            id="t", name="t", description="",
            default_model_strategy="balanced",
            phases=(
                FlowPhase(id="p1", description="", steps=steps_p1),
                FlowPhase(id="p2", description="", steps=steps_p2),
            ),
        )
        fake_executor.ask_once_responses["a"] = False  # decline → skipped

        runner = AutoRunner(flow, _make_state(), fake_executor)
        await runner.step()  # a → skipped (optional declined)
        await runner.step()  # b → should run despite p1's "skipped" record

        assert any(c["skill"] == "should-run" for c in fake_executor.skill_calls)

    @pytest.mark.asyncio
    async def test_skipped_predecessor_does_not_block_next(self, fake_executor):
        # If the predecessor was deliberately skipped (optional declined),
        # the dependent step should still run.
        flow = _make_flow([
            {
                "id": "ux", "skill": "create-ux",
                "optional": True, "ask_once": "Has UI?",
            },
            {
                "id": "next", "skill": "after",
                "condition": "previous_succeeded",
            },
        ])
        fake_executor.ask_once_responses["ux"] = False  # decline → skipped
        runner = AutoRunner(flow, _make_state(), fake_executor)

        await runner.step()  # ux → skipped
        await runner.step()  # next → should still run despite skipped predecessor

        assert any(c["skill"] == "after" for c in fake_executor.skill_calls)

    @pytest.mark.asyncio
    async def test_failed_predecessor_blocks_next(self):
        # When the immediately preceding step failed, condition skips us.
        flow = _make_flow([
            {"id": "a", "skill": "first", "max_retries": 1},
            {
                "id": "b", "skill": "second",
                "condition": "previous_succeeded",
            },
        ])
        ex = CountingFailExecutor("first", fail_count=999)
        runner = AutoRunner(flow, _make_state(), ex)

        await runner.step()  # first fails → paused-fail
        assert runner.state.status == STATUS_PAUSED_FAIL

    @pytest.mark.asyncio
    async def test_succeeded_predecessor_lets_next_run(self, fake_executor):
        flow = _make_flow([
            {"id": "a", "skill": "first"},
            {
                "id": "b", "skill": "second",
                "condition": "previous_succeeded",
            },
        ])
        runner = AutoRunner(flow, _make_state(), fake_executor)
        await runner.step()  # a → success
        await runner.step()  # b → should run
        assert [c["skill"] for c in fake_executor.skill_calls] == ["first", "second"]
