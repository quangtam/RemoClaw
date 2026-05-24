"""RemoClaw Core Engine — platform-agnostic business logic.

Handles CLI execution, streaming, session management, decision forwarding,
voice processing, and command routing. Platform adapters call into this.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from config import Config
from cli_runner import CliRunner
from cli_providers import get_available_providers
from message_utils import (
    format_output,
    split_message,
    strip_ansi,
    extract_final_response,
    strip_streaming_noise,
    detect_screenshots,
    is_code_heavy,
)
from session_manager import DecisionPrompt
import db
from db import DEFAULT_THREAD_ID, DB_PATH

logger = logging.getLogger(__name__)


@dataclass
class StreamEvent:
    """Events yielded during CLI streaming to the platform adapter."""

    type: str  # "text", "status", "decision", "done", "error", "screenshots", "voice"
    text: str | None = None
    html: str | None = None
    decision_prompt: DecisionPrompt | None = None
    screenshot_paths: list[str] = field(default_factory=list)
    voice_bytes: bytes | None = None


class CoreEngine:
    """Platform-agnostic core that drives CLI sessions.

    Platform adapters create one CoreEngine and call its methods
    to handle commands and messages.
    """

    def __init__(self, config: Config):
        self.config = config
        self.runner = CliRunner(config)
        self._thread_sessions: dict[str | None, int] = {}
        self._thread_tasks: dict[str | None, asyncio.Task] = {}

        # Voice services (auto-select backend)
        self.voice_transcriber = None
        self.voice_synthesizer = None
        if config.voice_enabled:
            try:
                from voice import VoiceTranscriber, VoiceSynthesizer

                self.voice_transcriber = VoiceTranscriber(
                    api_key=config.openai_api_key,
                    model=config.whisper_model,
                    timeout=config.whisper_timeout,
                    local_model=config.whisper_local_model,
                )
                self.voice_synthesizer = VoiceSynthesizer(
                    api_key=config.openai_api_key,
                    model=config.tts_model,
                    voice=config.tts_voice,
                    speed=config.tts_speed,
                    timeout=config.tts_timeout,
                    local_voice=config.tts_local_voice,
                    local_rate=config.tts_local_rate,
                )
            except ImportError as exc:
                logger.warning("Voice disabled: %s", exc)

    async def init(self) -> None:
        """Initialize DB and background tasks."""
        await db.init_db(DB_PATH, default_project_dir=self.config.project_dir)
        logger.info("Core engine initialized")

    def is_authorized(self, user_id: str) -> bool:
        """Check if user is allowed."""
        try:
            return int(user_id) in self.config.allowed_user_ids
        except (ValueError, TypeError):
            return False

    def is_busy(self, thread_id: str | None) -> bool:
        """Check if a thread has a running request."""
        return self.runner.is_busy(thread_id)

    async def execute_stream(
        self,
        prompt: str,
        thread_id: str | None,
        *,
        model: str | None = None,
        resume: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        """Execute a prompt and yield StreamEvents for the platform to render.

        This is the main streaming interface. The platform adapter iterates
        over events and renders them appropriately.
        """
        # Resolve per-thread config
        try:
            resolved = await db.resolve_thread_config(
                thread_id if thread_id is not None else DEFAULT_THREAD_ID,
                env_project_dir=self.config.project_dir,
                env_cli_provider=self.config.cli_provider,
                env_model=model,
                env_timeout_seconds=self.config.cli_timeout,
                path=DB_PATH,
            )
            resolved_timeout = resolved.timeout_seconds
            resolved_model = resolved.model or model
            resolved_project_dir = resolved.project_dir
            resolved_provider_name = resolved.cli_provider
        except Exception:
            resolved_timeout = self.config.cli_timeout
            resolved_model = model
            resolved_project_dir = self.config.project_dir
            resolved_provider_name = None

        effective_provider = self.runner.get_provider_for_thread(resolved_provider_name)
        response_marker = effective_provider.response_marker
        response_started = not response_marker

        raw_lines: list[str] = []

        async for line in self.runner.execute_stream(
            prompt,
            thread_id=thread_id,
            model=resolved_model,
            resume=resume,
            timeout_seconds=resolved_timeout,
            project_dir=resolved_project_dir,
            provider_name=resolved_provider_name,
        ):
            if isinstance(line, DecisionPrompt):
                yield StreamEvent(type="decision", decision_prompt=line)
                return

            raw_lines.append(line)
            clean = strip_streaming_noise(strip_ansi(line)).rstrip()
            if not clean:
                continue

            if response_marker and not response_started and clean.startswith(response_marker):
                response_started = True
                clean = clean[len(response_marker):]

            if response_started:
                yield StreamEvent(type="text", text=clean)
            else:
                yield StreamEvent(type="status", text=clean[:100])

        # Final output
        full_output = "".join(raw_lines)
        final_html = format_output(full_output)
        chunks = split_message(final_html)

        if not chunks or all(not c.strip() for c in chunks):
            yield StreamEvent(type="error", text="CLI returned no output.")
            return

        yield StreamEvent(type="done", html="\n".join(chunks))

        # Screenshots
        scan_text = extract_final_response(full_output) or full_output
        paths = detect_screenshots(scan_text)
        if paths:
            yield StreamEvent(type="screenshots", screenshot_paths=paths)

        # Voice (if enabled)
        if self.voice_synthesizer and not is_code_heavy(scan_text):
            plain = _strip_html_for_tts(final_html)
            if plain and len(plain) > 10:
                audio = await self.voice_synthesizer.synthesize(plain)
                if audio:
                    yield StreamEvent(type="voice", voice_bytes=audio)

        self._thread_sessions[thread_id] = self._thread_sessions.get(thread_id, 0) + 1

    async def pipe_decision_reply(
        self, thread_id: str | None, reply: str
    ) -> AsyncIterator[StreamEvent]:
        """Pipe user's decision reply to CLI and stream remaining output."""
        try:
            resolved = await db.resolve_thread_config(
                thread_id if thread_id is not None else DEFAULT_THREAD_ID,
                env_project_dir=self.config.project_dir,
                env_cli_provider=self.config.cli_provider,
                env_timeout_seconds=self.config.cli_timeout,
                path=DB_PATH,
            )
            timeout = resolved.timeout_seconds
        except Exception:
            timeout = self.config.cli_timeout

        raw_lines: list[str] = []
        async for line in self.runner.pipe_reply_stream(
            thread_id=thread_id, reply=reply, timeout_seconds=timeout
        ):
            if isinstance(line, DecisionPrompt):
                yield StreamEvent(type="decision", decision_prompt=line)
                return
            raw_lines.append(line)
            clean = strip_streaming_noise(strip_ansi(line)).rstrip()
            if clean:
                yield StreamEvent(type="text", text=clean)

        full_output = "".join(raw_lines)
        final_html = format_output(full_output)
        chunks = split_message(final_html)
        if chunks and any(c.strip() for c in chunks):
            yield StreamEvent(type="done", html="\n".join(chunks))

    async def transcribe_voice(self, audio_path: str) -> str | None:
        """Transcribe voice file to text."""
        if not self.voice_transcriber:
            return None
        return await self.voice_transcriber.transcribe(audio_path)

    async def synthesize_voice(self, text: str, speed: float | None = None) -> bytes | None:
        """Convert text to speech audio bytes."""
        if not self.voice_synthesizer:
            return None
        return await self.voice_synthesizer.synthesize(text, speed=speed)

    def get_available_providers(self) -> list[str]:
        """List available CLI providers."""
        return get_available_providers()

    def get_session_count(self, thread_id: str | None) -> int:
        """Get message count for a thread."""
        return self._thread_sessions.get(thread_id, 0)

    def should_resume(self, thread_id: str | None) -> bool:
        """Whether to resume an existing session."""
        return self._thread_sessions.get(thread_id, 0) > 0

    async def cancel_thread(self, thread_id: str | None) -> bool:
        """Cancel running task for a thread. Returns True if cancelled."""
        task = self._thread_tasks.get(thread_id)
        if task and not task.done():
            task.cancel()
            return True
        # Also kill PTY
        self.runner._session_mgr.kill(thread_id)
        return True

    async def cleanup_loop(self) -> None:
        """Background cleanup of idle sessions."""
        while True:
            try:
                await asyncio.sleep(self.config.cleanup_interval)
                killed = self.runner._session_mgr.cleanup_idle(
                    max_age_seconds=self.config.idle_session_max_age
                )
                if killed:
                    logger.info("[cleanup] killed %d idle sessions", killed)
                orphans = self.runner._session_mgr.cleanup_orphans()
                if orphans:
                    logger.warning("[cleanup] killed %d orphans", orphans)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("[cleanup] error: %s", exc)

    def shutdown(self) -> None:
        """Shutdown session manager."""
        self.runner._session_mgr.shutdown()


def _strip_html_for_tts(html_text: str) -> str:
    """Strip HTML tags for TTS input."""
    import re
    text = re.sub(r"<[^>]+>", "", html_text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text.strip()
