"""Strict HTTP/JSON adapter for the frozen Game Agent-turn surface."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as _StdlibThreadingHTTPServer
from typing import Any, Protocol, cast
from urllib.parse import parse_qs, urlsplit

from yaya_agent_contracts import ContractError, ErrorCategory, FrozenJsonObject

from .application import (
    AgentTurnApplication,
    BackendApplicationError,
    HttpAttempt,
    ResourceResult,
)
from .auth import AuthenticationError, JwtAuthenticator
from .codec import plain
from .wire import ContractSchemaValidator

_MAX_BODY = 8 * 1024 * 1024
_RESOURCE = r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}"
_POST_TURN = re.compile(rf"^/v1/agent-sessions/(?P<id>{_RESOURCE})/turns$")
_POST_BUILD = re.compile(r"^/v1/skill-builds$")
_GET_BUILD = re.compile(rf"^/v1/skill-builds/(?P<id>{_RESOURCE})$")
_POST_ACTIVATION = re.compile(rf"^/v1/skill-versions/(?P<id>{_RESOURCE})/activations$")
_GET_ACTIVATION = re.compile(rf"^/v1/skill-activations/(?P<id>{_RESOURCE})$")
_POST_SESSION = re.compile(r"^/v1/agent-sessions$")
_GET_SESSION = re.compile(rf"^/v1/agent-sessions/(?P<id>{_RESOURCE})$")
_GET_BOOTSTRAP = re.compile(r"^/v1/bootstrap$")
_GET_COMMAND = re.compile(rf"^/v1/commands/(?P<id>{_RESOURCE})$")
_GET_RUN = re.compile(rf"^/v1/runs/(?P<id>{_RESOURCE})$")
_GET_WORLD = re.compile(rf"^/v1/worlds/(?P<id>{_RESOURCE})/snapshot$")
_GET_EVENTS = re.compile(rf"^/v1/worlds/(?P<id>{_RESOURCE})/events$")
_GET_EVIDENCE = re.compile(r"^/v1/evidence/(?P<id>evidence_[A-Za-z0-9_-]{8,128})$")

type _ErrorMetadata = tuple[int, ErrorCategory, bool, str]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpApi(Protocol):
    async def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> HttpResponse: ...


class ThreadingHTTPServer(_StdlibThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exception()
        if isinstance(
            error,
            (ConnectionResetError, ConnectionAbortedError, BrokenPipeError),
        ):
            return
        super().handle_error(request, client_address)


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _headers(source: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in source.items():
        normalized = key.strip().lower()
        if not normalized or normalized in result or "\r" in value or "\n" in value:
            raise ValueError("HTTP headers are invalid or duplicated")
        result[normalized] = value.strip()
    return result


def _strict_object(raw: bytes) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result

    def constant(value: str) -> object:
        raise ValueError(f"invalid JSON constant {value}")

    value = json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=pairs,
        parse_constant=constant,
    )
    if not isinstance(value, Mapping):
        raise ValueError("request JSON root must be an object")
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()}


def _load_error_catalog(validator: ContractSchemaValidator) -> dict[str, _ErrorMetadata]:
    path = validator.contracts_root / "error-catalog.json"
    value = _strict_object(path.read_bytes())
    if value.get("catalog_version") != "1.0.0" or not isinstance(value.get("errors"), list):
        raise RuntimeError("frozen error catalog is invalid")
    result: dict[str, _ErrorMetadata] = {}
    for raw in cast(list[object], value["errors"]):
        if not isinstance(raw, Mapping):
            raise RuntimeError("frozen error catalog entry is invalid")
        item = cast(Mapping[str, object], raw)
        if set(item) != {
            "code",
            "http_status",
            "category",
            "retryable",
            "user_message_key",
        }:
            raise RuntimeError("frozen error catalog entry is not closed")
        code = item["code"]
        status = item["http_status"]
        retryable = item["retryable"]
        message_key = item["user_message_key"]
        if (
            not isinstance(code, str)
            or code in result
            or isinstance(status, bool)
            or not isinstance(status, int)
            or not isinstance(retryable, bool)
            or not isinstance(message_key, str)
        ):
            raise RuntimeError("frozen error catalog entry has invalid fields")
        result[code] = (
            status,
            ErrorCategory(cast(str, item["category"])),
            retryable,
            message_key,
        )
    return result


class AgentHttpApi:
    def __init__(
        self,
        *,
        application: AgentTurnApplication,
        authenticator: JwtAuthenticator,
        validator: ContractSchemaValidator,
        student_chain: object | None = None,
    ) -> None:
        self._application = application
        self._authenticator = authenticator
        self._validator = validator
        self._student_chain = student_chain
        self._catalog = _load_error_catalog(validator)

    async def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> HttpResponse:
        try:
            incoming = _headers(headers)
            if incoming.get("x-yaya-transport-invalid") == "ATTEMPT_IDENTITY_INVALID":
                return self._transport_rejection()
            attempt = HttpAttempt(
                request_id=incoming.get("x-request-id", ""),
                trace_id=incoming.get("x-trace-id", ""),
                correlation_id=incoming.get("x-correlation-id", ""),
                requested_at=datetime.now(UTC),
            )
        except ValueError:
            # There is no truthful current-attempt identity to echo.  The
            # frozen error schema therefore cannot be fabricated; reject at
            # the transport boundary without silently minting identifiers.
            return self._transport_rejection()
        request_id = attempt.request_id
        trace_id = attempt.trace_id
        correlation_id = attempt.correlation_id
        attempt_headers = {
            "X-Request-Id": request_id,
            "X-Trace-Id": trace_id,
            "X-Correlation-Id": correlation_id,
        }
        try:
            transport_invalid = incoming.get("x-yaya-transport-invalid")
            if transport_invalid == "PAYLOAD_TOO_LARGE":
                raise self._application_error(
                    "PAYLOAD_TOO_LARGE", 413, "ACCEPT", "Request body exceeds 8 MiB"
                )
            if transport_invalid is not None or "transfer-encoding" in incoming:
                raise self._application_error(
                    "INVALID_REQUEST", 400, "ACCEPT", "HTTP framing headers are invalid"
                )
            schema_version = incoming.get("x-schema-version")
            if schema_version != "1.0.0":
                raise self._application_error(
                    "SCHEMA_VERSION_UNSUPPORTED",
                    409,
                    "VALIDATE",
                    "X-Schema-Version must equal 1.0.0",
                )
            try:
                actor = self._authenticator.authenticate(incoming.get("authorization", ""))
            except AuthenticationError as error:
                raise self._application_error(
                    "AUTHENTICATION_REQUIRED",
                    401,
                    "AUTHENTICATE",
                    "Bearer JWT authentication failed",
                ) from error
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise self._application_error(
                    "INVALID_REQUEST", 400, "VALIDATE", "Request target is invalid"
                )
            if method == "POST":
                turn_match = _POST_TURN.fullmatch(parsed.path)
                build_match = _POST_BUILD.fullmatch(parsed.path)
                activation_match = _POST_ACTIVATION.fullmatch(parsed.path)
                session_match = _POST_SESSION.fullmatch(parsed.path)
                if (
                    parsed.query
                    or not any((turn_match, build_match, activation_match, session_match))
                    or (turn_match is None and self._student_chain is None)
                ):
                    raise self._application_error(
                        "NOT_FOUND", 404, "VALIDATE", "Route was not found"
                    )
                if incoming.get("content-type", "").split(";", 1)[0].strip().lower() != (
                    "application/json"
                ):
                    raise self._application_error(
                        "INVALID_REQUEST", 400, "ACCEPT", "Content-Type must be application/json"
                    )
                length_raw = incoming.get("content-length")
                try:
                    content_length = int(length_raw or "")
                except ValueError as error:
                    raise self._application_error(
                        "INVALID_REQUEST", 400, "ACCEPT", "Content-Length is required"
                    ) from error
                if content_length > _MAX_BODY:
                    raise self._application_error(
                        "PAYLOAD_TOO_LARGE", 413, "ACCEPT", "Request body exceeds 8 MiB"
                    )
                if content_length != len(body):
                    raise self._application_error(
                        "INVALID_REQUEST", 400, "ACCEPT", "Content-Length does not match body"
                    )
                try:
                    parsed_body = _strict_object(body)
                except (UnicodeError, json.JSONDecodeError, ValueError) as error:
                    raise self._application_error(
                        "INVALID_REQUEST", 400, "ACCEPT", "Body is not strict UTF-8 JSON"
                    ) from error
                if turn_match is not None:
                    accepted = await self._application.accept(
                        actor=actor,
                        attempt=attempt,
                        session_id=turn_match.group("id"),
                        idempotency_key=incoming.get("idempotency-key", ""),
                        raw_body=body,
                        body=parsed_body,
                    )
                else:
                    from .student_skill_chain import StudentSkillChainApplication

                    chain = self._student_chain
                    if not isinstance(chain, StudentSkillChainApplication):
                        raise self._application_error(
                            "NOT_FOUND", 404, "VALIDATE", "Route was not found"
                        )
                    if build_match is not None:
                        accepted = await chain.accept_build(
                            actor=actor,
                            attempt=attempt,
                            idempotency_key=incoming.get("idempotency-key", ""),
                            raw_body=body,
                            body=parsed_body,
                        )
                    elif activation_match is not None:
                        accepted = await chain.accept_activation(
                            actor=actor,
                            attempt=attempt,
                            skill_version_id=activation_match.group("id"),
                            idempotency_key=incoming.get("idempotency-key", ""),
                            raw_body=body,
                            body=parsed_body,
                        )
                    else:
                        accepted = await chain.accept_session(
                            actor=actor,
                            attempt=attempt,
                            idempotency_key=incoming.get("idempotency-key", ""),
                            raw_body=body,
                            body=parsed_body,
                        )
                response_headers = {
                    **attempt_headers,
                    "Location": f"/v1/commands/{accepted.command.command_id}",
                    "Retry-After": "1",
                    "Idempotency-Replayed": "true" if accepted.replayed else "false",
                }
                try:
                    return self._success(202, accepted.receipt, response_headers)
                except Exception as error:
                    raise BackendApplicationError(
                        "UNKNOWN_COMMIT_STATE",
                        503,
                        "WORLD_COMMIT",
                        "Durable acceptance could not be serialized for transport",
                        {"exception_type": type(error).__name__},
                        command_id=accepted.command.command_id,
                    ) from error
            if method != "GET" or body:
                raise self._application_error(
                    "INVALID_REQUEST", 400, "VALIDATE", "HTTP method or GET body is invalid"
                )
            result = await self._dispatch_get(parsed.path, parsed.query, actor, attempt)
            return self._success(200, result.payload, {**attempt_headers, **result.headers})
        except BackendApplicationError as error:
            return self._error_response(error, attempt_headers)
        except Exception:
            return self._error_response(
                self._application_error(
                    "INTERNAL_ERROR",
                    500,
                    "VALIDATE",
                    "The HTTP adapter could not produce a trustworthy response",
                ),
                attempt_headers,
            )

    async def _dispatch_get(
        self,
        path: str,
        query: str,
        actor: object,
        attempt: HttpAttempt,
    ) -> ResourceResult:
        from yaya_agent_contracts import ActorRef

        if not isinstance(actor, ActorRef):
            raise TypeError("authenticated actor has invalid type")
        if self._student_chain is not None:
            from .student_skill_chain import StudentSkillChainApplication

            if not isinstance(self._student_chain, StudentSkillChainApplication):
                raise TypeError("student chain application has invalid type")
            if _GET_BOOTSTRAP.fullmatch(path):
                if query:
                    raise self._application_error(
                        "INVALID_REQUEST", 400, "VALIDATE", "Unexpected query"
                    )
                return await self._student_chain.get_bootstrap(actor, attempt)
            if match := _GET_BUILD.fullmatch(path):
                if query:
                    raise self._application_error(
                        "INVALID_REQUEST", 400, "VALIDATE", "Unexpected query"
                    )
                return await self._student_chain.get_build(match.group("id"), actor)
            if match := _GET_ACTIVATION.fullmatch(path):
                if query:
                    raise self._application_error(
                        "INVALID_REQUEST", 400, "VALIDATE", "Unexpected query"
                    )
                return await self._student_chain.get_activation(match.group("id"), actor)
            if match := _GET_SESSION.fullmatch(path):
                if query:
                    raise self._application_error(
                        "INVALID_REQUEST", 400, "VALIDATE", "Unexpected query"
                    )
                return await self._student_chain.get_session(match.group("id"), actor)
        if match := _GET_COMMAND.fullmatch(path):
            if query:
                raise self._application_error(
                    "INVALID_REQUEST", 400, "VALIDATE", "Unexpected query"
                )
            return await self._application.get_command(match.group("id"), actor)
        if match := _GET_RUN.fullmatch(path):
            if query:
                raise self._application_error(
                    "INVALID_REQUEST", 400, "VALIDATE", "Unexpected query"
                )
            return await self._application.get_run(match.group("id"), actor)
        if match := _GET_WORLD.fullmatch(path):
            if query:
                raise self._application_error(
                    "INVALID_REQUEST", 400, "VALIDATE", "Unexpected query"
                )
            return await self._application.get_world(match.group("id"), actor)
        if match := _GET_EVIDENCE.fullmatch(path):
            if query:
                raise self._application_error(
                    "INVALID_REQUEST", 400, "VALIDATE", "Unexpected query"
                )
            return await self._application.get_evidence(match.group("id"), actor)
        if match := _GET_EVENTS.fullmatch(path):
            try:
                values = parse_qs(query, strict_parsing=True, keep_blank_values=True)
                if (
                    set(values) - {"after_sequence", "limit"}
                    or len(values.get("after_sequence", [])) != 1
                ):
                    raise ValueError("event query fields are invalid")
                if "limit" in values and len(values["limit"]) != 1:
                    raise ValueError("limit must occur once")
                after = int(values["after_sequence"][0])
                limit = int(values.get("limit", ["100"])[0])
                if after < 0 or not 1 <= limit <= 500:
                    raise ValueError("event query bounds are invalid")
            except (KeyError, ValueError) as error:
                raise self._application_error(
                    "INVALID_REQUEST", 400, "VALIDATE", "World event query is invalid"
                ) from error
            return await self._application.list_world_events(
                match.group("id"),
                after_sequence=after,
                limit=limit,
                actor=actor,
            )
        raise self._application_error("NOT_FOUND", 404, "VALIDATE", "Route was not found")

    @staticmethod
    def _application_error(
        code: str,
        status: int,
        stage: str,
        message: str,
    ) -> BackendApplicationError:
        return BackendApplicationError(code, status, stage, message)

    def _success(
        self,
        status: int,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> HttpResponse:
        return self._encode_response(status, payload, headers)

    @staticmethod
    def _encode_response(
        status: int,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> HttpResponse:
        body = _json_bytes(payload)
        return HttpResponse(
            status,
            {
                **headers,
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
                "Cache-Control": "no-store",
            },
            body,
        )

    @staticmethod
    def _transport_rejection() -> HttpResponse:
        return HttpResponse(
            400,
            {
                "Content-Length": "0",
                "Cache-Control": "no-store",
                "Connection": "close",
            },
            b"",
        )

    def _error_response(
        self,
        source: BackendApplicationError,
        attempt_headers: Mapping[str, str],
    ) -> HttpResponse:
        code = source.code if source.code in self._catalog else "INTERNAL_ERROR"
        if code == "UNKNOWN_COMMIT_STATE" and source.command_id is None:
            code = "INTERNAL_ERROR"
        status, category, retryable, message_key = self._catalog[code]
        contract = ContractError(
            code=code,
            category=category,
            retryable=retryable,
            user_message_key=message_key,
            stage=source.stage
            if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", source.stage)
            else "VALIDATE",
            message=str(source)[:512] or "Request failed",
            details=cast(FrozenJsonObject, source.details),
        )
        error_wire = cast(dict[str, object], plain(contract))
        response: dict[str, object] = {
            "request_id": attempt_headers["X-Request-Id"],
            "trace_id": attempt_headers["X-Trace-Id"],
            "status": (
                "UNKNOWN"
                if code == "UNKNOWN_COMMIT_STATE"
                else "FAILED"
                if status >= 500
                else "REJECTED"
            ),
            "data": None,
            "error": error_wire,
        }
        response_headers = dict(attempt_headers)
        if code == "UNKNOWN_COMMIT_STATE" and source.command_id is not None:
            response["command_id"] = source.command_id
            response_headers["Location"] = f"/v1/commands/{source.command_id}"
            response_headers["Retry-After"] = "1"
        elif status == 503:
            response_headers["Retry-After"] = "1"
        self._validator.validate("schemas/common/error-response.schema.json", response)
        return self._encode_response(status, response, response_headers)


def serve_http(
    api: HttpApi,
    host: str,
    port: int,
    *,
    ready: threading.Event | None = None,
    server_created: Callable[[ThreadingHTTPServer], None] | None = None,
) -> None:
    """Run the small HTTP boundary; process supervision owns restarts."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "YaYaAgent/1.0"

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch("PATCH")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._dispatch("OPTIONS")

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch("HEAD", suppress_body=True)

        def do_TRACE(self) -> None:  # noqa: N802
            self._dispatch("TRACE")

        def do_CONNECT(self) -> None:  # noqa: N802
            self._dispatch("CONNECT")

        def send_error(
            self,
            code: int,
            message: str | None = None,
            explain: str | None = None,
        ) -> None:
            # BaseHTTPRequestHandler otherwise emits an unversioned HTML 501
            # for any method token without a do_* member (including lowercase
            # variants).  Well-formed unknown methods belong to our closed JSON
            # boundary and must carry the current attempt identity.
            if code == 501 and self.command:
                self._dispatch(self.command)
                return
            super().send_error(code, message, explain)

        def _dispatch(self, method: str, *, suppress_body: bool = False) -> None:
            self.connection.settimeout(15)
            raw_headers = list(self.headers.raw_items())
            singleton = {
                "authorization",
                "x-schema-version",
                "x-request-id",
                "x-trace-id",
                "x-correlation-id",
                "idempotency-key",
                "content-type",
                "content-length",
                "transfer-encoding",
            }
            counts: dict[str, int] = {}
            request_headers: dict[str, str] = {}
            for key, value in raw_headers:
                normalized = key.lower()
                counts[normalized] = counts.get(normalized, 0) + 1
                request_headers.setdefault(normalized, value)
            attempt_identity_invalid = any(
                counts.get(name, 0) != 1
                for name in ("x-request-id", "x-trace-id", "x-correlation-id")
            )
            invalid_framing = any(counts.get(name, 0) > 1 for name in singleton)
            invalid_framing = invalid_framing or counts.get("transfer-encoding", 0) > 0
            invalid_framing = invalid_framing or attempt_identity_invalid
            if method in {"POST", "PUT"} and counts.get("content-length", 0) != 1:
                invalid_framing = True
            raw_length = self.headers.get("Content-Length", "0")
            if re.fullmatch(r"[0-9]+", raw_length) is None:
                invalid_framing = True
                length = 0
            else:
                length = int(raw_length)
            # GET and unsupported methods never accept an HTTP body.  Reject
            # the framing before reading it so pipelined bytes cannot be
            # reinterpreted as the next request.
            if method not in {"POST", "PUT"} and length != 0:
                invalid_framing = True
            too_large = length > _MAX_BODY
            if invalid_framing or too_large:
                self.close_connection = True
                request_body = b""
                if attempt_identity_invalid:
                    request_headers["X-YaYa-Transport-Invalid"] = "ATTEMPT_IDENTITY_INVALID"
                elif too_large:
                    request_headers["X-YaYa-Transport-Invalid"] = "PAYLOAD_TOO_LARGE"
                elif invalid_framing:
                    request_headers["X-YaYa-Transport-Invalid"] = "INVALID_FRAMING"
            else:
                try:
                    request_body = self.rfile.read(length)
                except (TimeoutError, OSError):
                    self.close_connection = True
                    request_body = b""
                    request_headers["X-YaYa-Transport-Invalid"] = "BODY_READ_FAILED"
            response = asyncio.run(api.handle(method, self.path, request_headers, request_body))
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if not suppress_body:
                self.wfile.write(response.body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer((host, port), Handler)
    if server_created is not None:
        server_created(server)
    if ready is not None:
        ready.set()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


__all__ = ["AgentHttpApi", "HttpApi", "HttpResponse", "serve_http"]
