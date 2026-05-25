"""AutoRunner — the orchestrator that walks a flow step-by-step.

This is the brain of autonomous mode. It owns no I/O directly — instead,
it asks an `Executor` (injected) to actually run skills, ask the user,
spawn party-mode, etc. That keeps the runner pure-Python and easily
testable, while the Telegram-flavored Executor lives in remoclaw.py.

State machine:
    STATUS_RUNNING ──► step.execute() ──┬─► success ─► advance() ─► RUNNING
                                        ├─► findings ─► party-mode ─► RUNNING
                                        ├─► failed (retry left) ─► step.execute()
                                        ├─► failed (no retry)   ─► PAUSED_FAIL
                                        └─► gate human-review   ─► PAUSED_GATE
                                              │
                                              └─► resume() ─► RUNNING

Persistence is the caller's responsibility — after every state mutation
the caller should `db.upsert_auto_run(state)`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from auto.flow import Flow, FlowStep, GateType, RunMode
from auto.state import (
    STATUS_ABORTED,
    STATUS_DONE,
    STATUS_PAUSED_FAIL,
    STATUS_PAUSED_GATE,
    STATUS_PAUSED_PARTY,
    STATUS_RUNNING,
    AutoState,
    StepRecord,
)

logger = logging.getLogger(__name__)


class RunStatus(str, Enum):
    """High-level status returned from `step()` so the caller knows what to do."""

    CONTINUE = "continue"   # runner advanced, ready for next step()
    PAUSED = "paused"       # waiting for user input (gate or fail)
    DONE = "done"           # flow complete
    ABORTED = "aborted"     # user cancelled


# ── Executor protocol ────────────────────────────────────────────────


class StepResult:
    """Outcome of executing a single step.

    Attributes:
        success: True if the skill ran without unhandled error.
        findings: True if the output contains review findings (issues/blockers).
        notes: Short summary for /auto status display.
        error: Error message if `not success`.
    """

    def __init__(
        self,
        *,
        success: bool,
        findings: bool = False,
        notes: str = "",
        error: str | None = None,
    ) -> None:
        self.success = success
        self.findings = findings
        self.notes = notes
        self.error = error


class Executor(Protocol):
    """What AutoRunner needs from the outside world.

    Implemented by remoclaw.AutoExecutor (Telegram-aware) for production
    and by tests with a fake.
    """

    async def run_skill(
        self, *, thread_id: int, skill: str, model: str | None,
        new_session: bool, prompt_override: str | None = None,
        timeout_seconds: int | None = None,
    ) -> StepResult:
        """Execute a BMAD skill and capture its outcome."""

    async def run_builtin(
        self, *, thread_id: int, builtin: str, state: AutoState,
    ) -> StepResult:
        """Execute a runner-internal handler (e.g. 'present-summary')."""

    async def ask_human_review(
        self, *, thread_id: int, step: FlowStep,
        timeout_seconds: int | None = None,
    ) -> bool:
        """Pause and ask the user to approve. Returns True to continue,
        False to abort. In /yolo mode this is bypassed by the runner."""

    async def run_party_mode(
        self, *, thread_id: int, context: str, min_rounds: int,
        timeout_seconds: int | None = None,
    ) -> int:
        """Run party-mode discussion. Returns the number of rounds executed."""

    async def ask_once(
        self, *, thread_id: int, step: FlowStep, prompt: str,
        timeout_seconds: int | None = None,
    ) -> bool:
        """Ask the user a yes/no question (used by `optional` + `ask_once`)."""

    async def list_pending_stories(self, *, thread_id: int) -> list[str]:
        """Return story ids that need implementing — for loop expansion.

        Reads the project's sprint-status.yaml (or equivalent) and returns
        the ids of stories not yet in a terminal state (e.g. 'backlog' or
        'ready-for-dev'). Returning an empty list causes the runner to
        skip the surrounding loop entirely.
        """

    async def artifact_exists(
        self, *, thread_id: int, path: str,
    ) -> bool:
        """Whether `path` (relative to thread's project_dir) already exists.

        Used by `skip_if_artifact` to avoid regenerating files that are
        already present (e.g. a PRD checked into the repo).
        """

    async def is_validation_passed(
        self, *, thread_id: int, skill: str,
    ) -> bool:
        """Whether `skill`'s output has already been validated previously.

        Used by `skip_if_validated`. Implementation is skill-specific —
        e.g. for `bmad-validate-prd`, look for a validation report with
        `validationStatus: COMPLETE`.
        """


# ── Runner ───────────────────────────────────────────────────────────


@dataclass
class AutoRunner:
    """Drive a flow forward, one step at a time."""

    flow: Flow
    state: AutoState
    executor: Executor

    async def step(self) -> RunStatus:
        """Execute the current step and update state.

        The caller persists state after each call.
        """
        if self.state.status == STATUS_ABORTED:
            return RunStatus.ABORTED
        if self.state.status == STATUS_DONE:
            return RunStatus.DONE

        # If we're at a paused gate and the caller called step() again,
        # treat it as approval and clear the pause.
        if self.state.status == STATUS_PAUSED_GATE:
            self.state.status = STATUS_RUNNING
            self.state.pending_gate_step_id = None

        # ── Loop expansion (Sprint C) ────────────────────────────
        # If the cursor is sitting on a per-story loop step and we
        # haven't snapshotted the story list yet, ask the executor
        # which stories are pending and freeze them on state. If
        # there are zero pending stories, skip the entire loop.
        loop_action = await self._maybe_expand_loop()
        if loop_action == "skipped":
            return RunStatus.CONTINUE

        current = self._current_step()
        if current is None:
            self.state.status = STATUS_DONE
            return RunStatus.DONE

        # ── Skip if validation already passed (Sprint C) ─────────
        # Cheaper than running the validator skill again.
        if current.skip_if_validated and current.skill:
            try:
                already_validated = await self.executor.is_validation_passed(
                    thread_id=self.state.thread_id,
                    skill=current.skill,
                )
            except Exception as exc:
                # Defensive: if the heuristic crashes, prefer to run the
                # skill — that's the safe default for "validation needed".
                logger.warning(
                    "[auto] is_validation_passed crashed for %s: %s",
                    current.skill, exc,
                )
                already_validated = False
            if already_validated:
                self._record(current, status="skipped", notes="validation already passed")
                self._advance()
                return RunStatus.CONTINUE

        # ── Skip if artifact already exists (Sprint C) ───────────
        # In yolo mode this is silent. In auto mode, if the step also
        # has `ask_once`, we ask the user whether to regenerate (yes)
        # or keep the existing file (no/skip). Without `ask_once`,
        # we just skip silently — same behavior as yolo.
        if current.skip_if_artifact:
            try:
                exists = await self.executor.artifact_exists(
                    thread_id=self.state.thread_id,
                    path=current.skip_if_artifact,
                )
            except Exception as exc:
                logger.warning(
                    "[auto] artifact_exists crashed for %s: %s",
                    current.skip_if_artifact, exc,
                )
                exists = False
            if exists:
                if (
                    self.state.mode != RunMode.YOLO
                    and current.ask_once
                    and current.id not in self.state.ask_once_asked
                ):
                    self.state.ask_once_asked.add(current.id)
                    wants_regen = await self.executor.ask_once(
                        thread_id=self.state.thread_id,
                        step=current,
                        prompt=current.ask_once,
                        timeout_seconds=self.flow.default_decision_timeout_seconds,
                    )
                    if not wants_regen:
                        self._record(
                            current, status="skipped",
                            notes=f"artifact exists at {current.skip_if_artifact}",
                        )
                        self._advance()
                        return RunStatus.CONTINUE
                    # User said yes → fall through and run the step
                else:
                    # YOLO or no ask_once → skip silently
                    self._record(
                        current, status="skipped",
                        notes=f"artifact exists at {current.skip_if_artifact}",
                    )
                    self._advance()
                    return RunStatus.CONTINUE

        # ── Optional + ask_once ──────────────────────────────────
        if current.optional and current.ask_once and current.id not in self.state.ask_once_asked:
            self.state.ask_once_asked.add(current.id)
            wants = await self.executor.ask_once(
                thread_id=self.state.thread_id,
                step=current,
                prompt=current.ask_once,
                timeout_seconds=self.flow.default_decision_timeout_seconds,
            )
            if not wants:
                self._record(current, status="skipped", notes="user declined")
                self._advance()
                return RunStatus.CONTINUE

        # ── Condition: previous_succeeded ─────────────────────────
        if current.condition == "previous_succeeded" and not self._prev_succeeded():
            self._record(current, status="skipped", notes="previous step failed")
            self._advance()
            return RunStatus.CONTINUE

        # ── Execute ──────────────────────────────────────────────
        try:
            result = await self._execute_step(current)
        except Exception as exc:
            # Treat any unhandled exception in the executor as a step failure
            # so the retry/pause-fail flow handles it consistently.
            logger.exception("[auto] executor raised on step %s", current.id)
            result = StepResult(success=False, error=str(exc))

        # ── Failure handling ─────────────────────────────────────
        if not result.success:
            attempts = self.state.attempts_for(current.id) + 1
            if attempts < current.max_retries:
                self._record(
                    current, status="failed",
                    attempts=attempts, notes=f"retry {attempts}/{current.max_retries}: {result.error}",
                )
                # Don't advance — next step() will retry the same step
                return RunStatus.CONTINUE

            self._record(
                current, status="failed",
                attempts=attempts, notes=f"exhausted retries: {result.error}",
            )
            self.state.status = STATUS_PAUSED_FAIL
            self.state.last_error = result.error or "unknown error"
            return RunStatus.PAUSED

        # ── Findings → party-mode divert ─────────────────────────
        rounds = 0
        if result.findings and current.on_findings == "party-mode":
            min_rounds = 2
            rounds = await self.executor.run_party_mode(
                thread_id=self.state.thread_id,
                context=result.notes or f"findings from {current.id}",
                min_rounds=min_rounds,
                timeout_seconds=self.flow.default_step_timeout_seconds,
            )

        # ── Success — record and apply gate ──────────────────────
        self._record(
            current, status="success",
            findings=result.findings, party_mode_rounds=rounds,
            notes=result.notes,
        )

        gate_action = self._evaluate_gate(current, result)
        if gate_action == "pause":
            self.state.status = STATUS_PAUSED_GATE
            self.state.pending_gate_step_id = current.id
            # In auto mode, ask via executor; caller decides when to resume()
            return RunStatus.PAUSED

        # gate_action == "continue" — auto-approve
        self._advance()
        return RunStatus.CONTINUE

    async def resume(self, *, approve: bool = True) -> RunStatus:
        """Called when user approves a paused gate (or aborts).

        After approve=True the next step() call will pick up where we
        left off. approve=False aborts the run.
        """
        if not approve:
            self.state.status = STATUS_ABORTED
            return RunStatus.ABORTED

        if self.state.status in (STATUS_PAUSED_GATE, STATUS_PAUSED_PARTY):
            self.state.status = STATUS_RUNNING
            self.state.pending_gate_step_id = None
            self._advance()
            return RunStatus.CONTINUE
        if self.state.status == STATUS_PAUSED_FAIL:
            # User chose to retry past the retry cap
            self.state.status = STATUS_RUNNING
            self.state.last_error = None
            return RunStatus.CONTINUE
        return RunStatus.CONTINUE

    async def abort(self) -> RunStatus:
        """User cancelled the run."""
        self.state.status = STATUS_ABORTED
        return RunStatus.ABORTED

    # ── Internals ────────────────────────────────────────────────

    async def _execute_step(self, step: FlowStep) -> StepResult:
        """Dispatch to skill/builtin executor."""
        # Resolve effective timeouts: step override → flow default
        step_timeout = step.timeout_seconds or self.flow.default_step_timeout_seconds

        if step.builtin:
            return await self.executor.run_builtin(
                thread_id=self.state.thread_id,
                builtin=step.builtin,
                state=self.state,
            )

        # Determine if we need a fresh session.
        # The substep itself may declare new_session; otherwise, if it sits
        # inside a per-story loop with `new_session_each: true`, the FIRST
        # substep of every iteration gets a fresh session — so each story
        # starts from a clean CLI context.
        # Also: flow.default_new_session forces fresh session on every skill.
        new_session = step.new_session or self.flow.default_new_session
        parent_loop = self._parent_loop_step()
        if (
            parent_loop is not None
            and parent_loop.new_session_each
            and self.state.current_substep_idx == 0
        ):
            new_session = True

        # Pass the user's initial intent on the FIRST skill invocation only.
        # Subsequent steps work off the artifacts the first step produced
        # (PRD, story file, etc.) so they don't need the prompt repeated.
        prompt_override: str | None = None
        if self.state.initial_prompt and not self._has_run_any_skill():
            prompt_override = self.state.initial_prompt

        return await self.executor.run_skill(
            thread_id=self.state.thread_id,
            skill=step.skill or "",
            model=step.model,
            new_session=new_session,
            prompt_override=prompt_override,
            timeout_seconds=step_timeout,
        )

    def _has_run_any_skill(self) -> bool:
        """True once at least one skill step has executed (success or fail)."""
        return any(r.skill for r in self.state.history)

    def _current_step(self) -> FlowStep | None:
        if self.state.current_phase_idx >= len(self.flow.phases):
            return None
        phase = self.flow.phases[self.state.current_phase_idx]
        if self.state.current_step_idx >= len(phase.steps):
            return None
        step = phase.steps[self.state.current_step_idx]

        # Loop step? Walk into substeps for the active iteration.
        # Loop expansion (populating self.state.loop_stories) happens in
        # _maybe_expand_loop() before _current_step() is consulted; here
        # we just consume the snapshot.
        if step.loop.value != "none":
            if not self.state.loop_stories:
                # Loop entered but no pending stories — treat as no current step.
                # _maybe_expand_loop is responsible for advancing past it.
                return None
            if self.state.current_loop_iter >= len(self.state.loop_stories):
                return None
            if self.state.current_substep_idx >= len(step.substeps):
                return None
            return step.substeps[self.state.current_substep_idx]

        return step

    def _parent_loop_step(self) -> FlowStep | None:
        """If the cursor is inside a loop step, return that loop's FlowStep.

        Loop substeps reference the parent's `new_session_each` etc.
        """
        if self.state.current_phase_idx >= len(self.flow.phases):
            return None
        phase = self.flow.phases[self.state.current_phase_idx]
        if self.state.current_step_idx >= len(phase.steps):
            return None
        step = phase.steps[self.state.current_step_idx]
        if step.loop.value != "none":
            return step
        return None

    async def _maybe_expand_loop(self) -> str:
        """If parked on an unexpanded per-story loop, snapshot pending stories.

        Returns:
            "expanded": loop just got its story list populated.
            "skipped": no pending stories — loop was advanced past, caller
                       should treat this as a CONTINUE without running anything.
            "noop":    not a loop, or already expanded; proceed normally.
        """
        if self.state.current_phase_idx >= len(self.flow.phases):
            return "noop"
        phase = self.flow.phases[self.state.current_phase_idx]
        if self.state.current_step_idx >= len(phase.steps):
            return "noop"
        step = phase.steps[self.state.current_step_idx]

        if step.loop.value == "none":
            return "noop"

        # Already expanded for this loop entry?
        # We treat loop_stories as the snapshot for the *current* loop step.
        # It's cleared (in _advance) when the loop step is fully consumed.
        if self.state.loop_stories:
            return "noop"

        # Only expand at the start of a loop (iter 0, substep 0). If we're
        # mid-iteration with an empty loop_stories the state was wiped and
        # we just bail out — _advance() will move past the loop.
        if self.state.current_substep_idx != 0 or self.state.current_loop_iter != 0:
            return "noop"

        try:
            stories = list(await self.executor.list_pending_stories(
                thread_id=self.state.thread_id,
            ))
        except Exception as exc:
            logger.warning("[auto] list_pending_stories crashed: %s", exc)
            stories = []

        if not stories:
            # No pending work — record skip on the loop step itself and
            # advance past it.
            self._record(step, status="skipped", notes="no pending stories")
            # Advance step within phase (don't recurse through _advance's
            # loop branch since the cursor is on the loop wrapper itself).
            self.state.current_step_idx += 1
            self.state.current_substep_idx = 0
            self.state.current_loop_iter = 0
            self._cross_phase_if_at_end()
            return "skipped"

        self.state.loop_stories = stories
        return "expanded"

    def _cross_phase_if_at_end(self) -> None:
        """If current_step_idx is past end of current phase, advance to the
        next phase (or mark DONE)."""
        if self.state.current_phase_idx >= len(self.flow.phases):
            return
        phase = self.flow.phases[self.state.current_phase_idx]
        if self.state.current_step_idx < len(phase.steps):
            return
        self.state.current_phase_idx += 1
        self.state.current_step_idx = 0
        self.state.current_substep_idx = 0
        self.state.current_loop_iter = 0
        if self.state.current_phase_idx >= len(self.flow.phases):
            self.state.status = STATUS_DONE

    def _advance(self) -> None:
        """Move to the next step; cross phase boundaries automatically.

        Loop semantics:
          - Inside substeps: bump substep_idx.
          - End of substeps: bump loop_iter, reset substep_idx to 0.
          - End of all iterations (loop_iter == len(loop_stories)):
              clear loop_stories, advance past the loop step.
        """
        phase = self.flow.phases[self.state.current_phase_idx]
        step = phase.steps[self.state.current_step_idx]

        if step.loop.value != "none":
            # Inside a loop step — advance substep first
            self.state.current_substep_idx += 1
            if self.state.current_substep_idx < len(step.substeps):
                return
            # Substeps exhausted — start the next iteration
            self.state.current_substep_idx = 0
            self.state.current_loop_iter += 1
            # Per-story loop is bounded by the snapshot taken at expansion.
            if self.state.current_loop_iter < len(self.state.loop_stories):
                return  # next iteration, restart from substep 0
            # All iterations done — clear snapshot, fall through to the
            # next step in the phase.
            self.state.loop_stories = []
            self.state.current_loop_iter = 0

        # Advance step within phase
        self.state.current_step_idx += 1
        if self.state.current_step_idx < len(phase.steps):
            return

        # Advance phase
        self.state.current_phase_idx += 1
        self.state.current_step_idx = 0
        self.state.current_substep_idx = 0
        self.state.current_loop_iter = 0
        self.state.loop_stories = []

        if self.state.current_phase_idx >= len(self.flow.phases):
            self.state.status = STATUS_DONE

    def _evaluate_gate(self, step: FlowStep, result: StepResult) -> str:
        """Return 'pause' or 'continue' based on gate type + run mode."""
        gate = step.gate

        if gate == GateType.AUTO:
            return "continue"

        if gate == GateType.AUTO_ON_PASS:
            return "pause" if result.findings else "continue"

        if gate == GateType.HUMAN_REVIEW:
            # /yolo bypasses human-review entirely
            return "continue" if self.state.mode == RunMode.YOLO else "pause"

        if gate == GateType.PARTY_MODE:
            # Party-mode is handled inline above; if we get here, just continue
            return "continue"

        return "continue"

    def _prev_succeeded(self) -> bool:
        """Did the immediately preceding step in the same phase succeed?

        "Preceding step" means the step at `current_step_idx - 1` of the
        current phase. We look up its most recent record in history.

        - First step in a phase → no predecessor → True (don't gate).
        - Predecessor record missing → True (treat as if it ran fine; this
          can happen with `optional` declined steps where we deliberately
          recorded `skipped`, but defensive against state corruption too).
        - Predecessor's last record is `success` or `skipped` → True.
        - Predecessor's last record is `failed` → False.
        """
        # First step in the phase has no predecessor in this phase.
        if self.state.current_step_idx == 0:
            return True

        if self.state.current_phase_idx >= len(self.flow.phases):
            return True
        phase = self.flow.phases[self.state.current_phase_idx]
        prev_idx = self.state.current_step_idx - 1
        if prev_idx < 0 or prev_idx >= len(phase.steps):
            return True
        prev_step = phase.steps[prev_idx]

        # Find the most recent record for the predecessor's id (or, for
        # loop wrappers, any of their substeps).
        predecessor_ids = {prev_step.id}
        for sub in prev_step.substeps:
            predecessor_ids.add(sub.id)

        for record in reversed(self.state.history):
            if record.step_id in predecessor_ids:
                # `skipped` is treated as a non-failure — predecessors that
                # were deliberately bypassed should not block the dependent
                # step from running.
                return record.status != "failed"
        # No record for predecessor — treat as not-yet-run (don't gate).
        return True

    def _record(
        self,
        step: FlowStep,
        *,
        status: str,
        attempts: int = 1,
        findings: bool = False,
        party_mode_rounds: int = 0,
        notes: str | None = None,
    ) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        record = StepRecord(
            step_id=step.id,
            phase_id=step.phase_id,
            skill=step.skill,
            status=status,
            attempts=attempts,
            findings=findings,
            party_mode_rounds=party_mode_rounds,
            started_at=now,
            finished_at=now,
            notes=notes,
        )
        self.state.history.append(record)
        self.state.last_active_at = now
