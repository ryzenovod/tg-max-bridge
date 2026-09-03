from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from typing import Any, Protocol

from .config import Settings
from .db import connect, init_schema
from .dispatcher import Dispatcher
from .max_transport import MaxTransport
from .outbox import OutboxRepository
from .outbox import now_ms as current_time_ms
from .telegram_bot import build_application

logger = logging.getLogger(__name__)
_BASE_LOG_RECORD_FACTORY = logging.getLogRecordFactory()
_BASE_LOGGER_MAKE_RECORD = logging.Logger.makeRecord
_REDACTION_TOKENS: set[str] = set()
_PRIMITIVE_LOG_TYPES = (type(None), bool, int, float)
_REDACTED_TELEGRAM_TOKEN = "[REDACTED_TELEGRAM_TOKEN]"


class LoggingSettings(Protocol):
    telegram_bot_token: Any
    log_level: str


class TokenRedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, *, telegram_bot_tokens: tuple[str, ...]) -> None:
        super().__init__(fmt)
        self._telegram_bot_tokens = telegram_bot_tokens

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        return _redact_text(message, self._telegram_bot_tokens)


def _redact_text(value: str, tokens: tuple[str, ...]) -> str:
    redacted = value
    for token in tokens:
        redacted = redacted.replace(token, _REDACTED_TELEGRAM_TOKEN)
    return redacted


def _safe_string(value: object, tokens: tuple[str, ...]) -> str:
    try:
        return _redact_text(str(value), tokens)
    except Exception:
        return f"<unprintable {type(value).__name__}>"


def _sanitize_log_value(
    value: object,
    tokens: tuple[str, ...],
    seen: set[int],
) -> object:
    if isinstance(value, str):
        return _redact_text(value, tokens)
    if isinstance(value, _PRIMITIVE_LOG_TYPES):
        return value

    value_id = id(value)
    if value_id in seen:
        return "<cycle>"

    if isinstance(value, dict):
        seen.add(value_id)
        try:
            return {
                _sanitize_log_value(key, tokens, seen): _sanitize_log_value(
                    item, tokens, seen
                )
                for key, item in value.items()
            }
        finally:
            seen.remove(value_id)
    if isinstance(value, list):
        seen.add(value_id)
        try:
            return [_sanitize_log_value(item, tokens, seen) for item in value]
        finally:
            seen.remove(value_id)
    if isinstance(value, tuple):
        seen.add(value_id)
        try:
            return tuple(_sanitize_log_value(item, tokens, seen) for item in value)
        finally:
            seen.remove(value_id)

    return _safe_string(value, tokens)


def _sanitize_log_record(record: logging.LogRecord, tokens: tuple[str, ...]) -> None:
    for key, value in record.__dict__.items():
        if key in {"args", "exc_info"}:
            continue
        record.__dict__[key] = _sanitize_log_value(value, tokens, set())


def _safe_message(record: logging.LogRecord, tokens: tuple[str, ...]) -> str:
    try:
        return _redact_text(record.getMessage(), tokens)
    except Exception:
        return _redact_text(
            f"{_safe_string(record.msg, tokens)} {_safe_string(record.args, tokens)}",
            tokens,
        )


def _coerce_token(settings: LoggingSettings) -> str:
    token = settings.telegram_bot_token
    if hasattr(token, "get_secret_value"):
        return str(token.get_secret_value())
    return str(token)


def _redacting_log_record_factory(
    telegram_bot_tokens: tuple[str, ...],
) -> Callable[..., logging.LogRecord]:
    def factory(
        *args: Any,
        **kwargs: Any,
    ) -> logging.LogRecord:
        record = _BASE_LOG_RECORD_FACTORY(*args, **kwargs)
        record.msg = _safe_message(record, telegram_bot_tokens)
        record.args = ()
        if record.exc_info is not None:
            record.exc_text = _redact_text(
                logging.Formatter().formatException(record.exc_info),
                telegram_bot_tokens,
            )
            record.exc_info = None
        if record.stack_info is not None:
            record.stack_info = _redact_text(record.stack_info, telegram_bot_tokens)
        _sanitize_log_record(record, telegram_bot_tokens)
        return record

    return factory


