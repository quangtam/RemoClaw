"""Session manager for RemoClaw v2.0.

Owns PTY session pool, lifecycle, and explicit state machine transitions.
Does NOT handle I/O streaming — that stays in cli_runner.py.

State machine:
    IDLE → STREAMING → (output, output, ...) → STREAMING → IDLE
                     ↓ idle 12s
                     DETECTING_PROMPT → STREAMING (false alarm)
                                     ↓ prompt matched
                                     WAITING_FOR_USER → PIPING_REPLY → STREAMING → IDLE
    ANY → DEAD (kill, crash, timeout)
"""

import concurrent.futures
import logging
import os
import re
import select
import signal
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class SessionLimitExceeded(Exception):
    """Raised when attempting to create a session beyond MAX_SESSIONS."""


@dataclass
class DecisionPrompt:
    """Represents a detected CLI interactive prompt requiring user input.

    Yielded from the streaming generator when the CLI is waiting for
    a response (e.g., "Continue? [y/N]"). Consumer (remoclaw.py) forwards
    this to the user via Telegram, then calls pipe_reply_stream() to
    resume execution once user replies.
    """

    prompt_text: str
    context_lines: list[str]


# ─── Prompt detection ─────────────────────────────────────────

_GENERIC_PROMPT_PATTERNS = [
    re.compile(r"\[y/N\]", re.IGNORECASE),
    re.compile(r"\[Y/n\]", re.IGNORECASE),
    re.compile(r"\(yes/no\)", re.IGNORECASE),
    re.compile(r"\(y/n\)", re.IGNORECASE),
]


def detect_decision_prompt(
    buffer_lines: list[str],
    provider_patterns: list[re.Pattern] | None = None,
    max_line_length: int = 100,
) -> DecisionPrompt | None:
    """Detect CLI interactive prompt from recent output lines.

    Detection rules:
    - Last non-empty line must be ≤ max_line_length chars (real prompts are short)
    - Line must match a generic or provider-specific pattern, OR end with '?'

    Returns:
        DecisionPrompt with prompt_text + last 5 context_lines, or None.
    """
    if not buffer_lines:
        return None

    # Find last non-empty line
    last_line = ""
    for line in reversed(buffer_lines):
        stripped = line.rstrip("\n").strip()
        if stripped:
            last_line = stripped
            break

    if not last_line or len(last_line) > max_line_length:
        return None

    # Check generic patterns
    for pat in _GENERIC_PROMPT_PATTERNS:
        if pat.search(last_line):
            return DecisionPrompt(
                prompt_text=last_line,
                context_lines=[line.rstrip("\n") for line in buffer_lines[-5:]],
            )

    # Check provider-specific patterns
    if provider_patterns:
        for pat in provider_patterns:
            if pat.search(last_line):
                return DecisionPrompt(
                    prompt_text=last_line,
                    context_lines=[line.rstrip("\n") for line in buffer_lines[-5:]],
                )

    # Fallback: ends with '?'
    if last_line.endswith("?"):
        return DecisionPrompt(
            prompt_text=last_line,
            context_lines=[line.rstrip("\n") for line in buffer_lines[-5:]],
        )

    return None


class PtyState(Enum):
    """Explicit states for PTY session lifecycle."""

    IDLE = "idle"                      # Session alive, no active task
    STREAMING = "streaming"            # Receiving output from CLI
    DETECTING_PROMPT = "detecting_prompt"  # Idle threshold counting
    WAITING_FOR_USER = "waiting_for_user"  # Decision prompt forwarded, waiting for reply
    PIPING_REPLY = "piping_reply"      # Reply being written to PTY
    DEAD = "dead"                      # Terminal state


# Valid transitions: from_state → set of allowed to_states
# Self-transitions (e.g., STREAMING → STREAMING) used for logging output events
VALID_TRANSITIONS: dict[PtyState, set[PtyState]] = {
    PtyState.IDLE: {PtyState.STREAMING, PtyState.DEAD},
    PtyState.STREAMING: {
        PtyState.STREAMING,
        PtyState.DETECTING_PROMPT,
        PtyState.IDLE,
        PtyState.DEAD,
    },
    PtyState.DETECTING_PROMPT: {
        PtyState.STREAMING,
        PtyState.WAITING_FOR_USER,
        PtyState.DEAD,
    },
    PtyState.WAITING_FOR_USER: {PtyState.PIPING_REPLY, PtyState.DEAD},
    PtyState.PIPING_REPLY: {PtyState.STREAMING, PtyState.DEAD},
    PtyState.DEAD: set(),  # Terminal — no outgoing transitions
}


@dataclass
class PtySession:
    """PTY-backed CLI session with explicit lifecycle state."""

    thread_id: int | None
    pid: int
    fd: int
    state: PtyState = PtyState.IDLE
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    ready: bool = False  # True once initial provider prompt reached

    @property
    def alive(self) -> bool:
        """Return True if underlying process is alive AND state != DEAD."""
        if self.state == PtyState.DEAD:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def write(self, data: str) -> None:
        """Write data to PTY fd."""
        os.write(self.fd, data.encode("utf-8"))

    def read(self, timeout: float = 1.0) -> str | None:
        """Read available data from PTY. Returns None on timeout, '' on EOF."""
        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            self.state = PtyState.DEAD
            return ""
        if not r:
            return None
        try:
            data = os.read(self.fd, 8192)
            if not data:
                return ""
            return data.decode("utf-8", errors="replace")
        except OSError:
            self.state = PtyState.DEAD
            return ""

    def kill(self) -> None:
        """Kill the PTY process and mark session as DEAD."""
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            pass
        self.state = PtyState.DEAD


