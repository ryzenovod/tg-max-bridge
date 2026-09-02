from __future__ import annotations

from types import SimpleNamespace

import pytest


class FakeRepo:
    def __init__(self, rows):
        self.rows = rows
        self.sent = []
        self.retried = []
        self.ambiguous = []
        self.released = []

    async def lease_due(self, *, now_ms: int, limit: int):
        return self.rows

    async def mark_sent(self, outbox_id, result, *, now_ms: int):
        self.sent.append((outbox_id, result, now_ms))

    async def mark_retry(self, outbox_id, error: str, *, now_ms: int):
        self.retried.append((outbox_id, error, now_ms))

    async def mark_ambiguous(self, outbox_id, error: str, *, now_ms: int):
        self.ambiguous.append((outbox_id, error, now_ms))

    async def mark_reconciliation_error(self, outbox_id, error: str, *, now_ms: int):
        self.retried.append((outbox_id, error, now_ms))

    async def release_after_reconciliation_miss(self, outbox_id, *, now_ms: int):
        self.released.append((outbox_id, now_ms))


def row(sample_payload, *, status="pending", next_attempt_at=0, updated_at=0):
    from tg_max_bridge.models import OutboxStatus

    if isinstance(status, str):
        status = OutboxStatus(status)
    return SimpleNamespace(
        id=1,
        max_chat_id=sample_payload.chat_id,
        max_text=sample_payload.text,
        marker=sample_payload.marker,
        status=status,
        next_attempt_at=next_attempt_at,
        updated_at=updated_at,
    )


def build_dispatcher(repo, transport, settings):
    from tg_max_bridge.dispatcher import Dispatcher

    return Dispatcher(outbox=repo, transport=transport, settings=settings)


@pytest.mark.asyncio
async def test_process_once_success_marks_sent(
    sample_payload, settings_factory, now_ms
):
    from conftest import FakeTransport

    from tg_max_bridge.models import MaxSendResult

    repo = FakeRepo([row(sample_payload)])
    transport = FakeTransport(
        send_result=MaxSendResult(message_id=999, raw={"message_id": 999})
    )
    dispatcher = build_dispatcher(repo, transport, settings_factory())

    processed = await dispatcher.process_once(now_ms=now_ms)

    assert processed == 1
    assert repo.sent[0][0] == 1
    assert repo.sent[0][1].message_id == 999
    assert transport.sent_texts == [(sample_payload.chat_id, sample_payload.text)]


@pytest.mark.asyncio
async def test_process_once_transient_error_marks_retry(
    sample_payload, settings_factory, now_ms
):
    from conftest import FakeTransport

    repo = FakeRepo([row(sample_payload)])
    dispatcher = build_dispatcher(
        repo,
        FakeTransport(send_error=RuntimeError("temporary MCP failure")),
        settings_factory(),
    )

    processed = await dispatcher.process_once(now_ms=now_ms)

    assert processed == 1
    assert repo.retried == [(1, "temporary MCP failure", now_ms)]
    assert repo.sent == []


@pytest.mark.asyncio
async def test_process_once_transport_unavailable_marks_retry(
    sample_payload, settings_factory, now_ms
):
    from conftest import FakeTransport

    from tg_max_bridge.max_transport import MaxTransportUnavailableError

    repo = FakeRepo([row(sample_payload)])
    dispatcher = build_dispatcher(
        repo,
        FakeTransport(send_error=MaxTransportUnavailableError("MAX unavailable")),
        settings_factory(),
    )

    await dispatcher.process_once(now_ms=now_ms)

    assert repo.retried == [(1, "MAX unavailable", now_ms)]
    assert repo.ambiguous == []


@pytest.mark.asyncio
async def test_process_once_uncertain_delivery_marks_ambiguous(
    sample_payload, settings_factory, now_ms
):
    from conftest import FakeTransport

    from tg_max_bridge.max_transport import MaxDeliveryUncertainError

    repo = FakeRepo([row(sample_payload)])
    dispatcher = build_dispatcher(
        repo,
        FakeTransport(send_error=MaxDeliveryUncertainError("missing message id")),
        settings_factory(),
    )

    await dispatcher.process_once(now_ms=now_ms)

    assert repo.ambiguous == [(1, "missing message id", now_ms)]
    assert repo.retried == []


@pytest.mark.asyncio
async def test_process_once_timeout_marks_ambiguous(
    sample_payload, settings_factory, now_ms
):
    from conftest import FakeTransport

    repo = FakeRepo([row(sample_payload)])
    dispatcher = build_dispatcher(
        repo,
        FakeTransport(send_error=TimeoutError("send timed out")),
        settings_factory(),
    )

    await dispatcher.process_once(now_ms=now_ms)

    assert repo.ambiguous == [(1, "send timed out", now_ms)]
    assert repo.retried == []


@pytest.mark.asyncio
async def test_ambiguous_row_reconciles_to_sent_before_resend(
    sample_payload, settings_factory, now_ms
):
    from conftest import FakeTransport

    from tg_max_bridge.models import MaxSendResult

    found = MaxSendResult(message_id=222, raw={"message_id": 222})
    repo = FakeRepo(
        [
            row(
                sample_payload,
                status="ambiguous",
                next_attempt_at=now_ms,
                updated_at=now_ms - 30_000,
            )
        ]
    )
    transport = FakeTransport(marker_result=found)
    dispatcher = build_dispatcher(repo, transport, settings_factory())

    await dispatcher.process_once(now_ms=now_ms)

    assert repo.sent[0][1].message_id == 222
    assert transport.sent_texts is None


@pytest.mark.asyncio
async def test_ambiguous_row_is_not_blindly_resent_after_reconciliation_miss(
    sample_payload,
    settings_factory,
    now_ms,
):
    from conftest import FakeTransport

    repo = FakeRepo(
        [
            row(
                sample_payload,
                status="ambiguous",
                next_attempt_at=now_ms,
                updated_at=now_ms - 30_000,
            )
        ]
    )
    transport = FakeTransport(marker_result=None)
    dispatcher = build_dispatcher(
        repo, transport, settings_factory(ambiguous_resend_after_seconds=600.0)
    )

    await dispatcher.process_once(now_ms=now_ms)

    assert repo.released == [(1, now_ms)]
    assert repo.sent == []
    assert transport.sent_texts is None


@pytest.mark.asyncio
async def test_ambiguous_reconciliation_error_does_not_strand_row(
    sample_payload,
    settings_factory,
    now_ms,
):
    from conftest import FakeTransport

    repo = FakeRepo(
        [
            row(
                sample_payload,
                status="ambiguous",
                next_attempt_at=now_ms,
                updated_at=now_ms - 30_000,
            )
        ]
    )
    transport = FakeTransport(marker_error=RuntimeError("MAX search unavailable"))
    dispatcher = build_dispatcher(repo, transport, settings_factory())

    processed = await dispatcher.process_once(now_ms=now_ms)

    assert processed == 1
    assert repo.retried == [(1, "MAX search unavailable", now_ms)]
    assert repo.sent == []
    assert transport.sent_texts is None
