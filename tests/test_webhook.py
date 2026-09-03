from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import ClientSession


@dataclass
class FakeResponse:
    status: int
    text: str = ""


class FakeRequest:
    def __init__(
        self,
        *,
        body: bytes,
        secret: str | None = "valid_secret",
        max_body_size: int = 1024,
    ) -> None:
        self.headers: dict[str, str] = {}
        if secret is not None:
            self.headers["X-Telegram-Bot-Api-Secret-Token"] = secret
        self._body = body
        self.content_length = len(body)
        self.app = {"max_body_size": max_body_size}

    async def read(self) -> bytes:
        return self._body


class FakeTelegramApplication:
    def __init__(self) -> None:
        self.updates: list[Any] = []
        self.started = False
        self.stopped = False
        self.bot = SimpleNamespace(
            set_webhook=self._set_webhook,
        )
        self.webhooks: list[dict[str, Any]] = []

    async def initialize(self) -> None:
        pass

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def shutdown(self) -> None:
        pass

    async def process_update(self, update: Any) -> None:
        self.updates.append(update)

    async def _set_webhook(self, **kwargs: Any) -> None:
        self.webhooks.append(kwargs)


class FakeDispatcher:
    def __init__(self, *, statuses: list[str]) -> None:
        self.statuses = statuses
        self.calls = 0
        self.running = 0
        self.max_running = 0
        self.close_transport_flags: list[bool] = []

    async def process_once(self, *, close_transport: bool = False) -> int:
        self.calls += 1
        self.close_transport_flags.append(close_transport)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            return 1 if self.statuses else 0
        finally:
            self.running -= 1


class BlockingDispatcher:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def process_once(self, *, close_transport: bool = False) -> int:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return 1


class FakeOutbox:
    def __init__(self, *, statuses: list[str]) -> None:
        self.statuses = statuses

    async def get_by_source(
        self,
        tg_chat_id: int,
        tg_message_id: int,
        max_chat_id: int,
    ):
        if not self.statuses:
            return None
        return SimpleNamespace(status=self.statuses[-1])


class RecordingOutbox:
    def __init__(self, *, status: str) -> None:
        self.status = status
        self.enqueued: list[tuple[Any, Any]] = []
        self.seen: set[tuple[int, int, int]] = set()

    async def enqueue(self, source: Any, payload: Any):
        key = (source.chat_id, source.message_id, payload.chat_id)
        created = key not in self.seen
        self.seen.add(key)
        self.enqueued.append((source, payload))
        return SimpleNamespace(
            record=SimpleNamespace(status=self.status),
            created=created,
        )

    async def get_by_source(
        self,
        tg_chat_id: int,
        tg_message_id: int,
        max_chat_id: int,
    ):
        return SimpleNamespace(status=self.status)


def _accepted_update() -> bytes:
    return json.dumps(
        {
            "update_id": 1000,
            "message": {
                "message_id": 456,
                "date": 1_788_342_600,
                "chat": {"id": -100111222333, "type": "supergroup"},
                "from": {
                    "id": 42,
                    "is_bot": False,
                    "first_name": "Alice",
                },
                "text": "/max",
                "entities": [{"type": "bot_command", "offset": 0, "length": 4}],
                "reply_to_message": {
                    "message_id": 123,
                    "date": 1_788_342_500,
                    "chat": {"id": -100111222333, "type": "supergroup"},
                    "from": {
                        "id": 42,
                        "is_bot": False,
                        "first_name": "Alice",
                    },
                    "text": "Forward this to MAX",
                },
            },
        }
    ).encode()


def _irrelevant_update() -> bytes:
    return json.dumps(
        {
            "update_id": 1001,
            "message": {
                "message_id": 999,
                "date": 1_788_342_600,
                "chat": {"id": -100111222333, "type": "supergroup"},
                "from": {
                    "id": 42,
                    "is_bot": False,
                    "first_name": "Alice",
                },
                "text": "ordinary message",
            },
        }
    ).encode()


def _inline_command_update() -> bytes:
    return json.dumps(
        {
            "update_id": 1003,
            "message": {
                "message_id": 457,
                "date": 1_788_342_600,
                "chat": {"id": -100111222333, "type": "supergroup"},
                "from": {
                    "id": 42,
                    "is_bot": False,
                    "first_name": "Alice",
                },
                "text": "/max Meet at entrance B",
                "entities": [{"type": "bot_command", "offset": 0, "length": 4}],
            },
        }
    ).encode()


