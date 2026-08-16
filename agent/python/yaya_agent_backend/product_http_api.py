"""Strict HTTP adapter for frozen Product reads and SkillDraft CAS writes."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol, cast
from urllib.parse import parse_qsl, urlsplit

from yaya_agent_contracts import (
    ActorRef,
    ContractError,
    ErrorCategory,
    canonical_json_sha256,
)

from .application import HttpAttempt
from .auth import AuthenticationError, JwtAuthenticator
from .codec import plain
from .http_api import HttpResponse
from .product_application import (
    ProductApplicationError,
    ProductReadResult,
)
from .product_semantics import (
    ProductProjectionSemanticError,
    validate_interaction_semantics,
    validate_page_semantics,
)
from .skill_drafts import (
    MAX_PRODUCT_WRITE_BODY_BYTES,
    ProductDraftReconciliationRequired,
    ProductDraftWriteResult,
    validate_skill_source_bundle,
)
from .wire import ContractSchemaValidator

_RESOURCE = r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}"
_LIST = re.compile(
    rf"^/product-experience/v1/sessions/(?P<session>{_RESOURCE})/agent-interactions$"
)
_GET = re.compile(
    rf"^/product-experience/v1/sessions/(?P<session>{_RESOURCE})/"
    rf"agent-interactions/(?P<interaction>{_RESOURCE})$"
)
_DRAFT = re.compile(
    rf"^/product-experience/v1/sessions/(?P<session>{_RESOURCE})/"
    rf"skill-drafts/(?P<draft>{_RESOURCE})$"
)
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)")
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}")
_MAX_SAFE_SEQUENCE = 9_007_199_254_740_991

type _ErrorMetadata = tuple[int, ErrorCategory, bool, str]

_PRODUCT_CODES_BY_STATUS: dict[int, frozenset[str]] = {
    400: frozenset({"INVALID_REQUEST"}),
    401: frozenset({"AUTHENTICATION_REQUIRED"}),
    403: frozenset({"AUTHORIZATION_DENIED", "POLICY_DENIED"}),
    404: frozenset({"NOT_FOUND"}),
    409: frozenset(
        {
            "SCHEMA_VERSION_UNSUPPORTED",
            "CONTENT_VERSION_MISMATCH",
            "IDEMPOTENCY_KEY_REUSED",
        }
    ),
    413: frozenset({"PAYLOAD_TOO_LARGE"}),
    429: frozenset({"RATE_LIMITED"}),
    500: frozenset({"INVARIANT_VIOLATION", "INTERNAL_ERROR"}),
    503: frozenset({"DEPENDENCY_UNAVAILABLE"}),
}


class ProductReadApplication(Protocol):
    async def list_interactions(
        self,
        actor: ActorRef,
        session_id: str,
        *,
        after_sequence: int,
        limit: int = 50,
    ) -> ProductReadResult: ...

    async def get_interaction(
        self,
        actor: ActorRef,
        session_id: str,
        interaction_id: str,
    ) -> ProductReadResult: ...


class ProductSkillDraftApplicationProtocol(Protocol):
    async def get_skill_draft(
        self,
        actor: ActorRef,
        session_id: str,
        draft_id: str,
    ) -> ProductReadResult: ...

    async def upsert_skill_draft(
        self,
        actor: ActorRef,
        attempt: HttpAttempt,
        session_id: str,
        draft_id: str,
        idempotency_key: str,
        raw_body: bytes,
        body: Mapping[str, object],
    ) -> ProductDraftWriteResult: ...


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

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number {value}")

    value = json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=pairs,
        parse_constant=reject_constant,
    )
    if not isinstance(value, Mapping):
        raise ValueError("JSON root is not an object")
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()}


def _load_error_catalog(validator: ContractSchemaValidator) -> dict[str, _ErrorMetadata]:
    value = _strict_object((validator.contracts_root / "error-catalog.json").read_bytes())
    raw_errors = value.get("errors")
    if value.get("catalog_version") != "1.0.0" or not isinstance(raw_errors, list):
        raise RuntimeError("frozen error catalog is invalid")
    result: dict[str, _ErrorMetadata] = {}
    for raw in cast(list[object], raw_errors):
        if not isinstance(raw, Mapping):
            raise RuntimeError("frozen error catalog entry is invalid")
        item = cast(Mapping[str, object], raw)
        code = item.get("code")
        status = item.get("http_status")
        retryable = item.get("retryable")
        message_key = item.get("user_message_key")
        category = item.get("category")
        if (
            not isinstance(code, str)
            or isinstance(status, bool)
            or not isinstance(status, int)
            or not isinstance(retryable, bool)
            or not isinstance(message_key, str)
            or not isinstance(category, str)
            or code in result
        ):
            raise RuntimeError("frozen error catalog entry has invalid fields")
        result[code] = (status, ErrorCategory(category), retryable, message_key)
    return result


class ProductHttpApi:
    def __init__(
        self,
        *,
        application: ProductReadApplication,
        draft_application: ProductSkillDraftApplicationProtocol | None = None,
        authenticator: JwtAuthenticator,
        validator: ContractSchemaValidator,
    ) -> None:
        self._application = application
        if draft_application is not None:
            self._draft_application = draft_application
        elif callable(getattr(application, "get_skill_draft", None)) and callable(
            getattr(application, "upsert_skill_draft", None)
        ):
            self._draft_application = cast(ProductSkillDraftApplicationProtocol, application)
        else:
            self._draft_application = None
        self._authenticator = authenticator
        self._validator = validator
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
            return self._transport_rejection()
        attempt_headers = {
            "X-Request-Id": attempt.request_id,
            "X-Trace-Id": attempt.trace_id,
            "X-Correlation-Id": attempt.correlation_id,
        }
        try:
            transport_marker = incoming.get("x-yaya-transport-invalid")
            if transport_marker == "PAYLOAD_TOO_LARGE" and self._is_draft_put_target(
                method, target
            ):
                raise self._error(
                    "PAYLOAD_TOO_LARGE",
                    413,
                    "PRODUCT_DRAFT_VALIDATE",
                    "SkillDraft request body exceeds the transport limit",
                )
            if transport_marker is not None or "transfer-encoding" in incoming:
                raise self._error(
                    "INVALID_REQUEST",
                    400,
                    "PRODUCT_VALIDATE",
                    "HTTP framing headers are invalid",
                )
            if incoming.get("x-schema-version") != "1.0.0":
                raise self._error(
                    "SCHEMA_VERSION_UNSUPPORTED",
                    409,
                    "PRODUCT_VALIDATE",
                    "X-Schema-Version must equal 1.0.0",
                )
            route, route_values = self._parse_request(method, target, body)
            try:
                actor = self._authenticator.authenticate(incoming.get("authorization", ""))
            except AuthenticationError as error:
                raise self._error(
                    "AUTHENTICATION_REQUIRED",
                    401,
                    "PRODUCT_AUTHENTICATE",
                    "Bearer JWT authentication failed",
                ) from error
            if route == "list":
                result = await self._application.list_interactions(
                    actor,
                    cast(str, route_values["session_id"]),
                    after_sequence=cast(int, route_values["after_sequence"]),
                    limit=cast(int, route_values["limit"]),
                )
                self._validate_list_result(result, route_values, actor)
                return self._encode_response(
                    200,
                    result.payload,
                    {**attempt_headers, **result.headers},
                )
            if route == "get":
                result = await self._application.get_interaction(
                    actor,
                    cast(str, route_values["session_id"]),
                    cast(str, route_values["interaction_id"]),
                )
                self._validate_get_result(result, route_values, actor)
                return self._encode_response(
                    200,
                    result.payload,
                    {**attempt_headers, **result.headers},
                )
            if self._draft_application is None:
                raise self._error(
                    "DEPENDENCY_UNAVAILABLE",
                    503,
                    "PRODUCT_DRAFT_READ" if route == "draft_get" else "PRODUCT_DRAFT_WRITE",
                    "Product SkillDraft application is not configured",
                )
            if route == "draft_get":
                draft_result = await self._draft_application.get_skill_draft(
                    actor,
                    cast(str, route_values["session_id"]),
                    cast(str, route_values["draft_id"]),
                )
                self._validate_draft_read_result(draft_result, route_values, actor)
                return self._encode_response(
                    200,
                    draft_result.payload,
                    {**attempt_headers, **draft_result.headers},
                )
            draft_body, idempotency_key = self._parse_draft_write(incoming, body)
            draft_write = await self._draft_application.upsert_skill_draft(
                actor,
                attempt,
                cast(str, route_values["session_id"]),
                cast(str, route_values["draft_id"]),
                idempotency_key,
                body,
                draft_body,
            )
            self._validate_draft_write_result(draft_write, route_values, actor)
            return self._encode_raw_response(
                draft_write.status,
                draft_write.response_body,
                {**attempt_headers, **draft_write.headers},
            )
        except ProductDraftReconciliationRequired as error:
            return self._safe_reconciliation_response(error, attempt_headers)
        except ProductApplicationError as error:
            return self._safe_error_response(error, attempt_headers)
        except Exception:
            return self._safe_error_response(
                self._error(
                    "INTERNAL_ERROR",
                    500,
                    "PRODUCT_READ",
                    "The Product HTTP adapter could not produce a trustworthy response",
                ),
                attempt_headers,
            )

    def _safe_error_response(
        self,
        source: ProductApplicationError,
        attempt_headers: Mapping[str, str],
    ) -> HttpResponse:
        try:
            return self._error_response(source, attempt_headers)
        except Exception:
            fallback = self._error(
                "INTERNAL_ERROR",
                500,
                "PRODUCT_READ",
                "The Product HTTP adapter could not encode a contract-closed error",
            )
            try:
                return self._error_response(fallback, attempt_headers)
            except Exception:
                return HttpResponse(
                    500,
                    {
                        **attempt_headers,
                        "Content-Length": "0",
                        "Cache-Control": "no-store",
                        "Connection": "close",
                    },
                    b"",
                )

    def _safe_reconciliation_response(
        self,
        source: ProductDraftReconciliationRequired,
        attempt_headers: Mapping[str, str],
    ) -> HttpResponse:
        try:
            return self._reconciliation_response(source, attempt_headers)
        except Exception:
            return self._safe_error_response(
                self._error(
                    "INTERNAL_ERROR",
                    500,
                    "PRODUCT_DRAFT_COMMIT",
                    "Product reconciliation metadata was not contract-closed",
                ),
                attempt_headers,
            )

    def _reconciliation_response(
        self,
        source: ProductDraftReconciliationRequired,
        attempt_headers: Mapping[str, str],
    ) -> HttpResponse:
        expected_url = (
            f"/product-experience/v1/sessions/{source.session_id}/skill-drafts/{source.draft_id}"
        )
        if (
            re.fullmatch(_RESOURCE, source.session_id) is None
            or re.fullmatch(_RESOURCE, source.draft_id) is None
            or source.resource_url != expected_url
            or re.fullmatch(r"trace_[A-Za-z0-9_-]{8,96}", source.original_trace_id) is None
        ):
            raise ValueError("reconciliation resource identity is invalid")
        payload: dict[str, object] = {
            "request_id": attempt_headers["X-Request-Id"],
            "trace_id": attempt_headers["X-Trace-Id"],
            "correlation_id": attempt_headers["X-Correlation-Id"],
            "status": "RECONCILE",
            "data": None,
            "error": {
                "code": "DEPENDENCY_UNAVAILABLE",
                "category": "DEPENDENCY",
                "retryable": True,
                "user_message_key": "dependency.temporarily_unavailable",
                "stage": "PRODUCT_DRAFT_COMMIT",
                "message": str(source)[:512],
                "details": {"operation_was_durably_accepted": True},
            },
            "reconciliation": {
                "resource_type": "SKILL_DRAFT",
                "session_id": source.session_id,
                "resource_id": source.draft_id,
                "resource_url": source.resource_url,
                "original_trace_id": source.original_trace_id,
            },
        }
        self._validator.validate(
            "schemas/product-experience/product-write-reconciliation.schema.json",
            payload,
        )
        return self._encode_response(
            503,
            payload,
            {**attempt_headers, "Location": source.resource_url},
        )

    @staticmethod
    def _is_draft_put_target(method: str, target: str) -> bool:
        if method != "PUT":
            return False
        parsed = urlsplit(target)
        return (
            not parsed.scheme
            and not parsed.netloc
            and not parsed.query
            and not parsed.fragment
            and _DRAFT.fullmatch(parsed.path) is not None
        )

    def _parse_draft_write(
        self,
        headers: Mapping[str, str],
        body: bytes,
    ) -> tuple[dict[str, object], str]:
        if len(body) > MAX_PRODUCT_WRITE_BODY_BYTES:
            raise self._error(
                "PAYLOAD_TOO_LARGE",
                413,
                "PRODUCT_DRAFT_VALIDATE",
                "SkillDraft request body exceeds 8 MiB",
            )
        content_type = headers.get("content-type", "").lower()
        if content_type not in {"application/json", "application/json; charset=utf-8"}:
            raise self._error(
                "INVALID_REQUEST",
                400,
                "PRODUCT_DRAFT_VALIDATE",
                "SkillDraft PUT requires application/json UTF-8 content",
            )
        content_length = headers.get("content-length")
        if content_length is not None and (
            _DECIMAL.fullmatch(content_length) is None or int(content_length) != len(body)
        ):
            raise self._error(
                "INVALID_REQUEST",
                400,
                "PRODUCT_DRAFT_VALIDATE",
                "SkillDraft Content-Length does not match its body",
            )
        idempotency_key = headers.get("idempotency-key", "")
        if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise self._error(
                "INVALID_REQUEST",
                400,
                "PRODUCT_DRAFT_VALIDATE",
                "Idempotency-Key is missing or invalid",
            )
        try:
            payload = _strict_object(body)
            self._validator.validate(
                "schemas/product-experience/skill-draft-upsert-request.schema.json",
                payload,
            )
            display_name = payload.get("display_name")
            if not isinstance(display_name, str):
                raise ValueError("display_name is not text")
            display_name.encode("utf-8", errors="strict")
            validate_skill_source_bundle(payload.get("source_bundle"))
            return payload, idempotency_key
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, RuntimeError) as error:
            raise self._error(
                "INVALID_REQUEST",
                400,
                "PRODUCT_DRAFT_VALIDATE",
                "SkillDraft request is not strict contract-valid JSON",
            ) from error

    def _parse_request(
        self,
        method: str,
        target: str,
        body: bytes,
    ) -> tuple[str, dict[str, object]]:
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise self._error(
                "INVALID_REQUEST",
                400,
                "PRODUCT_VALIDATE",
                "Request target is invalid",
            )
        if match := _DRAFT.fullmatch(parsed.path):
            if parsed.query:
                raise self._error(
                    "INVALID_REQUEST",
                    400,
                    "PRODUCT_DRAFT_VALIDATE",
                    "Product SkillDraft routes do not accept query parameters",
                )
            values: dict[str, object] = {
                "session_id": match.group("session"),
                "draft_id": match.group("draft"),
            }
            if method == "GET" and not body:
                return "draft_get", values
            if method == "PUT" and body:
                return "draft_put", values
            raise self._error(
                "INVALID_REQUEST",
                400,
                "PRODUCT_DRAFT_VALIDATE",
                "SkillDraft GET is bodyless and PUT requires one JSON body",
            )
        if method != "GET" or body:
            raise self._error(
                "INVALID_REQUEST",
                400,
                "PRODUCT_VALIDATE",
                "Product AgentInteraction reads require a bodyless GET",
            )
        if match := _LIST.fullmatch(parsed.path):
            after_sequence, limit = self._parse_list_query(parsed.query)
            return "list", {
                "session_id": match.group("session"),
                "after_sequence": after_sequence,
                "limit": limit,
            }
        if match := _GET.fullmatch(parsed.path):
            if parsed.query:
                raise self._error(
                    "INVALID_REQUEST",
                    400,
                    "PRODUCT_VALIDATE",
                    "Product interaction get does not accept query parameters",
                )
            return "get", {
                "session_id": match.group("session"),
                "interaction_id": match.group("interaction"),
            }
        raise self._error(
            "NOT_FOUND",
            404,
            "PRODUCT_VALIDATE",
            "Product route was not found",
        )

    def _parse_list_query(self, query: str) -> tuple[int, int]:
        try:
            if not query or _BAD_PERCENT.search(query):
                raise ValueError("query is missing or has invalid percent encoding")
            pairs = parse_qsl(
                query,
                keep_blank_values=True,
                strict_parsing=True,
                encoding="utf-8",
                errors="strict",
                separator="&",
            )
            values: dict[str, list[str]] = {}
            for key, value in pairs:
                values.setdefault(key, []).append(value)
            if set(values) - {"after_sequence", "limit"}:
                raise ValueError("query contains an unknown field")
            if len(values.get("after_sequence", ())) != 1:
                raise ValueError("after_sequence must occur exactly once")
            if "limit" in values and len(values["limit"]) != 1:
                raise ValueError("limit must occur at most once")
            after_raw = values["after_sequence"][0]
            limit_raw = values.get("limit", ["50"])[0]
            if _DECIMAL.fullmatch(after_raw) is None or _DECIMAL.fullmatch(limit_raw) is None:
                raise ValueError("query integers are not canonical decimals")
            after_sequence = int(after_raw)
            limit = int(limit_raw)
            if not 0 <= after_sequence <= _MAX_SAFE_SEQUENCE or not 1 <= limit <= 100:
                raise ValueError("query integer is outside its contract range")
            return after_sequence, limit
        except (UnicodeError, ValueError) as error:
            raise self._error(
                "INVALID_REQUEST",
                400,
                "PRODUCT_VALIDATE",
                "Product interaction list query is invalid",
            ) from error

    def _validate_list_result(
        self,
        result: ProductReadResult,
        route: Mapping[str, object],
        actor: ActorRef,
    ) -> None:
        if not isinstance(result, ProductReadResult):
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                "Product application returned an invalid list result",
            )
        payload = result.payload
        try:
            self._validator.validate(
                "schemas/product-experience/agent-interaction-page.schema.json",
                payload,
            )
        except (TypeError, ValueError, RuntimeError) as error:
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                "Product application returned an invalid list body",
            ) from error
        high = payload.get("high_watermark_sequence")
        if (
            set(result.headers) != {"X-Interaction-High-Watermark"}
            or result.headers.get("X-Interaction-High-Watermark") != str(high)
            or payload.get("session_id") != route["session_id"]
            or payload.get("requested_after_sequence") != route["after_sequence"]
            or payload.get("requested_limit") != route["limit"]
        ):
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                "Product list response headers or request echo drifted",
            )
        try:
            validate_page_semantics(
                payload,
                authenticated_actor=actor,
                expected_session_id=cast(str, route["session_id"]),
                expected_after_sequence=cast(int, route["after_sequence"]),
                expected_limit=cast(int, route["limit"]),
            )
        except ProductProjectionSemanticError as error:
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                str(error),
            ) from error

    def _validate_get_result(
        self,
        result: ProductReadResult,
        route: Mapping[str, object],
        actor: ActorRef,
    ) -> None:
        if not isinstance(result, ProductReadResult):
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                "Product application returned an invalid get result",
            )
        payload = result.payload
        try:
            self._validator.validate(
                "schemas/product-experience/agent-interaction.schema.json",
                payload,
            )
        except (TypeError, ValueError, RuntimeError) as error:
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                "Product application returned an invalid interaction body",
            ) from error
        revision = payload.get("interaction_revision")
        expected_etag = f'"interaction:{revision}:{canonical_json_sha256(payload)}"'
        if (
            set(result.headers) != {"ETag", "X-Interaction-Revision"}
            or result.headers.get("ETag") != expected_etag
            or result.headers.get("X-Interaction-Revision") != str(revision)
        ):
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                "Product get response identity, revision, or ETag drifted",
            )
        try:
            validate_interaction_semantics(
                payload,
                authenticated_actor=actor,
                expected_session_id=cast(str, route["session_id"]),
                expected_interaction_id=cast(str, route["interaction_id"]),
            )
        except ProductProjectionSemanticError as error:
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_READ",
                str(error),
            ) from error

    def _validate_draft_payload(
        self,
        payload: Mapping[str, object],
        route: Mapping[str, object],
        actor: ActorRef,
    ) -> tuple[int, str]:
        try:
            self._validator.validate(
                "schemas/product-experience/skill-draft.schema.json",
                payload,
            )
            context_value = payload.get("request_context")
            content_value = payload.get("content_ref")
            links_value = payload.get("links")
            if not isinstance(context_value, Mapping) or not isinstance(links_value, Mapping):
                raise ValueError("SkillDraft context or links are not objects")
            context = cast(Mapping[str, object], context_value)
            actor_value = context.get("actor")
            if not isinstance(actor_value, Mapping):
                raise ValueError("SkillDraft actor is not an object")
            origin_actor = cast(Mapping[str, object], actor_value)
            actor_type = getattr(actor.actor_type, "value", str(actor.actor_type))
            revision = payload.get("revision")
            digest = payload.get("draft_sha256")
            projection = {
                "session_id": payload["session_id"],
                "draft_id": payload["draft_id"],
                "skill_id": payload["skill_id"],
                "content_ref": content_value,
                "display_name": payload["display_name"],
                "source_bundle": payload["source_bundle"],
            }
            expected_self = (
                f"/product-experience/v1/sessions/{route['session_id']}/"
                f"skill-drafts/{route['draft_id']}"
            )
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or not isinstance(digest, str)
                or payload.get("session_id") != route["session_id"]
                or payload.get("draft_id") != route["draft_id"]
                or (
                    origin_actor.get("tenant_id"),
                    origin_actor.get("actor_id"),
                    origin_actor.get("actor_type"),
                )
                != (actor.tenant_id, actor.actor_id, actor_type)
                or context.get("content_ref") != content_value
                or digest != canonical_json_sha256(projection)
                or dict(cast(Mapping[str, object], links_value))
                != {
                    "self": expected_self,
                    "session_workspace": (
                        f"/product-experience/v1/sessions/{route['session_id']}/workspace"
                    ),
                    "builds": "/v1/skill-builds",
                }
            ):
                raise ValueError("SkillDraft identity or canonical hash drifted")
            validate_skill_source_bundle(payload.get("source_bundle"))
            return revision, digest
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_DRAFT_READ",
                "Product application returned an invalid SkillDraft",
            ) from error

    def _validate_draft_read_result(
        self,
        result: ProductReadResult,
        route: Mapping[str, object],
        actor: ActorRef,
    ) -> None:
        if not isinstance(result, ProductReadResult):
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_DRAFT_READ",
                "Product application returned an invalid SkillDraft read result",
            )
        revision, digest = self._validate_draft_payload(result.payload, route, actor)
        if result.headers != {
            "ETag": f'"draft:{revision}:{digest}"',
            "X-Draft-Revision": str(revision),
        }:
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_DRAFT_READ",
                "SkillDraft read headers drifted from its resource identity",
            )

    def _validate_draft_write_result(
        self,
        result: ProductDraftWriteResult,
        route: Mapping[str, object],
        actor: ActorRef,
    ) -> None:
        if not isinstance(result, ProductDraftWriteResult):
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_DRAFT_WRITE",
                "Product application returned an invalid SkillDraft write result",
            )
        revision, digest = self._validate_draft_payload(result.payload, route, actor)
        expected_location = (
            f"/product-experience/v1/sessions/{route['session_id']}/"
            f"skill-drafts/{route['draft_id']}"
        )
        expected_status = 201 if revision == 1 else 200
        expected_headers = {
            "Location": expected_location,
            "ETag": f'"draft:{revision}:{digest}"',
            "X-Draft-Revision": str(revision),
            "Idempotency-Replayed": "true" if result.replayed else "false",
        }
        try:
            parsed = _strict_object(result.response_body)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_DRAFT_WRITE",
                "SkillDraft receipt body is not strict UTF-8 JSON",
            ) from error
        if (
            result.status != expected_status
            or result.headers != expected_headers
            or parsed != dict(result.payload)
        ):
            raise self._error(
                "INVARIANT_VIOLATION",
                500,
                "PRODUCT_DRAFT_WRITE",
                "SkillDraft write status, headers, or receipt body drifted",
            )

    @staticmethod
    def _error(
        code: str,
        status: int,
        stage: str,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> ProductApplicationError:
        return ProductApplicationError(code, status, stage, message, details)

    def _error_response(
        self,
        source: ProductApplicationError,
        attempt_headers: Mapping[str, str],
    ) -> HttpResponse:
        metadata = self._catalog.get(source.code)
        if (
            metadata is None
            or source.http_status != metadata[0]
            or source.code not in _PRODUCT_CODES_BY_STATUS.get(source.http_status, frozenset())
        ):
            source = self._error(
                "INTERNAL_ERROR",
                500,
                "PRODUCT_READ",
                "Product error metadata was not contract-closed",
            )
            metadata = self._catalog["INTERNAL_ERROR"]
        status, category, retryable, message_key = metadata
        if status == 429:
            retry_after = source.details.get("retry_after_seconds", 1)
            if isinstance(retry_after, bool) or not isinstance(retry_after, int) or retry_after < 1:
                source = self._error(
                    "INTERNAL_ERROR",
                    500,
                    "PRODUCT_READ",
                    "Product retry metadata was not contract-closed",
                )
                status, category, retryable, message_key = self._catalog["INTERNAL_ERROR"]
        _json_bytes({"details": dict(source.details)})
        contract = ContractError(
            code=source.code,
            category=category,
            retryable=retryable,
            user_message_key=message_key,
            stage=(
                source.stage
                if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", source.stage)
                else "PRODUCT_READ"
            ),
            message=str(source)[:512] or "Product request failed",
            details=source.details,
        )
        response: dict[str, object] = {
            "request_id": attempt_headers["X-Request-Id"],
            "trace_id": attempt_headers["X-Trace-Id"],
            "status": "FAILED" if status >= 500 else "REJECTED",
            "data": None,
            "error": cast(dict[str, object], plain(contract)),
        }
        response_headers = dict(attempt_headers)
        if status == 429:
            response_headers["Retry-After"] = str(source.details.get("retry_after_seconds", 1))
        elif status == 503:
            response_headers["Retry-After"] = "1"
        self._validator.validate("schemas/common/error-response.schema.json", response)
        self._validator.validate_reference(
            (
                "https://contracts.yaya.local/product-experience/"
                f"product-error-responses-by-status.schema.json#/$defs/status{status}"
            ),
            response,
        )
        return self._encode_response(status, response, response_headers)

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
    def _encode_raw_response(
        status: int,
        body: bytes,
        headers: Mapping[str, str],
    ) -> HttpResponse:
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


__all__ = [
    "ProductHttpApi",
    "ProductReadApplication",
    "ProductSkillDraftApplicationProtocol",
]
