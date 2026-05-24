# RemoClaw — Architecture

## Executive Summary

RemoClaw is a single-process async Python bot that:

1. Receives messages from Telegram via long polling (`python-telegram-bot`)
2. Routes messages to a per-thread CLI subprocess (persistent PTY session when possible)
3. Streams subprocess stdout back to Telegram with progressive message edits
4. Formats final response as Telegram HTML

## High-Level Architecture

```text
┌─────────────────┐      ┌──────────────────────────────────────────┐
│  Telegram User  │◄────►│  Telegram Bot API (long polling)         │
└─────────────────┘      └──────────────────┬───────────────────────┘
                                            │
                                  ┌─────────▼──────────┐
                                  │    remoclaw.py        │
                                  │  (handlers + auth) │
                                  └─────────┬──────────┘
                                            │
                                  ┌─────────▼──────────┐
                                  │   cli_runner.py    │
                                  │   (CliRunner)      │
                                  │                    │
                                  │  Per-thread state: │
                                  │   - _sessions      │
                                  │   - _locks         │
                                  │   - _exit_codes    │
                                  └─────────┬──────────┘
                                            │
                                  ┌─────────▼──────────┐
                                  │  cli_providers/    │
                                  │   (pluggable)      │
                                  │                    │
                                  │  kiro.py           │
                                  │  claude.py         │
                                  │  gemini.py         │
                                  │  codex.py          │
                                  └─────────┬──────────┘
                                            │
                                  ┌─────────▼──────────┐
                                  │  PTY subprocess    │
                                  │  (interactive)     │
                                  │                    │
                                  │  kiro-cli chat     │
                                  │  claude -p ...     │
                                  │  gemini -p ...     │
                                  │  codex exec ...    │
                                  └────────────────────┘
```

## Data Flow

### Incoming Message → Response

```text
1. User sends message to Telegram bot
2. python-telegram-bot polls getUpdates → invokes handle_message
3. @authorized decorator checks update.effective_user.id against whitelist
4. _execute_and_reply extracts thread_id from message_thread_id
5. CliRunner.execute_stream(prompt, thread_id=X):
   a. Acquires per-thread asyncio.Lock (serializes same-thread messages)
   b. Gets or creates _PtySession for this thread
      - If new: forks PTY via pty.fork(), waits for !> prompt
      - If existing: reuses alive session
   c. Writes prompt + \n to PTY fd
   d. Reads PTY output via select.select() (runs in thread executor)
   e. Yields chunks until next !> prompt marker
6. Bot edits streaming message every 1.5s with preview
7. Background typing keepalive task sends ChatAction.TYPING every 4s
8. After stream ends:
   - format_output() strips ANSI, extracts response, converts MD → HTML
   - split_message() breaks into 4096-char chunks
   - Sends final messages via reply_text(parse_mode=HTML)
```

## Architecture Patterns

### 1. Pluggable Driver Pattern (Strategy)

Each CLI is a driver file in `cli_providers/` implementing `CliProvider` ABC:

- `provider_id` — unique key used in `.env CLI_PROVIDER`
- `build_args(prompt, model, resume)` — CLI command construction
- `build_env(base_env)` — provider-specific env vars
- `build_interactive_args(model)` — optional, enables PTY sessions
- `response_marker` — stdout prefix that separates tool noise from response

Registry in `cli_providers/registry.py` auto-discovers via `pkgutil.iter_modules()` on first access. Adding a new provider = creating one file.

### 2. Per-Thread State Isolation

`CliRunner` maintains per-`thread_id` dictionaries:

```python
_sessions:   dict[int | None, _PtySession]  # one PTY per thread
_locks:      dict[int | None, asyncio.Lock]  # sequential per thread
_exit_codes: dict[int | None, int]          # last exit per thread
```

`thread_id` comes from Telegram's `message_thread_id` (forum topic). `None` represents main chat. Different threads run parallel CLI subprocesses; same-thread messages queue via lock.

### 3. Persistent PTY Sessions

Cold-starting `kiro-cli chat --no-interactive` takes ~6-10s (MCP init, session setup). To avoid this per-message cost, RemoClaw spawns an **interactive** CLI via `pty.fork()` and keeps it alive:

- Write prompts to PTY fd via `os.write()`
- Read responses via `select.select()` + `os.read()` (runs in executor thread)
- Detect response completion by matching `!>` prompt regex
- Subsequent messages reuse the session → ~3s per message

Falls back to `--no-interactive` one-shot if provider doesn't implement `build_interactive_args()`.

### 4. Streaming with Rate-Limited Edits

Telegram API limits message edits to ~30/min/chat. RemoClaw:

- Sends one initial "⏳ Connecting..." message
- Buffers stream output in `preview_buffer`
- Edits message with latest preview every 1.5s (`_STREAM_UPDATE_INTERVAL`)
- Truncates preview to 3000 chars, keeps last N lines
- Deletes preview message on completion, sends final formatted response

### 5. Watchdog + Keepalive

Two concurrent tasks protect against stuck processes and user UX:

- **Idle watchdog** in `cli_runner.py`: warns user every 30s if no PTY output, kills on global timeout (600s default, configurable)
- **Typing keepalive** in `remoclaw.py`: background `asyncio.Task` sends `ChatAction.TYPING` every 4s, independent of stream loop

