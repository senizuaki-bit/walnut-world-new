"""Loopback-only, path-closed edge proxy for the INT3 Aily demonstration.

This process is deliberately not a second Backend.  It forwards only one
runtime capability path to the authoritative stateless teacher MCP endpoint.
The short-lived teacher Authorization is injected server-side, never accepted
from the public client, and never written to request logs.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, Protocol
from urllib.parse import urlsplit

LOOPBACK_HOST: Final = "127.0.0.1"
UPSTREAM_PORT: Final = 8790
DEFAULT_PROXY_PORT: Final = 18792
UPSTREAM_MCP_PATH: Final = "/integrations/feishu/v1/mcp"
CAPABILITY_PATH_PATTERN: Final = re.compile(r"^/mcp/[A-Za-z0-9_-]{43,128}$")
MAX_REQUEST_BYTES: Final = 64 * 1024
MAX_REJECTED_BODY_DRAIN_BYTES: Final = MAX_REQUEST_BYTES + 1
MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS: Final = 20.0
UPSTREAM_TIMEOUT_SECONDS: Final = 30.0

_FORWARDED_REQUEST_HEADERS: Final = (
    ("content-type", "Content-Type"),
    ("accept", "Accept"),
    ("mcp-protocol-version", "MCP-Protocol-Version"),
)
_FORWARDED_RESPONSE_HEADERS: Final = (
    "content-type",
    "mcp-protocol-version",
    "retry-after",
    "www-authenticate",
)
_MAX_FORWARDED_HEADER_BYTES: Final = 16 * 1024
_AUTHORIZATION_PATTERN: Final = re.compile(
    r"^Bearer [A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"
)
_TEACHER_TOKEN_LIFETIME_SECONDS: Final = 840
_TEACHER_READ_ROLES: Final = ("learner:read", "class-insights:read", "evidence:read")


@dataclass(frozen=True, slots=True)
class ProxyResponse:
    """A bounded response returned by the authoritative Backend."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class ProxyTransport(Protocol):
    """Injectable closed transport used by the proxy and its tests."""

    def __call__(self, body: bytes, headers: Mapping[str, str]) -> ProxyResponse: ...


class AuthorizationIssuer(Protocol):
    """Provides a server-side Authorization value immediately before forwarding."""

    def authorization(self) -> str: ...


@dataclass(frozen=True, slots=True)
class TeacherAuthorizationIssuer:
    """Mints the closed, short-lived teacher JWT inside the loopback edge process."""

    hmac_secret: str = field(repr=False)
    issuer: str
    audience: str
    tenant_id: str
    actor_id: str

    def __post_init__(self) -> None:
        if not all((self.hmac_secret, self.issuer, self.audience, self.tenant_id, self.actor_id)):
            raise ValueError("teacher token issuer is incomplete")

    def authorization(self) -> str:
        issued_at = int(time.time())
        header = _base64url_json({"alg": "HS256", "typ": "JWT"})
        claims = _base64url_json(
            {
                "iss": self.issuer,
                "aud": self.audience,
                "sub": self.actor_id,
                "tenant_id": self.tenant_id,
                "actor_id": self.actor_id,
                "actor_type": "teacher",
                "roles": _TEACHER_READ_ROLES,
                "iat": issued_at,
                "nbf": issued_at,
                "exp": issued_at + _TEACHER_TOKEN_LIFETIME_SECONDS,
            }
        )
        signing_input = f"{header}.{claims}".encode("ascii")
        signature = hmac.new(
            self.hmac_secret.encode("utf-8"), signing_input, hashlib.sha256
        ).digest()
        return f"Bearer {header}.{claims}.{_base64url(signature)}"


