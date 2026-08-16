from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from http.client import HTTPConnection
from pathlib import Path
from typing import NamedTuple

import pytest

from walnut_backend import int3_mcp_edge_proxy as proxy

AUTHORIZATION = "Bearer test_header.test_claims.test_signature"
CAPABILITY_PATH = "/mcp/" + "A" * 64
REQUEST_BODY = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'


class _HttpResponse(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, dict[str, str]]] = []

    def __call__(self, body: bytes, headers: Mapping[str, str]) -> proxy.ProxyResponse:
        self.calls.append((body, dict(headers)))
        return proxy.ProxyResponse(
            200,
            {
                "content-type": "application/json",
                "mcp-protocol-version": "2025-06-18",
            },
            b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}',
        )


class StaticAuthorizationIssuer:
    def authorization(self) -> str:
        return AUTHORIZATION


@pytest.fixture
def running_proxy() -> Iterator[tuple[int, RecordingTransport]]:
    transport = RecordingTransport()
    state = proxy.McpEdgeProxyState(
        capability_path=CAPABILITY_PATH,
        authorization_issuer=StaticAuthorizationIssuer(),
        transport=transport,
    )
    server = proxy.McpEdgeProxyServer((proxy.LOOPBACK_HOST, 0), state)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
    )
    thread.start()
    try:
        yield server.server_address[1], transport
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_exact_post_forwards_only_required_headers_and_never_logs_request(
    running_proxy: tuple[int, RecordingTransport],
    capsys: pytest.CaptureFixture[str],
) -> None:
    port, transport = running_proxy
    response = _request(
        port,
        "POST",
        CAPABILITY_PATH,
        REQUEST_BODY,
        {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
            "Origin": "https://aily.feishu.cn",
            "Cookie": "must-not-cross-edge=true",
            "X-Forwarded-For": "203.0.113.1",
        },
    )

    assert response.status == 200
    assert json.loads(response.body)["result"] == {"tools": []}
    assert response.headers["mcp-protocol-version"] == "2025-06-18"
    assert response.headers["cache-control"] == "no-store"
    assert transport.calls == [
        (
            REQUEST_BODY,
            {
                "Authorization": AUTHORIZATION,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )
    ]
    forwarded_headers = transport.calls[0][1]
    assert "Origin" not in forwarded_headers
    assert forwarded_headers["Authorization"] == AUTHORIZATION
    assert forwarded_headers["Content-Type"] == "application/json"
    assert forwarded_headers["Accept"] == "application/json, text/event-stream"
    assert forwarded_headers["MCP-Protocol-Version"] == "2025-06-18"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    "path",
    [
        "/",
        f"{CAPABILITY_PATH}/",
        f"{CAPABILITY_PATH}?debug=true",
        proxy.UPSTREAM_MCP_PATH,
        f"https://example.invalid{CAPABILITY_PATH}",
    ],
)
def test_post_to_every_non_exact_path_is_404_without_forwarding(
    running_proxy: tuple[int, RecordingTransport], path: str
) -> None:
    port, transport = running_proxy

    response = _request(
        port,
        "POST",
        path,
        REQUEST_BODY,
        {"Content-Type": "application/json"},
    )

    assert response.status == 404
    assert json.loads(response.body)["code"] == "NOT_FOUND"
    assert transport.calls == []


@pytest.mark.parametrize(
    "method",
    ["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS", "PROPFIND"],
)
def test_non_post_method_is_405_only_on_exact_path(
    running_proxy: tuple[int, RecordingTransport], method: str
) -> None:
    port, transport = running_proxy

    exact = _request(port, method, CAPABILITY_PATH)
    unrelated = _request(port, method, "/health")

    assert exact.status == 405
    assert exact.headers["allow"] == "POST"
    if method != "HEAD":
        assert json.loads(exact.body)["code"] == "METHOD_NOT_ALLOWED"
    else:
        assert exact.body == b""
    assert unrelated.status == 404
    assert transport.calls == []


def test_request_bounds_and_ambiguous_headers_fail_before_forwarding(
    running_proxy: tuple[int, RecordingTransport],
) -> None:
    port, transport = running_proxy

    oversized = _request(
        port,
        "POST",
        CAPABILITY_PATH,
        b"x" * (proxy.MAX_REQUEST_BYTES + 1),
        {"Content-Type": "application/json"},
    )
    duplicate = _raw_duplicate_authorization_request(port)

    assert oversized.status == 413
    assert json.loads(oversized.body)["code"] == "REQUEST_TOO_LARGE"
    assert duplicate.status == 400
    assert json.loads(duplicate.body)["code"] == "AMBIGUOUS_REQUEST_HEADERS"
    assert transport.calls == []


