from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Annotated

import typer
from telegram import Bot

from .config import MaxDiscoverySettings, Settings, TelegramDiscoverySettings
from .db import connect, init_schema
from .max_transport import MaxTransport
from .models import OutboxStatus
from .outbox import OutboxRepository
from .service import configure_logging, run_service
from .telegram_bot import FORWARD_ALLOWED_UPDATES

app = typer.Typer(no_args_is_help=True)
logger = logging.getLogger(__name__)


@app.command("init-db")
def init_db() -> None:
    settings = Settings()  # type: ignore[call-arg]
    asyncio.run(_init_db(settings))


@app.command()
def doctor() -> None:
    settings = Settings()  # type: ignore[call-arg]
    asyncio.run(_doctor(settings))


@app.command("discover-max")
def discover_max(
    query: Annotated[str | None, typer.Option("--query", "-q")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 100,
) -> None:
    settings = MaxDiscoverySettings()  # type: ignore[call-arg]
    _require_max_session(settings)
    asyncio.run(_discover_max(settings, query=query, limit=limit))


@app.command("discover-telegram")
def discover_telegram(
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 100,
) -> None:
    """Show Telegram chat/user IDs from recent bot commands without consuming them."""
    settings = TelegramDiscoverySettings()  # type: ignore[call-arg]
    configure_logging(settings)
    try:
        asyncio.run(_discover_telegram(settings, limit=limit))
    except Exception:
        logger.exception("Telegram discovery stopped with an unhandled error")
        raise typer.Exit(1) from None


@app.command()
def outbox(
    status: Annotated[OutboxStatus | None, typer.Option("--status")] = None,
) -> None:
    settings = Settings()  # type: ignore[call-arg]
    asyncio.run(_outbox(settings, status=status))


@app.command()
def run() -> None:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings)
    try:
        asyncio.run(run_service(settings))
    except Exception:
        logger.exception("Bridge stopped with an unhandled error")
        raise typer.Exit(1) from None


async def _init_db(settings: Settings) -> None:
    db = await connect(settings.sqlite_path)
    try:
        await init_schema(db)
    finally:
        await db.close()
    typer.echo(f"SQLite ready: {settings.sqlite_path}")


async def _doctor(settings: Settings) -> None:
    typer.echo("Checking environment...")
    await _init_db(settings)
    _require_max_session(settings)

    transport = MaxTransport(settings)
    await transport.start()
    try:
        chat = await transport.get_chat(settings.max_chat_id)
    finally:
        await transport.stop()
    title = _terminal_text(chat.get("title") or chat.get("name") or "(untitled)")
    typer.echo(f"MAX chat reachable: {settings.max_chat_id} {title}")
    typer.echo("Doctor passed without sending messages.")


def _require_max_session(settings: MaxDiscoverySettings) -> Path:
    session_dir = Path.home() / ".max-mcp"
    session_db = session_dir / "session.db"
    if not session_db.exists():
        raise typer.BadParameter(
            "MAX session is missing. Run: "
            f"uv run --no-dev --frozen --directory {settings.max_mcp_directory} "
            "max-mcp-login login-qr or: "
            f"uv run --no-dev --frozen --directory {settings.max_mcp_directory} "
            "max-mcp-login login-sms --phone +70000000000"
        )
    if not os.access(session_db, os.R_OK):
        raise typer.BadParameter(f"MAX session is not readable: {session_db}")
    return session_db


async def _discover_max(
    settings: MaxDiscoverySettings, *, query: str | None, limit: int
) -> None:
    transport = MaxTransport(settings)
    await transport.start()
    try:
        marker: int | None = None
        shown = 0
        while shown < limit:
            chats, marker = await transport.list_chats(
                limit=min(100, limit - shown), marker=marker
            )
            if not chats:
                break
            for chat in chats:
                title = str(chat.get("title") or chat.get("name") or "")
                if query and query.casefold() not in title.casefold():
                    continue
                chat_type = _terminal_text(chat.get("type", ""))
                typer.echo(f"{chat.get('id')}\t{chat_type}\t{_terminal_text(title)}")
                shown += 1
                if shown >= limit:
                    break
            if marker is None:
                break
    finally:
        await transport.stop()


async def _discover_telegram(
    settings: TelegramDiscoverySettings, *, limit: int
) -> None:
    token = settings.telegram_bot_token
    token_value = (
        token.get_secret_value() if hasattr(token, "get_secret_value") else str(token)
    )
    async with Bot(token=token_value) as bot:
        me = await bot.get_me()
        updates = await bot.get_updates(
            limit=limit,
            timeout=0,
            allowed_updates=list(FORWARD_ALLOWED_UPDATES),
        )

    shown: set[tuple[int, int | None]] = set()
    for update in updates:
        message = update.effective_message
        if message is None:
            continue
        chat_id = int(message.chat.id)
        user_id = int(message.from_user.id) if message.from_user is not None else None
        identity = (chat_id, user_id)
        if identity in shown:
            continue
        shown.add(identity)
        chat_title = _terminal_text(
            message.chat.title or message.chat.username or message.chat.type
        )
        user_name = (
            message.from_user.full_name
            if message.from_user is not None
            else "anonymous"
        )
        typer.echo(
            f"chat_id={chat_id}\tchat={chat_title}\t"
            f"user_id={user_id if user_id is not None else '-'}\t"
            f"user={_terminal_text(user_name)}"
        )

    if not shown:
        username = _terminal_text(me.username) if me.username else None
        command = f"/max@{username}" if username else "/max"
        typer.echo(
            "No recent group commands found. Add the bot to the group, send "
            f"{command}, then run this command again before starting the bridge."
        )


async def _outbox(settings: Settings, *, status: OutboxStatus | None) -> None:
    db = await connect(settings.sqlite_path)
    await init_schema(db)
    repo = OutboxRepository(
        db,
        retry_base_seconds=settings.retry_base_seconds,
        retry_max_seconds=settings.retry_max_seconds,
        ambiguous_reconcile_delay_seconds=settings.ambiguous_reconcile_delay_seconds,
    )
    try:
        records = await repo.list_records(status=status)
        for record in records:
            typer.echo(
                f"{record.id}\t{record.status.value}\t"
                f"tg={record.tg_chat_id}/{record.tg_message_id}\t"
                f"max={record.max_chat_id}\tattempts={record.attempt_count}\t"
                f"marker={record.marker}"
            )
    finally:
        await db.close()


def _terminal_text(value: object) -> str:
    return "".join(
        character if character.isprintable() else " " for character in str(value)
    )