@dataclass(frozen=True, slots=True)
class LoopbackBackendTransport:
    """Forward one MCP POST to the fixed loopback Backend listener."""

    timeout_seconds: float = UPSTREAM_TIMEOUT_SECONDS
    maximum_response_bytes: int = MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= 60:
            raise ValueError("timeout_seconds is out of bounds")
        if not 1 <= self.maximum_response_bytes <= 8 * 1024 * 1024:
            raise ValueError("maximum_response_bytes is out of bounds")

    def __call__(self, body: bytes, headers: Mapping[str, str]) -> ProxyResponse:
        if not 1 <= len(body) <= MAX_REQUEST_BYTES:
            raise ValueError("request body is out of bounds")
        upstream_headers = {
            **headers,
            "Connection": "close",
            "Content-Length": str(len(body)),
            "Host": f"{LOOPBACK_HOST}:{UPSTREAM_PORT}",
        }
        connection = HTTPConnection(
            LOOPBACK_HOST,
            UPSTREAM_PORT,
            timeout=self.timeout_seconds,
        )
        try:
            connection.request("POST", UPSTREAM_MCP_PATH, body=body, headers=upstream_headers)
            response = connection.getresponse()
            response_body = response.read(self.maximum_response_bytes + 1)
            if len(response_body) > self.maximum_response_bytes:
                raise OSError("Backend response exceeded the edge bound")
            selected_headers: dict[str, str] = {}
            for name in _FORWARDED_RESPONSE_HEADERS:
                values = response.headers.get_all(name, [])
                if len(values) > 1:
                    raise OSError("Backend returned an ambiguous response header")
                if values:
                    value = values[0]
                    if len(value.encode("utf-8")) > _MAX_FORWARDED_HEADER_BYTES:
                        raise OSError("Backend response header exceeded the edge bound")
                    selected_headers[name] = value
            return ProxyResponse(response.status, selected_headers, response_body)
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class McpEdgeProxyState:
    """Closed runtime state for one revocable capability and token issuer."""

    capability_path: str = field(repr=False)
    authorization_issuer: AuthorizationIssuer = field(repr=False)
    transport: ProxyTransport = field(default_factory=LoopbackBackendTransport, repr=False)

    def __post_init__(self) -> None:
        if CAPABILITY_PATH_PATTERN.fullmatch(self.capability_path) is None:
            raise ValueError("capability path is invalid")

    def accepts_path(self, raw_path: str) -> bool:
        return _closed_capability_path(raw_path, self.capability_path) is not None

    def forward(self, body: bytes, headers: Mapping[str, str]) -> ProxyResponse:
        if any(name.lower() == "authorization" for name in headers):
            raise ValueError("client Authorization cannot cross the edge")
        return self.transport(
            body,
            {**headers, "Authorization": self.authorization_issuer.authorization()},
        )


class McpEdgeProxyServer(ThreadingHTTPServer):
    """Threaded loopback server that never emits request tracebacks."""

    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], state: McpEdgeProxyState) -> None:
        if address[0] != LOOPBACK_HOST:
            raise ValueError("INT3 MCP edge proxy must bind to IPv4 loopback")
        super().__init__(address, McpEdgeProxyHandler)
        self.state = state

    def handle_error(self, request: object, client_address: object) -> None:
        del request, client_address


