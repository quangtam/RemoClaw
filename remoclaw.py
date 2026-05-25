"""RemoClaw — Remote control any AI coding agent from your phone.

Bridges messaging apps to AI CLIs (Kiro, Claude Code, Gemini, Codex)
in headless mode with streaming output.

Features:
- Thread-based sessions (each thread = separate conversation)
- Model selection via /model command with inline keyboard
- Pluggable CLI providers
- BMAD skill routing
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config
from cli_runner import CliRunner
from cli_providers import get_available_providers
from message_utils import format_output, split_message, strip_ansi, extract_final_response, strip_streaming_noise, detect_screenshots, is_code_heavy
from session_manager import DecisionPrompt, PtyState, SessionManager
import db
from db import DEFAULT_THREAD_ID, DB_PATH

# ── Globals ──────────────────────────────────────────────────────

config = Config.from_env()
runner = CliRunner(config)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, config.log_level, logging.INFO),
)
logger = logging.getLogger(__name__)

# Track which threads have had at least one message processed.
# Key: thread_id (int or None for non-thread), Value: message count.
_thread_sessions: dict[int | None, int] = {}

# Track the currently-running asyncio task per thread, so /cancel can
# abort the task (not just kill the PTY process). Set in _execute_and_reply,
# cleared in finally, cancelled in cmd_cancel.
_thread_tasks: dict[int | None, asyncio.Task] = {}

MAX_MSG_LEN = 4096


# ── Voice (Story 6.1) ────────────────────────────────────────────

# Module-level voice services — auto-select backend (OpenAI if key present, local otherwise).
voice_transcriber = None
voice_synthesizer = None
if config.voice_enabled:
    try:
        from voice import VoiceTranscriber, VoiceSynthesizer

        voice_transcriber = VoiceTranscriber(
            api_key=config.openai_api_key,
            model=config.whisper_model,
            timeout=config.whisper_timeout,
            local_model=config.whisper_local_model,
        )
        voice_synthesizer = VoiceSynthesizer(
            api_key=config.openai_api_key,
            model=config.tts_model,
            voice=config.tts_voice,
            speed=config.tts_speed,
            timeout=config.tts_timeout,
            local_voice=config.tts_local_voice,
            local_rate=config.tts_local_rate,
        )
        backend = "OpenAI" if config.openai_api_key else "local (faster-whisper + edge-tts)"
        logger.info("Voice enabled — backend: %s", backend)
    except ImportError as exc:
        logger.warning(
            "Voice features disabled: required package not installed (%s)",
            exc,
        )


# ── Auth Guard ───────────────────────────────────────────────────

def authorized(func):
    """Decorator to restrict bot access to allowed user IDs."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in config.allowed_user_ids:
            uid = user.id if user else "unknown"
            logger.warning("Unauthorized access attempt from user %s", uid)
            if update.message:
                await update.message.reply_text(
                    "⛔ Unauthorized. Your user ID is not in the allowed list."
                )
            return
        return await func(update, context)
    return wrapper


# ── Session Helpers ──────────────────────────────────────────────

def _get_thread_id(update: Update) -> int | None:
    """Extract thread ID from a Telegram message.

    Returns message_thread_id if in a topic/thread, else None.
    """
    if update.message and update.message.message_thread_id:
        return update.message.message_thread_id
    return None


