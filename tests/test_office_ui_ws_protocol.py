import asyncio
import json

import pytest

from opc.plugins.office_ui.ws_protocol import (
    WS_PROTOCOL_VERSION,
    WebSocketProtocolError,
    parse_inbound_envelope,
    version_outbound_envelope,
)


def test_legacy_and_current_protocol_frames_are_accepted() -> None:
    assert parse_inbound_envelope('{"type":"ping"}') == {"type": "ping"}
    assert parse_inbound_envelope(
        json.dumps({"type": "ping", "protocol_version": WS_PROTOCOL_VERSION})
    ) == {"type": "ping", "protocol_version": WS_PROTOCOL_VERSION}


def test_future_protocol_version_fails_closed() -> None:
    with pytest.raises(WebSocketProtocolError, match="Unsupported") as error:
        parse_inbound_envelope('{"type":"ping","protocol_version":99}')

    assert error.value.code == "unsupported_protocol_version"


def test_outbound_envelope_uses_authoritative_current_version() -> None:
    assert version_outbound_envelope(
        {"type": "ack", "payload": {}, "protocol_version": 99}
    )["protocol_version"] == WS_PROTOCOL_VERSION


def test_handler_rejects_unsupported_protocol_without_dispatch() -> None:
    from opc.plugins.office_ui.ws_handler import WSHandler

    sent: list[dict] = []

    class Socket:
        closed = False
        closing = False

        async def send_json(self, payload: dict) -> None:
            sent.append(payload)

    handler = object.__new__(WSHandler)
    handler._shutting_down = False
    handler._clients = set()

    asyncio.run(
        handler._route_message(
            Socket(),
            '{"type":"ping","protocol_version":99}',
        )
    )

    assert sent[0]["type"] == "ack"
    assert sent[0]["protocol_version"] == WS_PROTOCOL_VERSION
    assert sent[0]["payload"]["code"] == "unsupported_protocol_version"
