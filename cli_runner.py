"""RemoClaw CLI runner — persistent interactive sessions via PTY.

Keeps a long-running CLI process per thread using pseudo-terminals.
Messages are written to the PTY; responses are read until the next
prompt marker. Falls back to --no-interactive for CLIs that don't
support interactive mode.

Includes idle watchdog and per-thread concurrency.
State machine transitions delegated to SessionManager.
"""

import asyncio
import logging
import os
import pty
import re
import select
import signal
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from cli_providers import CliProvider, create_provider
from config import Config
from session_manager import (
    DecisionPrompt,
    PtySession,
    PtyState,
    SessionLimitExceeded,
    SessionManager,
    detect_decision_prompt,
)

logger = logging.getLogger(__name__)

# Warn user if no output for this many seconds
IDLE_WARN_INTERVAL = 30

# Regex to detect the interactive prompt (e.g. "2% !> " or "!> ")
_PROMPT_RE = re.compile(r"\d*%?\s*!>\s*$")

# Strip ANSI for prompt detection
_ANSI_RE = re.compile(r"\x1b\[[\x20-\x3f]*[\x40-\x7e]|\x1b[\x20-\x2f]*[\x30-\x7e]|\x9b[\x20-\x3f]*[\x40-\x7e]")

# How long to wait for the initial prompt after spawning
_INIT_TIMEOUT = 120


@dataclass
class CliResult:
    """Result from a CLI execution."""

    output: str
    exit_code: int
    timed_out: bool = False


