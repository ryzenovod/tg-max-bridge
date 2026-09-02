from __future__ import annotations

import asyncio

import pytest


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
