from __future__ import annotations

from pathlib import Path

import pytest


def test_settings_parse_allowed_ids_and_defaults(isolated_env):
    from tg_max_bridge.config import Settings

    settings = Settings(_env_file=None)

    assert settings.telegram_allowed_chat_ids == {-100111222333, -100444555666}
    assert settings.telegram_allowed_user_ids == {42, 99}
    assert settings.telegram_bot_username == "reservebridgebot"
    assert settings.max_chat_id == 777000
    assert settings.max_mcp_directory == Path("/opt/max-mcp")
    assert settings.max_mcp_args() == [
        "run",
        "--no-dev",
        "--frozen",
        "--no-sync",
        "--directory",
        "/opt/max-mcp",
        "max-mcp",
    ]


def test_settings_requires_telegram_token(monkeypatch, isolated_env):
    from pydantic import ValidationError

    from tg_max_bridge.config import Settings

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_rejects_invalid_allowed_id(monkeypatch, isolated_env):
    from pydantic import ValidationError

    from tg_max_bridge.config import Settings

    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42,not-an-int")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "environment_name",
    ["TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_ALLOWED_USER_IDS"],
)
@pytest.mark.parametrize("environment_value", ["", "   ", ", ,"])
def test_settings_rejects_empty_allowed_ids(
    monkeypatch,
    isolated_env,
    environment_name,
    environment_value,
):
    from pydantic import ValidationError

    from tg_max_bridge.config import Settings

    monkeypatch.setenv(environment_name, environment_value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "environment_name",
    ["TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_ALLOWED_USER_IDS"],
)
def test_settings_requires_allowed_ids(monkeypatch, isolated_env, environment_name):
    from pydantic import ValidationError

    from tg_max_bridge.config import Settings

    monkeypatch.delenv(environment_name)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_rejects_invalid_ack_mode(monkeypatch, isolated_env):
    from pydantic import ValidationError

    from tg_max_bridge.config import Settings

    monkeypatch.setenv("TELEGRAM_ACK_MODE", "chatty")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_discovery_settings_only_require_their_own_credentials(
    monkeypatch, isolated_env, tmp_path
):
    from tg_max_bridge.config import (
        MaxDiscoverySettings,
        TelegramDiscoverySettings,
    )

    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    monkeypatch.delenv("MAX_CHAT_ID", raising=False)
    monkeypatch.setenv("MAX_MCP_DIRECTORY", str(tmp_path / "max-mcp"))

    telegram = TelegramDiscoverySettings(_env_file=None)
    maximum = MaxDiscoverySettings(_env_file=None)

    assert telegram.telegram_bot_token.get_secret_value() == "123:test-token"
    assert maximum.max_mcp_directory == tmp_path / "max-mcp"


def test_telegram_discovery_rejects_empty_token(monkeypatch, isolated_env):
    from pydantic import ValidationError

    from tg_max_bridge.config import TelegramDiscoverySettings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")

    with pytest.raises(ValidationError, match="must not be empty"):
        TelegramDiscoverySettings(_env_file=None)


def test_settings_defaults_to_polling_mode(isolated_env):
    from tg_max_bridge.config import Settings

    settings = Settings(_env_file=None)

    assert settings.telegram_mode == "polling"
    assert settings.telegram_webhook_url is None
    assert settings.telegram_webhook_secret is None


def test_polling_mode_ignores_missing_webhook_settings(isolated_env):
    from tg_max_bridge.config import Settings

    settings = Settings(_env_file=None)

    assert settings.telegram_mode == "polling"


def test_webhook_mode_requires_url_and_secret(monkeypatch, isolated_env):
    from pydantic import ValidationError

    from tg_max_bridge.config import Settings

    monkeypatch.setenv("TELEGRAM_MODE", "webhook")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    message = str(exc_info.value)
    assert "TELEGRAM_WEBHOOK_URL" in message
    assert "TELEGRAM_WEBHOOK_SECRET" in message


def test_webhook_mode_accepts_valid_cloud_settings(monkeypatch, isolated_env):
    from tg_max_bridge.config import Settings

    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv(
        "TELEGRAM_WEBHOOK_URL",
        "https://reserve-bridge.containerapps.ru/telegram/webhook",
    )
    monkeypatch.setenv(
        "TELEGRAM_WEBHOOK_SECRET",
        "Az_09-secret.token.with-32-characters",
    )
    monkeypatch.setenv("PORT", "8080")

    settings = Settings(_env_file=None)

    assert settings.telegram_mode == "webhook"
    assert str(settings.telegram_webhook_url) == (
        "https://reserve-bridge.containerapps.ru/telegram/webhook"
    )
    assert settings.telegram_webhook_secret.get_secret_value() == (
        "Az_09-secret.token.with-32-characters"
    )
    assert settings.port == 8080


def test_webhook_direct_receiver_requires_ack_never(monkeypatch, isolated_env):
    from pydantic import ValidationError

    from tg_max_bridge.config import Settings

    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv(
        "TELEGRAM_WEBHOOK_URL",
        "https://reserve-bridge.containerapps.ru/telegram/webhook",
    )
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "valid_secret_with_minimum_length")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_AUTO_REGISTER", "false")
    monkeypatch.setenv("TELEGRAM_ACK_MODE", "errors")

    with pytest.raises(ValidationError, match="TELEGRAM_ACK_MODE=never"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "secret",
    [
        "",
        "too-short-but-allowed",
        "has space",
        "кириллица",
        "slash/not-allowed",
        "x" * 257,
    ],
)
def test_webhook_secret_uses_telegram_allowed_charset(
    monkeypatch, isolated_env, secret
):
    from pydantic import ValidationError

    from tg_max_bridge.config import Settings

    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv(
        "TELEGRAM_WEBHOOK_URL",
        "https://reserve-bridge.containerapps.ru/telegram/webhook",
    )
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", secret)

    with pytest.raises(ValidationError, match="TELEGRAM_WEBHOOK_SECRET"):
        Settings(_env_file=None)


def test_webhook_url_must_be_https(monkeypatch, isolated_env):
    from pydantic import ValidationError

    from tg_max_bridge.config import Settings

    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "http://localhost:8080/hook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "valid_secret_with_minimum_length")

    with pytest.raises(ValidationError, match="TELEGRAM_WEBHOOK_URL"):
        Settings(_env_file=None)
