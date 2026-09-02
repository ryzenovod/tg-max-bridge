from __future__ import annotations

import asyncio

from .config import Settings
from .max_transport import (
    MaxDeliveryUncertainError,
    MaxTransport,
    MaxTransportUnavailableError,
)
from .models import OutboxRecord, OutboxStatus
from .outbox import OutboxRepository
from .outbox import now_ms as current_time_ms


class Dispatcher:
    def __init__(
        self,
        outbox: OutboxRepository,
        transport: MaxTransport,
        settings: Settings,
    ) -> None:
        self._outbox = outbox
        self._transport = transport
        self._settings = settings
        self._stop = asyncio.Event()
        self._ambiguous_resend_after_ms = int(
            settings.ambiguous_resend_after_seconds * 1000
        )

    def stop(self) -> None:
        self._stop.set()

    async def run_until_stopped(self) -> None:
        try:
            while not self._stop.is_set():
                await self.process_once()
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self._settings.delivery_tick_seconds,
                    )
                except TimeoutError:
                    pass
        finally:
            await self._transport.stop()

    async def process_once(self, *, now_ms: int | None = None) -> int:
        current = now_ms if now_ms is not None else current_time_ms()
        records = await self._outbox.lease_due(now_ms=current, limit=10)
        for record in records:
            await self._process_record(record, current)
        return len(records)

    async def _process_record(self, record: OutboxRecord, now_ms: int) -> None:
        if record.status is OutboxStatus.AMBIGUOUS:
            try:
                found = await self._transport.find_marker(
                    record.max_chat_id,
                    record.marker,
                    scan_limit=self._settings.max_reconcile_scan_limit,
                )
            except Exception as exc:
                handler = getattr(
                    self._outbox,
                    "mark_reconciliation_error",
                    self._outbox.mark_retry,
                )
                await handler(record.id, str(exc), now_ms=now_ms)
                return
            if found is not None:
                await self._outbox.mark_sent(record.id, found, now_ms=now_ms)
                return
            if now_ms - record.updated_at < self._ambiguous_resend_after_ms:
                await self._outbox.release_after_reconciliation_miss(
                    record.id,
                    now_ms=now_ms,
                )
                return

        try:
            result = await self._transport.send_text(
                record.max_chat_id, record.max_text
            )
        except (MaxDeliveryUncertainError, TimeoutError) as exc:
            await self._outbox.mark_ambiguous(record.id, str(exc), now_ms=now_ms)
        except MaxTransportUnavailableError as exc:
            await self._outbox.mark_retry(record.id, str(exc), now_ms=now_ms)
        except Exception as exc:
            await self._outbox.mark_retry(record.id, str(exc), now_ms=now_ms)
        else:
            await self._outbox.mark_sent(record.id, result, now_ms=now_ms)
