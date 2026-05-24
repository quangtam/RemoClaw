# RemoClaw — Documentation Index

> Generated: 2026-05-05
> Scan level: Exhaustive
> Generator: BMad Document Project workflow

## Project Overview

- **Type:** Monolith (single Python application)
- **Primary Language:** Python 3.12+
- **Architecture:** Async event-driven with pluggable CLI providers
- **Repository:** [github.com/quangtam/remoclaw](https://github.com/quangtam/remoclaw)

## Quick Reference

| Attribute | Value |
| --------- | ----- |
| Tech Stack | Python + python-telegram-bot + asyncio + PTY |
| Entry Point | `remoclaw.py` → `main()` |
| Architecture Pattern | Event-driven, per-thread subprocess orchestration |
| Extension Point | `cli_providers/*.py` (pluggable drivers) |
| License | MIT |
| Version | 1.0.1 |

## Generated Documentation

- [Project Overview](./project-overview.md) — high-level summary, purpose, tech stack
- [Architecture](./architecture.md) — detailed technical design, data flow, patterns
- [Source Tree Analysis](./source-tree-analysis.md) — annotated directory structure
- [Component Inventory](./component-inventory.md) — all modules, classes, functions
- [Development Guide](./development-guide.md) — setup, run, extend, debug
- [Deployment Guide](./deployment-guide.md) — systemd, Docker, security, monitoring

## Setup Guides (Per CLI)

- [Setup Kiro CLI](./setup-kiro.md)
- [Setup Claude Code](./setup-claude.md)
- [Setup Gemini CLI](./setup-gemini.md)
- [Setup OpenAI Codex](./setup-codex.md)

## External Documentation

- [README.md](../README.md) — user-facing quick start
- [CLAUDE.md](../CLAUDE.md) — AI agent context (Claude-compatible IDEs)
- [LICENSE](../LICENSE) — MIT License

## Getting Started

### For Users

1. Clone repo: `git clone https://github.com/quangtam/remoclaw.git`
2. Run setup wizard: `bash setup.sh` (or `setup.bat` on Windows)
3. Login your CLI: `kiro-cli login` (or equivalent)
4. Start RemoClaw: `./remoclaw start`
5. Message your Telegram bot

### For Contributors

1. Read [Development Guide](./development-guide.md)
2. Read [Architecture](./architecture.md) to understand the design
3. For adding a new CLI: see "Adding a New CLI Provider" in dev guide
4. For adding tests: currently no test suite — contributions welcome

## Key Concepts

| Concept | Description |
| ------- | ----------- |
| **Thread = Session** | Each Telegram thread maps to a persistent CLI session |
| **PTY Session** | Pseudo-terminal keeping CLI alive across messages (no cold start) |
| **Pluggable Provider** | CLI drivers in `cli_providers/` auto-discovered on startup |
| **Stream Response** | Telegram message edited every 1.5s with live CLI output |
| **Watchdog** | Idle warnings + global timeout prevent stuck processes |

## File Map

```text
remoclaw/
├── remoclaw.py                # Entry point + handlers
├── cli_runner.py           # Subprocess + PTY manager
├── config.py               # .env → Config dataclass
├── message_utils.py        # ANSI strip + MD→HTML pipeline
├── cli_providers/          # Pluggable driver package
├── docs/                   # This documentation
├── remoclaw / remoclaw.bat       # Management scripts
├── setup.sh / setup.bat    # Setup wizards
└── .env                    # Secrets (gitignored)
```

See [Source Tree Analysis](./source-tree-analysis.md) for full annotated tree.

## Next Steps in BMad Workflow

With this documentation complete, you can now use other BMad skills:

- **[CP] Create PRD** (`bmad-create-prd`) — plan new features
- **[CA] Create Architecture** (`bmad-create-architecture`) — update architecture for new features
- **[CE] Create Epics and Stories** (`bmad-create-epics-and-stories`) — break down work
- **[QQ] Quick Dev** (`bmad-quick-dev`) — for small changes, use unified intent-to-code workflow

Run `/bmad-help` at any point to see recommended next steps based on current state.
