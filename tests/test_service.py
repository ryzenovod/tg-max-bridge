from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_webhook_without_auto_register_skips_application_build(
    monkeypatch,
    settings_factory,
    tmp_path,
):
    from tg_max_bridge import service

    events: list[str] = []
    dispatcher_stopped = asyncio.Event()

    class FakeDb:
        async def close(self):
            events.append("db_closed")

    class FakeOutbox:
        async def recover_stale_sending(self, *, now_ms: int):
            return 0

    class FakeTransport:
        async def start(self):
            events.append("transport_started")

        async def stop(self):
            events.append("transport_stopped")

    class FakeDispatcher:
        def __init__(self, outbox, transport, settings):
            pass

        def stop(self):
            events.append("dispatcher_stopped")
            dispatcher_stopped.set()

        async def run_until_stopped(self):
            events.append("dispatcher_started")
            await dispatcher_stopped.wait()

    def fail_build_application(settings, outbox):
        raise AssertionError("build_application should not be called")

    async def fake_run_webhook(application, dispatcher, outbox, settings, stop):
        events.append("webhook_started")
        assert application is None
        stop.set()

    monkeypatch.setattr(
        service,
        "connect",
        lambda path: asyncio.sleep(0, result=FakeDb()),
    )
    monkeypatch.setattr(service, "init_schema", lambda db: asyncio.sleep(0))
    monkeypatch.setattr(
        service, "OutboxRepository", lambda *args, **kwargs: FakeOutbox()
    )
    monkeypatch.setattr(service, "MaxTransport", lambda settings: FakeTransport())
    monkeypatch.setattr(service, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(service, "build_application", fail_build_application)
    monkeypatch.setattr(service, "run_webhook", fake_run_webhook)

    await asyncio.wait_for(
        service.run_service(
            settings_factory(
                sqlite_path=tmp_path / "bridge.sqlite3",
                telegram_mode="webhook",
                telegram_webhook_url=(
                    "https://reserve-bridge.containerapps.ru/telegram/webhook"
                ),
                telegram_webhook_secret="secret-with-at-least-32-characters",
                telegram_webhook_auto_register=False,
                telegram_ack_mode="never",
            )
        ),
        timeout=0.2,
    )

    assert "webhook_started" in events
    assert "db_closed" in events


@pytest.mark.asyncio
async def test_webhook_mode_runs_dispatcher_loop_until_shutdown(
    monkeypatch,
    settings_factory,
    tmp_path,
):
    from tg_max_bridge import service

    events: list[str] = []
    dispatcher_started = asyncio.Event()
    dispatcher_stopped = asyncio.Event()

    class FakeDb:
        async def close(self):
            events.append("db_closed")

    class FakeOutbox:
        async def recover_stale_sending(self, *, now_ms: int):
            return 0

    class FakeTransport:
        async def start(self):
            events.append("transport_started")

        async def stop(self):
            events.append("transport_stopped")

    class FakeDispatcher:
        def __init__(self, outbox, transport, settings):
            pass

        def stop(self):
            events.append("dispatcher_stop_requested")
            dispatcher_stopped.set()

        async def run_until_stopped(self):
            events.append("dispatcher_started")
            dispatcher_started.set()
            await dispatcher_stopped.wait()
            events.append("dispatcher_finished")

    async def fake_run_webhook(application, dispatcher, outbox, settings, stop):
        events.append("webhook_started")
        await dispatcher_started.wait()
        stop.set()

    monkeypatch.setattr(
        service,
        "connect",
        lambda path: asyncio.sleep(0, result=FakeDb()),
    )
    monkeypatch.setattr(service, "init_schema", lambda db: asyncio.sleep(0))
    monkeypatch.setattr(
        service, "OutboxRepository", lambda *args, **kwargs: FakeOutbox()
    )
    monkeypatch.setattr(service, "MaxTransport", lambda settings: FakeTransport())
    monkeypatch.setattr(service, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(service, "build_application", lambda settings, outbox: None)
    monkeypatch.setattr(service, "run_webhook", fake_run_webhook)

    await asyncio.wait_for(
        service.run_service(
            settings_factory(
                sqlite_path=tmp_path / "bridge.sqlite3",
                telegram_mode="webhook",
                telegram_webhook_url=(
                    "https://reserve-bridge.containerapps.ru/telegram/webhook"
                ),
                telegram_webhook_secret="secret-with-at-least-32-characters",
                telegram_webhook_auto_register=False,
                telegram_ack_mode="never",
            )
        ),
        timeout=0.2,
    )

    assert "webhook_started" in events
    assert "dispatcher_started" in events
    assert "dispatcher_stop_requested" in events
    assert "db_closed" in events


@pytest.mark.asyncio
async def test_service_closes_database_when_initialization_fails(
    monkeypatch, settings_factory, tmp_path
):
    from tg_max_bridge import service

    closed = False

    class FakeDb:
        async def close(self):
            nonlocal closed
            closed = True

    async def fail_init(db):
        raise RuntimeError("schema failed")

    monkeypatch.setattr(
        service,
        "connect",
        lambda path: asyncio.sleep(0, result=FakeDb()),
    )
    monkeypatch.setattr(service, "init_schema", fail_init)

    with pytest.raises(RuntimeError, match="schema failed"):
        await service.run_service(
            settings_factory(sqlite_path=tmp_path / "bridge.sqlite3")
        )

    assert closed is True


@pytest.mark.asyncio
async def test_service_starts_polling_without_transport_probe(
    monkeypatch,
    settings_factory,
    tmp_path,
):
    from tg_max_bridge import service

    events: list[str] = []
    dispatcher_stopped = asyncio.Event()

    class FakeDb:
        async def close(self):
            events.append("db_closed")

    class FakeOutbox:
        async def recover_stale_sending(self, *, now_ms: int):
            return 0

    class FakeTransport:
        def __init__(self, settings):
            self.settings = settings

        async def start(self):
            events.append("transport_started")

        async def stop(self):
            events.append("transport_stopped")

    class FakeDispatcher:
        def __init__(self, outbox, transport, settings):
            self.outbox = outbox
            self.transport = transport
            self.settings = settings

        def stop(self):
            events.append("dispatcher_stopped")
            dispatcher_stopped.set()

        async def run_until_stopped(self):
            events.append("dispatcher_started")
            await dispatcher_stopped.wait()

    async def fake_run_polling(application, settings, stop):
        events.append("polling_started")
        stop.set()

    monkeypatch.setattr(
        service,
        "connect",
        lambda path: asyncio.sleep(0, result=FakeDb()),
    )
    monkeypatch.setattr(service, "init_schema", lambda db: asyncio.sleep(0))
    monkeypatch.setattr(
        service, "OutboxRepository", lambda *args, **kwargs: FakeOutbox()
    )
    monkeypatch.setattr(service, "MaxTransport", FakeTransport)
    monkeypatch.setattr(service, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(service, "build_application", lambda settings, outbox: object())
    monkeypatch.setattr(service, "_run_polling", fake_run_polling)

    await asyncio.wait_for(
        service.run_service(settings_factory(sqlite_path=tmp_path / "bridge.sqlite3")),
        timeout=0.2,
    )

    assert "transport_started" not in events
    assert "polling_started" in events
    assert "db_closed" in events


@pytest.mark.asyncio
async def test_service_stops_polling_and_closes_db_when_dispatcher_fails(
    monkeypatch,
    settings_factory,
    tmp_path,
):
    from tg_max_bridge import service

    events: list[str] = []

    class FakeDb:
        async def close(self):
            events.append("db_closed")

    class FakeOutbox:
        async def recover_stale_sending(self, *, now_ms: int):
            return 0

    class FakeDispatcher:
        def __init__(self, outbox, transport, settings):
            pass

        def stop(self):
            events.append("dispatcher_stopped")

        async def run_until_stopped(self):
            events.append("dispatcher_started")
            raise RuntimeError("dispatcher crashed")

    async def fake_run_polling(application, settings, stop):
        events.append("polling_started")
        try:
            await stop.wait()
        finally:
            events.append("polling_stopped")

    monkeypatch.setattr(
        service,
        "connect",
        lambda path: asyncio.sleep(0, result=FakeDb()),
    )
    monkeypatch.setattr(service, "init_schema", lambda db: asyncio.sleep(0))
    monkeypatch.setattr(
        service, "OutboxRepository", lambda *args, **kwargs: FakeOutbox()
    )
    monkeypatch.setattr(service, "build_application", lambda settings, outbox: object())
    monkeypatch.setattr(service, "_run_polling", fake_run_polling)
    monkeypatch.setattr(service, "Dispatcher", FakeDispatcher)

    with pytest.raises(RuntimeError, match="dispatcher crashed"):
        await asyncio.wait_for(
            service.run_service(
                settings_factory(sqlite_path=tmp_path / "bridge.sqlite3")
            ),
            timeout=0.2,
        )

    assert "polling_started" in events
    assert "polling_stopped" in events
    assert "db_closed" in events


def test_configure_logging_redacts_telegram_token_and_suppresses_httpx(
    capsys: pytest.CaptureFixture[str], settings_factory: Any
) -> None:
    from tg_max_bridge import service

    token = "999:fake-private-token"
    previous_factory = logging.getLogRecordFactory()
    previous_make_record = logging.Logger.makeRecord
    previous_httpx_level = logging.getLogger("httpx").level
    previous_httpcore_level = logging.getLogger("httpcore").level
    try:
        service.configure_logging(
            settings_factory(telegram_bot_token=token, log_level="INFO")
        )

        logging.getLogger("tg_max_bridge.service").info(
            "direct token in message: %s", token
        )
        try:
            raise RuntimeError(f"token in exception text: {token}")
        except RuntimeError:
            logging.getLogger("thirdparty").exception(
                "token in third-party URL: https://api.telegram.org/bot%s/getUpdates",
                token,
            )
        logging.getLogger("httpx").info(
            "GET https://api.telegram.org/bot%s/getMe", token
        )
    finally:
        logging.setLogRecordFactory(previous_factory)
        logging.Logger.makeRecord = previous_make_record
        logging.getLogger("httpx").setLevel(previous_httpx_level)
        logging.getLogger("httpcore").setLevel(previous_httpcore_level)

    captured = capsys.readouterr().err
    assert token not in captured
    assert "[REDACTED_TELEGRAM_TOKEN]" in captured
    assert "token in exception text: [REDACTED_TELEGRAM_TOKEN]" in captured
    assert "getMe" not in captured


def test_configure_logging_redacts_child_logger_with_own_handler(
    settings_factory: Any,
) -> None:
    from tg_max_bridge import service

    token = "999:fake-private-token"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
    child_logger = logging.getLogger("thirdparty.own_handler")
    previous_factory = logging.getLogRecordFactory()
    previous_make_record = logging.Logger.makeRecord
    previous_handlers = child_logger.handlers[:]
    previous_propagate = child_logger.propagate
    previous_level = child_logger.level
    try:
        service.configure_logging(
            settings_factory(telegram_bot_token=token, log_level="INFO")
        )
        child_logger.handlers = [handler]
        child_logger.propagate = False
        child_logger.setLevel(logging.INFO)

        try:
            raise RuntimeError(f"owned handler exception includes {token}")
        except RuntimeError:
            child_logger.exception("owned handler URL contains %s", token)
    finally:
        logging.setLogRecordFactory(previous_factory)
        logging.Logger.makeRecord = previous_make_record
        child_logger.handlers = previous_handlers
        child_logger.propagate = previous_propagate
        child_logger.setLevel(previous_level)

    output = stream.getvalue()
    assert token not in output
    assert "[REDACTED_TELEGRAM_TOKEN]" in output


def test_configure_logging_redacts_string_extra_fields(settings_factory: Any) -> None:
    from tg_max_bridge import service

    token = "999:fake-private-token"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(telegram_url)s"))
    child_logger = logging.getLogger("thirdparty.extra_handler")
    previous_factory = logging.getLogRecordFactory()
    previous_make_record = logging.Logger.makeRecord
    previous_handlers = child_logger.handlers[:]
    previous_propagate = child_logger.propagate
    previous_level = child_logger.level
    try:
        service.configure_logging(
            settings_factory(telegram_bot_token=token, log_level="INFO")
        )
        child_logger.handlers = [handler]
        child_logger.propagate = False
        child_logger.setLevel(logging.INFO)

        child_logger.info(
            "extra field redaction",
            extra={
                "telegram_url": f"https://api.telegram.org/bot{token}/getUpdates",
            },
        )
    finally:
        logging.setLogRecordFactory(previous_factory)
        logging.Logger.makeRecord = previous_make_record
        child_logger.handlers = previous_handlers
        child_logger.propagate = previous_propagate
        child_logger.setLevel(previous_level)

    output = stream.getvalue()
    assert token not in output
    assert "bot[REDACTED_TELEGRAM_TOKEN]/getUpdates" in output


def test_configure_logging_sanitizes_nested_and_object_extra_values(
    settings_factory: Any,
) -> None:
    from httpx import URL
    from telegram import Bot

    from tg_max_bridge import service

    class TokenObject:
        def __str__(self) -> str:
            return f"custom object contains {token}"

    class BrokenStr:
        def __str__(self) -> str:
            raise RuntimeError("cannot stringify")

    class CaptureHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    token = "999:fake-private-token"
    payload = {
        f"key-{token}": [
            URL(f"https://api.telegram.org/bot{token}/getUpdates"),
            {"nested": (f"value-{token}", TokenObject())},
        ],
        "bot": Bot(token=token),
        "broken": BrokenStr(),
    }
    handler = CaptureHandler()
    structured_logger = logging.getLogger("thirdparty.structured")
    previous_factory = logging.getLogRecordFactory()
    previous_make_record = logging.Logger.makeRecord
    previous_handlers = structured_logger.handlers[:]
    previous_propagate = structured_logger.propagate
    previous_level = structured_logger.level
    try:
        service.configure_logging(
            settings_factory(telegram_bot_token=token, log_level="INFO")
        )
        structured_logger.handlers = [handler]
        structured_logger.propagate = False
        structured_logger.setLevel(logging.INFO)

        structured_logger.info(
            "structured extra",
            extra={
                "telegram_url": URL(f"https://api.telegram.org/bot{token}/getUpdates"),
                "payload": payload,
            },
        )
    finally:
        logging.setLogRecordFactory(previous_factory)
        logging.Logger.makeRecord = previous_make_record
        structured_logger.handlers = previous_handlers
        structured_logger.propagate = previous_propagate
        structured_logger.setLevel(previous_level)

    assert f"key-{token}" in payload
    assert isinstance(payload["bot"], Bot)
    record = handler.records[0]
    assert token not in repr(record.__dict__)
    assert isinstance(record.payload, dict)
    assert isinstance(record.payload["bot"], str)
    assert record.payload["broken"] == "<unprintable BrokenStr>"
    assert "bot[REDACTED_TELEGRAM_TOKEN]/getUpdates" in record.telegram_url


def test_configure_logging_reconfiguration_keeps_prior_tokens(
    settings_factory: Any,
) -> None:
    from tg_max_bridge import service

    first_token = "111:first-fake-token"
    second_token = "222:second-fake-token"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s %(telegram_url)s"))
    target_logger = logging.getLogger("thirdparty.reconfigured")
    previous_factory = logging.getLogRecordFactory()
    previous_make_record = logging.Logger.makeRecord
    previous_handlers = target_logger.handlers[:]
    previous_propagate = target_logger.propagate
    previous_level = target_logger.level
    try:
        service.configure_logging(
            settings_factory(telegram_bot_token=first_token, log_level="INFO")
        )
        service.configure_logging(
            settings_factory(telegram_bot_token=second_token, log_level="INFO")
        )
        target_logger.handlers = [handler]
        target_logger.propagate = False
        target_logger.setLevel(logging.INFO)

        target_logger.info(
            "old=%s new=%s",
            first_token,
            second_token,
            extra={"telegram_url": f"{first_token}/{second_token}"},
        )
    finally:
        logging.setLogRecordFactory(previous_factory)
        logging.Logger.makeRecord = previous_make_record
        target_logger.handlers = previous_handlers
        target_logger.propagate = previous_propagate
        target_logger.setLevel(previous_level)

    output = stream.getvalue()
    assert first_token not in output
    assert second_token not in output
    assert output.count("[REDACTED_TELEGRAM_TOKEN]") == 4


def test_configure_logging_survives_malformed_message_args(
    settings_factory: Any,
) -> None:
    from tg_max_bridge import service

    token = "999:fake-private-token"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    target_logger = logging.getLogger("thirdparty.malformed")
    previous_factory = logging.getLogRecordFactory()
    previous_make_record = logging.Logger.makeRecord
    previous_handlers = target_logger.handlers[:]
    previous_propagate = target_logger.propagate
    previous_level = target_logger.level
    try:
        service.configure_logging(
            settings_factory(telegram_bot_token=token, log_level="INFO")
        )
        target_logger.handlers = [handler]
        target_logger.propagate = False
        target_logger.setLevel(logging.INFO)

        target_logger.info("malformed %s %s", token)
    finally:
        logging.setLogRecordFactory(previous_factory)
        logging.Logger.makeRecord = previous_make_record
        target_logger.handlers = previous_handlers
        target_logger.propagate = previous_propagate
        target_logger.setLevel(previous_level)

    output = stream.getvalue()
    assert token not in output
    assert "[REDACTED_TELEGRAM_TOKEN]" in output
