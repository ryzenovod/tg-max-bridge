from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_transport_start_uses_configured_stdio_command(
    monkeypatch, settings_factory
):
    from conftest import AsyncContext, FakeCallToolResult, FakeMcpSession

    from tg_max_bridge.max_transport import MaxTransport

    captured = {}
    fake_session = FakeMcpSession(
        {
            "list_chats": FakeCallToolResult(
                structured={"chats": [], "next_marker": None}
            )
        }
    )

    def fake_stdio_client(params):
        captured["params"] = params
        return AsyncContext((object(), object()))

    def fake_client_session(read_stream, write_stream):
        return AsyncContext(fake_session)

    monkeypatch.setattr("tg_max_bridge.max_transport.stdio_client", fake_stdio_client)
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.ClientSession", fake_client_session
    )

    transport = MaxTransport(settings_factory())
    await transport.start()
    await transport.stop()

    assert fake_session.initialized is True
    assert captured["params"].command == "uv"
    assert captured["params"].args == [
        "run",
        "--no-dev",
        "--frozen",
        "--directory",
        "/opt/max-mcp",
        "max-mcp",
    ]
    assert captured["params"].cwd is None
    env = captured["params"].env
    assert env is not None
    assert "TELEGRAM_BOT_TOKEN" not in env


@pytest.mark.asyncio
async def test_transport_subprocess_env_allows_only_specific_uv_vars(
    monkeypatch, settings_factory
):
    from conftest import AsyncContext, FakeCallToolResult, FakeMcpSession

    from tg_max_bridge.max_transport import MaxTransport

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setenv("UV_CACHE_DIR", "/tmp/uv-cache")
    monkeypatch.setenv("UV_TOOL_BIN_DIR", "/tmp/should-not-pass")
    monkeypatch.setenv("MAX_MCP_SESSION_TARB64", "private-session-archive")

    captured = {}
    fake_session = FakeMcpSession(
        {
            "list_chats": FakeCallToolResult(
                structured={"chats": [], "next_marker": None}
            )
        }
    )

    monkeypatch.setattr(
        "tg_max_bridge.max_transport.stdio_client",
        lambda params: (
            captured.setdefault("params", params) and AsyncContext((object(), object()))
        ),
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.ClientSession",
        lambda r, w: AsyncContext(fake_session),
    )

    transport = MaxTransport(settings_factory())
    await transport.start()
    await transport.stop()

    env = captured["params"].env
    assert env["UV_CACHE_DIR"] == "/tmp/uv-cache"
    assert "UV_TOOL_BIN_DIR" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "MAX_MCP_SESSION_TARB64" not in env


@pytest.mark.asyncio
async def test_send_text_parses_structured_message_id(monkeypatch, settings_factory):
    from conftest import AsyncContext, FakeCallToolResult, FakeMcpSession

    from tg_max_bridge.max_transport import MaxTransport

    fake_session = FakeMcpSession(
        {
            "send_message": FakeCallToolResult(
                structured={"message_id": 12345, "ok": True}
            )
        }
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.stdio_client",
        lambda params: AsyncContext((object(), object())),
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.ClientSession",
        lambda r, w: AsyncContext(fake_session),
    )

    transport = MaxTransport(settings_factory())
    await transport.start()
    result = await transport.send_text(777000, "hello [tg:-1/2]")
    await transport.stop()

    assert result.message_id == 12345
    assert result.raw["ok"] is True
    assert fake_session.calls == [
        ("send_message", {"chat_id": 777000, "text": "hello [tg:-1/2]"})
    ]


@pytest.mark.asyncio
async def test_send_text_parses_json_text_content(monkeypatch, settings_factory):
    from conftest import AsyncContext, FakeCallToolResult, FakeMcpSession

    from tg_max_bridge.max_transport import MaxTransport

    fake_session = FakeMcpSession(
        {
            "send_message": FakeCallToolResult(
                text_json={"message": {"id": 54321}, "ok": True}
            )
        }
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.stdio_client",
        lambda params: AsyncContext((object(), object())),
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.ClientSession",
        lambda r, w: AsyncContext(fake_session),
    )

    transport = MaxTransport(settings_factory())
    await transport.start()
    result = await transport.send_text(777000, "hello")
    await transport.stop()

    assert result.message_id == 54321
    assert result.raw["ok"] is True


@pytest.mark.asyncio
async def test_find_marker_searches_with_scan_limit_and_parses_first_match(
    monkeypatch, settings_factory
):
    from conftest import AsyncContext, FakeCallToolResult, FakeMcpSession

    from tg_max_bridge.max_transport import MaxTransport

    fake_session = FakeMcpSession(
        {
            "search_messages": FakeCallToolResult(
                structured={"messages": [{"id": 888, "text": "[tg:-100/123]\ncopied"}]}
            )
        }
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.stdio_client",
        lambda params: AsyncContext((object(), object())),
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.ClientSession",
        lambda r, w: AsyncContext(fake_session),
    )

    transport = MaxTransport(settings_factory())
    await transport.start()
    result = await transport.find_marker(777000, "[tg:-100/123]", scan_limit=500)
    await transport.stop()

    assert result.message_id == 888
    assert fake_session.calls == [
        (
            "search_messages",
            {
                "chat_id": 777000,
                "query": "[tg:-100/123]",
                "limit": 10,
                "scan_limit": 500,
            },
        )
    ]


