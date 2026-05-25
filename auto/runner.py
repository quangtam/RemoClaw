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
    ) -> StepResult:
        """Execute a BMAD skill and capture its outcome."""

    async def run_builtin(
        self, *, thread_id: int, builtin: str, state: AutoState,
    ) -> StepResult:
        """Execute a runner-internal handler (e.g. 'present-summary')."""

    async def ask_human_review(
        self, *, thread_id: int, step: FlowStep,
    ) -> bool:
        """Pause and ask the user to approve. Returns True to continue,
        False to abort. In /yolo mode this is bypassed by the runner."""

    async def run_party_mode(
        self, *, thread_id: int, context: str, min_rounds: int,
    ) -> int:
        """Run party-mode discussion. Returns the number of rounds executed."""

    async def ask_once(
        self, *, thread_id: int, step: FlowStep, prompt: str,
    ) -> bool:
        """Ask the user a yes/no question (used by `optional` + `ask_once`)."""

    async def list_pending_stories(self, *, thread_id: int) -> list[str]:
        """Return story ids that need implementing — for loop expansion."""


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

        current = self._current_step()
        if current is None:
            self.state.status = STATUS_DONE
            return RunStatus.DONE

        # ── Optional + ask_once ──────────────────────────────────
        if current.optional and current.ask_once and current.id not in self.state.ask_once_asked:
            self.state.ask_once_asked.add(current.id)
            wants = await self.executor.ask_once(
                thread_id=self.state.thread_id,
                step=current,
                prompt=current.ask_once,
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
        result = await self._execute_step(current)

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
        if step.builtin:
            return await self.executor.run_builtin(
                thread_id=self.state.thread_id,
                builtin=step.builtin,
                state=self.state,
            )

        # Determine if we need a fresh session
        new_session = step.new_session
        if step.loop.value != "none" and step.new_session_each:
            new_session = True

        return await self.executor.run_skill(
            thread_id=self.state.thread_id,
            skill=step.skill or "",
            model=step.model,
            new_session=new_session,
        )

    def _current_step(self) -> FlowStep | None:
        if self.state.current_phase_idx >= len(self.flow.phases):
            return None
        phase = self.flow.phases[self.state.current_phase_idx]
        if self.state.current_step_idx >= len(phase.steps):
            return None
        step = phase.steps[self.state.current_step_idx]

        # Loop step? Walk into substeps
        if step.loop.value != "none":
            if self.state.current_substep_idx >= len(step.substeps):
                return None
            return step.substeps[self.state.current_substep_idx]

        return step

    def _advance(self) -> None:
        """Move to the next step; cross phase boundaries automatically."""
        phase = self.flow.phases[self.state.current_phase_idx]
        step = phase.steps[self.state.current_step_idx]

        if step.loop.value != "none":
            # Inside a loop step — advance substep first
            self.state.current_substep_idx += 1
            if self.state.current_substep_idx < len(step.substeps):
                return
            # Substeps done — advance loop iteration
            self.state.current_substep_idx = 0
            self.state.current_loop_iter += 1
            # Note: caller decides if loop is done by checking pending stories
            # via Executor; runner just keeps iterating until they say stop.
            # For now we exit the loop after one iteration if no expansion logic.
            # (Sprint A scope — loop expansion for per-story comes in Sprint C.)

        # Advance step within phase
        self.state.current_step_idx += 1
        if self.state.current_step_idx < len(phase.steps):
            return

        # Advance phase
        self.state.current_phase_idx += 1
        self.state.current_step_idx = 0
        self.state.current_substep_idx = 0
        self.state.current_loop_iter = 0

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
        if not self.state.history:
            return True
        last = self.state.history[-1]
        return last.status == "success"

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
