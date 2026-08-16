"""Stable logical identities for retry-safe Agent runtime traces."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from yaya_agent_contracts import ActorRef, ActorType, ContentRef, OperationContext
from yaya_agent_runtime import AgentTraceEvent

from walnut_backend.adapters.postgres.agent_runtime import (
    AgentRuntimeAuthorityError,
    _agent_trace_audit_id,
    _agent_trace_record,
)


def test_trace_identity_collapses_exact_retry_and_exposes_payload_drift() -> None:
    context = _context()
    event = AgentTraceEvent(
        "agent.model.requested",
        "turn_trace_identity_0001",
        "teaching_agent",
        {
            "request_number": 1,
            "message_count": 2,
            "tool_round_complete": False,
            "confidence": 0.8,
        },
    )
    record = _agent_trace_record(event, context)
    audit_id = _agent_trace_audit_id(context.actor.tenant_id, record)

    assert audit_id == _agent_trace_audit_id(
        context.actor.tenant_id,
        _agent_trace_record(event, context),
    )

    drifted = {**record, "fields": {**record["fields"], "message_count": 3}}
    assert _agent_trace_audit_id(context.actor.tenant_id, drifted) == audit_id

    next_occurrence = {
        **record,
        "fields": {**record["fields"], "request_number": 2},
    }
    assert _agent_trace_audit_id(context.actor.tenant_id, next_occurrence) != audit_id
    assert _agent_trace_audit_id("tenant_trace_identity_other", record) != audit_id


@pytest.mark.parametrize(
    ("name", "fields"),
    [
        ("agent.unknown", {}),
        ("agent.model.requested", {"request_number": True}),
        ("agent.model.requested", {"request_number": 0}),
        ("agent.output.invalid", {"repair_attempt": "1"}),
        ("agent.tool.started", {"execution_id": ""}),
    ],
)
def test_trace_identity_rejects_unknown_or_invalid_occurrences(
    name: str,
    fields: dict[str, object],
) -> None:
    record = {
        "name": name,
        "turn_id": "turn_trace_identity_0001",
        "role": "teaching_agent",
        "fields": fields,
        "command_id": "cmd_trace_identity_0001",
        "trace_id": "trace_trace_identity_0001",
    }

    with pytest.raises(AgentRuntimeAuthorityError, match="trace"):
        _agent_trace_audit_id("tenant_trace_identity", record)


def test_single_trace_occurrence_is_stable_across_field_drift() -> None:
    context = _context()
    first = _agent_trace_record(
        AgentTraceEvent(
            "agent.turn.finished",
            "turn_trace_identity_0001",
            "teaching_agent",
            {"validated": True, "input_tokens": 5},
        ),
        context,
    )
    drifted = {**first, "fields": {"validated": True, "input_tokens": 6}}

    assert _agent_trace_audit_id(
        context.actor.tenant_id,
        first,
    ) == _agent_trace_audit_id(context.actor.tenant_id, drifted)


def _context() -> OperationContext:
    return OperationContext(
        request_id="req_trace_identity_0001",
        correlation_id="corr_trace_identity_0001",
        trace_id="trace_trace_identity_0001",
        requested_at=datetime(2026, 8, 12, 14, 0, tzinfo=UTC),
        actor=ActorRef(
            "tenant_trace_identity",
            "student_trace_identity",
            ActorType.STUDENT,
            ("game:player",),
        ),
        content_ref=ContentRef("UNIT_TRACE", "1.0.0", "a" * 64),
        command_id="cmd_trace_identity_0001",
        causation_id=None,
    )
