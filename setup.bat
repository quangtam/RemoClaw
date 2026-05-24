@echo off
setlocal enabledelayedexpansion

:: ── RemoClaw Setup Wizard for Windows ──────────────────────────────

echo.
echo     ____                       ______  __           
echo    / __ \___  ____ ___  ____  / ____/ / /___ __      __
echo   / /_/ / _ \/ __ `__ \/ __ \/ /     / / __ `/ ^| /^| / /
echo  / _, _/  __/ / / / / / /_/ / /___  / / /_/ /^| ^|/ ^|/ /
echo /_/ ^|_^|\___/_/ /_/ /_/\____/\____/ /_/\__,_/ ^|__/^|__/
echo.
echo     Remote control any AI coding agent
echo.

:: ── Step 1: Check Python ────────────────────────────────────────

echo [*] Checking prerequisites...
echo.

set PYTHON=
for %%P in (python3 python py) do (
    where %%P >nul 2>&1
    if !errorlevel! equ 0 (
        for /f "tokens=2" %%V in ('%%P --version 2^>^&1') do (
            for /f "tokens=1,2 delims=." %%A in ("%%V") do (
                if %%A geq 3 if %%B geq 12 (
                    set PYTHON=%%P
                    echo [OK] Python: %%V
                )
            )
        )
    )
)

if "%PYTHON%"=="" (
    echo [ERROR] Python 3.12+ is required but not found.
    echo.
    echo   Download from: https://www.python.org/downloads/
    echo   Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

:: ── Step 2: Create venv + install deps ──────────────────────────

if not exist ".venv" (
    echo [*] Creating virtual environment...
    %PYTHON% -m venv .venv
    echo [OK] Virtual environment created
) else (
    echo [OK] Virtual environment exists
)

echo [*] Installing dependencies...
.venv\Scripts\pip install -q -r requirements.txt
echo [OK] Dependencies installed
echo.

:: ── Step 3: Choose CLI provider ─────────────────────────────────

echo Which AI CLI do you want to use?
echo.
echo   1) Kiro CLI        (kiro-cli)
echo   2) Claude Code     (claude)
echo   3) Gemini CLI      (gemini)
echo   4) OpenAI Codex    (codex)
echo.
set /p CHOICE="Choose [1-4]: "

if "%CHOICE%"=="1" (set CLI_PROVIDER=kiro&   set CLI_BIN=kiro-cli)
if "%CHOICE%"=="2" (set CLI_PROVIDER=claude& set CLI_BIN=claude)
if "%CHOICE%"=="3" (set CLI_PROVIDER=gemini& set CLI_BIN=gemini)
if "%CHOICE%"=="4" (set CLI_PROVIDER=codex&  set CLI_BIN=codex)

if "%CLI_PROVIDER%"=="" (
    echo [ERROR] Invalid choice
    pause
    exit /b 1
)

where %CLI_BIN% >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] %CLI_BIN% found
) else (
    echo [WARN] %CLI_BIN% not found in PATH.
    if "%CLI_PROVIDER%"=="kiro"   echo   Install: https://kiro.dev/docs/cli/installation/
    if "%CLI_PROVIDER%"=="claude" echo   Install: npm install -g @anthropic-ai/claude-code
    if "%CLI_PROVIDER%"=="gemini" echo   Install: npm install -g @google/gemini-cli
    if "%CLI_PROVIDER%"=="codex"  echo   Install: npm install -g @openai/codex
    echo.
    set /p CONT="Continue anyway? [y/N]: "
    if /i not "!CONT!"=="y" exit /b 1
)
echo.

:: ── Step 4: Telegram bot token ──────────────────────────────────

echo Telegram Bot Setup
echo.
echo   1. Open Telegram, find @BotFather
echo   2. Send /newbot, follow the prompts
echo   3. Copy the bot token
echo.
set /p BOT_TOKEN="Paste your Telegram bot token: "

if "%BOT_TOKEN%"=="" (
    echo [ERROR] Bot token is required
    pause
    exit /b 1
)
echo.

:: ── Step 5: Telegram user ID ────────────────────────────────────

