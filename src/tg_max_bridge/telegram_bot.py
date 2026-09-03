from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from telegram import Chat, Message, Update, User
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings
from .formatting import UnsupportedMessageError, build_max_payload
from .models import RejectReason, TelegramSourceMessage
from .outbox import OutboxRepository


def extract_trigger(
    update: Update, settings: Settings
) -> TelegramSourceMessage | RejectReason:
    message = update.effective_message
    if message is None:
        return RejectReason("no_message", "No Telegram message in update.")
    chat_id = _chat_id(message)
    if message.chat.type not in {"group", "supergroup"}:
        return RejectReason("not_group", "Use /max inside the configured group.")
    if chat_id not in settings.telegram_allowed_chat_ids:
        return RejectReason("unauthorized_chat", "This Telegram chat is not allowed.")
    inline_text = _inline_command_text(message, settings)
    if inline_text is not None:
        if _is_unattributed_bot_message(message):
            return RejectReason("bot_loop", "Bot-authored messages are ignored.")
        return _extract_source_message(
            message,
            chat_id,
            message.message_id,
            override_text=inline_text,
        )
    if (
        message.from_user is None
        or message.from_user.id not in settings.telegram_allowed_user_ids
    ):
        return RejectReason("unauthorized_user", "This Telegram user is not allowed.")
    if not _is_max_command(message, settings):
        return RejectReason(
            "wrong_command", "Use /max as a reply to a message or write /max text."
        )
    if message.reply_to_message is None:
        return RejectReason(
            "missing_reply", "Reply to a text message with /max or write /max text."
        )

    source = message.reply_to_message
    return _extract_source_message(source, chat_id, message.message_id)


def extract_marker_trigger(
    update: Update, settings: Settings
) -> TelegramSourceMessage | RejectReason:
    message = update.effective_message
    if message is None:
        return RejectReason("no_message", "No Telegram message in update.")
    chat_id = _chat_id(message)
    if message.chat.type not in {"group", "supergroup"}:
        return RejectReason("not_group", "Use the marker inside the configured group.")
    if chat_id not in settings.telegram_allowed_chat_ids:
        return RejectReason("unauthorized_chat", "This Telegram chat is not allowed.")
    marker = settings.telegram_forward_marker
    if marker is None:
        return RejectReason(
            "marker_disabled", "Telegram marker forwarding is disabled."
        )
    if _is_unattributed_bot_message(message):
        return RejectReason("bot_loop", "Bot-authored marker messages are ignored.")
    source_text = message.text or message.caption or ""
    stripped_text = _strip_forward_marker(source_text, marker)
    if stripped_text is None:
        return RejectReason("missing_marker", f"Add {marker} to forward to MAX.")

    return _extract_source_message(
        message,
        chat_id,
        message.message_id,
        override_text=stripped_text,
    )


def _extract_source_message(
    source: Message,
    chat_id: int,
    trigger_message_id: int,
    *,
    override_text: str | None = None,
) -> TelegramSourceMessage | RejectReason:
    if getattr(source, "has_protected_content", False):
        return RejectReason(
            "protected_content",
            "This protected Telegram message cannot be copied.",
        )
    source_text = (
        override_text
        if override_text is not None
        else source.text or source.caption or ""
    )
    if not source_text.strip():
        return RejectReason(
            "unsupported_message", "Only text and captions are supported."
        )
    sender_chat = getattr(source, "sender_chat", None)
    source_user = source.from_user
    author_signature = getattr(source, "author_signature", None)
    if sender_chat is not None:
        source_author_id = sender_chat.id
        source_author_name = _display_chat(sender_chat)
        if author_signature:
            source_author_name = f"{author_signature} via {source_author_name}"
    elif source_user is not None:
        source_author_id = source_user.id
        source_author_name = _display_name(source_user)
    elif author_signature:
        source_author_id = 0
        source_author_name = f"{author_signature} (anonymous Telegram author)"
    else:
        return RejectReason("missing_author", "Cannot identify the Telegram author.")

    return TelegramSourceMessage(
        chat_id=chat_id,
        message_id=source.message_id,
        trigger_message_id=trigger_message_id,
        from_user_id=source_author_id,
        from_display_name=source_author_name,
        text=source_text,
        date=source.date or datetime.now(tz=UTC),
        attachment_omitted=_has_attachment(source),
    )


TelegramApplication = Application[Any, Any, Any, Any, Any, Any]


