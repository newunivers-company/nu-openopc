from __future__ import annotations

import asyncio
import signal
import unittest
from unittest.mock import patch

from opc.plugins.office_ui.server import _install_sigterm_handler


class OfficeUISignalHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_requests_normal_async_shutdown(self) -> None:
        stop_event = asyncio.Event()
        registered: dict[str, object] = {}

        class Loop:
            def add_signal_handler(self, sig, callback) -> None:
                registered["signal"] = sig
                registered["callback"] = callback

        with patch("opc.plugins.office_ui.server.asyncio.get_running_loop", return_value=Loop()):
            installed = _install_sigterm_handler(stop_event)

        self.assertTrue(installed)
        self.assertEqual(registered["signal"], signal.SIGTERM)
        self.assertFalse(stop_event.is_set())
        callback = registered["callback"]
        assert callable(callback)
        callback()
        self.assertTrue(stop_event.is_set())

    async def test_unsupported_signal_loop_keeps_portable_fallback(self) -> None:
        stop_event = asyncio.Event()

        class Loop:
            def add_signal_handler(self, sig, callback) -> None:
                raise NotImplementedError

        with patch("opc.plugins.office_ui.server.asyncio.get_running_loop", return_value=Loop()):
            installed = _install_sigterm_handler(stop_event)

        self.assertFalse(installed)
        self.assertFalse(stop_event.is_set())
