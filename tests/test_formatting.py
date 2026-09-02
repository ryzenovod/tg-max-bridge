from __future__ import annotations

from datetime import UTC, datetime

import pytest


def test_build_marker_is_exact_and_stable():
    from tg_max_bridge.formatting import build_marker

    assert build_marker(-100111222333, 123) == "[tg:-100111222333/123]"
    assert build_marker(-100111222333, 123) == build_marker(-100111222333, 123)


def test_build_payload_includes_marker_once_author_time_and_text(sample_source):
    from tg_max_bridge.formatting import build_max_payload

    payload = build_max_payload(sample_source, max_chat_id=777000)

    assert payload.chat_id == 777000
    assert payload.marker == "[tg:-100111222333/123]"
    assert payload.text.count(payload.marker) == 1
    assert "Alice" in payload.text
    assert "2026" in payload.text
    assert "Important meetup point changed to entrance B." in payload.text


def test_build_payload_accepts_caption_text_from_source_model():
    from tg_max_bridge.formatting import (
        ATTACHMENT_OMITTED_NOTICE,
        build_max_payload,
    )
    from tg_max_bridge.models import TelegramSourceMessage

    source = TelegramSourceMessage(
        chat_id=-100111222333,
        message_id=124,
        trigger_message_id=456,
        from_user_id=42,
        from_display_name="Alice",
        text="Photo caption with the important instruction",
        date=datetime(2026, 9, 2, 10, 30, tzinfo=UTC),
        attachment_omitted=True,
    )

    payload = build_max_payload(source, max_chat_id=777000)

    assert "Photo caption with the important instruction" in payload.text
    assert ATTACHMENT_OMITTED_NOTICE in payload.text
    assert payload.marker == "[tg:-100111222333/124]"


def test_build_payload_keeps_marker_for_long_messages(sample_source):
    from dataclasses import replace

    from tg_max_bridge.formatting import (
        MAX_MESSAGE_LENGTH,
        TRUNCATION_NOTICE,
        build_max_payload,
    )

    source = replace(sample_source, text="x" * 9000)

    payload = build_max_payload(source, max_chat_id=777000)

    assert payload.text.count(payload.marker) == 1
    assert "x" * 100 in payload.text
    assert len(payload.text) == MAX_MESSAGE_LENGTH
    assert payload.text.endswith(TRUNCATION_NOTICE)


def test_build_payload_formats_timestamp_in_moscow_time(sample_source):
    from tg_max_bridge.formatting import build_max_payload

    payload = build_max_payload(sample_source, max_chat_id=777000)

    assert "At: 2026-09-02 13:30 MSK" in payload.text


def test_build_payload_rejects_media_only_or_empty_text(sample_source):
    from dataclasses import replace

    from tg_max_bridge.formatting import UnsupportedMessageError, build_max_payload

    with pytest.raises(UnsupportedMessageError):
        build_max_payload(replace(sample_source, text="   "), max_chat_id=777000)
