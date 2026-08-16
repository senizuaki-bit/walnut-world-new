"""HTTP attempt identity and bearer-derived actor middleware."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import Request
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from yaya_agent_contracts import ActorType

from walnut_backend.api.auth import AuthenticationError, JwtAuthenticator
from walnut_backend.api.dependencies import ActorRef, ContentRef, OperationContext
from walnut_backend.api.errors import TransportError, attempt_headers, error_response
from walnut_backend.bootstrap import Settings

ATTEMPT_PATTERNS = {
    "X-Request-Id": re.compile(r"^req_[A-Za-z0-9_-]{8,96}$"),
    "X-Trace-Id": re.compile(r"^trace_[A-Za-z0-9_-]{8,96}$"),
    "X-Correlation-Id": re.compile(r"^corr_[A-Za-z0-9_-]{8,96}$"),
}
DEVELOPMENT_TOKEN = re.compile(r"^Bearer\s+([A-Za-z0-9_-]{3,96}):([A-Za-z0-9_-]{3,128})$")
STREAM_PROTOCOL_VERSION = "1.0.0"
RUNTIME_SUBPROTOCOL = "yaya.runtime.v1"
MOCK_OPERATOR_ROLES = (
    "class-insights:read",
    "content:approver",
    "content:read",
    "content:submitter",
    "evidence:read",
    "learner:read",
    "operator",
    "report:create",
    "report:read",
)
MOCK_TEACHER_ROLES = (
    "class-insights:read",
    "content:read",
    "content:submitter",
    "evidence:read",
    "learner:read",
    "report:create",
    "report:read",
    "teacher",
)


class TransportMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        error_catalog: Mapping[str, tuple[int, str, bool, str]],
    ) -> None:
        super().__init__(app)
        self._settings = settings
        self._error_catalog = error_catalog

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        is_feishu_mcp = request.url.path == "/integrations/feishu/v1/mcp"
        identity, invalid_header = attempt_identity(
            request.headers, allow_missing=is_feishu_mcp
        )
        requested_schema_version = request.headers.get("X-Schema-Version")
        if is_feishu_mcp and requested_schema_version is None:
            requested_schema_version = "1.0.0"
        context = OperationContext(
            request_id=identity["X-Request-Id"],
            correlation_id=identity["X-Correlation-Id"],
            trace_id=identity["X-Trace-Id"],
            requested_at=datetime.now(UTC),
            actor=ActorRef("tenant_unknown", "actor_unknown", ActorType.SERVICE, ()),
            content_ref=ContentRef("UNIT_TRANSPORT", "1.0.0", "0" * 64),
            # The immutable context cannot represent an unsupported version;
            # retain a valid construction only long enough to return its error.
            schema_version=requested_schema_version if requested_schema_version == "1.0.0" else "1.0.0",
            command_id=f"cmd_{uuid4().hex}",
            causation_id=None,
        )
        if invalid_header is not None:
            return error_response(TransportError("INVALID_REQUEST", "TRANSPORT"), context, self._error_catalog)
        if requested_schema_version != "1.0.0":
            return error_response(
                TransportError("SCHEMA_VERSION_UNSUPPORTED", "TRANSPORT"), context, self._error_catalog
            )
        actor = authenticate(request.headers, self._settings)
        if actor is None:
            return error_response(
                TransportError("AUTHENTICATION_REQUIRED", "AUTHENTICATION"), context, self._error_catalog
            )
        context = OperationContext(
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            requested_at=context.requested_at,
            actor=actor,
            content_ref=context.content_ref,
            schema_version=context.schema_version,
            command_id=context.command_id,
            causation_id=context.causation_id,
            deadline_at=context.deadline_at,
        )
        request.state.operation_context = context
        response = await call_next(request)
        for name, value in attempt_headers(context).items():
            response.headers.setdefault(name, value)
        return response


def attempt_identity(
    headers: Headers, *, allow_missing: bool = False
) -> tuple[dict[str, str], str | None]:
    identity: dict[str, str] = {}
    invalid_header = None
    for name, pattern in ATTEMPT_PATTERNS.items():
        value = headers.get(name)
        if value is None or pattern.fullmatch(value) is None:
            if value is not None or not allow_missing:
                invalid_header = invalid_header or name
            prefix = {
                "X-Request-Id": "req",
                "X-Trace-Id": "trace",
                "X-Correlation-Id": "corr",
            }[name]
            identity[name] = f"{prefix}_{uuid4().hex}"
        else:
            identity[name] = value
    if headers.get("X-Schema-Version") is None and not allow_missing:
        invalid_header = invalid_header or "X-Schema-Version"
    return identity, invalid_header


def authenticate(headers: Headers, settings: Settings) -> ActorRef | None:
    """Derive actor identity from production JWT or an explicit local-only mock profile."""
    authorization = headers.get("Authorization", "")
    if settings.auth_hmac_secret is not None:
        try:
            return JwtAuthenticator(settings).authenticate(authorization)
        except AuthenticationError:
            return None
    if not settings.development_auth_enabled:
        return None
    match = DEVELOPMENT_TOKEN.fullmatch(authorization)
    if match is None:
        return None
    tenant_id, actor_id = match.groups()
    if actor_id.startswith(("operator_", "feishu_operator_")):
        return ActorRef(tenant_id, actor_id, ActorType.OPERATOR, MOCK_OPERATOR_ROLES)
    if actor_id.startswith("feishu_teacher_"):
        return ActorRef(tenant_id, actor_id, ActorType.TEACHER, MOCK_TEACHER_ROLES)
    return ActorRef(tenant_id, actor_id, ActorType.STUDENT, ("game:player",))


class WebSocketTransportMiddleware:
    """Apply the same verified identity and actor context before a WebSocket accepts."""

    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        error_catalog: Mapping[str, tuple[int, str, bool, str]],
    ) -> None:
        self.app = app
        self._settings = settings
        self._error_catalog = error_catalog

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        identity, invalid_header = attempt_identity(headers)
        if invalid_header is not None:
            await send({"type": "websocket.close", "code": 4400})
            return
        if headers.get("X-Schema-Version") != "1.0.0":
            await send({"type": "websocket.close", "code": 4406})
            return
        if headers.get("X-Stream-Protocol-Version") != STREAM_PROTOCOL_VERSION:
            await send({"type": "websocket.close", "code": 4400})
            return
        subprotocol_headers = [
            value.decode("latin-1")
            for name, value in scope["headers"]
            if name.lower() == b"sec-websocket-protocol"
        ]
        if subprotocol_headers != [RUNTIME_SUBPROTOCOL]:
            await send({"type": "websocket.close", "code": 4400})
            return
        actor = authenticate(headers, self._settings)
        if actor is None:
            await send({"type": "websocket.close", "code": 4401})
            return
        context = OperationContext(
            request_id=identity["X-Request-Id"],
            correlation_id=identity["X-Correlation-Id"],
            trace_id=identity["X-Trace-Id"],
            requested_at=datetime.now(UTC),
            actor=actor,
            content_ref=ContentRef("UNIT_TRANSPORT", "1.0.0", "0" * 64),
            schema_version="1.0.0",
            command_id="cmd_transport_00000001",
            causation_id=None,
        )
        scope.setdefault("state", {})["operation_context"] = context
        await self.app(scope, receive, negotiate_runtime_subprotocol(send))


def negotiate_runtime_subprotocol(send: Send) -> Send:
    """Select the only runtime subprotocol accepted by the frozen AsyncAPI contract."""

    async def send_with_runtime_subprotocol(message: Message) -> None:
        if message["type"] == "websocket.accept":
            message = {**message, "subprotocol": RUNTIME_SUBPROTOCOL}
        await send(message)

    return send_with_runtime_subprotocol
