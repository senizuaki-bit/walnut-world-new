"""Loopback-only response-loss proxy for the billable INT1 acceptance gate.

This is test infrastructure, not a Provider implementation.  It forwards the
closed ``YAYA_RECOVERABLE_LLM_V1`` surface to the private durable relay.  For
the first accepted dispatch only, it waits until the relay can reread a
terminal generation-count-one resource, then closes the client connection
without an HTTP response.  The first subsequent reconciliation GET is forced
to 503; later GETs are forwarded to the same durable resource.

The process must never inherit an upstream Provider credential.  Its only
secret is the independently generated relay bearer used on both sides of the
proxy.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import socket
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, Protocol
from urllib.parse import urlsplit

HOST: Final = "127.0.0.1"
PROTOCOL: Final = "YAYA_RECOVERABLE_LLM_V1"
CAPABILITIES_PATH: Final = "/v1/llm/capabilities"
DISPATCH_PATH: Final = "/v1/llm/dispatches/"
STATS_PATH: Final = "/__int1_real_provider_fault_proxy__/statistics"
CLASSIFICATION: Final = "REAL_PROVIDER_RESPONSE_LOSS_PROXY_TEST_ONLY"
API_KEY_ENV: Final = "WALNUT_LLM_RELAY_API_KEY"
OPT_IN_ENV: Final = "WALNUT_INT1_REAL_PROVIDER_E2E"
FORBIDDEN_PROVIDER_KEY_ENVS: Final = (
    "WALNUT_LLM_UPSTREAM_API_KEY",
    "WALNUT_LLM_UPSTREAM_API_KEY_FILE",
)
MAX_REQUEST_BYTES: Final = 4_194_304
MAX_UPSTREAM_RESPONSE_BYTES: Final = 8_388_608
UPSTREAM_TIMEOUT_SECONDS: Final = 10.0
TERMINAL_WAIT_SECONDS: Final = 180.0
POLL_SECONDS: Final = 0.1

_DISPATCH_ID = re.compile(r"llmdsp_[a-f0-9]{40}")


@dataclass(frozen=True, slots=True)
class ProxyResponse:
    """Bounded response bytes returned by the private relay or the proxy."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class ProxyTransport(Protocol):
    """Small injectable transport used by the fault state and unit tests."""

    def __call__(self, method: str, path: str, body: bytes | None) -> ProxyResponse: ...


@dataclass(frozen=True, slots=True)
class LoopbackRelayTransport:
    """Closed transport to one private relay on the same host."""

    upstream_port: int
    api_key: str = field(repr=False)
    timeout_seconds: float = UPSTREAM_TIMEOUT_SECONDS
    maximum_response_bytes: int = MAX_UPSTREAM_RESPONSE_BYTES

    def __post_init__(self) -> None:
        _port(self.upstream_port, "upstream_port")
        _secret(self.api_key, API_KEY_ENV)
        if not 0 < self.timeout_seconds <= 30:
            raise ValueError("timeout_seconds is out of bounds")
        if not 1 <= self.maximum_response_bytes <= 16_777_216:
            raise ValueError("maximum_response_bytes is out of bounds")

    def __call__(self, method: str, path: str, body: bytes | None) -> ProxyResponse:
        if method not in {"GET", "PUT"} or not _allowed_upstream_path(method, path):
            raise ValueError("upstream request is outside the closed relay surface")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Connection": "close",
            "Host": f"{HOST}:{self.upstream_port}",
            "X-Yaya-Llm-Protocol": PROTOCOL,
        }
        if body is not None:
            if not body or len(body) > MAX_REQUEST_BYTES:
                raise ValueError("upstream request body is out of bounds")
            headers["Content-Type"] = "application/json"
        connection = HTTPConnection(HOST, self.upstream_port, timeout=self.timeout_seconds)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read(self.maximum_response_bytes + 1)
            if len(response_body) > self.maximum_response_bytes:
                raise OSError("private relay response exceeded the proxy bound")
            selected_headers: dict[str, str] = {}
            for name in ("content-type", "retry-after"):
                value = response.headers.get(name)
                if value is not None:
                    selected_headers[name] = value
            return ProxyResponse(response.status, selected_headers, response_body)
        finally:
            connection.close()


