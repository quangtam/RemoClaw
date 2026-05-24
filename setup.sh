#!/usr/bin/env bash
#
# RemoClaw — interactive setup wizard
# Run: bash setup.sh
#

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$DIR/.env"

# ── Colors ───────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}ℹ ${NC}$1"; }
ok()    { echo -e "${GREEN}✅ ${NC}$1"; }
warn()  { echo -e "${YELLOW}⚠️  ${NC}$1"; }
err()   { echo -e "${RED}❌ ${NC}$1"; }
ask()   { echo -en "${BOLD}$1${NC}"; }

# ── Banner ───────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}"
echo '    ____                       ______  __           '
echo '   / __ \___  ____ ___  ____  / ____/ / /___ __      __'
echo '  / /_/ / _ \/ __ `__ \/ __ \/ /     / / __ `/ | /| / /'
echo ' / _, _/  __/ / / / / / /_/ / /___  / / /_/ /| |/ |/ /'
echo '/_/ |_|\___/_/ /_/ /_/\____/\____/ /_/\__,_/ |__/|__/'
echo -e "${NC}"
echo -e "    ${CYAN}🦞 Remote control any AI coding agent 📱→💻${NC}"
echo ""

# ── Step 1: Check Python ────────────────────────────────────────

info "Checking prerequisites..."
echo ""

PYTHON=""
for cmd in python3.14 python3.13 python3.12 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [[ "$major" -ge 3 && "$minor" -ge 12 ]]; then
            PYTHON="$cmd"
            ok "Python: $("$cmd" --version)"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    err "Python 3.12+ is required but not found."
    echo ""
    echo "  Install options:"
    echo "    macOS:   brew install python@3.12"
    echo "    Ubuntu:  sudo apt install python3.12"
    echo "    Other:   https://www.python.org/downloads/"
    echo ""
    exit 1
fi

# ── Step 2: Create venv + install deps ───────────────────────────

if [[ ! -d "$DIR/.venv" ]]; then
    info "Creating virtual environment..."
    "$PYTHON" -m venv "$DIR/.venv"
    ok "Virtual environment created"
else
    ok "Virtual environment exists"
fi

info "Installing dependencies..."
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
ok "Dependencies installed"
echo ""

# ── Step 3: Choose CLI provider ─────────────────────────────────

echo -e "${BOLD}Which AI CLI do you want to use?${NC}"
echo ""
echo "  1) Kiro CLI        (kiro-cli)"
echo "  2) Claude Code     (claude)"
echo "  3) Gemini CLI      (gemini)"
echo "  4) OpenAI Codex    (codex)"
echo ""
ask "Choose [1-4]: "
read -r choice

case "$choice" in
    1) CLI_PROVIDER="kiro";   CLI_BIN="kiro-cli" ;;
    2) CLI_PROVIDER="claude"; CLI_BIN="claude"    ;;
    3) CLI_PROVIDER="gemini"; CLI_BIN="gemini"    ;;
    4) CLI_PROVIDER="codex";  CLI_BIN="codex"     ;;
    *)
        err "Invalid choice"
        exit 1
        ;;
esac

# Check if CLI is installed
if command -v "$CLI_BIN" &>/dev/null; then
    ok "$CLI_BIN found: $(command -v "$CLI_BIN")"
else
    warn "$CLI_BIN not found in PATH."
    echo ""
    case "$CLI_PROVIDER" in
        kiro)   echo "  Install: brew install --cask kiro-cli" ;;
        claude) echo "  Install: npm install -g @anthropic-ai/claude-code" ;;
        gemini) echo "  Install: npm install -g @google/gemini-cli" ;;
        codex)  echo "  Install: npm install -g @openai/codex" ;;
    esac
    echo "  Then re-run: bash setup.sh"
    echo ""
    ask "Continue anyway? [y/N]: "
    read -r cont
    [[ "$cont" =~ ^[Yy] ]] || exit 1
fi
echo ""

# ── Step 4: Telegram bot token ───────────────────────────────────

echo -e "${BOLD}Telegram Bot Setup${NC}"
echo ""
echo "  1. Open Telegram → find @BotFather"
echo "  2. Send /newbot → follow the prompts"
echo "  3. Copy the bot token"
echo ""
ask "Paste your Telegram bot token: "
read -r BOT_TOKEN

if [[ -z "$BOT_TOKEN" ]]; then
    err "Bot token is required"
    exit 1
