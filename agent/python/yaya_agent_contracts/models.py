"""Provider-neutral, deeply immutable domain values for Python 3.12 adapters."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, cast
from urllib.parse import urlsplit

_GENERIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_UNIT_ID = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,79}$")
_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9_-]{8,96}$")
_CORRELATION_ID = re.compile(r"^corr_[A-Za-z0-9_-]{8,96}$")
_TRACE_ID = re.compile(r"^trace_[A-Za-z0-9_-]{8,96}$")
_COMMAND_ID = re.compile(r"^cmd_[A-Za-z0-9_-]{8,96}$")
_EVENT_ID = re.compile(r"^evt_[A-Za-z0-9_-]{8,128}$")
_AUDIT_ID = re.compile(r"^audit_[A-Za-z0-9_-]{8,128}$")
_EVIDENCE_ID = re.compile(r"^evidence_[A-Za-z0-9_-]{8,128}$")
_STREAM_ID = re.compile(r"^[A-Za-z][A-Za-z0-9:_-]{2,159}$")
_ACTOR_TENANT_ID = re.compile(r"^[A-Za-z0-9_-]{3,96}$")
_ACTOR_ID = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
_ROLE = re.compile(r"^[a-z][a-z0-9:_-]{1,63}$")
_LOWER_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")
_UPPER_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_MESSAGE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
_LEARNER_CONCEPT = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SHA256_IMAGE_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_VERSION_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOCALE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
_ACTIVATION_ID = re.compile(r"^activation_[A-Za-z0-9_-]{8,118}$")
_SKILL_VERSION_ID = re.compile(r"^skillver_[A-Za-z0-9_-]{8,118}$")
_CERTIFICATION_ID = re.compile(r"^cert_[A-Za-z0-9_-]{8,122}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_SUBSCRIPTION_ID = re.compile(r"^sub_[A-Za-z0-9_-]{8,96}$")
_HEARTBEAT_NONCE = re.compile(r"^hb_[A-Za-z0-9_-]{8,96}$")
_SOURCE_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9_.\/-]{1,240}$")
_MAX_SOURCE_FILES = 32
_MAX_SOURCE_BYTES = 1_048_576
_MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
_RFC3339_DATE_TIME = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"[Tt]"
    r"(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?)"
    r"(?P<offset>[Zz]|[+-][0-9]{2}:[0-9]{2})"
)
_RFC3986_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_RFC3986_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-"
)
_RFC3986_SUB_DELIMITERS = frozenset("!$&'()*+,;=")
type CommandType = Literal[
    "CREATE_SKILL_BUILD",
    "ACTIVATE_SKILL_VERSION",
    "CREATE_AGENT_SESSION",
    "EXECUTE_AGENT_TURN",
    "INGEST_CLIENT_EVENTS",
]
type DeliveryOperation = Literal["FEISHU_REPORT_DRAFT"]
_COMMAND_TYPES: frozenset[CommandType] = frozenset(
    {
        "CREATE_SKILL_BUILD",
        "ACTIVATE_SKILL_VERSION",
        "CREATE_AGENT_SESSION",
        "EXECUTE_AGENT_TURN",
        "INGEST_CLIENT_EVENTS",
    }
)
_COMMAND_STAGES = frozenset(
    {
        "ACCEPT",
        "VALIDATE",
        "POLICY",
        "REGISTRY",
        "SANDBOX",
        "WORLD_VALIDATE",
        "WORLD_COMMIT",
        "EVIDENCE",
        "COMPLETE",
    }
)


type JsonPrimitive = str | int | float | bool | None
type FrozenJsonValue = (
    JsonPrimitive | Mapping[str, "FrozenJsonValue"] | tuple["FrozenJsonValue", ...]
)
type FrozenJsonObject = Mapping[str, FrozenJsonValue]


def _require_pattern(value: object, pattern: re.Pattern[str], field_name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{field_name} does not match its contract format")
    return value


def _require_identifier(value: object, field_name: str) -> str:
    return _require_pattern(value, _GENERIC_ID, field_name)


def _require_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be timezone-aware")
    offset = value.utcoffset()
    if offset is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if offset.total_seconds() % 60 != 0:
        raise ValueError(f"{field_name} UTC offset must use whole minutes for RFC 3339")
    return value


def _require_integer(
    value: object,
    field_name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} must be at most {maximum}")
    return value


def _require_text(value: object, field_name: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{field_name} length must be between {minimum} and {maximum}")
    return value


def _is_rfc3986_component(
    value: str,
    extra_characters: str = "",
    *,
    allow_percent_encoding: bool = True,
) -> bool:
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            if (
                not allow_percent_encoding
                or index + 2 >= len(value)
                or not all(
                    item in "0123456789ABCDEFabcdef" for item in value[index + 1 : index + 3]
                )
            ):
                return False
            index += 3
            continue
        if (
            character not in _RFC3986_UNRESERVED
            and character not in _RFC3986_SUB_DELIMITERS
            and character not in extra_characters
        ):
            return False
        index += 1
    return True


def _is_rfc3986_ip_literal(value: str) -> bool:
    try:
        ipaddress.IPv6Address(value)
        return True
    except ipaddress.AddressValueError:
        match = re.fullmatch(r"[vV][0-9A-Fa-f]+\.(.+)", value)
        return bool(match) and _is_rfc3986_component(
            match.group(1), ":", allow_percent_encoding=False
        )


def _is_rfc3986_authority(value: str) -> bool:
    if value.count("@") > 1:
        return False
    host_and_port = value
    if "@" in value:
        user_info, host_and_port = value.split("@", 1)
        if not _is_rfc3986_component(user_info, ":"):
            return False
    if host_and_port.startswith("["):
        closing_bracket = host_and_port.find("]")
        if closing_bracket < 0 or not _is_rfc3986_ip_literal(host_and_port[1:closing_bracket]):
            return False
        suffix = host_and_port[closing_bracket + 1 :]
        return suffix == "" or bool(re.fullmatch(r":[0-9]*", suffix))
    if "[" in host_and_port or "]" in host_and_port or host_and_port.count(":") > 1:
        return False
    host, separator, port = host_and_port.rpartition(":")
    if not separator:
        host, port = host_and_port, None
    return _is_rfc3986_component(host) and (port is None or bool(re.fullmatch(r"[0-9]*", port)))


def _is_rfc3986_path(value: str, absolute_uri: bool) -> bool:
    if value.startswith("//"):
        path_index = value.find("/", 2)
        authority = value[2:] if path_index < 0 else value[2:path_index]
        path = "" if path_index < 0 else value[path_index:]
        return _is_rfc3986_authority(authority) and _is_rfc3986_component(path, ":@/")
    if value == "" or value.startswith("/"):
        return _is_rfc3986_component(value, ":@/")
    if not _is_rfc3986_component(value, ":@/"):
        return False
    if absolute_uri:
        return True
    first_segment = value.split("/", 1)[0]
    return bool(first_segment) and _is_rfc3986_component(first_segment, "@")


def _is_rfc3986_reference(value: object, require_scheme: bool = False) -> bool:
    if not isinstance(value, str) or not value.isascii():
        return False
    if value.count("#") > 1:
        return False
    without_fragment, separator, fragment = value.partition("#")
    if separator and not _is_rfc3986_component(fragment, ":@/?"):
        return False
    path_and_authority, separator, query = without_fragment.partition("?")
    if separator and not _is_rfc3986_component(query, ":@/?"):
        return False
    scheme = _RFC3986_SCHEME.match(path_and_authority)
    if require_scheme and scheme is None:
        return False
    path = path_and_authority[scheme.end() :] if scheme else path_and_authority
    return _is_rfc3986_path(path, scheme is not None)


def _require_uri_reference(
    value: object,
    field_name: str,
    minimum: int,
    maximum: int,
) -> str:
    reference = _require_text(value, field_name, minimum, maximum)
    if not _is_rfc3986_reference(reference):
        raise ValueError(f"{field_name} must be an RFC 3986 URI reference")
    return reference


def _require_wss_url(value: object, field_name: str) -> str:
    url = _require_text(value, field_name, 1, 2048)
    parsed = urlsplit(url)
    if (
        not _is_rfc3986_reference(url, require_scheme=True)
        or parsed.scheme != "wss"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{field_name} must be a credential-free wss URL without query or fragment"
        )
    return url


def _require_boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _require_object(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return cast(Mapping[str, Any], value)


def _require_exact_keys(
    value: Mapping[str, Any],
    required: set[str],
    field_name: str,
) -> None:
    actual = set(value)
    if actual != required:
        missing = sorted(required - actual)
        extra = sorted(actual - required)
        raise ValueError(f"{field_name} has missing keys {missing} and extra keys {extra}")


def _require_array(value: object, field_name: str, maximum: int) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be an array")
    items = cast(Sequence[Any], value)
    if len(items) > maximum:
        raise ValueError(f"{field_name} must contain at most {maximum} items")
    return items


def _deep_freeze_json(value: Any, field_name: str = "value") -> FrozenJsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return cast(JsonPrimitive, value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must contain finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJsonValue] = {}
        mapping = cast(Mapping[object, Any], value)
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} must contain only string object keys")
            frozen[key] = _deep_freeze_json(item, f"{field_name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = cast(Sequence[Any], value)
        return tuple(
            _deep_freeze_json(item, f"{field_name}[{index}]") for index, item in enumerate(items)
        )
    raise TypeError(f"{field_name} contains non-JSON value {type(value).__name__}")


def _freeze_mapping(value: Mapping[str, Any], field_name: str) -> FrozenJsonObject:
    frozen = _deep_freeze_json(value, field_name)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    return cast(FrozenJsonObject, frozen)


def _plain_json(value: FrozenJsonValue) -> JsonPrimitive | dict[str, Any] | list[Any]:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _canonical_json_v1(value: FrozenJsonValue, field_name: str = "value") -> str:
    """Encode YAYA_CANONICAL_JSON_V1 identically in Python, JS and Godot."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_JSON_INTEGER:
            raise ValueError(f"{field_name} must contain only safe integer JSON numbers")
        return str(value)
    if isinstance(value, float):
        if (
            not math.isfinite(value)
            or not value.is_integer()
            or abs(value) > _MAX_SAFE_JSON_INTEGER
        ):
            raise ValueError(f"{field_name} must contain only safe integer JSON numbers")
        # Normalizes integral floats, including negative zero, to base-10 integers.
        return str(int(value))
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError(f"{field_name} must contain only Unicode scalar values")
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError(f"{field_name} must contain only string object keys")
            if any(0xD800 <= ord(character) <= 0xDFFF for character in key):
                raise ValueError(
                    f"{field_name} object keys must contain only Unicode scalar values"
                )
            encoded_key = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
            encoded_value = _canonical_json_v1(value[key], f"{field_name}.{key}")
            parts.append(f"{encoded_key}:{encoded_value}")
        return "{" + ",".join(parts) + "}"
    if isinstance(value, tuple):
        return (
            "["
            + ",".join(
                _canonical_json_v1(item, f"{field_name}[{index}]")
                for index, item in enumerate(value)
            )
            + "]"
        )
    raise TypeError(f"{field_name} contains non-JSON value {type(value).__name__}")


def _json_sha256(value: FrozenJsonObject) -> str:
    canonical = _canonical_json_v1(value).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def canonical_json_v1(value: object) -> str:
    """Public encoder for the frozen YAYA_CANONICAL_JSON_V1 contract."""

    return _canonical_json_v1(_deep_freeze_json(value, "value"))


def canonical_json_sha256(value: Mapping[str, object]) -> str:
    """Hash a JSON object with the cross-language canonical JSON contract."""

    return _json_sha256(_freeze_mapping(value, "value"))


def _empty_frozen_json_object() -> FrozenJsonObject:
    return MappingProxyType({})


def _freeze_tuple[T](value: Sequence[T], field_name: str) -> tuple[T, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be an array-like sequence")
    return tuple(value)


class ActorType(StrEnum):
    STUDENT = "student"
    AGENT = "agent"
    TEACHER = "teacher"
    RESEARCHER = "researcher"
    OPERATOR = "operator"
    SERVICE = "service"


class CommandStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    VALIDATING = "VALIDATING"
    RUNNING_SANDBOX = "RUNNING_SANDBOX"
    APPLYING_WORLD = "APPLYING_WORLD"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            CommandStatus.APPLIED,
            CommandStatus.REJECTED,
            CommandStatus.FAILED,
            CommandStatus.UNKNOWN,
            CommandStatus.CANCELLED,
        }


_COMMAND_STATUS_STAGES: Mapping[CommandStatus, frozenset[str]] = MappingProxyType(
    {
        CommandStatus.ACCEPTED: frozenset({"ACCEPT"}),
        CommandStatus.VALIDATING: frozenset({"VALIDATE", "POLICY", "REGISTRY"}),
        CommandStatus.RUNNING_SANDBOX: frozenset({"SANDBOX"}),
        CommandStatus.APPLYING_WORLD: frozenset({"WORLD_VALIDATE", "WORLD_COMMIT"}),
        CommandStatus.APPLIED: frozenset({"COMPLETE"}),
        CommandStatus.UNKNOWN: frozenset({"WORLD_COMMIT"}),
    }
)
_COMMAND_TRANSITIONS: Mapping[CommandStatus, frozenset[CommandStatus]] = MappingProxyType(
    {
        CommandStatus.ACCEPTED: frozenset(
            {
                CommandStatus.VALIDATING,
                CommandStatus.REJECTED,
                CommandStatus.FAILED,
                CommandStatus.CANCELLED,
            }
        ),
        CommandStatus.VALIDATING: frozenset(
            {
                CommandStatus.VALIDATING,
                CommandStatus.RUNNING_SANDBOX,
                CommandStatus.APPLYING_WORLD,
                CommandStatus.APPLIED,
                CommandStatus.REJECTED,
                CommandStatus.FAILED,
                CommandStatus.CANCELLED,
            }
        ),
        CommandStatus.RUNNING_SANDBOX: frozenset(
            {
                CommandStatus.APPLYING_WORLD,
                CommandStatus.APPLIED,
                CommandStatus.REJECTED,
                CommandStatus.FAILED,
                CommandStatus.CANCELLED,
            }
        ),
        CommandStatus.APPLYING_WORLD: frozenset(
            {
                CommandStatus.APPLYING_WORLD,
                CommandStatus.APPLIED,
                CommandStatus.REJECTED,
                CommandStatus.FAILED,
                CommandStatus.UNKNOWN,
                CommandStatus.CANCELLED,
            }
        ),
        CommandStatus.APPLIED: frozenset(),
        CommandStatus.REJECTED: frozenset(),
        CommandStatus.FAILED: frozenset(),
        CommandStatus.UNKNOWN: frozenset(),
        CommandStatus.CANCELLED: frozenset(),
    }
)
_COMMAND_STAGE_INDEX = {
    stage: index
    for index, stage in enumerate(
        (
            "ACCEPT",
            "VALIDATE",
            "POLICY",
            "REGISTRY",
            "SANDBOX",
            "WORLD_VALIDATE",
            "WORLD_COMMIT",
            "EVIDENCE",
            "COMPLETE",
        )
    )
}


class ErrorCategory(StrEnum):
    VALIDATION = "VALIDATION"
    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    POLICY = "POLICY"
    CONCURRENCY = "CONCURRENCY"
    SKILL = "SKILL"
    SANDBOX = "SANDBOX"
    WORLD_RULE = "WORLD_RULE"
    DEPENDENCY = "DEPENDENCY"
    INVARIANT = "INVARIANT"
    RATE_LIMIT = "RATE_LIMIT"
    INTERNAL = "INTERNAL"


_ERROR_CATALOG: Mapping[str, tuple[ErrorCategory, bool, str]] = MappingProxyType(
    {
        "INVALID_REQUEST": (ErrorCategory.VALIDATION, False, "request.invalid"),
        "SCHEMA_VERSION_UNSUPPORTED": (
            ErrorCategory.VALIDATION,
            False,
            "schema.version_unsupported",
        ),
        "CONTENT_VERSION_MISMATCH": (
            ErrorCategory.VALIDATION,
            False,
            "content.version_mismatch",
        ),
        "AUTHENTICATION_REQUIRED": (
            ErrorCategory.AUTHENTICATION,
            False,
            "auth.login_required",
        ),
        "AUTHORIZATION_DENIED": (
            ErrorCategory.AUTHORIZATION,
            False,
            "auth.permission_denied",
        ),
        "POLICY_DENIED": (ErrorCategory.POLICY, False, "policy.action_denied"),
        "NOT_FOUND": (ErrorCategory.VALIDATION, False, "resource.not_found"),
        "PAYLOAD_TOO_LARGE": (
            ErrorCategory.VALIDATION,
            False,
            "request.payload_too_large",
        ),
        "IDEMPOTENCY_KEY_REUSED": (
            ErrorCategory.CONCURRENCY,
            False,
            "request.idempotency_conflict",
        ),
        "WORLD_REVISION_CONFLICT": (
            ErrorCategory.CONCURRENCY,
            True,
            "world.changed_retry",
        ),
        "EVENT_SEQUENCE_GAP": (
            ErrorCategory.CONCURRENCY,
            True,
            "event.resync_required",
        ),
        "SKILL_NOT_CERTIFIED": (ErrorCategory.SKILL, False, "skill.not_certified"),
        "SKILL_VERSION_MISMATCH": (
            ErrorCategory.SKILL,
            False,
            "skill.version_mismatch",
        ),
        "ACTIVE_SKILL_ARTIFACT_MISMATCH": (
            ErrorCategory.INVARIANT,
            False,
            "skill.artifact_mismatch",
        ),
        "SANDBOX_COMPILE_ERROR": (
            ErrorCategory.SANDBOX,
            False,
            "sandbox.compile_error",
        ),
        "SANDBOX_RUNTIME_ERROR": (
            ErrorCategory.SANDBOX,
            False,
            "sandbox.runtime_error",
        ),
        "SANDBOX_RESOURCE_LIMIT": (
            ErrorCategory.SANDBOX,
            False,
            "sandbox.resource_limit",
        ),
        "WORLD_RULE_REJECTED": (
            ErrorCategory.WORLD_RULE,
            False,
            "world.rule_rejected",
        ),
        "DEPENDENCY_UNAVAILABLE": (
            ErrorCategory.DEPENDENCY,
            True,
            "dependency.temporarily_unavailable",
        ),
        "FEISHU_SIGNATURE_INVALID": (
            ErrorCategory.AUTHENTICATION,
            False,
            "feishu.signature_invalid",
        ),
        "FEISHU_REPLAY_DETECTED": (
            ErrorCategory.AUTHENTICATION,
            False,
            "feishu.replay_detected",
        ),
        "FEISHU_SYNC_FAILED": (
            ErrorCategory.DEPENDENCY,
            True,
            "feishu.sync_delayed",
        ),
        "RATE_LIMITED": (ErrorCategory.RATE_LIMIT, True, "request.rate_limited"),
        "UNKNOWN_COMMIT_STATE": (
            ErrorCategory.DEPENDENCY,
            False,
            "command.reconciling",
        ),
        "INVARIANT_VIOLATION": (
            ErrorCategory.INVARIANT,
            False,
            "system.invariant_violation",
        ),
        "INTERNAL_ERROR": (ErrorCategory.INTERNAL, False, "system.internal_error"),
    }
)


