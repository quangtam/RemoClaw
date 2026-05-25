"""AutoExecutor — bridges AutoRunner to Telegram + CliRunner.

AutoRunner (auto/runner.py) is pure logic. AutoExecutor implements the
Executor protocol by talking to:
  - CliRunner: actually executes BMAD skills via the per-thread CLI
  - db: persists thread_config (model switches) and reads project context
  - Telegram: gate prompts, ask_once questions, party-mode routing

A single AutoExecutor instance is created per-run (per /auto invocation).
It holds references to the bot, runner, and originating chat/thread so
follow-up messages land in the right place.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from auto.findings import extract_findings_summary, has_findings

if TYPE_CHECKING:
    from auto.flow import FlowStep
    from auto.runner import StepResult
    from auto.state import AutoState

logger = logging.getLogger(__name__)

# Throttle Telegram message edits to stay under rate limits (~30/min/chat).
# 1.5s gives us 40/min headroom while still feeling responsive.
_STREAM_EDIT_INTERVAL = 1.5

# Hard cap on Telegram message length (4096 in API; we leave headroom for HTML).
_TELEGRAM_MSG_CAP = 3800

# Max chars of trailing output to show in the streaming preview.
_STREAM_PREVIEW_TAIL = 1500


# Module-level futures dict — keyed by (thread_id, step_id) → future
# Set by callback handlers (gate approve/reject buttons, ask_once yes/no)
# and awaited by AutoExecutor.ask_human_review / ask_once.
_pending_user_decisions: dict[tuple[int, str], asyncio.Future[bool]] = {}


def resolve_pending_decision(
    thread_id: int, step_id: str, *, approved: bool,
) -> bool:
    """Called by Telegram callback handlers when user clicks a button.

    Returns True if a pending decision was resolved, False if there wasn't
    one waiting (e.g. timed out, double-click, etc.).
    """
    key = (thread_id, step_id)
    fut = _pending_user_decisions.pop(key, None)
    if fut is None or fut.done():
        return False
    fut.set_result(approved)
    return True


def has_pending_decision(thread_id: int, step_id: str) -> bool:
    return (thread_id, step_id) in _pending_user_decisions


@dataclass
class AutoExecutor:
    """Telegram-aware executor. Implements auto.runner.Executor protocol."""

    bot: object  # telegram.Bot — kept loose to avoid import cycle
    chat_id: int
    runner_ref: object  # cli_runner.CliRunner
    config_ref: object  # config.Config
    db_ref: object      # db module
    db_path: str
    project_dir_resolver: object | None = None  # callable(thread_id) -> str | None
    decision_timeout: int = 86400  # 24h — autonomous runs may pause overnight
    # Optional: AutoState reference. When set, the executor writes
    # current_step_started_at + last_progress_line into it during long-running
    # skill calls so /auto status can show real-time progress without waiting
    # for the step to finish.
    progress_state: object | None = None
    # Persist callback: async (state_row: dict) -> None. Called when progress
    # fields are updated, throttled to once every ~5s to avoid hammering SQLite.
    progress_persist: object | None = None

    # Captured output from the most recent skill run — used by builtins like
    # 'present-summary' that summarize what happened.
    _last_output: str = field(default="", init=False)

    # ── Skill execution ──────────────────────────────────────────

    async def run_skill(
        self, *,
        thread_id: int,
        skill: str,
        model: str | None,
        new_session: bool,
        prompt_override: str | None = None,
        timeout_seconds: int | None = None,
    ) -> "StepResult":
        """Run a BMAD skill, capturing output for findings detection.

        The skill is invoked exactly the way a user would — by sending its
        slash form (e.g. `bmad-create-prd`) to the CLI. We don't reach
        inside the skill's internal steps.

        If `prompt_override` is provided (e.g. user's intent for quick-dev),
        we send `<skill> <prompt>` instead of just the skill name.

        `timeout_seconds`: per-step CLI runtime cap. If None, falls back to
        the global config.cli_timeout. Skill steps in autonomous mode are
        long-running, so callers (AutoRunner) usually pass 1800+.

        Streams progress to Telegram via a single message that gets edited
        as new lines arrive — same pattern as normal /chat messages, but
        with a "🤖 step name" header so users can see which step is running.
        """
        from auto.runner import StepResult

        # Resolve tier (strong/balanced/fast) → provider-specific model.
        # If `model` is already a real model name, this returns it unchanged.
        provider = self.runner_ref.get_provider_for_thread(
            await self._resolve_provider_name(thread_id),
        )
        resolved_model = provider.resolve_tier(model)

        # Apply per-thread overrides for this step
        if resolved_model:
            try:
                await self.db_ref.upsert_thread_config(
                    thread_id, model=resolved_model, path=self.db_path,
                )
            except Exception as exc:
                logger.warning("[auto] model upsert failed: %s", exc)

        if new_session:
            try:
                await self.runner_ref.cancel(thread_id)
            except Exception as exc:
                logger.warning("[auto] cancel failed: %s", exc)

        if prompt_override:
            prompt = f"{skill}\n\n{prompt_override}"
        else:
            prompt = skill

        # Send the initial "Running" message that we'll edit as output streams
        stream_msg = await self._send_stream_header(skill, thread_id)

        # Mark step start in shared state so /auto status can show elapsed time
        from datetime import datetime, timezone
        await self._mark_step_started(skill)

        # Resolve project_dir for this thread
        project_dir = None
        if self.project_dir_resolver:
            try:
                project_dir = await self.project_dir_resolver(thread_id)
            except Exception:
                project_dir = None

        collected: list[str] = []
        last_edit = 0.0
        last_progress_persist = 0.0

        try:
            async for line in self.runner_ref.execute_stream(
                prompt,
                thread_id=thread_id,
                model=resolved_model,
                resume=not new_session,
                project_dir=project_dir,
                timeout_seconds=timeout_seconds,
            ):
                if isinstance(line, str):
                    collected.append(line)
                    # Throttled edit so we don't hit Telegram rate limit
                    import time
                    now = time.monotonic()
                    if now - last_edit >= _STREAM_EDIT_INTERVAL:
                        await self._edit_stream_message(stream_msg, skill, "".join(collected))
                        last_edit = now
                    # Update last_progress_line every ~5s — cheap heartbeat
                    # for /auto status to surface "still working, last said: …"
                    if now - last_progress_persist >= 5.0:
                        await self._update_progress(line)
                        last_progress_persist = now
                else:
                    logger.warning("[auto] unexpected DecisionPrompt during autonomous skill")
                    await self._mark_step_finished()
                    await self._finalize_stream_message(
                        stream_msg, skill,
                        "❌ CLI asked for interactive input — auto mode can't answer",
                    )
                    return StepResult(
                        success=False,
                        error="CLI asked for interactive input — auto mode can't answer",
                    )
        except asyncio.CancelledError:
            await self._mark_step_finished()
            raise
        except Exception as exc:
            logger.exception("[auto] skill %s crashed", skill)
            await self._mark_step_finished()
            await self._finalize_stream_message(stream_msg, skill, f"💥 {exc}")
            return StepResult(success=False, error=str(exc))

        output = "".join(collected)
        self._last_output = output
        await self._mark_step_finished()

        # Did the CLI exit with a non-zero code?
        exit_code = self.runner_ref.get_exit_code(thread_id)
        if exit_code != 0:
            await self._finalize_stream_message(
                stream_msg, skill,
                f"❌ Exit code {exit_code}\n\n{_truncate(output, 800)}",
            )
            return StepResult(
                success=False,
                error=f"skill exited with code {exit_code}",
                notes=_truncate(output, 400),
            )

        findings = has_findings(output)

        # Final edit — show the complete output (truncated to keep msg short)
        status_icon = "🎉 findings" if findings else "✅"
        await self._finalize_stream_message(
            stream_msg, skill,
            f"{status_icon}\n\n{_truncate(output, 1500)}",
        )

        return StepResult(
            success=True,
            findings=findings,
            notes=extract_findings_summary(output) if findings else _truncate(output, 200),
        )

    # ── Builtins ─────────────────────────────────────────────────

    async def run_builtin(
        self, *, thread_id: int, builtin: str, state: "AutoState",
    ) -> "StepResult":
        from auto.runner import StepResult

        if builtin == "present-summary":
            await self._present_summary(thread_id, state)
            return StepResult(success=True, notes="summary presented")

        return StepResult(
            success=False, error=f"unknown builtin: {builtin}",
        )

    async def _present_summary(self, thread_id: int, state: "AutoState") -> None:
        """Wrap up the run with a quick recap of what happened."""
        from html import escape

        lines = [
            f"🏁 <b>{escape(state.flow_id)}</b> finished",
            "",
            "<b>Steps:</b>",
        ]
        for record in state.history:
            icon = {
                "success": "✅",
                "failed": "❌",
                "skipped": "⏭️",
                "in-progress": "⏳",
            }.get(record.status, "•")
            line = f"{icon} <code>{escape(record.step_id)}</code>"
            if record.attempts > 1:
                line += f" (×{record.attempts})"
            if record.findings:
                line += f" — findings, party-mode ×{record.party_mode_rounds}"
            lines.append(line)

        await self._notify("\n".join(lines), thread_id)

    # ── User interaction ─────────────────────────────────────────

    async def ask_human_review(
        self, *, thread_id: int, step: "FlowStep",
        timeout_seconds: int | None = None,
    ) -> bool:
        """Pause and ask the user to approve before continuing.

        Sends a message with [Approve] / [Abort] inline keyboard, then
        awaits a button click via the module-level _pending_user_decisions
        future. Returns True on approve, False on abort/timeout.

        `timeout_seconds`: how long to wait for the user. Defaults to
        the executor's `decision_timeout` (24h for autonomous runs).
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from html import escape

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"auto:approve:{step.id}"),
            InlineKeyboardButton("❌ Abort", callback_data=f"auto:abort:{step.id}"),
        ]])

        text = (
            f"⏸ <b>Review needed</b>\n"
            f"Step <code>{escape(step.id)}</code> finished. "
            f"Approve to continue, abort to stop the run."
        )
        await self._send_with_keyboard(text, keyboard, thread_id)

        return await self._await_decision(
            thread_id, step.id,
            timeout=timeout_seconds or self.decision_timeout,
        )

    async def ask_once(
        self, *, thread_id: int, step: "FlowStep", prompt: str,
        timeout_seconds: int | None = None,
    ) -> bool:
        """Ask a yes/no question for `optional` steps (asked at most once
        per run thanks to AutoState.ask_once_asked tracking)."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from html import escape

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes", callback_data=f"auto:approve:{step.id}"),
            InlineKeyboardButton("⏭️ Skip", callback_data=f"auto:abort:{step.id}"),
        ]])
        text = f"❓ <b>Optional step</b>\n{escape(prompt)}"
        await self._send_with_keyboard(text, keyboard, thread_id)
        return await self._await_decision(
            thread_id, step.id,
            timeout=timeout_seconds or self.decision_timeout,
        )

    async def run_party_mode(
        self, *, thread_id: int, context: str, min_rounds: int,
        timeout_seconds: int | None = None,
    ) -> int:
        """Trigger party-mode discussion via the bmad-party-mode skill.

        The skill itself orchestrates the rounds. We just kick it off with
        the findings as input, stream the output back, and trust the skill
        to do its thing. Returns the number of rounds executed (best-effort
        — for now we trust the skill to honor min_rounds).
        """
        prompt = (
            f"bmad-party-mode\n\n"
            f"Discuss the following review findings. Run at least {min_rounds} "
            f"rounds; reach a recommendation by the end.\n\n"
            f"---\n{context}\n---"
        )

        # Use the same streaming path as run_skill so users see the debate
        stream_msg = await self._send_stream_header("bmad-party-mode", thread_id)

        collected: list[str] = []
        last_edit = 0.0
        import time

        try:
            async for line in self.runner_ref.execute_stream(
                prompt,
                thread_id=thread_id,
                resume=True,  # stay in same session
                timeout_seconds=timeout_seconds,
            ):
                if not isinstance(line, str):
                    continue
                collected.append(line)
                now = time.monotonic()
                if now - last_edit >= _STREAM_EDIT_INTERVAL:
                    await self._edit_stream_message(
                        stream_msg, "bmad-party-mode", "".join(collected),
                    )
                    last_edit = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[auto] party-mode failed: %s", exc)
            await self._finalize_stream_message(
                stream_msg, "bmad-party-mode",
                f"❌ Party-mode crashed: {exc}",
            )
            return 0

        output = "".join(collected)
        await self._finalize_stream_message(
            stream_msg, "bmad-party-mode",
            f"✅ {min_rounds}+ rounds complete\n\n{_truncate(output, 1200)}",
        )
        return min_rounds

    async def list_pending_stories(self, *, thread_id: int) -> list[str]:
        """Read sprint-status.yaml to find stories that still need work.

        Looks for `<project_dir>/docs/implementation-artifacts/sprint-status.yaml`
        (the BMAD convention) and returns ids whose status is not yet in a
        terminal state. Anything other than `done` (or unrecognized) counts
        as pending.

        Returning an empty list signals "no work" and the runner skips the
        surrounding loop entirely.
        """
        project_dir = await self._project_dir(thread_id)
        if not project_dir:
            return []

        from pathlib import Path
        candidates = [
            Path(project_dir) / "docs" / "implementation-artifacts" / "sprint-status.yaml",
            Path(project_dir) / "docs" / "implementation-artifacts" / "sprint-status.yml",
        ]
        sprint_file: Path | None = None
        for c in candidates:
            if c.is_file():
                sprint_file = c
                break
        if sprint_file is None:
            return []

        try:
            import yaml  # local import — yaml is already a dep via flow.py
            with sprint_file.open("r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
        except Exception as exc:
            logger.warning("[auto] failed to read %s: %s", sprint_file, exc)
            return []

        statuses = doc.get("development_status") or {}
        if not isinstance(statuses, dict):
            return []

        pending: list[str] = []
        # BMAD convention: keys like '6-1-voice-input-whisper' are stories,
        # 'epic-N' keys roll up to the epic level — skip those.
        for story_id, status in statuses.items():
            if not isinstance(story_id, str) or not isinstance(status, str):
                continue
            if story_id.startswith("epic-"):
                continue
            if status == "done":
                continue
            pending.append(story_id)
        return pending

    async def artifact_exists(
        self, *, thread_id: int, path: str,
    ) -> bool:
        """Whether `path` (relative to project_dir) exists.

        Absolute paths are honored as-is so flows can reference shared
        artifacts outside the project root if they want.
        """
        from pathlib import Path
        p = Path(path)
        if not p.is_absolute():
            project_dir = await self._project_dir(thread_id)
            if not project_dir:
                return False
            p = Path(project_dir) / path
        return p.exists()

    async def is_validation_passed(
        self, *, thread_id: int, skill: str,
    ) -> bool:
        """Heuristic: has the validation report been produced cleanly?

        For `bmad-validate-prd`: look for a validation report under
        `docs/planning-artifacts/` containing `validationStatus: COMPLETE`.

        For `bmad-check-implementation-readiness`: look for a readiness
        report containing a comparable `status: ready` / `readinessStatus`
        marker.

        Anything we can't confidently interpret returns False, which means
        "run the validator anyway" — the safe default.
        """
        project_dir = await self._project_dir(thread_id)
        if not project_dir:
            return False

        from pathlib import Path
        planning = Path(project_dir) / "docs" / "planning-artifacts"
        if not planning.is_dir():
            return False

        # Pick the right glob + marker for this skill. We deliberately
        # accept several name variations because BMAD skills produce report
        # filenames using project-name slugs we can't predict here.
        if skill == "bmad-validate-prd":
            patterns = ["*prd-validation*.md", "*validation-report*.md", "*-validation.md"]
            markers = ["validationStatus: COMPLETE", "validationStatus: complete"]
        elif skill == "bmad-check-implementation-readiness":
            patterns = ["*readiness-report*.md", "*readiness*.md"]
            markers = ["readinessStatus: COMPLETE", "readinessStatus: complete", "readiness: ready"]
        else:
            return False

        for pattern in patterns:
            for report in planning.glob(pattern):
                try:
                    text = report.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                if any(m in text for m in markers):
                    return True
        return False

    async def _project_dir(self, thread_id: int) -> str | None:
        """Resolve the project directory bound to this thread.

        Falls back to the bot's default project_dir if the thread isn't
        bound. Returns None if we can't determine one — callers should
        treat that as "no project" and degrade gracefully.
        """
        if self.project_dir_resolver:
            try:
                pd = await self.project_dir_resolver(thread_id)
                if pd:
                    return pd
            except Exception:
                pass
        # Try thread_config directly
        try:
            tc = await self.db_ref.get_thread_config(thread_id, path=self.db_path)
            if tc and tc.project_dir:
                return tc.project_dir
        except Exception:
            pass
        # Last resort: cwd
        import os
        return os.getcwd()

    # ── Internal helpers ─────────────────────────────────────────

    async def _resolve_provider_name(self, thread_id: int) -> str | None:
        """Look up which provider this thread is using (for tier resolution)."""
        try:
            tc = await self.db_ref.get_thread_config(thread_id, path=self.db_path)
            if tc and tc.cli_provider:
                return tc.cli_provider
        except Exception:
            pass
        # Fall back to global default (config.cli_provider)
        return getattr(self.config_ref, "cli_provider", None)

    async def _mark_step_started(self, skill: str) -> None:
        """Set current_step_started_at so /auto status can show elapsed time."""
        if self.progress_state is None or self.progress_persist is None:
            return
        from datetime import datetime, timezone
        self.progress_state.current_step_started_at = datetime.now(timezone.utc).isoformat()
        self.progress_state.last_progress_line = f"starting {skill}…"
        try:
            await self.progress_persist(self.progress_state.to_db_row())
        except Exception as exc:
            logger.debug("[auto] progress persist (start) failed: %s", exc)

    async def _mark_step_finished(self) -> None:
        """Clear current_step_started_at when step ends (success/fail/cancel)."""
        if self.progress_state is None or self.progress_persist is None:
            return
        self.progress_state.current_step_started_at = None
        self.progress_state.last_progress_line = None
        try:
            await self.progress_persist(self.progress_state.to_db_row())
        except Exception as exc:
            logger.debug("[auto] progress persist (finish) failed: %s", exc)

    async def _update_progress(self, line: str) -> None:
        """Update last_progress_line with the freshest non-empty CLI output line.

        Best-effort: silenced exceptions so a slow SQLite write never blocks
        the streaming loop.
        """
        if self.progress_state is None or self.progress_persist is None:
            return
        # Strip ANSI + spinner noise so the heartbeat is human-readable
        try:
            from message_utils import strip_ansi, strip_streaming_noise
            clean = strip_streaming_noise(strip_ansi(line)).strip()
        except Exception:
            clean = line.strip()
        if not clean:
            return
        # Take the last non-empty line — usually the most informative
        last = clean.splitlines()[-1] if "\n" in clean else clean
        self.progress_state.last_progress_line = last[:200]
        try:
            await self.progress_persist(self.progress_state.to_db_row())
        except Exception as exc:
            logger.debug("[auto] progress persist (update) failed: %s", exc)

    async def _await_decision(
        self, thread_id: int, step_id: str,
        *, timeout: int | None = None,
    ) -> bool:
        """Wait for the user to click a gate button, with timeout."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        key = (thread_id, step_id)
        _pending_user_decisions[key] = fut

        wait_seconds = timeout or self.decision_timeout
        try:
            return await asyncio.wait_for(fut, timeout=wait_seconds)
        except asyncio.TimeoutError:
            _pending_user_decisions.pop(key, None)
            await self._notify(
                f"⏱ Decision timeout after {wait_seconds // 60} min — aborting",
                thread_id,
            )
            return False

    async def _notify(self, text: str, thread_id: int | None) -> None:
        """Send a status message to the user. Best-effort — never raises."""
        try:
            await self.bot.send_message(  # type: ignore[attr-defined]
                chat_id=self.chat_id,
                message_thread_id=thread_id if thread_id else None,
                text=text,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("[auto] notify failed: %s", exc)

    async def _send_stream_header(self, skill: str, thread_id: int | None):
        """Send the initial 'running' message we'll keep editing as output arrives.

        Returns the Message object so we can edit it later. Returns None if
        send fails (we'll degrade gracefully — no streaming, just final result).
        """
        from html import escape
        try:
            return await self.bot.send_message(  # type: ignore[attr-defined]
                chat_id=self.chat_id,
                message_thread_id=thread_id if thread_id else None,
                text=(
                    f"⚙️ <b>Running</b> <code>{escape(skill)}</code>\n"
                    f"<i>Streaming output…</i>"
                ),
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("[auto] stream header send failed: %s", exc)
            return None

    async def _edit_stream_message(self, msg, skill: str, output_so_far: str) -> None:
        """Edit the streaming message with the latest tail of output.

        Stripping ANSI is delegated to message_utils to share logic with the
        regular streaming path. Failures are silenced — we'd rather drop an
        edit than crash the run.
        """
        if msg is None:
            return
        from html import escape
        try:
            from message_utils import strip_ansi, strip_streaming_noise
        except ImportError:
            strip_ansi = lambda s: s
            strip_streaming_noise = lambda s: s

        clean = strip_streaming_noise(strip_ansi(output_so_far))
        tail = clean[-_STREAM_PREVIEW_TAIL:].strip()
        # If we truncated the head, show an ellipsis at the start
        if len(clean) > _STREAM_PREVIEW_TAIL:
            tail = f"…\n{tail}"

        text = (
            f"⚙️ <b>Running</b> <code>{escape(skill)}</code>\n"
            f"<pre>{escape(tail) or '(starting…)'}</pre>"
        )
        if len(text) > _TELEGRAM_MSG_CAP:
            text = text[:_TELEGRAM_MSG_CAP] + "…</pre>"

        try:
            await msg.edit_text(text, parse_mode="HTML")
        except Exception as exc:
            # 'message is not modified' is fine; rate-limit hits will retry next interval
            logger.debug("[auto] stream edit skipped: %s", exc)

    async def _finalize_stream_message(self, msg, skill: str, summary: str) -> None:
        """Replace the streaming message with a final compact result line."""
        if msg is None:
            return
        from html import escape
        text = (
            f"<b>{escape(skill)}</b>\n"
            f"<pre>{escape(summary[:_TELEGRAM_MSG_CAP - 200])}</pre>"
        )
        try:
            await msg.edit_text(text, parse_mode="HTML")
        except Exception as exc:
            logger.debug("[auto] finalize edit skipped: %s", exc)

    async def _send_with_keyboard(
        self, text: str, keyboard, thread_id: int | None,
    ) -> None:
        try:
            await self.bot.send_message(  # type: ignore[attr-defined]
                chat_id=self.chat_id,
                message_thread_id=thread_id if thread_id else None,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.warning("[auto] send_with_keyboard failed: %s", exc)


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "…"
