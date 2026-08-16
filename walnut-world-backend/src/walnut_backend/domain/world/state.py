"""Immutable-facing state utilities and explicit domain-rule failures."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from yaya_agent_contracts import canonical_json_sha256


@dataclass(frozen=True, slots=True)
class WorldRuleViolation(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def mutable_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-copy the contract state before the deterministic reducer changes it."""
    return _thaw(state)


def _thaw(value: Any) -> Any:
    """Convert frozen contract mappings/tuples into an independently mutable tree."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def state_hash(state: Mapping[str, Any]) -> str:
    return canonical_json_sha256(state)
