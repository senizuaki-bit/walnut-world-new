"""Stable immutable identities for Agent runtime audit traces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .workflow_jobs import workflow_json_sha256

AGENT_TRACE_OPERATION = "AGENT_RUNTIME_TRACE"
AGENT_TRACE_OUTCOME = "SUCCESS"

_SINGLE_AGENT_TRACE_EVENTS = frozenset(
    {
        "agent.turn.failed",
        "agent.turn.finished",
        "agent.turn.recovered",
        "agent.turn.started",
    }
)
_INDEXED_AGENT_TRACE_EVENTS = {
    "agent.model.requested": "request_number",
    "agent.output.invalid": "repair_attempt",
    "agent.tool.failed": "execution_id",
    "agent.tool.rejected": "execution_id",
    "agent.tool.started": "execution_id",
    "agent.tool.succeeded": "execution_id",
}


class AgentTraceIdentityError(ValueError):
    """A trace cannot resolve to one supported immutable audit identity."""


def agent_trace_audit_id(tenant_id: str, record_json: Mapping[str, Any]) -> str:
    name = record_json.get("name")
    fields = record_json.get("fields")
    if not isinstance(name, str) or not isinstance(fields, Mapping):
        raise AgentTraceIdentityError("Agent trace identity fields are invalid")
    if name in _SINGLE_AGENT_TRACE_EVENTS:
        occurrence: object = "single"
    else:
        discriminator = _INDEXED_AGENT_TRACE_EVENTS.get(name)
        if discriminator is None:
            raise AgentTraceIdentityError("Agent trace event name is unsupported")
        occurrence = fields.get(discriminator)
        if discriminator in {"request_number", "repair_attempt"}:
            if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 1:
                raise AgentTraceIdentityError(
                    "Agent trace numeric occurrence discriminator is invalid"
                )
        elif not isinstance(occurrence, str) or not occurrence:
            raise AgentTraceIdentityError(
                "Agent trace execution occurrence discriminator is invalid"
            )
    identity = {
        "tenant_id": tenant_id,
        "operation": AGENT_TRACE_OPERATION,
        "outcome": AGENT_TRACE_OUTCOME,
        "command_id": record_json.get("command_id"),
        "trace_id": record_json.get("trace_id"),
        "turn_id": record_json.get("turn_id"),
        "role": record_json.get("role"),
        "name": name,
        "occurrence": occurrence,
    }
    if any(
        not isinstance(identity[key], str) or not identity[key]
        for key in ("tenant_id", "command_id", "trace_id", "turn_id", "role")
    ):
        raise AgentTraceIdentityError("Agent trace scope identity is invalid")
    workflow_json_sha256(record_json)
    return f"audit_{workflow_json_sha256(identity)[:32]}"


__all__ = [
    "AGENT_TRACE_OPERATION",
    "AGENT_TRACE_OUTCOME",
    "AgentTraceIdentityError",
    "agent_trace_audit_id",
]