class EvidenceType(StrEnum):
    DOMAIN_EVENT = "DOMAIN_EVENT"
    ACTION_LOG = "ACTION_LOG"
    SANDBOX_LOG = "SANDBOX_LOG"
    TEST_REPORT = "TEST_REPORT"
    POLICY_DECISION = "POLICY_DECISION"
    WORLD_COMMIT = "WORLD_COMMIT"
    LEARNER_UPDATE = "LEARNER_UPDATE"
    AUDIT_LOG = "AUDIT_LOG"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    RETRYING = "RETRYING"
    DEAD_LETTER = "DEAD_LETTER"


class LearnerInferenceRole(StrEnum):
    TEACHING_AGENT = "teaching_agent"
    BUG_AGENT = "bug_agent"
    BOOK_AGENT = "book_agent"


class RuntimeEventType(StrEnum):
    COMMAND_ACCEPTED = "command.accepted"
    COMMAND_STAGE_CHANGED = "command.stage_changed"
    COMMAND_TERMINAL = "command.terminal"
    AGENT_TURN_FEEDBACK_READY = "agent.turn.feedback_ready"
    SKILL_BUILD_REQUESTED = "skill.build.requested"
    SKILL_BUILD_STARTED = "skill.build.started"
    SKILL_BUILD_COMPLETED = "skill.build.completed"
    SKILL_BUILD_FAILED = "skill.build.failed"
    SKILL_CERTIFICATION_GRANTED = "skill.certification.granted"
    SKILL_CERTIFICATION_REJECTED = "skill.certification.rejected"
    SKILL_ACTIVATION_APPLIED = "skill.activation.applied"
    SKILL_ACTIVATION_REJECTED = "skill.activation.rejected"
    SANDBOX_RUN_STARTED = "sandbox.run.started"
    SANDBOX_RUN_COMPLETED = "sandbox.run.completed"
    SANDBOX_RUN_FAILED = "sandbox.run.failed"
    WORLD_COMMITTED = "world.committed"
    WORLD_REJECTED = "world.rejected"
    LEARNER_EVIDENCE_RECORDED = "learner.evidence.recorded"
    LEARNER_INFERENCE_RECORDED = "learner.inference.recorded"
    LEARNER_MODEL_UPDATED = "learner.model.updated"
    LEARNER_PROJECTION_FAILED = "learner.projection.failed"
    FEISHU_SYNC_REQUESTED = "feishu.sync.requested"
    FEISHU_SYNC_SUCCEEDED = "feishu.sync.succeeded"
    FEISHU_SYNC_FAILED = "feishu.sync.failed"
    FEISHU_SYNC_DEAD_LETTERED = "feishu.sync.dead_lettered"


_RUNTIME_EVENT_PAYLOAD_FIELDS: Mapping[RuntimeEventType, frozenset[str]] = MappingProxyType(
    {
        RuntimeEventType.COMMAND_ACCEPTED: frozenset({"command_type", "status", "accepted_at"}),
        RuntimeEventType.COMMAND_STAGE_CHANGED: frozenset(
            {"from_status", "to_status", "command_revision", "attempt"}
        ),
        RuntimeEventType.COMMAND_TERMINAL: frozenset(
            {"status", "terminal_at", "result_ref", "error"}
        ),
        RuntimeEventType.AGENT_TURN_FEEDBACK_READY: frozenset(
            {
                "session_id",
                "turn_id",
                "command_id",
                "run_id",
                "message_key",
                "message",
                "source",
                "degraded",
                "fallback_reason",
                "evidence_refs",
                "completed_at",
            }
        ),
        RuntimeEventType.SKILL_BUILD_REQUESTED: frozenset(
            {"build_id", "skill_id", "source_sha256", "compiler_profile", "test_suite_version"}
        ),
        RuntimeEventType.SKILL_BUILD_STARTED: frozenset(
            {"build_id", "worker_id", "attempt", "started_at"}
        ),
        RuntimeEventType.SKILL_BUILD_COMPLETED: frozenset(
            {"build_id", "artifact", "tests", "completed_at"}
        ),
        RuntimeEventType.SKILL_BUILD_FAILED: frozenset({"build_id", "failed_at", "error"}),
        RuntimeEventType.SKILL_CERTIFICATION_GRANTED: frozenset(
            {
                "build_id",
                "certification_id",
                "skill_id",
                "skill_version_id",
                "artifact_sha256",
                "capabilities",
                "certified_at",
            }
        ),
        RuntimeEventType.SKILL_CERTIFICATION_REJECTED: frozenset(
            {"build_id", "skill_id", "rejected_at", "error", "evidence_refs"}
        ),
        RuntimeEventType.SKILL_ACTIVATION_APPLIED: frozenset(
            {
                "skill_id",
                "skill_version_id",
                "certification_id",
                "artifact_sha256",
                "activation_scope",
                "previous_registry_revision",
                "registry_revision",
                "activated_at",
            }
        ),
        RuntimeEventType.SKILL_ACTIVATION_REJECTED: frozenset(
            {
                "skill_version_id",
                "activation_scope",
                "expected_registry_revision",
                "current_registry_revision",
                "rejected_at",
                "error",
            }
        ),
        RuntimeEventType.SANDBOX_RUN_STARTED: frozenset(
            {
                "run_id",
                "skill_version_id",
                "world_id",
                "expected_world_revision",
                "worker_id",
                "started_at",
            }
        ),
        RuntimeEventType.SANDBOX_RUN_COMPLETED: frozenset(
            {"run_id", "exit_code", "action_intents", "finished_at", "evidence_refs"}
        ),
        RuntimeEventType.SANDBOX_RUN_FAILED: frozenset(
            {"run_id", "failed_at", "error", "evidence_refs"}
        ),
        RuntimeEventType.WORLD_COMMITTED: frozenset(
            {
                "commit_id",
                "run_id",
                "world_id",
                "previous_world_revision",
                "world_revision",
                "state_hash",
                "applied_intent_ids",
                "committed_at",
                "evidence_refs",
            }
        ),
        RuntimeEventType.WORLD_REJECTED: frozenset(
            {
                "run_id",
                "world_id",
                "expected_world_revision",
                "current_world_revision",
                "rejected_intent_ids",
                "rejected_at",
                "error",
            }
        ),
        RuntimeEventType.LEARNER_EVIDENCE_RECORDED: frozenset(
            {"learner_id", "evidence_refs", "competency_ids", "recorded_at"}
        ),
        RuntimeEventType.LEARNER_INFERENCE_RECORDED: frozenset(
            {
                "actor",
                "learner_id",
                "session_id",
                "turn_id",
                "command_id",
                "run_id",
                "source_event_id",
                "source_event_sha256",
                "turn_commit_sha256",
                "task_id",
                "teaching_spec_version",
                "role",
                "concept",
                "score_delta",
                "confidence",
                "reason",
                "evidence_refs",
                "inferred_at",
                "inference_sha256",
            }
        ),
        RuntimeEventType.LEARNER_MODEL_UPDATED: frozenset(
            {
                "learner_id",
                "previous_revision",
                "learner_revision",
                "projected_through_sequence",
                "changed_competency_ids",
                "updated_at",
                "evidence_refs",
            }
        ),
        RuntimeEventType.LEARNER_PROJECTION_FAILED: frozenset(
            {"learner_id", "source_event_id", "failed_at", "error"}
        ),
        RuntimeEventType.FEISHU_SYNC_REQUESTED: frozenset(
            {"sync_id", "sync_kind", "target_ref", "attempt", "requested_at"}
        ),
        RuntimeEventType.FEISHU_SYNC_SUCCEEDED: frozenset(
            {"sync_id", "remote_object_id", "attempt", "succeeded_at"}
        ),
        RuntimeEventType.FEISHU_SYNC_FAILED: frozenset(
            {"sync_id", "attempt", "next_attempt_at", "failed_at", "error"}
        ),
        RuntimeEventType.FEISHU_SYNC_DEAD_LETTERED: frozenset(
            {"sync_id", "attempts", "dead_lettered_at", "error"}
        ),
    }
)

_LEARNER_INFERENCE_PAYLOAD_FIELDS = _RUNTIME_EVENT_PAYLOAD_FIELDS[
    RuntimeEventType.LEARNER_INFERENCE_RECORDED
]
_LEARNER_INFERENCE_HASH_SOURCE_FIELDS = _LEARNER_INFERENCE_PAYLOAD_FIELDS - {"inference_sha256"}