def _marker_update() -> bytes:
    return json.dumps(
        {
            "update_id": 1002,
            "message": {
                "message_id": 790,
                "date": 1_788_342_600,
                "chat": {"id": -100111222333, "type": "supergroup"},
                "from": {
                    "id": 777,
                    "is_bot": False,
                    "first_name": "Class Rep",
                },
                "text": "Meet at entrance B #max",
            },
        }
    ).encode()


def _settings(settings_factory, **overrides: Any):
    from pydantic import SecretStr

    values = dict(
        telegram_mode="webhook",
        telegram_webhook_url="https://reserve-bridge.containerapps.ru/telegram/hook",
        telegram_webhook_secret=SecretStr("valid_secret"),
        telegram_ack_mode="never",
    )
    values.update(overrides)
    return settings_factory(**values)


def _webhook(settings_factory, *, statuses: list[str]):
    from tg_max_bridge.webhook import TelegramWebhook

    dispatcher = FakeDispatcher(statuses=statuses)
    webhook = TelegramWebhook(
        settings=_settings(settings_factory),
        application=FakeTelegramApplication(),
        dispatcher=dispatcher,
        outbox=FakeOutbox(statuses=statuses),
        max_body_size=1024,
    )
    return webhook, dispatcher


@pytest.mark.asyncio
async def test_webhook_rejects_missing_and_wrong_secret_without_leaking_it(
    caplog, settings_factory
):
    webhook, _dispatcher = _webhook(settings_factory, statuses=["sent"])

    missing = await webhook.handle_update(
        FakeRequest(body=_accepted_update(), secret=None)
    )
    wrong = await webhook.handle_update(
        FakeRequest(body=_accepted_update(), secret="wrong_secret")
    )

    assert missing.status == 403
    assert wrong.status == 403
    assert "wrong_secret" not in caplog.text
    assert "valid_secret" not in caplog.text
    assert "Forward this to MAX" not in caplog.text


@pytest.mark.asyncio
async def test_webhook_rejects_malformed_json(settings_factory):
    webhook, dispatcher = _webhook(settings_factory, statuses=["sent"])

    response = await webhook.handle_update(FakeRequest(body=b"{not-json"))

    assert response.status == 400
    assert dispatcher.calls == 0


@pytest.mark.asyncio
async def test_webhook_rejects_oversized_body(settings_factory):
    webhook, dispatcher = _webhook(settings_factory, statuses=["sent"])

    response = await webhook.handle_update(
        FakeRequest(body=b"x" * 1025, max_body_size=1024)
    )

    assert response.status == 413
    assert dispatcher.calls == 0


@pytest.mark.asyncio
async def test_webhook_accepted_update_returns_fast_retryable_without_dispatch_wait(
    settings_factory,
):
    from tg_max_bridge.webhook import TelegramWebhook

    dispatcher = BlockingDispatcher()
    outbox = RecordingOutbox(status="pending")
    webhook = TelegramWebhook(
        settings=_settings(
            settings_factory,
            telegram_webhook_auto_register=False,
            telegram_ack_mode="never",
        ),
        application=None,
        dispatcher=dispatcher,
        outbox=outbox,
        max_body_size=1024,
    )

    response = await asyncio.wait_for(
        webhook.handle_update(FakeRequest(body=_accepted_update())),
        timeout=0.05,
    )

    assert response.status == 503
    assert dispatcher.calls == 0
    assert len(outbox.enqueued) == 1


@pytest.mark.asyncio
async def test_webhook_already_sent_update_returns_2xx(settings_factory):
    webhook, dispatcher = _webhook(settings_factory, statuses=["sent"])

    response = await webhook.handle_update(FakeRequest(body=_accepted_update()))

    assert 200 <= response.status < 300
    assert dispatcher.calls == 0
    assert dispatcher.close_transport_flags == []


@pytest.mark.parametrize("status", ["pending", "ambiguous"])
@pytest.mark.asyncio
async def test_webhook_retryable_outbox_state_returns_fast_503_without_dispatch(
    settings_factory, status
):
    webhook, dispatcher = _webhook(settings_factory, statuses=[status])

    response = await webhook.handle_update(FakeRequest(body=_accepted_update()))

    assert response.status == 503
    assert dispatcher.calls == 0


