"""Telegram platform adapter for RemoClaw.

Wraps python-telegram-bot to implement the PlatformAdapter interface.
This is the primary adapter — extracted from the original remoclaw.py.
"""

import logging

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application

from platforms.base import (
    CallbackQuery,
    IncomingMessage,
    OutgoingMessage,
    PlatformAdapter,
)
from config import Config

logger = logging.getLogger(__name__)


class TelegramAdapter(PlatformAdapter):
    """Telegram implementation of PlatformAdapter."""

    def __init__(self, config: Config):
        self.config = config
        self._app: Application | None = None

    @property
    def name(self) -> str:
        return "telegram"

    async def start(self) -> None:
        """Start Telegram polling. Called by the main entry point."""
        # The actual Telegram app is built and run in remoclaw.py
        # This adapter is used for send operations
        pass

    async def stop(self) -> None:
        pass

    def set_app(self, app: Application) -> None:
        """Inject the Telegram Application instance."""
        self._app = app

    async def send_message(self, chat_id: str, message: OutgoingMessage) -> str | None:
        if not self._app:
            return None

        bot = self._app.bot

        # Delete request
        if message.delete_message_id:
            try:
                await bot.delete_message(
                    chat_id=int(chat_id),
                    message_id=int(message.delete_message_id),
                )
            except Exception:
                pass
            return None

        # Voice message
        if message.voice_bytes:
            import io
            msg = await bot.send_voice(
                chat_id=int(chat_id),
                voice=io.BytesIO(message.voice_bytes),
            )
            return str(msg.message_id)

        # Image
        if message.image_path:
            from telegram import InputFile
            from pathlib import Path
            p = Path(message.image_path)
            if p.exists():
                msg = await bot.send_photo(
                    chat_id=int(chat_id),
                    photo=InputFile(open(p, "rb"), filename=p.name),
                )
                return str(msg.message_id)
            return None

        # Edit existing message
        if message.edit_message_id:
            text = message.html or message.text or ""
            try:
                if message.html:
                    await bot.edit_message_text(
                        chat_id=int(chat_id),
                        message_id=int(message.edit_message_id),
                        text=text,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await bot.edit_message_text(
                        chat_id=int(chat_id),
                        message_id=int(message.edit_message_id),
                        text=text,
                    )
            except Exception:
                pass
            return message.edit_message_id

        # New message
        text = message.html or message.text or ""
        try:
            if message.html:
                msg = await bot.send_message(
                    chat_id=int(chat_id),
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
            else:
                msg = await bot.send_message(
                    chat_id=int(chat_id),
                    text=text,
                )
            return str(msg.message_id)
        except Exception:
            # Fallback: strip HTML and send plain
            import re
            plain = re.sub(r"<[^>]+>", "", text)
            msg = await bot.send_message(chat_id=int(chat_id), text=plain)
            return str(msg.message_id)

    async def send_typing(self, chat_id: str) -> None:
        if self._app:
            try:
                await self._app.bot.send_chat_action(
                    chat_id=int(chat_id), action=ChatAction.TYPING
                )
            except Exception:
                pass

    async def download_voice(self, incoming: IncomingMessage) -> str | None:
        # Voice download is handled in the Telegram handler directly
        # since it needs the Update object
        return incoming.voice_file_path

    async def send_inline_buttons(
        self, chat_id: str, text: str, buttons: list[tuple[str, str]]
    ) -> str | None:
        if not self._app:
            return None
        keyboard = [
            [InlineKeyboardButton(label, callback_data=data)]
            for label, data in buttons
        ]
        msg = await self._app.bot.send_message(
            chat_id=int(chat_id),
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML,
        )
        return str(msg.message_id)

    async def answer_callback(self, callback: CallbackQuery, text: str = "") -> None:
        if self._app:
            try:
                await self._app.bot.answer_callback_query(
                    callback_query_id=callback.message_id,
                    text=text,
                )
            except Exception:
                pass

    async def register_commands(self, commands: list[tuple[str, str]]) -> None:
        if not self._app:
            return
        bot_commands = [BotCommand(cmd, desc) for cmd, desc in commands]
        await self._app.bot.set_my_commands(bot_commands)
        logger.info("Telegram: registered %d bot commands", len(commands))
