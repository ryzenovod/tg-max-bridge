from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import typer


@pytest.mark.asyncio
async def test_doctor_without_max_session_points_to_manual_login(
    monkeypatch, settings_factory, tmp_path
):
    from tg_max_bridge import cli

    monkeypatch.setenv("HOME", str(tmp_path / "missing-home"))

    with pytest.raises(typer.BadParameter) as exc_info:
        await cli._doctor(settings_factory(sqlite_path=tmp_path / "bridge.sqlite3"))

    message = str(exc_info.value)
    assert "MAX session is missing" in message
    assert "max-mcp-login login-qr" in message
    assert "max-mcp-login login-sms" in message
    assert "123:test-token" not in message


@pytest.mark.asyncio
async def test_discover_max_filters_chats_and_never_sends(
    monkeypatch, settings_factory, capsys
):
    from tg_max_bridge import cli

    class FakeCliTransport:
        def __init__(self, settings):
            self.settings = settings
            self.sent = []

        async def start(self):
            pass

        async def stop(self):
            pass

        async def list_chats(self, *, limit: int = 100, marker: int | None = None):
            assert limit == 10
            assert marker is None
            return (
                [
                    {"id": 1, "type": "group", "title": "Reserve Ops"},
                    {"id": 2, "type": "channel", "title": "Random\x1b[31m"},
                ],
                None,
            )

        async def send_text(self, chat_id: int, text: str):
            self.sent.append((chat_id, text))
            raise AssertionError("discover-max must not send messages")

    monkeypatch.setattr(cli, "MaxTransport", FakeCliTransport)

    await cli._discover_max(settings_factory(), query="reserve", limit=10)

    output = capsys.readouterr().out
    assert "1\tgroup\tReserve Ops" in output
    assert "Random" not in output


def test_terminal_text_removes_control_characters():
    from tg_max_bridge.cli import _terminal_text

    assert _terminal_text("safe\n\x1b[31m") == "safe  [31m"


@pytest.mark.asyncio
async def test_discover_telegram_prints_chat_and_user_ids(
    monkeypatch, settings_factory, capsys
):
    from telegram import Chat, Message, Update, User

    from tg_max_bridge import cli

    update = Update(
        update_id=1,
        message=Message(
            message_id=10,
            date=datetime.now(UTC),
            chat=Chat(id=-100987, type="supergroup", title="Reserve group"),
            from_user=User(id=456, first_name="Allowed User", is_bot=False),
            text="/max",
        ),
    )

    class FakeBot:
        def __init__(self, *, token):
            assert token == "123:test-token"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get_me(self):
            return User(
                id=999,
                first_name="Bridge Bot",
                is_bot=True,
                username="maxmcpbot",
            )

        async def get_updates(self, **kwargs):
            assert kwargs == {
                "limit": 10,
                "timeout": 0,
                "allowed_updates": [Update.MESSAGE],
            }
            return (update, update)

    monkeypatch.setattr(cli, "Bot", FakeBot)

    await cli._discover_telegram(settings_factory(), limit=10)

    output = capsys.readouterr().out
    assert output.count("chat_id=-100987") == 1
    assert "user_id=456" in output
    assert "Reserve group" in output


@pytest.mark.asyncio
async def test_discover_telegram_empty_result_shows_addressed_command(
    monkeypatch, settings_factory, capsys
):
    from telegram import User

    from tg_max_bridge import cli

    class FakeBot:
        def __init__(self, *, token):
            assert token == "123:test-token"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get_me(self):
            return User(
                id=999,
                first_name="Bridge Bot",
                is_bot=True,
                username="maxmcpbot",
            )

        async def get_updates(self, **kwargs):
            return ()

    monkeypatch.setattr(cli, "Bot", FakeBot)

    await cli._discover_telegram(settings_factory(), limit=10)

    output = capsys.readouterr().out
    assert "/max@maxmcpbot" in output


def test_operator_docs_and_examples_do_not_contain_real_secrets_or_sessions():
    root = Path(__file__).resolve().parents[1]
    checked = [
        root / ".env.example",
        root / "README.md",
        root / "docker-compose.example.yml",
        root / "systemd" / "tg-max-bridge.service",
    ]

    for path in checked:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b", text), path
        assert "MAX_CHAT_ID=123456789" not in text or path.name == ".env.example"

    assert not list(root.rglob("session.db"))


def test_run_command_logs_sanitized_exception_without_reraising_raw_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings_factory: Any,
) -> None:
    from tg_max_bridge import cli

    token = "999:fake-private-token"
    settings = settings_factory(telegram_bot_token=token)

    async def fail_service(_settings: Any) -> None:
        raise RuntimeError(f"startup failed with {token}")

    previous_factory = logging.getLogRecordFactory()
    previous_make_record = logging.Logger.makeRecord
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "run_service", fail_service)

    try:
        with pytest.raises(typer.Exit) as exc_info:
            cli.run()
    finally:
        logging.setLogRecordFactory(previous_factory)
        logging.Logger.makeRecord = previous_make_record

    captured = capsys.readouterr().err
    assert exc_info.value.exit_code == 1
    assert token not in captured
    assert "RuntimeError: startup failed with [REDACTED_TELEGRAM_TOKEN]" in captured


def test_discover_telegram_command_logs_sanitized_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings_factory: Any,
) -> None:
    from telegram import User

    from tg_max_bridge import cli

    token = "999:fake-private-token"
    settings = settings_factory(telegram_bot_token=token)

    class FakeBot:
        def __init__(self, *, token: str) -> None:
            assert token == "999:fake-private-token"

        async def __aenter__(self) -> FakeBot:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def get_me(self) -> User:
            return User(
                id=999,
                first_name="Bridge Bot",
                is_bot=True,
                username="maxmcpbot",
            )

        async def get_updates(self, **kwargs: object) -> tuple[()]:
            raise RuntimeError(f"https://api.telegram.org/bot{token}/getUpdates failed")

    previous_factory = logging.getLogRecordFactory()
    previous_make_record = logging.Logger.makeRecord
    monkeypatch.setattr(cli, "TelegramDiscoverySettings", lambda: settings)
    monkeypatch.setattr(cli, "Bot", FakeBot)

    try:
        with pytest.raises(typer.Exit) as exc_info:
            cli.discover_telegram(limit=10)
    finally:
        logging.setLogRecordFactory(previous_factory)
        logging.Logger.makeRecord = previous_make_record

    captured = capsys.readouterr().err
    assert exc_info.value.exit_code == 1
    assert token not in captured
    assert "bot[REDACTED_TELEGRAM_TOKEN]/getUpdates failed" in captured
