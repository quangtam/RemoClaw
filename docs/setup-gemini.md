# Setup RemoClaw with Gemini CLI

## Prerequisites

- [Gemini CLI](https://github.com/google-gemini/gemini-cli) installed
- Google account

## Step 1: Install Gemini CLI

```bash
# npm (all platforms)
npm install -g @anthropic-ai/gemini-cli

# Or via the official installer
curl -sSL https://geminicli.com/install | bash

# Verify
gemini --version
```

## Step 2: Login on your machine

```bash
gemini auth
```

This opens a browser window. Sign in with your Google account. The session is saved locally — you only need to do this once.

> **Headless/SSH?** Set `GEMINI_API_KEY` in `.env` instead. Get an API key from [Google AI Studio](https://aistudio.google.com/apikey).

## Step 3: Verify Gemini works

```bash
gemini -p "say hello"
```

You should see a response. If this works, RemoClaw will work too.

## Step 4: Configure RemoClaw

Edit `.env`:

```env
CLI_PROVIDER=gemini
PROJECT_DIR=/path/to/your/project
```

That's it. No API key needed — RemoClaw uses your local login session.

> **Optional:** If you prefer API key auth, set `GEMINI_API_KEY=your-key` in `.env`.

## Step 5: Start and verify

```bash
./remoclaw start
```

Send `/status` in Telegram — you should see the Gemini CLI version.

## Notes

- Auth via local Google login — no API key in `.env` by default
- Free tier: 60 requests/min, 1000/day
- Does not support `--resume` (each message is independent)
- Uses `--sandbox=false` in headless mode (trust_all_tools=true)
