"""Strict role configuration loader.

Role files use the JSON subset of YAML 1.2.  This keeps the documented YAML
deployment format while allowing a dependency-free, duplicate-key-rejecting
loader.  Unsupported YAML syntax fails loudly instead of being guessed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Protocol, cast

from .domain import GameEventType, RoleId
from .errors import AgentConfigurationError

_ROLE_IDS: tuple[RoleId, ...] = (
    "world_agent",
    "xiaohutao",
    "teaching_agent",
    "bug_agent",
    "book_agent",
)
_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "task_started",
        "compile_succeeded",
        "compile_failed",
        "run_skill_requested",
        "run_succeeded",
        "run_failed",
        "task_completed",
        "hint_requested",
        "skill_patch_requested",
        "skill_patch_confirmed",
    }
)
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_TOP_LEVEL_KEYS = frozenset(
    {
        "id",
        "display_name",
        "purpose",
        "allowed_events",
        "allowed_tools",
        "response_schema",
        "temperature",
        "max_output_tokens",
        "timeout_ms",
        "prompt",
        "limits",
    }
)
_LIMIT_KEYS = frozenset(
    {
        "max_tool_calls",
        "max_message_chars",
        "allow_skill_patch",
        "require_confirmation_for_patch",
    }
)


def _configuration_error(code: str, message: str, **details: object) -> AgentConfigurationError:
    return AgentConfigurationError(code, message, details)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _configuration_error(
                "ROLE_CONFIG_DUPLICATE_KEY",
                f"role configuration contains duplicate key {key}",
                key=key,
            )
        value[key] = item
    return value


def _expect_exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise _configuration_error(
            "ROLE_CONFIG_KEYS_MISMATCH",
            f"{label} must use the exact supported key set",
            missing=sorted(expected - actual),
            extra=sorted(actual - expected),
        )


def _text(value: object, field_name: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise _configuration_error(
            "ROLE_CONFIG_INVALID_FIELD",
            f"{field_name} length must be between {minimum} and {maximum}",
        )
    return value


def _integer(value: object, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _configuration_error(
            "ROLE_CONFIG_INVALID_FIELD",
            f"{field_name} must be an integer between {minimum} and {maximum}",
        )
    return value


def _string_array(value: object, field_name: str, *, maximum: int) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _configuration_error(
            "ROLE_CONFIG_INVALID_FIELD",
            f"{field_name} must be an array",
        )
    items = tuple(cast(Sequence[object], value))
    if any(not isinstance(item, str) for item in items):
        raise _configuration_error(
            "ROLE_CONFIG_INVALID_FIELD",
            f"{field_name} must contain only strings",
        )
    string_items = cast(tuple[str, ...], items)
    if len(string_items) > maximum or len(set(string_items)) != len(string_items):
        raise _configuration_error(
            "ROLE_CONFIG_INVALID_FIELD",
            f"{field_name} must contain at most {maximum} unique strings",
        )
    return string_items


@dataclass(frozen=True, slots=True)
class RoleLimits:
    max_tool_calls: int
    max_message_chars: int
    allow_skill_patch: bool
    require_confirmation_for_patch: bool

    def __post_init__(self) -> None:
        _integer(self.max_tool_calls, "limits.max_tool_calls", 0, 10)
        _integer(self.max_message_chars, "limits.max_message_chars", 1, 4000)
        if not isinstance(self.allow_skill_patch, bool):
            raise _configuration_error(
                "ROLE_CONFIG_INVALID_FIELD",
                "limits.allow_skill_patch must be boolean",
            )
        if not isinstance(self.require_confirmation_for_patch, bool):
            raise _configuration_error(
                "ROLE_CONFIG_INVALID_FIELD",
                "limits.require_confirmation_for_patch must be boolean",
            )
        if self.require_confirmation_for_patch and not self.allow_skill_patch:
            raise _configuration_error(
                "ROLE_CONFIG_INVALID_LIMITS",
                "patch confirmation cannot be required when patches are disabled",
            )


@dataclass(frozen=True, slots=True)
class RoleConfig:
    id: RoleId
    display_name: str
    purpose: str
    allowed_events: tuple[GameEventType, ...]
    allowed_tools: tuple[str, ...]
    response_schema: str
    temperature: float
    max_output_tokens: int
    timeout_ms: int
    prompt: str
    limits: RoleLimits

    def __post_init__(self) -> None:
        if self.id not in _ROLE_IDS:
            raise _configuration_error("ROLE_CONFIG_INVALID_ID", "role id is not supported")
        _text(self.display_name, "display_name", 1, 80)
        _text(self.purpose, "purpose", 1, 1000)
        events = tuple(self.allowed_events)
        if not events or len(events) != len(set(events)):
            raise _configuration_error(
                "ROLE_CONFIG_INVALID_EVENTS",
                "allowed_events must contain unique values",
            )
        if any(item not in _EVENT_TYPES for item in events):
            raise _configuration_error(
                "ROLE_CONFIG_INVALID_EVENTS",
                "allowed_events contains an unsupported event",
            )
        tools = tuple(self.allowed_tools)
        if len(tools) != len(set(tools)) or any(not _TOOL_NAME.fullmatch(item) for item in tools):
            raise _configuration_error(
                "ROLE_CONFIG_INVALID_TOOLS",
                "allowed_tools contains a duplicate or invalid tool name",
            )
        if self.response_schema != "AgentDecisionV1":
            raise _configuration_error(
                "ROLE_CONFIG_INVALID_SCHEMA",
                "response_schema must be AgentDecisionV1",
            )
        if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)):
            raise _configuration_error(
                "ROLE_CONFIG_INVALID_FIELD",
                "temperature must be numeric",
            )
        if not 0 <= self.temperature <= 2:
            raise _configuration_error(
                "ROLE_CONFIG_INVALID_FIELD",
                "temperature must be between 0 and 2",
            )
        _integer(self.max_output_tokens, "max_output_tokens", 1, 32_768)
        _integer(self.timeout_ms, "timeout_ms", 1, 120_000)
        _text(self.prompt, "prompt", 1, 20_000)
        if not isinstance(self.limits, RoleLimits):
            raise _configuration_error(
                "ROLE_CONFIG_INVALID_LIMITS",
                "limits must be a RoleLimits value",
            )
        object.__setattr__(self, "allowed_events", events)
        object.__setattr__(self, "allowed_tools", tools)


class RoleConfigProvider(Protocol):
    def get(self, role: RoleId) -> RoleConfig: ...


class PackagedRoleConfigProvider:
    """Load and validate all five role files once at process startup."""

    def __init__(self, configs: Mapping[RoleId, RoleConfig]) -> None:
        actual = frozenset(configs)
        expected = frozenset(_ROLE_IDS)
        if actual != expected:
            raise _configuration_error(
                "ROLE_CONFIG_CATALOG_MISMATCH",
                "the role catalog must contain exactly five roles",
                missing=sorted(expected - actual),
                extra=sorted(actual - expected),
            )
        self._configs = dict(configs)

    @classmethod
    def load(cls) -> PackagedRoleConfigProvider:
        role_root = files("yaya_agent_runtime.config.roles")
        loaded: dict[RoleId, RoleConfig] = {}
        for role in _ROLE_IDS:
            resource = role_root.joinpath(f"{role}.yaml")
            raw = resource.read_text(encoding="utf-8")
            loaded[role] = parse_role_config(raw, expected_role=role)
        return cls(loaded)

    def get(self, role: RoleId) -> RoleConfig:
        try:
            return self._configs[role]
        except KeyError as error:
            raise _configuration_error(
                "ROLE_CONFIG_NOT_FOUND",
                f"role configuration was not loaded for {role}",
                role=role,
            ) from error


def parse_role_config(raw: str, *, expected_role: RoleId) -> RoleConfig:
    try:
        parsed = json.loads(raw, object_pairs_hook=_strict_object)
    except AgentConfigurationError:
        raise
    except (json.JSONDecodeError, UnicodeError) as error:
        raise _configuration_error(
            "ROLE_CONFIG_PARSE_FAILED",
            "role YAML must use the supported JSON-compatible YAML 1.2 subset",
            line=getattr(error, "lineno", None),
            column=getattr(error, "colno", None),
        ) from error
    if not isinstance(parsed, Mapping):
        raise _configuration_error(
            "ROLE_CONFIG_INVALID_ROOT", "role configuration must be an object"
        )
    value = cast(Mapping[str, object], parsed)
    _expect_exact_keys(value, _TOP_LEVEL_KEYS, "role configuration")
    if value["id"] != expected_role:
        raise _configuration_error(
            "ROLE_CONFIG_ID_MISMATCH",
            "role file name and id do not match",
            expected=expected_role,
            actual=value["id"],
        )
    limits_raw = value["limits"]
    if not isinstance(limits_raw, Mapping):
        raise _configuration_error("ROLE_CONFIG_INVALID_LIMITS", "limits must be an object")
    limits_value = cast(Mapping[str, object], limits_raw)
    _expect_exact_keys(limits_value, _LIMIT_KEYS, "limits")
    limits = RoleLimits(
        max_tool_calls=_integer(limits_value["max_tool_calls"], "limits.max_tool_calls", 0, 10),
        max_message_chars=_integer(
            limits_value["max_message_chars"],
            "limits.max_message_chars",
            1,
            4000,
        ),
        allow_skill_patch=cast(bool, limits_value["allow_skill_patch"]),
        require_confirmation_for_patch=cast(
            bool,
            limits_value["require_confirmation_for_patch"],
        ),
    )
    allowed_events = _string_array(value["allowed_events"], "allowed_events", maximum=10)
    allowed_tools = _string_array(value["allowed_tools"], "allowed_tools", maximum=32)
    return RoleConfig(
        id=expected_role,
        display_name=_text(value["display_name"], "display_name", 1, 80),
        purpose=_text(value["purpose"], "purpose", 1, 1000),
        allowed_events=cast(tuple[GameEventType, ...], allowed_events),
        allowed_tools=allowed_tools,
        response_schema=_text(value["response_schema"], "response_schema", 1, 64),
        temperature=cast(float, value["temperature"]),
        max_output_tokens=_integer(value["max_output_tokens"], "max_output_tokens", 1, 32_768),
        timeout_ms=_integer(value["timeout_ms"], "timeout_ms", 1, 120_000),
        prompt=_text(value["prompt"], "prompt", 1, 20_000),
        limits=limits,
    )


__all__ = [
    "PackagedRoleConfigProvider",
    "RoleConfig",
    "RoleConfigProvider",
    "RoleLimits",
    "parse_role_config",
]