@dataclass(slots=True)
class FaultProxyState:
    """Single-fault state with sanitized, lock-protected evidence."""

    api_key: str = field(repr=False)
    upstream_port: int
    terminal_wait_seconds: float = TERMINAL_WAIT_SECONDS
    poll_seconds: float = POLL_SECONDS
    transport: ProxyTransport | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _fault_finished: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _first_reconcile_finished: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _fault_dispatch_id: str | None = field(default=None, init=False)
    _first_reconcile_reserved: bool = field(default=False, init=False)
    _acknowledgement_drops: int = field(default=0, init=False)
    _reconcile_unavailable_attempted: int = field(default=0, init=False)
    _reconcile_unavailable_delivered: int = field(default=0, init=False)
    _terminal_before_drop: bool = field(default=False, init=False)
    _terminal_state: str | None = field(default=None, init=False)
    _terminal_generation_count: int | None = field(default=None, init=False)
    _recovered_dispatch_id: str | None = field(default=None, init=False)
    _recovered_generation_count: int | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        _secret(self.api_key, API_KEY_ENV)
        _port(self.upstream_port, "upstream_port")
        if not 0 < self.terminal_wait_seconds <= 300:
            raise ValueError("terminal_wait_seconds is out of bounds")
        if not 0 < self.poll_seconds <= 1:
            raise ValueError("poll_seconds is out of bounds")
        if self.transport is None:
            self.transport = LoopbackRelayTransport(self.upstream_port, self.api_key)

    def put(self, dispatch_id: str, body: bytes) -> tuple[ProxyResponse | None, bool]:
        """Forward PUT; return ``drop=True`` only after durable terminal proof."""

        _dispatch_id(dispatch_id)
        if not body or len(body) > MAX_REQUEST_BYTES:
            return _error(413, "REQUEST_TOO_LARGE"), False
        with self._lock:
            is_fault_dispatch = self._fault_dispatch_id is None
            if is_fault_dispatch:
                self._fault_dispatch_id = dispatch_id

        try:
            response = self._request("PUT", f"{DISPATCH_PATH}{dispatch_id}", body)
            if not is_fault_dispatch or response.status not in {200, 201, 202}:
                return response, False
            terminal = self._wait_for_terminal(dispatch_id, response)
            state, generation_count = _terminal_identity(terminal, dispatch_id)
            with self._lock:
                self._terminal_before_drop = True
                self._terminal_state = state
                self._terminal_generation_count = generation_count
                self._acknowledgement_drops = 1
            return None, True
        except (OSError, ValueError):
            return _error(503, "FAULT_PROXY_TERMINAL_WAIT_FAILED"), False
        finally:
            if is_fault_dispatch:
                self._fault_finished.set()

    def get(self, dispatch_id: str) -> tuple[ProxyResponse, bool]:
        """Return a response plus whether it is the one forced unavailable attempt."""

        _dispatch_id(dispatch_id)
        with self._lock:
            fault_dispatch_id = self._fault_dispatch_id
            is_first_reconcile = False
            if dispatch_id == fault_dispatch_id and not self._first_reconcile_reserved:
                self._first_reconcile_reserved = True
                is_first_reconcile = True
        if dispatch_id == fault_dispatch_id:
            fault_finished = self._fault_finished.wait(self.terminal_wait_seconds)
            if not fault_finished:
                if is_first_reconcile:
                    self._first_reconcile_finished.set()
                return _error(503, "FAULT_PROXY_TERMINAL_WAIT_FAILED"), False
            if is_first_reconcile:
                with self._lock:
                    if self._acknowledgement_drops == 1:
                        self._reconcile_unavailable_attempted = 1
                        response = _error(503, "FORCED_RECONCILIATION_UNAVAILABLE")
                    else:
                        response = None
                self._first_reconcile_finished.set()
                if response is not None:
                    return response, True
            else:
                if not self._first_reconcile_finished.wait(self.terminal_wait_seconds):
                    return _error(503, "FAULT_PROXY_RECONCILIATION_WAIT_FAILED"), False

        try:
            response = self._request("GET", f"{DISPATCH_PATH}{dispatch_id}", None)
            if dispatch_id == fault_dispatch_id and response.status in {200, 201}:
                state, generation_count = _terminal_identity(response, dispatch_id)
                del state
                with self._lock:
                    self._recovered_dispatch_id = dispatch_id
                    self._recovered_generation_count = generation_count
            return response, False
        except (OSError, ValueError):
            return _error(503, "PRIVATE_RELAY_UNAVAILABLE"), False

    def record_reconcile_unavailable_delivered(self, dispatch_id: str) -> None:
        """Record the forced 503 only after its bytes were flushed to the client."""

        _dispatch_id(dispatch_id)
        with self._lock:
            if (
                dispatch_id != self._fault_dispatch_id
                or self._reconcile_unavailable_attempted != 1
                or self._reconcile_unavailable_delivered != 0
            ):
                raise ValueError("forced reconciliation delivery is not recordable")
            self._reconcile_unavailable_delivered = 1

    def forward(self, method: str, path: str) -> ProxyResponse:
        """Forward a bodyless closed-surface GET."""

        if method != "GET" or path != CAPABILITIES_PATH:
            return _error(404, "NOT_FOUND")
        try:
            return self._request(method, path, None)
        except (OSError, ValueError):
            return _error(503, "PRIVATE_RELAY_UNAVAILABLE")

    def statistics(self) -> dict[str, object]:
        """Return fault evidence only; never include requests, responses, or secrets."""

        with self._lock:
            recovered_same_dispatch = (
                self._fault_dispatch_id is not None
                and self._recovered_dispatch_id == self._fault_dispatch_id
                and self._recovered_generation_count == 1
            )
            return {
                "schema_version": "1.0.0",
                "classification": CLASSIFICATION,
                "fault_dispatch_id": self._fault_dispatch_id,
                "acknowledgement_drops": self._acknowledgement_drops,
                "reconcile_unavailable_attempted": self._reconcile_unavailable_attempted,
                "reconcile_unavailable_delivered": self._reconcile_unavailable_delivered,
                "terminal_before_drop": self._terminal_before_drop,
                "terminal_state": self._terminal_state,
                "terminal_generation_count": self._terminal_generation_count,
                "recovered_dispatch_id": self._recovered_dispatch_id,
                "recovered_generation_count": self._recovered_generation_count,
                "recovered_same_dispatch": recovered_same_dispatch,
            }

    def _request(self, method: str, path: str, body: bytes | None) -> ProxyResponse:
        transport = self.transport
        if transport is None:
            raise AssertionError("proxy transport disappeared")
        return transport(method, path, body)

    def _wait_for_terminal(
        self,
        dispatch_id: str,
        initial: ProxyResponse,
    ) -> ProxyResponse:
        deadline = time.monotonic() + self.terminal_wait_seconds
        response = initial
        while True:
            state, generation_count = _resource_identity(response, dispatch_id)
            if state in {"SUCCEEDED", "FAILED"}:
                if generation_count != 1:
                    raise ValueError("terminal resource generation_count is not one")
                return response
            if state != "PENDING" or time.monotonic() >= deadline:
                raise OSError("private relay did not reach a bounded terminal state")
            time.sleep(self.poll_seconds)
            response = self._request("GET", f"{DISPATCH_PATH}{dispatch_id}", None)
            if response.status not in {200, 202}:
                raise OSError("private relay terminal poll was unavailable")