def test_upstream_failure_is_sanitized_and_contains_no_request_material(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable(body: bytes, headers: Mapping[str, str]) -> proxy.ProxyResponse:
        del body, headers
        raise OSError(f"sensitive failure {AUTHORIZATION}")

    state = proxy.McpEdgeProxyState(
        capability_path=CAPABILITY_PATH,
        authorization_issuer=StaticAuthorizationIssuer(),
        transport=unavailable,
    )
    server = proxy.McpEdgeProxyServer((proxy.LOOPBACK_HOST, 0), state)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        response = _request(
            server.server_address[1],
            "POST",
            CAPABILITY_PATH,
            REQUEST_BODY,
            {"Content-Type": "application/json"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.status == 502
    assert json.loads(response.body) == {
        "schema_version": "1.0.0",
        "code": "BACKEND_UNAVAILABLE",
    }
    assert AUTHORIZATION.encode() not in response.body
    assert REQUEST_BODY not in response.body
    captured = capsys.readouterr()
    assert AUTHORIZATION not in captured.out + captured.err
    assert REQUEST_BODY.decode() not in captured.out + captured.err


def test_binding_and_startup_contract_are_fixed_to_loopback_and_backend_8790() -> None:
    with pytest.raises(ValueError, match="loopback"):
        proxy.McpEdgeProxyServer(
            ("0.0.0.0", 0),
            proxy.McpEdgeProxyState(
                capability_path=CAPABILITY_PATH,
                authorization_issuer=StaticAuthorizationIssuer(),
                transport=RecordingTransport(),
            ),
        )
    with pytest.raises(ValueError, match="differ"):
        proxy._port(proxy.UPSTREAM_PORT)

    repository_root = Path(__file__).resolve().parents[2]
    starter = (repository_root / "scripts" / "run-int3-mcp-edge-proxy.ps1").read_text(
        encoding="utf-8"
    )
    assert "$BackendPort = 8790" in starter
    assert "[int]$Port = 18792" in starter
    assert proxy.DEFAULT_PROXY_PORT == 18792
    assert "$LoopbackAddress = [System.Net.IPAddress]::Loopback" in starter
    assert "TcpClient" in starter
    assert "0.0.0.0" not in starter
    assert "ProtectedData" in starter
    assert "WALNUT_INT3_EDGE_HMAC_SECRET" in starter
    assert "--authorization" not in starter.lower()


def test_client_authorization_is_rejected_and_injected_authorization_cannot_be_overridden(
    running_proxy: tuple[int, RecordingTransport],
) -> None:
    port, transport = running_proxy

    response = _request(
        port,
        "POST",
        CAPABILITY_PATH,
        REQUEST_BODY,
        {"Authorization": "Bearer attacker.value.signature", "Content-Type": "application/json"},
    )

    assert response.status == 400
    assert json.loads(response.body)["code"] == "AMBIGUOUS_REQUEST_HEADERS"
    assert transport.calls == []


def test_teacher_authorization_is_minted_per_request_with_the_closed_read_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecordingTransport()
    issuer = proxy.TeacherAuthorizationIssuer(
        hmac_secret="unit-test-secret",
        issuer="issuer",
        audience="audience",
        tenant_id="tenant_yaya",
        actor_id="teacher_aily",
    )
    state = proxy.McpEdgeProxyState(
        capability_path=CAPABILITY_PATH,
        authorization_issuer=issuer,
        transport=transport,
    )
    server = proxy.McpEdgeProxyServer((proxy.LOOPBACK_HOST, 0), state)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        monkeypatch.setattr(proxy.time, "time", lambda: 1_000.0)
        first = _request(
            server.server_address[1],
            "POST",
            CAPABILITY_PATH,
            REQUEST_BODY,
            {"Content-Type": "application/json"},
        )
        monkeypatch.setattr(proxy.time, "time", lambda: 2_000.0)
        second = _request(
            server.server_address[1],
            "POST",
            CAPABILITY_PATH,
            REQUEST_BODY,
            {"Content-Type": "application/json"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert first.status == 200
    assert second.status == 200
    first_claims = _jwt_claims(transport.calls[0][1]["Authorization"])
    second_claims = _jwt_claims(transport.calls[1][1]["Authorization"])
    assert first_claims == {
        "iss": "issuer",
        "aud": "audience",
        "sub": "teacher_aily",
        "tenant_id": "tenant_yaya",
        "actor_id": "teacher_aily",
        "actor_type": "teacher",
        "roles": ["learner:read", "class-insights:read", "evidence:read"],
        "iat": 1_000,
        "nbf": 1_000,
        "exp": 1_840,
    }
    assert second_claims["iat"] == 2_000
    assert second_claims["exp"] == 2_840


def _request(
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> _HttpResponse:
    connection = HTTPConnection(proxy.LOOPBACK_HOST, port, timeout=2)
    try:
        connection.request(method, path, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        return _HttpResponse(
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


def _raw_duplicate_authorization_request(port: int) -> _HttpResponse:
    connection = HTTPConnection(proxy.LOOPBACK_HOST, port, timeout=2)
    try:
        connection.putrequest("POST", CAPABILITY_PATH)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(REQUEST_BODY)))
        connection.putheader("Authorization", AUTHORIZATION)
        connection.putheader("Authorization", "Bearer second-value")
        connection.endheaders(REQUEST_BODY)
        response = connection.getresponse()
        return _HttpResponse(
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


def _jwt_claims(authorization: str) -> dict[str, object]:
    encoded_claims = authorization.removeprefix("Bearer ").split(".")[1]
    padding = "=" * (-len(encoded_claims) % 4)
    return json.loads(proxy.base64.urlsafe_b64decode(encoded_claims + padding))
