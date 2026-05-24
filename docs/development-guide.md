# RemoClaw — Development Guide

## Prerequisites

- **Python 3.12+** (tested on 3.14)
- **Git**
- **One AI CLI** installed and logged in (Kiro, Claude Code, Gemini, or Codex)
- **Telegram Bot** created via [@BotFather](https://t.me/BotFather)

## Initial Setup

### Clone and Setup

```bash
git clone https://github.com/quangtam/remoclaw.git
cd remoclaw
```

### Option A: Setup Wizard (recommended)

**POSIX (macOS/Linux):**

```bash
bash setup.sh
```

**Windows:**

```cmd
setup.bat
```

The wizard will:

1. Verify Python 3.12+ is installed
2. Create `.venv/` virtual environment
3. Install dependencies from `requirements.txt`
4. Ask which CLI to use (Kiro/Claude/Gemini/Codex)
5. Check if the CLI binary is installed
6. Prompt for Telegram bot token and allowed user IDs
7. Prompt for project directory
8. Generate `.env` file

### Option B: Manual Setup

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your values
```

## Environment Configuration

Edit `.env` with these required variables:

```env
TELEGRAM_BOT_TOKEN=123456:ABC...
ALLOWED_USER_IDS=123456789
CLI_PROVIDER=kiro
PROJECT_DIR=/path/to/your/coding/project
```

See `.env.example` for full documentation of optional variables.

## Running Locally

### Start the bot (background)

**POSIX:**

```bash
./remoclaw start
```

**Windows:**

```cmd
remoclaw start
```

### Other commands

```bash
./remoclaw stop      # Stop the bot
./remoclaw restart   # Restart (handles 409 Conflict by waiting)
./remoclaw status    # Show PID and uptime
./remoclaw log       # Follow logs with tail -f
```

### Direct Python (foreground, useful for debugging)

```bash
.venv/bin/python remoclaw.py
```

## Logs

Logs are written to `remoclaw.log` (rolling append). Format:

```text
2026-05-05 12:34:56 [INFO] __main__: Starting RemoClaw
2026-05-05 12:34:56 [INFO] __main__: CLI Provider: kiro
```

Change verbosity via `LOG_LEVEL` in `.env`: `DEBUG`, `INFO`, `WARNING`, `ERROR`.

## Adding a New CLI Provider

Create a file in `cli_providers/`, e.g. `cli_providers/my_cli.py`:

```python
"""My CLI driver."""

from cli_providers.base import CliProvider


class MyCLIProvider(CliProvider):
    provider_id = "mycli"              # .env CLI_PROVIDER=mycli
    name = "My CLI"                    # Shown in Telegram
    default_cli_path = "mycli"         # PATH lookup

    def build_args(self, prompt, *, model=None, resume=False):
        args = [self.config.cli_path, "run"]
        if self.config.trust_all_tools:
            args.append("--yes")
        if model:
            args.extend(["--model", model])
        args.append(prompt)
        return args

    def build_env(self, base_env):
        env = self._base_env(base_env)  # sets NO_COLOR, TERM=dumb
        if self.config.api_key:
            env["MY_CLI_API_KEY"] = self.config.api_key
        return env
```

No other code changes needed. Registry auto-discovers on next restart.

### Optional provider features

```python
class MyCLIProvider(CliProvider):
    # Enable PTY interactive sessions:
    def build_interactive_args(self, *, model=None):
        return [self.config.cli_path, "interactive"]

    response_marker = "> "  # If your CLI prefixes responses

    # Enable model selection via /model command:
    def list_models_args(self):
        return [self.config.cli_path, "models", "--json"]

    def parse_models_output(self, stdout):
        import json
        return json.loads(stdout)

    # Custom health check:
    def status_check_args(self):
        return [self.config.cli_path, "whoami"]

    # Mark capabilities:
    def supports_resume(self): return True
    def supports_model_selection(self): return True
```

## Code Style

- **Python**: Follow PEP 8. Use type hints. Prefer `dataclass` for data containers.
- **Async**: All I/O-bound handlers are `async`. Blocking PTY reads go via `loop.run_in_executor()`.
- **Strings**: Prefer f-strings. Use double quotes consistently.
- **Imports**: Standard lib first, then third-party, then local. One per line.

## Testing

**Current state**: No automated test suite exists.

Manual testing workflow:

1. Start RemoClaw: `./remoclaw start`
2. Open your bot in Telegram
3. Send `/start` → should show welcome message
4. Send `/status` → should show CLI ready
5. Send a free-form message → should stream response
6. Send `/cancel` during streaming → should kill process
7. Send `/new` → send message → should start fresh session

## Debugging

### Bot not responding

1. `./remoclaw status` — verify process is running
2. `./remoclaw log` — check for errors
3. Verify `ALLOWED_USER_IDS` in `.env` matches your Telegram user ID
4. Test CLI binary directly: `kiro-cli whoami` (or equivalent)

### 409 Conflict error

Only one bot instance can poll a token. Causes:

- Multiple RemoClaw instances running
- Another bot using the same token

Fix: `./remoclaw restart` (waits 3s for Telegram to release)

### Stuck sessions

PTY processes surviving restart? Kill manually:

```bash
pgrep -f "kiro-cli chat" | xargs kill -9
```

### Stream timeout

Default `CLI_TIMEOUT=600` (10 min). Increase in `.env` if your tasks are slower:

```env
CLI_TIMEOUT=1200
```

## Git Workflow

```bash
# Feature branch
git checkout -b feat/my-feature

# Commit with conventional prefix
git commit -m "feat: add WhatsApp provider"
git commit -m "fix: PTY session leak on SIGKILL"
git commit -m "docs: update setup guide for Gemini"

# Push and open PR
git push -u origin feat/my-feature
```

## Release Process

```bash
# Tag release
git tag -a v1.0.2 -m "Release notes..."
git push origin v1.0.2

# Create GitHub release
gh release create v1.0.2 --title "..." --notes "..."
```

## Contributing

Contributions welcome. Priority areas:

1. New CLI providers (see above)
2. Additional chat platforms (WhatsApp, Zalo, Messenger, Slack)
3. Test suite (pytest + mock providers)
4. Windows PTY support (currently falls back to non-interactive)

Open an issue first for larger changes.
