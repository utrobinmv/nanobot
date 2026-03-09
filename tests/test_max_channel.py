from types import SimpleNamespace

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.max import MaxChannel
from nanobot.config.schema import MaxConfig


class _FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []
        self.token_value = None

    def __init__(self, token=None):
        self.token_value = token
        self.sent_messages: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.sent_messages.append(kwargs)


class _FakeMessage:
    def __init__(
        self,
        message_id: str = "msg_123",
        text: str = "",
        from_user_id: str = "user_123",
        chat_id: str = "chat_123",
        attachments: list | None = None,
    ) -> None:
        self.id = message_id
        self.body = SimpleNamespace(text=text, attachments=attachments or [])
        self.from_user = SimpleNamespace(id=from_user_id, username=None, first_name="Test User")
        self.chat = SimpleNamespace(id=chat_id, type="private")

    async def answer(self, text: str) -> None:
        pass


class _FakeEvent:
    def __init__(self, message: _FakeMessage | None = None) -> None:
        self.message = message or _FakeMessage()


class _FakeDispatcher:
    def __init__(self) -> None:
        self.handlers: list[tuple[str, callable]] = []
        self._polling_task: asyncio.Task | None = None

    def message_created(self, command=None):
        def decorator(handler):
            self.handlers.append((command, handler))
            return handler
        return decorator

    async def start_polling(self, bot) -> None:
        # Simulate polling loop
        while True:
            await asyncio.sleep(1)

    async def stop_polling(self) -> None:
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()


@pytest.mark.asyncio
async def test_start_initializes_bot_with_token(monkeypatch) -> None:
    """Test that MAX channel initializes bot with correct token."""
    config = MaxConfig(
        enabled=True,
        token="max_test_token_123",
        allow_from=["*"],
    )
    bus = MessageBus()
    channel = MaxChannel(config, bus)

    original_bot = None

    def mock_bot(token=None):
        nonlocal original_bot
        original_bot = _FakeBot(token=token)
        return original_bot

    monkeypatch.setattr("nanobot.channels.max.Bot", mock_bot)
    monkeypatch.setattr("nanobot.channels.max.Dispatcher", lambda: _FakeDispatcher())

    # Start and immediately stop for test
    import asyncio
    start_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.1)
    channel._running = False
    await channel.stop()

    assert original_bot is not None
    assert original_bot.token_value == config.token


@pytest.mark.asyncio
async def test_start_sets_up_handlers(monkeypatch) -> None:
    """Test that MAX channel sets up message handlers."""
    config = MaxConfig(
        enabled=True,
        token="max_test_token_123",
        allow_from=["*"],
    )
    bus = MessageBus()
    channel = MaxChannel(config, bus)

    dispatcher_instance = None

    def mock_dispatcher():
        nonlocal dispatcher_instance
        dispatcher_instance = _FakeDispatcher()
        return dispatcher_instance

    monkeypatch.setattr("nanobot.channels.max.Bot", lambda token=None: _FakeBot(token=token))
    monkeypatch.setattr("nanobot.channels.max.Dispatcher", mock_dispatcher)

    import asyncio
    start_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.1)
    channel._running = False
    await channel.stop()

    assert dispatcher_instance is not None
    # Should have handlers for start, new, stop, help, and general messages
    assert len(dispatcher_instance.handlers) >= 5


def test_get_sender_id_from_event() -> None:
    """Test extraction of sender ID from event."""
    config = MaxConfig(enabled=True, token="test", allow_from=["*"])
    channel = MaxChannel(config, MessageBus())

    event = _FakeEvent(_FakeMessage(from_user_id="user_456"))
    sender_id = MaxChannel._get_sender_id(event)

    assert sender_id == "user_456"


def test_get_chat_id_from_event() -> None:
    """Test extraction of chat ID from event."""
    config = MaxConfig(enabled=True, token="test", allow_from=["*"])
    channel = MaxChannel(config, MessageBus())

    event = _FakeEvent(_FakeMessage(chat_id="chat_789"))
    chat_id = MaxChannel._get_chat_id(event)

    assert chat_id == "chat_789"


def test_build_metadata_includes_user_info() -> None:
    """Test that metadata includes user information."""
    config = MaxConfig(enabled=True, token="test", allow_from=["*"])
    channel = MaxChannel(config, MessageBus())

    event = _FakeEvent(_FakeMessage(
        message_id="msg_999",
        from_user_id="user_123",
        chat_id="chat_456",
    ))
    metadata = MaxChannel._build_metadata(event)

    assert metadata["message_id"] == "msg_999"
    assert metadata["user_id"] == "user_123"
    assert metadata["chat_id"] == "chat_456"


def test_is_allowed_accepts_allowed_users() -> None:
    """Test that is_allowed accepts users from allow_from list."""
    channel = MaxChannel(MaxConfig(allow_from=["user_123", "user_456"]), MessageBus())

    assert channel.is_allowed("user_123") is True
    assert channel.is_allowed("user_456") is True
    assert channel.is_allowed("user_789") is False


def test_is_allowed_allows_all_with_star() -> None:
    """Test that is_allowed allows all users when allow_from contains '*'."""
    channel = MaxChannel(MaxConfig(allow_from=["*"]), MessageBus())

    assert channel.is_allowed("any_user") is True


def test_is_allowed_denies_all_with_empty_list() -> None:
    """Test that is_allowed denies all users when allow_from is empty."""
    channel = MaxChannel(MaxConfig(allow_from=[]), MessageBus())

    assert channel.is_allowed("any_user") is False