class SessionManager:
    """Owns session pool and enforces state machine transitions."""

    def __init__(self, max_sessions: int = 5) -> None:
        self._sessions: dict[int | None, PtySession] = {}
        self._max_sessions = max_sessions
        # Dedicated executor for blocking PTY I/O (prevents default-pool starvation)
        self._pty_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="pty"
        )

    @property
    def pty_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """Expose dedicated executor for PTY blocking reads/writes."""
        return self._pty_executor

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    def can_create(self) -> bool:
        """Return True if under the session limit (counting non-DEAD sessions)."""
        return self.active_count() < self._max_sessions

    def shutdown(self) -> None:
        """Shutdown executor and kill all sessions. Call on app exit."""
        for tid in list(self._sessions.keys()):
            self.kill(tid)
        self._pty_executor.shutdown(wait=False)

    # ── Basic pool operations ──────────────────────────────────

    def get(self, thread_id: int | None) -> PtySession | None:
        """Return session for thread_id, or None if not found."""
        return self._sessions.get(thread_id)

    def create(
        self, thread_id: int | None, pid: int, fd: int
    ) -> PtySession:
        """Create a new session in IDLE state and register it.

        Raises:
            SessionLimitExceeded: if already at max_sessions
        """
        if not self.can_create():
            raise SessionLimitExceeded(
                f"Maximum sessions reached ({self._max_sessions}). "
                f"Use /cancel in another thread to free a slot."
            )
        session = PtySession(thread_id=thread_id, pid=pid, fd=fd)
        self._sessions[thread_id] = session
        logger.debug(
            f"[SessionManager] create: thread_id={thread_id} pid={pid} state=idle"
        )
        return session

    def kill(self, thread_id: int | None) -> bool:
        """Kill the session and remove from pool. Returns True if killed."""
        session = self._sessions.pop(thread_id, None)
        if session is None:
            return False
        session.kill()
        logger.info(f"[SessionManager] kill: thread_id={thread_id}")
        return True

    def list_all(self) -> dict[int | None, PtySession]:
        """Return a shallow copy of the session pool."""
        return dict(self._sessions)

    def active_count(self) -> int:
        """Count sessions not in DEAD state."""
        return sum(
            1 for s in self._sessions.values() if s.state != PtyState.DEAD
        )

    # ── State machine ─────────────────────────────────────────

    def transition(
        self,
        thread_id: int | None,
        to_state: PtyState,
        reason: str = "",
    ) -> None:
        """Transition a session to a new state, validating against VALID_TRANSITIONS.

        Raises:
            KeyError: if thread_id has no session
            RuntimeError: if transition from current state to `to_state` is invalid
        """
        session = self._sessions.get(thread_id)
        if session is None:
            raise KeyError(f"No session for thread_id={thread_id}")

        from_state = session.state
        allowed = VALID_TRANSITIONS.get(from_state, set())
        if to_state not in allowed:
            raise RuntimeError(
                f"Invalid transition for thread_id={thread_id}: "
                f"{from_state.value} → {to_state.value} "
                f"(allowed: {sorted(s.value for s in allowed)})"
            )

        session.state = to_state
        session.last_active_at = time.monotonic()
        logger.debug(
            f"[PTY:{thread_id}] {from_state.value} → {to_state.value}"
            f"{': ' + reason if reason else ''}"
        )

    def get_state(self, thread_id: int | None) -> PtyState | None:
        """Return current state for a thread, or None if no session."""
        session = self._sessions.get(thread_id)
        return session.state if session else None

    def cleanup_orphans(self) -> int:
        """Kill sessions whose underlying process has died.

        Walks the session pool, checks `.alive` for each, and kills any
        that are dead (transitions to DEAD, closes fd, removes from pool).

        Returns:
            Number of orphan sessions cleaned up.
        """
        orphans: list[int | None] = []
        for tid, session in list(self._sessions.items()):
            if not session.alive and session.state != PtyState.DEAD:
                orphans.append(tid)
        for tid in orphans:
            logger.warning(f"[SessionManager] cleanup_orphans: killing thread_id={tid}")
            self.kill(tid)
        if orphans:
            logger.info(f"[SessionManager] cleanup_orphans: cleaned up {len(orphans)} orphan sessions")
        return len(orphans)

    @staticmethod
    def get_status_emoji(state: PtyState) -> str:
        """Return Telegram-friendly status emoji for a PtyState."""
        return {
            PtyState.IDLE: "💤",
            PtyState.STREAMING: "🟢",
            PtyState.DETECTING_PROMPT: "🟢",
            PtyState.WAITING_FOR_USER: "⏳",
            PtyState.PIPING_REPLY: "🟢",
            PtyState.DEAD: "❌",
        }.get(state, "❓")

    def cleanup_idle(self, max_age_seconds: int = 1800) -> int:
        """Kill sessions idle (IDLE state) for longer than max_age_seconds."""
        now = time.monotonic()
        to_kill: list[int | None] = []
        for tid, session in list(self._sessions.items()):
            if (
                session.state == PtyState.IDLE
                and (now - session.last_active_at) > max_age_seconds
            ):
                to_kill.append(tid)
        for tid in to_kill:
            logger.info(
                f"[SessionManager] cleanup_idle: killing thread_id={tid} "
                f"(idle > {max_age_seconds}s)"
            )
            self.kill(tid)
        return len(to_kill)

    def expired_decisions(self, max_wait_seconds: int = 1800) -> list[int | None]:
        """Return thread_ids where WAITING_FOR_USER has exceeded max_wait_seconds.

        Does NOT kill — caller handles notification then kill.
        """
        now = time.monotonic()
        expired: list[int | None] = []
        for tid, session in self._sessions.items():
            if (
                session.state == PtyState.WAITING_FOR_USER
                and (now - session.last_active_at) > max_wait_seconds
            ):
                expired.append(tid)
        return expired