def _redacting_logger_make_record(
    telegram_bot_tokens: tuple[str, ...],
) -> Callable[..., logging.LogRecord]:
    def make_record(
        self: logging.Logger,
        *args: Any,
        **kwargs: Any,
    ) -> logging.LogRecord:
        record = _BASE_LOGGER_MAKE_RECORD(self, *args, **kwargs)
        _sanitize_log_record(record, telegram_bot_tokens)
        return record

    return make_record


def configure_logging(settings: LoggingSettings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    telegram_bot_token = _coerce_token(settings)
    if telegram_bot_token:
        _REDACTION_TOKENS.add(telegram_bot_token)
    telegram_bot_tokens = tuple(sorted(_REDACTION_TOKENS, key=len, reverse=True))
    logging.setLogRecordFactory(_redacting_log_record_factory(telegram_bot_tokens))
    logging.Logger.makeRecord = _redacting_logger_make_record(  # type: ignore[method-assign]
        telegram_bot_tokens
    )
    formatter = TokenRedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        telegram_bot_tokens=telegram_bot_tokens,
    )
    logging.basicConfig(level=level, handlers=[logging.StreamHandler()], force=True)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


async def run_service(settings: Settings) -> None:
    configure_logging(settings)
    db = await connect(settings.sqlite_path)
    dispatcher: Dispatcher | None = None
    stop_event: asyncio.Event | None = None
    polling_task: asyncio.Task[None] | None = None
    dispatcher_task: asyncio.Task[None] | None = None
    loop = asyncio.get_running_loop()
    installed_signal_handlers: list[signal.Signals] = []
    try:
        await init_schema(db)
        outbox = OutboxRepository(
            db,
            retry_base_seconds=settings.retry_base_seconds,
            retry_max_seconds=settings.retry_max_seconds,
            ambiguous_reconcile_delay_seconds=(
                settings.ambiguous_reconcile_delay_seconds
            ),
        )
        recovered = await outbox.recover_stale_sending(now_ms=current_time_ms())
        if recovered:
            logger.warning("Recovered %s stale sending outbox row(s)", recovered)
        dispatcher = Dispatcher(outbox, MaxTransport(settings), settings)
        application = build_application(settings, outbox)
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                continue
            installed_signal_handlers.append(sig)

        polling_task = asyncio.create_task(
            _run_polling(application, settings, stop_event),
            name="telegram-polling",
        )
        dispatcher_task = asyncio.create_task(
            dispatcher.run_until_stopped(),
            name="max-dispatcher",
        )
        await asyncio.sleep(0)
        done, _pending = await asyncio.wait(
            {polling_task, dispatcher_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        error = _first_task_error(done)
        if error is not None:
            raise error
        if dispatcher_task in done and not stop_event.is_set():
            raise RuntimeError("Dispatcher stopped unexpectedly")
        if polling_task in done and not stop_event.is_set():
            raise RuntimeError("Telegram polling stopped unexpectedly")
    finally:
        if dispatcher is not None:
            dispatcher.stop()
        if stop_event is not None:
            stop_event.set()
        tasks = [task for task in (polling_task, dispatcher_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for sig in installed_signal_handlers:
            loop.remove_signal_handler(sig)
        await db.close()


async def _run_polling(
    application: object, settings: Settings, stop: asyncio.Event
) -> None:
    from telegram import Update
    from telegram.ext import Application

    if not isinstance(application, Application):
        raise TypeError("application must be a Telegram Application")
    async with application:
        app_started = False
        updater_started = False
        try:
            await application.start()
            app_started = True
            if application.updater is None:
                raise RuntimeError("Telegram application has no updater")
            await application.updater.start_polling(
                timeout=settings.poll_timeout_seconds,
                allowed_updates=[Update.MESSAGE],
                drop_pending_updates=False,
            )
            updater_started = True
            await stop.wait()
        finally:
            if updater_started and application.updater is not None:
                await application.updater.stop()
            if app_started:
                await application.stop()


def _first_task_error(done: set[asyncio.Task[None]]) -> BaseException | None:
    for task in done:
        if task.cancelled():
            return None
        error = task.exception()
        if error is not None:
            return error
    return None
