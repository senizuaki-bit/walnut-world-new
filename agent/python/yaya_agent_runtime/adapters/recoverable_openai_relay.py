"""Recoverable OpenAI-compatible inference through a durable private relay.

The relay owns one logical Provider generation per client-generated
``dispatch_id``.  ``PUT`` is an atomic create-or-replay operation and ``GET``
is read-only reconciliation.  Original Provider response bytes are returned
with a digest and pass through the same strict parser as the direct adapter.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

from yaya_agent_contracts import (
    ContractError,
    ErrorCategory,
    Failure,
    FrozenJsonObject,
    LlmRequest,
    OperationContext,
)

from ..provider_recovery import (
    LlmDispatchIdentity,
    LlmDispatchResource,
    LlmRelayCapabilities,
    RecoverableLlmConflict,
    RecoverableLlmError,
    RecoverableLlmExpired,
    RecoverableLlmProtocolError,
    RecoverableLlmUnavailable,
    llm_recovery_sha256,
    llm_request_sha256,
    operation_context_sha256,
)
from .openai_compatible import (
    HttpResponse,
    OpenAICompatibleConfig,
    ProviderProtocolError,
    parse_openai_completion_response,
    prepare_openai_completion,
    strict_json_object,
)

_PROTOCOL = "YAYA_RECOVERABLE_LLM_V1"
_CAPABILITIES_PATH = "/v1/llm/capabilities"
_DISPATCH_PATH = "/v1/llm/dispatches/"


class RelayError(RecoverableLlmError):
    """Base class for sanitized relay failures."""


class RelayCapabilityError(RecoverableLlmProtocolError, RelayError):
    """The configured endpoint cannot prove the required recovery contract."""


class RelayProtocolError(RecoverableLlmProtocolError, RelayError):
    """Relay status, identity, or bytes violated the immutable protocol."""


class RelayConflictError(RecoverableLlmConflict, RelayProtocolError):
    """A dispatch identity was reused with different immutable bytes."""


class RelayResultExpired(RecoverableLlmExpired, RelayProtocolError):
    """The relay discarded a result still referenced by durable workflow state."""


class RelayDependencyUnavailable(RecoverableLlmUnavailable, RelayError):
    """The relay cannot currently serve dispatch or reconciliation."""


class RelayTransportError(ConnectionError):
    """No trustworthy HTTP acknowledgement was received from the relay."""


class RelayHttpTransport(Protocol):
    async def request_json(
        self,
        method: Literal["GET", "PUT"],
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object] | None,
        timeout_ms: int,
    ) -> HttpResponse: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class UrllibRelayHttpTransport:
    """Bounded GET/PUT transport; all transport failures remain acknowledgement-unknown."""

    def __init__(self, *, max_response_bytes: int = 2_097_152) -> None:
        _bounded_integer(
            max_response_bytes,
            "max_response_bytes",
            minimum=1,
            maximum=16_777_216,
        )
        self._max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    async def request_json(
        self,
        method: Literal["GET", "PUT"],
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object] | None,
        timeout_ms: int,
    ) -> HttpResponse:
        return await asyncio.to_thread(
            self._request,
            method,
            url,
            headers,
            body,
            timeout_ms,
        )

    def _request(
        self,
        method: Literal["GET", "PUT"],
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object] | None,
        timeout_ms: int,
    ) -> HttpResponse:
        if method == "GET" and body is not None:
            raise ValueError("GET relay request cannot have a body")
        if method == "PUT" and body is None:
            raise ValueError("PUT relay request requires a body")
        encoded = (
            None
            if body is None
            else json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        request = urllib.request.Request(
            url,
            data=encoded,
            headers=dict(headers),
            method=method,
        )
        try:
            with self._opener.open(request, timeout=timeout_ms / 1000) as response:
                response_body = response.read(self._max_response_bytes + 1)
                if len(response_body) > self._max_response_bytes:
                    raise RelayProtocolError("relay response exceeds max_response_bytes")
                return HttpResponse(response.status, dict(response.headers.items()), response_body)
        except urllib.error.HTTPError as error:
            response_body = error.read(self._max_response_bytes + 1)
            if len(response_body) > self._max_response_bytes:
                response_body = b""
            return HttpResponse(error.code, dict(error.headers.items()), response_body)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
            raise RelayTransportError("relay HTTP acknowledgement is unknown") from error


@dataclass(frozen=True, slots=True)
class RecoverableOpenAIRelayConfig:
    relay_endpoint: str
    api_key: str = field(repr=False)
    model: str
    provider: str
    response_format: Literal["json_object", "json_schema"] = "json_object"
    allow_insecure_localhost: bool = False
    thinking_mode: Literal["enabled", "disabled"] | None = None
    protocol: str = _PROTOCOL
    required_retention_seconds: int = 604_800
    max_response_bytes: int = 2_097_152
    capability_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        parsed = urlsplit(self.relay_endpoint)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("relay endpoint cannot contain userinfo")
        if parsed.query or parsed.fragment or not parsed.hostname:
            raise ValueError("relay endpoint must be an absolute URL without query or fragment")
        localhost = parsed.hostname.lower() in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (
            self.allow_insecure_localhost and localhost and parsed.scheme == "http"
        ):
            raise ValueError("relay endpoint must use HTTPS except explicit localhost tests")
        normalized_path = parsed.path.rstrip("/").lower()
        if normalized_path.endswith("/chat/completions"):
            raise ValueError("direct chat-completions endpoint is not a recoverable relay")
        if not isinstance(self.allow_insecure_localhost, bool):
            raise TypeError("allow_insecure_localhost must be boolean")
        if not 8 <= len(self.api_key) <= 4096:
            raise ValueError("api_key length is invalid")
        _bounded_text(self.model, "model", 128)
        _bounded_text(self.provider, "provider", 128)
        if self.response_format not in {"json_object", "json_schema"}:
            raise ValueError("response_format is not supported")
        if self.thinking_mode not in {None, "enabled", "disabled"}:
            raise ValueError("thinking_mode is not supported")
        if self.protocol != _PROTOCOL:
            raise ValueError("relay protocol is unsupported")
        _bounded_integer(
            self.required_retention_seconds,
            "required_retention_seconds",
            minimum=1,
            maximum=315_360_000,
        )
        _bounded_integer(
            self.max_response_bytes,
            "max_response_bytes",
            minimum=1,
            maximum=16_777_216,
        )
        _bounded_integer(
            self.capability_timeout_ms,
            "capability_timeout_ms",
            minimum=1,
            maximum=300_000,
        )


class RecoverableOpenAIRelayAdapter:
    """Client for ``YAYA_RECOVERABLE_LLM_V1`` PUT/GET resources."""

    def __init__(
        self,
        config: RecoverableOpenAIRelayConfig,
        transport: RelayHttpTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or UrllibRelayHttpTransport(
            max_response_bytes=config.max_response_bytes
        )
        self._capabilities: LlmRelayCapabilities | None = None
        # The endpoint is unused by the shared body/parser helpers.  Supplying
        # a valid derived path keeps one authoritative OpenAI config validator.
        self._completion_config = OpenAICompatibleConfig(
            endpoint=f"{config.relay_endpoint.rstrip('/')}/_openai_payload_only",
            api_key=config.api_key,
            model=config.model,
            provider=config.provider,
            response_format=config.response_format,
            allow_insecure_localhost=config.allow_insecure_localhost,
            thinking_mode=config.thinking_mode,
        )

    async def validate_capabilities(self) -> LlmRelayCapabilities:
        if self._capabilities is not None:
            return self._capabilities
        try:
            response = await self._transport.request_json(
                "GET",
                self._url(_CAPABILITIES_PATH),
                self._headers(content=False),
                None,
                self._config.capability_timeout_ms,
            )
        except RelayTransportError as error:
            raise RelayDependencyUnavailable("relay capabilities are unavailable") from error
        if response.status == 503:
            raise RelayDependencyUnavailable("relay capabilities are temporarily unavailable")
        if response.status != 200:
            raise RelayCapabilityError(f"relay capability endpoint returned HTTP {response.status}")
        _require_json_content_type(response, "relay capabilities")
        value = _json_object(response.body, "relay capabilities")
        _exact_keys(
            value,
            {
                "schema_version",
                "protocol",
                "result_retention_seconds",
                "max_request_bytes",
                "max_response_bytes",
                "atomic_put_by_dispatch_id",
                "linearizable_get",
                "immutable_request_hash",
                "max_generation_count",
            },
            "relay capabilities",
        )
        if value.get("schema_version") != "1.0.0":
            raise RelayCapabilityError("relay capability schema is unsupported")
        try:
            capabilities = LlmRelayCapabilities(
                protocol=_text(value, "protocol"),
                result_retention_seconds=_integer(value, "result_retention_seconds"),
                max_request_bytes=_integer(value, "max_request_bytes"),
                max_response_bytes=_integer(value, "max_response_bytes"),
                atomic_put_by_dispatch_id=_boolean(value, "atomic_put_by_dispatch_id"),
                linearizable_get=_boolean(value, "linearizable_get"),
                immutable_request_hash=_boolean(value, "immutable_request_hash"),
                max_generation_count=_integer(value, "max_generation_count"),
            )
        except (TypeError, ValueError) as error:
            raise RelayCapabilityError("relay capability guarantee is invalid") from error
        if capabilities.result_retention_seconds < self._config.required_retention_seconds:
            raise RelayCapabilityError("relay result retention is below the required horizon")
        if capabilities.max_response_bytes < self._config.max_response_bytes:
            raise RelayCapabilityError("relay response limit is below the configured parser bound")
        self._capabilities = capabilities
        return capabilities

    async def dispatch(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource:
        await self.validate_capabilities()
        completion, schema = self._prepare(identity, request, context)
        completion_sha256 = _completion_sha256(
            identity.provider,
            identity.model,
            completion,
        )
        body: dict[str, object] = {
            "schema_version": "1.0.0",
            "dispatch_id": identity.dispatch_id,
            "request_sha256": identity.request_sha256,
            "context_sha256": identity.context_sha256,
            "completion_sha256": completion_sha256,
            "provider": identity.provider,
            "model": identity.model,
            "completion": completion,
        }
        capabilities = self._capabilities
        if capabilities is None:
            raise AssertionError("relay capabilities disappeared after validation")
        encoded_body = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded_body) > capabilities.max_request_bytes:
            raise RelayProtocolError("relay request exceeds advertised max_request_bytes")
        try:
            response = await self._transport.request_json(
                "PUT",
                self._url(f"{_DISPATCH_PATH}{identity.dispatch_id}"),
                self._headers(content=True),
                body,
                request.timeout_ms,
            )
        except RelayTransportError:
            # The PUT may already have materialized a terminal resource.  GET
            # is read-only and therefore the only safe immediate recovery.
            return await self._reconcile_prepared(
                identity,
                request,
                context,
                completion_sha256,
                schema,
            )
        return self._resource_from_response(
            response,
            identity,
            completion_sha256,
            schema,
            method="PUT",
        )

    async def reconcile(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource:
        await self.validate_capabilities()
        completion, schema = self._prepare(identity, request, context)
        return await self._reconcile_prepared(
            identity,
            request,
            context,
            _completion_sha256(identity.provider, identity.model, completion),
            schema,
        )

    async def _reconcile_prepared(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
        completion_sha256: str,
        schema: Mapping[str, object],
    ) -> LlmDispatchResource:
        # Revalidate at the recovery boundary so a caller cannot swap request
        # or authority between the ambiguous PUT and the GET.
        self._validate_identity(identity, request, context)
        try:
            response = await self._transport.request_json(
                "GET",
                self._url(f"{_DISPATCH_PATH}{identity.dispatch_id}"),
                self._headers(content=False),
                None,
                request.timeout_ms,
            )
        except RelayTransportError as error:
            raise RelayDependencyUnavailable("relay reconciliation is unavailable") from error
        return self._resource_from_response(
            response,
            identity,
            completion_sha256,
            schema,
            method="GET",
        )

    def _prepare(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> tuple[dict[str, object], Mapping[str, object]]:
        self._validate_identity(identity, request, context)
        prepared = prepare_openai_completion(self._completion_config, request)
        if isinstance(prepared, Failure):
            raise RelayProtocolError("LLM request could not be encoded for the relay")
        return prepared

    def _validate_identity(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> None:
        if not isinstance(identity, LlmDispatchIdentity):
            raise TypeError("identity must be an LlmDispatchIdentity")
        if identity.request_sha256 != llm_request_sha256(request):
            raise RelayProtocolError("relay request hash does not match LlmRequest")
        if identity.context_sha256 != operation_context_sha256(context):
            raise RelayProtocolError("relay context hash does not match OperationContext")
        if identity.provider != self._config.provider or identity.model != self._config.model:
            raise RelayProtocolError("relay Provider or model identity drifted")

    def _resource_from_response(
        self,
        response: HttpResponse,
        identity: LlmDispatchIdentity,
        completion_sha256: str,
        schema: Mapping[str, object],
        *,
        method: Literal["GET", "PUT"],
    ) -> LlmDispatchResource:
        if response.status == 503:
            raise RelayDependencyUnavailable("relay is temporarily unavailable")
        if response.status == 409:
            raise RelayConflictError("relay dispatch identity conflicts with durable bytes")
        if response.status == 410:
            raise RelayResultExpired("relay result expired before workflow reconciliation")
        if response.status == 404:
            if method != "GET":
                raise RelayProtocolError("relay PUT endpoint does not implement dispatch resources")
            _require_json_content_type(response, "relay not-found response")
            _validate_not_found(response.body, identity.dispatch_id)
            return LlmDispatchResource(
                identity=identity,
                completion_sha256=completion_sha256,
                state="ABSENT",
                generation_count=0,
                replayed=False,
            )
        if response.status not in {200, 201, 202}:
            raise RelayProtocolError(f"relay returned unsupported HTTP {response.status}")
        _require_json_content_type(response, "relay dispatch resource")
        value = _json_object(response.body, "relay dispatch resource")
        _exact_keys(
            value,
            {
                "schema_version",
                "dispatch_id",
                "request_sha256",
                "context_sha256",
                "completion_sha256",
                "provider",
                "model",
                "state",
                "generation_count",
                "replayed",
                "created_at",
                "updated_at",
            },
            "relay dispatch resource",
            optional={"provider_response", "failure"},
        )
        if value.get("schema_version") != "1.0.0":
            raise RelayProtocolError("relay dispatch schema is unsupported")
        comparisons = {
            "dispatch_id": identity.dispatch_id,
            "request_sha256": identity.request_sha256,
            "context_sha256": identity.context_sha256,
            "completion_sha256": completion_sha256,
            "provider": identity.provider,
            "model": identity.model,
        }
        for name, expected in comparisons.items():
            if value.get(name) != expected:
                raise RelayProtocolError(f"relay dispatch {name} drifted")
        state = _text(value, "state")
        if state not in {"PENDING", "SUCCEEDED", "FAILED"}:
            raise RelayProtocolError("relay dispatch state is unsupported")
        if response.status == 202 and state != "PENDING":
            raise RelayProtocolError("HTTP 202 relay resource must be pending")
        if response.status == 200 and state == "PENDING":
            raise RelayProtocolError("pending replay must use HTTP 202")
        generation_count = _integer(value, "generation_count")
        replayed = _boolean(value, "replayed")
        created_at = _datetime(value, "created_at")
        updated_at = _datetime(value, "updated_at")
        retry_after = _retry_after(response.headers) if state == "PENDING" else None

        result = None
        raw_sha256 = None
        if state == "SUCCEEDED":
            raw = _mapping(value.get("provider_response"), "provider_response")
            _exact_keys(
                raw,
                {"http_status", "content_type", "body_base64", "body_sha256"},
                "provider_response",
            )
            raw_bytes = _decode_body(raw, self._config.max_response_bytes)
            raw_sha256 = _text(raw, "body_sha256")
            result = parse_openai_completion_response(
                self._completion_config,
                HttpResponse(
                    _integer(raw, "http_status"),
                    {"content-type": _text(raw, "content_type")},
                    raw_bytes,
                ),
                schema,
            )
            if value.get("failure") is not None:
                raise RelayProtocolError("successful relay resource cannot contain failure")
        elif state == "FAILED":
            failure = _mapping(value.get("failure"), "failure")
            _exact_keys(failure, {"code", "retryable"}, "relay failure")
            result = Failure(_relay_failure(_text(failure, "code"), _boolean(failure, "retryable")))
            if value.get("provider_response") is not None:
                raise RelayProtocolError("failed relay resource cannot contain Provider bytes")
        elif value.get("provider_response") is not None or value.get("failure") is not None:
            raise RelayProtocolError("pending relay resource cannot contain terminal data")

        try:
            return LlmDispatchResource(
                identity=identity,
                completion_sha256=completion_sha256,
                state=cast(AnyDispatchState, state),
                generation_count=generation_count,
                replayed=replayed,
                result=result,
                retry_after_seconds=retry_after,
                created_at=created_at,
                updated_at=updated_at,
                raw_response_sha256=raw_sha256,
            )
        except (TypeError, ValueError) as error:
            raise RelayProtocolError(
                "relay dispatch resource violates recovery invariants"
            ) from error

    def _headers(self, *, content: bool) -> dict[str, str]:
        headers = {
            "authorization": f"Bearer {self._config.api_key}",
            "accept": "application/json",
            "x-yaya-llm-protocol": self._config.protocol,
        }
        if content:
            headers["content-type"] = "application/json; charset=utf-8"
        return headers

    def _url(self, path: str) -> str:
        return f"{self._config.relay_endpoint.rstrip('/')}{path}"


# A small alias keeps the cast local without widening the public model type.
AnyDispatchState = Literal["PENDING", "SUCCEEDED", "FAILED"]


def _completion_sha256(
    provider: str,
    model: str,
    completion: Mapping[str, object],
) -> str:
    return llm_recovery_sha256(
        {
            "schema_version": "1.0.0",
            "provider": provider,
            "model": model,
            "completion": dict(completion),
        }
    )


def _decode_body(value: Mapping[str, object], maximum: int) -> bytes:
    body_base64 = _text(value, "body_base64")
    try:
        body = base64.b64decode(body_base64, validate=True)
    except (ValueError, binascii.Error) as error:
        raise RelayProtocolError("relay Provider body is not strict base64") from error
    if len(body) > maximum:
        raise RelayProtocolError("relay Provider body exceeds max_response_bytes")
    expected = _text(value, "body_sha256")
    if hashlib.sha256(body).hexdigest() != expected:
        raise RelayProtocolError("relay Provider body hash mismatch")
    return body


def _relay_failure(code: str, retryable: bool) -> ContractError:
    _bounded_text(code, "relay failure code", 96)
    return ContractError(
        code="DEPENDENCY_UNAVAILABLE",
        category=ErrorCategory.DEPENDENCY,
        retryable=retryable,
        user_message_key="dependency.temporarily_unavailable",
        stage="MODEL_PROVIDER",
        message="recoverable model relay reported a terminal failure",
        details=cast(FrozenJsonObject, {"reason": code}),
    )


def _validate_not_found(body: bytes, dispatch_id: str) -> None:
    value = _json_object(body, "relay not-found response")
    _exact_keys(
        value,
        {"schema_version", "code", "dispatch_id"},
        "relay not-found response",
    )
    if (
        value.get("schema_version") != "1.0.0"
        or value.get("code") != "DISPATCH_NOT_FOUND"
        or value.get("dispatch_id") != dispatch_id
    ):
        raise RelayProtocolError("relay not-found identity is invalid")


def _retry_after(headers: Mapping[str, str]) -> int:
    value = headers.get("retry-after")
    if value is None or not value.isascii() or not value.isdecimal():
        raise RelayProtocolError("pending relay response requires integer Retry-After")
    retry_after = int(value)
    _bounded_integer(retry_after, "Retry-After", minimum=1, maximum=86_400)
    return retry_after


def _json_object(data: bytes, label: str) -> dict[str, object]:
    try:
        return strict_json_object(data, label)
    except ProviderProtocolError as error:
        raise RelayProtocolError(f"{label} is not strict JSON") from error


def _require_json_content_type(response: HttpResponse, label: str) -> None:
    content_type = response.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise RelayProtocolError(f"{label} does not use application/json")


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RelayProtocolError(f"{label} must be an object")
    return dict(cast(Mapping[str, object], value))


def _exact_keys(
    value: Mapping[str, object],
    required: set[str],
    label: str,
    *,
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    missing = required - set(value)
    extra = set(value) - allowed
    if missing or extra:
        raise RelayProtocolError(f"{label} fields are not closed")


def _text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise RelayProtocolError(f"relay {key} must be text")
    return item


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise RelayProtocolError(f"relay {key} must be an integer")
    return item


def _boolean(value: Mapping[str, object], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise RelayProtocolError(f"relay {key} must be boolean")
    return item


def _datetime(value: Mapping[str, object], key: str) -> datetime:
    raw = _text(value, key)
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise RelayProtocolError(f"relay {key} is not RFC 3339") from error
    if result.tzinfo is None:
        raise RelayProtocolError(f"relay {key} has no offset")
    return result


def _bounded_text(value: object, name: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"{name} must be bounded printable text")


def _bounded_integer(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


__all__ = [
    "RecoverableOpenAIRelayAdapter",
    "RecoverableOpenAIRelayConfig",
    "RelayCapabilityError",
    "RelayConflictError",
    "RelayDependencyUnavailable",
    "RelayError",
    "RelayHttpTransport",
    "RelayProtocolError",
    "RelayResultExpired",
    "RelayTransportError",
    "UrllibRelayHttpTransport",
]
