"""MAX messenger channel implementation using maxapi."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from maxapi import Bot, Dispatcher, F
from maxapi.types import MessageCreated, Command, BotStarted

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import MaxConfig
from nanobot.utils.helpers import split_message

MAX_MAX_MESSAGE_LEN = 4096  # MAX message character limit (adjust if needed)


class MaxChannel(BaseChannel):
    """
    MAX messenger channel using maxapi library.

    Uses polling mode to receive messages.
    """

    name = "max"

    def __init__(
        self,
        config: MaxConfig,
        bus: MessageBus,
    ):
        super().__init__(config, bus)
        self.config: MaxConfig = config
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self._running = False
        self._chat_ids: dict[str, str] = {}  # Map sender_id to chat_id for replies
        self._polling_task: asyncio.Task | None = None

    def _setup_handlers(self) -> None:
        """Set up message handlers for the dispatcher."""
        if not self.dp:
            return

        @self.dp.bot_started()
        async def on_bot_started(event: BotStarted) -> None:
            """Handle bot started event."""
            logger.info("MAX bot started successfully")

        @self.dp.message_created(Command('start'))
        async def start_handler(event: MessageCreated) -> None:
            """Handle /start command."""
            sender_id = self._get_sender_id(event)
            chat_id = self._get_chat_id(event)

            if not self.is_allowed(sender_id):
                logger.warning("Access denied for sender {} on /start", sender_id)
                return

            await event.message.answer(
                "👋 Привет! Я nanobot.\n\n"
                "Напишите сообщение, и я отвечу!\n"
                "Команды:\n"
                "/new — Начать новый разговор\n"
                "/stop — Остановить текущую задачу\n"
                "/help — Показать команды"
            )

        @self.dp.message_created(Command('new'))
        async def new_handler(event: MessageCreated) -> None:
            """Handle /new command."""
            await self._forward_command(event)

        @self.dp.message_created(Command('stop'))
        async def stop_handler(event: MessageCreated) -> None:
            """Handle /stop command."""
            await self._forward_command(event)

        @self.dp.message_created(Command('help'))
        async def help_handler(event: MessageCreated) -> None:
            """Handle /help command."""
            sender_id = self._get_sender_id(event)

            if not self.is_allowed(sender_id):
                logger.warning("Access denied for sender {} on /help", sender_id)
                return

            await event.message.answer(
                "🐈 Команды nanobot:\n"
                "/new — Начать новый разговор\n"
                "/stop — Остановить текущую задачу\n"
                "/help — Показать команды"
            )

        @self.dp.message_created(F.message.body.text)
        async def text_handler(event: MessageCreated) -> None:
            """Handle text messages (non-commands)."""
            await self._on_message(event)

        @self.dp.message_created(F.message.body.attachments)
        async def attachment_handler(event: MessageCreated) -> None:
            """Handle messages with attachments."""
            await self._on_message(event)

    async def _forward_command(self, event: MessageCreated) -> None:
        """Forward slash commands to the message bus."""
        sender_id = self._get_sender_id(event)
        chat_id = self._get_chat_id(event)
        content = event.message.body.text or ""

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            metadata=self._build_metadata(event),
        )

    async def _on_message(self, event: MessageCreated) -> None:
        """Handle incoming messages (text, media, etc.)."""
        sender_id = self._get_sender_id(event)
        chat_id = self._get_chat_id(event)

        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id

        # Check permissions
        if not self.is_allowed(sender_id):
            return

        # Extract content
        content = event.message.body.text or ""
        if not content:
            content = "[empty message]"

        # Handle media if present
        media_paths: list[str] = []
        if event.message.body.attachments:
            logger.debug("MAX message has {} attachments", len(event.message.body.attachments))
            # For now, just note in content (media download not yet implemented)
            content += f"\n[{len(event.message.body.attachments)} attachment(s)]"

        logger.debug("MAX message from {}: {}...", sender_id, content[:50])

        # Forward to message bus
        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=media_paths,
            metadata=self._build_metadata(event),
        )

    @staticmethod
    def _get_sender_id(event: MessageCreated) -> str:
        """Extract sender ID from event."""
        if event.from_user:
            return str(event.from_user.user_id)
        return str(event.message.id)

    @staticmethod
    def _get_chat_id(event: MessageCreated) -> str:
        """Extract chat ID from event."""
        if event.chat:
            return str(event.chat.chat_id)
        return str(event.message.id)

    @staticmethod
    def _build_metadata(event: MessageCreated) -> dict[str, Any]:
        """Build metadata dict from event."""
        metadata: dict[str, Any] = {
            "message_id": str(event.message.id),
        }

        if event.from_user:
            metadata["user_id"] = event.from_user.user_id
            metadata["username"] = getattr(event.from_user, "username", None)
            metadata["first_name"] = getattr(event.from_user, "first_name", None)

        if event.chat:
            metadata["chat_id"] = event.chat.chat_id
            metadata["chat_title"] = getattr(event.chat, "title", None)
            metadata["chat_type"] = getattr(event.chat, "type", None)

        return metadata

    async def start(self) -> None:
        """Start the MAX bot with polling."""
        if not self.config.token:
            logger.error("MAX bot token not configured")
            return

        self._running = True

        # Initialize bot and dispatcher
        self.bot = Bot(token=self.config.token)
        self.dp = Dispatcher()

        # Set up handlers
        self._setup_handlers()

        logger.info("Starting MAX bot (polling mode)...")

        try:
            # Start polling (this runs until stopped)
            await self.dp.start_polling(self.bot)
        except Exception as e:
            logger.error("Error in MAX bot polling: {}", e)
            self._running = False
        except asyncio.CancelledError:
            logger.info("MAX bot polling cancelled")

    async def stop(self) -> None:
        """Stop the MAX bot."""
        self._running = False
        logger.info("Stopping MAX bot...")

        # Cancel polling task if exists
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()

        # Clean up
        self.bot = None
        self.dp = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through MAX."""
        if not self.bot:
            logger.warning("MAX bot not running")
            return

        try:
            chat_id = msg.chat_id
        except Exception as e:
            logger.error("Invalid chat_id: {}: {}", msg.chat_id, e)
            return

        # Send media files first
        for media_path in (msg.media or []):
            # TODO: Implement media upload if maxapi supports it
            logger.warning("Media upload not yet implemented for MAX: {}", media_path)
            # For now, just note in text
            await self._send_text(chat_id, f"[Failed to send media: {media_path}]")

        # Send text content
        if msg.content and msg.content != "[empty message]":
            # Split long messages
            for chunk in split_message(msg.content, MAX_MAX_MESSAGE_LEN):
                await self._send_text(chat_id, chunk)

    async def _send_text(self, chat_id: str, text: str, reply_to_message_id: str | None = None) -> None:
        """Send a text message to MAX."""
        if not self.bot:
            logger.warning("MAX bot not initialized")
            return

        try:
            # Use the bot's send_message method
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
            )
        except Exception as e:
            logger.error("Error sending MAX message: {}", e)