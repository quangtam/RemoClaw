# Setup RemoClaw with Kiro CLI

## Prerequisites

- [Kiro CLI](https://kiro.dev/docs/cli/) installed
- Kiro Pro, Pro+, or Power subscription (required for headless mode)

## Step 1: Install Kiro CLI

```bash
# macOS
brew install --cask kiro-cli

# Or download from https://kiro.dev/docs/cli/installation/
```

Verify:

```bash
kiro-cli --version
```

## Step 2: Login on your machine

```bash
kiro-cli login
```

This opens a browser window. Sign in with your Kiro account. The session is saved locally — you only need to do this once.

> **Headless/SSH?** Set `KIRO_API_KEY` in `.env` instead. Get the key from your Kiro subscription settings.

## Step 3: Verify Kiro works

```bash
kiro-cli chat --no-interactive "say hello"
```

You should see a response. If this works, RemoClaw will work too.

## Step 4: Configure RemoClaw

Edit `.env`:

```env
CLI_PROVIDER=kiro
PROJECT_DIR=/path/to/your/project
```

That's it. No API key needed — RemoClaw uses your local login session.

## Step 5: Start and verify

```bash
./remoclaw start
```

Send `/status` in Telegram — you should see:

```text
✅ Kiro CLI ready

username: your-email@example.com
plan: Pro
```

## Notes

- Auth via local login — no API key in `.env` by default
- Supports `--resume` for conversation continuity across messages
- Supports `/model` to switch between available models
- Supports BMAD skills via `/bmad-*` commands
