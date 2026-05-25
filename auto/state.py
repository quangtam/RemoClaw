"""Persistent autonomous-run state — mirrors the SQLite `auto_run` row.

Why persist? A single autonomous run can take hours (esp. /yolo full).
The bot might restart for any reason — config change, OOM, manual restart.
We want to resume exactly where we left off, not re-run completed steps.

The state is keyed by `thread_id` because each Telegram thread has at most
one autonomous run at a time. Multi-thread is supported because every
thread gets its own row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from auto.flow import RunMode


@dataclass
class StepRecord:
    """Outcome of a single executed step. Logged so /auto status can show
    progress and so retries know how many attempts they've used."""

    step_id: str
    phase_id: str
    skill: str | None
    status: str           # 'success' | 'failed' | 'skipped' | 'paused'
    attempts: int = 1
    findings: bool = False
    party_mode_rounds: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    notes: str | None = None  # short summary, not full output

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "phase_id": self.phase_id,
            "skill": self.skill,
            "status": self.status,
            "attempts": self.attempts,
            "findings": self.findings,
            "party_mode_rounds": self.party_mode_rounds,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StepRecord":
        return cls(**{k: d.get(k) for k in (
            "step_id", "phase_id", "skill", "status", "attempts",
            "findings", "party_mode_rounds",
            "started_at", "finished_at", "notes",
        ) if k in d})


# AutoRunner status values — what the run is doing right now.
STATUS_RUNNING = "running"      # actively executing a step
STATUS_PAUSED_GATE = "paused-gate"     # stopped at a human-review gate
STATUS_PAUSED_FAIL = "paused-fail"     # stopped after exhausting retries
STATUS_PAUSED_PARTY = "paused-party"   # in party-mode, waiting for round
STATUS_DONE = "done"            # all steps complete
STATUS_ABORTED = "aborted"      # user cancelled


@dataclass
class AutoState:
    """Snapshot of an autonomous run for a single thread.

    Reads/writes to SQLite are handled by the db.auto_run.* helpers.
    """

    thread_id: int
    flow_id: str
    mode: RunMode
    status: str = STATUS_RUNNING
    current_phase_idx: int = 0
    current_step_idx: int = 0
    # For loop steps: which substep + which loop iteration we're on
    current_substep_idx: int = 0
    current_loop_iter: int = 0
    # When paused at a gate, what the runner is waiting on
    pending_gate_step_id: str | None = None
    # When at a fail-pause, the last error
    last_error: str | None = None
    # Asked-once tracking — set of step_ids whose `ask_once` we've already
    # asked the user, so a retry/resume doesn't re-prompt
    ask_once_asked: set[str] = field(default_factory=set)
    # Append-only log of completed/in-flight steps, ordered chronologically
    history: list[StepRecord] = field(default_factory=list)
    started_at: str | None = None
    last_active_at: str | None = None

    def to_db_row(self) -> dict[str, Any]:
        """Serialize for SQLite — collapse complex fields into JSON columns."""
        return {
            "thread_id": self.thread_id,
            "flow_id": self.flow_id,
            "mode": self.mode.value,
            "status": self.status,
            "current_phase_idx": self.current_phase_idx,
            "current_step_idx": self.current_step_idx,
            "current_substep_idx": self.current_substep_idx,
            "current_loop_iter": self.current_loop_iter,
            "pending_gate_step_id": self.pending_gate_step_id,
            "last_error": self.last_error,
            "ask_once_asked": json.dumps(sorted(self.ask_once_asked)),
            "history": json.dumps([r.to_dict() for r in self.history]),
            "started_at": self.started_at,
            "last_active_at": self.last_active_at,
        }

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "AutoState":
        """Inverse of to_db_row(). Tolerant of missing fields for forward-compat."""
        history_raw = row.get("history") or "[]"
        ask_once_raw = row.get("ask_once_asked") or "[]"
        try:
            history = [StepRecord.from_dict(d) for d in json.loads(history_raw)]
        except (json.JSONDecodeError, TypeError):
            history = []
        try:
            ask_once = set(json.loads(ask_once_raw))
        except (json.JSONDecodeError, TypeError):
            ask_once = set()

        return cls(
            thread_id=row["thread_id"],
            flow_id=row["flow_id"],
            mode=RunMode(row.get("mode", "auto")),
            status=row.get("status", STATUS_RUNNING),
            current_phase_idx=row.get("current_phase_idx", 0),
            current_step_idx=row.get("current_step_idx", 0),
            current_substep_idx=row.get("current_substep_idx", 0),
            current_loop_iter=row.get("current_loop_iter", 0),
            pending_gate_step_id=row.get("pending_gate_step_id"),
            last_error=row.get("last_error"),
            ask_once_asked=ask_once,
            history=history,
            started_at=row.get("started_at"),
            last_active_at=row.get("last_active_at"),
        )

    def is_active(self) -> bool:
        return self.status in (
            STATUS_RUNNING, STATUS_PAUSED_GATE,
            STATUS_PAUSED_FAIL, STATUS_PAUSED_PARTY,
        )

    def is_paused(self) -> bool:
        return self.status.startswith("paused-")

    def attempts_for(self, step_id: str) -> int:
        """How many times have we tried `step_id`? (counts only the most
        recent contiguous run — older attempts in earlier loop iterations
        don't count toward retry limit.)"""
        n = 0
        # Walk history backwards; count only consecutive runs of step_id
        for record in reversed(self.history):
            if record.step_id == step_id and record.status in ("failed", "in-progress"):
                n += 1
            else:
                break
        return n
