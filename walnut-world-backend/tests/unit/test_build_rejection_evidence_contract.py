"""Public contract closure for rejected Build Evidence."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    OperationContext,
    canonical_json_sha256,
)

from walnut_backend.api.errors import TransportError
from walnut_backend.api.response_validation import validate_semantic_invariants
from walnut_backend.api.routes.game_reads import _evidence_schema_path
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, ContractRelease, Settings

SCHEMA = "contracts/schemas/game/build-rejection-evidence.schema.json"


def test_rejected_build_evidence_uses_the_additive_public_schema() -> None:
    context = _context()
    evidence = _evidence(context)
    release = ContractRelease(Settings.for_test(contract_path=DEFAULT_CONTRACT_PATH))

    assert _evidence_schema_path(evidence) == SCHEMA
    assert release.validate(SCHEMA, evidence) == []
    validate_semantic_invariants(SCHEMA, evidence, context, _etag(evidence))


def test_rejected_build_evidence_rejects_another_build_source() -> None:
    context = _context()
    evidence = _evidence(context)
    source = evidence["source"]
    assert isinstance(source, dict)
    source["source_id"] = "build_other_00000001"

    with pytest.raises(TransportError):
        validate_semantic_invariants(SCHEMA, evidence, context, _etag(evidence))


def _context() -> OperationContext:
    return OperationContext(
        request_id="req_build_rejection_00000001",
        correlation_id="corr_build_rejection_00000001",
        trace_id="trace_build_rejection_00000001",
        requested_at=datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC),
        actor=ActorRef(
            "tenant_yaya",
            "student_demo_00000001",
            ActorType.STUDENT,
            ("game:player",),
        ),
        content_ref=ContentRef("YAYA_FARM_001", "1.0.0", "a" * 64),
        command_id="cmd_build_rejection_00000001",
        causation_id=None,
    )


def _evidence(context: OperationContext) -> dict[str, object]:
    payload = {
        "evidence_kind": "BUILD_REJECTION",
        "build_id": "build_rejected_00000001",
        "skill_id": "skill_watering_00000001",
        "test_suite_version": "watering-suite-v1",
        "outcome": "REJECTED",
        "failure_stage": "VALIDATE",
        "failure_code": "CPP_COMPILE_FAILED",
        "diagnostic_codes": ["CPP_PARSE_ERROR"],
    }
    digest = canonical_json_sha256(payload)
    requested_at = context.requested_at.isoformat().replace("+00:00", "Z")
    return {
        "request_context": {
            "schema_version": context.schema_version,
            "request_id": context.request_id,
            "correlation_id": context.correlation_id,
            "trace_id": context.trace_id,
            "requested_at": requested_at,
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
        "evidence_ref": {
            "evidence_id": "evidence_build_rejection_00000001",
            "evidence_type": "TEST_REPORT",
            "created_at": requested_at,
            "sha256": digest,
        },
        "subject": {"learner_id": "student_demo_00000001"},
        "source": {
            "source_type": "SKILL_BUILD",
            "source_id": payload["build_id"],
            "command_id": context.command_id,
            "world_id": "world_watering_00000001",
        },
        "occurred_at": requested_at,
        "recorded_at": requested_at,
        "integrity": {"payload_sha256": digest, "previous_evidence_sha256": None},
        "payload": payload,
        "related_evidence": [],
        "versions": {
            "api_version": "1.0.0",
            "event_version": "1.0.0",
            "policy_version": "1.0.0",
            "world_rules_version": "1.0.0",
            "teaching_spec_version": "1.0.0",
        },
    }


def _etag(evidence: dict[str, object]) -> dict[str, str]:
    integrity = evidence["integrity"]
    assert isinstance(integrity, dict)
    return {"ETag": f'"{integrity["payload_sha256"]}"'}