echo   To get your Telegram user ID:
echo   Open Telegram, find @userinfobot, send /start
echo.
set /p USER_IDS="Your Telegram user ID: "

if "%USER_IDS%"=="" (
    echo [ERROR] User ID is required
    pause
    exit /b 1
)
echo.

:: ── Step 6: Project directory ───────────────────────────────────

set /p PROJECT_DIR="Project directory [%CD%]: "
if "%PROJECT_DIR%"=="" set PROJECT_DIR=%CD%

if not exist "%PROJECT_DIR%" (
    echo [ERROR] Directory does not exist: %PROJECT_DIR%
    pause
    exit /b 1
)
echo [OK] Project: %PROJECT_DIR%
echo.

:: ── Step 7: Voice features ──────────────────────────────────────

echo Voice Features (optional)
echo.
echo   Voice lets you send voice messages to the bot (transcribed to text)
echo   and optionally receive responses as audio.
echo.
echo   Two backends available:
echo     Free (no API key): faster-whisper (local) + edge-tts (Microsoft)
echo     Premium (OpenAI):  Whisper API + TTS API (higher quality, costs money)
echo.
echo   Voice works out of the box with the free backend.
echo   To use OpenAI, enter your API key below (or leave blank).
echo.
set /p OPENAI_API_KEY="OpenAI API key (leave blank for free local backend): "
echo.

if not "%OPENAI_API_KEY%"=="" (
    echo [OK] OpenAI backend selected (Whisper + TTS)
) else (
    echo [OK] Free local backend selected (faster-whisper + edge-tts)
    echo [*]  faster-whisper 'base' model will download on first voice message (~140MB)
)
echo.

:: ── Step 8: Write .env ─────────────────────────────────────────

set WRITE_ENV=y
if exist ".env" (
    set /p OVERWRITE="[WARN] .env already exists. Overwrite? [y/N]: "
    if /i not "!OVERWRITE!"=="y" (
        echo [*] Keeping existing .env
        set WRITE_ENV=n
    )
)

if /i "%WRITE_ENV%"=="y" (
    (
        echo TELEGRAM_BOT_TOKEN=%BOT_TOKEN%
        echo ALLOWED_USER_IDS=%USER_IDS%
        echo CLI_PROVIDER=%CLI_PROVIDER%
        echo PROJECT_DIR=%PROJECT_DIR%
        echo CLI_TIMEOUT=300
        echo CLI_TRUST_ALL_TOOLS=true
        echo LOG_LEVEL=INFO
    ) > .env

    if not "%OPENAI_API_KEY%"=="" (
        echo. >> .env
        echo # Voice features -- OpenAI backend >> .env
        echo OPENAI_API_KEY=%OPENAI_API_KEY% >> .env
    ) else (
        echo. >> .env
        echo # Voice features -- free local backend (faster-whisper + edge-tts) >> .env
        echo # Uncomment OPENAI_API_KEY to switch to OpenAI cloud backend: >> .env
        echo # OPENAI_API_KEY=your-key-here >> .env
        echo WHISPER_LOCAL_MODEL=base >> .env
        echo TTS_LOCAL_VOICE=vi-VN-HoaiMyNeural >> .env
    )

    echo [OK] .env created
)

:: ── Done ────────────────────────────────────────────────────────

echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo   RemoClaw is ready!
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo.
echo   Make sure your CLI is logged in:
if "%CLI_PROVIDER%"=="kiro"   echo     kiro-cli login
if "%CLI_PROVIDER%"=="claude" echo     claude login
if "%CLI_PROVIDER%"=="gemini" echo     gemini auth
if "%CLI_PROVIDER%"=="codex"  echo     codex login
echo.
echo   Then start RemoClaw:
echo     remoclaw start
echo.
echo   Voice commands (in Telegram):
echo     Send a voice message  -- bot transcribes and confirms
echo     /voice                -- toggle voice responses on/off
echo.
echo   Other commands:
echo     remoclaw stop       -- stop
echo     remoclaw restart    -- restart
echo     remoclaw status     -- check status
echo     remoclaw log        -- view logs
echo.
pause
