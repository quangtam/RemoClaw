# RemoClaw — Deployment Guide

## Overview

RemoClaw is designed as a **long-running process on a single trusted machine**. There's no server component, no database, no external service dependencies beyond:

- Telegram Bot API (outbound HTTPS)
- One AI CLI binary (local subprocess)

## Deployment Targets

### 1. Personal laptop / desktop

**Best for**: Individual use, development, testing.

- Run `./remoclaw start` when needed
- Optionally add to shell profile as alias
- No public IP or firewall changes needed

### 2. Always-on home server / Raspberry Pi

**Best for**: Personal 24/7 access from any device.

- Run as systemd service (Linux)
- Auto-restart on crash or reboot
- CLI stays logged in across restarts

### 3. Cloud VM (AWS EC2, GCP, DigitalOcean, etc.)

**Best for**: Team use, remote-only setup.

- Reliable uptime
- Fixed IP for webhook (not needed — RemoClaw uses polling)
- Moderate cost (~$5-10/month for smallest tier)

### 4. Docker container (recommended for teams)

**Best for**: Reproducible deployment, easy updates.

See [Docker](#docker-deployment) section below.

## Prerequisites for Production

1. **Python 3.12+** installed
2. **One AI CLI** installed and logged in (persistent session)
3. **Telegram Bot token** from @BotFather
4. **Outbound HTTPS** access to `api.telegram.org`
5. **Whitelisted user IDs** in `ALLOWED_USER_IDS`
6. **Trusted filesystem** — RemoClaw can read/write/execute in `PROJECT_DIR`

## Systemd Service (Linux)

Create `/etc/systemd/system/remoclaw.service`:

```ini
[Unit]
Description=RemoClaw — AI CLI Telegram bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tony
Group=tony
WorkingDirectory=/home/tony/remoclaw
ExecStart=/home/tony/remoclaw/.venv/bin/python /home/tony/remoclaw/remoclaw.py
Restart=on-failure
RestartSec=10
StandardOutput=append:/home/tony/remoclaw/remoclaw.log
StandardError=append:/home/tony/remoclaw/remoclaw.log

# Security hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable remoclaw
sudo systemctl start remoclaw
sudo systemctl status remoclaw
```

View logs:

```bash
journalctl -u remoclaw -f
# or
tail -f /home/tony/remoclaw/remoclaw.log
```

## Docker Deployment

### Dockerfile

RemoClaw doesn't ship a Dockerfile yet. Here's a reference example:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install any needed system packages (e.g., git, curl for CLI installers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install AI CLI (example: Claude Code via npm)
# RUN apt-get install -y nodejs npm && npm install -g @anthropic-ai/claude-code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as non-root
RUN useradd -u 1000 -m remoclaw && chown -R remoclaw:remoclaw /app
USER remoclaw

CMD ["python", "remoclaw.py"]
```

### Challenges with Docker

- **CLI authentication**: CLIs like Kiro/Claude/Gemini/Codex authenticate via browser OAuth. In Docker, you need to either:
  - Mount the host's CLI config directory (e.g., `~/.config/kiro`) into the container
  - Use API key authentication via `*_API_KEY` env vars
- **PTY**: POSIX `pty.fork()` works in Docker; Windows containers fall back to non-interactive
- **Project directory**: Mount `PROJECT_DIR` into container as volume

Example `docker-compose.yml`:

```yaml
services:
  remoclaw:
    build: .
    volumes:
      - ./your-project:/workspace
      - ~/.config/kiro:/home/remoclaw/.config/kiro  # Persist CLI auth
    environment:
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - ALLOWED_USER_IDS=${ALLOWED_USER_IDS}
      - CLI_PROVIDER=kiro
      - PROJECT_DIR=/workspace
    restart: unless-stopped
```

## Resource Requirements

RemoClaw itself is lightweight. The AI CLI subprocesses consume most resources.

| Resource | Idle (bot only) | With active CLI session |
| -------- | --------------- | ----------------------- |
| RAM | ~50-80 MB | +200-500 MB (CLI + LLM context) |
| CPU | <1% | Spikes during CLI execution |
| Disk | <100 MB (code + venv) | Depends on CLI cache/logs |
| Network | Light (polling) | Depends on CLI API calls |

Minimum viable: **1 vCPU, 512 MB RAM, 1 GB disk** (plus CLI binary size).

Recommended for smooth multi-thread use: **2 vCPU, 1 GB RAM**.

## Security Considerations

### 1. Whitelist users

**Always** set `ALLOWED_USER_IDS` in `.env`. Bot accessible to anyone who guesses the token otherwise (but user IDs filter at application layer).

### 2. Revoke leaked tokens

If token ever appears in logs or public commits:

- DM `@BotFather` → `/revoke` → select bot → get new token
- Update `.env` and restart

### 3. Trust boundary

`CLI_TRUST_ALL_TOOLS=true` (default) allows the AI CLI to execute any tool without confirmation. This means:

- AI can read/write any file in `PROJECT_DIR`
- AI can run shell commands
- AI can install packages

**Only deploy with this setting if you trust**:

- The AI model (prompt injection risks)
- The users with access (they can prompt the AI arbitrarily)
- The scope — don't point `PROJECT_DIR` at `/` or sensitive directories

For higher security, set `CLI_TRUST_ALL_TOOLS=false` and the CLI will prompt for confirmation (which breaks headless mode for most CLIs).

### 4. Network isolation

RemoClaw only needs outbound HTTPS to:

- `api.telegram.org` (Telegram Bot API)
- CLI-specific endpoints (e.g., `api.anthropic.com`, `generativelanguage.googleapis.com`)

Block all other outbound if using a firewall.

### 5. Logs may contain sensitive data

`remoclaw.log` includes prompts and AI responses. Protect the log file:

```bash
chmod 600 remoclaw.log
```

## Monitoring

### Health check

A simple health check — bot is alive if it's polling:

```bash
./remoclaw status
```

Exit code 0 if running, 1 otherwise. Usable in monitoring scripts.

### Telegram-based monitoring

Add a scheduled `/status` check from another bot or manually.

### Log monitoring

Watch for these patterns:

- `ERROR` — handler exceptions
- `409 Conflict` — duplicate bot instance
- `timed out` — stuck CLI sessions
- `PTY session failed` — CLI authentication issues

## Updates

```bash
cd remoclaw
git pull
./remoclaw restart
```

If `requirements.txt` changed:

```bash
.venv/bin/pip install -r requirements.txt
./remoclaw restart
```

## Rollback

```bash
git log --oneline
git checkout <previous-tag>
./remoclaw restart
```

## Backup

There's no persistent state to back up — all state is either:

- Configuration (`.env`) — back up once
- Ephemeral (in-memory thread sessions) — lost on restart anyway
- CLI auth (in user home) — back up CLI config directories

## Production Checklist

Before going live:

- [ ] `ALLOWED_USER_IDS` whitelist set
- [ ] Telegram token stored only in `.env` (not committed)
- [ ] CLI logged in and verified: `<cli> whoami` or `--version`
- [ ] `PROJECT_DIR` points to correct, trusted directory
- [ ] `CLI_TRUST_ALL_TOOLS` set appropriately for your risk tolerance
- [ ] Log file permissions set to `600`
- [ ] Auto-restart configured (systemd / Docker `restart: unless-stopped`)
- [ ] Disk space monitored (logs grow unbounded)
- [ ] Test end-to-end: send message → receive response

## Log Rotation

`remoclaw.log` grows unbounded. Configure rotation:

**Systemd + logrotate** (`/etc/logrotate.d/remoclaw`):

```text
/home/tony/remoclaw/remoclaw.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
```

**Manual rotation**:

```bash
mv remoclaw.log remoclaw.log.old
./remoclaw restart
```

## Troubleshooting Production Issues

### Bot stops responding

1. Check process: `./remoclaw status`
2. Check logs: `tail -100 remoclaw.log`
3. Check Telegram API: `curl https://api.telegram.org/bot<TOKEN>/getMe`
4. Check CLI: `kiro-cli whoami` (or equivalent)
5. Last resort: `./remoclaw restart`

### High memory usage

Long-running PTY sessions accumulate context. Mitigations:

- Periodic `./remoclaw restart` (daily cron)
- Users send `/new` more often to reset sessions
- Decrease `CLI_TIMEOUT` to kill long-running processes sooner

### Rate limiting from Telegram

If you hit 30 edits/min/chat limit, increase `_STREAM_UPDATE_INTERVAL` in `remoclaw.py` (default 1.5s). Tradeoff: less responsive streaming.
