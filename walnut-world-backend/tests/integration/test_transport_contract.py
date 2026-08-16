"""Transport behavior verified against the current Agent contract workspace."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from walnut_backend.api.app import create_app
from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.errors import TransportError, error_response
from walnut_backend.api.response_validation import canonical_payload, contract_response
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]

ATTEMPT_HEADERS = {
    "X-Request-Id": "req_transport_0001",
    "X-Trace-Id": "trace_transport_0001",
    "X-Correlation-Id": "corr_transport_0001",
    "X-Schema-Version": "1.0.0",
}
AUTHORIZATION = "Bearer tenant_yaya:student_actor"
STREAM_PROTOCOL_VERSION = "1.0.0"
RUNTIME_SUBPROTOCOL = "yaya.runtime.v1"


def test_int1_excluded_public_routes_require_explicit_opt_in() -> None:
    def mounted_paths(app: FastAPI) -> set[str]:
        paths: set[str] = set()
        for route in app.routes:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.add(path)
            nested = getattr(route, "original_router", None)
            for child in getattr(nested, "routes", ()):
                child_path = getattr(child, "path", None)
                if isinstance(child_path, str):
                    paths.add(child_path)
        return paths

    base = Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH)
    default_paths = mounted_paths(create_app(base))
    assert "/v1/client-events:batch" not in default_paths
    assert "/v1/realtime" not in default_paths

    enabled = create_app(
        replace(
            base,
            client_event_batch_enabled=True,
            realtime_wss_enabled=True,
            public_realtime_url="wss://gateway.example/v1/realtime",
        )
    )
    enabled_paths = mounted_paths(enabled)
    assert "/v1/client-events:batch" in enabled_paths
    assert "/v1/realtime" in enabled_paths


def app_for_transport_test(settings: Settings | None = None) -> FastAPI:
    """Install a test-only handler over the production transport gateway."""
    app = create_app(settings or Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH))

    @app.post("/_transport-test/actors/{actor_id}")
    async def actor_response(actor_id: str, request: Request):
        context = get_operation_context(request)
        await request.json()
        if request.query_params.get("force_not_found") == "true":
            return error_response(TransportError("NOT_FOUND", "TEST"), context, request.app.state.error_catalog)
        if request.query_params.get("force_unknown_error") == "true":
            return error_response(TransportError("NOT_IN_LOCKED_CATALOG", "TEST"), context, request.app.state.error_catalog)
        if request.query_params.get("force_unknown_commit") == "true":
            return error_response(
                TransportError("UNKNOWN_COMMIT_STATE", "WORLD_COMMIT"),
                context,
                request.app.state.error_catalog,
            )
        response_headers = (
            {"X-Request-Id": "req_wrong_response"}
            if request.query_params.get("break_response_header") == "true"
            else None
        )
        actor_type = "teacher" if request.query_params.get("break_semantic") == "true" else context.actor.actor_type
        return contract_response(
            request=request,
            payload={
                "tenant_id": context.actor.tenant_id,
                "actor_id": context.actor.actor_id,
                "actor_type": actor_type,
                "roles": list(context.actor.roles),
            },
            schema_path="contracts/schemas/common/actor-ref.schema.json",
            resource_identity={"actor_id": actor_id},
            headers=response_headers,
        )

    @app.websocket("/_transport-test/ws-context")
    async def websocket_context(websocket: WebSocket) -> None:
        context = get_operation_context(websocket)
        await websocket.accept()
        await websocket.send_json(
            {
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                "correlation_id": context.correlation_id,
                "actor_id": context.actor.actor_id,
                "schema_version": context.schema_version,
                "content_unit_id": context.content_ref.unit_id,
                "content_version": context.content_ref.version,
                "content_hash": context.content_ref.content_hash,
                "command_id": context.command_id,
                "causation_id": context.causation_id,
                "deadline_at": context.deadline_at.isoformat() if context.deadline_at else None,
            }
        )
        await websocket.close()

    return app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app_for_transport_test()) as test_client:
        yield test_client


def request_headers(**overrides: str) -> dict[str, str]:
    """Create a hand-derived, contract-valid request attempt."""
    return {**ATTEMPT_HEADERS, "Authorization": AUTHORIZATION, **overrides}


def websocket_headers(**overrides: str) -> dict[str, str]:
    """Use the runtime-event AsyncAPI handshake values locked in the release."""
    return {
        **request_headers(),
        "X-Stream-Protocol-Version": STREAM_PROTOCOL_VERSION,
        **overrides,
    }


def production_settings() -> Settings:
    return Settings(
        database_url="postgresql://test/walnut",
        contract_path=DEFAULT_CONTRACT_PATH,
        sandbox_url="http://127.0.0.1:8791",
        llm_url="http://127.0.0.1:8792",
        feishu_url="http://127.0.0.1:8793",
        request_timeout_seconds=30.0,
        development_auth_enabled=False,
        auth_hmac_secret="transport-production-secret-" + "s" * 40,
        feishu_pseudonym_secret="transport-feishu-pseudonym-secret-" + "s" * 40,
        auth_issuer="https://identity.walnut.local",
        auth_audience="walnut-game-api",
    )


def production_token(settings: Settings, *, actor_id: str = "student_jwt_actor") -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": settings.auth_issuer,
        "aud": settings.auth_audience,
        "sub": actor_id,
        "tenant_id": "tenant_jwt",
        "actor_id": actor_id,
        "actor_type": "student",
        "roles": ["game:player"],
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }

    def encode(value: object) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).rstrip(b"=").decode("ascii")

    header, body = encode({"alg": "HS256", "typ": "JWT"}), encode(claims)
    signature = hmac.new(
        settings.auth_hmac_secret.encode("utf-8"),
        f"{header}.{body}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"Bearer {header}.{body}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"


def assert_attempt_headers(response: object) -> None:
    """Every transport response identifies this HTTP attempt, including errors."""
    headers = response.headers
    assert headers["x-request-id"] == ATTEMPT_HEADERS["X-Request-Id"]
    assert headers["x-trace-id"] == ATTEMPT_HEADERS["X-Trace-Id"]
    assert headers["x-correlation-id"] == ATTEMPT_HEADERS["X-Correlation-Id"]


@pytest.mark.parametrize("authorization", [None, "Bearer malformed token"])
def test_rejects_missing_or_invalid_bearer_with_catalog_error(
    client: TestClient, authorization: str | None
) -> None:
    """Catches an authentication middleware branch that admits unauthenticated requests."""
    headers = request_headers()
    if authorization is None:
        del headers["Authorization"]
    else:
        headers["Authorization"] = authorization

    response = client.post("/_transport-test/actors/student_actor", headers=headers, json={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert_attempt_headers(response)


@pytest.mark.parametrize(
    "header_name",
    ["X-Request-Id", "X-Trace-Id", "X-Correlation-Id", "X-Schema-Version"],
)
def test_rejects_each_missing_required_attempt_header(
    client: TestClient, header_name: str
) -> None:
    """Catches handlers that run without one part of the mandated attempt identity."""
    headers = request_headers()
    del headers[header_name]

    response = client.post("/_transport-test/actors/student_actor", headers=headers, json={})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert response.headers["x-request-id"].startswith("req_")
    assert response.headers["x-trace-id"].startswith("trace_")
    assert response.headers["x-correlation-id"].startswith("corr_")


def test_rejects_an_unknown_schema_version(client: TestClient) -> None:
    """Catches coercing an unsupported client schema to the current wire contract."""
    response = client.post(
        "/_transport-test/actors/student_actor",
        headers=request_headers(**{"X-Schema-Version": "9.9.9"}),
        json={},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCHEMA_VERSION_UNSUPPORTED"
    assert_attempt_headers(response)


def test_maps_a_later_catalog_error_without_a_transport_code_change(client: TestClient) -> None:
    """Catches a hard-coded error map that cannot represent an error produced by a later port."""
    response = client.post(
        "/_transport-test/actors/student_actor?force_not_found=true",
        headers=request_headers(),
        json={},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert_attempt_headers(response)


def test_unknown_error_code_becomes_valid_internal_error(client: TestClient) -> None:
    """Catches an error constructor that leaks a KeyError for a non-catalog code."""
    response = client.post(
        "/_transport-test/actors/student_actor?force_unknown_error=true",
        headers=request_headers(),
        json={},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert_attempt_headers(response)


def test_unknown_commit_state_is_schema_valid_and_reconcilable(client: TestClient) -> None:
    """Catches an UNKNOWN_COMMIT_STATE body without its required UNKNOWN status and command link."""
    response = client.post(
        "/_transport-test/actors/student_actor?force_unknown_commit=true",
        headers=request_headers(),
        json={},
    )

    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "UNKNOWN"
    assert body["command_id"].startswith("cmd_")
    assert response.headers["location"] == f"/v1/commands/{body['command_id']}"
    assert_attempt_headers(response)


def test_replaces_a_response_with_wrong_attempt_header_by_contract_error(client: TestClient) -> None:
    """Catches a response gateway that would send a body after response-header validation fails."""
    response = client.post(
        "/_transport-test/actors/student_actor?break_response_header=true",
        headers=request_headers(),
        json={},
    )

    assert response.status_code == 500
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["data"] is None
    assert body["error"]["code"] == "INVARIANT_VIOLATION"
    assert_attempt_headers(response)


def test_replaces_schema_valid_response_that_breaks_authenticated_actor_invariant(
    client: TestClient,
) -> None:
    """Catches a gateway that checks JSON shape but not the explicit actor semantic invariant."""
    response = client.post(
        "/_transport-test/actors/student_actor?break_semantic=true",
        headers=request_headers(),
        json={},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INVARIANT_VIOLATION"
    assert_attempt_headers(response)


def test_replaces_path_and_body_identity_mismatch_by_contract_error(client: TestClient) -> None:
    """Catches a resource response whose canonical body identity differs from the path identity."""
    response = client.post(
        "/_transport-test/actors/student_other",
        headers=request_headers(),
        json={},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INVARIANT_VIOLATION"
    assert_attempt_headers(response)


def test_authenticated_identity_cannot_be_overridden_by_request_body(client: TestClient) -> None:
    """Catches a transport that trusts tenant or actor fields supplied by the request body."""
    response = client.post(
        "/_transport-test/actors/student_actor",
        headers=request_headers(),
        json={"tenant_id": "evil_tenant", "actor_id": "operator_attack", "roles": ["operator"]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "tenant_yaya",
        "actor_id": "student_actor",
        "actor_type": "student",
        "roles": ["game:player"],
    }
    assert_attempt_headers(response)


def test_production_jwt_derives_identity_and_rejects_local_mock_tokens() -> None:
    """Production accepts only Agent-compatible signed HS256 JWTs."""
    settings = production_settings()
    with TestClient(app_for_transport_test(settings)) as production_client:
        accepted = production_client.post(
            "/_transport-test/actors/student_jwt_actor",
            headers={**ATTEMPT_HEADERS, "Authorization": production_token(settings)},
            json={"actor_id": "attacker"},
        )
        denied = production_client.post(
            "/_transport-test/actors/student_actor",
            headers={**ATTEMPT_HEADERS, "Authorization": AUTHORIZATION},
            json={},
        )

    assert accepted.status_code == 200
    assert accepted.json() == {
        "tenant_id": "tenant_jwt",
        "actor_id": "student_jwt_actor",
        "actor_type": "student",
        "roles": ["game:player"],
    }
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_canonical_response_uses_locked_byte_representation_and_rejects_fractional_numbers(
    client: TestClient,
) -> None:
    """Catches a gateway that computes canonical JSON but sends a framework-reserialized body."""
    response = client.post(
        "/_transport-test/actors/student_actor",
        headers=request_headers(),
        json={},
    )

    assert response.content == (
        b'{"actor_id":"student_actor","actor_type":"student","roles":["game:player"],'
        b'"tenant_id":"tenant_yaya"}'
    )
    with pytest.raises(TransportError):
        canonical_payload({"fraction": 1.5})
    assert canonical_payload({"negative_zero": -0.0}) == b'{"negative_zero":0}'


def test_websocket_requires_bearer_and_exposes_the_same_trusted_attempt_context(client: TestClient) -> None:
    """Catches a WSS path that bypasses HTTP bearer and attempt-context transport checks."""
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect(
            "/_transport-test/ws-context",
            headers={**ATTEMPT_HEADERS, "X-Stream-Protocol-Version": STREAM_PROTOCOL_VERSION},
            subprotocols=[RUNTIME_SUBPROTOCOL],
        ):
            pass
    assert denied.value.code == 4401

    with client.websocket_connect(
        "/_transport-test/ws-context",
        headers=websocket_headers(),
        subprotocols=[RUNTIME_SUBPROTOCOL],
    ) as websocket:
        assert websocket.accepted_subprotocol == RUNTIME_SUBPROTOCOL
        assert websocket.receive_json() == {
            "request_id": ATTEMPT_HEADERS["X-Request-Id"],
            "trace_id": ATTEMPT_HEADERS["X-Trace-Id"],
            "correlation_id": ATTEMPT_HEADERS["X-Correlation-Id"],
            "actor_id": "student_actor",
            "schema_version": "1.0.0",
            "content_unit_id": "UNIT_TRANSPORT",
            "content_version": "1.0.0",
            "content_hash": "0" * 64,
            "command_id": "cmd_transport_00000001",
            "causation_id": None,
            "deadline_at": None,
        }


@pytest.mark.parametrize(
    ("headers", "subprotocols"),
    [
        (request_headers(), [RUNTIME_SUBPROTOCOL]),
        (websocket_headers(**{"X-Stream-Protocol-Version": "9.9.9"}), [RUNTIME_SUBPROTOCOL]),
        (websocket_headers(), ["yaya.runtime.v9"]),
        (websocket_headers(), [RUNTIME_SUBPROTOCOL, "yaya.runtime.v9"]),
    ],
)
def test_websocket_rejects_missing_or_wrong_runtime_protocol_handshake(
    client: TestClient, headers: dict[str, str], subprotocols: list[str]
) -> None:
    """Catches a WSS transport that admits a peer without the locked AsyncAPI handshake."""
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(
            "/_transport-test/ws-context", headers=headers, subprotocols=subprotocols
        ):
            pass
    assert rejected.value.code == 4400


def test_websocket_closes_unsupported_schema_version_with_contract_code(client: TestClient) -> None:
    """Catches treating an unsupported wire schema as a generic malformed WebSocket handshake."""
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(
            "/_transport-test/ws-context",
            headers=websocket_headers(**{"X-Schema-Version": "9.9.9"}),
            subprotocols=[RUNTIME_SUBPROTOCOL],
        ):
            pass
    assert rejected.value.code == 4406


def test_websocket_rejects_repeated_sec_websocket_protocol_header(client: TestClient) -> None:
    """Catches a parser that reads only the first of multiple forbidden subprotocol headers."""
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope: dict[str, object] = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": "/_transport-test/ws-context",
        "raw_path": b"/_transport-test/ws-context",
        "query_string": b"",
        "root_path": "",
        "headers": [
            *[(name.lower().encode(), value.encode()) for name, value in websocket_headers().items()],
            (b"sec-websocket-protocol", RUNTIME_SUBPROTOCOL.encode()),
            (b"sec-websocket-protocol", b"yaya.runtime.v9"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
        "state": {},
    }

    asyncio.run(app_for_transport_test()(scope, receive, send))

    assert messages == [{"type": "websocket.close", "code": 4400}]
