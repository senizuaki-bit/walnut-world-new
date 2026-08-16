"""EvidenceRef public serializers use one byte-stable UTC representation."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    CommandRecord,
    CommandStatus,
    ContentRef,
    ContractError,
    ErrorCategory,
    EvidenceRef,
    EvidenceType,
    FrozenJsonObject,
    RequestContext,
    VersionSet,
)

from walnut_backend.adapters.postgres import skill_invocation
from walnut_backend.adapters.postgres.models import (
    command_record_data,
    command_record_from_data,
)
from walnut_backend.application.game.queries import public_command_record_data


def test_command_and_run_evidence_refs_share_canonical_utc_wire() -> None:
    created_at = datetime(
        2026,
        8,
        12,
        14,
        29,
        43,
        123456,
        tzinfo=timezone(timedelta(hours=8)),
    )
    reference = EvidenceRef(
        evidence_id="evidence_serializer_unit_0001",
        evidence_type=EvidenceType.SANDBOX_LOG,
        created_at=created_at,
        sha256="a" * 64,
    )
    command = _command(reference)

    durable_command_wire = command_record_data(command)
    public_command_wire = public_command_record_data(command)
    run_reference_wire = skill_invocation._evidence_ref_wire(  # pyright: ignore[reportPrivateUsage]
        reference
    )

    assert durable_command_wire["evidence_refs"][0]["created_at"] == (
        "2026-08-12T14:29:43.123456+08:00"
    )
    assert public_command_wire["evidence_refs"] == [run_reference_wire]
    assert run_reference_wire["created_at"] == "2026-08-12T06:29:43.123456Z"
    assert "uri" not in run_reference_wire


def test_legacy_utc_offset_command_ref_reads_and_reemits_canonical_z() -> None:
    reference = EvidenceRef(
        evidence_id="evidence_serializer_unit_0002",
        evidence_type=EvidenceType.SANDBOX_LOG,
        created_at=datetime(2026, 8, 12, 6, 29, 43, 123456, tzinfo=UTC),
        sha256="b" * 64,
    )
    legacy = copy.deepcopy(command_record_data(_command(reference)))
    evidence_refs = legacy["evidence_refs"]
    assert isinstance(evidence_refs, list)
    evidence_refs[0]["created_at"] = "2026-08-12T06:29:43.123456+00:00"

    restored = command_record_from_data(legacy)

    assert command_record_data(restored)["evidence_refs"][0]["created_at"] == (
        "2026-08-12T06:29:43.123456+00:00"
    )
    assert public_command_record_data(restored)["evidence_refs"][0]["created_at"] == (
        "2026-08-12T06:29:43.123456Z"
    )


def _command(reference: EvidenceRef) -> CommandRecord:
    now = datetime(2026, 8, 12, 6, 29, 44, tzinfo=UTC)
    context = RequestContext(
        request_id="req_evidence_serializer_unit_0001",
        correlation_id="corr_evidence_serializer_unit_0001",
        trace_id="trace_evidence_serializer_unit_0001",
        requested_at=now,
        actor=ActorRef(
            "tenant_evidence_serializer",
            "student_evidence_serializer",
            ActorType.STUDENT,
            ("game:player",),
        ),
        content_ref=ContentRef("UNIT_SERIALIZER", "1.0.0", "c" * 64),
    )
    return CommandRecord(
        request_context=context,
        command_id="cmd_evidence_serializer_unit_0001",
        command_type="EXECUTE_AGENT_TURN",
        status=CommandStatus.REJECTED,
        stage="WORLD_VALIDATE",
        terminal=True,
        accepted_at=now,
        updated_at=now,
        result=None,
        error=ContractError(
            code="WORLD_RULE_REJECTED",
            category=ErrorCategory.WORLD_RULE,
            retryable=False,
            user_message_key="world.rule_rejected",
            stage="WORLD_VALIDATE",
            message="The objective was not completed.",
        ),
        evidence_refs=(reference,),
        versions=VersionSet(
            api_version="1.0.0",
            event_version="1.0.0",
            policy_version="policy-1",
            world_rules_version="rules-1",
            teaching_spec_version="teaching-1",
        ),
        links=cast(
            FrozenJsonObject,
            {
                "self": "/v1/commands/cmd_evidence_serializer_unit_0001",
                "run": "/v1/runs/run_serializer_unit_0001",
            },
        ),
    )
