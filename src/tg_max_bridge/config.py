from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

SETTINGS_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    validate_default=True,
)


def _parse_int_set(value: object) -> set[int]:
    if isinstance(value, set):
        return {int(item) for item in value}
    if isinstance(value, (list, tuple)):
        return {int(item) for item in value}
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
        return {int(item) for item in items}
    raise TypeError("expected comma-separated integers")


class TelegramDiscoverySettings(BaseSettings):
    model_config = SETTINGS_CONFIG

    telegram_bot_token: SecretStr
    log_level: str = "INFO"

    @field_validator("telegram_bot_token")
    @classmethod
    def require_bot_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Telegram bot token must not be empty")
        return value


class MaxDiscoverySettings(BaseSettings):
    model_config = SETTINGS_CONFIG

    max_mcp_directory: Path
    max_mcp_command: str = "uv"
    delivery_timeout_seconds: float = 30.0

    @field_validator("max_mcp_directory")
    @classmethod
    def normalize_max_mcp_directory(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    def max_mcp_args(self) -> list[str]:
        return [
            "run",
            "--no-dev",
            "--frozen",
            "--directory",
            str(self.max_mcp_directory),
            "max-mcp",
        ]


class Settings(MaxDiscoverySettings):
    model_config = SETTINGS_CONFIG

    telegram_bot_token: SecretStr
    telegram_mode: Literal["polling", "webhook"] = "polling"
    telegram_allowed_chat_ids: Annotated[set[int], NoDecode] = Field(
        default_factory=set
    )
    telegram_allowed_user_ids: Annotated[set[int], NoDecode] = Field(
        default_factory=set
    )
    telegram_bot_username: str | None = None
    telegram_webhook_url: str | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_webhook_auto_register: bool = True
    telegram_webhook_listen_host: str = "0.0.0.0"
    port: int = Field(default=8080, alias="PORT")
    telegram_webhook_max_bytes: int = 1_000_000
    max_chat_id: int
    sqlite_path: Path = Path("data/bridge.sqlite3")
    poll_timeout_seconds: int = 30
    delivery_tick_seconds: float = 1.0
    retry_base_seconds: float = 5.0
    retry_max_seconds: float = 300.0
    ambiguous_reconcile_delay_seconds: float = 30.0
    ambiguous_resend_after_seconds: float = 600.0
    max_reconcile_scan_limit: int = 500
    telegram_ack_mode: Literal["never", "errors", "always"] = "errors"
    log_level: str = "INFO"

    @field_validator(
        "telegram_allowed_chat_ids", "telegram_allowed_user_ids", mode="before"
    )
    @classmethod
    def parse_allowed_ids(cls, value: object) -> set[int]:
        return _parse_int_set(value)

    @field_validator("telegram_allowed_chat_ids", "telegram_allowed_user_ids")
    @classmethod
    def require_allowed_ids(cls, value: set[int]) -> set[int]:
        if not value:
            raise ValueError("at least one allowed Telegram ID is required")
        return value

    @field_validator("telegram_bot_token")
    @classmethod
    def require_bot_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Telegram bot token must not be empty")
        return value

    @field_validator("telegram_bot_username")
    @classmethod
    def normalize_bot_username(cls, value: str | None) -> str | None:
        if not value:
            return None
        return value.removeprefix("@").casefold()

    @field_validator("telegram_webhook_url")
    @classmethod
    def validate_webhook_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("TELEGRAM_WEBHOOK_URL must be an HTTPS URL")
        return value

    @field_validator("telegram_webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        allowed = set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
        )
        if not 1 <= len(secret) <= 256:
            raise ValueError("TELEGRAM_WEBHOOK_SECRET must be 1-256 characters")
        if any(character not in allowed for character in secret):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET contains invalid characters")
        return value

    @field_validator("port")
    @classmethod
    def validate_webhook_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        return value

    @field_validator("telegram_webhook_max_bytes")
    @classmethod
    def validate_webhook_max_bytes(cls, value: int) -> int:
        if not 1024 <= value <= 10_000_000:
            raise ValueError("TELEGRAM_WEBHOOK_MAX_BYTES must be 1024-10000000")
        return value

    @model_validator(mode="after")
    def require_webhook_settings(self) -> Settings:
        if self.telegram_mode != "webhook":
            return self
        missing = []
        if self.telegram_webhook_url is None:
            missing.append("TELEGRAM_WEBHOOK_URL")
        if self.telegram_webhook_secret is None:
            missing.append("TELEGRAM_WEBHOOK_SECRET")
        if missing:
            names = " and ".join(missing)
            raise ValueError(f"{names} required in webhook mode")
        if (
            not self.telegram_webhook_auto_register
            and self.telegram_ack_mode != "never"
        ):
            raise ValueError(
                "TELEGRAM_ACK_MODE=never required when "
                "TELEGRAM_WEBHOOK_AUTO_REGISTER=false"
            )
        return self