fi
echo ""

# ── Step 5: Telegram user ID ────────────────────────────────────

echo "  To get your Telegram user ID:"
echo "  → Open Telegram → find @userinfobot → send /start"
echo ""
ask "Your Telegram user ID: "
read -r USER_IDS

if [[ -z "$USER_IDS" ]]; then
    err "User ID is required"
    exit 1
fi
echo ""

# ── Step 6: Project directory ────────────────────────────────────

ask "Project directory for CLI to work in [$(pwd)]: "
read -r PROJECT_DIR
PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"

if [[ ! -d "$PROJECT_DIR" ]]; then
    err "Directory does not exist: $PROJECT_DIR"
    exit 1
fi
ok "Project: $PROJECT_DIR"
echo ""

# ── Step 7: Voice features ───────────────────────────────────────

echo -e "${BOLD}Voice Features (optional)${NC}"
echo ""
echo "  Voice lets you send voice messages to the bot (transcribed to text)"
echo "  and optionally receive responses as audio."
echo ""
echo "  Two backends available:"
echo "    • Free (no API key): faster-whisper (local) + edge-tts (Microsoft)"
echo "    • Premium (OpenAI):  Whisper API + TTS API (higher quality, costs money)"
echo ""
echo "  Voice input/output works out of the box with the free backend."
echo "  To use OpenAI, add OPENAI_API_KEY to .env after setup."
echo ""

OPENAI_API_KEY=""
ask "OpenAI API key (leave blank to use free local backend): "
read -r OPENAI_API_KEY
echo ""

if [[ -n "$OPENAI_API_KEY" ]]; then
    ok "OpenAI backend selected (Whisper + TTS)"
else
    ok "Free local backend selected (faster-whisper + edge-tts)"
    info "Downloading faster-whisper 'base' model on first voice message (~140MB)"
fi
echo ""

# ── Step 8: Write .env ──────────────────────────────────────────

OVERWRITE="y"
if [[ -f "$ENV_FILE" ]]; then
    warn ".env already exists"
    ask "Overwrite? [y/N]: "
    read -r OVERWRITE
fi

if [[ "$OVERWRITE" =~ ^[Yy] ]]; then
    cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
ALLOWED_USER_IDS=$USER_IDS
CLI_PROVIDER=$CLI_PROVIDER
PROJECT_DIR=$PROJECT_DIR
CLI_TIMEOUT=300
CLI_TRUST_ALL_TOOLS=true
LOG_LEVEL=INFO
EOF

    # Append voice config if OpenAI key provided
    if [[ -n "$OPENAI_API_KEY" ]]; then
        cat >> "$ENV_FILE" <<EOF

# Voice features — OpenAI backend
OPENAI_API_KEY=$OPENAI_API_KEY
EOF
    else
        cat >> "$ENV_FILE" <<EOF

# Voice features — free local backend (faster-whisper + edge-tts)
# Uncomment and set OPENAI_API_KEY to switch to OpenAI cloud backend:
# OPENAI_API_KEY=your-key-here
#
# Local model size: tiny (75MB, fastest) | base (140MB) | small (460MB, best accuracy)
WHISPER_LOCAL_MODEL=base
# TTS voice options: vi-VN-HoaiMyNeural (female) | vi-VN-NamMinhNeural (male)
TTS_LOCAL_VOICE=vi-VN-HoaiMyNeural
EOF
    fi

    ok ".env created"
fi

# ── Step 9: Make remoclaw executable ─────────────────────────────

chmod +x "$DIR/remoclaw"

# ── Done ─────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}${BOLD}  RemoClaw is ready!${NC}"
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  Make sure your CLI is logged in:"
case "$CLI_PROVIDER" in
    kiro)   echo "    kiro-cli login" ;;
    claude) echo "    claude login" ;;
    gemini) echo "    gemini auth" ;;
    codex)  echo "    codex login" ;;
esac
echo ""
echo "  Then start RemoClaw:"
echo "    ./remoclaw start"
echo ""
echo "  Voice commands (in Telegram):"
echo "    🎤 Send a voice message → bot transcribes and confirms"
echo "    /voice                  → toggle voice responses on/off"
echo ""
echo "  Other commands:"
echo "    ./remoclaw stop       # stop"
echo "    ./remoclaw restart    # restart"
echo "    ./remoclaw status     # check status"
echo "    ./remoclaw log        # view logs"
echo ""
