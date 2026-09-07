from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_forward_allowed_updates_covers_messages_and_edits():
    from tg_max_bridge.telegram_bot import FORWARD_ALLOWED_UPDATES

    assert list(FORWARD_ALLOWED_UPDATES) == ["message", "edited_message"]


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


def test_extract_trigger_accepts_inline_command_text(make_update, settings_factory):
    from tg_max_bridge.models import TelegramSourceMessage
    from tg_max_bridge.telegram_bot import extract_trigger

    result = extract_trigger(
        make_update("/max Meet at entrance B", reply=False), settings_factory()
    )

    assert isinstance(result, TelegramSourceMessage)
    assert result.message_id == 456
    assert result.trigger_message_id == 456
    assert result.text == "Meet at entrance B"


def test_extract_trigger_accepts_inline_command_from_any_group_member(
    make_update, settings_factory
):
    from tg_max_bridge.models import TelegramSourceMessage
    from tg_max_bridge.telegram_bot import extract_trigger

    result = extract_trigger(
        make_update(
            "/max Class starts at ten",
            reply=False,
            from_user_id=777,
        ),
        settings_factory(),
    )

    assert isinstance(result, TelegramSourceMessage)
    assert result.from_user_id == 777
    assert result.text == "Class starts at ten"


def test_extract_trigger_rejects_inline_command_from_bot(make_update, settings_factory):
    from tg_max_bridge.telegram_bot import extract_trigger

    update = make_update(
        "/max automated relay",
        reply=False,
        from_user_id=777,
    )
    update.effective_message.from_user.is_bot = True

    result = extract_trigger(update, settings_factory())

    assert_rejected(result)
    assert result.code == "bot_loop"


def test_extract_trigger_accepts_inline_addressed_command_text(
    make_update, settings_factory
):
    from tg_max_bridge.telegram_bot import extract_trigger

    result = extract_trigger(
        make_update("/max@ReserveBridgeBot Meet at entrance B", reply=False),
        settings_factory(),
    )

    assert result.text == "Meet at entrance B"


@pytest.mark.parametrize(
    "command",
    ["/max@OtherBot Meet at entrance B", "/max@ReserveBridgeBot Meet at entrance B"],
)
def test_extract_trigger_rejects_inline_addressed_command_without_configured_username(
    make_update, settings_factory, command
):
    from tg_max_bridge.telegram_bot import extract_trigger

    result = extract_trigger(
        make_update(command, reply=False),
        settings_factory(telegram_bot_username=None),
    )

    assert_rejected(result)
    assert result.code == "wrong_command"


def test_extract_marker_trigger_accepts_group_marker_from_any_user(settings_factory):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.models import TelegramSourceMessage
    from tg_max_bridge.telegram_bot import extract_marker_trigger

    update = fake_update(
        FakeMessage(
            text="Meet at entrance B #max",
            from_user_id=777,
            from_user_name="Class Rep",
            message_id=789,
        )
    )

    result = extract_marker_trigger(update, settings_factory())

    assert isinstance(result, TelegramSourceMessage)
    assert result.chat_id == -100111222333
    assert result.message_id == 789
    assert result.trigger_message_id == 789
    assert result.from_user_id == 777
    assert result.from_display_name == "Class Rep"
    assert result.text == "Meet at entrance B"


def test_extract_marker_trigger_accepts_edited_message_from_telegram_update(
    settings_factory,
):
    from telegram import Update

    from tg_max_bridge.models import TelegramSourceMessage
    from tg_max_bridge.telegram_bot import extract_marker_trigger

    update = Update.de_json(
        {
            "update_id": 1004,
            "edited_message": {
                "message_id": 790,
                "date": 1_788_342_600,
                "edit_date": 1_788_342_660,
                "chat": {"id": -100111222333, "type": "supergroup"},
                "from": {
                    "id": 777,
                    "is_bot": False,
                    "first_name": "Class Rep",
                },
                "text": "Meet at entrance B #max",
            },
        },
        bot=None,
    )

    result = extract_marker_trigger(update, settings_factory())

    assert isinstance(result, TelegramSourceMessage)
    assert result.chat_id == -100111222333
    assert result.message_id == 790
    assert result.trigger_message_id == 790
    assert result.from_user_id == 777
    assert result.from_display_name == "Class Rep"
    assert result.text == "Meet at entrance B"


def test_extract_marker_trigger_accepts_caption_marker(settings_factory):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.telegram_bot import extract_marker_trigger

    result = extract_marker_trigger(
        fake_update(FakeMessage(text=None, caption="#max Updated map")),
        settings_factory(),
    )

    assert result.text == "Updated map"
    assert result.attachment_omitted is True


def test_extract_marker_trigger_is_case_insensitive(settings_factory):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.telegram_bot import extract_marker_trigger

    result = extract_marker_trigger(
        fake_update(FakeMessage(text="Meet at entrance B #MAX")),
        settings_factory(),
    )

    assert result.text == "Meet at entrance B"