def _learner_inference_number_ppm(
    value: object,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite JSON number")
    try:
        exact = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{field_name} must be a finite JSON number") from error
    if not exact.is_finite():
        raise ValueError(f"{field_name} must be a finite JSON number")
    scaled = exact * Decimal(1_000_000)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{field_name} must have at most six decimal places")
    ppm = int(scaled)
    if not minimum <= ppm <= maximum:
        raise ValueError(f"{field_name} is outside its contract bounds")
    return ppm


def learner_inference_sha256(payload: Mapping[str, object]) -> str:
    """Hash a closed inference payload using YAYA_LEARNER_INFERENCE_HASH_V1."""

    actual_fields = frozenset(payload)
    if actual_fields not in {
        _LEARNER_INFERENCE_PAYLOAD_FIELDS,
        _LEARNER_INFERENCE_HASH_SOURCE_FIELDS,
    }:
        missing = sorted(_LEARNER_INFERENCE_HASH_SOURCE_FIELDS - actual_fields)
        extra = sorted(actual_fields - _LEARNER_INFERENCE_PAYLOAD_FIELDS)
        raise ValueError(
            f"learner inference hash payload has missing keys {missing} and extra keys {extra}"
        )
    projection: dict[str, object] = {
        key: value
        for key, value in payload.items()
        if key not in {"inference_sha256", "score_delta", "confidence"}
    }
    projection["score_delta_ppm"] = _learner_inference_number_ppm(
        payload["score_delta"],
        "learner inference score_delta",
        minimum=-300_000,
        maximum=300_000,
    )
    projection["confidence_ppm"] = _learner_inference_number_ppm(
        payload["confidence"],
        "learner inference confidence",
        minimum=0,
        maximum=1_000_000,
    )
    return canonical_json_sha256(projection)


@dataclass(frozen=True, slots=True)
class ContentRef:
    unit_id: str
    version: str
    content_hash: str

    def __post_init__(self) -> None:
        _require_pattern(self.unit_id, _UNIT_ID, "unit_id")
        _require_pattern(self.version, _SEMVER, "version")
        _require_pattern(self.content_hash, _SHA256, "content_hash")


@dataclass(frozen=True, slots=True)
class ActorRef:
    tenant_id: str
    actor_id: str
    actor_type: ActorType
    roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_pattern(self.tenant_id, _ACTOR_TENANT_ID, "tenant_id")
        _require_pattern(self.actor_id, _ACTOR_ID, "actor_id")
        try:
            object.__setattr__(self, "actor_type", ActorType(self.actor_type))
        except ValueError as error:
            raise ValueError("actor_type is not supported") from error
        roles = _freeze_tuple(self.roles, "roles")
        if len(roles) > 16 or len(set(roles)) != len(roles):
            raise ValueError("roles must contain at most 16 unique values")
        for role in roles:
            _require_pattern(role, _ROLE, "roles item")
        object.__setattr__(self, "roles", roles)


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    correlation_id: str
    trace_id: str
    requested_at: datetime
    actor: ActorRef
    content_ref: ContentRef
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _require_pattern(self.request_id, _REQUEST_ID, "request_id")
        _require_pattern(self.correlation_id, _CORRELATION_ID, "correlation_id")
        _require_pattern(self.trace_id, _TRACE_ID, "trace_id")
        _require_datetime(self.requested_at, "requested_at")
        if not isinstance(self.actor, ActorRef):
            raise TypeError("actor must be an ActorRef")
        if not isinstance(self.content_ref, ContentRef):
            raise TypeError("content_ref must be a ContentRef")
        if self.schema_version != "1.0.0":
            raise ValueError("unsupported schema_version")


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationContext(RequestContext):
    command_id: str
    causation_id: str | None
    deadline_at: datetime | None = None

    def __post_init__(self) -> None:
        RequestContext.__post_init__(self)
        _require_pattern(self.command_id, _COMMAND_ID, "command_id")
        if self.causation_id is not None:
            if not (
                _COMMAND_ID.fullmatch(self.causation_id) or _EVENT_ID.fullmatch(self.causation_id)
            ):
                raise ValueError("causation_id must be a command or event identifier")
        if self.deadline_at is not None:
            _require_datetime(self.deadline_at, "deadline_at")
            if self.deadline_at <= self.requested_at:
                raise ValueError("deadline_at must be later than requested_at")


@dataclass(frozen=True, slots=True)
class VersionSet:
    api_version: str
    event_version: str
    policy_version: str
    world_rules_version: str
    teaching_spec_version: str
    skill_version: str | None = None
    artifact_sha256: str | None = None
    compiler_version: str | None = None
    sandbox_image_digest: str | None = None
    test_suite_version: str | None = None
    prompt_version: str | None = None
    model_version: str | None = None

    def __post_init__(self) -> None:
        for name, maximum in (
            ("api_version", 64),
            ("event_version", 64),
            ("policy_version", 96),
            ("world_rules_version", 96),
            ("teaching_spec_version", 96),
        ):
            _require_text(getattr(self, name), name, 1, maximum)
        for name, maximum in (
            ("skill_version", 96),
            ("compiler_version", 96),
            ("sandbox_image_digest", 256),
            ("test_suite_version", 96),
            ("prompt_version", 96),
            ("model_version", 128),
        ):
            value = getattr(self, name)
            if value is not None:
                _require_text(value, name, 1, maximum)
        if self.artifact_sha256 is not None:
            _require_pattern(self.artifact_sha256, _SHA256, "artifact_sha256")


@dataclass(frozen=True, slots=True)
class StudentBootstrapCapabilities:
    skill_builds: bool
    skill_activations: bool
    agent_sessions: bool
    http_world_recovery: bool
    evidence_query: bool

    def __post_init__(self) -> None:
        for field_name in (
            "skill_builds",
            "skill_activations",
            "agent_sessions",
            "http_world_recovery",
            "evidence_query",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be boolean")


@dataclass(frozen=True, slots=True)
class StudentSessionCreateRequest:
    world_id: str
    learner_id: str
    agent_profile_id: str
    channel: Literal["GAME"]
    locale: str
    content: ContentRef
    expected_world_revision: int

    def __post_init__(self) -> None:
        _require_identifier(self.world_id, "world_id")
        _require_identifier(self.learner_id, "learner_id")
        _require_identifier(self.agent_profile_id, "agent_profile_id")
        if self.channel != "GAME":
            raise ValueError("student bootstrap session channel must be GAME")
        _require_pattern(self.locale, _LOCALE, "locale")
        if not isinstance(self.content, ContentRef):
            raise TypeError("content must be a ContentRef")
        _require_integer(self.expected_world_revision, "expected_world_revision", minimum=0)


@dataclass(frozen=True, slots=True)
class StudentBootstrapSession:
    current_session_id: str | None
    teaching_spec_version: str
    create_request: StudentSessionCreateRequest

    def __post_init__(self) -> None:
        if self.current_session_id is not None:
            _require_identifier(self.current_session_id, "current_session_id")
        _require_pattern(
            self.teaching_spec_version,
            _VERSION_IDENTIFIER,
            "teaching_spec_version",
        )
        if not isinstance(self.create_request, StudentSessionCreateRequest):
            raise TypeError("create_request must be a StudentSessionCreateRequest")


@dataclass(frozen=True, slots=True)
class StudentBootstrapBuild:
    build_policy_id: str
    compiler_profile: str
    compiler_version: str
    sandbox_image_digest: str
    test_suite_version: str
    allowed_capabilities: tuple[str, ...]
    max_source_files: Literal[32] = 32
    max_source_bytes: Literal[1048576] = 1_048_576

    def __post_init__(self) -> None:
        for field_name in (
            "build_policy_id",
            "compiler_profile",
            "compiler_version",
            "test_suite_version",
        ):
            _require_pattern(getattr(self, field_name), _VERSION_IDENTIFIER, field_name)
        _require_pattern(
            self.sandbox_image_digest,
            _SHA256_IMAGE_DIGEST,
            "sandbox_image_digest",
        )
        capabilities = _freeze_tuple(self.allowed_capabilities, "allowed_capabilities")
        allowed = {"WORLD_READ", "MOVE", "PLANT", "WATER", "HARVEST", "INTERACT", "SPEAK"}
        if len(capabilities) > 7 or len(set(capabilities)) != len(capabilities):
            raise ValueError("allowed_capabilities must contain at most seven unique values")
        if any(capability not in allowed for capability in capabilities):
            raise ValueError("allowed_capabilities contains an unsupported capability")
        object.__setattr__(self, "allowed_capabilities", capabilities)
        if self.max_source_files != _MAX_SOURCE_FILES:
            raise ValueError("max_source_files must remain 32")
        if self.max_source_bytes != _MAX_SOURCE_BYTES:
            raise ValueError("max_source_bytes must remain 1048576")


@dataclass(frozen=True, slots=True)
class StudentActivationScope:
    world_id: str
    agent_profile_id: str

    def __post_init__(self) -> None:
        _require_identifier(self.world_id, "world_id")
        _require_identifier(self.agent_profile_id, "agent_profile_id")


@dataclass(frozen=True, slots=True)
class StudentActiveSkill:
    activation_id: str
    skill_id: str
    skill_version_id: str
    artifact_sha256: str
    certification_id: str
    registry_revision: int
    activated_at: datetime

    def __post_init__(self) -> None:
        _require_pattern(self.activation_id, _ACTIVATION_ID, "activation_id")
        _require_identifier(self.skill_id, "skill_id")
        _require_pattern(self.skill_version_id, _SKILL_VERSION_ID, "skill_version_id")
        _require_pattern(self.artifact_sha256, _SHA256, "artifact_sha256")
        _require_pattern(self.certification_id, _CERTIFICATION_ID, "certification_id")
        _require_integer(self.registry_revision, "registry_revision", minimum=1)
        _require_datetime(self.activated_at, "activated_at")


@dataclass(frozen=True, slots=True)
class StudentBootstrapActivation:
    scope: StudentActivationScope
    registry_revision: int
    active: StudentActiveSkill | None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, StudentActivationScope):
            raise TypeError("scope must be a StudentActivationScope")
        _require_integer(self.registry_revision, "registry_revision", minimum=0)
        if self.active is not None:
            if not isinstance(self.active, StudentActiveSkill):
                raise TypeError("active must be a StudentActiveSkill or None")
            if self.active.registry_revision != self.registry_revision:
                raise ValueError("active registry_revision must equal activation registry_revision")


@dataclass(frozen=True, slots=True)
class StudentBootstrapWorld:
    world_id: str
    revision: int
    last_event_sequence: int
    state_hash: str
    snapshot_url: str
    events_url: str

    def __post_init__(self) -> None:
        _require_identifier(self.world_id, "world_id")
        _require_integer(self.revision, "revision", minimum=0)
        _require_integer(self.last_event_sequence, "last_event_sequence", minimum=0)
        _require_pattern(self.state_hash, _SHA256, "state_hash")
        _require_uri_reference(self.snapshot_url, "snapshot_url", 1, 2048)
        _require_uri_reference(self.events_url, "events_url", 1, 2048)
        if self.snapshot_url != f"/v1/worlds/{self.world_id}/snapshot":
            raise ValueError("snapshot_url must identify the bootstrap world")
        if self.events_url != f"/v1/worlds/{self.world_id}/events":
            raise ValueError("events_url must identify the bootstrap world")


@dataclass(frozen=True, slots=True)
class StudentBootstrapV2:
    request_context: RequestContext
    server_time: datetime
    actor: ActorRef
    content: ContentRef
    capabilities: StudentBootstrapCapabilities
    session: StudentBootstrapSession
    build: StudentBootstrapBuild
    activation: StudentBootstrapActivation
    world: StudentBootstrapWorld
    api_version: Literal["1.1.0"] = "1.1.0"
    contract_version: Literal["0.4.0"] = "0.4.0"

    def __post_init__(self) -> None:
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")
        _require_datetime(self.server_time, "server_time")
        if not isinstance(self.actor, ActorRef) or not isinstance(self.content, ContentRef):
            raise TypeError("actor and content must use their contract DTOs")
        if self.actor.actor_type is not ActorType.STUDENT:
            raise ValueError("public student bootstrap actor must be a student")
        if (
            self.request_context.actor != self.actor
            or self.request_context.content_ref != self.content
        ):
            raise ValueError("request_context actor/content must equal bootstrap actor/content")
        if not isinstance(self.capabilities, StudentBootstrapCapabilities):
            raise TypeError("capabilities must be StudentBootstrapCapabilities")
        if not isinstance(self.session, StudentBootstrapSession):
            raise TypeError("session must be StudentBootstrapSession")
        if not isinstance(self.build, StudentBootstrapBuild):
            raise TypeError("build must be StudentBootstrapBuild")
        if not isinstance(self.activation, StudentBootstrapActivation):
            raise TypeError("activation must be StudentBootstrapActivation")
        if not isinstance(self.world, StudentBootstrapWorld):
            raise TypeError("world must be StudentBootstrapWorld")
        create_request = self.session.create_request
        if create_request.world_id != self.world.world_id:
            raise ValueError("session create_request world_id must equal bootstrap world_id")
        if create_request.learner_id != self.actor.actor_id:
            raise ValueError("session learner_id must equal bootstrap actor_id")
        if create_request.content != self.content:
            raise ValueError("session create_request content must equal bootstrap content")
        if create_request.expected_world_revision != self.world.revision:
            raise ValueError(
                "session create_request expected_world_revision must equal bootstrap world revision"
            )
        if self.activation.scope.world_id != self.world.world_id:
            raise ValueError("activation scope world_id must equal bootstrap world_id")
        if self.activation.scope.agent_profile_id != create_request.agent_profile_id:
            raise ValueError(
                "activation scope agent_profile_id must equal session agent_profile_id"
            )
        if self.api_version != "1.1.0" or self.contract_version != "0.4.0":
            raise ValueError("student bootstrap version authority drifted")


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    evidence_type: EvidenceType
    created_at: datetime
    sha256: str | None = None
    uri: str | None = None

    def __post_init__(self) -> None:
        _require_pattern(self.evidence_id, _EVIDENCE_ID, "evidence_id")
        object.__setattr__(self, "evidence_type", EvidenceType(self.evidence_type))
        _require_datetime(self.created_at, "created_at")
        if self.sha256 is not None:
            _require_pattern(self.sha256, _SHA256, "sha256")
        if self.uri is not None:
            _require_text(self.uri, "uri", 1, 1024)


@dataclass(frozen=True, slots=True)
class ContractError:
    code: str
    category: ErrorCategory
    retryable: bool
    user_message_key: str
    stage: str
    message: str | None = None
    details: FrozenJsonObject = field(default_factory=_empty_frozen_json_object)
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_pattern(self.code, _ERROR_CODE, "code")
        catalog_entry = _ERROR_CATALOG.get(self.code)
        if catalog_entry is None:
            raise ValueError(f"error code {self.code} is not in the contract catalog")
        try:
            category = ErrorCategory(self.category)
        except ValueError as error:
            raise ValueError("category is not supported") from error
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a boolean")
        _require_pattern(self.user_message_key, _MESSAGE_KEY, "user_message_key")
        actual_metadata = (category, self.retryable, self.user_message_key)
        if actual_metadata != catalog_entry:
            raise ValueError(f"error metadata does not match catalog entry {self.code}")
        object.__setattr__(self, "category", category)
        _require_pattern(self.stage, _UPPER_NAME, "stage")
        if self.message is not None:
            _require_text(self.message, "message", 1, 512)
        evidence_ids = _freeze_tuple(self.evidence_ids, "evidence_ids")
        if len(evidence_ids) > 64 or len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("evidence_ids must contain at most 64 unique values")
        for evidence_id in evidence_ids:
            _require_pattern(evidence_id, _EVIDENCE_ID, "evidence_ids item")
        object.__setattr__(self, "details", _freeze_mapping(self.details, "details"))
        object.__setattr__(self, "evidence_ids", evidence_ids)


@dataclass(frozen=True, slots=True)
class Success[T]:
    value: T

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Failure:
    error: ContractError

    def __post_init__(self) -> None:
        if not isinstance(self.error, ContractError):
            raise TypeError("error must be a ContractError")

    @property
    def ok(self) -> bool:
        return False


type Result[T] = Success[T] | Failure


@dataclass(frozen=True, slots=True)
class PolicyInput:
    actor: ActorRef
    action: str
    resource: FrozenJsonObject
    environment: FrozenJsonObject
    content_ref: ContentRef
    versions: VersionSet

    def __post_init__(self) -> None:
        if not isinstance(self.actor, ActorRef):
            raise TypeError("actor must be an ActorRef")
        if not isinstance(self.content_ref, ContentRef):
            raise TypeError("content_ref must be a ContentRef")
        if not isinstance(self.versions, VersionSet):
            raise TypeError("versions must be a VersionSet")
        _require_text(self.action, "action", 1, 128)
        object.__setattr__(self, "resource", _freeze_mapping(self.resource, "resource"))
        object.__setattr__(
            self,
            "environment",
            _freeze_mapping(self.environment, "environment"),
        )


@dataclass(frozen=True, slots=True)
class PolicyGrant:
    decision_id: str
    policy_version: str
    issued_at: datetime
    expires_at: datetime
    capability_token: str
    constraints: FrozenJsonObject
    allowed: Literal[True] = True

    def __post_init__(self) -> None:
        _require_identifier(self.decision_id, "decision_id")
        _require_text(self.policy_version, "policy_version", 1, 96)
        _require_datetime(self.issued_at, "issued_at")
        _require_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        if self.allowed is not True:
            raise ValueError("PolicyGrant.allowed must be true")
        _require_text(self.capability_token, "capability_token", 1, 4096)
        object.__setattr__(
            self,
            "constraints",
            _freeze_mapping(self.constraints, "constraints"),
        )


@dataclass(frozen=True, slots=True)
class BuildArtifact:
    artifact_sha256: str
    source_sha256: str
    compiler_profile: str
    compiler_version: str
    sandbox_image_digest: str
    test_suite_version: str
    artifact_uri: str

    def __post_init__(self) -> None:
        _require_pattern(self.artifact_sha256, _SHA256, "artifact_sha256")
        _require_pattern(self.source_sha256, _SHA256, "source_sha256")
        for name, maximum in (
            ("compiler_profile", 64),
            ("compiler_version", 96),
            ("sandbox_image_digest", 256),
            ("test_suite_version", 96),
            ("artifact_uri", 1024),
        ):
            _require_text(getattr(self, name), name, 1, maximum)


@dataclass(frozen=True, slots=True)
class TestCaseResult:
    test_case_id: str
    visibility: Literal["PUBLIC", "HIDDEN"]
    status: Literal["PASSED", "FAILED", "ERROR", "TIMEOUT"]
    duration_ms: int
    diagnostic_codes: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        _require_text(self.test_case_id, "test_case_id", 1, 128)
        if self.visibility not in ("PUBLIC", "HIDDEN"):
            raise ValueError("visibility must be PUBLIC or HIDDEN")
        if self.status not in ("PASSED", "FAILED", "ERROR", "TIMEOUT"):
            raise ValueError("status is not a supported test result")
        _require_integer(self.duration_ms, "duration_ms", minimum=0)
        diagnostic_codes = _freeze_tuple(self.diagnostic_codes, "diagnostic_codes")
        if len(diagnostic_codes) > 100 or len(set(diagnostic_codes)) != len(diagnostic_codes):
            raise ValueError("diagnostic_codes must contain at most 100 unique codes")
        for code in diagnostic_codes:
            _require_text(code, "diagnostic_codes item", 1, 96)
        evidence_refs = _freeze_tuple(self.evidence_refs, "evidence_refs")
        if len(evidence_refs) > 64:
            raise ValueError("evidence_refs must contain at most 64 items")
        for index, evidence in enumerate(evidence_refs):
            if not isinstance(evidence, EvidenceRef):
                raise TypeError(f"evidence_refs[{index}] must be an EvidenceRef")
        if len({evidence.evidence_id for evidence in evidence_refs}) != len(evidence_refs):
            raise ValueError("evidence_refs must contain unique evidence_id values")
        object.__setattr__(self, "diagnostic_codes", diagnostic_codes)
        object.__setattr__(self, "evidence_refs", evidence_refs)


@dataclass(frozen=True, slots=True)
class CertificationEvidence:
    build_id: str
    artifact: BuildArtifact
    tests: tuple[TestCaseResult, ...]
    all_required_tests_passed: bool
    evidence_refs: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.build_id, "build_id")
        if not isinstance(self.artifact, BuildArtifact):
            raise TypeError("artifact must be a BuildArtifact")
        tests = _freeze_tuple(self.tests, "tests")
        if not tests:
            raise ValueError("tests must not be empty")
        for index, result in enumerate(tests):
            if not isinstance(result, TestCaseResult):
                raise TypeError(f"tests[{index}] must be a TestCaseResult")
        passed = _require_boolean(self.all_required_tests_passed, "all_required_tests_passed")
        all_passed = all(result.status == "PASSED" for result in tests)
        if passed != all_passed:
            raise ValueError("all_required_tests_passed must agree with every test result")
        evidence_refs = _freeze_tuple(self.evidence_refs, "evidence_refs")
        if len(evidence_refs) > 64:
            raise ValueError("evidence_refs must contain at most 64 items")
        for index, evidence in enumerate(evidence_refs):
            if not isinstance(evidence, EvidenceRef):
                raise TypeError(f"evidence_refs[{index}] must be an EvidenceRef")
        if len({evidence.evidence_id for evidence in evidence_refs}) != len(evidence_refs):
            raise ValueError("evidence_refs must contain unique evidence_id values")
        object.__setattr__(self, "tests", tests)
        object.__setattr__(self, "evidence_refs", evidence_refs)


@dataclass(frozen=True, slots=True)
class CertifiedSkill:
    certification_id: str
    skill_id: str
    skill_version_id: str
    semantic_version: str
    artifact: BuildArtifact
    capabilities: tuple[str, ...]
    certified_at: datetime
    revoked_at: datetime | None
    metadata: FrozenJsonObject = field(default_factory=_empty_frozen_json_object)

    def __post_init__(self) -> None:
        for name in ("certification_id", "skill_id", "skill_version_id"):
            _require_identifier(getattr(self, name), name)
        _require_pattern(self.semantic_version, _SEMVER, "semantic_version")
        if not isinstance(self.artifact, BuildArtifact):
            raise TypeError("artifact must be a BuildArtifact")
        _require_datetime(self.certified_at, "certified_at")
        if self.revoked_at is not None:
            _require_datetime(self.revoked_at, "revoked_at")
            if self.revoked_at < self.certified_at:
                raise ValueError("revoked_at must not precede certified_at")
        capabilities = _freeze_tuple(self.capabilities, "capabilities")
        if len(capabilities) > 64 or len(set(capabilities)) != len(capabilities):
            raise ValueError("capabilities must contain at most 64 unique values")
        for capability in capabilities:
            _require_text(capability, "capabilities item", 1, 64)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class SkillRef:
    skill_id: str
    skill_version_id: str
    artifact_sha256: str
    certification_id: str

    def __post_init__(self) -> None:
        for name in ("skill_id", "skill_version_id", "certification_id"):
            _require_identifier(getattr(self, name), name)
        _require_pattern(self.artifact_sha256, _SHA256, "artifact_sha256")


@dataclass(frozen=True, slots=True)
class ActiveSkill:
    skill: CertifiedSkill
    registry_revision: int
    activated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.skill, CertifiedSkill):
            raise TypeError("skill must be a CertifiedSkill")
        _require_integer(self.registry_revision, "registry_revision", minimum=0)
        _require_datetime(self.activated_at, "activated_at")
        if self.skill.revoked_at is not None:
            raise ValueError("a revoked skill cannot be active")
        if self.activated_at < self.skill.certified_at:
            raise ValueError("activated_at must not precede certified_at")


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    revision: int
    skills: tuple[ActiveSkill, ...]

    def __post_init__(self) -> None:
        _require_integer(self.revision, "revision", minimum=0)
        skills = _freeze_tuple(self.skills, "skills")
        skill_ids: set[str] = set()
        for index, active_skill in enumerate(skills):
            if not isinstance(active_skill, ActiveSkill):
                raise TypeError(f"skills[{index}] must be an ActiveSkill")
            if active_skill.registry_revision > self.revision:
                raise ValueError(f"skills[{index}] revision exceeds snapshot revision")
            skill_id = active_skill.skill.skill_id
            if skill_id in skill_ids:
                raise ValueError(f"skills contains duplicate skill_id {skill_id}")
            skill_ids.add(skill_id)
        object.__setattr__(self, "skills", skills)


@dataclass(frozen=True, slots=True)
class ActivateSkillInput:
    skill_version_id: str
    artifact_sha256: str
    certification_id: str
    expected_registry_revision: int

    def __post_init__(self) -> None:
        _require_identifier(self.skill_version_id, "skill_version_id")
        _require_pattern(self.artifact_sha256, _SHA256, "artifact_sha256")
        _require_identifier(self.certification_id, "certification_id")
        _require_integer(
            self.expected_registry_revision,
            "expected_registry_revision",
            minimum=0,
        )


@dataclass(frozen=True, slots=True)
class WorldPosition:
    x: int
    y: int

    def __post_init__(self) -> None:
        _require_integer(self.x, "x", minimum=-100_000, maximum=100_000)
        _require_integer(self.y, "y", minimum=-100_000, maximum=100_000)


def _validate_intent_identity(
    intent_id: str,
    actor_entity_id: str,
    expected_world_revision: int,
) -> None:
    _require_identifier(intent_id, "intent_id")
    _require_identifier(actor_entity_id, "actor_entity_id")
    _require_integer(
        expected_world_revision,
        "expected_world_revision",
        minimum=0,
    )


@dataclass(frozen=True, slots=True)
class MoveIntent:
    intent_id: str
    actor_entity_id: str
    expected_world_revision: int
    destination: WorldPosition
    action_type: Literal["MOVE"] = field(init=False, default="MOVE")

    def __post_init__(self) -> None:
        _validate_intent_identity(
            self.intent_id,
            self.actor_entity_id,
            self.expected_world_revision,
        )
        if not isinstance(self.destination, WorldPosition):
            raise TypeError("destination must be a WorldPosition")


@dataclass(frozen=True, slots=True)
class PlantIntent:
    intent_id: str
    actor_entity_id: str
    expected_world_revision: int
    plot_id: str
    crop_type: str
    action_type: Literal["PLANT"] = field(init=False, default="PLANT")

    def __post_init__(self) -> None:
        _validate_intent_identity(
            self.intent_id,
            self.actor_entity_id,
            self.expected_world_revision,
        )
        _require_identifier(self.plot_id, "plot_id")
        _require_pattern(self.crop_type, _LOWER_NAME, "crop_type")


@dataclass(frozen=True, slots=True)
class WaterIntent:
    intent_id: str
    actor_entity_id: str
    expected_world_revision: int
    plot_id: str
    amount_ml: int
    action_type: Literal["WATER"] = field(init=False, default="WATER")

    def __post_init__(self) -> None:
        _validate_intent_identity(
            self.intent_id,
            self.actor_entity_id,
            self.expected_world_revision,
        )
        _require_identifier(self.plot_id, "plot_id")
        _require_integer(self.amount_ml, "amount_ml", minimum=1, maximum=10_000)


@dataclass(frozen=True, slots=True)
class HarvestIntent:
    intent_id: str
    actor_entity_id: str
    expected_world_revision: int
    plot_id: str
    action_type: Literal["HARVEST"] = field(init=False, default="HARVEST")

    def __post_init__(self) -> None:
        _validate_intent_identity(
            self.intent_id,
            self.actor_entity_id,
            self.expected_world_revision,
        )
        _require_identifier(self.plot_id, "plot_id")


@dataclass(frozen=True, slots=True)
class InteractIntent:
    intent_id: str
    actor_entity_id: str
    expected_world_revision: int
    target_entity_id: str
    interaction: str
    action_type: Literal["INTERACT"] = field(init=False, default="INTERACT")

    def __post_init__(self) -> None:
        _validate_intent_identity(
            self.intent_id,
            self.actor_entity_id,
            self.expected_world_revision,
        )
        _require_identifier(self.target_entity_id, "target_entity_id")
        _require_pattern(self.interaction, _LOWER_NAME, "interaction")


@dataclass(frozen=True, slots=True)
class SpeakIntent:
    intent_id: str
    actor_entity_id: str
    expected_world_revision: int
    text: str
    audience: Literal["LEARNER", "NEARBY_ENTITIES"]
    action_type: Literal["SPEAK"] = field(init=False, default="SPEAK")

    def __post_init__(self) -> None:
        _validate_intent_identity(
            self.intent_id,
            self.actor_entity_id,
            self.expected_world_revision,
        )
        _require_text(self.text, "text", 1, 500)
        if self.audience not in ("LEARNER", "NEARBY_ENTITIES"):
            raise ValueError("audience is not supported")


type ActionIntent = (
    MoveIntent | PlantIntent | WaterIntent | HarvestIntent | InteractIntent | SpeakIntent
)


def _validate_position(value: object, field_name: str) -> None:
    position = _require_object(value, field_name)
    _require_exact_keys(position, {"x", "y"}, field_name)
    _require_integer(position["x"], f"{field_name}.x", minimum=-100_000, maximum=100_000)
    _require_integer(position["y"], f"{field_name}.y", minimum=-100_000, maximum=100_000)


