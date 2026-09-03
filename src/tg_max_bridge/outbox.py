from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import replace
from typing import Any

import aiosqlite

from .db import transaction
from .models import (
    EnqueueResult,
    MaxPayload,
    MaxSendResult,
    OutboxRecord,
    OutboxStatus,
    TelegramSourceMessage,
)


def now_ms() -> int:
    return int(time.time() * 1000)


def _row_to_record(row: aiosqlite.Row) -> OutboxRecord:
    data: dict[str, Any] = dict(row)
    data["status"] = OutboxStatus(data["status"])
    return OutboxRecord(**data)


async def _fetchone(
    db: aiosqlite.Connection,
    sql: str,
    params: tuple[object, ...],
) -> aiosqlite.Row | None:
    async with db.execute(sql, params) as cursor:
        return await cursor.fetchone()


class OutboxRepository:
    def __init__(
        self,
        db: aiosqlite.Connection,
        *,
        retry_base_seconds: float,
        retry_max_seconds: float,
        ambiguous_reconcile_delay_seconds: float,
    ) -> None:
        self._db = db
        self._retry_base_ms = int(retry_base_seconds * 1000)
        self._retry_max_ms = int(retry_max_seconds * 1000)
        self._ambiguous_reconcile_delay_ms = int(
            ambiguous_reconcile_delay_seconds * 1000
        )
        self._sending_stale_ms = 300_000
        self._lock = asyncio.Lock()

    async def enqueue(
        self,
        source: TelegramSourceMessage,
        payload: MaxPayload,
    ) -> EnqueueResult:
        current = now_ms()
        async with self._lock:
            async with transaction(self._db):
                cursor = await self._db.execute(
                    """
                    INSERT OR IGNORE INTO outbox (
                      tg_chat_id, tg_message_id, tg_trigger_message_id,
                      tg_from_user_id, tg_from_display_name, source_text,
                      max_chat_id, max_text, marker, status, next_attempt_at,
                      created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source.chat_id,
                        source.message_id,
                        source.trigger_message_id,
                        source.from_user_id,
                        source.from_display_name,
                        source.text,
                        payload.chat_id,
                        payload.text,
                        payload.marker,
                        OutboxStatus.PENDING.value,
                        0,
                        current,
                        current,
                    ),
                )
                created = cursor.rowcount == 1
            record = await self._get_by_source_unlocked(
                source.chat_id,
                source.message_id,
                payload.chat_id,
            )
        if record is None:
            raise RuntimeError("failed to load enqueued outbox row")
        return EnqueueResult(record=record, created=created)

    async def lease_due(self, *, now_ms: int, limit: int) -> list[OutboxRecord]:
        async with self._lock:
            async with transaction(self._db):
                rows = await self._db.execute_fetchall(
                    """
                    SELECT *
                    FROM outbox
                    WHERE (
                      status IN (?, ?) AND next_attempt_at <= ?
                    ) OR (
                      status = ? AND locked_at IS NOT NULL AND locked_at <= ?
                    )
                    ORDER BY next_attempt_at ASC, id ASC
                    LIMIT ?
                    """,
                    (
                        OutboxStatus.PENDING.value,
                        OutboxStatus.AMBIGUOUS.value,
                        now_ms,
                        OutboxStatus.SENDING.value,
                        now_ms - self._sending_stale_ms,
                        limit,
                    ),
                )
                ids = [int(row["id"]) for row in rows]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    await self._db.execute(
                        f"""
                        UPDATE outbox
                        SET status = ?, locked_at = ?
                        WHERE id IN ({placeholders})
                        """,
                        (OutboxStatus.SENDING.value, now_ms, *ids),
                    )
        leased_records: list[OutboxRecord] = []
        for record in (_row_to_record(row) for row in rows):
            if record.status is OutboxStatus.PENDING:
                record = replace(record, status=OutboxStatus.SENDING)
            elif record.status is OutboxStatus.SENDING:
                # A stale in-flight delivery may already exist in MAX. The row is
                # leased as SENDING in SQLite, but the dispatcher must reconcile it
                # before attempting another send.
                record = replace(record, status=OutboxStatus.AMBIGUOUS)
            leased_records.append(record)
        return leased_records

    async def mark_sent(
        self,
        outbox_id: int,
        result: MaxSendResult,
        *,
        now_ms: int,
    ) -> None:
        async with self._lock:
            async with transaction(self._db):
                await self._db.execute(
                    """
                    UPDATE outbox
                    SET status = ?, locked_at = NULL, last_error = NULL,
                        max_message_id = ?, max_response_json = ?,
                        sent_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        OutboxStatus.SENT.value,
                        result.message_id,
                        json.dumps(result.raw, ensure_ascii=False, sort_keys=True),
                        now_ms,
                        now_ms,
                        outbox_id,
                    ),
                )

    async def mark_retry(self, outbox_id: int, error: str, *, now_ms: int) -> None:
        async with self._lock:
            async with transaction(self._db):
                record = await self._get_unlocked(outbox_id)
                if record is None:
                    return
                attempt_count = record.attempt_count + 1
                delay = min(
                    self._retry_max_ms,
                    self._retry_base_ms * int(math.pow(2, min(attempt_count - 1, 8))),
                )
                await self._db.execute(
                    """
                    UPDATE outbox
                    SET status = ?, attempt_count = ?, next_attempt_at = ?,
                        locked_at = NULL, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        OutboxStatus.PENDING.value,
                        attempt_count,
                        now_ms + delay,
                        error,
                        now_ms,
                        outbox_id,
                    ),
                )

    async def mark_ambiguous(self, outbox_id: int, error: str, *, now_ms: int) -> None:
        async with self._lock:
            async with transaction(self._db):
                record = await self._get_unlocked(outbox_id)
                if record is None:
                    return
                await self._db.execute(
                    """
                    UPDATE outbox
                    SET status = ?, attempt_count = ?, next_attempt_at = ?,
                        locked_at = NULL, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        OutboxStatus.AMBIGUOUS.value,
                        record.attempt_count + 1,
                        now_ms + self._ambiguous_reconcile_delay_ms,
                        error,
                        now_ms,
                        outbox_id,
                    ),
                )

    async def release_after_reconciliation_miss(
        self,
        outbox_id: int,
        *,
        now_ms: int,
    ) -> None:
        async with self._lock:
            async with transaction(self._db):
                await self._db.execute(
                    """
                    UPDATE outbox
                    SET status = ?, locked_at = NULL, next_attempt_at = ?
                    WHERE id = ?
                    """,
                    (
                        OutboxStatus.AMBIGUOUS.value,
                        now_ms + self._ambiguous_reconcile_delay_ms,
                        outbox_id,
                    ),
                )

    async def recover_stale_sending(self, *, now_ms: int) -> int:
        async with self._lock:
            async with transaction(self._db):
                cursor = await self._db.execute(
                    """
                    UPDATE outbox
                    SET status = ?, locked_at = NULL, next_attempt_at = ?,
                        last_error = ?, updated_at = ?
                    WHERE status = ?
                    """,
                    (
                        OutboxStatus.AMBIGUOUS.value,
                        now_ms + self._ambiguous_reconcile_delay_ms,
                        "Recovered stale sending row after startup.",
                        now_ms,
                        OutboxStatus.SENDING.value,
                    ),
                )
                return cursor.rowcount

    async def mark_reconciliation_error(
        self,
        outbox_id: int,
        error: str,
        *,
        now_ms: int,
    ) -> None:
        async with self._lock:
            async with transaction(self._db):
                await self._db.execute(
                    """
                    UPDATE outbox
                    SET status = ?, locked_at = NULL, next_attempt_at = ?,
                        last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        OutboxStatus.AMBIGUOUS.value,
                        now_ms + self._ambiguous_reconcile_delay_ms,
                        error,
                        now_ms,
                        outbox_id,
                    ),
                )

    async def get_by_source(
        self,
        tg_chat_id: int,
        tg_message_id: int,
        max_chat_id: int,
    ) -> OutboxRecord | None:
        async with self._lock:
            return await self._get_by_source_unlocked(
                tg_chat_id,
                tg_message_id,
                max_chat_id,
            )

    async def get(self, outbox_id: int) -> OutboxRecord | None:
        async with self._lock:
            return await self._get_unlocked(outbox_id)

    async def _get_by_source_unlocked(
        self,
        tg_chat_id: int,
        tg_message_id: int,
        max_chat_id: int,
    ) -> OutboxRecord | None:
        row = await _fetchone(
            self._db,
            """
            SELECT *
            FROM outbox
            WHERE tg_chat_id = ? AND tg_message_id = ? AND max_chat_id = ?
            """,
            (tg_chat_id, tg_message_id, max_chat_id),
        )
        return _row_to_record(row) if row else None

    async def _get_unlocked(self, outbox_id: int) -> OutboxRecord | None:
        row = await _fetchone(
            self._db,
            "SELECT * FROM outbox WHERE id = ?",
            (outbox_id,),
        )
        return _row_to_record(row) if row else None

    async def list_records(
        self,
        *,
        status: OutboxStatus | None = None,
        limit: int = 50,
    ) -> list[OutboxRecord]:
        async with self._lock:
            if status is None:
                rows = await self._db.execute_fetchall(
                    "SELECT * FROM outbox ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            else:
                rows = await self._db.execute_fetchall(
                    "SELECT * FROM outbox WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status.value, limit),
                )
            return [_row_to_record(row) for row in rows]