def test_extract_marker_trigger_preserves_multiline_and_internal_whitespace(
    settings_factory,
):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.telegram_bot import extract_marker_trigger

    result = extract_marker_trigger(
        fake_update(FakeMessage(text="  Line 1\n#max\nLine   2  ")),
        settings_factory(),
    )

    assert result.text == "Line 1\nLine   2"


def test_extract_marker_trigger_keeps_word_boundary_around_middle_marker(
    settings_factory,
):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.telegram_bot import extract_marker_trigger

    result = extract_marker_trigger(
        fake_update(FakeMessage(text="Meet #max at entrance B")),
        settings_factory(),
    )

    assert result.text == "Meet at entrance B"


def test_extract_marker_trigger_preserves_indented_marker_only_line(settings_factory):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.telegram_bot import extract_marker_trigger

    result = extract_marker_trigger(
        fake_update(FakeMessage(text="Line 1\n  #max\nLine 2")),
        settings_factory(),
    )

    assert result.text == "Line 1\nLine 2"


@pytest.mark.parametrize("text", ["foo#max", "#maximum", "#max."])
def test_extract_marker_trigger_rejects_non_standalone_marker(settings_factory, text):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.telegram_bot import extract_marker_trigger

    result = extract_marker_trigger(
        fake_update(FakeMessage(text=text)),
        settings_factory(),
    )

    assert_rejected(result)
    assert result.code == "missing_marker"


@pytest.mark.parametrize("text", ["#max", "  #max  ", "#max\n#MAX"])
def test_extract_marker_trigger_rejects_blank_body_after_marker_removal(
    settings_factory, text
):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.telegram_bot import extract_marker_trigger

    result = extract_marker_trigger(
        fake_update(FakeMessage(text=text)),
        settings_factory(),
    )

    assert_rejected(result)
    assert result.code == "unsupported_message"


def test_extract_marker_trigger_ignores_messages_without_marker(settings_factory):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.telegram_bot import extract_marker_trigger

    result = extract_marker_trigger(
        fake_update(FakeMessage(text="ordinary message")),
        settings_factory(),
    )

    assert_rejected(result)
    assert result.code == "missing_marker"


def test_extract_marker_trigger_rejects_protected_content(settings_factory):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.telegram_bot import extract_marker_trigger

    result = extract_marker_trigger(
        fake_update(FakeMessage(text="Do not copy #max", has_protected_content=True)),
        settings_factory(),
    )

    assert_rejected(result)
    assert result.code == "protected_content"


@pytest.mark.parametrize(
    ("chat_id", "chat_type", "expected_code"),
    [
        (-100999888777, "supergroup", "unauthorized_chat"),
        (-100111222333, "private", "not_group"),
    ],
)
def test_extract_marker_trigger_rejects_unconfigured_or_private_chats(
    settings_factory, chat_id, chat_type, expected_code
):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.telegram_bot import extract_marker_trigger

    result = extract_marker_trigger(
        fake_update(
            FakeMessage(
                text="Meet at entrance B #max",
                chat_id=chat_id,
                chat_type=chat_type,
            )
        ),
        settings_factory(),
    )

    assert_rejected(result)
    assert result.code == expected_code


def test_extract_marker_trigger_rejects_bot_user_without_sender_chat(
    settings_factory,
):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.telegram_bot import extract_marker_trigger

    result = extract_marker_trigger(
        fake_update(
            FakeMessage(
                text="Meet at entrance B #max",
                from_user_id=888,
                from_user_name="Relay Bot",
                from_user_is_bot=True,
            )
        ),
        settings_factory(),
    )

    assert_rejected(result)
    assert result.code == "bot_loop"


def test_extract_marker_trigger_allows_sender_chat_even_with_bot_user(
    settings_factory,
):
    from conftest import FakeMessage, fake_update

    from tg_max_bridge.telegram_bot import extract_marker_trigger

    result = extract_marker_trigger(
        fake_update(
            FakeMessage(
                text="Anonymous notice #max",
                from_user_id=888,
                from_user_name="Relay Bot",
                from_user_is_bot=True,
                sender_chat=SimpleNamespace(
                    id=-100111222333,
                    title="Operations group",
                    username=None,
                ),
            )
        ),
        settings_factory(),
    )

    assert result.from_user_id == -100111222333
    assert result.from_display_name == "Operations group"
    assert result.text == "Anonymous notice"


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


def test_build_application_serializes_updates_and_accepts_command_args(
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
    assert len(application.handlers) == 2


@pytest.mark.parametrize(
    ("update_kwargs", "settings_kwargs"),
    [
        ({"reply": False}, {}),
        ({"from_user_id": 666}, {}),
        ({"chat_id": -100999888777}, {}),
        ({"chat_type": "private"}, {}),
        ({"command": "/start"}, {}),
        ({"command": "/max@OtherBot"}, {}),
        (
            {"command": "/max@ReserveBridgeBot"},
            {"telegram_bot_username": None},
        ),
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
