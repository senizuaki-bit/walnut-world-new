"""Private HTTP surface for YAYA_RECOVERABLE_LLM_V1."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response

from walnut_backend.adapters.postgres.session import create_session_factory

from .config import RelaySettings
from .dispatcher import RelayDispatcher
from .protocol import (
    CAPABILITIES_PATH,
    DISPATCH_PATH,
    PROTOCOL,
    RelayDispatchConflict,
    RelayDispatchExpired,
    RelayProtocolError,
    canonical_response_bytes,
    capabilities_document,
    parse_put_request,
    resource_document,
    valid_dispatch_id,
)
from .store import PostgresRelayStore, RelayGenerationLimitExceeded, RelayStore
from .upstream import UpstreamTransport, UrllibUpstreamTransport

STATS_PATH = "/__private__/llm-relay/statistics"


def create_relay_app(
    settings: RelaySettings,
    *,
    store: RelayStore | None = None,
    upstream: UpstreamTransport | None = None,
    start_dispatcher: bool = True,
) -> FastAPI:
    sessions = None if store is not None else create_session_factory(settings.database_url)
    relay_store = store or PostgresRelayStore(
        sessions,  # type: ignore[arg-type]
        result_retention_seconds=settings.result_retention_seconds,
    )
    relay_upstream = upstream or UrllibUpstreamTransport(
        endpoint=settings.upstream_endpoint,
        api_key=settings.upstream_api_key,
        timeout_ms=settings.upstream_timeout_ms,
        max_response_bytes=settings.max_response_bytes,
    )
    dispatcher = RelayDispatcher(
        relay_store,
        relay_upstream,
        upstream_deadline_seconds=settings.upstream_deadline_seconds,
        idle_poll_seconds=settings.poll_seconds,
        max_total_generations=settings.max_total_generations,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        stop = asyncio.Event()
        task = (
            asyncio.create_task(dispatcher.run_forever(stop), name="llm-relay-dispatcher")
            if start_dispatcher
            else None
        )
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task
            if sessions is not None:
                await sessions.kw["bind"].dispose()

    app = FastAPI(
        title="Walnut private recoverable LLM relay",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.relay_settings = settings
    app.state.relay_store = relay_store
    app.state.relay_dispatcher = dispatcher

    @app.middleware("http")
    async def private_protocol(request: Request, call_next: object) -> Response:
        if request.url.path not in {CAPABILITIES_PATH, STATS_PATH} and not request.url.path.startswith(
            DISPATCH_PATH
        ):
            return _json(404, {"schema_version": "1.0.0", "code": "NOT_FOUND"})
        if request.headers.get("x-yaya-llm-protocol") != PROTOCOL:
            return _json(400, {"schema_version": "1.0.0", "code": "PROTOCOL_REQUIRED"})
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {settings.relay_api_key}"
        if not hmac.compare_digest(authorization, expected):
            return _json(401, {"schema_version": "1.0.0", "code": "UNAUTHORIZED"})
        hostname = urlsplit(f"//{request.headers.get('host', '')}").hostname
        if hostname is None or hostname.lower() not in {
            "127.0.0.1",
            "localhost",
            "::1",
            "llm-relay",
            "testserver",
        }:
            return _json(421, {"schema_version": "1.0.0", "code": "PRIVATE_HOST_REQUIRED"})
        response = await call_next(request)  # type: ignore[operator]
        response.headers["cache-control"] = "no-store"
        response.headers["x-content-type-options"] = "nosniff"
        return response

    @app.get(CAPABILITIES_PATH)
    async def capabilities() -> Response:
        return _json(
            200,
            capabilities_document(
                retention_seconds=settings.result_retention_seconds,
                max_request_bytes=settings.max_request_bytes,
                max_response_bytes=settings.max_response_bytes,
            ),
        )

    @app.get(STATS_PATH)
    async def statistics() -> Response:
        return _json(200, await relay_store.statistics())

    @app.put(f"{DISPATCH_PATH}{{dispatch_id}}")
    async def put_dispatch(dispatch_id: str, request: Request) -> Response:
        if request.headers.get("content-type", "").partition(";")[0].strip().lower() != (
            "application/json"
        ):
            return _json(415, {"schema_version": "1.0.0", "code": "JSON_REQUIRED"})
        length = request.headers.get("content-length")
        if length is not None and (not length.isdecimal() or int(length) > settings.max_request_bytes):
            return _json(413, {"schema_version": "1.0.0", "code": "REQUEST_TOO_LARGE"})
        body = await request.body()
        try:
            value = parse_put_request(
                dispatch_id,
                body,
                provider=settings.provider,
                model=settings.model,
                maximum_bytes=settings.max_request_bytes,
            )
            result = await relay_store.put(
                value,
                max_total_generations=settings.max_total_generations,
            )
        except RelayProtocolError:
            return _json(400, {"schema_version": "1.0.0", "code": "INVALID_DISPATCH"})
        except RelayDispatchConflict:
            return _json(409, {"schema_version": "1.0.0", "code": "DISPATCH_CONFLICT"})
        except RelayDispatchExpired:
            return _json(410, {"schema_version": "1.0.0", "code": "DISPATCH_EXPIRED"})
        except RelayGenerationLimitExceeded:
            return _json(
                429,
                {
                    "schema_version": "1.0.0",
                    "code": "GENERATION_LIMIT_EXCEEDED",
                },
            )
        resource = result.resource
        status = 202 if resource.state == "PENDING" else (201 if result.created else 200)
        headers = {"retry-after": "1"} if resource.state == "PENDING" else None
        return _json(status, resource_document(resource, replayed=not result.created), headers)

    @app.get(f"{DISPATCH_PATH}{{dispatch_id}}")
    async def get_dispatch(dispatch_id: str) -> Response:
        if not valid_dispatch_id(dispatch_id):
            return _json(400, {"schema_version": "1.0.0", "code": "INVALID_DISPATCH"})
        try:
            resource = await relay_store.get(dispatch_id)
        except RelayDispatchExpired:
            return _json(410, {"schema_version": "1.0.0", "code": "DISPATCH_EXPIRED"})
        if resource is None:
            return _json(
                404,
                {
                    "schema_version": "1.0.0",
                    "code": "DISPATCH_NOT_FOUND",
                    "dispatch_id": dispatch_id,
                },
            )
        status = 202 if resource.state == "PENDING" else 200
        headers = {"retry-after": "1"} if resource.state == "PENDING" else None
        return _json(status, resource_document(resource, replayed=True), headers)

    return app


def _json(
    status: int,
    value: dict[str, object],
    headers: dict[str, str] | None = None,
) -> Response:
    return Response(
        canonical_response_bytes(value),
        status_code=status,
        media_type="application/json",
        headers=headers,
    )


__all__ = ["create_relay_app"]
