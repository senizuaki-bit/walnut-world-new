"""Closed byte-level YAYA_RECOVERABLE_LLM_V1 request and resource codec."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from yaya_agent_runtime import llm_recovery_sha256

PROTOCOL = "YAYA_RECOVERABLE_LLM_V1"
CAPABILITIES_PATH = "/v1/llm/capabilities"
DISPATCH_PATH = "/v1/llm/dispatches/"
_DISPATCH_ID = re.compile(r"llmdsp_[a-f0-9]{40}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_REQUEST_FIELDS = {
    "schema_version",
    "dispatch_id",
    "request_sha256",
    "context_sha256",
    "completion_sha256",
    "provider",
    "model",
    "completion",
}
_COMPLETION_FIELDS = {
    "model",
    "messages",
    "temperature",
    "max_tokens",
    "response_format",
    "stream",
}


class RelayProtocolError(ValueError):
    """Caller bytes violate the closed relay protocol."""


class RelayDispatchConflict(RuntimeError):
    """One dispatch identity was reused with different immutable bytes."""


class RelayDispatchExpired(RuntimeError):
    """A terminal resource was scrubbed after its promised retention."""


@dataclass(frozen=True, slots=True)
class RelayPutRequest:
    dispatch_id: str
    request_sha256: str
    context_sha256: str
    completion_sha256: str
    provider: str
    model: str
    completion: dict[str, object]
    body: bytes
    body_sha256: str


RelayState = Literal["PENDING", "SUCCEEDED", "FAILED", "EXPIRED"]


@dataclass(frozen=True, slots=True)
class RelayResource:
    dispatch_id: str
    request_sha256: str
    context_sha256: str
    completion_sha256: str
    provider: str
    model: str
    request_body_sha256: str
    request_body: bytes | None
    state: RelayState
    generation_count: int
    created_at: datetime
    updated_at: datetime
    dispatch_started_at: datetime | None = None
    upstream_deadline_at: datetime | None = None
    response_http_status: int | None = None
    response_content_type: str | None = None
    response_body_sha256: str | None = None
    response_body: bytes | None = None
    failure_code: str | None = None
    failure_retryable: bool | None = None
    terminal_at: datetime | None = None
    expires_at: datetime | None = None

    @property
    def completion(self) -> dict[str, object]:
        if self.request_body is None:
            raise RelayDispatchExpired("dispatch request bytes have expired")
        value = _strict_object(self.request_body, "persisted relay request")
        completion = value.get("completion")
        if not isinstance(completion, Mapping):
            raise RelayProtocolError("persisted completion is not an object")
        return dict(cast(Mapping[str, object], completion))


def parse_put_request(
    dispatch_id: str,
    body: bytes,
    *,
    provider: str,
    model: str,
    maximum_bytes: int,
) -> RelayPutRequest:
    if _DISPATCH_ID.fullmatch(dispatch_id) is None:
        raise RelayProtocolError("dispatch identity is invalid")
    if not body or len(body) > maximum_bytes:
        raise RelayProtocolError("dispatch request size is invalid")
    value = _strict_object(body, "relay request")
    if set(value) != _REQUEST_FIELDS:
        raise RelayProtocolError("dispatch request fields are not closed")
    canonical_body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if body != canonical_body:
        raise RelayProtocolError("dispatch request must use canonical UTF-8 JSON bytes")
    if value.get("schema_version") != "1.0.0" or value.get("dispatch_id") != dispatch_id:
        raise RelayProtocolError("dispatch schema or path identity is invalid")
    request_sha256 = _digest(value, "request_sha256")
    context_sha256 = _digest(value, "context_sha256")
    completion_sha256 = _digest(value, "completion_sha256")
    request_provider = _text(value, "provider", 128)
    request_model = _text(value, "model", 128)
    if request_provider != provider or request_model != model:
        raise RelayProtocolError("Provider or model differs from relay authority")
    raw_completion = value.get("completion")
    if not isinstance(raw_completion, Mapping):
        raise RelayProtocolError("completion must be an object")
    completion = dict(cast(Mapping[str, object], raw_completion))
    _validate_completion(completion, model)
    expected_completion_hash = llm_recovery_sha256(
        {
            "schema_version": "1.0.0",
            "provider": provider,
            "model": model,
            "completion": completion,
        }
    )
    if completion_sha256 != expected_completion_hash:
        raise RelayProtocolError("completion hash differs from immutable completion bytes")
    return RelayPutRequest(
        dispatch_id=dispatch_id,
        request_sha256=request_sha256,
        context_sha256=context_sha256,
        completion_sha256=completion_sha256,
        provider=provider,
        model=model,
        completion=completion,
        body=body,
        body_sha256=hashlib.sha256(body).hexdigest(),
    )


def capabilities_document(
    *,
    retention_seconds: int,
    max_request_bytes: int,
    max_response_bytes: int,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "protocol": PROTOCOL,
        "result_retention_seconds": retention_seconds,
        "max_request_bytes": max_request_bytes,
        "max_response_bytes": max_response_bytes,
        "atomic_put_by_dispatch_id": True,
        "linearizable_get": True,
        "immutable_request_hash": True,
        "max_generation_count": 1,
    }


def resource_document(resource: RelayResource, *, replayed: bool) -> dict[str, object]:
    if resource.state == "EXPIRED":
        raise RelayDispatchExpired("terminal relay resource has expired")
    value: dict[str, object] = {
        "schema_version": "1.0.0",
        "dispatch_id": resource.dispatch_id,
        "request_sha256": resource.request_sha256,
        "context_sha256": resource.context_sha256,
        "completion_sha256": resource.completion_sha256,
        "provider": resource.provider,
        "model": resource.model,
        "state": resource.state,
        "generation_count": resource.generation_count,
        "replayed": replayed,
        "created_at": _timestamp(resource.created_at),
        "updated_at": _timestamp(resource.updated_at),
    }
    if resource.state == "SUCCEEDED":
        if (
            resource.response_http_status is None
            or resource.response_content_type is None
            or resource.response_body is None
            or resource.response_body_sha256 is None
            or hashlib.sha256(resource.response_body).hexdigest()
            != resource.response_body_sha256
        ):
            raise RelayProtocolError("persisted Provider response bytes are corrupt")
        value["provider_response"] = {
            "http_status": resource.response_http_status,
            "content_type": resource.response_content_type,
            "body_base64": base64.b64encode(resource.response_body).decode("ascii"),
            "body_sha256": resource.response_body_sha256,
        }
    elif resource.state == "FAILED":
        if resource.failure_code is None or resource.failure_retryable is None:
            raise RelayProtocolError("persisted relay failure is incomplete")
        value["failure"] = {
            "code": resource.failure_code,
            "retryable": resource.failure_retryable,
        }
    return value


def canonical_response_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def valid_dispatch_id(value: str) -> bool:
    return _DISPATCH_ID.fullmatch(value) is not None


def _validate_completion(value: Mapping[str, object], model: str) -> None:
    allowed = _COMPLETION_FIELDS | {"thinking"}
    if set(value) - allowed or not _COMPLETION_FIELDS <= set(value):
        raise RelayProtocolError("completion fields are not closed")
    if value.get("model") != model or value.get("stream") is not False:
        raise RelayProtocolError("completion model or stream mode is invalid")
    max_tokens = value.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 1_000_000:
        raise RelayProtocolError("completion max_tokens is invalid")
    temperature = value.get("temperature")
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(temperature)
        or not 0 <= temperature <= 2
    ):
        raise RelayProtocolError("completion temperature is invalid")
    messages = value.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 512:
        raise RelayProtocolError("completion messages are invalid")
    for message in messages:
        if not isinstance(message, Mapping):
            raise RelayProtocolError("completion message must be an object")
        item = dict(cast(Mapping[str, object], message))
        if not {"role", "content"} <= set(item) or set(item) - {
            "role",
            "content",
            "name",
            "tool_call_id",
        }:
            raise RelayProtocolError("completion message fields are not closed")
        if item.get("role") not in {"system", "user", "assistant", "tool"}:
            raise RelayProtocolError("completion message role is invalid")
        _text(item, "content", 4_000_000, allow_empty=True)
        for optional in ("name", "tool_call_id"):
            if optional in item:
                _text(item, optional, 256)
    response_format = value.get("response_format")
    if not isinstance(response_format, Mapping):
        raise RelayProtocolError("completion response_format must be an object")
    response_format_value = dict(cast(Mapping[str, object], response_format))
    if response_format_value.get("type") == "json_object":
        if set(response_format_value) != {"type"}:
            raise RelayProtocolError("json_object response_format is not closed")
    elif response_format_value.get("type") == "json_schema":
        if set(response_format_value) != {"type", "json_schema"} or not isinstance(
            response_format_value.get("json_schema"), Mapping
        ):
            raise RelayProtocolError("json_schema response_format is invalid")
    else:
        raise RelayProtocolError("completion response_format is unsupported")
    if "thinking" in value:
        thinking = value["thinking"]
        if not isinstance(thinking, Mapping) or dict(thinking) not in (
            {"type": "enabled"},
            {"type": "disabled"},
        ):
            raise RelayProtocolError("completion thinking mode is invalid")


def _strict_object(data: bytes, label: str) -> dict[str, object]:
    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RelayProtocolError(f"{label} is not strict UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise RelayProtocolError(f"{label} must be an object")
    return dict(cast(Mapping[str, object], value))


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise RelayProtocolError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _digest(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or _SHA256.fullmatch(item) is None:
        raise RelayProtocolError(f"{key} is not a SHA-256 digest")
    return item


def _text(
    value: Mapping[str, object],
    key: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    item = value.get(key)
    minimum = 0 if allow_empty else 1
    if (
        not isinstance(item, str)
        or not minimum <= len(item) <= maximum
        or any(ord(character) < 0x20 and character not in "\n\r\t" for character in item)
        or "\x7f" in item
    ):
        raise RelayProtocolError(f"{key} is not bounded text")
    return item


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise RelayProtocolError("relay timestamp is not offset-aware")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "CAPABILITIES_PATH",
    "DISPATCH_PATH",
    "PROTOCOL",
    "RelayDispatchConflict",
    "RelayDispatchExpired",
    "RelayProtocolError",
    "RelayPutRequest",
    "RelayResource",
    "canonical_response_bytes",
    "capabilities_document",
    "parse_put_request",
    "resource_document",
    "valid_dispatch_id",
]
