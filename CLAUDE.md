# RemoClaw — Agent Context

## What Is This?

RemoClaw — remote control any AI coding agent from your phone. A Python bot that bridges messaging apps to AI coding CLIs (Kiro, Claude Code, Gemini, Codex) in headless mode. Standalone project.

## Architecture

```text
Chat App User -> RemoClaw (python-telegram-bot)
  -> CliProvider.build_args() -> subprocess (streaming stdout)
  -> strip ANSI -> extract response -> Markdown to HTML -> chat reply
```

## File Structure

```text
remoclaw.py             # Main entry: handlers, auth guard, streaming, command routing
remoclaw                # Bash management script (start/stop/restart/status/log)
remoclaw.bat            # Windows management script
config.py               # Config dataclass from .env (multi-CLI aware)
cli_runner.py           # CliRunner: subprocess wrapper, streaming, model listing
session_manager.py      # PTY session pool, lifecycle, state machine
db.py                   # SQLite persistence: thread_config, schema migration, resolve_thread_config
voice.py                # Whisper transcription + TTS synthesis (dual-backend)
message_utils.py        # ANSI strip, response extraction, MD→HTML, splitting, code-heavy detection

cli_providers/          # Pluggable CLI driver package (auto-discovery)
  __init__.py           # Re-exports: CliProvider, create_provider, get_available_providers
  base.py               # CliProvider ABC + CliProviderConfig dataclass
  registry.py           # Auto-scans cli_providers/*.py, registers by provider_id
  kiro.py               # Kiro CLI driver
  claude.py             # Claude Code driver
  gemini.py             # Gemini CLI driver
  codex.py              # OpenAI Codex driver

.env                    # Secrets (not committed)
.env.example            # Template
requirements.txt        # python-telegram-bot, aiosqlite, openai, edge-tts, faster-whisper
```

## How to Run

```bash
./remoclaw start        # start in background
./remoclaw restart      # kill + wait + start
./remoclaw status       # check PID
./remoclaw log          # tail -f remoclaw.log
```

## Tech Stack

- Python 3.12+
- python-telegram-bot 21.10 (async)
- aiosqlite (per-thread persistence)
- openai (cloud Whisper + TTS, optional)
- faster-whisper + edge-tts (free local backends)

## Key Design Decisions

1. **Pluggable CLI providers** — `cli_providers/` package with auto-discovery. Each driver is a single file with a CliProvider subclass. Registry scans on import via pkgutil.iter_modules. Adding a new CLI = one file, zero changes elsewhere.

2. **Streaming output** — `execute_stream()` reads stdout line-by-line via asyncio.subprocess. Bot sends one message and edits it every 1.5s (respects Telegram rate limit of 30 edits/min). Tool activity shown as status line; response content shown as pre preview. Final formatted message sent after process exits.

3. **Response extraction** — Some CLIs (e.g. Kiro) prefix the actual response with `> `. The `extract_final_response()` function finds this marker and strips everything before it (trust warnings, tool invocations, spawn logs, credits footer). Providers with no marker (Claude, Gemini, Codex) treat all output as response.

4. **Thread-based sessions** — `message_thread_id` maps to session state. First message = new session; subsequent = `--resume`. `/new` resets. Tracked in `_thread_sessions` dict (in-memory) + `thread_config` table (SQLite, persistent).

5. **3-layer config resolution** — Per-thread SQLite → global `.env` → hardcoded defaults. `resolve_thread_config()` in `db.py` enforces this. Lets users bind threads to different projects, providers, models, and voice settings.

6. **PTY session pool** — `SessionManager` keeps long-running CLI processes per thread. State machine: IDLE → BUSY → WAITING_FOR_USER (decision prompt) → IDLE. Idle cleanup runs on `cleanup_interval`. Concurrent sessions across threads supported.

7. **Decision forwarding** — When CLI emits a Y/n prompt or file selection, generator yields a `DecisionPrompt`. RemoClaw forwards it to Telegram, sets `pending_decision` flag. Next user message in that thread is piped back to the CLI process via PTY stdin.

8. **Voice dual-backend** — `voice.py` auto-selects: if `OPENAI_API_KEY` set → OpenAI Whisper + TTS; else → faster-whisper (local CPU) + edge-tts (free Microsoft Edge). Same interface, callers don't know which backend is active.

9. **Model selection** — `/model` fetches models via `provider.list_models_args()` + `parse_models_output()`. Inline keyboard, per-thread storage in SQLite.

10. **Markdown to Telegram HTML** — Pipeline: strip ANSI → extract response → convert MD to HTML (b, pre, code, i, blockquote, a). Falls back to plain text on parse error.

11. **subprocess stdin=DEVNULL** (legacy CliRunner path) — prevents SIGTTIN (stopped) when bot runs via nohup.

12. **Auth guard** — `@authorized` decorator checks `update.effective_user.id` against `ALLOWED_USER_IDS` whitelist.

13. **BMAD routing** — Telegram commands use underscores (`/bmad_create_prd`), CLI skills use hyphens (`/bmad-create-prd`). Auto-converted.

14. **Management script** — `./remoclaw {start|stop|restart|status|log}` with PID file tracking, graceful shutdown, polling release wait.

## Environment Variables

| Variable | Required | Description |
| --- | --- | --- |
| TELEGRAM_BOT_TOKEN | Yes | From @BotFather |
| ALLOWED_USER_IDS | Yes | Comma-separated Telegram user IDs |
| CLI_PROVIDER | No | kiro (default), claude, gemini, codex |
| CLI_PATH | No | Override CLI binary path |
| CLI_API_KEY | No | API key (only for headless/SSH machines) |
| CLI_TIMEOUT | No | Default: 600s |
| CLI_TRUST_ALL_TOOLS | No | Default: true |
| CLI_EXTRA_ARGS | No | Space-separated extra CLI flags |
| PROJECT_DIR | Yes | Default working directory for CLI subprocess |
| LOG_LEVEL | No | Default: INFO |
| MAX_SESSIONS | No | Concurrent session pool size, default: 5 |
| IDLE_SESSION_MAX_AGE | No | Seconds before idle session is killed, default: 1800 |
| CLEANUP_INTERVAL | No | Seconds between cleanup sweeps, default: 300 |
| DECISION_REPLY_TIMEOUT | No | Seconds to wait for decision reply, default: 1800 |
| OPENAI_API_KEY | No | Enables cloud voice backends (Whisper + TTS) |
| VOICE_OUTPUT_ENABLED | No | Global default for voice output, default: false |
| VOICE_AUTO_SEND | No | Skip confirm keyboard for voice input, default: false |
| TTS_SPEED | No | TTS playback speed 0.25–4.0, default: 1.5 |
| TTS_VOICE | No | OpenAI voice (coral default) |
| WHISPER_LOCAL_MODEL | No | faster-whisper model: tiny/base/small, default: base |

## Adding a CLI Provider

Create `cli_providers/newcli.py` with a `CliProvider` subclass. Set `provider_id`, `name`, `default_cli_path`. Implement `build_args()` and `build_env()`. Registry auto-discovers it. Set `CLI_PROVIDER=newcli` in `.env` and restart.

## Known Issues

- 409 Conflict if multiple instances poll the same token
- Voice speed control is most precise on OpenAI TTS; edge-tts uses rate-percent mapping
- AI IDEs without a headless CLI (Cursor, Windsurf, VS Code) aren't supported — RemoClaw bridges to CLI agents only