## Component Responsibilities

### `remoclaw.py` (Main Entry)

- Loads config via `Config.from_env()`
- Builds `python-telegram-bot` `Application` with `concurrent_updates(True)`
- Registers command handlers: `/start /help /model /skills /status /cancel /new /resume`
- Registers BMAD regex handler (`^/bmad_\w+`) that converts underscores → hyphens
- Registers free-form text handler
- `_execute_and_reply` is the core streaming loop
- `@authorized` decorator gates all handlers on `ALLOWED_USER_IDS`

### `cli_runner.py` (Subprocess Manager)

- Creates `CliProvider` instance from config
- `execute_stream()` is the primary entry — async generator yielding output chunks
- `_PtySession` wraps pid/fd pair, tracks aliveness via `os.kill(pid, 0)`
- `_get_or_create_session()` manages session lifecycle
- `_stream_pty()` / `_stream_non_interactive()` are the two execution paths
- `cancel(thread_id)` kills specific thread or all

### `config.py` (Configuration)

- Immutable frozen `@dataclass` loaded from `.env` via `python-dotenv`
- Validates required env vars (raises `ValueError` on missing)
- Falls back through multiple env var names for API keys:
  `CLI_API_KEY` → `KIRO_API_KEY` → `ANTHROPIC_API_KEY` → `GEMINI_API_KEY` → `OPENAI_API_KEY`

### `message_utils.py` (Output Formatting)

Pipeline: `format_output(text) = strip_ansi → extract_final_response → markdown_to_telegram_html`

- `strip_ansi()` — comprehensive regex for CSI/OSC/charset-selection/bare-CSI sequences
- `extract_final_response()` — finds the `> ` marker (for Kiro), strips tool invocations, thinking indicators, credits footer
- `markdown_to_telegram_html()` — converts MD headings/tables/code/lists to Telegram HTML
- `split_message()` — splits long messages at paragraph → line → space → hard-cut boundaries

### `cli_providers/` (Pluggable Drivers)

- `base.py` — `CliProvider` ABC with required/optional methods
- `registry.py` — auto-discovery via `pkgutil.iter_modules()`
- `__init__.py` — re-exports public API
- `kiro.py` — Kiro CLI (interactive mode supported, has response_marker)
- `claude.py` — Claude Code (`-p` flag)
- `gemini.py` — Gemini CLI (`-p` flag, no sandbox)
- `codex.py` — OpenAI Codex (`exec` subcommand, full-auto)

## Concurrency Model

- **Event loop**: single asyncio event loop per Python process
- **concurrent_updates(True)** in `python-telegram-bot` allows handlers to run concurrently
- **Per-thread locks**: `asyncio.Lock` per `thread_id` ensures same-thread messages queue
- **Different threads**: fully parallel — separate PTY subprocesses
- **Blocking I/O**: PTY `select.select()` and `os.read()` run in `loop.run_in_executor()` to avoid blocking event loop

## Security

- **Whitelist auth**: `ALLOWED_USER_IDS` in `.env`; `@authorized` decorator rejects others
- **Local login**: CLIs authenticate via their own mechanism (browser OAuth), RemoClaw doesn't handle credentials
- **API key fallback**: optional env vars for headless/SSH machines without browser
- **Secrets**: `.env` is gitignored; `.env.example` provides template
- **Tool trust**: `CLI_TRUST_ALL_TOOLS=true` by default — user controls this per-provider

## Error Handling

- `ValueError` on missing required env vars → bot fails to start with clear message
- `FileNotFoundError` when CLI binary missing → user-facing error message
- `asyncio.TimeoutError` in subprocess → kills process, reports timeout
- HTML parse errors → fallback to plain text via regex strip of tags
- PTY `BrokenPipeError` → kills session, prompts user to retry

## Performance Characteristics

| Scenario | Latency |
| -------- | ------- |
| First message (cold start, PTY init) | ~6-10s |
| Subsequent messages (warm PTY) | ~3-4s (depends on model) |
| Message edit during stream | ~1.5s interval |
| Typing indicator | Every 4s |
| Global timeout | 600s (configurable) |
| Idle warning | Every 30s after first idle period |

## Testing Strategy

**Current state**: No automated tests exist. Manual testing via Telegram.

**Recommended future additions**:

- Unit tests for `message_utils.py` (pure functions, easy to test)
- Mock `CliProvider` implementations for `cli_runner.py` tests
- Integration tests with a fake Telegram Bot API server

## Deployment Architecture

RemoClaw is designed as a **long-running process on a single machine**:

- No external dependencies (database, message queue, cache)
- State is in-memory (thread sessions reset on restart)
- Single bot instance per Telegram token (409 Conflict otherwise)
- PTY sessions require POSIX — Windows uses non-interactive fallback

See [Deployment Guide](./deployment-guide.md) for production setup.

## Known Limitations

- Single `PROJECT_DIR` per bot instance (no `/project` switch command)
- In-memory thread sessions reset on restart
- Interactive PTY mode requires POSIX (macOS/Linux); Windows falls back to non-interactive
- Telegram 4096-char message limit (auto-split, but breaks long code blocks)
- Only one bot instance can poll the same token at a time
