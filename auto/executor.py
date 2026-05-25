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
    decision_timeout: int = 1800  # 30 min default

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
    ) -> "StepResult":
        """Run a BMAD skill, capturing output for findings detection.

        The skill is invoked exactly the way a user would — by sending its
        slash form (e.g. `bmad-create-prd`) to the CLI. We don't reach
        inside the skill's internal steps.

        If `prompt_override` is provided (e.g. user's intent for quick-dev),
        we send `<skill> <prompt>` instead of just the skill name.
        """
        from auto.runner import StepResult

        # Apply per-thread overrides for this step
        if model:
            try:
                await self.db_ref.upsert_thread_config(
                    thread_id, model=model, path=self.db_path,
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

        await self._notify(f"⚙️ <i>Running</i> <code>{skill}</code>", thread_id)

        # Resolve project_dir for this thread
        project_dir = None
        if self.project_dir_resolver:
            try:
                project_dir = await self.project_dir_resolver(thread_id)
            except Exception:
                project_dir = None

        collected: list[str] = []
        timed_out = False
        try:
            async for line in self.runner_ref.execute_stream(
                prompt,
                thread_id=thread_id,
                model=model,
                resume=not new_session,
                project_dir=project_dir,
            ):
                # Best-effort string capture — DecisionPrompts are rare in
                # autonomous mode (we use yolo/auto-approve flags), but if
                # one shows up we treat it as a failure to avoid hanging.
                if isinstance(line, str):
                    collected.append(line)
                else:
                    logger.warning("[auto] unexpected DecisionPrompt during autonomous skill")
                    return StepResult(
                        success=False,
                        error="CLI asked for interactive input — auto mode can't answer",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[auto] skill %s crashed", skill)
            return StepResult(success=False, error=str(exc))

        output = "".join(collected)
        self._last_output = output

        # Did the CLI exit with a non-zero code?
        exit_code = self.runner_ref.get_exit_code(thread_id)
        if exit_code != 0:
            return StepResult(
                success=False,
                error=f"skill exited with code {exit_code}",
                notes=_truncate(output, 400),
            )

        findings = has_findings(output)
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
    ) -> bool:
        """Pause and ask the user to approve before continuing.

        Sends a message with [Approve] / [Abort] inline keyboard, then
        awaits a button click via the module-level _pending_user_decisions
        future. Returns True on approve, False on abort/timeout.
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

        return await self._await_decision(thread_id, step.id)

    async def ask_once(
        self, *, thread_id: int, step: "FlowStep", prompt: str,
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
        return await self._await_decision(thread_id, step.id)

    async def run_party_mode(
        self, *, thread_id: int, context: str, min_rounds: int,
    ) -> int:
        """Trigger party-mode discussion via the bmad-party-mode skill.

        The skill itself orchestrates the rounds. We just kick it off with
        the findings as input and trust the skill to do its thing.
        """
        await self._notify(
            f"🎉 <i>Diverting to party-mode</i> — {min_rounds} rounds minimum",
            thread_id,
        )

        prompt = (
            f"bmad-party-mode\n\n"
            f"Discuss the following review findings. Run at least {min_rounds} "
            f"rounds; reach a recommendation by the end.\n\n"
            f"---\n{context}\n---"
        )

        try:
            async for line in self.runner_ref.execute_stream(
                prompt,
                thread_id=thread_id,
                resume=True,  # stay in same session
            ):
                if not isinstance(line, str):
                    continue
                # Stream party-mode output to user verbatim — they should see it
                pass  # output already streams via existing mechanism (TODO Sprint D)
        except Exception as exc:
            logger.warning("[auto] party-mode failed: %s", exc)
            return 0

        return min_rounds

    async def list_pending_stories(self, *, thread_id: int) -> list[str]:
        """Read sprint-status.yaml to find next story to implement.

        Sprint A scope: returns empty list (loop expansion is Sprint C).
        """
        return []

    # ── Internal helpers ─────────────────────────────────────────

    async def _await_decision(self, thread_id: int, step_id: str) -> bool:
        """Wait for the user to click a gate button, with timeout."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        key = (thread_id, step_id)
        _pending_user_decisions[key] = fut

        try:
            return await asyncio.wait_for(fut, timeout=self.decision_timeout)
        except asyncio.TimeoutError:
            _pending_user_decisions.pop(key, None)
            await self._notify(
                f"⏱ Decision timeout after {self.decision_timeout // 60} min — aborting",
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
