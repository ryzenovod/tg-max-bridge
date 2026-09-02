from __future__ import annotations

from datetime import UTC
from zoneinfo import ZoneInfo

from .models import MaxPayload, TelegramSourceMessage


class UnsupportedMessageError(ValueError):
    pass


MAX_MESSAGE_LENGTH = 4000
MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")
ATTACHMENT_OMITTED_NOTICE = "[Telegram attachment was not copied; the caption follows.]"
TRUNCATION_NOTICE = "\n\n[Truncated to the MAX 4000-character limit.]"


def build_marker(tg_chat_id: int, tg_message_id: int) -> str:
    return f"[tg:{tg_chat_id}/{tg_message_id}]"


def build_max_payload(source: TelegramSourceMessage, max_chat_id: int) -> MaxPayload:
    text = source.text.strip()
    if not text:
        raise UnsupportedMessageError("source message has no text or caption")

    marker = build_marker(source.chat_id, source.message_id)
    source_date = source.date
    if source_date.tzinfo is None:
        source_date = source_date.replace(tzinfo=UTC)
    timestamp = source_date.astimezone(MOSCOW_TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")
    attachment_notice = (
        f"{ATTACHMENT_OMITTED_NOTICE}\n\n" if source.attachment_omitted else ""
    )
    body = (
        f"{marker}\n"
        f"Telegram reserve copy\n"
        f"From: {source.from_display_name} ({source.from_user_id})\n"
        f"At: {timestamp}\n\n"
        f"{attachment_notice}"
        f"{text}"
    )
    if len(body) > MAX_MESSAGE_LENGTH:
        body = body[: MAX_MESSAGE_LENGTH - len(TRUNCATION_NOTICE)]
        body += TRUNCATION_NOTICE
    return MaxPayload(chat_id=max_chat_id, text=body, marker=marker)