def _validate_world_state(value: FrozenJsonObject) -> None:
    required = {"clock", "avatar", "inventory", "plots", "agents"}
    _require_exact_keys(value, required, "state")

    clock = _require_object(value["clock"], "state.clock")
    _require_exact_keys(clock, {"day", "minute_of_day", "tick"}, "state.clock")
    _require_integer(clock["day"], "state.clock.day", minimum=1)
    _require_integer(
        clock["minute_of_day"],
        "state.clock.minute_of_day",
        minimum=0,
        maximum=1439,
    )
    _require_integer(clock["tick"], "state.clock.tick", minimum=0)

    avatar = _require_object(value["avatar"], "state.avatar")
    _require_exact_keys(avatar, {"entity_id", "position", "energy"}, "state.avatar")
    _require_identifier(avatar["entity_id"], "state.avatar.entity_id")
    _validate_position(avatar["position"], "state.avatar.position")
    _require_integer(
        avatar["energy"],
        "state.avatar.energy",
        minimum=0,
        maximum=10_000,
    )

    inventory = _require_array(value["inventory"], "state.inventory", 1000)
    for index, raw_item in enumerate(inventory):
        field_name = f"state.inventory[{index}]"
        item = _require_object(raw_item, field_name)
        _require_exact_keys(item, {"item_id", "quantity"}, field_name)
        _require_pattern(item["item_id"], _LOWER_NAME, f"{field_name}.item_id")
        _require_integer(
            item["quantity"],
            f"{field_name}.quantity",
            minimum=0,
            maximum=1_000_000,
        )

    plots = _require_array(value["plots"], "state.plots", 10_000)
    plot_keys = {
        "plot_id",
        "position",
        "soil_state",
        "hydration",
        "crop",
        "last_updated_event_sequence",
    }
    for index, raw_plot in enumerate(plots):
        field_name = f"state.plots[{index}]"
        plot = _require_object(raw_plot, field_name)
        _require_exact_keys(plot, plot_keys, field_name)
        _require_identifier(plot["plot_id"], f"{field_name}.plot_id")
        _validate_position(plot["position"], f"{field_name}.position")
        if plot["soil_state"] not in ("UNTILLED", "TILLED"):
            raise ValueError(f"{field_name}.soil_state is not supported")
        _require_integer(
            plot["hydration"],
            f"{field_name}.hydration",
            minimum=0,
            maximum=10_000,
        )
        crop_value = plot["crop"]
        if crop_value is not None:
            crop = _require_object(crop_value, f"{field_name}.crop")
            crop_keys = {
                "crop_type",
                "growth_stage",
                "planted_at_tick",
                "ready_to_harvest",
            }
            _require_exact_keys(crop, crop_keys, f"{field_name}.crop")
            _require_pattern(
                crop["crop_type"],
                _LOWER_NAME,
                f"{field_name}.crop.crop_type",
            )
            _require_integer(
                crop["growth_stage"],
                f"{field_name}.crop.growth_stage",
                minimum=0,
                maximum=100,
            )
            _require_integer(
                crop["planted_at_tick"],
                f"{field_name}.crop.planted_at_tick",
                minimum=0,
            )
            _require_boolean(
                crop["ready_to_harvest"],
                f"{field_name}.crop.ready_to_harvest",
            )
        _require_integer(
            plot["last_updated_event_sequence"],
            f"{field_name}.last_updated_event_sequence",
            minimum=0,
        )

    agents = _require_array(value["agents"], "state.agents", 256)
    for index, raw_agent in enumerate(agents):
        field_name = f"state.agents[{index}]"
        agent = _require_object(raw_agent, field_name)
        agent_keys = {"entity_id", "agent_profile_id", "position", "activity"}
        _require_exact_keys(agent, agent_keys, field_name)
        _require_identifier(agent["entity_id"], f"{field_name}.entity_id")
        _require_identifier(agent["agent_profile_id"], f"{field_name}.agent_profile_id")
        _validate_position(agent["position"], f"{field_name}.position")
        if agent["activity"] not in ("IDLE", "THINKING", "EXECUTING", "BLOCKED"):
            raise ValueError(f"{field_name}.activity is not supported")


@dataclass(frozen=True, slots=True)
class WorldSnapshot:
    request_context: RequestContext
    world_id: str
    revision: int
    last_event_sequence: int
    state_hash: str
    generated_at: datetime
    world_rules_version: str
    state: FrozenJsonObject
    state_schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")
        _require_identifier(self.world_id, "world_id")
        _require_integer(self.revision, "revision", minimum=0)
        _require_integer(self.last_event_sequence, "last_event_sequence", minimum=0)
        _require_pattern(self.state_hash, _SHA256, "state_hash")
        _require_datetime(self.generated_at, "generated_at")
        _require_text(self.world_rules_version, "world_rules_version", 1, 96)
        if self.state_schema_version != "1.0.0":
            raise ValueError("unsupported state_schema_version")
        state = _freeze_mapping(self.state, "state")
        _validate_world_state(state)
        object.__setattr__(self, "state", state)


@dataclass(frozen=True, slots=True)
class WorldCommand:
    run_id: str
    world_id: str
    expected_world_revision: int
    world_rules_version: str
    skill_ref: SkillRef
    intents: tuple[ActionIntent, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.run_id, "run_id")
        _require_identifier(self.world_id, "world_id")
        _require_integer(
            self.expected_world_revision,
            "expected_world_revision",
            minimum=0,
        )
        _require_text(self.world_rules_version, "world_rules_version", 1, 96)
        if not isinstance(self.skill_ref, SkillRef):
            raise TypeError("skill_ref must be a SkillRef")
        intents = _freeze_tuple(self.intents, "intents")
        if not intents:
            raise ValueError("world command must contain at least one intent")
        intent_types = (
            MoveIntent,
            PlantIntent,
            WaterIntent,
            HarvestIntent,
            InteractIntent,
            SpeakIntent,
        )
        intent_ids: set[str] = set()
        for index, intent in enumerate(intents):
            if not isinstance(intent, intent_types):
                raise TypeError(f"intents[{index}] must be an ActionIntent")
            if intent.expected_world_revision != self.expected_world_revision:
                raise ValueError(f"intents[{index}].expected_world_revision does not match command")
            if intent.intent_id in intent_ids:
                raise ValueError(f"intents contains duplicate intent_id {intent.intent_id}")
            intent_ids.add(intent.intent_id)
        object.__setattr__(self, "intents", intents)


@dataclass(frozen=True, slots=True)
class WorldCommitReceipt:
    world_id: str
    previous_revision: int
    world_revision: int
    first_event_sequence: int
    last_event_sequence: int
    committed_at: datetime
    state_hash: str

    def __post_init__(self) -> None:
        _require_identifier(self.world_id, "world_id")
        _require_integer(self.previous_revision, "previous_revision", minimum=0)
        _require_integer(self.world_revision, "world_revision", minimum=1)
        if self.world_revision != self.previous_revision + 1:
            raise ValueError("world commit must advance exactly one revision")
        _require_integer(self.first_event_sequence, "first_event_sequence", minimum=1)
        _require_integer(self.last_event_sequence, "last_event_sequence", minimum=1)
        if self.last_event_sequence < self.first_event_sequence:
            raise ValueError("invalid committed event sequence range")
        _require_datetime(self.committed_at, "committed_at")
        _require_pattern(self.state_hash, _SHA256, "state_hash")


@dataclass(frozen=True, slots=True)
class DomainEvent[P: Mapping[str, Any]]:
    event_id: str
    event_type: str
    event_version: int
    stream_id: str
    sequence: int
    occurred_at: datetime
    producer: str
    trace_id: str
    command_id: str
    correlation_id: str
    causation_id: str | None
    content_ref: ContentRef
    payload: P
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _require_pattern(self.event_id, _EVENT_ID, "event_id")
        _require_pattern(self.event_type, re.compile(r"^[a-z][a-z0-9_.-]{2,127}$"), "event_type")
        _require_integer(self.event_version, "event_version", minimum=1)
        _require_pattern(self.stream_id, _STREAM_ID, "stream_id")
        _require_integer(self.sequence, "sequence", minimum=1)
        _require_datetime(self.occurred_at, "occurred_at")
        _require_pattern(self.producer, re.compile(r"^[a-z][a-z0-9_-]{2,63}$"), "producer")
        _require_pattern(self.trace_id, _TRACE_ID, "trace_id")
        _require_pattern(self.command_id, _COMMAND_ID, "command_id")
        _require_pattern(self.correlation_id, _CORRELATION_ID, "correlation_id")
        if self.causation_id is not None and not (
            _EVENT_ID.fullmatch(self.causation_id) or _COMMAND_ID.fullmatch(self.causation_id)
        ):
            raise ValueError("causation_id must be a command or event identifier")
        if self.schema_version not in {"1.0.0", "2.0.0"}:
            raise ValueError("unsupported event schema_version")
        if not isinstance(self.content_ref, ContentRef):
            raise TypeError("content_ref must be a ContentRef")
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "payload"))


