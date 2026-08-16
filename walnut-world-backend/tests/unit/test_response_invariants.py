"""Regression coverage for cross-field response invariants outside JSON Schema."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from yaya_agent_contracts import ActorRef, ActorType, ContentRef, OperationContext

from walnut_backend.api.errors import TransportError
from walnut_backend.api.response_validation import validate_semantic_invariants


def test_world_event_page_rejects_a_schema_shaped_sequence_mismatch() -> None:
    context = OperationContext(
        request_id="req_response_invariant_0001",
        correlation_id="corr_response_invariant_0001",
        trace_id="trace_response_invariant_0001",
        requested_at=datetime.now(UTC),
        actor=ActorRef("tenant_yaya", "student_invariant", ActorType.STUDENT, ("game:player",)),
        content_ref=ContentRef("UNIT_INVARIANT", "1.0.0", "a" * 64),
        schema_version="1.0.0",
        command_id="cmd_response_invariant_0001",
        causation_id=None,
    )
    payload = {
        "request_context": {
            "schema_version": "1.0.0",
            "request_id": context.request_id,
            "correlation_id": context.correlation_id,
            "trace_id": context.trace_id,
            "requested_at": context.requested_at.isoformat(),
            "actor": {
                "tenant_id": context.actor.tenant_id,
                "actor_id": context.actor.actor_id,
                "actor_type": context.actor.actor_type.value,
                "roles": list(context.actor.roles),
            },
            "content_ref": {
                "unit_id": context.content_ref.unit_id,
                "version": context.content_ref.version,
                "content_hash": context.content_ref.content_hash,
            },
        },
        "world_id": "world_invariant_0001",
        "snapshot_revision": 1,
        "from_sequence": 1,
        "to_sequence": 2,
        "has_more": False,
        "next_after_sequence": 2,
        "events": [
            {"event_id": "evt_invariant_0001", "sequence": 2, "stream_id": "world:world_invariant_0001"}
        ],
    }
    with pytest.raises(TransportError):
        validate_semantic_invariants(
            "contracts/schemas/game/world-event-page.schema.json", payload, context, {}
        )
