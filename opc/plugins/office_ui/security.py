"""Security boundary for the Office UI HTTP and WebSocket surfaces."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import ipaddress
import os
from typing import Iterable
from urllib.parse import urlsplit


UI_AUTH_TOKEN_ENV = "OPC_UI_AUTH_TOKEN"
UI_AUTH_COOKIE = "opc_ui_session"


def is_loopback_host(host: str) -> bool:
    """Return whether a bind host is restricted to the local machine."""

    normalized = str(host or "").strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def resolve_auth_token(explicit: str | None = None) -> str:
    """Resolve an Office UI token without ever logging or serializing it."""

    return str(explicit or os.environ.get(UI_AUTH_TOKEN_ENV) or "").strip()


def require_safe_binding(host: str, auth_token: str | None) -> None:
    """Reject remotely reachable bindings that do not have authentication."""

    if is_loopback_host(host):
        return
    if not resolve_auth_token(auth_token):
        raise ValueError(
            "Office UI refuses a non-loopback bind without authentication. "
            f"Set {UI_AUTH_TOKEN_ENV} or pass --auth-token, or bind to 127.0.0.1."
        )


def normalize_allowed_origins(origins: Iterable[str] | None) -> frozenset[str]:
    normalized: set[str] = set()
    for value in origins or ():
        candidate = _normalized_origin(value)
        if candidate:
            normalized.add(candidate)
    return frozenset(normalized)


def _normalized_origin(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw or raw.lower() == "null":
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


@dataclass(frozen=True)
class OfficeUISecurity:
    """Immutable request-security policy shared by HTTP and WebSocket routes."""

    auth_token: str = ""
    require_auth: bool = False
    allowed_origins: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls,
        *,
        auth_token: str | None = None,
        require_auth: bool = False,
        allowed_origins: Iterable[str] | None = None,
    ) -> "OfficeUISecurity":
        token = resolve_auth_token(auth_token)
        if require_auth and not token:
            raise ValueError("Office UI authentication is required but no token was configured")
        return cls(
            auth_token=token,
            require_auth=bool(require_auth),
            allowed_origins=normalize_allowed_origins(allowed_origins),
        )

    def token_matches(self, candidate: str | None) -> bool:
        if not self.auth_token:
            return not self.require_auth
        value = str(candidate or "")
        return bool(value) and hmac.compare_digest(value, self.auth_token)

    def request_token(self, request: object) -> str:
        headers = getattr(request, "headers", {})
        authorization = str(headers.get("Authorization", "") or "")
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        header_token = str(headers.get("X-OPC-UI-Token", "") or "").strip()
        if header_token:
            return header_token
        cookies = getattr(request, "cookies", {})
        cookie_token = str(cookies.get(UI_AUTH_COOKIE, "") or "").strip()
        if cookie_token:
            return cookie_token
        query = getattr(request, "query", {})
        return str(query.get("token", "") or "").strip()

    def is_authenticated(self, request: object) -> bool:
        if not self.require_auth and not self.auth_token:
            return True
        return self.token_matches(self.request_token(request))

    def origin_allowed(self, request: object) -> bool:
        headers = getattr(request, "headers", {})
        raw_origin = str(headers.get("Origin", "") or "").strip()
        if not raw_origin:
            # Non-browser clients do not send Origin. Authentication still applies.
            return True
        origin = _normalized_origin(raw_origin)
        if not origin:
            return False
        if origin in self.allowed_origins:
            return True
        scheme = str(getattr(request, "scheme", "http") or "http").lower()
        host = str(getattr(request, "host", "") or "").lower()
        return origin == f"{scheme}://{host}"