def _require_iso_datetime(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an RFC 3339 date-time string")
    match = _RFC3339_DATE_TIME.fullmatch(value)
    if match is None:
        raise ValueError(f"{field_name} must be an RFC 3339 date-time string")
    offset = match.group("offset")
    normalized_offset = "+00:00" if offset in {"Z", "z"} else offset
    try:
        parsed = datetime.fromisoformat(
            f"{match.group('date')}T{match.group('time')}{normalized_offset}"
        )
    except ValueError as error:
        raise ValueError(f"{field_name} must be an RFC 3339 date-time string") from error
    if parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be an RFC 3339 date-time string")


def _require_unique_array(value: object, field_name: str, maximum: int) -> Sequence[Any]:
    items = _require_array(value, field_name, maximum)
    for index, item in enumerate(items):
        if item in items[:index]:
            raise ValueError(f"{field_name} must contain unique items")
    return items


def _validate_contract_error_object(value: object, field_name: str) -> None:
    error = _require_object(value, field_name)
    required = {"code", "category", "retryable", "user_message_key", "stage"}
    allowed = required | {"message", "details", "evidence_ids"}
    actual = set(error)
    if not required <= actual or not actual <= allowed:
        raise ValueError(f"{field_name} has invalid fields")
    _require_pattern(error["code"], _ERROR_CODE, f"{field_name}.code")
    catalog = _ERROR_CATALOG.get(error["code"])
    if catalog is None:
        raise ValueError(f"{field_name}.code is not in the contract catalog")
    try:
        category = ErrorCategory(error["category"])
    except (TypeError, ValueError) as exception:
        raise ValueError(f"{field_name}.category is not supported") from exception
    _require_boolean(error["retryable"], f"{field_name}.retryable")
    _require_pattern(error["user_message_key"], _MESSAGE_KEY, f"{field_name}.user_message_key")
    if (category, error["retryable"], error["user_message_key"]) != catalog:
        raise ValueError(f"{field_name} metadata does not match its catalog entry")
    _require_pattern(error["stage"], _UPPER_NAME, f"{field_name}.stage")
    if "message" in error:
        _require_text(error["message"], f"{field_name}.message", 1, 512)
    if "details" in error:
        _require_object(error["details"], f"{field_name}.details")
    if "evidence_ids" in error:
        evidence_ids = _require_unique_array(
            error["evidence_ids"], f"{field_name}.evidence_ids", 64
        )
        for evidence_id in evidence_ids:
            _require_pattern(evidence_id, _EVIDENCE_ID, f"{field_name}.evidence_ids[]")


def _validate_evidence_ref_object(value: object, field_name: str) -> None:
    evidence = _require_object(value, field_name)
    required = {"evidence_id", "evidence_type", "created_at"}
    allowed = required | {"sha256", "uri"}
    actual = set(evidence)
    if not required <= actual or not actual <= allowed:
        raise ValueError(f"{field_name} has invalid fields")
    _require_pattern(evidence["evidence_id"], _EVIDENCE_ID, f"{field_name}.evidence_id")
    try:
        EvidenceType(evidence["evidence_type"])
    except (TypeError, ValueError) as exception:
        raise ValueError(f"{field_name}.evidence_type is not supported") from exception
    _require_iso_datetime(evidence["created_at"], f"{field_name}.created_at")
    if "sha256" in evidence:
        _require_pattern(evidence["sha256"], _SHA256, f"{field_name}.sha256")
    if "uri" in evidence:
        _require_text(evidence["uri"], f"{field_name}.uri", 1, 1024)


def _validate_evidence_refs(value: object, field_name: str) -> None:
    evidence_refs = _require_unique_array(value, field_name, 64)
    for index, evidence in enumerate(evidence_refs):
        _validate_evidence_ref_object(evidence, f"{field_name}[{index}]")


def _validate_actor_ref_object(value: object, field_name: str) -> Mapping[str, Any]:
    actor = _require_object(value, field_name)
    _require_exact_keys(actor, {"tenant_id", "actor_id", "actor_type", "roles"}, field_name)
    _require_pattern(actor["tenant_id"], _ACTOR_TENANT_ID, f"{field_name}.tenant_id")
    _require_pattern(actor["actor_id"], _ACTOR_ID, f"{field_name}.actor_id")
    try:
        ActorType(actor["actor_type"])
    except (TypeError, ValueError) as exception:
        raise ValueError(f"{field_name}.actor_type is not supported") from exception
    roles = _require_unique_array(actor["roles"], f"{field_name}.roles", 16)
    for role in roles:
        _require_pattern(role, _ROLE, f"{field_name}.roles[]")
    return actor


def _validate_learner_inference_evidence_refs(value: object, field_name: str) -> None:
    evidence_refs = _require_array(value, field_name, 16)
    if not evidence_refs:
        raise ValueError(f"{field_name} must contain at least one item")
    evidence_ids: list[str] = []
    for index, value_ref in enumerate(evidence_refs):
        item_name = f"{field_name}[{index}]"
        _validate_evidence_ref_object(value_ref, item_name)
        evidence = _require_object(value_ref, item_name)
        if "sha256" not in evidence:
            raise ValueError(f"{item_name}.sha256 is required")
        evidence_ids.append(cast(str, evidence["evidence_id"]))
    if evidence_ids != sorted(evidence_ids) or len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError(f"{field_name} must be strictly sorted by unique evidence_id")


def _validate_feedback_discriminator(
    source: object,
    degraded: object,
    fallback_reason: object,
    field_name: str,
) -> None:
    is_degraded = _require_boolean(degraded, f"{field_name}.degraded")
    if is_degraded:
        if source != "provider_fallback" or fallback_reason is None:
            raise ValueError(
                f"{field_name} degraded feedback must use provider_fallback "
                "source and carry one fallback reason"
            )
        _require_pattern(fallback_reason, _ERROR_CODE, f"{field_name}.fallback_reason")
    elif source != "provider" or fallback_reason is not None:
        raise ValueError(
            f"{field_name} non-degraded feedback must use provider source without a fallback reason"
        )


def _validate_runtime_build_artifact(value: object, field_name: str) -> None:
    artifact = _require_object(value, field_name)
    required = {
        "artifact_sha256",
        "source_sha256",
        "compiler_profile",
        "compiler_version",
        "sandbox_image_digest",
        "test_suite_version",
        "artifact_uri",
    }
    _require_exact_keys(artifact, required, field_name)
    _require_pattern(artifact["artifact_sha256"], _SHA256, f"{field_name}.artifact_sha256")
    _require_pattern(artifact["source_sha256"], _SHA256, f"{field_name}.source_sha256")
    for name, maximum in (
        ("compiler_profile", 64),
        ("compiler_version", 96),
        ("sandbox_image_digest", 256),
        ("test_suite_version", 96),
        ("artifact_uri", 1024),
    ):
        _require_text(artifact[name], f"{field_name}.{name}", 1, maximum)


def _validate_runtime_test_result(value: object, field_name: str) -> None:
    result = _require_object(value, field_name)
    _require_exact_keys(
        result,
        {
            "test_case_id",
            "visibility",
            "status",
            "duration_ms",
            "diagnostic_codes",
            "evidence_refs",
        },
        field_name,
    )
    _require_text(result["test_case_id"], f"{field_name}.test_case_id", 1, 128)
    if result["visibility"] not in ("PUBLIC", "HIDDEN"):
        raise ValueError(f"{field_name}.visibility is not supported")
    if result["status"] not in ("PASSED", "FAILED", "ERROR", "TIMEOUT"):
        raise ValueError(f"{field_name}.status is not supported")
    _require_integer(result["duration_ms"], f"{field_name}.duration_ms", minimum=0)
    diagnostics = _require_unique_array(
        result["diagnostic_codes"],
        f"{field_name}.diagnostic_codes",
        100,
    )
    for diagnostic in diagnostics:
        _require_text(diagnostic, f"{field_name}.diagnostic_codes[]", 1, 96)
    _validate_evidence_refs(result["evidence_refs"], f"{field_name}.evidence_refs")


def _validate_activation_scope(value: object, field_name: str) -> None:
    scope = _require_object(value, field_name)
    _require_exact_keys(scope, {"world_id", "agent_profile_id"}, field_name)
    _require_identifier(scope["world_id"], f"{field_name}.world_id")
    _require_identifier(scope["agent_profile_id"], f"{field_name}.agent_profile_id")


def _validate_runtime_action_intent(value: object, field_name: str) -> None:
    intent = _require_object(value, field_name)
    action_type = intent.get("action_type")
    common = {"intent_id", "action_type", "actor_entity_id", "expected_world_revision"}
    variants: dict[str, set[str]] = {
        "MOVE": {"destination"},
        "PLANT": {"plot_id", "crop_type"},
        "WATER": {"plot_id", "amount_ml"},
        "HARVEST": {"plot_id"},
        "INTERACT": {"target_entity_id", "interaction"},
        "SPEAK": {"text", "audience"},
    }
    if not isinstance(action_type, str) or action_type not in variants:
        raise ValueError(f"{field_name}.action_type is not supported")
    _require_exact_keys(intent, common | variants[action_type], field_name)
    _require_identifier(intent["intent_id"], f"{field_name}.intent_id")
    _require_identifier(intent["actor_entity_id"], f"{field_name}.actor_entity_id")
    _require_integer(
        intent["expected_world_revision"],
        f"{field_name}.expected_world_revision",
        minimum=0,
    )
    if action_type == "MOVE":
        _validate_position(intent["destination"], f"{field_name}.destination")
    elif action_type in ("PLANT", "WATER", "HARVEST"):
        _require_identifier(intent["plot_id"], f"{field_name}.plot_id")
        if action_type == "PLANT":
            _require_pattern(intent["crop_type"], _LOWER_NAME, f"{field_name}.crop_type")
        elif action_type == "WATER":
            _require_integer(
                intent["amount_ml"], f"{field_name}.amount_ml", minimum=1, maximum=10_000
            )
    elif action_type == "INTERACT":
        _require_identifier(intent["target_entity_id"], f"{field_name}.target_entity_id")
        _require_pattern(intent["interaction"], _LOWER_NAME, f"{field_name}.interaction")
    else:
        _require_text(intent["text"], f"{field_name}.text", 1, 500)
        if intent["audience"] not in ("LEARNER", "NEARBY_ENTITIES"):
            raise ValueError(f"{field_name}.audience is not supported")


def _validate_runtime_event_payload(
    event_type: RuntimeEventType,
    payload: Mapping[str, Any],
) -> None:
    label = f"{event_type}.payload"
    identifier_fields: Mapping[RuntimeEventType, tuple[str, ...]] = {
        RuntimeEventType.AGENT_TURN_FEEDBACK_READY: ("session_id", "turn_id"),
        RuntimeEventType.SKILL_BUILD_REQUESTED: ("build_id", "skill_id"),
        RuntimeEventType.SKILL_BUILD_STARTED: ("build_id", "worker_id"),
        RuntimeEventType.SKILL_BUILD_COMPLETED: ("build_id",),
        RuntimeEventType.SKILL_BUILD_FAILED: ("build_id",),
        RuntimeEventType.SKILL_CERTIFICATION_GRANTED: (
            "build_id",
            "certification_id",
            "skill_id",
            "skill_version_id",
        ),
        RuntimeEventType.SKILL_CERTIFICATION_REJECTED: ("build_id", "skill_id"),
        RuntimeEventType.SKILL_ACTIVATION_APPLIED: (
            "skill_id",
            "skill_version_id",
            "certification_id",
        ),
        RuntimeEventType.SKILL_ACTIVATION_REJECTED: ("skill_version_id",),
        RuntimeEventType.SANDBOX_RUN_STARTED: (
            "run_id",
            "skill_version_id",
            "world_id",
            "worker_id",
        ),
        RuntimeEventType.SANDBOX_RUN_COMPLETED: ("run_id",),
        RuntimeEventType.SANDBOX_RUN_FAILED: ("run_id",),
        RuntimeEventType.WORLD_COMMITTED: ("commit_id", "run_id", "world_id"),
        RuntimeEventType.WORLD_REJECTED: ("run_id", "world_id"),
        RuntimeEventType.LEARNER_EVIDENCE_RECORDED: ("learner_id",),
        RuntimeEventType.LEARNER_INFERENCE_RECORDED: (
            "learner_id",
            "session_id",
            "turn_id",
            "task_id",
        ),
        RuntimeEventType.LEARNER_MODEL_UPDATED: ("learner_id",),
        RuntimeEventType.LEARNER_PROJECTION_FAILED: ("learner_id",),
        RuntimeEventType.FEISHU_SYNC_REQUESTED: ("sync_id",),
        RuntimeEventType.FEISHU_SYNC_SUCCEEDED: ("sync_id",),
        RuntimeEventType.FEISHU_SYNC_FAILED: ("sync_id",),
        RuntimeEventType.FEISHU_SYNC_DEAD_LETTERED: ("sync_id",),
    }
    for field_name in identifier_fields.get(event_type, ()):
        _require_identifier(payload[field_name], f"{label}.{field_name}")

    timestamp_fields: Mapping[RuntimeEventType, tuple[str, ...]] = {
        RuntimeEventType.AGENT_TURN_FEEDBACK_READY: ("completed_at",),
        RuntimeEventType.COMMAND_ACCEPTED: ("accepted_at",),
        RuntimeEventType.COMMAND_TERMINAL: ("terminal_at",),
        RuntimeEventType.SKILL_BUILD_STARTED: ("started_at",),
        RuntimeEventType.SKILL_BUILD_COMPLETED: ("completed_at",),
        RuntimeEventType.SKILL_BUILD_FAILED: ("failed_at",),
        RuntimeEventType.SKILL_CERTIFICATION_GRANTED: ("certified_at",),
        RuntimeEventType.SKILL_CERTIFICATION_REJECTED: ("rejected_at",),
        RuntimeEventType.SKILL_ACTIVATION_APPLIED: ("activated_at",),
        RuntimeEventType.SKILL_ACTIVATION_REJECTED: ("rejected_at",),
        RuntimeEventType.SANDBOX_RUN_STARTED: ("started_at",),
        RuntimeEventType.SANDBOX_RUN_COMPLETED: ("finished_at",),
        RuntimeEventType.SANDBOX_RUN_FAILED: ("failed_at",),
        RuntimeEventType.WORLD_COMMITTED: ("committed_at",),
        RuntimeEventType.WORLD_REJECTED: ("rejected_at",),
        RuntimeEventType.LEARNER_EVIDENCE_RECORDED: ("recorded_at",),
        RuntimeEventType.LEARNER_INFERENCE_RECORDED: ("inferred_at",),
        RuntimeEventType.LEARNER_MODEL_UPDATED: ("updated_at",),
        RuntimeEventType.LEARNER_PROJECTION_FAILED: ("failed_at",),
        RuntimeEventType.FEISHU_SYNC_REQUESTED: ("requested_at",),
        RuntimeEventType.FEISHU_SYNC_SUCCEEDED: ("succeeded_at",),
        RuntimeEventType.FEISHU_SYNC_FAILED: ("failed_at",),
        RuntimeEventType.FEISHU_SYNC_DEAD_LETTERED: ("dead_lettered_at",),
    }
    for field_name in timestamp_fields.get(event_type, ()):
        _require_iso_datetime(payload[field_name], f"{label}.{field_name}")

    error_events = {
        RuntimeEventType.SKILL_BUILD_FAILED,
        RuntimeEventType.SKILL_CERTIFICATION_REJECTED,
        RuntimeEventType.SKILL_ACTIVATION_REJECTED,
        RuntimeEventType.SANDBOX_RUN_FAILED,
        RuntimeEventType.WORLD_REJECTED,
        RuntimeEventType.LEARNER_PROJECTION_FAILED,
        RuntimeEventType.FEISHU_SYNC_FAILED,
        RuntimeEventType.FEISHU_SYNC_DEAD_LETTERED,
    }
    if event_type in error_events:
        _validate_contract_error_object(payload["error"], f"{label}.error")

    evidence_events = {
        RuntimeEventType.AGENT_TURN_FEEDBACK_READY,
        RuntimeEventType.SKILL_CERTIFICATION_REJECTED,
        RuntimeEventType.SANDBOX_RUN_COMPLETED,
        RuntimeEventType.SANDBOX_RUN_FAILED,
        RuntimeEventType.WORLD_COMMITTED,
        RuntimeEventType.LEARNER_EVIDENCE_RECORDED,
        RuntimeEventType.LEARNER_MODEL_UPDATED,
    }
    if event_type in evidence_events:
        _validate_evidence_refs(payload["evidence_refs"], f"{label}.evidence_refs")

    if event_type is RuntimeEventType.AGENT_TURN_FEEDBACK_READY:
        _require_pattern(payload["command_id"], _COMMAND_ID, f"{label}.command_id")
        if payload["run_id"] is not None:
            _require_identifier(payload["run_id"], f"{label}.run_id")
        _require_pattern(payload["message_key"], _MESSAGE_KEY, f"{label}.message_key")
        _require_text(payload["message"], f"{label}.message", 1, 4000)
        _validate_feedback_discriminator(
            payload["source"],
            payload["degraded"],
            payload["fallback_reason"],
            label,
        )
    elif event_type is RuntimeEventType.COMMAND_ACCEPTED:
        if payload["command_type"] not in _COMMAND_TYPES or payload["status"] != "ACCEPTED":
            raise ValueError(f"{label} command_type or status is invalid")
    elif event_type is RuntimeEventType.COMMAND_STAGE_CHANGED:
        statuses: dict[str, CommandStatus] = {}
        for field_name in ("from_status", "to_status"):
            try:
                statuses[field_name] = CommandStatus(payload[field_name])
            except (TypeError, ValueError) as exception:
                raise ValueError(f"{label}.{field_name} is invalid") from exception
        from_status = statuses["from_status"]
        to_status = statuses["to_status"]
        if to_status is from_status:
            raise ValueError(f"{label} must change status")
        if to_status not in _COMMAND_TRANSITIONS[from_status]:
            raise ValueError(f"{label} contains an invalid command status transition")
        _require_integer(payload["command_revision"], f"{label}.command_revision", minimum=1)
        _require_integer(payload["attempt"], f"{label}.attempt", minimum=1)
    elif event_type is RuntimeEventType.COMMAND_TERMINAL:
        try:
            status = CommandStatus(payload["status"])
        except (TypeError, ValueError) as exception:
            raise ValueError(f"{label}.status is invalid") from exception
        if status not in {
            CommandStatus.APPLIED,
            CommandStatus.REJECTED,
            CommandStatus.FAILED,
            CommandStatus.UNKNOWN,
            CommandStatus.CANCELLED,
        }:
            raise ValueError(f"{label}.status is not terminal")
        error_value = payload["error"]
        if status is CommandStatus.APPLIED:
            _require_text(payload["result_ref"], f"{label}.result_ref", 1, 1024)
            if error_value is not None:
                raise ValueError(f"{label}.error must be null for APPLIED")
        elif status in {CommandStatus.REJECTED, CommandStatus.FAILED, CommandStatus.UNKNOWN}:
            if payload["result_ref"] is not None:
                raise ValueError(f"{label}.result_ref must be null")
            _validate_contract_error_object(error_value, f"{label}.error")
            if status is CommandStatus.UNKNOWN:
                error = _require_object(error_value, f"{label}.error")
                if error["code"] != "UNKNOWN_COMMIT_STATE" or error["stage"] != "WORLD_COMMIT":
                    raise ValueError(f"{label}.UNKNOWN must bind UNKNOWN_COMMIT_STATE")
        else:
            if payload["result_ref"] is not None:
                raise ValueError(f"{label}.result_ref must be null for CANCELLED")
            if error_value is not None:
                _validate_contract_error_object(error_value, f"{label}.error")
    elif event_type is RuntimeEventType.SKILL_BUILD_REQUESTED:
        _require_pattern(payload["source_sha256"], _SHA256, f"{label}.source_sha256")
        _require_text(payload["compiler_profile"], f"{label}.compiler_profile", 1, 64)
        _require_text(payload["test_suite_version"], f"{label}.test_suite_version", 1, 96)
    elif event_type is RuntimeEventType.SKILL_BUILD_STARTED:
        _require_integer(payload["attempt"], f"{label}.attempt", minimum=1)
    elif event_type is RuntimeEventType.SKILL_BUILD_COMPLETED:
        _validate_runtime_build_artifact(payload["artifact"], f"{label}.artifact")
        tests = _require_array(payload["tests"], f"{label}.tests", 2**31 - 1)
        if not tests:
            raise ValueError(f"{label}.tests must not be empty")
        for index, test_result in enumerate(tests):
            _validate_runtime_test_result(test_result, f"{label}.tests[{index}]")
    elif event_type is RuntimeEventType.SKILL_CERTIFICATION_GRANTED:
        _require_pattern(payload["artifact_sha256"], _SHA256, f"{label}.artifact_sha256")
        capabilities = _require_unique_array(
            payload["capabilities"], f"{label}.capabilities", 2**31 - 1
        )
        for capability in capabilities:
            _require_text(capability, f"{label}.capabilities[]", 1, 64)
    elif event_type is RuntimeEventType.SKILL_ACTIVATION_APPLIED:
        _require_pattern(payload["artifact_sha256"], _SHA256, f"{label}.artifact_sha256")
        _validate_activation_scope(payload["activation_scope"], f"{label}.activation_scope")
        _require_integer(
            payload["previous_registry_revision"], f"{label}.previous_registry_revision", minimum=0
        )
        _require_integer(payload["registry_revision"], f"{label}.registry_revision", minimum=1)
        if payload["registry_revision"] != payload["previous_registry_revision"] + 1:
            raise ValueError(f"{label} must advance exactly one registry revision")
    elif event_type is RuntimeEventType.SKILL_ACTIVATION_REJECTED:
        _validate_activation_scope(payload["activation_scope"], f"{label}.activation_scope")
        _require_integer(
            payload["expected_registry_revision"], f"{label}.expected_registry_revision", minimum=0
        )
        _require_integer(
            payload["current_registry_revision"], f"{label}.current_registry_revision", minimum=0
        )
    elif event_type is RuntimeEventType.SANDBOX_RUN_STARTED:
        _require_integer(
            payload["expected_world_revision"], f"{label}.expected_world_revision", minimum=0
        )
    elif event_type is RuntimeEventType.SANDBOX_RUN_COMPLETED:
        if isinstance(payload["exit_code"], bool) or payload["exit_code"] != 0:
            raise ValueError(f"{label}.exit_code must be integer zero")
        intents = _require_array(payload["action_intents"], f"{label}.action_intents", 1000)
        for index, intent in enumerate(intents):
            _validate_runtime_action_intent(intent, f"{label}.action_intents[{index}]")
    elif event_type is RuntimeEventType.WORLD_COMMITTED:
        _require_integer(
            payload["previous_world_revision"], f"{label}.previous_world_revision", minimum=0
        )
        _require_integer(payload["world_revision"], f"{label}.world_revision", minimum=1)
        if payload["world_revision"] != payload["previous_world_revision"] + 1:
            raise ValueError(f"{label} must advance exactly one world revision")
        _require_pattern(payload["state_hash"], _SHA256, f"{label}.state_hash")
        _validate_identifier_array(payload["applied_intent_ids"], f"{label}.applied_intent_ids")
    elif event_type is RuntimeEventType.WORLD_REJECTED:
        _require_integer(
            payload["expected_world_revision"], f"{label}.expected_world_revision", minimum=0
        )
        _require_integer(
            payload["current_world_revision"], f"{label}.current_world_revision", minimum=0
        )
        _validate_identifier_array(payload["rejected_intent_ids"], f"{label}.rejected_intent_ids")
    elif event_type is RuntimeEventType.LEARNER_EVIDENCE_RECORDED:
        _validate_string_array(payload["competency_ids"], f"{label}.competency_ids", 128)
    elif event_type is RuntimeEventType.LEARNER_INFERENCE_RECORDED:
        actor = _validate_actor_ref_object(payload["actor"], f"{label}.actor")
        if actor["actor_id"] != payload["learner_id"]:
            raise ValueError(f"{label}.actor.actor_id must equal learner_id")
        _require_pattern(payload["command_id"], _COMMAND_ID, f"{label}.command_id")
        if payload["run_id"] is not None:
            _require_identifier(payload["run_id"], f"{label}.run_id")
        _require_pattern(payload["source_event_id"], _EVENT_ID, f"{label}.source_event_id")
        _require_pattern(payload["source_event_sha256"], _SHA256, f"{label}.source_event_sha256")
        _require_pattern(payload["turn_commit_sha256"], _SHA256, f"{label}.turn_commit_sha256")
        _require_text(payload["teaching_spec_version"], f"{label}.teaching_spec_version", 1, 96)
        try:
            LearnerInferenceRole(payload["role"])
        except (TypeError, ValueError) as exception:
            raise ValueError(f"{label}.role is not supported") from exception
        _require_pattern(payload["concept"], _LEARNER_CONCEPT, f"{label}.concept")
        _learner_inference_number_ppm(
            payload["score_delta"],
            f"{label}.score_delta",
            minimum=-300_000,
            maximum=300_000,
        )
        _learner_inference_number_ppm(
            payload["confidence"],
            f"{label}.confidence",
            minimum=0,
            maximum=1_000_000,
        )
        _require_text(payload["reason"], f"{label}.reason", 1, 1000)
        _validate_learner_inference_evidence_refs(
            payload["evidence_refs"], f"{label}.evidence_refs"
        )
        _require_pattern(payload["inference_sha256"], _SHA256, f"{label}.inference_sha256")
        if payload["inference_sha256"] != learner_inference_sha256(
            cast(Mapping[str, object], payload)
        ):
            raise ValueError(f"{label}.inference_sha256 does not match its canonical payload")
    elif event_type is RuntimeEventType.LEARNER_MODEL_UPDATED:
        _require_integer(payload["previous_revision"], f"{label}.previous_revision", minimum=0)
        _require_integer(payload["learner_revision"], f"{label}.learner_revision", minimum=1)
        if payload["learner_revision"] != payload["previous_revision"] + 1:
            raise ValueError(f"{label} must advance exactly one learner revision")
        _require_integer(
            payload["projected_through_sequence"], f"{label}.projected_through_sequence", minimum=1
        )
        _validate_string_array(
            payload["changed_competency_ids"], f"{label}.changed_competency_ids", 128
        )
    elif event_type is RuntimeEventType.LEARNER_PROJECTION_FAILED:
        _require_pattern(payload["source_event_id"], _EVENT_ID, f"{label}.source_event_id")
    elif event_type is RuntimeEventType.FEISHU_SYNC_REQUESTED:
        if payload["sync_kind"] not in (
            "TEACHER_PROJECTION",
            "REPORT_DRAFT",
            "REVIEW_CARD",
            "OPERATION_ALERT",
        ):
            raise ValueError(f"{label}.sync_kind is invalid")
        _require_text(payload["target_ref"], f"{label}.target_ref", 1, 256)
        _require_integer(payload["attempt"], f"{label}.attempt", minimum=1)
    elif event_type is RuntimeEventType.FEISHU_SYNC_SUCCEEDED:
        _require_text(payload["remote_object_id"], f"{label}.remote_object_id", 1, 256)
        _require_integer(payload["attempt"], f"{label}.attempt", minimum=1)
    elif event_type is RuntimeEventType.FEISHU_SYNC_FAILED:
        _require_integer(payload["attempt"], f"{label}.attempt", minimum=1)
        if payload["next_attempt_at"] is not None:
            _require_iso_datetime(payload["next_attempt_at"], f"{label}.next_attempt_at")
    elif event_type is RuntimeEventType.FEISHU_SYNC_DEAD_LETTERED:
        _require_integer(payload["attempts"], f"{label}.attempts", minimum=1)


def _validate_identifier_array(value: object, field_name: str) -> None:
    identifiers = _require_unique_array(value, field_name, 2**31 - 1)
    for identifier in identifiers:
        _require_identifier(identifier, f"{field_name}[]")


def _validate_string_array(value: object, field_name: str, maximum_length: int) -> None:
    values = _require_unique_array(value, field_name, 2**31 - 1)
    for item in values:
        _require_text(item, f"{field_name}[]", 1, maximum_length)


@dataclass(frozen=True, slots=True)
class RuntimeEvent(DomainEvent[Mapping[str, Any]]):
    """Closed integration-bus event union; domain event streams remain extensible."""

    def __post_init__(self) -> None:
        # Zero-argument super() is not runtime-safe on this slotted dataclass
        # subclass after PEP 695 generic transformation.
        DomainEvent.__post_init__(self)  # pyright: ignore[reportUnknownMemberType]
        if self.event_version != 1:
            raise ValueError("runtime event_version must be exactly 1")
        try:
            runtime_type = RuntimeEventType(self.event_type)
        except ValueError as error:
            raise ValueError(f"unknown runtime event_type {self.event_type}") from error
        expected_schema_version = (
            "2.0.0" if runtime_type is RuntimeEventType.LEARNER_INFERENCE_RECORDED else "1.0.0"
        )
        if self.schema_version != expected_schema_version:
            raise ValueError(
                f"{runtime_type} must use event schema_version {expected_schema_version}"
            )
        required_fields = _RUNTIME_EVENT_PAYLOAD_FIELDS[runtime_type]
        _require_exact_keys(self.payload, set(required_fields), f"{runtime_type}.payload")
        _validate_runtime_event_payload(runtime_type, self.payload)
        if (
            runtime_type is RuntimeEventType.AGENT_TURN_FEEDBACK_READY
            and self.payload["command_id"] != self.command_id
        ):
            raise ValueError(
                "agent.turn.feedback_ready.payload.command_id must equal envelope command_id"
            )
        if runtime_type is RuntimeEventType.LEARNER_INFERENCE_RECORDED:
            learner_id = self.payload["learner_id"]
            if self.stream_id != f"learner:{learner_id}":
                raise ValueError(
                    "learner.inference.recorded stream_id must equal learner:<learner_id>"
                )
            if self.payload["command_id"] != self.command_id:
                raise ValueError(
                    "learner.inference.recorded payload.command_id must equal envelope command_id"
                )
            if self.causation_id != self.payload["source_event_id"]:
                raise ValueError(
                    "learner.inference.recorded causation_id must equal payload.source_event_id"
                )
        object.__setattr__(self, "event_type", runtime_type)


type RealtimeProtocolVersion = Literal["1.0.0"]
type RealtimeCloseCode = Literal[
    4400,
    4401,
    4403,
    4404,
    4406,
    4408,
    4409,
    4429,
    4500,
    4503,
]
type RealtimeWorldEvent = DomainEvent[Mapping[str, Any]]

_REALTIME_CLOSE_CODES: frozenset[int] = frozenset(
    {4400, 4401, 4403, 4404, 4406, 4408, 4409, 4429, 4500, 4503}
)
_REALTIME_ERROR_CLOSE_CODES: Mapping[str, frozenset[int]] = MappingProxyType(
    {
        "INVALID_REQUEST": frozenset({4400}),
        "AUTHENTICATION_REQUIRED": frozenset({4401}),
        "AUTHORIZATION_DENIED": frozenset({4403}),
        "POLICY_DENIED": frozenset({4403}),
        "NOT_FOUND": frozenset({4404}),
        "SCHEMA_VERSION_UNSUPPORTED": frozenset({4406}),
        "DEPENDENCY_UNAVAILABLE": frozenset({4408, 4503}),
        "EVENT_SEQUENCE_GAP": frozenset({4409}),
        "WORLD_REVISION_CONFLICT": frozenset({4409}),
        "RATE_LIMITED": frozenset({4429}),
        "INTERNAL_ERROR": frozenset({4500}),
        "INVARIANT_VIOLATION": frozenset({4500}),
    }
)


def _require_realtime_protocol_version(value: object) -> None:
    if value != "1.0.0":
        raise ValueError("unsupported realtime protocol_version")


@dataclass(frozen=True, slots=True)
class RealtimeBootstrap:
    """Realtime subset embedded in the closed bootstrap.world wire object."""

    world_id: str
    stream_id: str
    stream_url: str
    last_event_sequence: int
    stream_protocol_version: RealtimeProtocolVersion = "1.0.0"

    def __post_init__(self) -> None:
        _require_identifier(self.world_id, "world_id")
        _require_pattern(self.stream_id, _STREAM_ID, "stream_id")
        if self.stream_id != f"world:{self.world_id}":
            raise ValueError("stream_id must equal world: plus world_id")
        _require_wss_url(self.stream_url, "stream_url")
        _require_integer(
            self.last_event_sequence,
            "last_event_sequence",
            minimum=0,
        )
        _require_realtime_protocol_version(self.stream_protocol_version)


@dataclass(frozen=True, slots=True)
class RealtimeSubscribeFrame:
    request_id: str
    stream_id: str
    after_sequence: int
    protocol_version: RealtimeProtocolVersion = "1.0.0"
    frame_type: Literal["subscribe"] = field(default="subscribe", init=False)

    def __post_init__(self) -> None:
        _require_pattern(self.request_id, _REQUEST_ID, "request_id")
        _require_pattern(self.stream_id, _STREAM_ID, "stream_id")
        _require_integer(self.after_sequence, "after_sequence", minimum=0)
        _require_realtime_protocol_version(self.protocol_version)


@dataclass(frozen=True, slots=True)
class RealtimeResumeFrame:
    request_id: str
    subscription_id: str
    stream_id: str
    after_sequence: int
    protocol_version: RealtimeProtocolVersion = "1.0.0"
    frame_type: Literal["resume"] = field(default="resume", init=False)

    def __post_init__(self) -> None:
        _require_pattern(self.request_id, _REQUEST_ID, "request_id")
        _require_pattern(
            self.subscription_id,
            _SUBSCRIPTION_ID,
            "subscription_id",
        )
        _require_pattern(self.stream_id, _STREAM_ID, "stream_id")
        _require_integer(self.after_sequence, "after_sequence", minimum=0)
        _require_realtime_protocol_version(self.protocol_version)


@dataclass(frozen=True, slots=True)
class RealtimeAckFrame:
    subscription_id: str
    stream_id: str
    sequence: int
    event_id: str
    protocol_version: RealtimeProtocolVersion = "1.0.0"
    frame_type: Literal["ack"] = field(default="ack", init=False)

    def __post_init__(self) -> None:
        _require_pattern(
            self.subscription_id,
            _SUBSCRIPTION_ID,
            "subscription_id",
        )
        _require_pattern(self.stream_id, _STREAM_ID, "stream_id")
        _require_integer(self.sequence, "sequence", minimum=1)
        _require_pattern(self.event_id, _EVENT_ID, "event_id")
        _require_realtime_protocol_version(self.protocol_version)


@dataclass(frozen=True, slots=True)
class RealtimeHeartbeatAckFrame:
    subscription_id: str
    nonce: str
    received_at: datetime
    protocol_version: RealtimeProtocolVersion = "1.0.0"
    frame_type: Literal["heartbeat_ack"] = field(default="heartbeat_ack", init=False)

    def __post_init__(self) -> None:
        _require_pattern(
            self.subscription_id,
            _SUBSCRIPTION_ID,
            "subscription_id",
        )
        _require_pattern(self.nonce, _HEARTBEAT_NONCE, "nonce")
        _require_datetime(self.received_at, "received_at")
        _require_realtime_protocol_version(self.protocol_version)


@dataclass(frozen=True, slots=True)
class RealtimeSubscribedFrame:
    request_id: str
    subscription_id: str
    stream_id: str
    accepted_after_sequence: int
    high_watermark_sequence: int
    heartbeat_interval_ms: int
    max_unacked_events: int
    protocol_version: RealtimeProtocolVersion = "1.0.0"
    frame_type: Literal["subscribed"] = field(default="subscribed", init=False)

    def __post_init__(self) -> None:
        _require_pattern(self.request_id, _REQUEST_ID, "request_id")
        _require_pattern(
            self.subscription_id,
            _SUBSCRIPTION_ID,
            "subscription_id",
        )
        _require_pattern(self.stream_id, _STREAM_ID, "stream_id")
        accepted = _require_integer(
            self.accepted_after_sequence,
            "accepted_after_sequence",
            minimum=0,
        )
        high_watermark = _require_integer(
            self.high_watermark_sequence,
            "high_watermark_sequence",
            minimum=0,
        )
        if accepted > high_watermark:
            raise ValueError("accepted_after_sequence cannot exceed high_watermark_sequence")
        _require_integer(
            self.heartbeat_interval_ms,
            "heartbeat_interval_ms",
            minimum=1000,
            maximum=120_000,
        )
        _require_integer(
            self.max_unacked_events,
            "max_unacked_events",
            minimum=1,
            maximum=10_000,
        )
        _require_realtime_protocol_version(self.protocol_version)


@dataclass(frozen=True, slots=True)
class RealtimeHeartbeatFrame:
    subscription_id: str
    stream_id: str
    nonce: str
    server_time: datetime
    high_watermark_sequence: int
    protocol_version: RealtimeProtocolVersion = "1.0.0"
    frame_type: Literal["heartbeat"] = field(default="heartbeat", init=False)

    def __post_init__(self) -> None:
        _require_pattern(
            self.subscription_id,
            _SUBSCRIPTION_ID,
            "subscription_id",
        )
        _require_pattern(self.stream_id, _STREAM_ID, "stream_id")
        _require_pattern(self.nonce, _HEARTBEAT_NONCE, "nonce")
        _require_datetime(self.server_time, "server_time")
        _require_integer(
            self.high_watermark_sequence,
            "high_watermark_sequence",
            minimum=0,
        )
        _require_realtime_protocol_version(self.protocol_version)


@dataclass(frozen=True, slots=True)
class RealtimeErrorFrame:
    request_id: str | None
    subscription_id: str | None
    stream_id: str | None
    fatal: bool
    close_code: RealtimeCloseCode | None
    retry_after_ms: int | None
    error: ContractError
    protocol_version: RealtimeProtocolVersion = "1.0.0"
    frame_type: Literal["error"] = field(default="error", init=False)

    def __post_init__(self) -> None:
        if self.request_id is not None:
            _require_pattern(self.request_id, _REQUEST_ID, "request_id")
        if self.subscription_id is not None:
            _require_pattern(
                self.subscription_id,
                _SUBSCRIPTION_ID,
                "subscription_id",
            )
        if self.stream_id is not None:
            _require_pattern(self.stream_id, _STREAM_ID, "stream_id")
        _require_boolean(self.fatal, "fatal")
        if (self.close_code is None) == self.fatal:
            raise ValueError("close_code must be non-null exactly when fatal is true")
        if self.close_code is not None:
            _require_integer(self.close_code, "close_code")
            if self.close_code not in _REALTIME_CLOSE_CODES:
                raise ValueError("close_code is not reserved by the realtime contract")
        if self.retry_after_ms is not None:
            _require_integer(
                self.retry_after_ms,
                "retry_after_ms",
                minimum=0,
                maximum=86_400_000,
            )
        if not isinstance(self.error, ContractError):
            raise TypeError("error must be a ContractError")
        if self.retry_after_ms is not None and not self.error.retryable:
            raise ValueError("retry_after_ms requires a retryable error")
        if self.close_code is not None and self.close_code not in _REALTIME_ERROR_CLOSE_CODES.get(
            self.error.code,
            frozenset(),
        ):
            raise ValueError("close_code does not match error.code")
        _require_realtime_protocol_version(self.protocol_version)


@dataclass(frozen=True, slots=True)
class RealtimeCheckpoint:
    """Highest contiguous event durably projected by a client adapter."""

    stream_id: str
    last_applied_sequence: int
    last_event_id: str | None

    def __post_init__(self) -> None:
        _require_pattern(self.stream_id, _STREAM_ID, "stream_id")
        sequence = _require_integer(
            self.last_applied_sequence,
            "last_applied_sequence",
            minimum=0,
        )
        if sequence == 0 and self.last_event_id is not None:
            raise ValueError("last_event_id must be null when last_applied_sequence is zero")
        if sequence > 0 and self.last_event_id is None:
            raise ValueError("last_event_id is required for a non-zero checkpoint")
        if self.last_event_id is not None:
            _require_pattern(self.last_event_id, _EVENT_ID, "last_event_id")


type RealtimeClientFrame = (
    RealtimeSubscribeFrame | RealtimeResumeFrame | RealtimeAckFrame | RealtimeHeartbeatAckFrame
)
type RealtimeServerControlFrame = (
    RealtimeSubscribedFrame | RealtimeHeartbeatFrame | RealtimeErrorFrame
)
type RealtimeServerFrame = RealtimeWorldEvent | RealtimeServerControlFrame


@dataclass(frozen=True, slots=True)
class UncommittedEvent:
    event_type: str
    event_version: int
    producer: str
    trace_id: str
    command_id: str
    correlation_id: str
    causation_id: str | None
    content_ref: ContentRef
    payload: FrozenJsonObject
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        _require_pattern(self.event_type, re.compile(r"^[a-z][a-z0-9_.-]{2,127}$"), "event_type")
        _require_integer(self.event_version, "event_version", minimum=1)
        _require_pattern(self.producer, re.compile(r"^[a-z][a-z0-9_-]{2,63}$"), "producer")
        _require_pattern(self.trace_id, _TRACE_ID, "trace_id")
        _require_pattern(self.command_id, _COMMAND_ID, "command_id")
        _require_pattern(self.correlation_id, _CORRELATION_ID, "correlation_id")
        if self.causation_id is not None and not (
            _EVENT_ID.fullmatch(self.causation_id) or _COMMAND_ID.fullmatch(self.causation_id)
        ):
            raise ValueError("causation_id must be a command or event identifier")
        if self.schema_version != "1.0.0":
            raise ValueError("unsupported event schema_version")
        if not isinstance(self.content_ref, ContentRef):
            raise TypeError("content_ref must be a ContentRef")
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "payload"))


