# Voice Configuration

RemoClaw supports voice input (Whisper transcription) and voice output (TTS readback) — both with **dual-backend** auto-selection.

## Backends

The backend is chosen automatically based on whether `OPENAI_API_KEY` is set in `.env`:

| Feature | With `OPENAI_API_KEY` | Without (free) |
| ------- | -------------------- | -------------- |
| Transcription | OpenAI Whisper (cloud) | faster-whisper (local CPU) |
| TTS | OpenAI TTS (cloud) | edge-tts (Microsoft Edge, free) |
| Quality | Higher | Good |
| Cost | Per-token | Free |
| Latency | ~1–2s | ~1–3s |
| Internet | Required | Required for edge-tts; not for whisper |

Voice works out of the box with **zero configuration** using the free local backends.

## Voice input

Send a voice message in Telegram. RemoClaw transcribes it and shows a confirmation keyboard:

```text
🎤 Transcription: "check the test results"
[✅ Send] [✏️ Edit] [🗑️ Cancel]
```

**Auto-send mode** — set `VOICE_AUTO_SEND=true` in `.env` to skip the confirm keyboard and forward immediately.

## Voice output

When voice output is enabled, RemoClaw reads CLI responses aloud as voice messages.

Toggle per-thread:

```text
/voice          # toggle on/off (persisted in SQLite)
/voice status   # show current config
/voice speed 1.5  # set TTS speed (0.25–4.0)
/voice speed reset  # revert to global default
```

**Code-heavy detection** — voice output is automatically skipped for responses where >50% of the content is code blocks. This avoids reading walls of code aloud.

## Configuration

All `.env` settings (all optional):

```env
# Cloud backend (omit to use free local backends)
OPENAI_API_KEY=sk-...

# Voice output defaults
VOICE_OUTPUT_ENABLED=true              # Global default for new threads
VOICE_AUTO_SEND=false                  # Skip confirm keyboard for voice input

# OpenAI backend (when API key is set)
WHISPER_MODEL=gpt-4o-mini-transcribe   # OpenAI transcription model
WHISPER_TIMEOUT=10
TTS_MODEL=gpt-4o-mini-tts              # OpenAI TTS model
TTS_VOICE=coral                        # alloy, ash, coral, echo, fable, nova, onyx, sage, shimmer
TTS_SPEED=1.5                          # 0.25–4.0
TTS_TIMEOUT=10

# Local backend (when no API key)
WHISPER_LOCAL_MODEL=base                # tiny, base, small
TTS_LOCAL_VOICE=vi-VN-HoaiMyNeural      # edge-tts voice (any locale supported)
TTS_LOCAL_RATE=+30%                    # edge-tts rate (used when no per-thread override)
```

## Per-thread vs global config

`VOICE_OUTPUT_ENABLED` and `TTS_SPEED` are **resolved per-thread** through the 3-layer config chain:

1. Per-thread SQLite override (set via `/voice` or `/voice speed`)
2. Global `.env` value
3. Hardcoded default

This means you can have voice output on for personal threads and off for noisy debug threads, with the setting surviving bot restarts.

## Limitations

- **Voice speed** — fully supported on OpenAI TTS backend. On edge-tts, speed is converted to rate format (e.g. `1.5x → "+50%"`) which works but is less precise.
- **`/voice` toggle** — requires the thread to have been used at least once (so it has a `thread_config` row). For brand-new threads, the toggle works in-session but doesn't persist until you bind a project with `/project`.
- **Local Whisper** — first invocation downloads the model (~75MB for `base`), subsequent calls are fast.

## Choosing a Whisper model (local backend)

| Model | Size | Speed | Quality |
| ----- | ---- | ----- | ------- |
| `tiny` | 39MB | Fastest | OK for English |
| `base` | 74MB | Fast | Good (default) |
| `small` | 244MB | Moderate | Better, multilingual |

For Vietnamese transcription, `base` or `small` works well.
