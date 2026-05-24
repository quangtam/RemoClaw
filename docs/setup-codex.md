# Setup RemoClaw with OpenAI Codex CLI

## Prerequisites

- [Codex CLI](https://github.com/openai/codex) installed
- OpenAI account (ChatGPT Plus or API access)

## Step 1: Install Codex CLI

```bash
# npm (all platforms)
npm install -g @openai/codex

# macOS via Homebrew
brew install --cask codex

# Verify
codex --version
```

## Step 2: Login on your machine

```bash
codex login
```

This opens a browser window. Sign in with your OpenAI/ChatGPT account. The session is saved locally — you only need to do this once.

> **Headless/SSH?** Use `codex login --device-auth` or set `OPENAI_API_KEY` in `.env`.

## Step 3: Verify Codex works

```bash
codex exec "say hello"
```

You should see a response. If this works, RemoClaw will work too.

## Step 4: Configure RemoClaw

Edit `.env`:

```env
CLI_PROVIDER=codex
PROJECT_DIR=/path/to/your/project
```

That's it. No API key needed — RemoClaw uses your local login session.

> **Optional:** If you prefer API key auth, set `OPENAI_API_KEY=sk-...` in `.env`.

## Step 5: Start and verify

```bash
./remoclaw start
```

Send `/status` in Telegram — you should see the Codex CLI version.

## Notes

- Auth via local login — no API key in `.env` by default
- Does not support `--resume` (each message is independent)
- Uses `--full-auto` in headless mode (trust_all_tools=true)
- Runs with OS-native sandboxing (Seatbelt on macOS, Bubblewrap on Linux)
