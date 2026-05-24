"""Abstract platform interface for RemoClaw.

Any messaging platform (Telegram, WhatsApp, Zalo, Messenger, etc.)
implements this interface to plug into RemoClaw's core engine.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class IncomingMessage:
    """Platform-agnostic incoming message."""

    platform: str  # "telegram", "whatsapp", "zalo", etc.
    chat_id: str  # Unique chat/conversation identifier
    thread_id: str | None  # Thread within chat (Telegram topic, etc.)
    user_id: str  # Sender identifier
    text: str | None  # Text content (None for voice-only)
    voice_file_path: str | None  # Local path to downloaded voice file
    is_command: bool  # Whether this is a /command
    command: str | None  # Command name without slash (e.g., "help")
    command_args: list[str]  # Command arguments
    reply_to_message_id: str | None  # If replying to a specific message


@dataclass
class OutgoingMessage:
    """Platform-agnostic outgoing message."""

    text: str | None = None
    html: str | None = None  # HTML-formatted text (platform converts as needed)
    voice_bytes: bytes | None = None  # Audio data to send as voice
    image_path: str | None = None  # Path to image file to send
    edit_message_id: str | None = None  # Edit existing message instead of new
    delete_message_id: str | None = None  # Delete a message
    reply_to: str | None = None  # Reply to specific message


@dataclass
class CallbackQuery:
    """Platform-agnostic callback (inline button press, etc.)."""

    chat_id: str
    thread_id: str | None
    user_id: str
    data: str  # Callback data string
    message_id: str  # Message the button belongs to


class PlatformAdapter(ABC):
    """Abstract base for messaging platform adapters.

    Each platform implements this to handle:
    - Receiving messages and routing to core
    - Sending responses (text, voice, images)
    - Platform-specific features (typing indicators, inline buttons, etc.)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Platform name (e.g., 'telegram', 'whatsapp')."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """Start the platform adapter (begin listening for messages)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the adapter."""
        ...

    @abstractmethod
    async def send_message(self, chat_id: str, message: OutgoingMessage) -> str | None:
        """Send a message. Returns the sent message ID (for future edits)."""
        ...

    @abstractmethod
    async def send_typing(self, chat_id: str) -> None:
        """Send typing/recording indicator."""
        ...

    @abstractmethod
    async def download_voice(self, incoming: IncomingMessage) -> str | None:
        """Download voice message to local file. Returns path or None."""
        ...

    async def send_inline_buttons(
        self, chat_id: str, text: str, buttons: list[tuple[str, str]]
    ) -> str | None:
        """Send message with inline buttons [(label, callback_data), ...].

        Optional — platforms without inline buttons can fall back to numbered list.
        Returns message ID.
        """
        # Default: send as numbered list
        lines = [text, ""]
        for i, (label, _) in enumerate(buttons, 1):
            lines.append(f"{i}. {label}")
        return await self.send_message(
            chat_id, OutgoingMessage(text="\n".join(lines))
        )

    async def answer_callback(self, callback: CallbackQuery, text: str = "") -> None:
        """Acknowledge a callback query. Optional for platforms without callbacks."""
        pass

    async def register_commands(self, commands: list[tuple[str, str]]) -> None:
        """Register bot commands with the platform. Optional."""
        pass