class FaultProxyServer(ThreadingHTTPServer):
    """Threaded loopback server that suppresses handler tracebacks."""

    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], state: FaultProxyState) -> None:
        super().__init__(address, FaultProxyHandler)
        self.state = state

    def handle_error(self, request: object, client_address: object) -> None:
        del request, client_address


class FaultProxyHandler(BaseHTTPRequestHandler):
    """Authenticated, closed HTTP facade for the response-loss fault."""

    protocol_version = "HTTP/1.1"
    server_version = "WalnutInt1FaultProxy"
    sys_version = ""

    @property
    def _proxy_server(self) -> FaultProxyServer:
        if not isinstance(self.server, FaultProxyServer):
            raise RuntimeError("fault proxy server type is invalid")
        return self.server

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler hook
        try:
            if not self._authorized():
                self._send(_error(401, "UNAUTHORIZED"))
                return
            path = _closed_path(self.path)
            if path is None:
                self._send(_error(404, "NOT_FOUND"))
            elif path == STATS_PATH:
                self._send(_json_response(200, self._proxy_server.state.statistics()))
            elif path == CAPABILITIES_PATH:
                self._send(self._proxy_server.state.forward("GET", path))
            else:
                self._send_dispatch_get(path[len(DISPATCH_PATH) :])
        except (OSError, ValueError):
            self.close_connection = True

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler hook
        try:
            if not self._authorized():
                self._send(_error(401, "UNAUTHORIZED"))
                return
            path = _closed_path(self.path)
            if path is None or not path.startswith(DISPATCH_PATH):
                self._send(_error(404, "NOT_FOUND"))
                return
            if self.headers.get("transfer-encoding") is not None:
                self._send(_error(400, "CONTENT_LENGTH_REQUIRED"))
                return
            content_types = self.headers.get_all("content-type", [])
            if len(content_types) != 1 or content_types[0].partition(";")[0].strip().lower() != (
                "application/json"
            ):
                self._send(_error(415, "JSON_REQUIRED"))
                return
            lengths = self.headers.get_all("content-length", [])
            if len(lengths) != 1 or not lengths[0].isdecimal():
                self._send(_error(411, "CONTENT_LENGTH_REQUIRED"))
                return
            length = int(lengths[0])
            if not 1 <= length <= MAX_REQUEST_BYTES:
                self._send(_error(413, "REQUEST_TOO_LARGE"))
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self.close_connection = True
                return
            response, drop = self._proxy_server.state.put(path[len(DISPATCH_PATH) :], body)
            if drop:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.close_connection = True
                return
            if response is None:
                raise AssertionError("non-drop proxy response is absent")
            self._send(response)
        except (OSError, ValueError):
            self.close_connection = True

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler hook
        self._send(_error(405, "METHOD_NOT_ALLOWED"))

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler hook
        self._send(_error(405, "METHOD_NOT_ALLOWED"))

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler hook
        self._send(_error(405, "METHOD_NOT_ALLOWED"))

    def _authorized(self) -> bool:
        try:
            client_is_loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False
        return authorized_private_request(
            protocol_values=self.headers.get_all("x-yaya-llm-protocol", []),
            authorization_values=self.headers.get_all("authorization", []),
            host_values=self.headers.get_all("host", []),
            api_key=self._proxy_server.state.api_key,
            client_is_loopback=client_is_loopback,
        )

    def _send(self, response: ProxyResponse) -> None:
        self.send_response(response.status)
        content_type = response.headers.get("content-type", "application/json")
        self.send_header("Content-Type", content_type)
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            self.send_header("Retry-After", retry_after)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response.body)
        self.wfile.flush()
        self.close_connection = True

    def _send_dispatch_get(self, dispatch_id: str) -> None:
        response, forced_unavailable = self._proxy_server.state.get(dispatch_id)
        self._send(response)
        if forced_unavailable:
            self._proxy_server.state.record_reconcile_unavailable_delivered(dispatch_id)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def authorized_private_request(
    *,
    protocol_values: Sequence[str],
    authorization_values: Sequence[str],
    host_values: Sequence[str],
    api_key: str,
    client_is_loopback: bool,
) -> bool:
    """Validate unambiguous client protocol, bearer, host, and network origin."""

    if not client_is_loopback or list(protocol_values) != [PROTOCOL]:
        return False
    if len(authorization_values) != 1 or not hmac.compare_digest(
        authorization_values[0], f"Bearer {api_key}"
    ):
        return False
    if len(host_values) != 1:
        return False
    hostname = urlsplit(f"//{host_values[0]}").hostname
    return hostname is not None and hostname.lower() in {HOST, "localhost", "::1"}


