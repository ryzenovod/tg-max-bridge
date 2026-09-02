from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

from .models import MaxSendResult


class MaxTransportError(RuntimeError):
    pass


class MaxTransportUnavailableError(MaxTransportError):
    pass


class MaxDeliveryUncertainError(MaxTransportError):
    pass


class MaxTransportSettings(Protocol):
    max_mcp_command: str
    delivery_timeout_seconds: float

    def max_mcp_args(self) -> list[str]: ...


class MaxTransport:
    def __init__(self, settings: MaxTransportSettings) -> None:
        self._settings = settings
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def start(self) -> None:
        if self._session is not None:
            return
        stack = AsyncExitStack()
        params = StdioServerParameters(
            command=self._settings.max_mcp_command,
            args=self._settings.max_mcp_args(),
            env=_subprocess_env(),
        )
        try:
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(params)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
        except asyncio.CancelledError:
            await stack.aclose()
            raise
        except Exception as exc:
            await stack.aclose()
            raise MaxTransportUnavailableError(str(exc)) from exc
        else:
            self._stack = stack
            self._session = session

    async def stop(self) -> None:
        stack = self._stack
        self._stack = None
        self._session = None
        if stack is not None:
            await stack.aclose()

    async def send_text(self, chat_id: int, text: str) -> MaxSendResult:
        raw = await self._call_tool(
            "send_message",
            {"chat_id": chat_id, "text": text},
        )
        try:
            return self._send_result_from_raw(raw)
        except MaxTransportError as exc:
            raise MaxDeliveryUncertainError(str(exc)) from exc

    async def find_marker(
        self,
        chat_id: int,
        marker: str,
        *,
        scan_limit: int,
    ) -> MaxSendResult | None:
        raw = await self._call_tool(
            "search_messages",
            {
                "chat_id": chat_id,
                "query": marker,
                "limit": 10,
                "scan_limit": scan_limit,
            },
        )
        messages = raw.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        for message in messages:
            if not isinstance(message, dict):
                continue
            if str(message.get("text") or "").startswith(f"{marker}\n"):
                return self._send_result_from_raw(message)
        return None

    async def list_chats(
        self,
        *,
        limit: int = 100,
        marker: int | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        args: dict[str, Any] = {"limit": limit}
        if marker is not None:
            args["marker"] = marker
        raw = await self._call_tool("list_chats", args)
        chats = raw.get("chats")
        if not isinstance(chats, list):
            raise MaxTransportError("list_chats returned no chats list")
        typed_chats = [chat for chat in chats if isinstance(chat, dict)]
        next_marker = raw.get("next_marker")
        if next_marker is not None:
            next_marker = int(next_marker)
        return typed_chats, next_marker

    async def get_chat(self, chat_id: int) -> dict[str, Any]:
        return await self._call_tool("get_chat", {"chat_id": chat_id})

    async def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            await self.start()
        if self._session is None:
            raise MaxTransportError("MAX MCP session did not start")
        try:
            result = await self._session.call_tool(
                name,
                args,
                read_timeout_seconds=timedelta(
                    seconds=self._settings.delivery_timeout_seconds
                ),
            )
        except TimeoutError:
            await self.stop()
            raise
        except Exception as exc:
            await self.stop()
            if name == "send_message":
                raise MaxDeliveryUncertainError(str(exc)) from exc
            raise MaxTransportError(str(exc)) from exc
        if result.isError:
            message = _result_text(result) or f"MCP tool failed: {name}"
            if name == "send_message":
                raise MaxDeliveryUncertainError(message)
            raise MaxTransportError(message)
        try:
            raw = _result_json(result)
        except (TypeError, ValueError) as exc:
            if name == "send_message":
                raise MaxDeliveryUncertainError(
                    "send_message returned malformed JSON"
                ) from exc
            raise MaxTransportError(
                f"MCP tool returned malformed JSON: {name}"
            ) from exc
        if not isinstance(raw, dict):
            if name == "send_message":
                raise MaxDeliveryUncertainError(
                    "send_message returned non-object payload"
                )
            raise MaxTransportError(f"MCP tool returned non-object payload: {name}")
        return raw

    @staticmethod
    def _send_result_from_raw(raw: dict[str, Any]) -> MaxSendResult:
        message_id = raw.get("id")
        if message_id is None:
            message_id = raw.get("message_id")
        if message_id is None and isinstance(raw.get("message"), dict):
            message_id = raw["message"].get("id")
        if message_id is None:
            raise MaxTransportError("MAX response has no message id")
        return MaxSendResult(message_id=int(message_id), raw=raw)


def _result_json(result: CallToolResult) -> Any:
    if result.structuredContent is not None:
        return result.structuredContent
    text = _result_text(result)
    if not text:
        return None
    return json.loads(text)


def _result_text(result: CallToolResult) -> str | None:
    for item in result.content:
        if isinstance(item, TextContent):
            return item.text
    return None


def _subprocess_env() -> dict[str, str]:
    prefixes = ("LC_", "MAX_MCP_")
    allowed = {
        "HOME",
        "PATH",
        "LANG",
        "UV_CACHE_DIR",
        "UV_OFFLINE",
        "UV_NO_PROGRESS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NO_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith(prefixes)
    }