@dataclass(frozen=True, slots=True)
class EventAppendReceipt:
    stream_id: str
    previous_sequence: int
    next_sequence: int
    events: tuple[DomainEvent[Mapping[str, Any]], ...]

    def __post_init__(self) -> None:
        _require_pattern(self.stream_id, _STREAM_ID, "stream_id")
        _require_integer(self.previous_sequence, "previous_sequence", minimum=0)
        _require_integer(self.next_sequence, "next_sequence", minimum=0)
        events = _freeze_tuple(self.events, "events")
        expected_sequence = self.previous_sequence + 1
        event_ids: set[str] = set()
        for index, event in enumerate(events):
            if not isinstance(event, DomainEvent):
                raise ValueError(f"events[{index}] must be a DomainEvent")
            if event.stream_id != self.stream_id:
                raise ValueError(f"events[{index}].stream_id does not match receipt stream_id")
            if event.sequence != expected_sequence:
                raise ValueError(
                    f"events[{index}].sequence must be contiguous at {expected_sequence}"
                )
            if event.event_id in event_ids:
                raise ValueError(f"events contains duplicate event_id {event.event_id}")
            event_ids.add(event.event_id)
            expected_sequence += 1
        if self.next_sequence != expected_sequence - 1:
            raise ValueError("event append receipt has an inconsistent sequence range")
        object.__setattr__(self, "events", events)


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    cpu_ms: int
    wall_ms: int
    memory_bytes: int
    max_intents: int
    max_output_bytes: int
    max_processes: int
    network_access: Literal[False] = False

    def __post_init__(self) -> None:
        for name in (
            "cpu_ms",
            "wall_ms",
            "memory_bytes",
            "max_intents",
            "max_output_bytes",
            "max_processes",
        ):
            _require_integer(getattr(self, name), name, minimum=1)
        if self.network_access is not False:
            raise ValueError("sandbox network_access must be false")


@dataclass(frozen=True, slots=True)
class SkillSourceFile:
    path: str
    content: str
    content_sha256: str

    def __post_init__(self) -> None:
        _require_pattern(self.path, _SOURCE_PATH, "path")
        _require_text(self.content, "content", 0, 1_048_576)
        _require_pattern(self.content_sha256, _SHA256, "content_sha256")
        actual_sha256 = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_sha256 != actual_sha256:
            raise ValueError("content_sha256 does not match UTF-8 source content")


@dataclass(frozen=True, slots=True)
class SkillSourceBundle:
    entrypoint: str
    files: tuple[SkillSourceFile, ...]
    language: Literal["CPP20"] = "CPP20"

    def __post_init__(self) -> None:
        if self.language != "CPP20":
            raise ValueError("language must be CPP20")
        _require_pattern(self.entrypoint, _SOURCE_PATH, "entrypoint")
        files = _freeze_tuple(self.files, "files")
        if not 1 <= len(files) <= _MAX_SOURCE_FILES:
            raise ValueError(f"files must contain between 1 and {_MAX_SOURCE_FILES} source files")
        for index, source_file in enumerate(files):
            if not isinstance(source_file, SkillSourceFile):
                raise TypeError(f"files[{index}] must be a SkillSourceFile")
        paths = tuple(source_file.path for source_file in files)
        if len(set(paths)) != len(paths):
            raise ValueError("source file paths must be unique")
        if self.entrypoint not in paths:
            raise ValueError("entrypoint must identify one source file")
        total_source_bytes = sum(len(source_file.content.encode("utf-8")) for source_file in files)
        if total_source_bytes > _MAX_SOURCE_BYTES:
            raise ValueError(f"source content UTF-8 bytes must total at most {_MAX_SOURCE_BYTES}")
        object.__setattr__(self, "files", files)


@dataclass(frozen=True, slots=True)
class CompileAndTestRequest:
    build_id: str
    skill_id: str
    source_bundle: SkillSourceBundle
    compiler_profile: str
    test_suite_version: str
    limits: SandboxLimits

    def __post_init__(self) -> None:
        _require_identifier(self.build_id, "build_id")
        _require_identifier(self.skill_id, "skill_id")
        if not isinstance(self.source_bundle, SkillSourceBundle):
            raise TypeError("source_bundle must be a SkillSourceBundle")
        if not isinstance(self.limits, SandboxLimits):
            raise TypeError("limits must be SandboxLimits")
        _require_text(self.compiler_profile, "compiler_profile", 1, 64)
        _require_text(self.test_suite_version, "test_suite_version", 1, 96)


@dataclass(frozen=True, slots=True)
class SandboxRunRequest:
    run_id: str
    skill_ref: SkillRef
    world_id: str
    world_snapshot: WorldSnapshot
    input: FrozenJsonObject
    deterministic_seed: str
    limits: SandboxLimits

    def __post_init__(self) -> None:
        _require_identifier(self.run_id, "run_id")
        _require_identifier(self.world_id, "world_id")
        if not isinstance(self.skill_ref, SkillRef):
            raise TypeError("skill_ref must be a SkillRef")
        if not isinstance(self.world_snapshot, WorldSnapshot):
            raise TypeError("world_snapshot must be a WorldSnapshot")
        if self.world_snapshot.world_id != self.world_id:
            raise ValueError("world_snapshot.world_id must match world_id")
        if not isinstance(self.limits, SandboxLimits):
            raise TypeError("limits must be SandboxLimits")
        _require_text(self.deterministic_seed, "deterministic_seed", 1, 256)
        object.__setattr__(self, "input", _freeze_mapping(self.input, "input"))


@dataclass(frozen=True, slots=True)
class SandboxUsage:
    cpu_ms: int
    wall_ms: int
    peak_memory_bytes: int

    def __post_init__(self) -> None:
        for name in ("cpu_ms", "wall_ms", "peak_memory_bytes"):
            _require_integer(getattr(self, name), name, minimum=0)