def _should_resume(thread_id: int | None, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Determine if this message should resume an existing session.

    - If user explicitly requested /new → don't resume.
    - If this thread has had previous messages → resume.
    - First message in a thread → don't resume (fresh session).
    """
    if context.user_data.get("force_new_session"):
        context.user_data["force_new_session"] = False
        # Reset thread counter
        _thread_sessions[thread_id] = 0
        return False

    count = _thread_sessions.get(thread_id, 0)
    return count > 0


def _track_thread(thread_id: int | None) -> None:
    """Increment message count for a thread."""
    _thread_sessions[thread_id] = _thread_sessions.get(thread_id, 0) + 1


def _get_model(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Get the user's selected model, or None for default."""
    model = context.user_data.get("model")
    if model == "auto":
        return None  # auto = let CLI decide
    return model


# ── Command Handlers ─────────────────────────────────────────────

@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    model = context.user_data.get("model", "auto")

    # Resolve per-thread provider name
    thread_id = _get_thread_id(update) or DEFAULT_THREAD_ID
    try:
        resolved = await db.resolve_thread_config(
            thread_id,
            env_project_dir=config.project_dir,
            env_cli_provider=config.cli_provider,
            path=DB_PATH,
        )
        provider_name = runner.get_provider_for_thread(resolved.cli_provider).name
        project_dir = resolved.project_dir
    except Exception:
        provider_name = runner.provider.name
        project_dir = config.project_dir

    await update.message.reply_text(
        f"🚀 <b>RemoClaw</b> — {_escape_html(provider_name)}\n\n"
        "Gửi tin nhắn bất kỳ → CLI xử lý trong project:\n"
        f"<code>{_escape_html(project_dir)}</code>\n\n"
        "<b>Commands:</b>\n"
        "/help — Hướng dẫn sử dụng\n"
        "/model — Chọn AI model\n"
        "/skills — Liệt kê BMAD skills\n"
        "/status — Kiểm tra CLI\n"
        "/cancel — Hủy lệnh đang chạy\n"
        "/new — Tạo session mới\n\n"        "<b>💡 Thread = Session:</b>\n"
        "Mỗi thread riêng biệt = 1 conversation session.\n"
        "Tin nhắn tiếp theo trong cùng thread sẽ tự động resume.\n\n"
        f"<b>Model:</b> <code>{model}</code>",
        parse_mode=ParseMode.HTML,
    )


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help — show all available v2 commands."""
    model = context.user_data.get("model", "auto")

    # Resolve per-thread provider name
    thread_id = _get_thread_id(update) or DEFAULT_THREAD_ID
    try:
        resolved = await db.resolve_thread_config(
            thread_id,
            env_project_dir=config.project_dir,
            env_cli_provider=config.cli_provider,
            path=DB_PATH,
        )
        provider_name = runner.get_provider_for_thread(resolved.cli_provider).name
    except Exception:
        provider_name = runner.provider.name

    await update.message.reply_text(
        "📖 <b>RemoClaw v2.0 — Command Reference</b>\n\n"

        "<b>💬 Chat:</b>\n"
        "Send any message → forwarded to CLI\n"
        "Reply to decision prompt → piped to CLI\n\n"

        "<b>🔧 Session:</b>\n"
        "/start — Welcome message\n"
        "/new — Start fresh session (kills current)\n"
        "/cancel — Kill running process\n"
        "/resume — Resume previous session\n"
        "/info — Current session details\n"
        "/sessions — All active sessions\n\n"

        "<b>⚙️ Configuration:</b>\n"
        "/project &lt;path&gt; — Bind thread to project\n"
        "/projects — Browse previous projects\n"
        "/provider &lt;name&gt; — Switch CLI provider\n"
        "/model — Select AI model\n"
        "/voice — Toggle voice output for this thread\n\n"

        "<b>📊 Status:</b>\n"
        "/status — CLI health check\n"
        "/git — Git info (branch, log, status, diff)\n"
        "/voice — Toggle voice output (/voice status for config)\n"
        "/skills — List BMAD workflows\n"
        "/help — This message\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Provider: <code>{_escape_html(provider_name)}</code>\n"
        f"🤖 Model: <code>{_escape_html(model)}</code>",
        parse_mode=ParseMode.HTML,
    )


# ── Model Selection ──────────────────────────────────────────────

# Emoji indicators for model tiers
_MODEL_EMOJI = {
    "auto": "🔄",
    "claude-opus": "🟣",
    "claude-sonnet": "🔵",
    "claude-haiku": "🟢",
    "deepseek": "🟠",
    "minimax": "🟡",
    "glm": "🟤",
    "qwen": "⚪",
}


def _model_emoji(model_id: str) -> str:
    """Get emoji for a model based on its family."""
    for prefix, emoji in _MODEL_EMOJI.items():
        if model_id.startswith(prefix):
            return emoji
    return "⚙️"


@authorized
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model — show model selection keyboard."""
    await update.message.reply_chat_action(ChatAction.TYPING)

    # Resolve per-thread provider to list correct models
    thread_id = _get_thread_id(update) or DEFAULT_THREAD_ID
    try:
        resolved = await db.resolve_thread_config(
            thread_id,
            env_project_dir=config.project_dir,
            env_cli_provider=config.cli_provider,
            path=DB_PATH,
        )
        resolved_provider_name = resolved.cli_provider
    except Exception:
        resolved_provider_name = None

    models = await runner.list_models(provider_name=resolved_provider_name)
    if not models:
        await update.message.reply_text("⚠️ Could not fetch models from CLI.")
        return

    current = context.user_data.get("model", "auto")

    # Build inline keyboard — 2 columns
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for m in models:
        mid = m["model_id"]
        name = m["model_name"]
        rate = m.get("rate_multiplier", 1.0)
        emoji = _model_emoji(mid)
        check = " ✓" if mid == current else ""
        label = f"{emoji} {name} ({rate}x){check}"

        row.append(InlineKeyboardButton(label, callback_data=f"model:{mid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    await update.message.reply_text(
        f"🤖 <b>Chọn AI Model</b>\n\n"
        f"Đang dùng: <code>{current}</code>\n"
        f"Giá hiển thị là credit multiplier (thấp = rẻ hơn).",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@authorized
async def handle_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callback for model selection."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data or not data.startswith("model:"):
        return

    model_id = data.removeprefix("model:")
    context.user_data["model"] = model_id

    # Persist to SQLite (per-thread) so it survives bot restart
    thread_id = _get_thread_id(update) or DEFAULT_THREAD_ID
    try:
        await db.upsert_thread_config(thread_id, model=model_id, path=DB_PATH)
    except ValueError:
        # Thread has no row yet (no project_dir) — only store in user_data
        pass

    emoji = _model_emoji(model_id)
    await query.edit_message_text(
        f"{emoji} Model đã chuyển sang: <b>{model_id}</b>",
        parse_mode=ParseMode.HTML,
    )
    logger.info("User %s switched model to: %s (thread=%s)", update.effective_user.id, model_id, thread_id)


# ── Other Commands ───────────────────────────────────────────────

@authorized
async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /skills — list available BMAD skills."""
    skills_dir = Path(config.project_dir) / ".kiro" / "skills"
    if not skills_dir.is_dir():
        await update.message.reply_text("⚠️ No .kiro/skills/ directory found in project.")
        return

    bmad_skills = sorted(
        d.name for d in skills_dir.iterdir()
        if d.is_dir() and d.name.startswith("bmad-")
    )

    if not bmad_skills:
        await update.message.reply_text("⚠️ No BMAD skills found.")
        return

    agents = [s for s in bmad_skills if "agent-" in s]
    workflows = [s for s in bmad_skills if s not in agents]

    lines = ["🧠 <b>BMAD Skills Available</b>\n"]
    if workflows:
        lines.append("<b>Workflows:</b>")
        for skill in workflows:
            lines.append(f"  <code>/{skill}</code>")
    if agents:
        lines.append("\n<b>Agents:</b>")
        for skill in agents:
            lines.append(f"  <code>/{skill}</code>")
    lines.append(f"\n📊 Total: {len(bmad_skills)} skills")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status — check CLI availability and session health.

    Shows: CLI binary status, auth check, session pool stats,
    current thread state, model, project, timeout.
    """
    await update.message.reply_chat_action(ChatAction.TYPING)

    thread_id = _get_thread_id(update)
    model = context.user_data.get("model", "auto")

    # Resolve per-thread provider for status check
    effective_thread_id = thread_id if thread_id is not None else DEFAULT_THREAD_ID
    try:
        resolved = await db.resolve_thread_config(
            effective_thread_id,
            env_project_dir=config.project_dir,
            env_cli_provider=config.cli_provider,
            path=DB_PATH,
        )
        resolved_provider_name = resolved.cli_provider
    except Exception:
        resolved_provider_name = None

    # CLI health check (shells out — expected for /status)
    cli_status = await runner.check_status(provider_name=resolved_provider_name)

    # Session stats
    active = runner._session_mgr.active_count()
    max_s = runner._session_mgr.max_sessions

    # Current thread state
    thread_state = runner._session_mgr.get_state(thread_id)
    if thread_state:
        emoji = SessionManager.get_status_emoji(thread_state)
        thread_info = f"{emoji} {thread_state.value}"
    else:
        thread_info = "No session"

    # Thread config (best-effort)
    try:
        thread_config = await db.get_thread_config(
            thread_id if thread_id is not None else DEFAULT_THREAD_ID, path=DB_PATH
        )
    except Exception:
        thread_config = None

    project_name = (
        Path(thread_config.project_dir).name
        if thread_config and thread_config.project_dir
        else Path(config.project_dir).name
    )
    timeout = (
        thread_config.timeout_seconds
        if thread_config and thread_config.timeout_seconds
        else config.cli_timeout
    )

    lines = [
        "🔍 <b>CLI Status</b>\n",
        _escape_html(cli_status),
        f"\n⚡ Sessions: {active}/{max_s} active",
        f"🧵 This thread: {thread_info}",
        f"\n🤖 Model: <code>{_escape_html(model)}</code>",
        f"📁 Project: <code>{_escape_html(project_name)}</code>",
        f"⏱️ Timeout: {timeout}s",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def _format_duration(seconds: float) -> str:
    """Format elapsed seconds as human-readable duration.

    Examples: 45s → "45s", 3660s → "1h 1m", 125s → "2m 5s"
    Clamps negative values to 0 (logs warning for debugging).
    """
    if seconds < 0:
        logger.debug("[_format_duration] negative seconds=%.2f, clamping to 0", seconds)
        seconds = 0
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


def _render_info_no_session(
    project_name: str,
    project_dir: str,
    provider_name: str,
    thread_provider: str,
    model: str,
    timeout_s: int,
    thread_label: str,
    active_total: int,
    max_slots: int,
    cli_path: str,
) -> str:
    """Render /info output when no active session exists."""
    esc = _escape_html
    lines = [
        f"📁 <b>{esc(project_name)}</b>",
        f"   <code>{esc(project_dir)}</code>",
        "",
        "💤 <b>No active session</b>",
        "",
        f"📡 {esc(provider_name)} (<code>{esc(thread_provider)}</code>)  |  🤖 <code>{esc(model)}</code>",
        f"👤 Logged-in: <i>see /status</i>",
        "",
        "<i>Send a message to start a session.</i>",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🧵 Thread: <code>{esc(thread_label)}</code>  |  "
        f"⚙️ Timeout: {timeout_s}s  |  "
        f"⚡ Pool: {active_total}/{max_slots}",
        f"🛠 Binary: <code>{esc(cli_path)}</code>",
    ]
    return "\n".join(lines)


def _render_info_active(
    project_name: str,
    project_dir: str,
    provider_name: str,
    thread_provider: str,
    model: str,
    status_emoji: str,
    state_name: str,
    ready_mark: str,
    duration: str,
    idle_for: str,
    msg_count: int,
    usage_text: str,
    timeout_s: int,
    thread_label: str,
    active_total: int,
    max_slots: int,
    pid: int,
    cli_path: str,
    pending_decision_remaining: str | None,
) -> str:
    """Render /info output when an active session exists."""
    esc = _escape_html
    lines = [
        # 1. Project — top priority, bold
        f"📁 <b>{esc(project_name)}</b>",
        f"   <code>{esc(project_dir)}</code>",
        "",
        # 2. Status — what's happening right now
        f"{status_emoji} <b>{esc(state_name)}</b>  ({ready_mark})",
    ]

    # 3. Pending-decision alert — if applicable, right after status
    if pending_decision_remaining is not None:
        lines.append(
            f"⚠️ <b>Waiting for your reply</b> — expires in {pending_decision_remaining}"
        )

    lines.extend([
        "",
        # 4. Provider + model — who you're talking to
        f"📡 {esc(provider_name)} (<code>{esc(thread_provider)}</code>)  |  🤖 <code>{esc(model)}</code>",
        f"👤 Logged-in: <i>see /status</i>",
        "",
        # 5. Session usage — duration, activity, messages
        f"⏱️ Duration: <b>{duration}</b>  (last activity: {idle_for} ago)",
        f"💬 Messages: <b>{msg_count}</b>",
        f"💳 Usage: {esc(usage_text)}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        # 6. Technical details — debug row
        f"🧵 Thread: <code>{esc(thread_label)}</code>  |  "
        f"⚙️ Timeout: {timeout_s}s  |  "
        f"⚡ Pool: {active_total}/{max_slots}",
        f"🔢 PID: <code>{pid}</code>  |  "
        f"🛠 Binary: <code>{esc(cli_path)}</code>",
    ])

    return "\n".join(lines)


@authorized
async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /info — show current session details (read-only).

    Never shells out to CLI (must be instant). Never mutates session state.
    """
    if update.message is None:
        return

    await update.message.reply_chat_action(ChatAction.TYPING)

    thread_id = _get_thread_id(update)
    lookup_id = thread_id if thread_id is not None else DEFAULT_THREAD_ID

    # Thread config from SQLite (best-effort)
    try:
        thread_config = await db.get_thread_config(lookup_id, path=DB_PATH)
    except Exception as exc:
        logger.warning("[cmd_info] failed to load thread config: %s", exc)
        thread_config = None

    provider = runner.provider
    # Resolve per-thread provider for display
    thread_provider = (
        thread_config.cli_provider if thread_config and thread_config.cli_provider else config.cli_provider
    )
    effective_provider = runner.get_provider_for_thread(thread_provider)
    provider_name = effective_provider.name
    cli_path = effective_provider.config.cli_path
    # Model resolution: thread_config.model → user_data → "default"
    model = (
        (thread_config.model if thread_config and thread_config.model else None)
        or context.user_data.get("model")
        or "default"
    )
    project_dir = (thread_config.project_dir if thread_config else None) or config.project_dir
    project_name = Path(project_dir).name if project_dir else "—"
    timeout_s = (
        thread_config.timeout_seconds
        if thread_config and thread_config.timeout_seconds is not None and thread_config.timeout_seconds > 0
        else config.cli_timeout
    )

    # Session pool stats
    session_mgr = runner._session_mgr
    active_total = session_mgr.active_count()
    max_slots = session_mgr.max_sessions
    thread_label = str(thread_id) if thread_id is not None else "main"

    # Active session info (from memory, no CLI shell-out)
    session = session_mgr.get(thread_id)

    # ── Branch A: no active session ──────────────────────────────
    if session is None or session.state == PtyState.DEAD:
        text = _render_info_no_session(
            project_name=project_name,
            project_dir=project_dir,
            provider_name=provider_name,
            thread_provider=thread_provider,
            model=model,
            timeout_s=timeout_s,
            thread_label=thread_label,
            active_total=active_total,
            max_slots=max_slots,
            cli_path=cli_path,
        )
        # Append voice state if voice is configured (Story 6.3)
        if config.voice_enabled:
            voice_out = await _is_voice_output_enabled(thread_id, context)
            text += f"\n🎤 Voice output: {'🔊 on' if voice_out else '🔇 off'}"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    # ── Branch B: active session ─────────────────────────────────
    now = time.monotonic()
    duration = _format_duration(now - session.created_at)
    idle_for = _format_duration(now - session.last_active_at)
    msg_count = _thread_sessions.get(thread_id, 0)
    status_emoji = SessionManager.get_status_emoji(session.state)
    state_name = session.state.value
    ready_mark = "ready" if session.ready else "warming up"
    usage_text = effective_provider.parse_usage_output("") or "Usage data not available for this provider"

    pending_remaining: str | None = None
    if session.state == PtyState.WAITING_FOR_USER:
        elapsed = now - session.last_active_at
        remaining = max(0, config.decision_reply_timeout - int(elapsed))
        pending_remaining = _format_duration(remaining)

    text = _render_info_active(
        project_name=project_name,
        project_dir=project_dir,
        provider_name=provider_name,
        thread_provider=thread_provider,
        model=model,
        status_emoji=status_emoji,
        state_name=state_name,
        ready_mark=ready_mark,
        duration=duration,
        idle_for=idle_for,
        msg_count=msg_count,
        usage_text=usage_text,
        timeout_s=timeout_s,
        thread_label=thread_label,
        active_total=active_total,
        max_slots=max_slots,
        pid=session.pid,
        cli_path=cli_path,
        pending_decision_remaining=pending_remaining,
    )
    # Append voice state if voice is configured (Story 6.3)
    if config.voice_enabled:
        voice_out = await _is_voice_output_enabled(thread_id, context)
        text += f"\n🎤 Voice output: {'🔊 on' if voice_out else '🔇 off'}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@authorized
async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sessions — list ALL active sessions across threads (read-only).

    Reads from SessionManager pool (memory) + SQLite thread_config (single
    batch query). Never shells out, never mutates session state. Paginates
    at 10 sessions. Filters out DEAD sessions from the display list.
    Sorted by thread_id for stable pagination.
    """
    if update.message is None:
        return

    await update.message.reply_chat_action(ChatAction.TYPING)

    session_mgr = runner._session_mgr
    sessions = session_mgr.list_all()
    max_slots = session_mgr.max_sessions
    active_total = session_mgr.active_count()

    # Filter out DEAD sessions — only show actionable ones
    live_sessions = {
        tid: s for tid, s in sessions.items() if s.state != PtyState.DEAD
    }

    # ── Branch A: no live sessions ───────────────────────────────
    if not live_sessions:
        await update.message.reply_text(
            "📋 <b>No active sessions.</b>\n\n"
            "<i>Send a message in any thread to start one.</i>\n"
            f"Slots available: {max_slots}/{max_slots}",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Branch B: list sessions (paginated at 10) ────────────────
    # Sort by thread_id for stable pagination (None → main chat first)
    items = sorted(
        live_sessions.items(),
        key=lambda x: (x[0] is not None, x[0] if x[0] is not None else -1),
    )
    page_size = 10
    display_items = items[:page_size]

    # Batch-load thread configs once (fixes N+1 query pattern)
    try:
        all_configs = await db.list_all_threads(path=DB_PATH)
        config_by_tid = {cfg.thread_id: cfg for cfg in all_configs}
    except Exception as exc:
        logger.warning("[cmd_sessions] list_all_threads failed: %s", exc)
        config_by_tid = {}

    default_provider_id = runner.provider.provider_id
    now = time.monotonic()

    lines: list[str] = [
        f"📋 <b>Active Sessions</b>  ({active_total}/{max_slots} slots)",
        "",
    ]

    for tid, session in display_items:
        lookup_id = tid if tid is not None else DEFAULT_THREAD_ID
        thread_config = config_by_tid.get(lookup_id)

        project = (
            Path(thread_config.project_dir).name
            if thread_config and thread_config.project_dir
            else "—"
        )
        model = (
            thread_config.model
            if thread_config and thread_config.model
            else "default"
        )
        thread_provider = (
            thread_config.cli_provider
            if thread_config and thread_config.cli_provider
            else default_provider_id
        )

        emoji = SessionManager.get_status_emoji(session.state)
        thread_label = str(tid) if tid is not None else "main"
        duration = _format_duration(now - session.created_at)
        msg_count = _thread_sessions.get(tid, 0)

        lines.append(f"{emoji} <b>Thread {_escape_html(thread_label)}</b>")
        lines.append(
            f"   📁 {_escape_html(project)}  |  "
            f"📡 {_escape_html(thread_provider)}  |  "
            f"🤖 <code>{_escape_html(str(model))}</code>"
        )

        # State-specific third line
        if session.state == PtyState.WAITING_FOR_USER:
            lines.append(f"   ⏱️ {duration}  |  ⚠️ Waiting for input")
        elif session.state == PtyState.IDLE:
            lines.append(f"   ⏱️ {duration}  |  Idle")
        else:
            lines.append(f"   ⏱️ {duration}  |  💬 {msg_count} messages")
        lines.append("")

    remaining = len(items) - len(display_items)
    if remaining > 0:
        lines.append(f"... and {remaining} more")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        f"Slots: {active_total}/{max_slots}  |  "
        "/cancel &lt;thread&gt; to free"
    )

    # Guard against Telegram 4096-char limit
    output = "\n".join(lines)
    if len(output) <= MAX_MSG_LEN:
        await update.message.reply_text(output, parse_mode=ParseMode.HTML)
    else:
        for chunk in split_message(output):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)


@authorized
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel — kill running CLI process for this thread.

    Steps:
    1. Clear pending decision state (if any)
    2. Cancel the registered asyncio task (releases per-thread lock)
    3. Kill the PTY session (via runner.cancel)
    4. Reset thread message counter so next message starts fresh
    """
    if update.message is None:
        return

    thread_id = _get_thread_id(update)

    # Clear any pending decision state for this thread
    context.bot_data.pop(f"thread:{thread_id}:pending_decision", None)

    # Cancel the running task (releases per-thread lock)
    task = _thread_tasks.pop(thread_id, None)
    task_cancelled = False
    if task is not None and not task.done():
        task.cancel()
        task_cancelled = True
        # Wait briefly for the task to actually finish (release lock)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass  # Best-effort wait; proceed regardless
        logger.info("[cmd_cancel] cancelled task [thread=%s]", thread_id)

    # Kill the PTY session (protected against exceptions)
    try:
        cancelled = await runner.cancel(thread_id)
    except Exception as exc:
        logger.warning("[cmd_cancel] runner.cancel failed: %s", exc)
        cancelled = False

    # Reset thread counter so next message starts fresh (no stale resume)
    if cancelled or task_cancelled:
        _thread_sessions[thread_id] = 0
        await update.message.reply_text("✅ Cancelled running CLI process.")
    else:
        await update.message.reply_text("ℹ️ No active CLI process to cancel.")


@authorized
async def cmd_new_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new — reset session for current thread."""
    thread_id = _get_thread_id(update)
    context.user_data["force_new_session"] = True
    context.bot_data.pop(f"thread:{thread_id}:pending_decision", None)
    _thread_sessions[thread_id] = 0

    thread_label = f"thread {thread_id}" if thread_id else "main chat"
    await update.message.reply_text(
        f"🆕 Session reset cho {thread_label}.\n"
        "Tin nhắn tiếp theo sẽ bắt đầu conversation mới."
    )


@authorized
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume — explicitly resume previous session."""
    await _execute_and_reply(update, context, "")


@authorized
async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /project <path> — bind current thread to a project directory."""
    text = update.message.text or ""
    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        await update.message.reply_text(
            "⚠️ Usage: <code>/project &lt;path&gt;</code>\n\n"
            "Example: <code>/project /home/user/myapp</code>\n"
            "Use <code>/projects</code> to see previously-used projects.",
            parse_mode=ParseMode.HTML,
        )
        return

    path = parts[1].strip()
    if not os.path.isdir(path):
        await update.message.reply_text(
            f"⚠️ Path not found or not a directory:\n<code>{_escape_html(path)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    thread_id = _get_thread_id(update) or DEFAULT_THREAD_ID
    await db.upsert_thread_config(thread_id, project_dir=path, path=DB_PATH)

    thread_label = f"thread {thread_id}" if thread_id != DEFAULT_THREAD_ID else "main chat"
    await update.message.reply_text(
        f"✅ Bound {thread_label} to project:\n<code>{_escape_html(path)}</code>",
        parse_mode=ParseMode.HTML,
    )
    logger.info("[cmd_project] thread=%s → %s", thread_id, path)


@authorized
async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /projects — show inline keyboard of previously-used projects."""
    projects = await db.list_distinct_project_dirs(path=DB_PATH)
    if not projects:
        await update.message.reply_text(
            "No previous projects found. Use <code>/project &lt;path&gt;</code> to bind a project.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Store full paths for callback lookup (callback_data has 64-byte limit)
    context.chat_data["_projects_list"] = projects

    # Mark current thread's binding if any
    thread_id = _get_thread_id(update) or DEFAULT_THREAD_ID
    current_config = await db.get_thread_config(thread_id, path=DB_PATH)
    current_path = current_config.project_dir if current_config else None

    keyboard = []
    for idx, path in enumerate(projects):
        marker = "✓ " if path == current_path else ""
        display = path if len(path) <= 55 else "..." + path[-52:]
        label = f"{marker}{display}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"project:{idx}")])

    await update.message.reply_text(
        "📂 Select a project to bind this thread to:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_projects_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle project selection from the /projects inline keyboard."""
    query = update.callback_query
    await query.answer()

    try:
        idx = int(query.data.split(":", 1)[1])
        projects = context.chat_data.get("_projects_list", [])
        path = projects[idx]
    except (ValueError, IndexError, KeyError):
        await query.edit_message_text(
            "⚠️ Invalid selection. Try /projects again."
        )
        return

    thread_id = _get_thread_id(update) or DEFAULT_THREAD_ID
    await db.upsert_thread_config(thread_id, project_dir=path, path=DB_PATH)

    thread_label = f"thread {thread_id}" if thread_id != DEFAULT_THREAD_ID else "main chat"
    await query.edit_message_text(
        f"✅ Bound {thread_label} to:\n<code>{_escape_html(path)}</code>",
        parse_mode=ParseMode.HTML,
    )
    logger.info("[cmd_projects] thread=%s → %s (from history)", thread_id, path)


@authorized
async def cmd_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /provider <name> — switch CLI provider for current thread."""
    text = update.message.text or ""
    parts = text.split(maxsplit=1)

    available = get_available_providers()

    if len(parts) < 2:
        await update.message.reply_text(
            f"⚠️ Usage: <code>/provider &lt;name&gt;</code>\n\n"
            f"Available: <code>{', '.join(sorted(available.keys()))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name = parts[1].strip().lower()
    if name not in available:
        await update.message.reply_text(
            f"⚠️ Unknown provider: <code>{_escape_html(name)}</code>\n\n"
            f"Available: <code>{', '.join(sorted(available.keys()))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    thread_id = _get_thread_id(update) or DEFAULT_THREAD_ID

    # Auto-kill active session if provider is changing (session is bound to old CLI binary)
    session = runner._sessions.get(thread_id)
    session_was_killed = False
    if session and session.alive:
        runner._kill_session(thread_id)
        session_was_killed = True
        logger.info("[cmd_provider] killed active session [thread=%s] for provider switch", thread_id)

    try:
        await db.upsert_thread_config(thread_id, cli_provider=name, path=DB_PATH)
    except ValueError:
        # No row yet and no project_dir — must bind project first
        await update.message.reply_text(
            "⚠️ No project bound yet. Use <code>/project &lt;path&gt;</code> first.",
            parse_mode=ParseMode.HTML,
        )
        return

    thread_label = f"thread {thread_id}" if thread_id != DEFAULT_THREAD_ID else "main chat"
    provider_class = available[name]
    session_note = " (previous session ended)" if session_was_killed else ""
    await update.message.reply_text(
        f"✅ {thread_label} provider switched to <b>{provider_class.name}</b> "
        f"(<code>{name}</code>){session_note}",
        parse_mode=ParseMode.HTML,
    )
    logger.info("[cmd_provider] thread=%s → %s", thread_id, name)


# ── BMAD Slash Command Handler ───────────────────────────────────

@authorized
async def handle_bmad_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /bmad_* commands → forward as CLI slash commands."""
    text = update.message.text.strip()
    command = text.split()[0]
    extra = text[len(command):].strip()

    kiro_command = command.replace("_", "-")
    prompt = kiro_command if not extra else f"{kiro_command} {extra}"

    await _execute_and_reply(update, context, prompt)


# ── Git Commands (direct, no CLI proxy) ──────────────────────────


@authorized
async def cmd_git(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /git — run git commands directly on the project directory.

    Usage:
        /git branch     — list branches, highlight current
        /git log        — last 10 commits (short format)
        /git status     — working tree status
        /git diff       — show unstaged changes (truncated)
        /git <anything> — run arbitrary git subcommand
    """
    text = (update.message.text or "").strip()
    parts = text.split(maxsplit=1)
    subcommand = parts[1] if len(parts) > 1 else "status"

    # Resolve project dir for this thread
    thread_id = _get_thread_id(update)
    lookup_id = thread_id if thread_id is not None else DEFAULT_THREAD_ID
    try:
        tc = await db.get_thread_config(lookup_id, path=DB_PATH)
        project_dir = (tc.project_dir if tc else None) or config.project_dir
    except Exception:
        project_dir = config.project_dir

    # Build git command with sensible defaults
    if subcommand == "log":
        cmd = ["git", "log", "--oneline", "--graph", "-15"]
    elif subcommand == "branch":
        cmd = ["git", "branch", "-a"]
    elif subcommand == "status":
        cmd = ["git", "status", "--short", "--branch"]
    elif subcommand == "diff":
        cmd = ["git", "diff", "--stat"]
    else:
        # Arbitrary git subcommand
        cmd = ["git"] + subcommand.split()

    await update.message.reply_chat_action(ChatAction.TYPING)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=project_dir,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        output = stdout.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        output = "⏱ Git command timed out (10s)"
    except FileNotFoundError:
        output = "❌ git not found in PATH"
    except Exception as exc:
        output = f"❌ Error: {exc}"

    if not output:
        output = "(no output)"

    # Truncate to fit Telegram limit
    if len(output) > MAX_MSG_LEN - 50:
        output = output[: MAX_MSG_LEN - 80] + "\n\n... (truncated)"

    await update.message.reply_text(
        f"<pre>{_escape_html(output)}</pre>",
        parse_mode=ParseMode.HTML,
    )


# ── Voice Toggle Command (Story 6.2 / 6.3) ──────────────────────


@authorized
async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /voice — toggle voice output, set speed, or show status.

    Subcommands:
      /voice           — toggle voice output on/off
      /voice status    — show current voice config for this thread
      /voice speed 1.5 — set TTS speed (0.25–4.0)
      /voice speed reset — clear per-thread speed, revert to global default
    """
    if not config.voice_enabled:
        await update.message.reply_text(
            "🎤 Voice features not configured.\n"
            "Set OPENAI_API_KEY in .env to enable voice."
        )
        return

    thread_id = _get_thread_id(update)
    args = context.args  # e.g., ["status"] or ["speed", "1.5"]

    if args and args[0].lower() == "status":
        await _voice_status(update, context, thread_id)
        return

    if args and args[0].lower() == "speed":
        await _voice_set_speed(update, thread_id, args[1:])
        return

    # Default: toggle voice output
    thread_key = f"thread:{thread_id}:voice_output"

    # Resolve current state: SQLite → .env → False
    current = await _resolve_voice_output(thread_id)
    new_state = not current

    # Persist to SQLite (best-effort — only updates existing rows)
    persisted = False
    try:
        tid = thread_id if thread_id is not None else DEFAULT_THREAD_ID
        tc = await db.get_thread_config(tid, path=DB_PATH)
        if tc is not None:
            await db.upsert_voice_output(tid, voice_output=new_state, path=DB_PATH)
            persisted = True
        else:
            logger.debug("[cmd_voice] thread %s has no row — session-only toggle", tid)
    except Exception as exc:
        logger.warning("[cmd_voice] SQLite persist failed: %s", exc)

    # Update in-memory cache for immediate effect
    context.bot_data[thread_key] = new_state

    persist_note = (
        "<i>Setting persisted — survives bot restart.</i>"
        if persisted
        else "<i>Session-only — use /project first to persist across restarts.</i>"
    )

    if new_state:
        await update.message.reply_text(
            "🔊 Voice output <b>enabled</b> for this thread.\n"
            f"CLI responses will include voice messages (except code-heavy ones).\n"
            f"{persist_note}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "🔇 Voice output <b>disabled</b> for this thread.\n"
            f"Use /voice again to re-enable.\n"
            f"{persist_note}",
            parse_mode=ParseMode.HTML,
        )


async def _voice_set_speed(
    update: Update, thread_id: int | None, speed_args: list[str]
) -> None:
    """Handle /voice speed <value|reset>."""
    tid = thread_id if thread_id is not None else DEFAULT_THREAD_ID

    if not speed_args:
        current = await _resolve_tts_speed(thread_id)
        await update.message.reply_text(
            f"⚡ Current TTS speed: <b>{current:.2f}x</b>\n\n"
            "Usage:\n"
            "  <code>/voice speed 1.5</code> — set speed (0.25–4.0)\n"
            "  <code>/voice speed reset</code> — revert to global default",
            parse_mode=ParseMode.HTML,
        )
        return

    raw = speed_args[0].lower()

    if raw == "reset":
        try:
            await db.upsert_tts_speed(tid, tts_speed=None, path=DB_PATH)
        except Exception as exc:
            logger.warning("[cmd_voice speed] SQLite persist failed: %s", exc)
        await update.message.reply_text(
            f"⚡ TTS speed reset to global default (<b>{config.tts_speed:.2f}x</b>).\n"
            "<i>Setting persisted — survives bot restart.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        value = float(raw)
    except ValueError:
        await update.message.reply_text(
            f"⚠️ Invalid speed: <code>{_escape_html(raw)}</code>\n"
            "Provide a number between 0.25 and 4.0, e.g. <code>/voice speed 1.5</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    clamped = max(0.25, min(4.0, value))
    clamp_note = f" (clamped from {value})" if clamped != value else ""

    # Check if thread has a row for persistence
    tc = await db.get_thread_config(tid, path=DB_PATH)
    persisted = False
    if tc is not None:
        try:
            await db.upsert_tts_speed(tid, tts_speed=clamped, path=DB_PATH)
            persisted = True
        except Exception as exc:
            logger.warning("[cmd_voice speed] SQLite persist failed: %s", exc)

    persist_note = (
        "<i>Setting persisted — survives bot restart.</i>"
        if persisted
        else "<i>Session-only — use /project first to persist across restarts.</i>"
    )

    await update.message.reply_text(
        f"⚡ TTS speed set to <b>{clamped:.2f}x</b>{clamp_note} for this thread.\n"
        f"{persist_note}",
        parse_mode=ParseMode.HTML,
    )


async def _voice_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE, thread_id: int | None
) -> None:
    """Show voice configuration status for this thread."""
    voice_out = await _resolve_voice_output(thread_id)
    has_override = await _has_thread_voice_override(thread_id)
    source = "per-thread (SQLite)" if has_override else "global default"
    tts_speed = await _resolve_tts_speed(thread_id)

    # Check if speed has a per-thread override
    tid = thread_id if thread_id is not None else DEFAULT_THREAD_ID
    tc = await db.get_thread_config(tid, path=DB_PATH)
    speed_source = "per-thread (SQLite)" if (tc and tc.tts_speed is not None) else "global default"

    lines = [
        "🎤 <b>Voice Configuration</b>\n",
        f"Voice features: {'✅ enabled' if config.voice_enabled else '❌ disabled'}",
        f"Whisper model: <code>{_escape_html(config.whisper_model)}</code>",
        f"TTS model: <code>{_escape_html(config.tts_model)}</code>",
        f"TTS voice: <code>{_escape_html(config.tts_voice)}</code>",
        "",
        "<b>This thread:</b>",
        f"Voice output: {'🔊 on' if voice_out else '🔇 off'} ({source})",
        f"TTS speed: <b>{tts_speed:.2f}x</b> ({speed_source})",
        "",
        "<i>Use /voice to toggle, /voice speed &lt;value&gt; to set speed, /voice status to see this.</i>",
    ]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )

# ── Voice Message Handlers (Story 6.1) ──────────────────────────


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages — transcribe via Whisper and show confirmation.

    Auth is enforced inline (rather than via @authorized) because voice
    messages arrive via a ``filters.VOICE`` handler distinct from the text
    pipeline, and we want the "voice not configured" branch to still reply
    even when voice is disabled. See Story 6.1 AC.
    """
    user = update.effective_user
    if not user or user.id not in config.allowed_user_ids:
        logger.warning(
            "Unauthorized voice message from user %s",
            user.id if user else "unknown",
        )
        if update.message:
            await update.message.reply_text(
                "⛔ Unauthorized. Your user ID is not in the allowed list."
            )
        return

    if not config.voice_enabled or voice_transcriber is None:
        await update.message.reply_text(
            "🎤 Voice features not configured. Please type your message.\n"
            "Set OPENAI_API_KEY in .env to enable voice input."
        )
        return

    await update.message.reply_chat_action(ChatAction.TYPING)

    # Download the voice file from Telegram to a temp file.
    voice = update.message.voice
    voice_file = await context.bot.get_file(voice.file_id)

    # Telegram voice messages are OGG Opus; Whisper accepts OGG natively.
    import tempfile as _tempfile
    with _tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        try:
            await voice_file.download_to_drive(tmp_path)
        except Exception as exc:
            logger.warning("[voice] download failed: %s", exc)
            await update.message.reply_text(
                "⚠️ Voice transcription temporarily unavailable. Please type your message."
            )
            return

        try:
            text = await voice_transcriber.transcribe(tmp_path)
        except Exception as exc:
            # Defensive — VoiceTranscriber.transcribe() already catches,
            # but we never let a handler raise up to python-telegram-bot.
            logger.exception("[voice] unexpected transcribe error: %s", exc)
            text = None

        if not text:
            await update.message.reply_text(
                "⚠️ Voice transcription temporarily unavailable. Please type your message."
            )
            return

        # Remember this thread's pending transcription for the callback.
        thread_id = _get_thread_id(update)
        context.bot_data[f"thread:{thread_id}:voice_transcription"] = text

        # Auto-send mode: skip confirm keyboard, forward immediately.
        if config.voice_auto_send:
            await update.message.reply_text(
                f"🎤 <i>{_escape_html(text)}</i>",
                parse_mode=ParseMode.HTML,
            )
            await _execute_and_reply(update, context, text)
            return

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Send", callback_data=f"voice:send:{thread_id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"voice:edit:{thread_id}"),
            InlineKeyboardButton("🗑️ Cancel", callback_data=f"voice:cancel:{thread_id}"),
        ]])

        await update.message.reply_text(
            f"🎤 <b>Transcription:</b>\n\n<i>{_escape_html(text)}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def handle_voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice transcription confirmation callbacks (send/edit/cancel)."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    # Auth check — callback handler is registered separately from messages.
    user = query.from_user
    if not user or user.id not in config.allowed_user_ids:
        logger.warning(
            "Unauthorized voice callback from user %s",
            user.id if user else "unknown",
        )
        return

    # callback_data format: "voice:<action>:<thread_id>"
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        return
    _, action, thread_id_str = parts
    try:
        thread_id: int | None = int(thread_id_str) if thread_id_str != "None" else None
    except ValueError:
        return

    transcription_key = f"thread:{thread_id}:voice_transcription"
    text = context.bot_data.get(transcription_key, "")

    if action == "send":
        # Guard: transcription may be lost if bot restarted between voice
        # message and button tap (bot_data is in-memory only).
        if not text:
            try:
                await query.edit_message_text(
                    "⚠️ Transcription expired. Please send a new voice message."
                )
            except Exception:
                pass
            return

        context.bot_data.pop(transcription_key, None)
        try:
            await query.edit_message_text(
                f"🎤 ✅ <i>{_escape_html(text)}</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            # Non-fatal — forwarding matters more than rendering.
            pass
        # Forward to CLI as if the user had typed it.
        # Callback queries have update.message=None; build a lightweight
        # proxy so _execute_and_reply can call reply_text/reply_chat_action
        # on the original message that carried the inline keyboard.
        class _UpdateProxy:
            """Thin proxy that redirects .message to query.message."""
            def __init__(self, real_update, msg):
                object.__setattr__(self, '_real', real_update)
                object.__setattr__(self, 'message', msg)
            def __getattr__(self, name):
                return getattr(object.__getattribute__(self, '_real'), name)

        proxy = _UpdateProxy(update, query.message)
        await _execute_and_reply(proxy, context, text)

    elif action == "edit":
        # Next text message in this thread replaces the transcription.
        context.bot_data[f"thread:{thread_id}:voice_edit_mode"] = True
        try:
            await query.edit_message_text(
                f"🎤 ✏️ Original: <i>{_escape_html(text)}</i>\n\n"
                "Type your corrected message:",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    elif action == "cancel":
        context.bot_data.pop(transcription_key, None)
        try:
            await query.edit_message_text("🎤 🗑️ Voice message cancelled.")
        except Exception:
            pass


# ── Free-form Message Handler ────────────────────────────────────

@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-form text messages → forward to CLI (or pipe as decision reply)."""
    text = update.message.text
    if not text or not text.strip():
        return

    thread_id = _get_thread_id(update)

    # Voice edit mode — user is typing a corrected transcription (Story 6.1)
    edit_key = f"thread:{thread_id}:voice_edit_mode"
    if context.bot_data.get(edit_key):
        context.bot_data.pop(edit_key, None)
        context.bot_data.pop(f"thread:{thread_id}:voice_transcription", None)
        await _execute_and_reply(update, context, text.strip())
        return

    pending_key = f"thread:{thread_id}:pending_decision"

    # If a decision is pending, treat this message as the reply
    if context.bot_data.get(pending_key):
        context.bot_data.pop(pending_key, None)
        await _pipe_decision_reply(update, context, text.strip(), thread_id)
        return

    await _execute_and_reply(update, context, text.strip())


# ── Core Execute + Reply (Streaming) ─────────────────────────────

# Telegram rate limits: ~30 edits/min/chat → update every ~2s
_STREAM_UPDATE_INTERVAL = 1.5

# Max chars to show in streaming preview (keep it readable)
_STREAM_PREVIEW_MAX = 3000

# Send typing indicator every N seconds to keep Telegram alive
_TYPING_KEEPALIVE_INTERVAL = 4.0


async def _execute_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
) -> None:
    """Execute a prompt via CLI with streaming output.

    Features:
    - Per-thread parallel execution (different threads run concurrently)
    - Same-thread messages queue sequentially (shared session)
    - Progressive message edits (ChatGPT-like streaming)
    - Typing indicator keepalive
    - Idle watchdog (kills stuck processes)
    - Sends partial output if process dies mid-stream
    - Task is registered in _thread_tasks so /cancel can abort it
    """
    thread_id = _get_thread_id(update)

    # Notify if this thread is already busy (will queue behind the lock)
    if runner.is_busy(thread_id):
        await update.message.reply_text(
            "⏳ This thread has a running request — your message is queued.\n"
            "Use /cancel to stop the current one."
        )

    # Register current task so /cancel can abort it
    task = asyncio.current_task()
    if task is not None:
        _thread_tasks[thread_id] = task

    try:
        # The runner's per-thread lock handles queuing automatically
        await _execute_and_reply_inner(update, context, prompt, thread_id)
    except asyncio.CancelledError:
        logger.info("[execute_and_reply] cancelled [thread=%s]", thread_id)
        # Re-raise so asyncio knows we honored the cancellation
        raise
    finally:
        # Only unregister if our task is still the registered one
        if _thread_tasks.get(thread_id) is task:
            _thread_tasks.pop(thread_id, None)


async def _handle_decision_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    decision: DecisionPrompt,
    thread_id: int | None,
) -> None:
    """Forward a detected CLI decision prompt to the user via Telegram.

    Sets pending_decision flag in bot_data so the next user message is
    treated as a reply to this prompt (see handle_message).
    """
    context_text = "\n".join(decision.context_lines[-5:])
    msg = (
        "⚠️ <b>CLI is waiting for input</b>\n\n"
        f"<pre>{_escape_html(context_text)}</pre>\n\n"
        "Reply to proceed, or /cancel to abort."
    )
    try:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("[decision] failed to forward prompt: %s", exc)
        await update.message.reply_text(
            "⚠️ CLI is waiting for input. Reply to proceed or /cancel to abort."
        )
    logger.info(
        "[decision] forwarded to user [thread=%s]: %s",
        thread_id, decision.prompt_text[:80],
    )


async def _pipe_decision_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reply: str,
    thread_id: int | None,
) -> None:
    """Pipe user's reply to the waiting CLI session and stream remaining output."""
    logger.info("[pipe_reply] routing user message as decision reply [thread=%s]", thread_id)

    # Resolve per-thread config for timeout
    try:
        resolved = await db.resolve_thread_config(
            thread_id if thread_id is not None else DEFAULT_THREAD_ID,
            env_project_dir=config.project_dir,
            env_cli_provider=config.cli_provider,
            env_timeout_seconds=config.cli_timeout,
            path=DB_PATH,
        )
        resolved_timeout = resolved.timeout_seconds
    except Exception:
        resolved_timeout = config.cli_timeout

    await _stream_to_telegram(
        update, context, thread_id,
        stream=runner.pipe_reply_stream(
            thread_id=thread_id, reply=reply, timeout_seconds=resolved_timeout,
        ),
    )


async def _stream_to_telegram(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    thread_id: int | None,
    stream,
) -> None:
    """Shared stream-to-Telegram helper for both execute and pipe_reply paths.

    Handles DecisionPrompt yields, preview edits, final response formatting.
    """
    chat_id = update.effective_chat.id

    try:
        preview_msg = await update.message.reply_text("⏳ Connecting...")
    except Exception as exc:
        logger.exception("Failed to send preview message: %s", exc)
        return

    typing_active = True

    async def _typing_keepalive():
        while typing_active:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(_TYPING_KEEPALIVE_INTERVAL)

    typing_task = asyncio.create_task(_typing_keepalive())

    raw_lines: list[str] = []
    preview_buffer = ""
    last_edit_time = 0.0
    response_started = False
    response_marker = runner.provider.response_marker
    if not response_marker:
        response_started = True

    import time as _t

    try:
        async for line in stream:
            if isinstance(line, DecisionPrompt):
                context.bot_data[f"thread:{thread_id}:pending_decision"] = True
                await _handle_decision_prompt(update, context, line, thread_id)
                typing_active = False
                if not typing_task.done():
                    typing_task.cancel()
                # Delete preview placeholder (decision prompt sent separately)
                try:
                    await preview_msg.delete()
                except Exception:
                    pass
                return

            raw_lines.append(line)
            now = _t.monotonic()
            clean_line = strip_streaming_noise(strip_ansi(line)).rstrip()
            if not clean_line:
                continue

            if response_marker and not response_started and clean_line.startswith(response_marker):
                response_started = True

            if response_started:
                preview_buffer += clean_line + "\n"
                if now - last_edit_time >= _STREAM_UPDATE_INTERVAL:
                    preview = preview_buffer[-_STREAM_PREVIEW_MAX:]
                    if len(preview_buffer) > _STREAM_PREVIEW_MAX:
                        preview = "...\n" + preview
                    try:
                        await preview_msg.edit_text(f"<pre>{_escape_html(preview)}</pre>", parse_mode=ParseMode.HTML)
                        last_edit_time = now
                    except Exception:
                        pass
            else:
                if now - last_edit_time >= _STREAM_UPDATE_INTERVAL:
                    status_line = clean_line[:100]
                    try:
                        await preview_msg.edit_text(f"🔧 {_escape_html(status_line)}", parse_mode=ParseMode.HTML)
                        last_edit_time = now
                    except Exception:
                        pass
    finally:
        typing_active = False
        if not typing_task.done():
            typing_task.cancel()

    # Compose final response
    raw_output = "".join(raw_lines)
    final = format_output(raw_output)
    if not final.strip():
        final = "(CLI returned no output)"

    # Delete preview, send final
    try:
        await preview_msg.delete()
    except Exception:
        pass

    for chunk in split_message(final):
        await _send_html_with_fallback(update, chunk)

    # Screenshot forwarding (Story 5.1 / FR32) — post-processing after text.
    # Scope detection to the final response only (not tool invocation logs) to
    # avoid forwarding paths that the CLI merely *mentioned* vs actually created.
    screenshot_scan_text = extract_final_response(raw_output) or raw_output
    screenshot_paths = detect_screenshots(screenshot_scan_text)
    if screenshot_paths:
        try:
            resolved = await db.resolve_thread_config(
                thread_id if thread_id is not None else DEFAULT_THREAD_ID,
                env_project_dir=config.project_dir,
                env_cli_provider=config.cli_provider,
                env_timeout_seconds=config.cli_timeout,
                path=DB_PATH,
            )
            project_dir = resolved.project_dir
        except Exception:
            project_dir = config.project_dir
        await _send_screenshots(update, screenshot_paths, project_dir=project_dir)

    # Voice output (Story 6.2 / FR35) — additive, text always sent first.
    if voice_synthesizer and await _is_voice_output_enabled(thread_id, context):
        raw_for_code_check = extract_final_response(raw_output) or raw_output
        if not is_code_heavy(raw_for_code_check):
            plain_text = _strip_html_for_tts(final)
            if plain_text and len(plain_text) > 10:
                tts_speed = await _resolve_tts_speed(thread_id)
                audio_bytes = await voice_synthesizer.synthesize(plain_text, speed=tts_speed)
                if audio_bytes:
                    await _send_voice_message(update, audio_bytes)
                else:
                    try:
                        await update.message.reply_text("🔇 Voice temporarily unavailable")
                    except Exception:
                        pass


async def _execute_and_reply_inner(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    thread_id: int | None,
) -> None:
    """Inner implementation of execute and reply."""
    model = _get_model(context)
    resume = _should_resume(thread_id, context)

    await update.message.reply_chat_action(ChatAction.TYPING)

    model_label = model or "auto"
    session_label = "resume" if resume else "new"

    # Resolve per-thread config (provider, model, timeout, project_dir from SQLite with .env fallback)
    try:
        resolved = await db.resolve_thread_config(
            thread_id if thread_id is not None else DEFAULT_THREAD_ID,
            env_project_dir=config.project_dir,
            env_cli_provider=config.cli_provider,
            env_model=model,
            env_timeout_seconds=config.cli_timeout,
            path=DB_PATH,
        )
        resolved_timeout = resolved.timeout_seconds
        resolved_model = resolved.model or model
        resolved_project_dir = resolved.project_dir
        resolved_provider_name = resolved.cli_provider
    except Exception as exc:
        logger.warning("[resolve_thread_config] fallback to defaults: %s", exc)
        resolved_timeout = config.cli_timeout
        resolved_model = model
        resolved_project_dir = config.project_dir
        resolved_provider_name = None

    # Get the effective provider for display + response marker
    effective_provider = runner.get_provider_for_thread(resolved_provider_name)
    provider_name = effective_provider.name
    response_marker = effective_provider.response_marker

    # Send initial streaming message (now shows the correct per-thread provider)
    stream_msg = await update.message.reply_text(
        f"⏳ <i>Connecting to {_escape_html(provider_name)}...</i>"
        f" [{model_label} · {session_label}]",
        parse_mode=ParseMode.HTML,
    )

    # Background typing keepalive — runs independently of stream loop
    typing_active = True

    async def _typing_keepalive():
        while typing_active:
            try:
                await update.message.reply_chat_action(ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(_TYPING_KEEPALIVE_INTERVAL)

    typing_task = asyncio.create_task(_typing_keepalive())

    # Collect all raw output + stream preview to Telegram
    raw_lines: list[str] = []
    preview_buffer = ""
    last_edit_time = 0.0
    response_started = False

    import time

    if not response_marker:
        response_started = True

    try:
        async for line in runner.execute_stream(
            prompt,
            thread_id=thread_id,
            model=resolved_model,
            resume=resume,
            timeout_seconds=resolved_timeout,
            project_dir=resolved_project_dir,
            provider_name=resolved_provider_name,
        ):
            # Handle DecisionPrompt — CLI is waiting for user input
            if isinstance(line, DecisionPrompt):
                context.bot_data[f"thread:{thread_id}:pending_decision"] = True
                await _handle_decision_prompt(update, context, line, thread_id)
                typing_active = False
                if not typing_task.done():
                    typing_task.cancel()
                return  # Exit the handler; next user message will trigger pipe_reply

            raw_lines.append(line)
            now = time.monotonic()

            # Strip ANSI and spinner/thinking noise for preview
            clean_line = strip_streaming_noise(strip_ansi(line)).rstrip()
            if not clean_line:
                continue

            # Detect response start
            if response_marker and not response_started and clean_line.startswith(response_marker):
                response_started = True
                clean_line = clean_line[len(response_marker):]

            if response_started:
                preview_buffer += clean_line + "\n"

                if now - last_edit_time >= _STREAM_UPDATE_INTERVAL:
                    preview = preview_buffer[-_STREAM_PREVIEW_MAX:]
                    if len(preview_buffer) > _STREAM_PREVIEW_MAX:
                        nl = preview.find("\n")
                        if nl > 0:
                            preview = preview[nl + 1:]
                        preview = "...\n" + preview

                    try:
                        await stream_msg.edit_text(
                            f"✍️ <i>Streaming...</i>\n\n"
                            f"<pre>{_escape_html(preview.rstrip())}</pre>",
                            parse_mode=ParseMode.HTML,
                        )
                        last_edit_time = now
                    except Exception:
                        pass
            else:
                if now - last_edit_time >= _STREAM_UPDATE_INTERVAL:
                    status_line = clean_line[:100]
                    try:
                        await stream_msg.edit_text(
                            f"⚙️ <i>{_escape_html(status_line)}</i>",
                            parse_mode=ParseMode.HTML,
                        )
                        last_edit_time = now
                    except Exception:
                        pass
    finally:
        # Stop typing keepalive
        typing_active = False
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

    # Track this thread for future resume
    _track_thread(thread_id)

    # Delete the streaming preview message
    try:
        await stream_msg.delete()
    except Exception:
        pass

    # Format and send final result
    full_output = "".join(raw_lines)
    logger.debug("[raw_output] %r", full_output[:500])
    is_error = runner.get_exit_code(thread_id) != 0
    output = format_output(full_output, is_error)
    chunks = split_message(output)

    if not chunks or all(not c.strip() for c in chunks):
        # No meaningful output — send a notice
        await update.message.reply_text(
            "⚠️ CLI returned no output. The process may have been stuck.\n"
            "Try /cancel then send your message again."
        )
        return

    for chunk in chunks:
        await _send_html_with_fallback(update, chunk)

    # Screenshot forwarding (Story 5.1 / FR32) — post-processing after text.
    # Scope detection to the final response only (not tool invocation logs) to
    # avoid forwarding paths that the CLI merely *mentioned* vs actually created.
    screenshot_scan_text = extract_final_response(full_output) or full_output
    screenshot_paths = detect_screenshots(screenshot_scan_text)
    if screenshot_paths:
        await _send_screenshots(
            update, screenshot_paths, project_dir=resolved_project_dir
        )

    # Voice output (Story 6.2 / FR35) — additive, text always sent first.
    if voice_synthesizer and await _is_voice_output_enabled(thread_id, context):
        raw_for_code_check = extract_final_response(full_output) or full_output
        if not is_code_heavy(raw_for_code_check):
            plain_text = _strip_html_for_tts(output)
            if plain_text and len(plain_text) > 10:
                tts_speed = await _resolve_tts_speed(thread_id)
                audio_bytes = await voice_synthesizer.synthesize(plain_text, speed=tts_speed)
                if audio_bytes:
                    await _send_voice_message(update, audio_bytes)
                else:
                    try:
                        await update.message.reply_text("🔇 Voice temporarily unavailable")
                    except Exception:
                        pass


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _strip_html_for_tts(html_text: str) -> str:
    """Strip HTML tags and entities for plain-text TTS input."""
    import html as _html_mod
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", html_text)
    # Decode all HTML entities (handles &lt; &gt; &amp; &quot; &#39; etc.)
    text = _html_mod.unescape(text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def _is_voice_output_enabled(
    thread_id: int | None, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Check if voice output is enabled for this thread (async, Story 6.3).

    Resolution order:
      1. In-memory cache (context.bot_data) — fast path
      2. SQLite per-thread override — populated on cache miss
      3. Global config default (config.voice_output_enabled)
    """
    thread_key = f"thread:{thread_id}:voice_output"

    # Fast path: in-memory cache
    cached = context.bot_data.get(thread_key)
    if cached is not None:
        return cached

    # Cache miss — resolve from SQLite
    resolved = await _resolve_voice_output(thread_id)
    context.bot_data[thread_key] = resolved
    return resolved


async def _resolve_voice_output(thread_id: int | None) -> bool:
    """Resolve voice output setting: SQLite → .env → False."""
    tid = thread_id if thread_id is not None else DEFAULT_THREAD_ID
    tc = await db.get_thread_config(tid, path=DB_PATH)
    if tc is not None and tc.voice_output is not None:
        return bool(tc.voice_output)
    return config.voice_output_enabled


async def _resolve_tts_speed(thread_id: int | None) -> float:
    """Resolve TTS speed: SQLite per-thread → .env global → 1.5."""
    tid = thread_id if thread_id is not None else DEFAULT_THREAD_ID
    tc = await db.get_thread_config(tid, path=DB_PATH)
    if tc is not None and tc.tts_speed is not None:
        return float(tc.tts_speed)
    return config.tts_speed


async def _has_thread_voice_override(thread_id: int | None) -> bool:
    """Check if this thread has an explicit voice_output override in SQLite."""
    tid = thread_id if thread_id is not None else DEFAULT_THREAD_ID
    tc = await db.get_thread_config(tid, path=DB_PATH)
    return tc is not None and tc.voice_output is not None


async def _send_voice_message(update: Update, audio_bytes: bytes) -> None:
    """Send audio bytes as a Telegram voice message.

    Supports OGG Opus (from OpenAI TTS) and MP3 (from edge-tts).
    Telegram's sendVoice accepts both formats.
    """
    import io as _io
    from telegram import InputFile as _InputFile

    # Detect format from magic bytes to set correct filename/MIME
    # MP3: starts with 0xFF 0xFB, 0xFF 0xF3, 0xFF 0xF2, or ID3 tag
    # OGG: starts with "OggS"
    if audio_bytes[:4] == b"OggS":
        filename = "response.ogg"
    elif audio_bytes[:3] == b"ID3" or (len(audio_bytes) >= 2 and audio_bytes[0] == 0xFF and audio_bytes[1] in (0xFB, 0xF3, 0xF2, 0xFA)):
        filename = "response.mp3"
    else:
        filename = "response.ogg"  # default — let Telegram figure it out

    try:
        voice_file = _InputFile(_io.BytesIO(audio_bytes), filename=filename)
        await update.message.reply_voice(voice=voice_file)
    except Exception as exc:
        logger.warning("[voice] failed to send voice message: %s", exc)


async def _send_html_with_fallback(update: Update, text: str) -> None:
    """Send a message as HTML, falling back to plain text on parse error."""
    try:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.warning("HTML parse failed, falling back to plain text: %s", exc)
        plain = re.sub(r"<[^>]+>", "", text)
        try:
            await update.message.reply_text(plain[:MAX_MSG_LEN])
        except Exception as exc2:
            logger.error("Failed to send even plain text: %s", exc2)


# ── Screenshot Forwarding (Story 5.1) ────────────────────────────

# Telegram API limit for inline photos (sendPhoto). Larger files must go
# through sendDocument.
_TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


async def _send_screenshots(
    update: Update,
    screenshot_paths: list[str],
    project_dir: str | None = None,
) -> int:
    """Send detected screenshots as Telegram photos (or documents if >10MB).

    Graceful degradation: missing files, oversize images, or API errors are
    logged and skipped — they never raise to the caller.

    A 0.5s delay is inserted between sends to respect Telegram's per-chat
    rate limit (30 messages/min).

    Args:
        update: Telegram Update (used for reply context).
        screenshot_paths: Paths detected from CLI output.
        project_dir: Base directory for resolving relative paths (``./foo.png``).
            Falls back to the current working directory if not provided.

    Returns:
        Number of screenshots successfully delivered to Telegram.
    """
    if not screenshot_paths:
        return 0

    sent = 0
    for raw_path in screenshot_paths:
        # Resolve relative paths against the thread's project_dir.
        if raw_path.startswith("./") and project_dir:
            resolved_path = os.path.join(project_dir, raw_path[2:])
        else:
            resolved_path = raw_path

        # Existence check
        try:
            if not os.path.isfile(resolved_path):
                logger.warning("[screenshot] file not found: %s", resolved_path)
                continue
        except OSError as exc:
            logger.warning("[screenshot] stat failed for %s: %s", resolved_path, exc)
            continue

        # Size check — decides photo vs document
        try:
            size = os.path.getsize(resolved_path)
        except OSError as exc:
            logger.warning("[screenshot] getsize failed for %s: %s", resolved_path, exc)
            continue

        filename = os.path.basename(resolved_path)

        try:
            if size <= _TELEGRAM_PHOTO_MAX_BYTES:
                with open(resolved_path, "rb") as fp:
                    await update.message.reply_photo(
                        photo=InputFile(fp, filename=filename),
                        caption=f"📸 {filename}",
                    )
            else:
                size_mb = size / (1024 * 1024)
                with open(resolved_path, "rb") as fp:
                    await update.message.reply_document(
                        document=InputFile(fp, filename=filename),
                        caption=f"📎 {filename} ({size_mb:.1f}MB)",
                    )
            sent += 1
            # Rate-limit guard: Telegram allows ~30 messages/min/chat.
            # Pause briefly between sends when there are more to come.
            if sent < len(screenshot_paths):
                await asyncio.sleep(0.5)
        except Exception as exc:
            # Telegram API errors, file I/O races, etc. — log and skip.
            logger.warning(
                "[screenshot] failed to send %s: %s", resolved_path, exc
            )

    return sent


# ── Error Handler ────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and notify user."""
    logger.error("Exception while handling update:", exc_info=context.error)
    if isinstance(update, Update) and update.message:
        await update.message.reply_text(
            f"❌ Bot error: {context.error}\n\nTry again or use /cancel."
        )


# ── Main ─────────────────────────────────────────────────────────

def main() -> None:
    """Start RemoClaw."""
    logger.info("Starting RemoClaw")
    logger.info("Project: %s", config.project_dir)
    logger.info("Allowed users: %s", config.allowed_user_ids)
    logger.info("CLI Provider: %s", config.cli_provider)
    logger.info("Timeout: %ds", config.cli_timeout)

    # Initialize SQLite schema + default row (v2.0)
    asyncio.get_event_loop().run_until_complete(
        db.init_db(DB_PATH, default_project_dir=config.project_dir)
    )
    logger.info("SQLite initialized: %s", DB_PATH)

    app = (
        Application.builder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .build()
    )

    # Built-in commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("new", cmd_new_session))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("provider", cmd_provider))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("git", cmd_git))

    # Model selection callback
    app.add_handler(CallbackQueryHandler(handle_model_callback, pattern=r"^model:"))

    # Project selection callback (/projects)
    app.add_handler(CallbackQueryHandler(handle_projects_callback, pattern=r"^project:"))

    # Voice confirmation callback (Story 6.1)
    app.add_handler(CallbackQueryHandler(handle_voice_callback, pattern=r"^voice:"))

    # Voice message handler (Story 6.1)
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    # BMAD slash commands
    app.add_handler(MessageHandler(
        filters.Regex(r"^/bmad_\w+"),
        handle_bmad_command,
    ))

    # Free-form messages (must be last)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Error handler
    app.add_error_handler(error_handler)

    # Background: idle session cleanup (runs every N seconds)
    async def _post_init(app_):
        # Auto-register bot commands with Telegram (both default + private chats scope)
        from telegram import BotCommand, BotCommandScopeAllPrivateChats
        commands = [
            BotCommand("start", "Welcome message"),
            BotCommand("help", "Command reference"),
            BotCommand("new", "Start fresh session"),
            BotCommand("cancel", "Kill running process"),
            BotCommand("resume", "Resume previous session"),
            BotCommand("info", "Current session details"),
            BotCommand("sessions", "All active sessions"),
            BotCommand("model", "Select AI model"),
            BotCommand("project", "Bind thread to project"),
            BotCommand("projects", "Browse previous projects"),
            BotCommand("provider", "Switch CLI provider"),
            BotCommand("voice", "Toggle voice output"),
            BotCommand("git", "Git info"),
            BotCommand("status", "CLI health check"),
            BotCommand("skills", "List BMAD workflows"),
        ]
        await app_.bot.set_my_commands(commands)
        await app_.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        logger.info("Bot commands registered with Telegram (default + all_private_chats)")

        async def _cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(config.cleanup_interval)

                    # Snapshot threads alive before cleanup; after cleanup,
                    # any thread_id no longer in the pool was cleaned up —
                    # reset its _thread_sessions counter so the next message
                    # starts fresh (no stale resume-count).
                    before = set(runner._session_mgr.list_all().keys())

                    killed = runner._session_mgr.cleanup_idle(
                        max_age_seconds=config.idle_session_max_age
                    )
                    if killed:
                        logger.info("[cleanup_idle] killed %d idle sessions", killed)
                    orphans = runner._session_mgr.cleanup_orphans()
                    if orphans:
                        logger.warning("[cleanup_orphans] killed %d orphan sessions", orphans)

                    # Reset per-thread counters for cleaned-up threads
                    after = set(runner._session_mgr.list_all().keys())
                    for tid in before - after:
                        if tid in _thread_sessions:
                            _thread_sessions.pop(tid, None)
                            logger.debug("[cleanup] reset _thread_sessions[%s]", tid)

                    # Expired decision replies
                    expired = runner._session_mgr.expired_decisions(
                        max_wait_seconds=config.decision_reply_timeout
                    )
                    for tid in expired:
                        logger.warning("[decision] reply timeout [thread=%s]", tid)
                        app_.bot_data.pop(f"thread:{tid}:pending_decision", None)
                        # Kill session — state may transition to DEAD
                        runner._session_mgr.kill(tid)
                        # Reset per-thread counter for this thread too
                        _thread_sessions.pop(tid, None)
                        # Notify user via bot
                        try:
                            # Notify the last known chat for this thread
                            # We don't know chat_id without explicit tracking;
                            # the next user message in this thread will get "session expired"
                            logger.info(
                                "[decision] auto-killed thread %s — user will be notified on next message",
                                tid,
                            )
                        except Exception as exc:
                            logger.exception("[decision] notify failed: %s", exc)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.exception("[cleanup_loop] error: %s", exc)

        app_.bot_data["_cleanup_task"] = asyncio.create_task(_cleanup_loop())
        logger.info(
            "Started background cleanup task (interval=%ds, idle_max=%ds, decision_timeout=%ds)",
            config.cleanup_interval, config.idle_session_max_age,
            config.decision_reply_timeout,
        )

    async def _post_shutdown(app_):
        task = app_.bot_data.get("_cleanup_task")
        if task:
            task.cancel()
        runner._session_mgr.shutdown()
        logger.info("Session manager shut down")

    app.post_init = _post_init
    app.post_shutdown = _post_shutdown

    # Start polling
    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    main()
