from __future__ import annotations

import asyncio
import logging
import signal

from .config import Settings
from .db import connect, init_schema
from .dispatcher import Dispatcher
from .max_transport import MaxTransport
from .outbox import OutboxRepository
from .outbox import now_ms as current_time_ms
from .telegram_bot import build_application

logger = logging.getLogger(__name__)


async def run_service(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
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