@pytest.mark.asyncio
async def test_find_marker_rejects_exact_marker_mismatch(monkeypatch, settings_factory):
    from conftest import AsyncContext, FakeCallToolResult, FakeMcpSession

    from tg_max_bridge.max_transport import MaxTransport

    fake_session = FakeMcpSession(
        {
            "search_messages": FakeCallToolResult(
                structured={"messages": [{"id": 889, "text": "copied [tg:-100/1234]"}]}
            )
        }
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.stdio_client",
        lambda params: AsyncContext((object(), object())),
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.ClientSession",
        lambda r, w: AsyncContext(fake_session),
    )

    transport = MaxTransport(settings_factory())
    await transport.start()
    result = await transport.find_marker(777000, "[tg:-100/123]", scan_limit=500)
    await transport.stop()

    assert result is None


@pytest.mark.asyncio
async def test_find_marker_skips_quoted_marker_before_exact_copy(
    monkeypatch, settings_factory
):
    from conftest import AsyncContext, FakeCallToolResult, FakeMcpSession

    from tg_max_bridge.max_transport import MaxTransport

    fake_session = FakeMcpSession(
        {
            "search_messages": FakeCallToolResult(
                structured={
                    "messages": [
                        {"id": 889, "text": "quoted [tg:-100/123]"},
                        {"id": 888, "text": "[tg:-100/123]\nreserve copy"},
                    ]
                }
            )
        }
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.stdio_client",
        lambda params: AsyncContext((object(), object())),
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.ClientSession",
        lambda r, w: AsyncContext(fake_session),
    )

    transport = MaxTransport(settings_factory())
    await transport.start()
    result = await transport.find_marker(777000, "[tg:-100/123]", scan_limit=500)
    await transport.stop()

    assert result is not None
    assert result.message_id == 888


@pytest.mark.asyncio
async def test_send_tool_error_is_delivery_uncertain(monkeypatch, settings_factory):
    from conftest import AsyncContext, FakeCallToolResult, FakeMcpSession

    from tg_max_bridge.max_transport import MaxDeliveryUncertainError, MaxTransport

    fake_session = FakeMcpSession(
        {
            "send_message": FakeCallToolResult(
                is_error=True, text_json={"error": "denied"}
            )
        }
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.stdio_client",
        lambda params: AsyncContext((object(), object())),
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.ClientSession",
        lambda r, w: AsyncContext(fake_session),
    )

    transport = MaxTransport(settings_factory())
    await transport.start()
    with pytest.raises(MaxDeliveryUncertainError):
        await transport.send_text(777000, "hello")
    await transport.stop()


@pytest.mark.asyncio
async def test_start_failure_raises_unavailable(monkeypatch, settings_factory):
    from tg_max_bridge.max_transport import MaxTransport, MaxTransportUnavailableError

    def failing_stdio_client(params):
        raise OSError("missing uv")

    monkeypatch.setattr(
        "tg_max_bridge.max_transport.stdio_client", failing_stdio_client
    )

    transport = MaxTransport(settings_factory())

    with pytest.raises(MaxTransportUnavailableError, match="missing uv"):
        await transport.start()


@pytest.mark.asyncio
async def test_cancelled_start_closes_partially_entered_contexts(
    monkeypatch, settings_factory
):
    from conftest import AsyncContext

    from tg_max_bridge.max_transport import MaxTransport

    initialized = asyncio.Event()

    class BlockingSession:
        async def initialize(self):
            initialized.set()
            await asyncio.Event().wait()

    stdio_context = AsyncContext((object(), object()))
    session_context = AsyncContext(BlockingSession())
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.stdio_client", lambda params: stdio_context
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.ClientSession", lambda r, w: session_context
    )

    task = asyncio.create_task(MaxTransport(settings_factory()).start())
    await initialized.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert session_context.exited is True
    assert stdio_context.exited is True


@pytest.mark.asyncio
async def test_send_call_failure_raises_delivery_uncertain(
    monkeypatch, settings_factory
):
    from conftest import AsyncContext

    from tg_max_bridge.max_transport import MaxDeliveryUncertainError, MaxTransport

    class FailingSession:
        async def initialize(self):
            pass

        async def call_tool(self, name, arguments, **kwargs):
            raise ConnectionError("lost after write")

    monkeypatch.setattr(
        "tg_max_bridge.max_transport.stdio_client",
        lambda params: AsyncContext((object(), object())),
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.ClientSession",
        lambda r, w: AsyncContext(FailingSession()),
    )

    transport = MaxTransport(settings_factory())
    await transport.start()

    with pytest.raises(MaxDeliveryUncertainError, match="lost after write"):
        await transport.send_text(777000, "hello")


@pytest.mark.asyncio
async def test_send_missing_message_id_raises_delivery_uncertain(
    monkeypatch, settings_factory
):
    from conftest import AsyncContext, FakeCallToolResult, FakeMcpSession

    from tg_max_bridge.max_transport import MaxDeliveryUncertainError, MaxTransport

    fake_session = FakeMcpSession(
        {"send_message": FakeCallToolResult(structured={"ok": True})}
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.stdio_client",
        lambda params: AsyncContext((object(), object())),
    )
    monkeypatch.setattr(
        "tg_max_bridge.max_transport.ClientSession",
        lambda r, w: AsyncContext(fake_session),
    )

    transport = MaxTransport(settings_factory())
    await transport.start()

    with pytest.raises(MaxDeliveryUncertainError, match="message id"):
        await transport.send_text(777000, "hello")
