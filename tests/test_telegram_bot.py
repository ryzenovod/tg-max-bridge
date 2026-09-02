from __future__ import annotations

from types import SimpleNamespace

import pytest


def assert_rejected(result) -> None:
    from tg_max_bridge.models import TelegramSourceMessage

    assert not isinstance(result, TelegramSourceMessage)


def test_extract_trigger_accepts_privacy_mode_reply_command(
    make_update, settings_factory
):
    from tg_max_bridge.models import TelegramSourceMessage
    from tg_max_bridge.telegram_bot import extract_trigger

    result = extract_trigger(
        make_update("/max@ReserveBridgeBot", source_text="Bridge this"),
        settings_factory(),
    )

    assert isinstance(result, TelegramSourceMessage)
    assert result.chat_id == -100111222333
    assert result.message_id == 123
    assert result.trigger_message_id == 456
    assert result.from_user_id == 42
    assert result.text == "Bridge this"


def test_extract_trigger_accepts_plain_max_alias(make_update, settings_factory):
    from tg_max_bridge.models import TelegramSourceMessage
    from tg_max_bridge.telegram_bot import extract_trigger

    result = extract_trigger(
        make_update("/max", source_text="Bridge this"), settings_factory()
    )

    assert isinstance(result, TelegramSourceMessage)


def test_extract_trigger_accepts_reply_caption(make_update, settings_factory):
    from tg_max_bridge.telegram_bot import extract_trigger

    result = extract_trigger(
        make_update("/max", source_text=None, source_caption="Caption matters"),
        settings_factory(),
    )

    assert result.text == "Caption matters"
    assert result.attachment_omitted is True


def test_extract_trigger_rejects_protected_content(make_update, settings_factory):
    from tg_max_bridge.telegram_bot import extract_trigger

    update = make_update("/max", source_text="Do not copy")
    update.effective_message.reply_to_message.has_protected_content = True

    result = extract_trigger(update, settings_factory())

    assert_rejected(result)
    assert result.code == "protected_content"


def test_extract_trigger_attributes_sender_chat_not_trigger_user(
    make_update, settings_factory
):
    from tg_max_bridge.telegram_bot import extract_trigger

    update = make_update("/max", source_text="Anonymous admin notice")
    source = update.effective_message.reply_to_message
    source.sender_chat = SimpleNamespace(
        id=-100111222333,
        title="Operations group",
        username=None,
    )

    result = extract_trigger(update, settings_factory())

    assert result.from_user_id == -100111222333
    assert result.from_display_name == "Operations group"
    assert result.from_user_id != update.effective_message.from_user.id


def test_extract_trigger_keeps_anonymous_admin_signature_with_sender_chat(
    make_update, settings_factory
):
    from tg_max_bridge.telegram_bot import extract_trigger

    update = make_update("/max", source_text="Anonymous admin notice")
    source = update.effective_message.reply_to_message
    source.sender_chat = SimpleNamespace(
        id=-100111222333,
        title="Operations group",
        username=None,
    )
    source.author_signature = "Duty admin"

    result = extract_trigger(update, settings_factory())

    assert result.from_user_id == -100111222333
    assert result.from_display_name == "Duty admin via Operations group"


def test_extract_trigger_attributes_anonymous_signature(make_update, settings_factory):
    from tg_max_bridge.telegram_bot import extract_trigger

    update = make_update("/max", source_text="Signed admin notice")
    source = update.effective_message.reply_to_message
    source.from_user = None
    source.sender_chat = None
    source.author_signature = "Duty admin"

    result = extract_trigger(update, settings_factory())

    assert result.from_user_id == 0
    assert result.from_display_name == "Duty admin (anonymous Telegram author)"
    assert result.from_user_id != update.effective_message.from_user.id


def test_build_application_serializes_updates_and_rejects_command_args(
    monkeypatch, settings_factory
):
    from tg_max_bridge import telegram_bot

    calls = {}

    class FakeTelegramApplication:
        def __init__(self):
            self.handlers = []

        def add_handler(self, handler):
            self.handlers.append(handler)

    application = FakeTelegramApplication()

    class FakeBuilder:
        def token(self, token):
            calls["token"] = token
            return self

        def concurrent_updates(self, enabled):
            calls["concurrent_updates"] = enabled
            return self

        def build(self):
            return application

    class FakeApplicationType:
        @staticmethod
        def builder():
            return FakeBuilder()

    def fake_command_handler(command, callback, **kwargs):
        calls["command"] = command
        calls["callback"] = callback
        calls.update(kwargs)
        return object()

    monkeypatch.setattr(telegram_bot, "Application", FakeApplicationType)
    monkeypatch.setattr(telegram_bot, "CommandHandler", fake_command_handler)

    result = telegram_bot.build_application(settings_factory(), object())

    assert result is application
    assert calls["concurrent_updates"] is False
    assert calls["command"] == "max"
    assert calls["has_args"] is False
    assert len(application.handlers) == 1


@pytest.mark.parametrize(
    ("update_kwargs", "settings_kwargs"),
    [
        ({"reply": False}, {}),
        ({"from_user_id": 666}, {}),
        ({"chat_id": -100999888777}, {}),
        ({"chat_type": "private"}, {}),
        ({"command": "/start"}, {}),
        ({"command": "/max unexpected-argument"}, {}),
        ({"command": "/max@OtherBot"}, {}),
        ({"source_text": None, "source_caption": None}, {}),
        ({}, {"telegram_bot_username": "OtherBot"}),
    ],
)
def test_extract_trigger_rejects_invalid_or_unsupported_updates(
    make_update,
    settings_factory,
    update_kwargs,
    settings_kwargs,
):
    from tg_max_bridge.telegram_bot import extract_trigger

    command = update_kwargs.pop("command", "/max@ReserveBridgeBot")
    result = extract_trigger(
        make_update(command, **update_kwargs), settings_factory(**settings_kwargs)
    )

    assert_rejected(result)
