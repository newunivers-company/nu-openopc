from __future__ import annotations

from types import SimpleNamespace
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from opc.plugins.office_ui.security import (
    UI_AUTH_COOKIE,
    OfficeUISecurity,
    is_loopback_host,
    require_safe_binding,
)
from opc.plugins.office_ui.server import (
    _UI_SECURITY_KEY,
    _authenticate_browser,
    _security_middleware,
)


class OfficeUISecurityPolicyTests(unittest.TestCase):
    def test_loopback_detection_is_explicit(self) -> None:
        for host in ("127.0.0.1", "127.1.2.3", "::1", "localhost"):
            self.assertTrue(is_loopback_host(host), host)
        for host in ("0.0.0.0", "::", "192.168.0.10", "office.internal"):
            self.assertFalse(is_loopback_host(host), host)

    def test_remote_binding_requires_token(self) -> None:
        require_safe_binding("127.0.0.1", None)
        require_safe_binding("0.0.0.0", "secret")
        with self.assertRaisesRegex(ValueError, "non-loopback bind"):
            require_safe_binding("0.0.0.0", None)

    def test_token_comparison_and_same_origin(self) -> None:
        security = OfficeUISecurity.build(auth_token="secret", require_auth=True)
        request = SimpleNamespace(
            headers={"Origin": "http://127.0.0.1:8765"},
            cookies={UI_AUTH_COOKIE: "secret"},
            query={},
            scheme="http",
            host="127.0.0.1:8765",
        )
        self.assertTrue(security.is_authenticated(request))
        self.assertTrue(security.origin_allowed(request))
        request.headers["Origin"] = "https://evil.example"
        self.assertFalse(security.origin_allowed(request))


class OfficeUISecurityMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        app = web.Application(middlewares=[_security_middleware])
        app[_UI_SECURITY_KEY] = OfficeUISecurity.build(
            auth_token="secret",
            require_auth=True,
        )

        async def ok(_request: web.Request) -> web.Response:
            return web.json_response({"ok": True})

        app.router.add_get("/ws", ok)
        app.router.add_get("/api/check", ok)
        app.router.add_get("/auth", _authenticate_browser)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_protected_routes_reject_anonymous_requests(self) -> None:
        response = await self.client.get("/api/check")
        self.assertEqual(response.status, 401)

    async def test_auth_exchange_sets_cookie_and_allows_same_origin(self) -> None:
        response = await self.client.get("/auth?token=secret", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertIn(UI_AUTH_COOKIE, response.cookies)

        origin = str(self.client.make_url("/")).rstrip("/")
        allowed = await self.client.get("/ws", headers={"Origin": origin})
        self.assertEqual(allowed.status, 200)
        self.assertEqual(allowed.headers["X-Frame-Options"], "DENY")

        rejected = await self.client.get(
            "/ws",
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(rejected.status, 403)


if __name__ == "__main__":
    unittest.main()
