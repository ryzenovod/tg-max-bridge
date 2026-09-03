from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest


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

    async def process_once(self) -> int:
        self.calls += 1
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            return 1 if self.statuses else 0
        finally:
            self.running -= 1


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


def _settings(settings_factory):
    from pydantic import SecretStr

    return settings_factory(
        telegram_mode="webhook",
        telegram_webhook_url="https://reserve-bridge.containerapps.ru/telegram/hook",
        telegram_webhook_secret=SecretStr("valid_secret"),
        telegram_ack_mode="never",
    )


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
async def test_webhook_accepted_update_returns_2xx_only_after_sent(settings_factory):
    webhook, dispatcher = _webhook(settings_factory, statuses=["sent"])

    response = await webhook.handle_update(FakeRequest(body=_accepted_update()))

    assert 200 <= response.status < 300
    assert dispatcher.calls == 1


@pytest.mark.parametrize("status", ["pending", "ambiguous"])
@pytest.mark.asyncio
async def test_webhook_retryable_outbox_state_returns_503(settings_factory, status):
    webhook, dispatcher = _webhook(settings_factory, statuses=[status])

    response = await webhook.handle_update(FakeRequest(body=_accepted_update()))

    assert response.status == 503
    assert dispatcher.calls == 1


@pytest.mark.asyncio
async def test_webhook_irrelevant_update_returns_2xx_without_dispatch(settings_factory):
    webhook, dispatcher = _webhook(settings_factory, statuses=[])

    response = await webhook.handle_update(FakeRequest(body=_irrelevant_update()))

    assert 200 <= response.status < 300
    assert dispatcher.calls == 0


@pytest.mark.asyncio
async def test_webhook_serializes_dispatcher_runs(settings_factory):
    webhook, dispatcher = _webhook(settings_factory, statuses=["sent"])

    responses = await asyncio.gather(
        webhook.handle_update(FakeRequest(body=_accepted_update())),
        webhook.handle_update(FakeRequest(body=_accepted_update())),
    )

    assert [response.status for response in responses] == [200, 200]
    assert dispatcher.calls == 2
    assert dispatcher.max_running == 1


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
