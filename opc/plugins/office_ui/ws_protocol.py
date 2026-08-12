"""Versioned wire-envelope helpers for the Office UI WebSocket transport."""

from __future__ import annotations

import json
from typing import Any

import aiohttp.web


WS_PROTOCOL_VERSION = 1
SUPPORTED_WS_PROTOCOL_VERSIONS = (WS_PROTOCOL_VERSION,)


class WebSocketProtocolError(ValueError):
    """An inbound frame cannot be handled under the negotiated protocol."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_inbound_envelope(raw: str) -> dict[str, Any] | None:
    """Parse one object frame, accepting legacy unversioned clients."""

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    message_type = data.get("type")
    if not isinstance(message_type, str) or not message_type.strip():
        raise WebSocketProtocolError(
            "invalid_message_type",
            "WebSocket message type must be a non-empty string.",
        )
    data["type"] = message_type.strip()

    version = data.get("protocol_version")
    if version is None:
        return data
    if isinstance(version, bool) or not isinstance(version, int):
        raise WebSocketProtocolError(
            "invalid_protocol_version",
            "WebSocket protocol_version must be an integer.",
        )
    if version not in SUPPORTED_WS_PROTOCOL_VERSIONS:
        raise WebSocketProtocolError(
            "unsupported_protocol_version",
            f"Unsupported WebSocket protocol version {version}.",
        )
    return data


def version_outbound_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Return an envelope carrying the current protocol version."""

    return {**envelope, "protocol_version": WS_PROTOCOL_VERSION}


class VersionedWebSocketResponse(aiohttp.web.WebSocketResponse):
    """aiohttp response that versions direct JSON sends from legacy handlers."""

    async def send_json(
        self,
        data: Any,
        compress: int | None = None,
        *,
        dumps: Any = json.dumps,
    ) -> None:
        if isinstance(data, dict):
            data = version_outbound_envelope(data)
        await super().send_json(data, compress=compress, dumps=dumps)
