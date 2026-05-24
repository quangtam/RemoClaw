"""WhatsApp platform adapter for RemoClaw (stub).

TODO: Implement using one of:
- WhatsApp Business API (official, requires Meta approval)
- whatsapp-web.js via Node.js bridge (unofficial, free)
- Baileys (unofficial, lightweight)
"""

import logging

from platforms.base import (
    CallbackQuery,
    IncomingMessage,
    OutgoingMessage,
    PlatformAdapter,
)

logger = logging.getLogger(__name__)


class WhatsAppAdapter(PlatformAdapter):
    """WhatsApp implementation of PlatformAdapter.

    Stub — not yet implemented. See module docstring for options.
    """

    @property
    def name(self) -> str:
        return "whatsapp"

    async def start(self) -> None:
        raise NotImplementedError(
            "WhatsApp adapter not yet implemented. "
            "See platforms/whatsapp.py for implementation options."
        )

    async def stop(self) -> None:
        pass

    async def send_message(self, chat_id: str, message: OutgoingMessage) -> str | None:
        raise NotImplementedError

    async def send_typing(self, chat_id: str) -> None:
        raise NotImplementedError

    async def download_voice(self, incoming: IncomingMessage) -> str | None:
        raise NotImplementedError
