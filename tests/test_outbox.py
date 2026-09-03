from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_by_source_and_target(
    tmp_path, sample_source, sample_payload
):
    from conftest import build_outbox_repository

    repo, conn = await build_outbox_repository(tmp_path)
    try:
        first = await repo.enqueue(sample_source, sample_payload)
        second = await repo.enqueue(sample_source, sample_payload)

        first_record = getattr(first, "record", first)
        second_record = getattr(second, "record", second)
        assert first_record.id == second_record.id

        rows = await conn.execute_fetchall("SELECT COUNT(*) FROM outbox")
        assert rows[0][0] == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_duplicate_enqueue_preserves_original_updated_at(
    monkeypatch, tmp_path, sample_source, sample_payload
):
    from conftest import build_outbox_repository

    from tg_max_bridge import outbox as outbox_module

    repo, conn = await build_outbox_repository(tmp_path)
    try:
        monkeypatch.setattr(outbox_module, "now_ms", lambda: 1_000)
        first = await repo.enqueue(sample_source, sample_payload)
        monkeypatch.setattr(outbox_module, "now_ms", lambda: 99_000)
        duplicate = await repo.enqueue(sample_source, sample_payload)

        assert first.created is True
        assert duplicate.created is False
        assert duplicate.record.id == first.record.id
        assert duplicate.record.updated_at == first.record.updated_at == 1_000

        rows = await conn.execute_fetchall(
            "SELECT created_at, updated_at FROM outbox WHERE id = ?",
            (first.record.id,),
        )
        assert rows[0][0] == 1_000
        assert rows[0][1] == 1_000
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_lease_due_marks_pending_rows_as_sending(
    tmp_path, sample_source, sample_payload, now_ms
):
    from conftest import build_outbox_repository

    from tg_max_bridge.models import OutboxStatus

    repo, conn = await build_outbox_repository(tmp_path)
    try:
        await repo.enqueue(sample_source, sample_payload)

        leased = await repo.lease_due(now_ms=now_ms, limit=10)

        assert len(leased) == 1
        assert leased[0].status == OutboxStatus.SENDING
        rows = await conn.execute_fetchall(
            "SELECT status, locked_at FROM outbox WHERE id = ?", (leased[0].id,)
        )
        assert rows[0][0] == OutboxStatus.SENDING.value
        assert rows[0][1] == now_ms
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_mark_sent_persists_max_metadata(
    tmp_path, sample_source, sample_payload, now_ms
):
    from conftest import build_outbox_repository

    from tg_max_bridge.models import MaxSendResult, OutboxStatus

    repo, conn = await build_outbox_repository(tmp_path)
    try:
        await repo.enqueue(sample_source, sample_payload)
        leased = await repo.lease_due(now_ms=now_ms, limit=1)

        await repo.mark_sent(
            leased[0].id,
            MaxSendResult(message_id=9876, raw={"message_id": 9876, "ok": True}),
            now_ms=now_ms + 100,
        )

        rows = await conn.execute_fetchall(
            "SELECT status, max_message_id, max_response_json, sent_at "
            "FROM outbox WHERE id = ?",
            (leased[0].id,),
        )
        assert rows[0][0] == OutboxStatus.SENT.value
        assert rows[0][1] == 9876
        assert json.loads(rows[0][2])["ok"] is True
        assert rows[0][3] == now_ms + 100
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_retry_uses_exponential_backoff_and_caps(
    tmp_path, sample_source, sample_payload, now_ms
):
    from conftest import build_outbox_repository

    from tg_max_bridge.models import OutboxStatus

    repo, conn = await build_outbox_repository(tmp_path)
    try:
        await repo.enqueue(sample_source, sample_payload)
        leased = await repo.lease_due(now_ms=now_ms, limit=1)

        await repo.mark_retry(leased[0].id, "network down", now_ms=now_ms)
        rows = await conn.execute_fetchall(
            "SELECT status, attempt_count, next_attempt_at, last_error, locked_at "
            "FROM outbox WHERE id = ?",
            (leased[0].id,),
        )

        assert rows[0][0] == OutboxStatus.PENDING.value
        assert rows[0][1] == 1
        assert now_ms < rows[0][2] <= now_ms + 300_000
        assert rows[0][3] == "network down"
        assert rows[0][4] is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ambiguous_then_reconciliation_miss_schedules_later_retry(
    tmp_path,
    sample_source,
    sample_payload,
    now_ms,
):
    from conftest import build_outbox_repository

    from tg_max_bridge.models import OutboxStatus

    repo, conn = await build_outbox_repository(tmp_path)
    try:
        await repo.enqueue(sample_source, sample_payload)
        leased = await repo.lease_due(now_ms=now_ms, limit=1)

        await repo.mark_ambiguous(
            leased[0].id, "timeout after write may have succeeded", now_ms=now_ms
        )
        rows = await conn.execute_fetchall(
            "SELECT status, last_error, next_attempt_at, locked_at "
            "FROM outbox WHERE id = ?",
            (leased[0].id,),
        )
        assert rows[0][0] == OutboxStatus.AMBIGUOUS.value
        assert "timeout" in rows[0][1]
        assert rows[0][2] > now_ms
        assert rows[0][3] is None

        await repo.release_after_reconciliation_miss(
            leased[0].id, now_ms=now_ms + 30_000
        )
        rows = await conn.execute_fetchall(
            "SELECT status, next_attempt_at FROM outbox WHERE id = ?", (leased[0].id,)
        )
        assert rows[0][0] == OutboxStatus.AMBIGUOUS.value
        assert rows[0][1] > now_ms + 30_000
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_lease_due_recovers_stale_sending_rows(
    tmp_path, sample_source, sample_payload, now_ms
):
    from conftest import build_outbox_repository

    from tg_max_bridge.models import OutboxStatus

    repo, conn = await build_outbox_repository(tmp_path)
    try:
        await repo.enqueue(sample_source, sample_payload)
        leased = await repo.lease_due(now_ms=now_ms, limit=1)
        stale_locked_at = now_ms - 3_600_000
        await conn.execute(
            """
            UPDATE outbox
            SET status = ?, locked_at = ?, next_attempt_at = ?
            WHERE id = ?
            """,
            (OutboxStatus.SENDING.value, stale_locked_at, 0, leased[0].id),
        )
        await conn.commit()

        recovered = await repo.lease_due(now_ms=now_ms, limit=1)

        assert [record.id for record in recovered] == [leased[0].id]
        assert recovered[0].status == OutboxStatus.AMBIGUOUS
        rows = await conn.execute_fetchall(
            "SELECT status, locked_at FROM outbox WHERE id = ?",
            (leased[0].id,),
        )
        assert rows[0][0] == OutboxStatus.SENDING.value
        assert rows[0][1] == now_ms
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_concurrent_repository_transactions_are_serialized(
    tmp_path, sample_source, sample_payload
):
    from conftest import build_outbox_repository

    repo, conn = await build_outbox_repository(tmp_path)
    try:
        sources_and_payloads = [
            (
                replace(
                    sample_source,
                    message_id=sample_source.message_id + offset,
                    trigger_message_id=sample_source.trigger_message_id + offset,
                ),
                replace(
                    sample_payload,
                    marker=f"{sample_payload.marker}-{offset}",
                    text=f"{sample_payload.text}\nconcurrent-{offset}",
                ),
            )
            for offset in range(25)
        ]

        results = await asyncio.gather(
            *(repo.enqueue(source, payload) for source, payload in sources_and_payloads)
        )

        assert all(result.created for result in results)
        assert len(await repo.list_records(limit=100)) == len(sources_and_payloads)
    finally:
        await conn.close()