class CliRunner:
    """Manages CLI subprocess execution with persistent PTY sessions."""

    def __init__(self, config: Config) -> None:
        self._config = config

        self._session_mgr = SessionManager(max_sessions=config.max_sessions)
        self._locks: dict[int | None, asyncio.Lock] = {}
        self._exit_codes: dict[int | None, int] = {}

        self._provider: CliProvider = create_provider(
            provider_name=config.cli_provider,
            cli_path=config.cli_path or None,
            api_key=config.cli_api_key,
            trust_all_tools=config.trust_all_tools,
            extra_args=list(config.cli_extra_args) if config.cli_extra_args else None,
        )

        # Per-thread provider cache: provider_id → CliProvider instance
        self._provider_cache: dict[str, CliProvider] = {
            self._provider.provider_id: self._provider,
        }

        # Track which provider each thread session is using
        self._session_providers: dict[int | None, str] = {}

        logger.info(
            "CLI Provider: %s (%s)",
            self._provider.name,
            self._provider.config.cli_path,
        )

    @property
    def _sessions(self) -> dict[int | None, PtySession]:
        """Backward-compat: expose session manager's internal dict."""
        return self._session_mgr._sessions

    @_sessions.setter
    def _sessions(self, value: dict[int | None, PtySession]) -> None:
        """Backward-compat setter: replace session manager's internal dict."""
        self._session_mgr._sessions = value

    @property
    def provider(self) -> CliProvider:
        return self._provider

    @property
    def _env(self) -> dict[str, str]:
        return self._provider.build_env(os.environ.copy())

    def _get_lock(self, thread_id: int | None) -> asyncio.Lock:
        if thread_id not in self._locks:
            self._locks[thread_id] = asyncio.Lock()
        return self._locks[thread_id]

    def is_busy(self, thread_id: int | None) -> bool:
        return self._get_lock(thread_id).locked()

    def get_exit_code(self, thread_id: int | None) -> int:
        return self._exit_codes.get(thread_id, 0)

    def get_provider_for_thread(self, provider_name: str | None = None) -> CliProvider:
        """Get or create a CliProvider instance by name.

        Uses a cache to avoid re-creating providers on every message.
        Returns the global default if provider_name is None or matches default.
        """
        if not provider_name or provider_name == self._provider.provider_id:
            return self._provider

        if provider_name in self._provider_cache:
            return self._provider_cache[provider_name]

        # Create new provider instance
        provider = create_provider(
            provider_name=provider_name,
            cli_path=None,  # auto-detect from PATH
            api_key=self._config.cli_api_key,
            trust_all_tools=self._config.trust_all_tools,
            extra_args=list(self._config.cli_extra_args) if self._config.cli_extra_args else None,
        )
        self._provider_cache[provider_name] = provider
        logger.info(
            "Created cached provider: %s (%s)",
            provider.name, provider.config.cli_path,
        )
        return provider

    # ── PTY session management ───────────────────────────────────

    async def _get_or_create_session(
        self, thread_id: int | None, *, model: str | None = None,
        project_dir: str | None = None,
        provider: CliProvider | None = None,
    ) -> PtySession | None:
        """Get existing session or spawn a new interactive CLI via PTY.

        Args:
            project_dir: Per-thread working directory for new sessions.
                If None or empty, falls back to self._config.project_dir (.env default).
                Existing sessions reuse their original spawn cwd regardless.
            provider: Per-thread CLI provider. If None, uses self._provider (global).
                If the provider differs from the session's original provider,
                the existing session is killed and a new one spawned.

        Returns:
            PtySession on success, None if cwd is invalid (non-existent dir)
            or session limit exceeded.
        """
        effective_provider = provider or self._provider
        effective_provider_id = effective_provider.provider_id

        session = self._session_mgr.get(thread_id)
        if session and session.alive:
            # Check if provider changed — must kill and respawn
            session_provider_id = self._session_providers.get(thread_id, self._provider.provider_id)
            if session_provider_id != effective_provider_id:
                logger.info(
                    "[SessionManager] provider switch [thread=%s]: %s → %s — killing old session",
                    thread_id, session_provider_id, effective_provider_id,
                )
                self._session_mgr.kill(thread_id)
                # Fall through to spawn new session
            else:
                # Warn if bound project_dir differs from session's spawn cwd
                effective_cwd = project_dir if project_dir else self._config.project_dir
                session_cwd = getattr(session, "_spawn_cwd", None)
                if session_cwd and session_cwd != effective_cwd:
                    logger.info(
                        "[SessionManager] session reuse [thread=%s]: bound project_dir=%s "
                        "differs from session spawn cwd=%s — use /new to switch",
                        thread_id, effective_cwd, session_cwd,
                    )
                return session
        elif session:
            # Clean up dead session
            self._session_mgr.kill(thread_id)

        args = effective_provider.build_interactive_args(model=model)
        if not args:
            return None

        env = effective_provider.build_env(os.environ.copy())
        cwd = project_dir if project_dir else self._config.project_dir

        # Validate cwd before forking — prevents silent child-process crashes
        # from os.chdir() failures that cause the parent to hang until timeout.
        if not os.path.isdir(cwd):
            logger.error(
                "[SessionManager] spawn failed [thread=%s]: cwd does not exist: %s",
                thread_id, cwd,
            )
            return None

        # Cwd logged at DEBUG (may contain user paths) — user actions at INFO
        logger.info(
            "Spawning PTY session [thread=%s]: %s", thread_id, " ".join(args),
        )
        logger.debug("[SessionManager] spawn cwd [thread=%s]: %s", thread_id, cwd)

        # Spawn in a thread to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        pid, fd = await loop.run_in_executor(
            self._session_mgr.pty_executor, lambda: self._spawn_pty(args, env, cwd)
        )

        try:
            session = self._session_mgr.create(thread_id, pid, fd)
            session._spawn_cwd = cwd  # Track for mismatch detection on reuse
            self._session_providers[thread_id] = effective_provider_id
        except SessionLimitExceeded:
            # Clean up the spawned process we can't use
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            logger.warning(
                "Session limit exceeded [thread=%s, max=%d]",
                thread_id, self._session_mgr.max_sessions,
            )
            return None

        # Wait for initial prompt
        deadline = time.monotonic() + _INIT_TIMEOUT
        found_prompt = False

        while time.monotonic() < deadline:
            chunk = await loop.run_in_executor(
                self._session_mgr.pty_executor, lambda: session.read(timeout=2.0)
            )
            if chunk is None:
                continue
            if chunk == "":
                break  # EOF

            clean = _ANSI_RE.sub("", chunk)
            if _PROMPT_RE.search(clean.rstrip()):
                found_prompt = True
                break

        if found_prompt:
            session.ready = True
            logger.info("PTY session ready [thread=%s] in %.1fs", thread_id, time.monotonic() - (deadline - _INIT_TIMEOUT))
        else:
            logger.warning("PTY session failed to reach prompt [thread=%s]", thread_id)
            self._session_mgr.kill(thread_id)
            return None

        return session

    @staticmethod
    def _spawn_pty(args: list[str], env: dict[str, str], cwd: str) -> tuple[int, int]:
        """Fork a PTY process (runs in executor thread)."""
        pid, fd = pty.fork()
        if pid == 0:
            # Child process
            os.chdir(cwd)
            os.environ.update(env)
            os.execvp(args[0], args)
            # Never reaches here
        return pid, fd

    def _kill_session(self, thread_id: int | None) -> None:
        """Kill and remove a session (sync, safe to call from anywhere)."""
        self._session_mgr.kill(thread_id)

    # ── Streaming execution ──────────────────────────────────────

    async def execute_stream(
        self,
        prompt: str,
        *,
        thread_id: int | None = None,
        model: str | None = None,
        resume: bool = False,
        timeout_seconds: int | None = None,
        project_dir: str | None = None,
        provider_name: str | None = None,
    ) -> AsyncGenerator["str | DecisionPrompt", None]:
        """Execute a prompt and yield output lines.

        Args:
            timeout_seconds: Per-thread override. If None, uses config.cli_timeout.
            project_dir: Per-thread working directory. If None, falls back to
                config.project_dir. Existing sessions reuse their original cwd.
            provider_name: Per-thread CLI provider ID. If None, uses global default.
                If different from the current session's provider, the session is
                killed and respawned with the new provider.
        """
        lock = self._get_lock(thread_id)
        provider = self.get_provider_for_thread(provider_name)

        async with lock:
            session = await self._get_or_create_session(
                thread_id, model=model, project_dir=project_dir,
                provider=provider,
            )

            if session and session.alive:
                async for line in self._stream_pty(
                    session, prompt, thread_id, timeout_seconds=timeout_seconds,
                    provider=provider,
                ):
                    yield line
            else:
                async for line in self._stream_non_interactive(
                    prompt, thread_id=thread_id, model=model, resume=resume,
                    project_dir=project_dir, provider=provider,
                ):
                    yield line

    async def _stream_pty(
        self,
        session: PtySession,
        prompt: str,
        thread_id: int | None,
        timeout_seconds: int | None = None,
        provider: CliProvider | None = None,
    ) -> AsyncGenerator["str | DecisionPrompt", None]:
        """Send prompt to PTY session and yield response chunks or DecisionPrompt.

        If an interactive prompt is detected, yields a DecisionPrompt and
        RETURNS (generator ends). Caller should then call pipe_reply_stream().

        Args:
            timeout_seconds: Per-thread timeout override. If None, uses global.
            provider: Per-thread provider for decision prompt patterns.
        """
        loop = asyncio.get_event_loop()
        effective_timeout = timeout_seconds or self._config.cli_timeout
        decision_idle_threshold = getattr(
            self._config, "decision_idle_threshold", 12
        )
        effective_provider = provider or self._provider

        # Transition: IDLE → STREAMING when prompt written
        if self._session_mgr.get_state(thread_id) == PtyState.IDLE:
            self._session_mgr.transition(
                thread_id, PtyState.STREAMING, reason="prompt written"
            )

        # Send the prompt
        try:
            await loop.run_in_executor(
                self._session_mgr.pty_executor, lambda: session.write(prompt + "\n")
            )
        except OSError:
            logger.warning("PTY write failed [thread=%s]", thread_id)
            self._kill_session(thread_id)
            self._exit_codes[thread_id] = -1
            yield "❌ CLI session died. Send your message again to start a new session.\n"
            return

        logger.info("Sent to PTY [thread=%s]: %s", thread_id, prompt[:80])

        deadline = time.monotonic() + effective_timeout
        last_output_time = time.monotonic()
        last_warn_time = 0.0
        # Time spent in WAITING_FOR_USER — paused from timeout accounting
        paused_duration = 0.0
        pause_start: float | None = None
        # Rolling buffer of recent output lines for decision detection
        line_buffer: list[str] = []
        # Track if we've checked for decision prompt in this idle window
        decision_checked_for_window = False

        # Provider-specific decision patterns (optional)
        provider_patterns = getattr(
            effective_provider, "decision_prompt_patterns", None
        ) or None

        while True:
            now = time.monotonic()
            current_state = self._session_mgr.get_state(thread_id)

            # Pause clock during WAITING_FOR_USER
            if current_state == PtyState.WAITING_FOR_USER:
                if pause_start is None:
                    pause_start = now
            else:
                if pause_start is not None:
                    paused_duration += now - pause_start
                    pause_start = None

            effective_deadline = deadline + paused_duration

            if now >= effective_deadline and current_state != PtyState.WAITING_FOR_USER:
                logger.warning("PTY stream timeout [thread=%s]", thread_id)
                self._kill_session(thread_id)
                self._exit_codes[thread_id] = -1
                yield "\n⏱ Global timeout reached. Session killed.\n"
                return

            if not session.alive:
                self._exit_codes[thread_id] = -1
                if self._session_mgr.get(thread_id) is not None:
                    try:
                        self._session_mgr.transition(
                            thread_id, PtyState.DEAD, reason="process died"
                        )
                    except (KeyError, RuntimeError):
                        pass
                    self._session_mgr.kill(thread_id)
                yield "\n❌ CLI process died unexpectedly.\n"
                return

            idle_seconds = now - last_output_time - paused_duration

            # Decision prompt detection (only during STREAMING, after idle threshold)
            if (
                current_state == PtyState.STREAMING
                and idle_seconds >= decision_idle_threshold
                and not decision_checked_for_window
            ):
                decision_checked_for_window = True
                # Transition STREAMING → DETECTING_PROMPT
                try:
                    self._session_mgr.transition(
                        thread_id, PtyState.DETECTING_PROMPT,
                        reason=f"idle {int(idle_seconds)}s",
                    )
                except (KeyError, RuntimeError) as exc:
                    logger.debug("[decision] transition skipped: %s", exc)
                else:
                    decision = detect_decision_prompt(
                        line_buffer,
                        provider_patterns=provider_patterns,
                    )
                    if decision is not None:
                        # Match! Transition → WAITING_FOR_USER and yield
                        try:
                            self._session_mgr.transition(
                                thread_id, PtyState.WAITING_FOR_USER,
                                reason=f"prompt detected: {decision.prompt_text[:40]}",
                            )
                        except (KeyError, RuntimeError):
                            pass
                        logger.info(
                            "[decision] detected [thread=%s]: %s",
                            thread_id, decision.prompt_text[:80],
                        )
                        yield decision
                        return  # Option B: generator ends, caller picks up
                    else:
                        # False alarm — back to STREAMING
                        try:
                            self._session_mgr.transition(
                                thread_id, PtyState.STREAMING,
                                reason="no prompt match",
                            )
                        except (KeyError, RuntimeError):
                            pass

            if (
                idle_seconds >= IDLE_WARN_INTERVAL
                and now - last_warn_time >= IDLE_WARN_INTERVAL
                and current_state != PtyState.WAITING_FOR_USER
            ):
                remaining = int(effective_deadline - now)
                yield f"\n⏳ Still working... no output for {int(idle_seconds)}s (timeout in {remaining}s)\n"
                last_warn_time = now

            # Read from PTY
            chunk = await loop.run_in_executor(
                self._session_mgr.pty_executor, lambda: session.read(timeout=2.0)
            )

            if chunk is None:
                continue  # no data yet
            if chunk == "":
                self._kill_session(thread_id)
                self._exit_codes[thread_id] = -1
                break

            # Meaningful output (contains newline) resets timeout + decision window
            if "\n" in chunk:
                last_output_time = time.monotonic()
                last_warn_time = 0.0
                decision_checked_for_window = False
                # Append chunk's lines to buffer (keep last 20 for detection context)
                for line in chunk.split("\n"):
                    if line.strip():
                        line_buffer.append(line)
                if len(line_buffer) > 20:
                    line_buffer = line_buffer[-20:]

            # Check for prompt marker → response complete
            clean = _ANSI_RE.sub("", chunk)
            if _PROMPT_RE.search(clean.rstrip()):
                before_prompt = _PROMPT_RE.split(clean.rstrip())[0]
                if before_prompt.strip():
                    yield before_prompt
                self._exit_codes[thread_id] = 0
                try:
                    self._session_mgr.transition(
                        thread_id, PtyState.IDLE, reason="response marker reached"
                    )
                except (KeyError, RuntimeError):
                    pass
                break

            yield chunk

    async def pipe_reply_stream(
        self,
        thread_id: int | None,
        reply: str,
        timeout_seconds: int | None = None,
    ) -> AsyncGenerator["str | DecisionPrompt", None]:
        """Pipe user reply to a waiting session and resume streaming.

        Called after execute_stream yielded DecisionPrompt and returned.
        New generator — pipes reply, yields remaining output until next
        prompt marker (or another DecisionPrompt for chained decisions).
        """
        lock = self._get_lock(thread_id)

        # Resolve the provider for this thread's session
        provider_id = self._session_providers.get(thread_id, self._provider.provider_id)
        provider = self.get_provider_for_thread(provider_id)

        async with lock:
            session = self._session_mgr.get(thread_id)
            if session is None or not session.alive:
                yield "❌ Session no longer available. Send a new message to start fresh.\n"
                return

            current_state = self._session_mgr.get_state(thread_id)
            if current_state != PtyState.WAITING_FOR_USER:
                logger.warning(
                    "[pipe_reply] unexpected state [thread=%s]: %s",
                    thread_id, current_state,
                )
            else:
                try:
                    self._session_mgr.transition(
                        thread_id, PtyState.PIPING_REPLY, reason="user reply"
                    )
                except (KeyError, RuntimeError) as exc:
                    logger.debug("[pipe_reply] transition skipped: %s", exc)

            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(
                    self._session_mgr.pty_executor,
                    lambda: session.write(reply + "\n"),
                )
            except OSError as exc:
                logger.warning("[pipe_reply] write failed [thread=%s]: %s", thread_id, exc)
                self._kill_session(thread_id)
                yield "❌ Failed to send reply to CLI. Session ended.\n"
                return

            logger.info("[pipe_reply] wrote reply [thread=%s]: %s", thread_id, reply[:80])

            try:
                self._session_mgr.transition(
                    thread_id, PtyState.STREAMING, reason="reply piped"
                )
            except (KeyError, RuntimeError):
                pass

            async for chunk in self._stream_pty_read_loop(
                session, thread_id, timeout_seconds=timeout_seconds,
                provider=provider,
            ):
                yield chunk

    async def _stream_pty_read_loop(
        self,
        session: PtySession,
        thread_id: int | None,
        timeout_seconds: int | None = None,
        provider: CliProvider | None = None,
    ) -> AsyncGenerator["str | DecisionPrompt", None]:
        """Shared read loop — reads output, detects decision prompts + response marker."""
        loop = asyncio.get_event_loop()
        effective_timeout = timeout_seconds or self._config.cli_timeout
        decision_idle_threshold = getattr(self._config, "decision_idle_threshold", 12)
        effective_provider = provider or self._provider

        deadline = time.monotonic() + effective_timeout
        last_output_time = time.monotonic()
        last_warn_time = 0.0
        paused_duration = 0.0
        pause_start: float | None = None
        line_buffer: list[str] = []
        decision_checked_for_window = False
        provider_patterns = getattr(effective_provider, "decision_prompt_patterns", None) or None

        while True:
            now = time.monotonic()
            current_state = self._session_mgr.get_state(thread_id)

            if current_state == PtyState.WAITING_FOR_USER:
                if pause_start is None:
                    pause_start = now
            else:
                if pause_start is not None:
                    paused_duration += now - pause_start
                    pause_start = None

            effective_deadline = deadline + paused_duration

            if now >= effective_deadline and current_state != PtyState.WAITING_FOR_USER:
                logger.warning("[read_loop] timeout [thread=%s]", thread_id)
                self._kill_session(thread_id)
                self._exit_codes[thread_id] = -1
                yield "\n⏱ Global timeout reached. Session killed.\n"
                return

            if not session.alive:
                self._exit_codes[thread_id] = -1
                if self._session_mgr.get(thread_id) is not None:
                    try:
                        self._session_mgr.transition(
                            thread_id, PtyState.DEAD, reason="process died"
                        )
                    except (KeyError, RuntimeError):
                        pass
                    self._session_mgr.kill(thread_id)
                yield "\n❌ CLI process died unexpectedly.\n"
                return

            idle_seconds = now - last_output_time - paused_duration

            if (
                current_state == PtyState.STREAMING
                and idle_seconds >= decision_idle_threshold
                and not decision_checked_for_window
            ):
                decision_checked_for_window = True
                try:
                    self._session_mgr.transition(
                        thread_id, PtyState.DETECTING_PROMPT,
                        reason=f"idle {int(idle_seconds)}s",
                    )
                except (KeyError, RuntimeError):
                    pass
                else:
                    decision = detect_decision_prompt(
                        line_buffer, provider_patterns=provider_patterns
                    )
                    if decision is not None:
                        try:
                            self._session_mgr.transition(
                                thread_id, PtyState.WAITING_FOR_USER,
                                reason=f"chained prompt: {decision.prompt_text[:40]}",
                            )
                        except (KeyError, RuntimeError):
                            pass
                        yield decision
                        return
                    else:
                        try:
                            self._session_mgr.transition(
                                thread_id, PtyState.STREAMING, reason="no prompt match"
                            )
                        except (KeyError, RuntimeError):
                            pass

            if (
                idle_seconds >= IDLE_WARN_INTERVAL
                and now - last_warn_time >= IDLE_WARN_INTERVAL
                and current_state != PtyState.WAITING_FOR_USER
            ):
                remaining = int(effective_deadline - now)
                yield f"\n⏳ Still working... no output for {int(idle_seconds)}s (timeout in {remaining}s)\n"
                last_warn_time = now

            chunk = await loop.run_in_executor(
                self._session_mgr.pty_executor, lambda: session.read(timeout=2.0)
            )

            if chunk is None:
                continue
            if chunk == "":
                self._kill_session(thread_id)
                self._exit_codes[thread_id] = -1
                break

            if "\n" in chunk:
                last_output_time = time.monotonic()
                last_warn_time = 0.0
                decision_checked_for_window = False
                for line in chunk.split("\n"):
                    if line.strip():
                        line_buffer.append(line)
                if len(line_buffer) > 20:
                    line_buffer = line_buffer[-20:]

            clean = _ANSI_RE.sub("", chunk)
            if _PROMPT_RE.search(clean.rstrip()):
                before_prompt = _PROMPT_RE.split(clean.rstrip())[0]
                if before_prompt.strip():
                    yield before_prompt
                self._exit_codes[thread_id] = 0
                try:
                    self._session_mgr.transition(
                        thread_id, PtyState.IDLE, reason="response marker reached"
                    )
                except (KeyError, RuntimeError):
                    pass
                break

            yield chunk

    async def _stream_non_interactive(
        self,
        prompt: str,
        *,
        thread_id: int | None,
        model: str | None,
        resume: bool,
        project_dir: str | None = None,
        provider: CliProvider | None = None,
    ) -> AsyncGenerator[str, None]:
        """Fallback: non-interactive single-shot execution.

        Args:
            project_dir: Per-thread cwd. If None, falls back to config.project_dir.
            provider: Per-thread CLI provider. If None, uses self._provider.
        """
        effective_provider = provider or self._provider
        args = effective_provider.build_args(prompt, model=model, resume=resume)
        env = effective_provider.build_env(os.environ.copy())
        cwd = project_dir if project_dir else self._config.project_dir

        # Validate cwd before spawn
        if not os.path.isdir(cwd):
            logger.error(
                "[Non-interactive] spawn failed [thread=%s]: cwd does not exist: %s",
                thread_id, cwd,
            )
            yield f"❌ Project directory does not exist: {cwd}\n"
            return

        logger.info("Non-interactive [thread=%s]: %s", thread_id, " ".join(args))
        logger.debug("[Non-interactive] spawn cwd [thread=%s]: %s", thread_id, cwd)

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            assert process.stdout is not None

            deadline = time.monotonic() + self._config.cli_timeout
            last_output_time = time.monotonic()
            last_warn_time = 0.0

            while True:
                now = time.monotonic()

                if now >= deadline:
                    try:
                        process.kill()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except (ProcessLookupError, asyncio.TimeoutError):
                        pass
                    self._exit_codes[thread_id] = -1
                    yield "\n⏱ Global timeout reached. Process killed.\n"
                    return

                idle_seconds = now - last_output_time
                if idle_seconds >= IDLE_WARN_INTERVAL and now - last_warn_time >= IDLE_WARN_INTERVAL:
                    remaining = int(deadline - now)
                    yield f"\n⏳ Still working... no output for {int(idle_seconds)}s (timeout in {remaining}s)\n"
                    last_warn_time = now

                try:
                    line_bytes = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=max(min(5, deadline - now), 1),
                    )
                except asyncio.TimeoutError:
                    if process.returncode is not None:
                        break
                    continue

                if not line_bytes:
                    break

                last_output_time = time.monotonic()
                last_warn_time = 0.0
                yield line_bytes.decode("utf-8", errors="replace")

            await process.wait()
            self._exit_codes[thread_id] = process.returncode or 0

            if process.stderr:
                stderr_bytes = await process.stderr.read()
                stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
                if stderr and (process.returncode or 0) != 0:
                    yield f"\n⚠️ Stderr:\n{stderr}\n"

        except FileNotFoundError:
            self._exit_codes[thread_id] = -1
            yield f"❌ CLI not found at: {effective_provider.config.cli_path}\n"
        except Exception as exc:
            self._exit_codes[thread_id] = -1
            yield f"❌ Unexpected error: {exc}\n"
            logger.exception("Stream error [thread=%s]", thread_id)

    # ── Cancel / Status / Models ─────────────────────────────────

    async def cancel(self, thread_id: int | None = None) -> bool:
        """Cancel the running CLI for a thread."""
        if thread_id in self._session_mgr._sessions:
            self._kill_session(thread_id)
            logger.info("Cancelled session [thread=%s]", thread_id)
            return True

        if thread_id is None and self._session_mgr._sessions:
            for tid in list(self._session_mgr._sessions.keys()):
                self._kill_session(tid)
            logger.info("Cancelled all sessions")
            return True

        return False

    async def check_status(self, provider_name: str | None = None) -> str:
        """Check if CLI is available."""
        import shutil
        provider = self.get_provider_for_thread(provider_name)
        path = provider.config.cli_path
        if not shutil.which(path) and not os.path.isfile(path):
            return f"❌ CLI not found: {path}"

        try:
            test_args = provider.status_check_args()
            process = await asyncio.create_subprocess_exec(
                *test_args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=provider.build_env(os.environ.copy()),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=15,
            )
            output = stdout.decode("utf-8", errors="replace").strip()
            if process.returncode == 0 and output:
                active = self._session_mgr.active_count()
                status = f"✅ {provider.name} ready\n\n{output}"
                if active:
                    status += f"\n\n⚡ Active sessions: {active}"
                return status
            err = stderr.decode("utf-8", errors="replace").strip()
            return f"⚠️ {provider.name} issue:\n{err or output or 'Unknown error'}"
        except Exception as exc:
            return f"❌ Error checking {provider.name}: {exc}"

    async def list_models(self, provider_name: str | None = None) -> list[dict]:
        """Fetch available models from CLI."""
        provider = self.get_provider_for_thread(provider_name)
        list_args = provider.list_models_args()
        if not list_args:
            return []

        try:
            process = await asyncio.create_subprocess_exec(
                *list_args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=provider.build_env(os.environ.copy()),
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=15,
            )
            return provider.parse_models_output(
                stdout.decode("utf-8", errors="replace")
            )
        except Exception as exc:
            logger.error("Failed to list models: %s", exc)
            return []
