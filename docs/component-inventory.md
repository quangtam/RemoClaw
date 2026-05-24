# RemoClaw — Component Inventory

## Python Modules

### Application Modules (root)

| Module | Purpose | Key Classes/Functions |
| ------ | ------- | --------------------- |
| `remoclaw.py` | Telegram handlers + orchestration | `main()`, `authorized()`, `cmd_*`, `handle_*`, `_execute_and_reply()` |
| `cli_runner.py` | Subprocess management | `CliRunner`, `CliResult`, `_PtySession` |
| `config.py` | Configuration loading | `Config` (frozen dataclass), `Config.from_env()` |
| `message_utils.py` | Output text pipeline | `format_output()`, `split_message()`, `strip_ansi()`, `extract_final_response()`, `markdown_to_telegram_html()` |

### CLI Provider Drivers (cli_providers/)

Each provider implements the `CliProvider` ABC from `base.py`. All have these class attributes:

- `provider_id` — key for `CLI_PROVIDER` env var
- `name` — human-readable name
- `default_cli_path` — default binary name for PATH lookup
- `response_marker` — optional prefix marking response start

| Provider | provider_id | CLI binary | Interactive | Model selection | Resume |
| -------- | ----------- | ---------- | ----------- | --------------- | ------ |
| `KiroProvider` | `kiro` | `kiro-cli` | ✅ PTY | ✅ (list-models) | ✅ |
| `ClaudeProvider` | `claude` | `claude` | ❌ (one-shot `-p`) | ✅ | ✅ |
| `GeminiProvider` | `gemini` | `gemini` | ❌ (one-shot `-p`) | ✅ | ❌ |
| `CodexProvider` | `codex` | `codex` | ❌ (one-shot `exec`) | ✅ | ❌ |

### Provider Infrastructure

| Module | Purpose |
| ------ | ------- |
| `cli_providers/__init__.py` | Public API re-exports |
| `cli_providers/base.py` | `CliProvider` ABC, `CliProviderConfig` dataclass |
| `cli_providers/registry.py` | Auto-discovery, `create_provider()` factory |

## Command Handlers (Telegram)

Registered in `remoclaw.py:main()`. All wrapped with `@authorized`.

| Command | Handler | Purpose |
| ------- | ------- | ------- |
| `/start` | `cmd_start` | Welcome message with project info |
| `/help` | `cmd_help` | Usage guide |
| `/model` | `cmd_model` | Show inline keyboard for model selection |
| `/skills` | `cmd_skills` | List BMAD skills from `.kiro/skills/` |
| `/status` | `cmd_status` | Check CLI availability + show active sessions |
| `/cancel` | `cmd_cancel` | Kill running CLI for current thread |
| `/new` | `cmd_new_session` | Reset session for current thread |
| `/resume` | `cmd_resume` | Explicitly resume previous session |
| `/bmad_*` | `handle_bmad_command` | Convert `/bmad_create_prd` → `/bmad-create-prd` forwarded to CLI |
| *(any text)* | `handle_message` | Free-form text forwarded to CLI |
| *(callback)* | `handle_model_callback` | Handle inline keyboard model selection |
| *(errors)* | `error_handler` | Log errors, notify user |

## Core Functions by Module

### `remoclaw.py`

| Function | Category | Role |
| -------- | -------- | ---- |
| `authorized(func)` | Decorator | Whitelist check via `ALLOWED_USER_IDS` |
| `_get_thread_id(update)` | Helper | Extract `message_thread_id` |
| `_should_resume(thread_id, context)` | Helper | Session resume logic |
| `_track_thread(thread_id)` | Helper | Increment thread message count |
| `_get_model(context)` | Helper | Get user-selected model |
| `_model_emoji(model_id)` | Helper | Emoji for model family |
| `_execute_and_reply` | Core | Orchestrates streaming CLI execution |
| `_execute_and_reply_inner` | Core | Inner implementation (after busy check) |
| `_escape_html(text)` | Helper | HTML entity escaping |
| `_send_html_with_fallback` | Helper | HTML with plain-text fallback |
| `main()` | Entry | Builds and runs Telegram Application |

### `cli_runner.py`