@dataclass(frozen=True, slots=True)
class SandboxRunResult:
    run_id: str
    started_at: datetime
    finished_at: datetime
    action_intents: tuple[ActionIntent, ...]
    stdout_ref: EvidenceRef | None
    stderr_ref: EvidenceRef | None
    usage: SandboxUsage
    evidence_refs: tuple[EvidenceRef, ...]
    status: Literal["SUCCEEDED"] = "SUCCEEDED"
    exit_code: Literal[0] = 0

    def __post_init__(self) -> None:
        _require_identifier(self.run_id, "run_id")
        _require_datetime(self.started_at, "started_at")
        _require_datetime(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.status != "SUCCEEDED":
            raise ValueError("sandbox run result status must be SUCCEEDED")
        if isinstance(self.exit_code, bool) or self.exit_code != 0:
            raise ValueError("sandbox run result exit_code must be integer zero")
        if not isinstance(self.usage, SandboxUsage):
            raise TypeError("usage must be a SandboxUsage")
        action_intents = _freeze_tuple(self.action_intents, "action_intents")
        intent_types = (
            MoveIntent,
            PlantIntent,
            WaterIntent,
            HarvestIntent,
            InteractIntent,
            SpeakIntent,
        )
        intent_ids: set[str] = set()
        for index, intent in enumerate(action_intents):
            if not isinstance(intent, intent_types):
                raise TypeError(f"action_intents[{index}] must be an ActionIntent")
            if intent.intent_id in intent_ids:
                raise ValueError(f"action_intents contains duplicate intent_id {intent.intent_id}")
            intent_ids.add(intent.intent_id)
        for name in ("stdout_ref", "stderr_ref"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, EvidenceRef):
                raise TypeError(f"{name} must be an EvidenceRef or None")
        evidence_refs = _freeze_tuple(self.evidence_refs, "evidence_refs")
        if len(evidence_refs) > 64:
            raise ValueError("evidence_refs must contain at most 64 items")
        evidence_ids: set[str] = set()
        for index, evidence in enumerate(evidence_refs):
            if not isinstance(evidence, EvidenceRef):
                raise TypeError(f"evidence_refs[{index}] must be an EvidenceRef")
            if evidence.evidence_id in evidence_ids:
                raise ValueError("evidence_refs must contain unique evidence_id values")
            evidence_ids.add(evidence.evidence_id)
        object.__setattr__(self, "action_intents", action_intents)
        object.__setattr__(self, "evidence_refs", evidence_refs)


def _validate_command_result(command_type: CommandType, result: FrozenJsonObject) -> None:
    result_type = result.get("result_type")
    if result_type == "WORLD_COMMIT":
        fields = {
            "result_type",
            "world_id",
            "previous_revision",
            "world_revision",
            "first_event_sequence",
            "last_event_sequence",
        }
        _require_exact_keys(result, fields, "result")
        if command_type != "EXECUTE_AGENT_TURN":
            raise ValueError("WORLD_COMMIT result is not valid for command_type")
        _require_identifier(result["world_id"], "result.world_id")
        previous_revision = _require_integer(
            result["previous_revision"], "result.previous_revision", minimum=0
        )
        world_revision = _require_integer(
            result["world_revision"], "result.world_revision", minimum=1
        )
        if world_revision != previous_revision + 1:
            raise ValueError("result.world_revision must advance exactly one revision")
        first_event_sequence = _require_integer(
            result["first_event_sequence"],
            "result.first_event_sequence",
            minimum=1,
        )
        last_event_sequence = _require_integer(
            result["last_event_sequence"],
            "result.last_event_sequence",
            minimum=1,
        )
        if last_event_sequence < first_event_sequence:
            raise ValueError("result event sequence range is invalid")
        return
    if result_type == "RESOURCE_CREATED":
        fields = {"result_type", "resource_type", "resource_id", "resource_url"}
        _require_exact_keys(result, fields, "result")
        expected_resource = {
            "CREATE_SKILL_BUILD": "SKILL_BUILD",
            "ACTIVATE_SKILL_VERSION": "SKILL_ACTIVATION",
            "CREATE_AGENT_SESSION": "AGENT_SESSION",
        }.get(command_type)
        if expected_resource is None or result["resource_type"] != expected_resource:
            raise ValueError("RESOURCE_CREATED result does not match command_type")
        _require_identifier(result["resource_id"], "result.resource_id")
        _require_uri_reference(result["resource_url"], "result.resource_url", 1, 2048)
        return
    if result_type == "CLIENT_EVENTS_ACCEPTED":
        fields = {
            "result_type",
            "batch_id",
            "accepted_count",
            "duplicate_count",
            "rejected_count",
        }
        _require_exact_keys(result, fields, "result")
        if command_type != "INGEST_CLIENT_EVENTS":
            raise ValueError("CLIENT_EVENTS_ACCEPTED result is not valid for command_type")
        _require_identifier(result["batch_id"], "result.batch_id")
        for name in ("accepted_count", "duplicate_count", "rejected_count"):
            _require_integer(result[name], f"result.{name}", minimum=0)
        return
    if result_type == "NO_EFFECT":
        _require_exact_keys(result, {"result_type", "reason_code"}, "result")
        if command_type != "EXECUTE_AGENT_TURN":
            raise ValueError("NO_EFFECT result is not valid for command_type")
        _require_pattern(result["reason_code"], _ERROR_CODE, "result.reason_code")
        return
    raise ValueError("result.result_type is missing or unsupported")


@dataclass(frozen=True, slots=True)
class CommandRecord:
    request_context: RequestContext
    command_id: str
    command_type: CommandType
    status: CommandStatus
    stage: str
    terminal: bool
    accepted_at: datetime
    updated_at: datetime
    result: FrozenJsonObject | None
    error: ContractError | None
    evidence_refs: tuple[EvidenceRef, ...]
    versions: VersionSet
    links: FrozenJsonObject
    revision: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.request_context, RequestContext):
            raise TypeError("request_context must be a RequestContext")
        _require_pattern(self.command_id, _COMMAND_ID, "command_id")
        _require_integer(self.revision, "revision", minimum=1)
        if self.command_type not in _COMMAND_TYPES:
            raise ValueError("command_type is not supported")
        object.__setattr__(self, "status", CommandStatus(self.status))
        if self.stage not in _COMMAND_STAGES:
            raise ValueError("stage is not supported")
        _require_boolean(self.terminal, "terminal")
        _require_datetime(self.accepted_at, "accepted_at")
        _require_datetime(self.updated_at, "updated_at")
        if self.error is not None and not isinstance(self.error, ContractError):
            raise TypeError("error must be a ContractError or None")
        if not isinstance(self.versions, VersionSet):
            raise TypeError("versions must be a VersionSet")
        if self.updated_at < self.accepted_at:
            raise ValueError("updated_at must not precede accepted_at")
        if self.terminal != self.status.is_terminal:
            raise ValueError("terminal flag must agree with command status")
        if self.status is CommandStatus.APPLIED and self.result is None:
            raise ValueError("APPLIED command must contain a committed result")
        if self.status is CommandStatus.APPLIED and self.error is not None:
            raise ValueError("APPLIED command cannot contain an error")
        if self.status is CommandStatus.APPLIED and self.stage != "COMPLETE":
            raise ValueError("APPLIED command stage must be COMPLETE")
        if (
            self.status
            in {
                CommandStatus.REJECTED,
                CommandStatus.FAILED,
                CommandStatus.UNKNOWN,
            }
            and self.error is None
        ):
            raise ValueError("non-success terminal status must contain a structured error")
        if self.status is not CommandStatus.APPLIED and self.result is not None:
            raise ValueError("only APPLIED command may contain a result")
        if not self.terminal and (self.result is not None or self.error is not None):
            raise ValueError("non-terminal command cannot contain result or error")
        if self.status is CommandStatus.UNKNOWN:
            if self.stage != "WORLD_COMMIT":
                raise ValueError("UNKNOWN command stage must be WORLD_COMMIT")
            if self.error is None or self.error.code != "UNKNOWN_COMMIT_STATE":
                raise ValueError("UNKNOWN command requires UNKNOWN_COMMIT_STATE")
            if self.error.stage != "WORLD_COMMIT":
                raise ValueError("UNKNOWN command error stage must be WORLD_COMMIT")
        allowed_stages = _COMMAND_STATUS_STAGES.get(self.status)
        if allowed_stages is not None and self.stage not in allowed_stages:
            raise ValueError(f"stage {self.stage} is invalid for status {self.status}")
        if self.result is not None:
            result = _freeze_mapping(self.result, "result")
            _validate_command_result(self.command_type, result)
            object.__setattr__(self, "result", result)
        evidence_refs = _freeze_tuple(self.evidence_refs, "evidence_refs")
        if len(evidence_refs) > 64:
            raise ValueError("evidence_refs must contain at most 64 items")
        evidence_ids: set[str] = set()
        for index, evidence in enumerate(evidence_refs):
            if not isinstance(evidence, EvidenceRef):
                raise TypeError(f"evidence_refs[{index}] must be an EvidenceRef")
            if evidence.evidence_id in evidence_ids:
                raise ValueError("evidence_refs must contain unique evidence_id values")
            evidence_ids.add(evidence.evidence_id)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        links = _freeze_mapping(self.links, "links")
        allowed_link_keys = {"self", "run", "world_snapshot"}
        if "self" not in links or not set(links).issubset(allowed_link_keys):
            raise ValueError("links must contain self and no unknown fields")
        for name, value in links.items():
            _require_uri_reference(value, f"links.{name}", 1, 2048)
        object.__setattr__(self, "links", links)


@dataclass(frozen=True, slots=True)
class CommandCreateReceipt:
    """Atomic idempotency outcome; created=False means the exact request was replayed."""

    command: CommandRecord
    created: bool

    def __post_init__(self) -> None:
        if not isinstance(self.command, CommandRecord):
            raise TypeError("command must be a CommandRecord")
        _require_boolean(self.created, "created")


@dataclass(frozen=True, slots=True)
class CommandTransition:
    previous_record: CommandRecord
    next_record: CommandRecord

    def __post_init__(self) -> None:
        if not isinstance(self.previous_record, CommandRecord):
            raise TypeError("previous_record must be a CommandRecord")
        if not isinstance(self.next_record, CommandRecord):
            raise TypeError("next_record must be a CommandRecord")
        previous = self.previous_record
        next_record = self.next_record
        immutable_fields = (
            "request_context",
            "command_id",
            "command_type",
            "accepted_at",
            "versions",
        )
        for field_name in immutable_fields:
            if getattr(next_record, field_name) != getattr(previous, field_name):
                raise ValueError(f"next_record cannot change immutable {field_name}")
        if next_record.links.get("self") != previous.links.get("self"):
            raise ValueError("next_record cannot change immutable self link")
        if next_record.revision != previous.revision + 1:
            raise ValueError("next_record revision must advance exactly once")
        if next_record.updated_at < previous.updated_at:
            raise ValueError("next_record.updated_at must not precede previous_record")
        if next_record.status not in _COMMAND_TRANSITIONS[previous.status]:
            raise ValueError(
                f"command status transition {previous.status} -> {next_record.status} is invalid"
            )
        if next_record.status is previous.status and (
            _COMMAND_STAGE_INDEX[next_record.stage] <= _COMMAND_STAGE_INDEX[previous.stage]
        ):
            raise ValueError("same-status transition must advance the command stage")

    @property
    def command_id(self) -> str:
        return self.previous_record.command_id

    @property
    def expected_revision(self) -> int:
        return self.previous_record.revision

    @property
    def expected_status(self) -> CommandStatus:
        return self.previous_record.status


@dataclass(frozen=True, slots=True)
class NewCommand:
    """Input for atomically accepting a command exactly once.

    ``command_id`` and request identity come from ``OperationContext``; acceptance
    timestamps are generated by the store.  The idempotency namespace is the
    tuple ``(tenant_id, actor_id, command_type, idempotency_key)``.  The actor
    comes only from the authenticated ``OperationContext`` so callers cannot
    supply a second, drifting identity field.
    """

    command_type: CommandType
    idempotency_key: str
    request_sha256: str
    versions: VersionSet

    def __post_init__(self) -> None:
        if self.command_type not in _COMMAND_TYPES:
            raise ValueError("command_type is not supported")
        _require_pattern(self.idempotency_key, _IDEMPOTENCY_KEY, "idempotency_key")
        _require_pattern(self.request_sha256, _SHA256, "request_sha256")
        if not isinstance(self.versions, VersionSet):
            raise TypeError("versions must be a VersionSet")

    @property
    def operation(self) -> str:
        """Stable logical operation used in the idempotency namespace."""

        return self.command_type

    def idempotency_scope(
        self,
        context: OperationContext,
    ) -> tuple[str, str, str, str]:
        """Return the exact adapter key: tenant, actor, operation, key."""

        if not isinstance(context, OperationContext):
            raise TypeError("context must be an OperationContext")
        return (
            context.actor.tenant_id,
            context.actor.actor_id,
            self.operation,
            self.idempotency_key,
        )

    def initial_record(
        self,
        context: OperationContext,
        accepted_at: datetime,
    ) -> CommandRecord:
        """Materialize the complete initial record using store-owned fields."""

        if not isinstance(context, OperationContext):
            raise TypeError("context must be an OperationContext")
        _require_datetime(accepted_at, "accepted_at")
        request_context = RequestContext(
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            requested_at=context.requested_at,
            actor=context.actor,
            content_ref=context.content_ref,
            schema_version=context.schema_version,
        )
        return CommandRecord(
            request_context=request_context,
            command_id=context.command_id,
            command_type=self.command_type,
            status=CommandStatus.ACCEPTED,
            stage="ACCEPT",
            terminal=False,
            accepted_at=accepted_at,
            updated_at=accepted_at,
            result=None,
            error=None,
            evidence_refs=(),
            versions=self.versions,
            links={"self": f"/v1/commands/{context.command_id}"},
        )


@dataclass(frozen=True, slots=True)
class LlmMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ("system", "user", "assistant", "tool"):
            raise ValueError("unsupported LLM message role")
        _require_text(self.content, "content", 1, 1_000_000)
        if self.name is not None:
            _require_text(self.name, "name", 1, 128)
        if self.tool_call_id is not None:
            _require_text(self.tool_call_id, "tool_call_id", 1, 256)


@dataclass(frozen=True, slots=True)
class LlmRequest:
    messages: tuple[LlmMessage, ...]
    output_schema: FrozenJsonObject
    temperature: float
    max_output_tokens: int
    timeout_ms: int
    versions: VersionSet

    def __post_init__(self) -> None:
        messages = _freeze_tuple(self.messages, "messages")
        if not messages:
            raise ValueError("messages must not be empty")
        for index, message in enumerate(messages):
            if not isinstance(message, LlmMessage):
                raise TypeError(f"messages[{index}] must be an LlmMessage")
        if not isinstance(self.versions, VersionSet):
            raise TypeError("versions must be a VersionSet")
        if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)):
            raise ValueError("temperature must be a number")
        if not math.isfinite(float(self.temperature)) or not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        _require_integer(self.max_output_tokens, "max_output_tokens", minimum=1)
        _require_integer(self.timeout_ms, "timeout_ms", minimum=1)
        object.__setattr__(self, "messages", messages)
        object.__setattr__(
            self,
            "output_schema",
            _freeze_mapping(self.output_schema, "output_schema"),
        )


type LlmReplySource = Literal["provider", "provider_fallback"]

type AgentTurnFeedbackSource = LlmReplySource


@dataclass(frozen=True, slots=True)
class AgentTurnFeedback:
    session_id: str
    turn_id: str
    command_id: str
    run_id: str | None
    message_key: str
    message: str
    source: AgentTurnFeedbackSource
    degraded: bool
    fallback_reason: str | None
    evidence_refs: tuple[EvidenceRef, ...]
    completed_at: datetime

    def __post_init__(self) -> None:
        _require_identifier(self.session_id, "session_id")
        _require_identifier(self.turn_id, "turn_id")
        _require_pattern(self.command_id, _COMMAND_ID, "command_id")
        if self.run_id is not None:
            _require_identifier(self.run_id, "run_id")
        _require_pattern(self.message_key, _MESSAGE_KEY, "message_key")
        _require_text(self.message, "message", 1, 4000)
        _validate_feedback_discriminator(
            self.source,
            self.degraded,
            self.fallback_reason,
            "agent_turn_feedback",
        )
        _require_datetime(self.completed_at, "completed_at")
        evidence_refs = _freeze_tuple(self.evidence_refs, "evidence_refs")
        if len(evidence_refs) > 64:
            raise ValueError("evidence_refs must contain at most 64 items")
        evidence_ids: set[str] = set()
        for index, evidence in enumerate(evidence_refs):
            if not isinstance(evidence, EvidenceRef):
                raise TypeError(f"evidence_refs[{index}] must be an EvidenceRef")
            if evidence.evidence_id in evidence_ids:
                raise ValueError("evidence_refs must contain unique evidence_id values")
            evidence_ids.add(evidence.evidence_id)
        object.__setattr__(self, "evidence_refs", evidence_refs)


@dataclass(frozen=True, slots=True)
class LlmReply:
    output: FrozenJsonObject
    provider: str
    model: str
    source: LlmReplySource
    degraded: bool
    fallback_reason: str | None
    input_tokens: int
    output_tokens: int
    evidence_refs: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        _validate_feedback_discriminator(
            self.source,
            self.degraded,
            self.fallback_reason,
            "llm_reply",
        )
        _require_text(self.provider, "provider", 1, 128)
        _require_text(self.model, "model", 1, 128)
        _require_integer(self.input_tokens, "input_tokens", minimum=0)
        _require_integer(self.output_tokens, "output_tokens", minimum=0)
        evidence_refs = _freeze_tuple(self.evidence_refs, "evidence_refs")
        if len(evidence_refs) > 64:
            raise ValueError("evidence_refs must contain at most 64 items")
        evidence_ids: set[str] = set()
        for index, evidence in enumerate(evidence_refs):
            if not isinstance(evidence, EvidenceRef):
                raise TypeError(f"evidence_refs[{index}] must be an EvidenceRef")
            if evidence.evidence_id in evidence_ids:
                raise ValueError("evidence_refs must contain unique evidence_id values")
            evidence_ids.add(evidence.evidence_id)
        object.__setattr__(self, "output", _freeze_mapping(self.output, "output"))
        object.__setattr__(self, "evidence_refs", evidence_refs)


@dataclass(frozen=True, slots=True)
class LearnerModelSnapshot:
    learner_id: str
    revision: int
    model_version: str
    projected_through_sequence: int
    competencies: FrozenJsonObject
    updated_at: datetime
    evidence_refs: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.learner_id, "learner_id")
        _require_integer(self.revision, "revision", minimum=0)
        _require_text(self.model_version, "model_version", 1, 128)
        _require_integer(
            self.projected_through_sequence,
            "projected_through_sequence",
            minimum=0,
        )
        _require_datetime(self.updated_at, "updated_at")
        object.__setattr__(
            self,
            "competencies",
            _freeze_mapping(self.competencies, "competencies"),
        )
        evidence_refs = _freeze_tuple(self.evidence_refs, "evidence_refs")
        if len(evidence_refs) > 64:
            raise ValueError("evidence_refs must contain at most 64 items")
        evidence_ids: set[str] = set()
        for index, evidence in enumerate(evidence_refs):
            if not isinstance(evidence, EvidenceRef):
                raise TypeError(f"evidence_refs[{index}] must be an EvidenceRef")
            if evidence.evidence_id in evidence_ids:
                raise ValueError("evidence_refs must contain unique evidence_id values")
            evidence_ids.add(evidence.evidence_id)
        object.__setattr__(self, "evidence_refs", evidence_refs)