def build_state_from_env(
    upstream_port: int,
    environ: Mapping[str, str] | None = None,
    *,
    transport: ProxyTransport | None = None,
) -> FaultProxyState:
    """Build state only for the explicit live gate and without Provider keys."""

    values = os.environ if environ is None else environ
    if values.get(OPT_IN_ENV) != "true":
        raise ValueError(f"{OPT_IN_ENV}=true is required")
    if any(name in values for name in FORBIDDEN_PROVIDER_KEY_ENVS):
        raise ValueError("fault proxy must not inherit an upstream Provider credential")
    key = values.get(API_KEY_ENV)
    if key is None:
        raise ValueError(f"{API_KEY_ENV} is required")
    return FaultProxyState(key, upstream_port, transport=transport)


def _allowed_upstream_path(method: str, path: str) -> bool:
    if method == "GET" and path == CAPABILITIES_PATH:
        return True
    if not path.startswith(DISPATCH_PATH):
        return False
    dispatch_id = path[len(DISPATCH_PATH) :]
    return _DISPATCH_ID.fullmatch(dispatch_id) is not None and method in {"GET", "PUT"}


def _closed_path(raw_path: str) -> str | None:
    parsed = urlsplit(raw_path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    if parsed.path in {CAPABILITIES_PATH, STATS_PATH}:
        return parsed.path
    if parsed.path.startswith(DISPATCH_PATH):
        dispatch_id = parsed.path[len(DISPATCH_PATH) :]
        if _DISPATCH_ID.fullmatch(dispatch_id) is not None:
            return parsed.path
    return None


def _resource_identity(response: ProxyResponse, dispatch_id: str) -> tuple[str, int]:
    if response.status not in {200, 201, 202}:
        raise ValueError("private relay did not return a dispatch resource")
    content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise ValueError("private relay dispatch response is not JSON")
    value = _strict_object(response.body)
    if value.get("schema_version") != "1.0.0" or value.get("dispatch_id") != dispatch_id:
        raise ValueError("private relay dispatch identity drifted")
    raw_state = value.get("state")
    generation_count = value.get("generation_count")
    if not isinstance(raw_state, str) or raw_state not in {"PENDING", "SUCCEEDED", "FAILED"}:
        raise ValueError("private relay state is invalid")
    state = raw_state
    if isinstance(generation_count, bool) or not isinstance(generation_count, int):
        raise ValueError("private relay generation_count is invalid")
    if not 0 <= generation_count <= 1:
        raise ValueError("private relay generation_count exceeded one")
    if (response.status == 202) != (state == "PENDING"):
        raise ValueError("private relay status/state pair is invalid")
    return state, generation_count


def _terminal_identity(response: ProxyResponse, dispatch_id: str) -> tuple[str, int]:
    state, generation_count = _resource_identity(response, dispatch_id)
    if state not in {"SUCCEEDED", "FAILED"} or generation_count != 1:
        raise ValueError("private relay resource is not terminal generation one")
    return state, generation_count


def _strict_object(body: bytes) -> dict[str, object]:
    if not body or len(body) > MAX_UPSTREAM_RESPONSE_BYTES:
        raise ValueError("JSON body is out of bounds")

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is forbidden: {value}")

    def closed_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON member is forbidden")
            value[key] = item
        return value

    try:
        decoded = body.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            parse_constant=reject_constant,
            object_pairs_hook=closed_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("response is not strict UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("response JSON root is not an object")
    return value


def _json_response(status: int, value: Mapping[str, object]) -> ProxyResponse:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return ProxyResponse(status, {"content-type": "application/json"}, body)


def _error(status: int, code: str) -> ProxyResponse:
    return _json_response(status, {"schema_version": "1.0.0", "code": code})


def _dispatch_id(value: str) -> None:
    if _DISPATCH_ID.fullmatch(value) is None:
        raise ValueError("dispatch identity is invalid")


def _port(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65_535:
        raise ValueError(f"{name} is invalid")


def _secret(value: str, name: str) -> None:
    if not isinstance(value, str) or not 8 <= len(value) <= 4096:
        raise ValueError(f"{name} must be a bounded secret")
    if any(character.isspace() or not character.isprintable() for character in value):
        raise ValueError(f"{name} must be a bounded secret")


def main() -> None:
    parser = argparse.ArgumentParser(description="INT1 loopback response-loss fault proxy")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream-port", type=int, required=True)
    arguments = parser.parse_args()
    _port(arguments.port, "port")
    _port(arguments.upstream_port, "upstream_port")
    if arguments.port == arguments.upstream_port:
        raise ValueError("proxy and private relay ports must differ")
    state = build_state_from_env(arguments.upstream_port)
    server = FaultProxyServer((HOST, arguments.port), state)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
