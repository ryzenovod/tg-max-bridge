from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

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
        assert "session.db" not in text, path
        assert "MAX_CHAT_ID=123456789" not in text or path.name == ".env.example"
