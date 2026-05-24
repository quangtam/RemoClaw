"""Voice features for RemoClaw v2.0 (Growth phase).

Handles:
- Voice message transcription — OpenAI Whisper (cloud) or faster-whisper (local)
- TTS response synthesis — OpenAI TTS (cloud) or edge-tts (free, Microsoft Edge)

Backend selection is automatic:
- If OPENAI_API_KEY is set → use OpenAI cloud (higher quality, costs money)
- Otherwise → use local/free backends (faster-whisper + edge-tts, no API key needed)

Both backends expose the same interface so callers don't need to know which is active.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Transcription ─────────────────────────────────────────────────────────────


class VoiceTranscriber:
    """Transcribe voice audio to text.

    Uses OpenAI Whisper API when an API key is available, otherwise falls back
    to faster-whisper running locally (no API key, no cost).

    All failures collapse to ``None`` so callers degrade gracefully.
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "gpt-4o-mini-transcribe",
        timeout: int = 10,
        local_model: str = "base",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._local_model = local_model
        self._use_openai = bool(api_key)

        # Lazy-init: clients created on first use to avoid import cost at startup.
        self._openai_client = None
        self._local_model_instance = None

        if self._use_openai:
            logger.info("[voice] transcription backend: OpenAI Whisper (%s)", model)
        else:
            logger.info("[voice] transcription backend: faster-whisper local (%s)", local_model)

    def _get_openai_client(self):
        if self._openai_client is None:
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(api_key=self._api_key, timeout=self._timeout)
        return self._openai_client

    def _get_local_model(self):
        """Load faster-whisper model (cached after first load)."""
        if self._local_model_instance is None:
            from faster_whisper import WhisperModel
            # cpu + int8 = fastest on CPU, works on both Mac and Linux
            self._local_model_instance = WhisperModel(
                self._local_model, device="cpu", compute_type="int8"
            )
            logger.info("[voice] faster-whisper model loaded: %s", self._local_model)
        return self._local_model_instance

    async def transcribe(self, audio_path: str | Path) -> str | None:
        """Transcribe an audio file to text.

        Args:
            audio_path: Path to the audio file (OGG, MP3, WAV, etc.).

        Returns:
            Transcribed text (stripped), or ``None`` on any failure.
        """
        if self._use_openai:
            return await self._transcribe_openai(str(audio_path))
        else:
            return await self._transcribe_local(str(audio_path))

    async def _transcribe_openai(self, audio_path: str) -> str | None:
        """Transcribe via OpenAI Whisper API."""
        try:
            with open(audio_path, "rb") as audio_file:
                response = await asyncio.wait_for(
                    self._get_openai_client().audio.transcriptions.create(
                        model=self._model,
                        file=audio_file,
                        response_format="text",
                    ),
                    timeout=self._timeout,
                )
        except FileNotFoundError:
            logger.warning("[voice] audio file missing: %s", audio_path)
            return None
        except asyncio.TimeoutError:
            logger.warning("[voice] OpenAI transcription timed out after %ds", self._timeout)
            return None
        except Exception as exc:
            logger.error("[voice] OpenAI transcription failed: %s", exc)
            return None

        text = response if isinstance(response, str) else getattr(response, "text", "")
        text = (text or "").strip()
        if not text:
            return None
        logger.info("[voice] OpenAI transcription complete: %d chars", len(text))
        return text

    async def _transcribe_local(self, audio_path: str) -> str | None:
        """Transcribe via faster-whisper running on local CPU."""
        try:
            if not Path(audio_path).exists():
                logger.warning("[voice] audio file missing: %s", audio_path)
                return None

            # faster-whisper is synchronous — run in thread pool to avoid blocking.
            loop = asyncio.get_event_loop()
            text = await asyncio.wait_for(
                loop.run_in_executor(None, self._run_local_transcription, audio_path),
                timeout=self._timeout,
            )
            if not text:
                return None
            logger.info("[voice] local transcription complete: %d chars", len(text))
            return text
        except asyncio.TimeoutError:
            logger.warning("[voice] local transcription timed out after %ds", self._timeout)
            return None
        except Exception as exc:
            logger.error("[voice] local transcription failed: %s", exc)
            return None

    def _run_local_transcription(self, audio_path: str) -> str | None:
        """Synchronous faster-whisper call (runs in thread pool)."""
        try:
            model = self._get_local_model()
            segments, _info = model.transcribe(audio_path, beam_size=1)
            text = " ".join(seg.text for seg in segments).strip()
            return text if text else None
        except Exception as exc:
            logger.error("[voice] faster-whisper error: %s", exc)
            return None