def build_application(
    settings: Settings, outbox: OutboxRepository
) -> TelegramApplication:
    token = settings.telegram_bot_token
    token_value = (
        token.get_secret_value() if hasattr(token, "get_secret_value") else str(token)
    )
    application = (
        Application.builder().token(token_value).concurrent_updates(False).build()
    )

    async def bridge_command(
        update: Update,
        _context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        source = extract_trigger(update, settings)
        if isinstance(source, RejectReason):
            await _ack(update, settings, source.message, is_error=True)
            return
        try:
            payload = build_max_payload(source, settings.max_chat_id)
        except UnsupportedMessageError as exc:
            await _ack(update, settings, str(exc), is_error=True)
            return
        result = await outbox.enqueue(source, payload)
        if result.created:
            await _ack(update, settings, "Queued for MAX.", is_error=False)
        else:
            await _ack(update, settings, "Already queued for MAX.", is_error=False)

    chat_filter = filters.Chat(chat_id=list(settings.telegram_allowed_chat_ids))
    application.add_handler(
        CommandHandler(
            "max",
            bridge_command,
            filters=chat_filter,
        )
    )

    async def bridge_marker(
        update: Update,
        _context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        source = extract_marker_trigger(update, settings)
        if isinstance(source, RejectReason):
            return
        try:
            payload = build_max_payload(source, settings.max_chat_id)
        except UnsupportedMessageError:
            return
        await outbox.enqueue(source, payload)

    application.add_handler(
        MessageHandler(
            chat_filter & (filters.TEXT | filters.CaptionRegex(".+")),
            bridge_marker,
        )
    )
    return application


def _is_max_command(message: Message, settings: Settings) -> bool:
    text = message.text or ""
    parts = text.split()
    if len(parts) != 1:
        return False
    command = parts[0].casefold()
    if command == "/max":
        return True
    if not command.startswith("/max@"):
        return False
    bot_username = settings.telegram_bot_username
    return bot_username is not None and command == f"/max@{bot_username}"


def _inline_command_text(message: Message, settings: Settings) -> str | None:
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return None
    command = parts[0].casefold()
    if command == "/max":
        return parts[1].strip()
    if not command.startswith("/max@"):
        return None
    bot_username = settings.telegram_bot_username
    if bot_username is None or command != f"/max@{bot_username}":
        return None
    return parts[1].strip()


def _display_name(user: User) -> str:
    if user.full_name:
        return user.full_name
    if user.username:
        return f"@{user.username}"
    return str(user.id)


def _display_chat(chat: Chat) -> str:
    if chat.title:
        return chat.title
    if chat.username:
        return f"@{chat.username}"
    return str(chat.id)


def _has_attachment(message: Message) -> bool:
    if message.caption is not None:
        return True
    return any(
        bool(getattr(message, field, None))
        for field in (
            "animation",
            "audio",
            "document",
            "photo",
            "sticker",
            "video",
            "video_note",
            "voice",
        )
    )


def _is_unattributed_bot_message(message: Message) -> bool:
    return bool(
        getattr(message.from_user, "is_bot", False)
        and getattr(message, "sender_chat", None) is None
    )


def _strip_forward_marker(text: str, marker: str) -> str | None:
    pattern = re.compile(rf"(?<!\S){re.escape(marker)}(?!\S)", flags=re.IGNORECASE)
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    chunks: list[str] = []
    position = 0
    for match in matches:
        start, end = match.span()
        remove_start = start
        remove_end = end
        line_start = text.rfind("\n", 0, start) + 1
        line_prefix = text[line_start:start]
        if line_prefix.strip(" \t") == "":
            while remove_end < len(text) and text[remove_end] in " \t":
                remove_end += 1
            if remove_end == len(text) or text[remove_end] == "\n":
                remove_start = line_start
                if remove_start == 0 and remove_end < len(text):
                    remove_end += 1
                elif (
                    remove_start > position
                    and remove_end < len(text)
                    and text[remove_start - 1] == "\n"
                    and text[remove_end] == "\n"
                ):
                    remove_end += 1
        else:
            while remove_end < len(text) and text[remove_end] in " \t":
                remove_end += 1
        chunks.append(text[position:remove_start])
        position = remove_end
    chunks.append(text[position:])
    cleaned = "".join(chunks)
    return cleaned.strip()


async def _ack(
    update: Update,
    settings: Settings,
    text: str,
    *,
    is_error: bool,
) -> None:
    if settings.telegram_ack_mode == "never":
        return
    if settings.telegram_ack_mode == "errors" and not is_error:
        return
    message = update.effective_message
    if message is not None:
        await message.reply_text(text)


def _chat_id(message: Message) -> int:
    chat_id = getattr(message, "chat_id", None)
    if chat_id is not None:
        return int(chat_id)
    return int(message.chat.id)