@pytest.mark.asyncio
async def test_webhook_irrelevant_update_returns_2xx_without_dispatch(settings_factory):
    webhook, dispatcher = _webhook(settings_factory, statuses=[])

    response = await webhook.handle_update(FakeRequest(body=_irrelevant_update()))

    assert 200 <= response.status < 300
    assert dispatcher.calls == 0


@pytest.mark.asyncio
async def test_concurrent_webhooks_do_not_dispatch_in_request_path(settings_factory):
    webhook, dispatcher = _webhook(settings_factory, statuses=["sent"])

    responses = await asyncio.gather(
        webhook.handle_update(FakeRequest(body=_accepted_update())),
        webhook.handle_update(FakeRequest(body=_accepted_update())),
    )

    assert [response.status for response in responses] == [200, 200]
    assert dispatcher.calls == 0
    assert dispatcher.max_running == 0


@pytest.mark.asyncio
async def test_webhook_startup_sets_secret_webhook_and_cleanup_stops_app(
    settings_factory,
):
    webhook, _dispatcher = _webhook(settings_factory, statuses=[])

    await webhook.start_application()
    await webhook.register_webhook()
    await webhook.stop()

    assert webhook.application.started is True
    assert webhook.application.stopped is True
    assert webhook.application.webhooks == [
        {
            "url": "https://reserve-bridge.containerapps.ru/telegram/hook",
            "secret_token": "valid_secret",
            "allowed_updates": ["message"],
            "drop_pending_updates": False,
        }
    ]


@pytest.mark.parametrize(
    ("status", "expected_status"),
    [
        ("sent", 200),
        ("pending", 503),
    ],
)
@pytest.mark.asyncio
async def test_webhook_direct_receiver_enqueues_without_telegram_application(
    settings_factory,
    status: str,
    expected_status: int,
):
    from tg_max_bridge.webhook import TelegramWebhook

    dispatcher = FakeDispatcher(statuses=[status])
    outbox = RecordingOutbox(status=status)
    webhook = TelegramWebhook(
        settings=_settings(
            settings_factory,
            telegram_webhook_auto_register=False,
            telegram_ack_mode="never",
        ),
        application=None,
        dispatcher=dispatcher,
        outbox=outbox,
        max_body_size=1024,
    )

    response = await webhook.handle_update(FakeRequest(body=_accepted_update()))

    assert response.status == expected_status
    assert dispatcher.calls == 0
    assert len(outbox.enqueued) == 1
    source, payload = outbox.enqueued[0]
    assert source.chat_id == -100111222333
    assert source.message_id == 123
    assert payload.chat_id == 777000


@pytest.mark.asyncio
async def test_webhook_direct_receiver_accepts_inline_command_without_application(
    settings_factory,
):
    from tg_max_bridge.webhook import TelegramWebhook

    dispatcher = FakeDispatcher(statuses=["sent"])
    outbox = RecordingOutbox(status="sent")
    webhook = TelegramWebhook(
        settings=_settings(
            settings_factory,
            telegram_webhook_auto_register=False,
            telegram_ack_mode="never",
        ),
        application=None,
        dispatcher=dispatcher,
        outbox=outbox,
        max_body_size=1024,
    )

    response = await webhook.handle_update(FakeRequest(body=_inline_command_update()))

    assert response.status == 200
    assert dispatcher.calls == 0
    source, payload = outbox.enqueued[0]
    assert source.message_id == 457
    assert source.trigger_message_id == 457
    assert source.text == "Meet at entrance B"
    assert "Meet at entrance B" in payload.text


@pytest.mark.asyncio
async def test_webhook_direct_receiver_accepts_marker_without_application(
    settings_factory,
):
    from tg_max_bridge.webhook import TelegramWebhook

    dispatcher = FakeDispatcher(statuses=["sent"])
    outbox = RecordingOutbox(status="sent")
    webhook = TelegramWebhook(
        settings=_settings(
            settings_factory,
            telegram_webhook_auto_register=False,
            telegram_ack_mode="never",
        ),
        application=None,
        dispatcher=dispatcher,
        outbox=outbox,
        max_body_size=1024,
    )

    response = await webhook.handle_update(FakeRequest(body=_marker_update()))

    assert response.status == 200
    assert dispatcher.calls == 0
    source, payload = outbox.enqueued[0]
    assert source.message_id == 790
    assert source.text == "Meet at entrance B"
    assert "Meet at entrance B" in payload.text
    assert "#max" not in payload.text