# ── TTS Synthesis ─────────────────────────────────────────────────────────────


class VoiceSynthesizer:
    """Synthesize text to OGG Opus audio for Telegram voice messages.

    Uses OpenAI TTS API when an API key is available, otherwise falls back
    to edge-tts (Microsoft Edge TTS — free, no API key, needs internet).

    All failures collapse to ``None`` so callers degrade gracefully.
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "gpt-4o-mini-tts",
        voice: str = "coral",
        speed: float = 1.5,
        timeout: int = 10,
        local_voice: str = "vi-VN-HoaiMyNeural",
        local_rate: str = "+30%",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._speed = max(0.25, min(4.0, speed))  # clamp to API limits
        self._timeout = timeout
        self._local_voice = local_voice
        self._local_rate = local_rate
        self._use_openai = bool(api_key)

        self._openai_client = None

        if self._use_openai:
            logger.info("[voice] TTS backend: OpenAI (%s / %s / %.1fx)", model, voice, self._speed)
        else:
            logger.info("[voice] TTS backend: edge-tts (%s / %s)", local_voice, local_rate)

    def _get_openai_client(self):
        if self._openai_client is None:
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(api_key=self._api_key, timeout=self._timeout)
        return self._openai_client

    async def synthesize(self, text: str, *, speed: float | None = None) -> bytes | None:
        """Synthesize text to OGG Opus audio bytes.

        Args:
            text: Text to synthesize (truncated to 4096 chars).
            speed: Optional per-call speed override (0.25–4.0). Falls back to
                   the instance default set at construction time.

        Returns:
            OGG Opus audio bytes ready for Telegram ``sendVoice``, or ``None`` on failure.
        """
        if not text or not text.strip():
            return None

        if len(text) > 4096:
            text = text[:4093] + "..."

        effective_speed = max(0.25, min(4.0, speed)) if speed is not None else self._speed

        if self._use_openai:
            return await self._synthesize_openai(text, speed=effective_speed)
        else:
            return await self._synthesize_edge(text, speed=effective_speed)

    async def _synthesize_openai(self, text: str, *, speed: float | None = None) -> bytes | None:
        """Synthesize via OpenAI TTS API."""
        effective_speed = speed if speed is not None else self._speed
        try:
            response = await asyncio.wait_for(
                self._get_openai_client().audio.speech.create(
                    model=self._model,
                    voice=self._voice,
                    input=text,
                    speed=effective_speed,
                    response_format="opus",
                ),
                timeout=self._timeout,
            )
            audio_bytes = response.content
            if not audio_bytes:
                return None
            logger.info("[voice] OpenAI TTS complete: %d bytes", len(audio_bytes))
            return audio_bytes
        except asyncio.TimeoutError:
            logger.warning("[voice] OpenAI TTS timed out after %ds", self._timeout)
            return None
        except Exception as exc:
            logger.error("[voice] OpenAI TTS failed: %s", exc)
            return None

    async def _synthesize_edge(self, text: str, *, speed: float | None = None) -> bytes | None:
        """Synthesize via edge-tts (Microsoft Edge TTS, free, no API key).

        edge-tts outputs MP3 by default. Telegram's sendVoice accepts OGG Opus
        but also accepts MP3 — we send MP3 directly to avoid needing ffmpeg.

        Args:
            text: Text to synthesize.
            speed: Optional speed override (0.25–4.0). Converted to edge-tts
                   rate format (e.g., 1.5 → "+50%", 0.8 → "-20%").
        """
        try:
            import edge_tts

            # Convert speed float to edge-tts rate string (percentage offset from 1.0)
            if speed is not None:
                pct = int((speed - 1.0) * 100)
                rate = f"{pct:+d}%" if pct != 0 else "+0%"
            else:
                rate = self._local_rate

            # edge-tts writes to a file; use a temp file then read bytes back.
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp_path = tmp.name

            try:
                communicate = edge_tts.Communicate(text, self._local_voice, rate=rate)
                await asyncio.wait_for(
                    communicate.save(tmp_path),
                    timeout=self._timeout,
                )
                audio_bytes = Path(tmp_path).read_bytes()
                if not audio_bytes:
                    return None
                logger.info("[voice] edge-tts complete: %d bytes", len(audio_bytes))
                return audio_bytes
            finally:
                try:
                    Path(tmp_path).unlink()
                except OSError:
                    pass

        except asyncio.TimeoutError:
            logger.warning("[voice] edge-tts timed out after %ds", self._timeout)
            return None
        except Exception as exc:
            logger.error("[voice] edge-tts failed: %s", exc)
            return None
