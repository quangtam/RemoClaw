# RemoClaw — Project Overview

## Purpose

RemoClaw is a Python Telegram bot that bridges messaging apps to AI coding CLIs (Kiro, Claude Code, Gemini, Codex) in headless/interactive mode. It enables developers to interact with AI coding assistants from any chat app — no laptop needed.

**Tagline:** *Code from your pocket.*

## Executive Summary

| Attribute | Value |
| --------- | ----- |
| **Project Type** | CLI bridge / Bot |
| **Repository Type** | Monolith |
| **Primary Language** | Python 3.12+ |
| **Architecture Style** | Event-driven, async subprocess management |
| **License** | MIT |
| **Version** | 1.0.1 |
| **Repository** | [github.com/quangtam/remoclaw](https://github.com/quangtam/remoclaw) |

## Tech Stack Summary

| Category | Technology | Version | Purpose |
| -------- | ---------- | ------- | ------- |
| Runtime | Python | 3.12+ (tested on 3.14) | Core language |
| Telegram SDK | python-telegram-bot | 21.10 | Async Telegram Bot API wrapper |
| Config | python-dotenv | 1.1.0 | .env file loading |
| Concurrency | asyncio | stdlib | Async event loop + subprocess |
| Terminal | pty | stdlib (POSIX) | Pseudo-terminal for persistent sessions |

## Core Value Proposition

- **Multi-CLI** — one bot, any AI coding CLI (Kiro, Claude Code, Gemini, Codex)
- **Pluggable** — new CLI support = 1 Python file in `cli_providers/`
- **Fast** — persistent PTY sessions eliminate cold start overhead (~6s → ~3s per message)
- **Responsive** — real-time streaming output, typing keepalive, stuck detection
- **Thread-aware** — each Telegram thread = separate conversation session
- **Parallel** — different threads run concurrent CLI processes

## Repository Structure

Single-part monolith. All code in the root directory plus one sub-package:

- **Root**: main app entry, management scripts, config, docs
- **`cli_providers/`**: pluggable CLI driver package (auto-discovered via `pkgutil`)

## Key Artifacts

| Path | Purpose |
| ---- | ------- |
| `remoclaw.py` | Main entry — handlers, auth, streaming |
| `cli_runner.py` | Subprocess manager with PTY sessions |
| `config.py` | Env-based immutable configuration |
| `message_utils.py` | ANSI strip, MD→HTML, message splitting |
| `cli_providers/` | CLI driver package (base, registry, 4 providers) |
| `remoclaw` / `remoclaw.bat` | Management scripts (POSIX / Windows) |
| `setup.sh` / `setup.bat` | Interactive setup wizards |
| `.env` | Secrets (gitignored) |

## Links to Documentation

- [Architecture](./architecture.md) — detailed technical design
- [Source Tree Analysis](./source-tree-analysis.md) — annotated directory tree
- [Development Guide](./development-guide.md) — setup, build, run, test
- [Deployment Guide](./deployment-guide.md) — production considerations
- Setup guides per CLI: [Kiro](./setup-kiro.md) · [Claude](./setup-claude.md) · [Gemini](./setup-gemini.md) · [Codex](./setup-codex.md)
