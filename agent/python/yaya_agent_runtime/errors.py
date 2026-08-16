"""Typed, machine-readable failures for the Agent application layer.

The runtime deliberately does not return ``None`` or an empty mapping when a
boundary fails.  Expected model degradation is represented by a fallback
``AgentDecision``; programming, configuration and identity failures raise one
of the errors below and must be mapped by the inbound application adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class RuntimeBoundaryStage(StrEnum):
    """Fixed substages for unexpected value failures inside ``run``."""

    LLM_GENERATE = "LLM_GENERATE"
    PARSE_MODEL_ENVELOPE = "PARSE_MODEL_ENVELOPE"
    VALIDATE_DECISION = "VALIDATE_DECISION"
    MERGE_EVIDENCE = "MERGE_EVIDENCE"
    DECISION_TIME = "DECISION_TIME"
    CONSTRUCT_AGENT_DECISION = "CONSTRUCT_AGENT_DECISION"


class RuntimeBoundaryError(RuntimeError):
    """Redacted unexpected ``ValueError`` at one fixed runtime substage."""

    stage: RuntimeBoundaryStage

    def __init__(self, stage: RuntimeBoundaryStage) -> None:
        if not isinstance(stage, RuntimeBoundaryStage):
            raise TypeError("stage must be a RuntimeBoundaryStage")
        super().__init__("agent runtime rejected a value at a fixed boundary")
        self.stage = stage


class AgentRuntimeError(RuntimeError):
    """Base class carrying a stable code and redacted details."""

    code: str
    details: Mapping[str, object]

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(details or {}))


class AgentConfigurationError(AgentRuntimeError):
    """Role configuration is incomplete, duplicated or internally unsafe."""


class AgentContextError(AgentRuntimeError):
    """A context port returned data that does not belong to this turn."""


class AgentDependencyError(AgentRuntimeError):
    """A non-LLM dependency could not provide a trustworthy result."""


class InvalidAgentOutput(AgentRuntimeError):
    """Provider output parsed structurally but violated runtime policy."""


class AgentToolError(AgentRuntimeError):
    """Base class for closed-schema tool failures."""


class AgentToolAuthorizationError(AgentToolError):
    """A role attempted to use a tool outside its allowlist."""


class AgentToolInputError(AgentToolError):
    """A tool call did not match its declared closed input schema."""


class AgentToolExecutionError(AgentToolError):
    """A tool handler failed without producing a valid result."""


class AgentPersistenceError(AgentRuntimeError):
    """The durable turn commit failed; callers must not report success."""


__all__ = [
    "AgentConfigurationError",
    "AgentContextError",
    "AgentDependencyError",
    "AgentPersistenceError",
    "AgentRuntimeError",
    "AgentToolAuthorizationError",
    "AgentToolError",
    "AgentToolExecutionError",
    "AgentToolInputError",
    "InvalidAgentOutput",
    "RuntimeBoundaryError",
    "RuntimeBoundaryStage",
]