class McpEdgeProxyHandler(BaseHTTPRequestHandler):
    """Expose exactly one POST route and suppress all access logging."""

    protocol_version = "HTTP/1.1"
    server_version = "WalnutInt3McpEdge"
    sys_version = ""

    @property
    def _edge_server(self) -> McpEdgeProxyServer:
        if not isinstance(self.server, McpEdgeProxyServer):
            raise RuntimeError("MCP edge server type is invalid")
        return self.server

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler hook
        if not self._client_is_loopback():
            self._discard_bounded_request_body()
            self._send(_error(403, "LOOPBACK_REQUIRED"))
            return
        if not self._edge_server.state.accepts_path(self.path):
            self._discard_bounded_request_body()
            self._send(_error(404, "NOT_FOUND"))
            return
        if self.headers.get_all("transfer-encoding", []):
            self._send(_error(400, "CONTENT_LENGTH_REQUIRED"))
            return
        lengths = self.headers.get_all("content-length", [])
        if not lengths:
            self._send(_error(411, "CONTENT_LENGTH_REQUIRED"))
            return
        if len(lengths) != 1 or not lengths[0].isdecimal():
            self._send(_error(400, "INVALID_CONTENT_LENGTH"))
            return
        length = int(lengths[0])
        if length < 1:
            self._send(_error(400, "INVALID_CONTENT_LENGTH"))
            return
        if length > MAX_REQUEST_BYTES:
            if length <= MAX_REJECTED_BODY_DRAIN_BYTES:
                self.rfile.read(length)
            self._send(_error(413, "REQUEST_TOO_LARGE"))
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                self._send(_error(400, "INCOMPLETE_REQUEST_BODY"))
                return
            try:
                headers = self._selected_request_headers()
            except ValueError:
                self._send(_error(400, "AMBIGUOUS_REQUEST_HEADERS"))
                return
            response = self._edge_server.state.forward(body, headers)
            self._send(response)
        except (HTTPException, OSError, ValueError):
            try:
                self._send(_error(502, "BACKEND_UNAVAILABLE"))
            except OSError:
                self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler hook
        self._deny_method()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler hook
        self._deny_method(send_body=False)

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler hook
        self._deny_method()

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler hook
        self._deny_method()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler hook
        self._deny_method()

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler hook
        self._deny_method()

    def do_CONNECT(self) -> None:  # noqa: N802 - stdlib handler hook
        self._deny_method()

    def do_TRACE(self) -> None:  # noqa: N802 - stdlib handler hook
        self._deny_method()

    def __getattr__(self, name: str) -> object:
        """Map uncommon HTTP verbs to the same closed 404/405 surface."""

        if name.startswith("do_") and len(name) > 3:
            return self._deny_method
        raise AttributeError(name)

    def _deny_method(self, *, send_body: bool = True) -> None:
        if not self._edge_server.state.accepts_path(self.path):
            response = _error(404, "NOT_FOUND")
        else:
            response = ProxyResponse(
                405,
                {"allow": "POST", "content-type": "application/json"},
                _json_bytes({"schema_version": "1.0.0", "code": "METHOD_NOT_ALLOWED"}),
            )
        self._send(response, send_body=send_body)

    def _selected_request_headers(self) -> dict[str, str]:
        if self.headers.get_all("authorization", []):
            raise ValueError("client Authorization is forbidden")
        selected: dict[str, str] = {}
        total_bytes = 0
        for source_name, target_name in _FORWARDED_REQUEST_HEADERS:
            values = self.headers.get_all(source_name, [])
            if len(values) > 1:
                raise ValueError("ambiguous forwarded request header")
            if values:
                value = values[0]
                total_bytes += len(source_name.encode("ascii")) + len(value.encode("utf-8"))
                if total_bytes > _MAX_FORWARDED_HEADER_BYTES:
                    raise ValueError("forwarded request headers exceeded the edge bound")
                selected[target_name] = value
        return selected

    def _discard_bounded_request_body(self) -> None:
        """Drain only a small, unambiguous body so Windows can return the denial."""

        if self.headers.get_all("transfer-encoding", []):
            return
        lengths = self.headers.get_all("content-length", [])
        if len(lengths) != 1 or not lengths[0].isdecimal():
            return
        length = int(lengths[0])
        if 1 <= length <= MAX_REQUEST_BYTES:
            self.rfile.read(length)

    def _client_is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _send(self, response: ProxyResponse, *, send_body: bool = True) -> None:
        self.send_response(response.status)
        for name in _FORWARDED_RESPONSE_HEADERS:
            value = response.headers.get(name)
            if value is not None:
                self.send_header(name, value)
        allow = response.headers.get("allow")
        if allow is not None:
            self.send_header("Allow", allow)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if send_body:
            self.wfile.write(response.body)
            self.wfile.flush()
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _closed_capability_path(raw_path: str, capability_path: str) -> str | None:
    parsed = urlsplit(raw_path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    return capability_path if parsed.path == capability_path else None


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_json(value: Mapping[str, object]) -> str:
    return _base64url(_json_bytes(value))


def _error(status: int, code: str) -> ProxyResponse:
    return ProxyResponse(
        status,
        {"content-type": "application/json"},
        _json_bytes({"schema_version": "1.0.0", "code": code}),
    )


def _port(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65_535:
        raise ValueError("proxy port is invalid")
    if value == UPSTREAM_PORT:
        raise ValueError("proxy port must differ from the fixed Backend port")


def _required_secret_environment(name: str) -> str:
    value = os.environ.pop(name, None)
    if value is None or not value:
        raise ValueError(f"{name} is required")
    return value


def _runtime_state_from_environment() -> McpEdgeProxyState:
    capability_path = _required_secret_environment("WALNUT_INT3_EDGE_CAPABILITY_PATH")
    return McpEdgeProxyState(
        capability_path=capability_path,
        authorization_issuer=TeacherAuthorizationIssuer(
            hmac_secret=_required_secret_environment("WALNUT_INT3_EDGE_HMAC_SECRET"),
            issuer=_required_secret_environment("WALNUT_INT3_EDGE_ISSUER"),
            audience=_required_secret_environment("WALNUT_INT3_EDGE_AUDIENCE"),
            tenant_id=_required_secret_environment("WALNUT_INT3_EDGE_TENANT_ID"),
            actor_id=_required_secret_environment("WALNUT_INT3_EDGE_ACTOR_ID"),
        ),
    )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="INT3 loopback-only MCP edge proxy")
    parser.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    parsed = parser.parse_args(arguments)
    _port(parsed.port)
    state = _runtime_state_from_environment()
    server = McpEdgeProxyServer((LOOPBACK_HOST, parsed.port), state)
    print(
        f"INT3 MCP edge listening on loopback port {parsed.port}; "
        f"forwarding only to {UPSTREAM_MCP_PATH}; teacher credentials are issued per request",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the wrapper
    raise SystemExit(main())
