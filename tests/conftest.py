from __future__ import annotations

import inspect
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 2, 10, 30, tzinfo=UTC)


@pytest.fixture
def now_ms() -> int:
    return 1_788_342_600_000


@pytest.fixture
def settings_factory(tmp_path):
    def build(**overrides: Any) -> SimpleNamespace:
        values = {
            "telegram_bot_token": "123:test-token",
            "telegram_allowed_chat_ids": {-100111222333},
            "telegram_allowed_user_ids": {42},
            "telegram_bot_username": "reservebridgebot",
            "max_chat_id": 777000,
            "max_mcp_directory": Path("/opt/max-mcp"),
            "max_mcp_command": "uv",
            "sqlite_path": tmp_path / "bridge.sqlite3",
            "poll_timeout_seconds": 30,
            "delivery_tick_seconds": 0.01,
            "delivery_timeout_seconds": 0.05,
            "retry_base_seconds": 5.0,
            "retry_max_seconds": 300.0,
            "ambiguous_reconcile_delay_seconds": 30.0,
            "ambiguous_resend_after_seconds": 600.0,
            "max_reconcile_scan_limit": 500,
            "telegram_ack_mode": "errors",
            "log_level": "INFO",
        }
        values.update(overrides)
        settings = SimpleNamespace(**values)
        settings.max_mcp_args = lambda: [
            "run",
            "--no-dev",
            "--frozen",
            "--directory",
            str(settings.max_mcp_directory),
            "max-mcp",
        ]
        return settings

    return build


@pytest.fixture
def sample_source(now):
    from tg_max_bridge.models import TelegramSourceMessage

    return TelegramSourceMessage(
        chat_id=-100111222333,
        message_id=123,
        trigger_message_id=456,
        from_user_id=42,
        from_display_name="Alice",
        text="Important meetup point changed to entrance B.",
        date=now,
    )


@pytest.fixture
def sample_payload(sample_source):
    from tg_max_bridge.formatting import build_max_payload

    return build_max_payload(sample_source, max_chat_id=777000)


class FakeMessage:
    def __init__(
        self,
        *,
        text: str | None = None,
        caption: str | None = None,
        chat_id: int = -100111222333,
        chat_type: str = "supergroup",
        from_user_id: int = 42,
        from_user_name: str = "Alice",
        message_id: int = 456,
        reply_to_message: FakeMessage | None = None,
    ) -> None:
        self.text = text
        self.caption = caption
        self.message_id = message_id
        self.chat_id = chat_id
        self.date = datetime(2026, 9, 2, 10, 30, tzinfo=UTC)
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.from_user = SimpleNamespace(
            id=from_user_id,
            full_name=from_user_name,
            username=from_user_name.lower(),
        )
        self.reply_to_message = reply_to_message


def fake_update(message: FakeMessage) -> SimpleNamespace:
    return SimpleNamespace(message=message, effective_message=message)


@pytest.fixture
def make_update():
    def build(
        command: str,
        *,
        source_text: str | None = "Source text",
        source_caption: str | None = None,
        chat_id: int = -100111222333,
        chat_type: str = "supergroup",
        from_user_id: int = 42,
        reply: bool = True,
    ) -> SimpleNamespace:
        source = None
        if reply:
            source = FakeMessage(
                text=source_text,
                caption=source_caption,
                chat_id=chat_id,
                chat_type=chat_type,
                from_user_id=from_user_id,
                message_id=123,
            )
        trigger = FakeMessage(
            text=command,
            chat_id=chat_id,
            chat_type=chat_type,
            from_user_id=from_user_id,
            message_id=456,
            reply_to_message=source,
        )
        return fake_update(trigger)

    return build


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def build_outbox_repository(tmp_path: Path):
    from tg_max_bridge import db as db_module
    from tg_max_bridge.outbox import OutboxRepository

    sqlite_path = tmp_path / "bridge.sqlite3"
    if hasattr(db_module, "connect"):
        conn = await db_module.connect(sqlite_path)
    else:
        import aiosqlite

        conn = await aiosqlite.connect(sqlite_path)
    initializer = (
        getattr(db_module, "init_schema", None)
        or getattr(db_module, "init_db", None)
        or getattr(db_module, "initialize_db", None)
    )
    if initializer is None:
        raise AssertionError(
            "db.py must expose init_schema(conn), init_db(conn), or initialize_db(conn)"
        )
    await maybe_await(initializer(conn))

    repo = OutboxRepository(
        conn,
        retry_base_seconds=5.0,
        retry_max_seconds=300.0,
        ambiguous_reconcile_delay_seconds=30.0,
    )
    return repo, conn


@dataclass
class FakeTransport:
    send_result: Any = None
    send_error: BaseException | None = None
    marker_result: Any = None
    marker_error: BaseException | None = None
    sent_texts: list[tuple[int, str]] | None = None

    async def send_text(self, chat_id: int, text: str):
        if self.sent_texts is None:
            self.sent_texts = []
        self.sent_texts.append((chat_id, text))
        if self.send_error is not None:
            raise self.send_error
        return self.send_result

    async def find_marker(self, chat_id: int, marker: str, *, scan_limit: int):
        if self.marker_error is not None:
            raise self.marker_error
        return self.marker_result


class FakeCallToolResult:
    def __init__(
        self,
        *,
        structured: dict[str, Any] | None = None,
        text_json: dict[str, Any] | None = None,
        is_error: bool = False,
    ) -> None:
        self.isError = is_error
        self.structuredContent = structured
        self.content = []
        if text_json is not None:
            from mcp.types import TextContent

            self.content.append(TextContent(type="text", text=json.dumps(text_json)))


class FakeMcpSession:
    def __init__(self, results: dict[str, FakeCallToolResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True

    async def call_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any):
        self.calls.append((name, arguments))
        result = self.results.get(name)
        if result is None:
            raise AssertionError(f"unexpected MCP tool call: {name}")
        return result


class AsyncContext:
    def __init__(self, value: Any) -> None:
        self.value = value
        self.exited = False

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = True


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    keys = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_ALLOWED_USER_IDS",
        "TELEGRAM_BOT_USERNAME",
        "MAX_CHAT_ID",
        "MAX_MCP_DIRECTORY",
        "MAX_MCP_COMMAND",
        "SQLITE_PATH",
        "TELEGRAM_ACK_MODE",
        "TELEGRAM_MODE",
        "TELEGRAM_WEBHOOK_URL",
        "TELEGRAM_WEBHOOK_SECRET",
        "PORT",
        "MAX_MCP_SESSION_TARB64",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-100111222333, -100444555666")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42,99")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "ReserveBridgeBot")
    monkeypatch.setenv("MAX_CHAT_ID", "777000")
    monkeypatch.setenv("MAX_MCP_DIRECTORY", "/opt/max-mcp")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "bridge.sqlite3"))
