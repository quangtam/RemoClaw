# RemoClaw — Source Tree Analysis

## Annotated Directory Tree

```text
chati/
├── chati.py                    # Main entry — Telegram handlers, auth, streaming coordinator
├── chati                       # Bash management script (POSIX): start/stop/restart/status/log
├── chati.bat                   # Windows management script (equivalent)
├── cli_runner.py               # CliRunner — subprocess manager, PTY sessions, watchdog
├── config.py                   # Config dataclass — loads .env into frozen dataclass
├── message_utils.py            # Output pipeline — ANSI strip, MD→HTML, message splitting
│
├── cli_providers/              # Pluggable CLI driver package (auto-discovery)
│   ├── __init__.py             # Re-exports CliProvider, create_provider, get_available_providers
│   ├── base.py                 # CliProvider ABC + CliProviderConfig dataclass
│   ├── registry.py             # Auto-scan + create_provider() factory
│   ├── kiro.py                 # Kiro CLI driver (interactive PTY + --no-interactive fallback)
│   ├── claude.py               # Claude Code driver (claude -p)
│   ├── gemini.py               # Gemini CLI driver (gemini -p)
│   └── codex.py                # OpenAI Codex driver (codex exec)
│
├── docs/                       # Project documentation (this folder)
│   ├── project-overview.md     # High-level project summary
│   ├── architecture.md         # Technical architecture and design decisions
│   ├── source-tree-analysis.md # This file
│   ├── development-guide.md    # Setup, build, run, contribute
│   ├── deployment-guide.md     # Production deployment considerations
│   ├── component-inventory.md  # All components and their purposes
│   ├── setup-kiro.md           # Per-CLI setup guide: Kiro
│   ├── setup-claude.md         # Per-CLI setup guide: Claude Code
│   ├── setup-gemini.md         # Per-CLI setup guide: Gemini CLI
│   ├── setup-codex.md          # Per-CLI setup guide: OpenAI Codex
│   ├── index.md                # Master index for AI retrieval
│   ├── planning-artifacts/     # BMad planning phase outputs (PRD, architecture, epics)
│   ├── implementation-artifacts/ # BMad implementation outputs (sprint status, stories)
│   └── test-artifacts/         # BMad test design + reviews
│
├── assets/                     # Static assets for README
│   ├── demo.mp4                # Video demonstration
│   └── remoclaw.jpg          # Screenshot for README
│
├── setup.sh                    # Interactive setup wizard (POSIX)
├── setup.bat                   # Interactive setup wizard (Windows)
│
├── .env                        # Runtime secrets (GITIGNORED)
├── .env.example                # Template with all env var documentation
├── .gitignore                  # Excludes .env, venv, logs, pid, pycache
│
├── CLAUDE.md                   # AI agent context for Claude-compatible IDEs
├── README.md                   # User-facing project README
├── LICENSE                     # MIT License
├── requirements.txt            # Pip dependencies: python-telegram-bot, python-dotenv
│
├── _bmad/                      # BMad method configuration (AI workflow framework)
│   ├── _config/                # Assembled manifests (bmad-help.csv, skill-manifest.csv)
│   ├── bmm/                    # BMad Method module config + workflows
│   ├── cis/                    # Creative Intelligence Suite module
│   ├── core/                   # Core BMad skills
│   ├── custom/                 # User overrides (team + personal)
│   ├── scripts/                # Config resolver Python scripts
│   ├── tea/                    # Test Architecture Enterprise module
│   ├── wds/                    # Web Design Studio module
│   ├── config.toml             # Installer-generated main config
│   └── config.user.toml        # User-specific overrides
│
├── .kiro/                      # Kiro IDE skills (installed BMad skills)
│   └── skills/                 # Skill definitions and workflows
│
└── .venv/                      # Python virtual environment (GITIGNORED)
```

## Critical Directories Explained

### Source Code (root)

All application code lives at the root level except for the providers sub-package. This is intentional — RemoClaw is a small focused tool, flat structure keeps imports simple.

| File | Role | Key Exports |
| ---- | ---- | ----------- |
| `remoclaw.py` | Application entry point | `main()`, handler functions, `_execute_and_reply` |
| `cli_runner.py` | Subprocess orchestration | `CliRunner`, `CliResult`, `_PtySession` |
| `config.py` | Configuration | `Config` dataclass, `Config.from_env()` |
| `message_utils.py` | Output formatting | `format_output`, `split_message`, `strip_ansi` |

### `cli_providers/` (Extension Point)

The only place contributors need to touch when adding new CLI support. Each `.py` file (other than `base.py`, `registry.py`, `__init__.py`) is an independent driver.

**Entry point**: `cli_providers/__init__.py` re-exports the public API.

**Auto-discovery**: `registry.py:_discover_providers()` uses `pkgutil.iter_modules()` to import all modules in the package, then inspects for `CliProvider` subclasses with non-empty `provider_id`.

### `docs/` (Documentation + BMad Output)

- **Top-level `.md` files**: Project documentation (this scan's output + pre-existing setup guides)
- **`planning-artifacts/`**: Where BMad writes PRD, architecture docs, epics
- **`implementation-artifacts/`**: Where BMad writes sprint status, story files
- **`test-artifacts/`**: Where BMad test workflows write test designs and reviews

### `_bmad/` (BMad Framework)

Metadata and scripts for the [BMad Method](https://docs.bmad-method.org/) — a structured AI workflow framework. Not part of RemoClaw's runtime; used for AI-assisted development of RemoClaw itself.

### `.kiro/skills/` (Kiro IDE Skills)

Skill definitions for Kiro IDE integration. These are consumed by `kiro-cli` when running BMad commands like `/bmad-create-prd`.

## Entry Points

| Scenario | Entry Point |
| -------- | ----------- |
| **User installation** | `setup.sh` or `setup.bat` |
| **Starting the bot** | `./remoclaw start` → `chati.py::main()` |
| **Python direct** | `python chati.py` |
| **Adding a CLI provider** | Create file in `cli_providers/` inheriting `CliProvider` |
| **Changing behavior** | Edit `.env`, restart |

## File Size Overview

| File | Lines | Complexity |
| ---- | ----- | ---------- |
| `remoclaw.py` | ~630 | High — main coordination logic |
| `cli_runner.py` | ~470 | High — subprocess + PTY management |
| `message_utils.py` | ~440 | Medium — text processing |
| `cli_providers/*.py` | 30-80 each | Low — simple strategy implementations |
| `config.py` | ~90 | Low — dataclass + env loading |
| `remoclaw` (bash) | ~115 | Low — process management |
| `remoclaw.bat` | ~130 | Low — Windows equivalent |
| `setup.sh` | ~210 | Low — linear interactive script |
| `setup.bat` | ~190 | Low — Windows equivalent |

## Integration Points

RemoClaw is a monolith, but it integrates with external systems:

| External | Protocol | Direction |
| -------- | -------- | --------- |
| Telegram Bot API | HTTPS long polling | Bidirectional |
| AI CLI binaries | Subprocess (stdin/stdout/pty) | Bidirectional |
| Filesystem | Read `.env`, logs, PID file | Local |
| Environment variables | Read at startup | Local |

No network server, no database, no message queue.

## Excluded Paths

The following are excluded from source analysis:

- `.venv/` — Python virtual environment
- `__pycache__/` — Python bytecode cache
- `.git/` — Git metadata
- `assets/` — binary assets (mp4, jpg)
- `.env` — secrets
- `*.log`, `*.pid` — runtime artifacts