@pytest.mark.asyncio
async def test_webhook_direct_receiver_dedupes_through_outbox(settings_factory):
    from tg_max_bridge.webhook import TelegramWebhook

    dispatcher = FakeDispatcher(statuses=["sent"])
    outbox = RecordingOutbox(status="sent")
    webhook = TelegramWebhook(
        settings=_settings(
            settings_factory,
            telegram_webhook_auto_register=False,
            telegram_ack_mode="never",
        ),
        application=None,
        dispatcher=dispatcher,
        outbox=outbox,
        max_body_size=1024,
    )

    first = await webhook.handle_update(FakeRequest(body=_accepted_update()))
    second = await webhook.handle_update(FakeRequest(body=_accepted_update()))

    assert [first.status, second.status] == [200, 200]
    assert [item[0].message_id for item in outbox.enqueued] == [123, 123]
    assert len(outbox.seen) == 1
    assert dispatcher.calls == 0


@pytest.mark.asyncio
async def test_webhook_repeated_delivery_is_deduped_in_persistent_outbox(
    settings_factory,
    tmp_path,
):
    from conftest import build_outbox_repository

    from tg_max_bridge.webhook import TelegramWebhook

    outbox, conn = await build_outbox_repository(tmp_path)
    dispatcher = FakeDispatcher(statuses=["pending"])
    webhook = TelegramWebhook(
        settings=_settings(
            settings_factory,
            telegram_webhook_auto_register=False,
            telegram_ack_mode="never",
        ),
        application=None,
        dispatcher=dispatcher,
        outbox=outbox,
        max_body_size=1024,
    )
    try:
        first = await webhook.handle_update(FakeRequest(body=_accepted_update()))
        second = await webhook.handle_update(FakeRequest(body=_accepted_update()))

        rows = await conn.execute_fetchall(
            """
            SELECT tg_chat_id, tg_message_id, tg_trigger_message_id, status
            FROM outbox
            """
        )

        assert [first.status, second.status] == [503, 503]
        assert len(rows) == 1
        assert rows[0]["tg_chat_id"] == -100111222333
        assert rows[0]["tg_message_id"] == 123
        assert rows[0]["tg_trigger_message_id"] == 456
        assert rows[0]["status"] == "pending"
        assert dispatcher.calls == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_run_webhook_can_serve_health_without_telegram_startup(
    settings_factory,
):
    from pydantic import SecretStr

    from tg_max_bridge.webhook import run_webhook

    class ExplodingTelegramApplication(FakeTelegramApplication):
        async def initialize(self) -> None:
            raise AssertionError("initialize should not be called")

        async def _set_webhook(self, **kwargs: Any) -> None:
            raise AssertionError("set_webhook should not be called")

    port = _unused_port()
    settings = settings_factory(
        telegram_mode="webhook",
        telegram_webhook_url="https://reserve-bridge.containerapps.ru/telegram/hook",
        telegram_webhook_secret=SecretStr("valid_secret"),
        telegram_webhook_auto_register=False,
        telegram_ack_mode="never",
        telegram_webhook_listen_host="127.0.0.1",
        port=port,
    )
    stop = asyncio.Event()
    application = ExplodingTelegramApplication()
    task = asyncio.create_task(
        run_webhook(
            application,
            FakeDispatcher(statuses=["sent"]),
            RecordingOutbox(status="sent"),
            settings,
            stop,
        )
    )

    try:
        await _wait_for_healthz(port)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    assert application.started is False
    assert application.webhooks == []


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_healthz(port: int) -> None:
    async with ClientSession() as session:
        for _ in range(50):
            try:
                async with session.get(f"http://127.0.0.1:{port}/healthz") as response:
                    if response.status == 200 and await response.text() == "ok\n":
                        return
            except OSError:
                await asyncio.sleep(0.01)
    raise AssertionError("healthz did not become available")