@pytest.mark.asyncio
async def test_send_text_message(monkeypatch) -> None:
    """Test sending a text message through MAX."""
    config = MaxConfig(enabled=True, token="test_token", allow_from=["*"])
    channel = MaxChannel(config, MessageBus())

    bot_instance = _FakeBot(token="test_token")
    channel.bot = bot_instance
    channel.dp = _FakeDispatcher()

    await channel.send(
        OutboundMessage(
            channel="max",
            chat_id="chat_123",
            content="Hello, MAX!",
            metadata={},
        )
    )

    assert len(bot_instance.sent_messages) == 1
    assert bot_instance.sent_messages[0]["chat_id"] == "chat_123"
    assert bot_instance.sent_messages[0]["text"] == "Hello, MAX!"


@pytest.mark.asyncio
async def test_send_splits_long_messages(monkeypatch) -> None:
    """Test that long messages are split into chunks."""
    config = MaxConfig(enabled=True, token="test_token", allow_from=["*"])
    channel = MaxChannel(config, MessageBus())

    bot_instance = _FakeBot(token="test_token")
    channel.bot = bot_instance
    channel.dp = _FakeDispatcher()

    # Create a message longer than MAX_MAX_MESSAGE_LEN
    long_content = "A" * 5000

    await channel.send(
        OutboundMessage(
            channel="max",
            chat_id="chat_123",
            content=long_content,
            metadata={},
        )
    )

    # Should be split into multiple messages
    assert len(bot_instance.sent_messages) >= 2


@pytest.mark.asyncio
async def test_send_handles_empty_content() -> None:
    """Test that empty content is not sent."""
    config = MaxConfig(enabled=True, token="test_token", allow_from=["*"])
    channel = MaxChannel(config, MessageBus())

    bot_instance = _FakeBot(token="test_token")
    channel.bot = bot_instance
    channel.dp = _FakeDispatcher()

    await channel.send(
        OutboundMessage(
            channel="max",
            chat_id="chat_123",
            content="[empty message]",
            metadata={},
        )
    )

    assert len(bot_instance.sent_messages) == 0


@pytest.mark.asyncio
async def test_send_warns_when_bot_not_running(monkeypatch, caplog) -> None:
    """Test that send warns when bot is not running."""
    config = MaxConfig(enabled=True, token="test_token", allow_from=["*"])
    channel = MaxChannel(config, MessageBus())

    # Don't initialize bot
    channel.bot = None
    channel.dp = None

    await channel.send(
        OutboundMessage(
            channel="max",
            chat_id="chat_123",
            content="Hello",
            metadata={},
        )
    )

    assert "MAX bot not running" in caplog.text


@pytest.mark.asyncio
async def test_start_without_token_logs_error(monkeypatch, caplog) -> None:
    """Test that start logs error when token is not configured."""
    config = MaxConfig(enabled=True, token="", allow_from=["*"])
    channel = MaxChannel(config, MessageBus())

    await channel.start()

    assert "MAX bot token not configured" in caplog.text


@pytest.mark.asyncio
async def test_stop_sets_running_to_false(monkeypatch) -> None:
    """Test that stop sets _running to False."""
    config = MaxConfig(enabled=True, token="test_token", allow_from=["*"])
    channel = MaxChannel(config, MessageBus())

    channel._running = True
    channel.bot = _FakeBot(token="test_token")
    channel.dp = _FakeDispatcher()

    await channel.stop()

    assert channel._running is False
    assert channel.bot is None
    assert channel.dp is None


@pytest.mark.asyncio
async def test_on_message_forwards_to_bus(monkeypatch) -> None:
    """Test that incoming messages are forwarded to the message bus."""
    config = MaxConfig(enabled=True, token="test_token", allow_from=["*"])
    bus = MessageBus()
    channel = MaxChannel(config, bus)

    received_messages = []

    async def mock_publish_inbound(msg):
        received_messages.append(msg)

    monkeypatch.setattr(bus, "publish_inbound", mock_publish_inbound)

    event = _FakeEvent(_FakeMessage(
        message_id="msg_123",
        text="Test message",
        from_user_id="user_123",
        chat_id="chat_456",
    ))

    await channel._on_message(event)

    assert len(received_messages) == 1
    assert received_messages[0].sender_id == "user_123"
    assert received_messages[0].chat_id == "chat_456"
    assert received_messages[0].content == "Test message"
    assert received_messages[0].channel == "max"


@pytest.mark.asyncio
async def test_on_message_denies_unauthorized_users(monkeypatch) -> None:
    """Test that unauthorized users are denied."""
    config = MaxConfig(enabled=True, token="test_token", allow_from=["allowed_user"])
    bus = MessageBus()
    channel = MaxChannel(config, bus)

    received_messages = []

    async def mock_publish_inbound(msg):
        received_messages.append(msg)

    monkeypatch.setattr(bus, "publish_inbound", mock_publish_inbound)

    event = _FakeEvent(_FakeMessage(
        message_id="msg_123",
        text="Test message",
        from_user_id="unauthorized_user",
        chat_id="chat_456",
    ))

    await channel._on_message(event)

    # Message should not be forwarded
    assert len(received_messages) == 0


@pytest.mark.asyncio
async def test_forward_command_forwards_to_bus(monkeypatch) -> None:
    """Test that slash commands are forwarded to the message bus."""
    config = MaxConfig(enabled=True, token="test_token", allow_from=["*"])
    bus = MessageBus()
    channel = MaxChannel(config, bus)

    received_messages = []

    async def mock_publish_inbound(msg):
        received_messages.append(msg)

    monkeypatch.setattr(bus, "publish_inbound", mock_publish_inbound)

    event = _FakeEvent(_FakeMessage(
        message_id="msg_123",
        text="/start",
        from_user_id="user_123",
        chat_id="chat_456",
    ))

    await channel._forward_command(event)

    assert len(received_messages) == 1
    assert received_messages[0].content == "/start"