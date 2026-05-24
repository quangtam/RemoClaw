# Usage Guide

Full reference for using RemoClaw — commands, chat patterns, and workflows.

## Commands

### Session

| Command | Description |
| ------- | ----------- |
| `/start` | Welcome message |
| `/help` | Full command reference |
| `/new` | Start fresh session (kills current) |
| `/cancel` | Kill running CLI process |
| `/resume` | Resume previous session |
| `/info` | Current session details (project, model, status, voice) |
| `/sessions` | List all active sessions across threads |

### Configuration

| Command | Description |
| ------- | ----------- |
| `/model` | Select AI model (inline keyboard) |
| `/project <path>` | Bind thread to a project directory |
| `/projects` | Browse and switch between previous projects |
| `/provider <name>` | Switch CLI provider for this thread |
| `/voice` | Toggle voice output on/off |
| `/voice status` | Show voice configuration for this thread |
| `/voice speed 1.5` | Set TTS playback speed (0.25–4.0) |

### Status & Tools

| Command | Description |
| ------- | ----------- |
| `/status` | CLI health check |
| `/git` | Git info — branch, log, status, diff |
| `/git log` | Last 15 commits (graph) |
| `/git status` | Working tree status |
| `/git diff` | Unstaged changes (stat) |
| `/skills` | List available BMAD workflows |

## Chat patterns

### Basic prompt

Send any message — RemoClaw forwards it to the configured CLI:

```text
You: Check sprint status
RemoClaw: [streaming response from CLI]
```

### Thread-based sessions

Each Telegram thread maps to a separate CLI conversation:

- First message in a thread → new session
- Subsequent messages → auto-resume conversation
- `/new` in a thread → reset that thread's session
- Different threads can use different projects, providers, and models

### Multi-project workflow

Bind threads to different projects, switch in seconds:

```text
# In Thread A
/project /home/me/frontend-app
You: Add dark mode to the navbar
RemoClaw: [works in frontend-app]

# In Thread B
/project /home/me/backend-api
You: Add a /health endpoint
RemoClaw: [works in backend-api]
```

`/projects` shows an inline keyboard to switch between previously-used projects.

### Decision forwarding

When the CLI asks a question (Y/n, file selection), RemoClaw forwards it:

```text
RemoClaw: ⚠️ CLI is waiting for input
       > Apply changes to 3 files? [Y/n]
       Reply to proceed, or /cancel to abort.

You: Y

RemoClaw: [continues streaming CLI output]
```

The pending decision auto-times out after `DECISION_REPLY_TIMEOUT` seconds (default 1800 = 30 minutes).

### Voice messages

Send a voice message → RemoClaw transcribes it and shows a confirmation keyboard:

```text
🎤 Transcription: "check the test results"
[✅ Send] [✏️ Edit] [🗑️ Cancel]
```

- **Send** — forward to CLI as if you typed it
- **Edit** — type a corrected version
- **Cancel** — discard

Set `VOICE_AUTO_SEND=true` in `.env` to skip the confirmation keyboard.

Responses are read aloud when voice output is enabled (`/voice` to toggle), except for code-heavy responses (>50% code blocks).

See [Voice Configuration](voice.md) for details.

### Screenshot forwarding

When the CLI mentions image files in its output (`.png`, `.jpg`, `.gif`, `.webp`), RemoClaw automatically detects and sends them as Telegram photos. Files larger than 10MB are sent as documents.

### BMAD skills

If your project uses BMAD workflows, invoke them with slash commands:

```text
/bmad-sprint-status
/bmad-create-prd
/bmad-code-review
```

Telegram uses underscores (`/bmad_sprint_status`) which RemoClaw auto-converts to hyphens.

## Switching providers

Change the AI agent for the current thread:

```text
/provider claude
```

Available: `kiro`, `claude`, `gemini`, `codex`.

The thread must already be bound to a project (`/project <path>`) before switching providers.

## Model selection

```text
/model
```

Shows an inline keyboard of available models. The selected model is persisted per-thread in SQLite — survives bot restarts.

Models like `auto`, `claude-opus`, `claude-sonnet`, `claude-haiku`, `deepseek`, `glm`, `qwen`, etc. are auto-discovered from the active CLI provider.
