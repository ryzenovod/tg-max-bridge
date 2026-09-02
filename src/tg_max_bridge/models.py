from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


@dataclass(frozen=True)
class TelegramSourceMessage:
    chat_id: int
    message_id: int
    trigger_message_id: int
    from_user_id: int
    from_display_name: str
    text: str
    date: datetime
    attachment_omitted: bool = False


@dataclass(frozen=True)
class MaxPayload:
    chat_id: int
    text: str
    marker: str


@dataclass(frozen=True)
class MaxSendResult:
    message_id: int
    raw: dict[str, Any]


@dataclass(frozen=True)
class EnqueueResult:
    record: OutboxRecord
    created: bool


@dataclass(frozen=True)
class OutboxRecord:
    id: int
    tg_chat_id: int
    tg_message_id: int
    tg_trigger_message_id: int
    tg_from_user_id: int
    tg_from_display_name: str
    source_text: str
    max_chat_id: int
    max_text: str
    marker: str
    status: OutboxStatus
    attempt_count: int
    next_attempt_at: int
    locked_at: int | None
    last_error: str | None
    max_message_id: int | None
    max_response_json: str | None
    created_at: int
    updated_at: int
    sent_at: int | None


@dataclass(frozen=True)
class RejectReason:
    code: str
    message: str