| Function | Role |
| -------- | ---- |
| `CliRunner.__init__` | Creates provider, initializes per-thread state dicts |
| `CliRunner.execute` | Non-streaming execution (unused by main flow, kept for compat) |
| `CliRunner.execute_stream` | Primary entry — async generator yielding output chunks |
| `CliRunner._get_or_create_session` | PTY session lifecycle |
| `CliRunner._spawn_pty` | Static — fork PTY in executor thread |
| `CliRunner._stream_pty` | PTY session streaming |
| `CliRunner._stream_non_interactive` | Fallback one-shot streaming |
| `CliRunner._kill_session` | Clean up PTY session |
| `CliRunner.cancel` | Kill specific or all sessions |
| `CliRunner.check_status` | Verify CLI binary + show active sessions |
| `CliRunner.list_models` | Fetch available models via provider |
| `_PtySession` | Wraps pid/fd pair, tracks aliveness |

### `message_utils.py`

| Function | Role |
| -------- | ---- |
| `strip_ansi(text)` | Remove ANSI/VT escape sequences |
| `markdown_to_telegram_html(text)` | MD → HTML (headings, tables, code, lists, links, blockquotes) |
| `_inline_markdown_to_html(text)` | Inline MD (bold, italic, strikethrough, code, links) |
| `split_message(text)` | Split >4096 char messages at natural boundaries |
| `_find_split_point(text)` | Choose best split point (prefers `</pre>`, paragraphs) |
| `extract_final_response(text)` | Strip tool noise, find `> ` marker, remove credits footer |
| `format_output(text, is_error)` | Full pipeline: ANSI strip → extract → MD→HTML |

### `config.py`

| Field | Source | Required | Default |
| ----- | ------ | -------- | ------- |
| `telegram_token` | `TELEGRAM_BOT_TOKEN` | ✅ | — |
| `allowed_user_ids` | `ALLOWED_USER_IDS` | ✅ | — |
| `cli_provider` | `CLI_PROVIDER` | ❌ | `kiro` |
| `cli_path` | `CLI_PATH` or `KIRO_CLI_PATH` | ❌ | Auto-detect |
| `cli_api_key` | `CLI_API_KEY`/`KIRO_API_KEY`/etc. | ❌ | `""` |
| `cli_extra_args` | `CLI_EXTRA_ARGS` | ❌ | `()` |
| `project_dir` | `PROJECT_DIR` | ✅ | — |
| `cli_timeout` | `CLI_TIMEOUT` | ❌ | `600` |
| `trust_all_tools` | `CLI_TRUST_ALL_TOOLS` | ❌ | `True` |
| `log_level` | `LOG_LEVEL` | ❌ | `INFO` |

## Shell/Batch Scripts

| Script | Platform | Commands |
| ------ | -------- | -------- |
| `remoclaw` | POSIX | `start`, `stop`, `restart`, `status`, `log` |
| `remoclaw.bat` | Windows | Same as above |
| `setup.sh` | POSIX | Interactive wizard: Python check, venv, deps, CLI choice, Telegram config, `.env` generation |
| `setup.bat` | Windows | Same as above |

## Constants and Tunable Values

| Constant | Location | Value | Purpose |
| -------- | -------- | ----- | ------- |
| `MAX_MSG_LEN` | `remoclaw.py` | 4096 | Telegram message limit |
| `_STREAM_UPDATE_INTERVAL` | `remoclaw.py` | 1.5 | Seconds between message edits |
| `_STREAM_PREVIEW_MAX` | `remoclaw.py` | 3000 | Max preview chars |
| `_TYPING_KEEPALIVE_INTERVAL` | `remoclaw.py` | 4.0 | Seconds between typing indicators |
| `IDLE_WARN_INTERVAL` | `cli_runner.py` | 30 | Seconds before idle warning |
| `_INIT_TIMEOUT` | `cli_runner.py` | 60 | PTY init deadline |
| `MAX_MESSAGE_LENGTH` | `message_utils.py` | 4096 | Split threshold |
| `CONTINUATION_OVERHEAD` | `message_utils.py` | 40 | Reserve for `📄 [N/M]` marker |

## External Dependencies

| Package | Version | Purpose |
| ------- | ------- | ------- |
| `python-telegram-bot` | 21.10 | Async Telegram Bot API |
| `python-dotenv` | 1.1.0 | `.env` file loading |

## Runtime Artifacts

| Artifact | Created By | Purpose |
| -------- | ---------- | ------- |
| `.venv/` | `setup.sh`/`setup.bat` | Python virtual environment |
| `.env` | `setup.sh`/`setup.bat` | Secrets and config |
| `remoclaw.log` | `remoclaw` script | Logs from `nohup` redirect |
| `.remoclaw.pid` | `remoclaw` script | PID file for process tracking |
| `__pycache__/` | Python | Bytecode cache |
