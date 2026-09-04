from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, cast
from urllib.parse import urlparse

from aiohttp import web
from telegram import Update

from .config import Settings
from .dispatcher import Dispatcher
from .formatting import UnsupportedMessageError, build_max_payload
from .models import OutboxStatus, RejectReason, TelegramSourceMessage
from .outbox import OutboxRepository
from .telegram_bot import TelegramApplication, extract_marker_trigger, extract_trigger

logger = logging.getLogger(__name__)


async def run_webhook(
    application: TelegramApplication | None,
    dispatcher: Dispatcher,
    outbox: OutboxRepository,
    settings: Settings,
    stop: asyncio.Event,
) -> None:
    if settings.telegram_webhook_url is None:
        raise RuntimeError("TELEGRAM_WEBHOOK_URL is required in webhook mode")
    if settings.telegram_webhook_secret is None:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required in webhook mode")

    webhook = TelegramWebhook(
        settings=settings,
        application=application,
        dispatcher=dispatcher,
        outbox=outbox,
    )
    app = web.Application(client_max_size=settings.telegram_webhook_max_bytes)
    app["max_body_size"] = settings.telegram_webhook_max_bytes
    app.router.add_get("/healthz", _healthz)
    app.router.add_post(
        _webhook_path(settings.telegram_webhook_url),
        webhook.handle_update,
    )

    runner = web.AppRunner(app, access_log=None)
    webhook_started = False
    runner_started = False
    try:
        if settings.telegram_webhook_auto_register:
            await webhook.start_application()
            webhook_started = True
        await runner.setup()
        site = web.TCPSite(
            runner,
            host=settings.telegram_webhook_listen_host,
            port=settings.port,
        )
        await site.start()
        runner_started = True
        if settings.telegram_webhook_auto_register:
            await webhook.register_webhook()
        logger.info(
            "Telegram webhook listening on %s:%s",
            settings.telegram_webhook_listen_host,
            settings.port,
        )
        await stop.wait()
    finally:
        if runner_started:
            await runner.cleanup()
        if webhook_started:
            await webhook.stop()


class TelegramWebhook:
    def __init__(
        self,
        *,
        settings: Settings,
        application: TelegramApplication | None,
        dispatcher: Dispatcher,
        outbox: OutboxRepository,
        max_body_size: int | None = None,
    ) -> None:
        self._application = application
        self._dispatcher = dispatcher
        self._outbox = outbox
        self._settings = settings
        self._secret = settings.telegram_webhook_secret
        self._max_body_size = (
            max_body_size
            if max_body_size is not None
            else settings.telegram_webhook_max_bytes
        )
        self.application = application

    async def start_application(self) -> None:
        if self._application is None:
            raise RuntimeError("Telegram application is required for auto-registration")
        await self._application.initialize()
        app_started = False
        try:
            await self._application.start()
            app_started = True
        except Exception:
            if app_started:
                await self._application.stop()
            await self._application.shutdown()
            raise

    async def register_webhook(self) -> None:
        if self._settings.telegram_webhook_url is None:
            raise RuntimeError("TELEGRAM_WEBHOOK_URL is required in webhook mode")
        if self._secret is None:
            raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required in webhook mode")
        if self._application is None:
            raise RuntimeError("Telegram application is required for auto-registration")
        await self._application.bot.set_webhook(
            url=self._settings.telegram_webhook_url,
            secret_token=self._secret.get_secret_value(),
            allowed_updates=["message"],
            drop_pending_updates=False,
        )

    async def stop(self) -> None:
        if self._application is None:
            return
        await self._application.stop()
        await self._application.shutdown()

    async def handle_update(self, request: object) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=403)

        max_body_size = _max_body_size(request, self._max_body_size)
        content_length = getattr(request, "content_length", None)
        if content_length is not None and int(content_length) > max_body_size:
            return web.Response(status=413)
        body = await _read_body(request)
        if len(body) > max_body_size:
            return web.Response(status=413)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400)
        if not isinstance(payload, dict):
            return web.Response(status=400)

        try:
            bot = self._application.bot if self._application is not None else None
            update = Update.de_json(payload, bot)
        except (KeyError, TypeError, ValueError):
            return web.Response(status=400)
        expected_source = _accepted_source(update, self._settings)

        if expected_source is not None:
            try:
                payload = build_max_payload(expected_source, self._settings.max_chat_id)
            except UnsupportedMessageError:
                return web.Response(status=200)
            await self._outbox.enqueue(expected_source, payload)

        if expected_source is None:
            return web.Response(status=200)
        record = await self._outbox.get_by_source(
            expected_source.chat_id,
            expected_source.message_id,
            self._settings.max_chat_id,
        )
        if record is not None and str(record.status) == OutboxStatus.SENT.value:
            return web.Response(status=200)
        return web.Response(status=503)

    def _authorized(self, request: object) -> bool:
        if self._secret is None:
            return False
        headers = cast(Any, request).headers
        actual = str(headers.get("X-Telegram-Bot-Api-Secret-Token", ""))
        expected = self._secret.get_secret_value()
        return hmac.compare_digest(actual, expected)


def _accepted_source(
    update: Update,
    settings: Settings,
) -> TelegramSourceMessage | None:
    source = extract_trigger(update, settings)
    if isinstance(source, RejectReason):
        source = extract_marker_trigger(update, settings)
        if isinstance(source, RejectReason):
            return None
    try:
        build_max_payload(source, settings.max_chat_id)
    except UnsupportedMessageError:
        return None
    return source


async def _healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


async def _read_body(request: object) -> bytes:
    if hasattr(request, "read"):
        body = await cast(Any, request).read()
        if not isinstance(body, bytes):
            raise TypeError("request body must be bytes")
        return body
    content = cast(Any, request).content
    body = await content.read()
    if not isinstance(body, bytes):
        raise TypeError("request body must be bytes")
    return body


def _max_body_size(request: object, default: int) -> int:
    app = getattr(request, "app", None)
    if isinstance(app, dict):
        return int(app.get("max_body_size", default))
    return default


def _webhook_path(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    return path
