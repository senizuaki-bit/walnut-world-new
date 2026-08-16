"""Provider-neutral identities and states for recoverable LLM dispatch.

``LlmPort.generate`` is intentionally a small, best-effort inference boundary.
It cannot recover a successful remote result after an HTTP acknowledgement is
lost.  Production orchestrators that need that guarantee depend on the
separate ``RecoverableLlmPort`` below.  A conforming adapter addresses a
durable remote dispatch by a client-generated identity and reconciles that
same identity without creating a second logical inference.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, cast, runtime_checkable

from yaya_agent_contracts import (
    Failure,
    LlmReply,
    LlmRequest,
    OperationContext,
    Result,
    Success,
    canonical_json_sha256,
)

from .domain import thaw_value

_SHA256 = re.compile(r"[a-f0-9]{64}")
_DISPATCH_ID = re.compile(r"llmdsp_[a-f0-9]{40}")
_PROTOCOL = "YAYA_RECOVERABLE_LLM_V1"
_LLM_RECOVERY_HASH_PREFIX = b"YAYA_LLM_RECOVERY_HASH_V1\0"
_MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991

LlmDispatchState = Literal["ABSENT", "PENDING", "SUCCEEDED", "FAILED"]


class RecoverableLlmError(RuntimeError):
    """Base error for a Provider-neutral recoverable dispatch boundary."""


class RecoverableLlmProtocolError(RecoverableLlmError):
    """The recovery service violated immutable dispatch semantics."""


class RecoverableLlmConflict(RecoverableLlmProtocolError):
    """One dispatch identity was reused with different immutable bytes."""


class RecoverableLlmExpired(RecoverableLlmProtocolError):
    """A referenced terminal result expired before durable reconciliation."""


class RecoverableLlmUnavailable(RecoverableLlmError):
    """Dispatch acknowledgement or read-only reconciliation is unavailable."""


@dataclass(frozen=True, slots=True)
class LlmDispatchIdentity:
    """Immutable logical Provider call identity, stable across worker fences."""

    dispatch_id: str
    request_sha256: str
    context_sha256: str
    provider: str
    model: str

    def __post_init__(self) -> None:
        if _DISPATCH_ID.fullmatch(self.dispatch_id) is None:
            raise ValueError("dispatch_id must be llmdsp_ followed by 40 lowercase hex digits")
        _require_sha256(self.request_sha256, "request_sha256")
        _require_sha256(self.context_sha256, "context_sha256")
        _require_text(self.provider, "provider", 128)
        _require_text(self.model, "model", 128)


@dataclass(frozen=True, slots=True)
class LlmRelayCapabilities:
    """Verified relay guarantees required before a production dispatch."""

    protocol: str
    result_retention_seconds: int
    max_request_bytes: int
    max_response_bytes: int
    atomic_put_by_dispatch_id: bool
    linearizable_get: bool
    immutable_request_hash: bool
    max_generation_count: int

    def __post_init__(self) -> None:
        if self.protocol != _PROTOCOL:
            raise ValueError("relay protocol is unsupported")
        _require_integer(
            self.result_retention_seconds,
            "result_retention_seconds",
            minimum=1,
            maximum=315_360_000,
        )
        _require_integer(
            self.max_request_bytes,
            "max_request_bytes",
            minimum=1,
            maximum=67_108_864,
        )
        _require_integer(
            self.max_response_bytes,
            "max_response_bytes",
            minimum=1,
            maximum=67_108_864,
        )
        for name in (
            "atomic_put_by_dispatch_id",
            "linearizable_get",
            "immutable_request_hash",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        if not (
            self.atomic_put_by_dispatch_id and self.linearizable_get and self.immutable_request_hash
        ):
            raise ValueError("relay does not provide the required recovery guarantees")
        _require_integer(
            self.max_generation_count,
            "max_generation_count",
            minimum=1,
            maximum=1,
        )


@dataclass(frozen=True, slots=True)
class LlmDispatchResource:
    """Validated relay state for one immutable logical Provider call."""

    identity: LlmDispatchIdentity
    completion_sha256: str
    state: LlmDispatchState
    generation_count: int
    replayed: bool
    result: Result[LlmReply] | None = None
    retry_after_seconds: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    raw_response_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, LlmDispatchIdentity):
            raise TypeError("identity must be an LlmDispatchIdentity")
        _require_sha256(self.completion_sha256, "completion_sha256")
        if self.state not in {"ABSENT", "PENDING", "SUCCEEDED", "FAILED"}:
            raise ValueError("dispatch state is unsupported")
        _require_integer(self.generation_count, "generation_count", minimum=0, maximum=1)
        if not isinstance(self.replayed, bool):
            raise TypeError("replayed must be boolean")
        if self.retry_after_seconds is not None:
            _require_integer(
                self.retry_after_seconds,
                "retry_after_seconds",
                minimum=1,
                maximum=86_400,
            )
        for name in ("created_at", "updated_at"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, datetime) or value.tzinfo is None):
                raise ValueError(f"{name} must be an offset-aware datetime")
        if (
            self.created_at is not None
            and self.updated_at is not None
            and self.updated_at < self.created_at
        ):
            raise ValueError("updated_at cannot precede created_at")
        if self.raw_response_sha256 is not None:
            _require_sha256(self.raw_response_sha256, "raw_response_sha256")

        if self.state in {"ABSENT", "PENDING"}:
            if self.result is not None or self.raw_response_sha256 is not None:
                raise ValueError("non-terminal dispatch cannot contain a result")
            if self.state == "ABSENT" and self.generation_count != 0:
                raise ValueError("absent dispatch cannot report a Provider generation")
            if self.state == "PENDING" and self.retry_after_seconds is None:
                raise ValueError("pending dispatch requires retry_after_seconds")
        elif self.result is None or not isinstance(self.result, (Success, Failure)):
            raise ValueError("terminal dispatch requires a Result[LlmReply]")
        elif self.state == "SUCCEEDED":
            if self.generation_count != 1 or self.raw_response_sha256 is None:
                raise ValueError("successful dispatch requires one raw Provider response")
        elif not isinstance(self.result, Failure):
            raise ValueError("failed dispatch requires a Failure result")


@runtime_checkable
class RecoverableLlmPort(Protocol):
    """A Provider adapter with durable client identity and read reconciliation."""

    async def validate_capabilities(self) -> LlmRelayCapabilities: ...

    async def dispatch(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource: ...

    async def reconcile(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource: ...


def llm_request_sha256(request: LlmRequest) -> str:
    """Hash all logical request fields without Provider credentials."""

    if not isinstance(request, LlmRequest):
        raise TypeError("request must be an LlmRequest")
    messages: list[dict[str, object]] = []
    for message in request.messages:
        item: dict[str, object] = {"role": message.role, "content": message.content}
        if message.name is not None:
            item["name"] = message.name
        if message.tool_call_id is not None:
            item["tool_call_id"] = message.tool_call_id
        messages.append(item)
    versions: dict[str, object] = {}
    for field in fields(request.versions):
        value = getattr(request.versions, field.name)
        if value is not None:
            versions[field.name] = value
    return llm_recovery_sha256(
        {
            "messages": messages,
            "output_schema": thaw_value(request.output_schema),
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
            "timeout_ms": request.timeout_ms,
            "versions": versions,
        }
    )


def llm_recovery_sha256(value: Mapping[str, object]) -> str:
    """Hash LLM recovery state while preserving canonical integer-only vectors.

    The frozen canonical JSON v1 contract intentionally rejects fractional
    numbers. LLM requests legitimately contain them in temperatures and JSON
    Schemas, so this recovery-only domain uses an unambiguous decimal encoding
    when (and only when) canonical JSON v1 cannot encode the value.
    """

    if not isinstance(value, Mapping):
        raise TypeError("LLM recovery hash root must be a mapping")
    try:
        return canonical_json_sha256(value)
    except (TypeError, ValueError):
        encoded = _llm_recovery_json(value).encode("utf-8")
        return hashlib.sha256(_LLM_RECOVERY_HASH_PREFIX + encoded).hexdigest()


def _llm_recovery_json(value: object, field_name: str = "value") -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_JSON_INTEGER:
            raise ValueError(f"{field_name} contains an unsafe JSON integer")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} contains a non-finite float")
        normalized = format(Decimal(str(value)), "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        return "0" if normalized in {"-0", ""} else normalized
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError(f"{field_name} contains a Unicode surrogate")
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if any(not isinstance(key, str) for key in mapping):
            raise TypeError(f"{field_name} contains a non-string object key")
        parts: list[str] = []
        string_mapping = cast(Mapping[str, object], mapping)
        for key in sorted(string_mapping):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in key):
                raise ValueError(f"{field_name} contains a surrogate object key")
            encoded_key = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
            parts.append(
                f"{encoded_key}:{_llm_recovery_json(string_mapping[key], f'{field_name}.{key}')}"
            )
        return "{" + ",".join(parts) + "}"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return (
            "["
            + ",".join(
                _llm_recovery_json(item, f"{field_name}[{index}]")
                for index, item in enumerate(sequence)
            )
            + "]"
        )
    raise TypeError(f"{field_name} contains non-JSON value {type(value).__name__}")


def operation_context_sha256(context: OperationContext) -> str:
    """Hash immutable request authority while keeping identities out of relay bytes."""

    if not isinstance(context, OperationContext):
        raise TypeError("context must be an OperationContext")
    actor = context.actor
    content = context.content_ref
    value: dict[str, object] = {
        "schema_version": context.schema_version,
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "requested_at": context.requested_at.isoformat(),
        "actor": {
            "tenant_id": actor.tenant_id,
            "actor_id": actor.actor_id,
            "actor_type": actor.actor_type.value,
            "roles": list(actor.roles),
        },
        "content_ref": {
            "unit_id": content.unit_id,
            "version": content.version,
            "content_hash": content.content_hash,
        },
        "command_id": context.command_id,
        "causation_id": context.causation_id,
        "deadline_at": context.deadline_at.isoformat() if context.deadline_at else None,
    }
    return canonical_json_sha256(value)


def provider_dispatch_id(
    tenant_id: str,
    job_id: str,
    ordinal: int,
    request_sha256: str,
) -> str:
    """Derive one takeover-stable relay identity; never include a fencing token."""

    _require_text(tenant_id, "tenant_id", 128)
    _require_text(job_id, "job_id", 128)
    _require_integer(ordinal, "ordinal", minimum=1, maximum=999)
    _require_sha256(request_sha256, "request_sha256")
    framed = "\0".join(
        (
            "YAYA_LLM_DISPATCH_V1",
            tenant_id,
            job_id,
            str(ordinal),
            request_sha256,
        )
    ).encode("utf-8")
    return f"llmdsp_{hashlib.sha256(framed).hexdigest()[:40]}"


def _require_sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hex digits")


def _require_text(value: object, name: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"{name} must be bounded printable text")


def _require_integer(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


__all__ = [
    "LlmDispatchIdentity",
    "LlmDispatchResource",
    "LlmDispatchState",
    "LlmRelayCapabilities",
    "RecoverableLlmConflict",
    "RecoverableLlmError",
    "RecoverableLlmExpired",
    "RecoverableLlmPort",
    "RecoverableLlmProtocolError",
    "RecoverableLlmUnavailable",
    "llm_request_sha256",
    "llm_recovery_sha256",
    "operation_context_sha256",
    "provider_dispatch_id",
]