@dataclass(frozen=True, slots=True)
class LearnerUpdate:
    learner_id: str
    previous_revision: int
    revision: int
    model_version: str
    changed_competency_ids: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_identifier(self.learner_id, "learner_id")
        _require_integer(self.previous_revision, "previous_revision", minimum=0)
        _require_integer(self.revision, "revision", minimum=1)
        if self.revision != self.previous_revision + 1:
            raise ValueError("learner revision must advance exactly once")
        _require_text(self.model_version, "model_version", 1, 128)
        _require_datetime(self.updated_at, "updated_at")
        changed_competency_ids = _freeze_tuple(
            self.changed_competency_ids,
            "changed_competency_ids",
        )
        if len(set(changed_competency_ids)) != len(changed_competency_ids):
            raise ValueError("changed_competency_ids must be unique")
        for competency_id in changed_competency_ids:
            _require_text(competency_id, "changed_competency_ids item", 1, 128)
        evidence_refs = _freeze_tuple(self.evidence_refs, "evidence_refs")
        if len(evidence_refs) > 64:
            raise ValueError("evidence_refs must contain at most 64 items")
        evidence_ids: set[str] = set()
        for index, evidence in enumerate(evidence_refs):
            if not isinstance(evidence, EvidenceRef):
                raise TypeError(f"evidence_refs[{index}] must be an EvidenceRef")
            if evidence.evidence_id in evidence_ids:
                raise ValueError("evidence_refs must contain unique evidence_id values")
            evidence_ids.add(evidence.evidence_id)
        object.__setattr__(self, "changed_competency_ids", changed_competency_ids)
        object.__setattr__(self, "evidence_refs", evidence_refs)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    audit_id: str
    occurred_at: datetime
    operation: str
    outcome: Literal["ALLOWED", "DENIED", "FAILED"]
    actor: ActorRef
    request_id: str
    correlation_id: str
    trace_id: str
    resource_type: str
    resource_id: str
    purpose: str | None
    subject_hash: str | None
    evidence_ids: tuple[str, ...]
    error_code: str | None
    details: FrozenJsonObject
    schema_version: Literal["1.0.0"] = "1.0.0"
    redacted: Literal[True] = True

    def __post_init__(self) -> None:
        _require_pattern(self.audit_id, _AUDIT_ID, "audit_id")
        _require_datetime(self.occurred_at, "occurred_at")
        _require_text(self.operation, "operation", 1, 128)
        if self.outcome not in ("ALLOWED", "DENIED", "FAILED"):
            raise ValueError("outcome must be ALLOWED, DENIED or FAILED")
        if not isinstance(self.actor, ActorRef):
            raise TypeError("actor must be an ActorRef")
        _require_pattern(self.request_id, _REQUEST_ID, "request_id")
        _require_pattern(self.correlation_id, _CORRELATION_ID, "correlation_id")
        _require_pattern(self.trace_id, _TRACE_ID, "trace_id")
        _require_pattern(self.resource_type, _UPPER_NAME, "resource_type")
        _require_text(self.resource_id, "resource_id", 1, 256)
        if self.purpose is not None:
            _require_pattern(self.purpose, _UPPER_NAME, "purpose")
        if self.subject_hash is not None:
            _require_pattern(self.subject_hash, _SHA256, "subject_hash")
        evidence_ids = _freeze_tuple(self.evidence_ids, "evidence_ids")
        if len(evidence_ids) > 64 or len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("evidence_ids must contain at most 64 unique values")
        for evidence_id in evidence_ids:
            _require_pattern(evidence_id, _EVIDENCE_ID, "evidence_ids item")
        if self.error_code is not None:
            _require_pattern(self.error_code, _ERROR_CODE, "error_code")
            if self.error_code not in _ERROR_CATALOG:
                raise ValueError(f"error_code {self.error_code} is not in the contract catalog")
        if self.schema_version != "1.0.0" or self.redacted is not True:
            raise ValueError("audit records must be schema 1.0.0 and redacted")
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "details", _freeze_mapping(self.details, "details"))


@dataclass(frozen=True, slots=True)
class AuditQuery:
    operations: tuple[str, ...] = ()
    outcomes: tuple[Literal["ALLOWED", "DENIED", "FAILED"], ...] = ()
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None
    cursor: str | None = None
    limit: int = 100

    def __post_init__(self) -> None:
        operations = _freeze_tuple(self.operations, "operations")
        outcomes = _freeze_tuple(self.outcomes, "outcomes")
        if len(set(operations)) != len(operations):
            raise ValueError("operations must be unique")
        for operation in operations:
            _require_text(operation, "operations item", 1, 128)
        if len(set(outcomes)) != len(outcomes) or any(
            outcome not in ("ALLOWED", "DENIED", "FAILED") for outcome in outcomes
        ):
            raise ValueError("outcomes must be unique audit outcomes")
        for name in ("occurred_after", "occurred_before"):
            value = getattr(self, name)
            if value is not None:
                _require_datetime(value, name)
        if (
            self.occurred_after is not None
            and self.occurred_before is not None
            and self.occurred_before <= self.occurred_after
        ):
            raise ValueError("occurred_before must be later than occurred_after")
        if self.cursor is not None:
            _require_text(self.cursor, "cursor", 1, 512)
        _require_integer(self.limit, "limit", minimum=1, maximum=1000)
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "outcomes", outcomes)


@dataclass(frozen=True, slots=True)
class FeishuReportDraftBody:
    report_id: str

    def __post_init__(self) -> None:
        _require_identifier(self.report_id, "report_id")


@dataclass(frozen=True, slots=True)
class DeliveryPayload:
    delivery_id: str
    operation: DeliveryOperation
    deduplication_key: str
    attempt: int
    body: FeishuReportDraftBody

    def __post_init__(self) -> None:
        _require_identifier(self.delivery_id, "delivery_id")
        if self.operation != "FEISHU_REPORT_DRAFT":
            raise ValueError("operation must be FEISHU_REPORT_DRAFT")
        _require_pattern(self.deduplication_key, _IDEMPOTENCY_KEY, "deduplication_key")
        _require_integer(self.attempt, "attempt", minimum=1)
        if not isinstance(self.body, FeishuReportDraftBody):
            raise TypeError("body must be a FeishuReportDraftBody")

    def to_json_object(self) -> FrozenJsonObject:
        return _freeze_mapping(
            {
                "delivery_id": self.delivery_id,
                "operation": self.operation,
                "deduplication_key": self.deduplication_key,
                "attempt": self.attempt,
                "body": {"report_id": self.body.report_id},
            },
            "delivery payload",
        )


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    delivery_id: str
    operation: DeliveryOperation
    deduplication_key: str
    report_id: str
    remote_object_id: str
    sent_at: datetime
    attempt: int
    status: Literal["SENT"] = "SENT"

    def __post_init__(self) -> None:
        _require_identifier(self.delivery_id, "delivery_id")
        if self.operation != "FEISHU_REPORT_DRAFT":
            raise ValueError("operation must be FEISHU_REPORT_DRAFT")
        _require_pattern(self.deduplication_key, _IDEMPOTENCY_KEY, "deduplication_key")
        _require_identifier(self.report_id, "report_id")
        _require_text(self.remote_object_id, "remote_object_id", 1, 512)
        _require_datetime(self.sent_at, "sent_at")
        _require_integer(self.attempt, "attempt", minimum=1)
        if self.status != "SENT":
            raise ValueError("delivery receipt status must be SENT")


FeishuPayload = DeliveryPayload


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    message_id: str
    destination: DeliveryOperation
    idempotency_key: str
    payload: DeliveryPayload
    created_at: datetime
    operation_context: OperationContext
    status: OutboxStatus = OutboxStatus.PENDING
    attempt: int = 0
    next_attempt_at: datetime | None = None
    lease_id: str | None = None
    lease_expires_at: datetime | None = None
    last_error: ContractError | None = None
    delivery_receipt: DeliveryReceipt | None = None
    dead_lettered_at: datetime | None = None
    payload_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _require_identifier(self.message_id, "message_id")
        if self.destination != "FEISHU_REPORT_DRAFT":
            raise ValueError("destination must be FEISHU_REPORT_DRAFT")
        _require_pattern(self.idempotency_key, _IDEMPOTENCY_KEY, "idempotency_key")
        if not isinstance(self.payload, DeliveryPayload):
            raise TypeError("payload must be a DeliveryPayload")
        if (
            self.payload.delivery_id != self.message_id
            or self.payload.operation != self.destination
            or self.payload.deduplication_key != self.idempotency_key
        ):
            raise ValueError("delivery payload identity must match the outbox message")
        _require_datetime(self.created_at, "created_at")
        if not isinstance(self.operation_context, OperationContext):
            raise TypeError("operation_context must be an OperationContext")
        status = OutboxStatus(self.status)
        object.__setattr__(self, "status", status)
        _require_integer(self.attempt, "attempt", minimum=0)
        for name in ("next_attempt_at", "lease_expires_at", "dead_lettered_at"):
            value = getattr(self, name)
            if value is not None:
                _require_datetime(value, name)
        if self.lease_id is not None:
            _require_text(self.lease_id, "lease_id", 1, 128)
        if self.last_error is not None and not isinstance(self.last_error, ContractError):
            raise TypeError("last_error must be a ContractError")
        if self.delivery_receipt is not None and not isinstance(
            self.delivery_receipt, DeliveryReceipt
        ):
            raise TypeError("delivery_receipt must be a DeliveryReceipt")

        if status is OutboxStatus.PENDING:
            if self.payload.attempt != 1:
                raise ValueError("PENDING delivery payload attempt must be 1")
            if self.attempt != 0 or any(
                value is not None
                for value in (
                    self.next_attempt_at,
                    self.lease_id,
                    self.lease_expires_at,
                    self.last_error,
                    self.delivery_receipt,
                    self.dead_lettered_at,
                )
            ):
                raise ValueError("PENDING outbox message cannot contain delivery state")
        elif status is OutboxStatus.SENDING:
            if self.payload.attempt != self.attempt:
                raise ValueError("delivery payload attempt must match the outbox attempt")
            if self.attempt < 1 or self.lease_id is None or self.lease_expires_at is None:
                raise ValueError("SENDING outbox message requires an attempt and lease")
            if any(
                value is not None
                for value in (
                    self.next_attempt_at,
                    self.last_error,
                    self.delivery_receipt,
                    self.dead_lettered_at,
                )
            ):
                raise ValueError("SENDING outbox message contains contradictory terminal state")
            if self.lease_expires_at < self.created_at:
                raise ValueError("lease_expires_at cannot precede outbox creation")
        elif status is OutboxStatus.SENT:
            if self.payload.attempt != self.attempt:
                raise ValueError("delivery payload attempt must match the outbox attempt")
            if self.attempt < 1 or self.delivery_receipt is None:
                raise ValueError("SENT outbox message requires a delivery receipt")
            if any(
                value is not None
                for value in (
                    self.next_attempt_at,
                    self.lease_id,
                    self.lease_expires_at,
                    self.last_error,
                    self.dead_lettered_at,
                )
            ):
                raise ValueError("SENT outbox message contains contradictory retry state")
            if (
                self.delivery_receipt.delivery_id != self.message_id
                or self.delivery_receipt.attempt != self.attempt
                or self.delivery_receipt.operation != self.payload.operation
                or self.delivery_receipt.deduplication_key != self.payload.deduplication_key
                or self.delivery_receipt.report_id != self.payload.body.report_id
            ):
                raise ValueError("delivery receipt identity must match the delivery request")
            if self.delivery_receipt.sent_at < self.created_at:
                raise ValueError("delivery receipt cannot precede outbox creation")
        elif status is OutboxStatus.RETRYING:
            if self.payload.attempt != self.attempt:
                raise ValueError("delivery payload attempt must match the outbox attempt")
            if self.attempt < 1 or self.next_attempt_at is None or self.last_error is None:
                raise ValueError("RETRYING outbox message requires schedule and error")
            if any(
                value is not None
                for value in (
                    self.lease_id,
                    self.lease_expires_at,
                    self.delivery_receipt,
                    self.dead_lettered_at,
                )
            ):
                raise ValueError("RETRYING outbox message contains contradictory lease state")
            if self.next_attempt_at < self.created_at:
                raise ValueError("next_attempt_at cannot precede outbox creation")
        elif status is OutboxStatus.DEAD_LETTER:
            if self.payload.attempt != self.attempt:
                raise ValueError("delivery payload attempt must match the outbox attempt")
            if self.attempt < 1 or self.last_error is None or self.dead_lettered_at is None:
                raise ValueError("DEAD_LETTER outbox message requires error and timestamp")
            if any(
                value is not None
                for value in (
                    self.next_attempt_at,
                    self.lease_id,
                    self.lease_expires_at,
                    self.delivery_receipt,
                )
            ):
                raise ValueError("DEAD_LETTER outbox message contains contradictory retry state")
            if self.dead_lettered_at < self.created_at:
                raise ValueError("dead_lettered_at cannot precede outbox creation")

        object.__setattr__(
            self,
            "payload_sha256",
            _json_sha256(self.payload.to_json_object()),
        )

    @property
    def idempotency_scope(self) -> tuple[str, str, str]:
        """Return the service-delivery deduplication key.

        Unlike actor-bound command receipts, one outbound delivery is a
        tenant-level side effect.  ``operation_context.actor`` remains the
        immutable audit origin but is intentionally not a scope component.
        """

        return (
            self.operation_context.actor.tenant_id,
            self.destination,
            self.idempotency_key,
        )


@dataclass(frozen=True, slots=True)
class WorldAtomicCommit:
    stream_id: str
    expected_stream_sequence: int | Literal["NO_STREAM"]
    command: WorldCommand
    events: tuple[UncommittedEvent, ...]
    outbox_messages: tuple[OutboxMessage, ...]

    def __post_init__(self) -> None:
        _require_pattern(self.stream_id, _STREAM_ID, "stream_id")
        if self.expected_stream_sequence != "NO_STREAM":
            _require_integer(
                self.expected_stream_sequence,
                "expected_stream_sequence",
                minimum=0,
            )
        if not isinstance(self.command, WorldCommand):
            raise TypeError("command must be a WorldCommand")
        events = _freeze_tuple(self.events, "events")
        if not events:
            raise ValueError("world atomic commit must contain at least one event")
        for index, event in enumerate(events):
            if not isinstance(event, UncommittedEvent):
                raise TypeError(f"events[{index}] must be an UncommittedEvent")
            if event.payload.get("world_id") not in (None, self.command.world_id):
                raise ValueError(f"events[{index}] world_id does not match command")
            if event.payload.get("run_id") not in (None, self.command.run_id):
                raise ValueError(f"events[{index}] run_id does not match command")
        first_event = events[0]
        for index, event in enumerate(events[1:], start=1):
            if (
                event.command_id,
                event.trace_id,
                event.correlation_id,
                event.content_ref,
            ) != (
                first_event.command_id,
                first_event.trace_id,
                first_event.correlation_id,
                first_event.content_ref,
            ):
                raise ValueError(f"events[{index}] operation identity does not match")

        outbox_messages = _freeze_tuple(self.outbox_messages, "outbox_messages")
        for index, message in enumerate(outbox_messages):
            if not isinstance(message, OutboxMessage):
                raise TypeError(f"outbox_messages[{index}] must be an OutboxMessage")
            if message.status is not OutboxStatus.PENDING:
                raise ValueError("world transaction can only enqueue PENDING outbox messages")
            context = message.operation_context
            if (
                context.command_id,
                context.trace_id,
                context.correlation_id,
                context.content_ref,
            ) != (
                first_event.command_id,
                first_event.trace_id,
                first_event.correlation_id,
                first_event.content_ref,
            ):
                raise ValueError(f"outbox_messages[{index}] operation identity does not match")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "outbox_messages", outbox_messages)


@dataclass(frozen=True, slots=True)
class WorldAtomicCommitReceipt:
    stream_id: str
    world: WorldCommitReceipt
    events: EventAppendReceipt
    outbox_messages: tuple[OutboxMessage, ...]

    def __post_init__(self) -> None:
        _require_pattern(self.stream_id, _STREAM_ID, "stream_id")
        if not isinstance(self.world, WorldCommitReceipt):
            raise TypeError("world must be a WorldCommitReceipt")
        if not isinstance(self.events, EventAppendReceipt):
            raise TypeError("events must be an EventAppendReceipt")
        if self.events.stream_id != self.stream_id:
            raise ValueError("event receipt stream_id must match atomic commit stream_id")
        outbox_messages = _freeze_tuple(self.outbox_messages, "outbox_messages")
        for index, message in enumerate(outbox_messages):
            if not isinstance(message, OutboxMessage):
                raise TypeError(f"outbox_messages[{index}] must be an OutboxMessage")
            if message.status is not OutboxStatus.PENDING:
                raise ValueError("atomic commit receipt must return enqueued PENDING messages")
        object.__setattr__(self, "outbox_messages", outbox_messages)
        if self.world.first_event_sequence != self.events.previous_sequence + 1:
            raise ValueError("world and event receipts disagree on first sequence")
        if self.world.last_event_sequence != self.events.next_sequence:
            raise ValueError("world and event receipts disagree on last sequence")


@dataclass(frozen=True, slots=True)
class CursorPage[U]:
    items: tuple[U, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", _freeze_tuple(self.items, "items"))
        if self.next_cursor is not None:
            _require_text(self.next_cursor, "next_cursor", 1, 512)


__all__ = [
    "ActionIntent",
    "ActivateSkillInput",
    "ActiveSkill",
    "ActorRef",
    "ActorType",
    "AgentTurnFeedback",
    "AgentTurnFeedbackSource",
    "AuditQuery",
    "AuditRecord",
    "BuildArtifact",
    "canonical_json_sha256",
    "canonical_json_v1",
    "CertificationEvidence",
    "CertifiedSkill",
    "CommandRecord",
    "CommandCreateReceipt",
    "CommandStatus",
    "CommandTransition",
    "CommandType",
    "CompileAndTestRequest",
    "ContentRef",
    "ContractError",
    "CursorPage",
    "DeliveryOperation",
    "DeliveryPayload",
    "DeliveryReceipt",
    "DomainEvent",
    "ErrorCategory",
    "EventAppendReceipt",
    "EvidenceRef",
    "EvidenceType",
    "Failure",
    "FeishuPayload",
    "FeishuReportDraftBody",
    "FrozenJsonObject",
    "FrozenJsonValue",
    "HarvestIntent",
    "InteractIntent",
    "learner_inference_sha256",
    "LearnerInferenceRole",
    "LearnerModelSnapshot",
    "LearnerUpdate",
    "LlmMessage",
    "LlmReply",
    "LlmReplySource",
    "LlmRequest",
    "MoveIntent",
    "NewCommand",
    "OperationContext",
    "OutboxMessage",
    "OutboxStatus",
    "PlantIntent",
    "PolicyGrant",
    "PolicyInput",
    "RegistrySnapshot",
    "RequestContext",
    "RealtimeAckFrame",
    "RealtimeBootstrap",
    "RealtimeCheckpoint",
    "RealtimeClientFrame",
    "RealtimeCloseCode",
    "RealtimeErrorFrame",
    "RealtimeHeartbeatAckFrame",
    "RealtimeHeartbeatFrame",
    "RealtimeProtocolVersion",
    "RealtimeResumeFrame",
    "RealtimeServerControlFrame",
    "RealtimeServerFrame",
    "RealtimeSubscribeFrame",
    "RealtimeSubscribedFrame",
    "RealtimeWorldEvent",
    "Result",
    "RuntimeEvent",
    "RuntimeEventType",
    "SandboxLimits",
    "SandboxRunRequest",
    "SandboxRunResult",
    "SandboxUsage",
    "SkillRef",
    "SkillSourceBundle",
    "SkillSourceFile",
    "SpeakIntent",
    "StudentActivationScope",
    "StudentActiveSkill",
    "StudentBootstrapActivation",
    "StudentBootstrapBuild",
    "StudentBootstrapCapabilities",
    "StudentBootstrapSession",
    "StudentBootstrapV2",
    "StudentBootstrapWorld",
    "StudentSessionCreateRequest",
    "Success",
    "TestCaseResult",
    "UncommittedEvent",
    "VersionSet",
    "WaterIntent",
    "WorldAtomicCommit",
    "WorldAtomicCommitReceipt",
    "WorldCommand",
    "WorldCommitReceipt",
    "WorldPosition",
    "WorldSnapshot",
]
