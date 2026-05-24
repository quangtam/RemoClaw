# Setup RemoClaw with Claude Code

## Prerequisites

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code/) installed
- Anthropic account (free tier available)

## Step 1: Install Claude Code

```bash
# npm (all platforms)
npm install -g @anthropic-ai/claude-code

# Verify
claude --version
```

## Step 2: Login on your machine

```bash
claude login
```

This opens a browser window. Sign in with your Anthropic account. The session is saved locally — you only need to do this once.

> **Headless/SSH?** Use `claude login --device-auth` for machines without a browser.

## Step 3: Verify Claude works

```bash
claude -p "say hello"
```

You should see a response. If this works, RemoClaw will work too.

## Step 4: Configure RemoClaw

Edit `.env`:

```env
CLI_PROVIDER=claude
PROJECT_DIR=/path/to/your/project
```

That's it. No API key needed — RemoClaw uses your local login session.

> **Optional:** If you prefer API key auth instead of local login, set `ANTHROPIC_API_KEY=sk-ant-...` in `.env`.

## Step 5: Start and verify

```bash
./remoclaw start
```

Send `/status` in Telegram — you should see the Claude Code version.

## Notes

- Auth via local login — no API key in `.env` by default
- Supports `--resume` for conversation continuity
- Supports `--model` to switch models (e.g. `claude-sonnet-4`, `claude-opus-4`)
- Uses `--dangerously-skip-permissions` in headless mode (trust_all_tools=true)
