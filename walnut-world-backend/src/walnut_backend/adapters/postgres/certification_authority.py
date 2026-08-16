"""One strict Certification→Artifact→Build provenance authority validator."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yaya_agent_contracts import canonical_json_sha256

from walnut_backend.certified_skill_schema import (
    CertifiedSkillSchemaError,
    validated_certified_parameter_schema,
)

from .models import (
    BuildPolicyRow,
    CommandRow,
    JobStepReceiptRow,
    SkillArtifactRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
    SkillCertificationProvenanceRow,
    SkillCertificationRow,
    WorkflowJobRow,
)
from .skill_provenance import validate_build_provenance
from .workflow_jobs import workflow_receipt_sha256, workflow_step_receipt_id


async def validate_certification_authority(
    session: AsyncSession,
    certification: SkillCertificationRow,
    *,
    expected_certification_sha256: str | None = None,
    expected_artifact_authority_sha256: str | None = None,
    expected_build_provenance_sha256: str | None = None,
    for_update: bool = False,
) -> tuple[SkillArtifactRow, SkillBuildProvenanceRow] | None:
    wire = certification.certification_json
    artifact = await session.scalar(
        _locked(select(SkillArtifactRow).where(
            SkillArtifactRow.tenant_id == certification.tenant_id,
            SkillArtifactRow.actor_id == certification.actor_id,
            SkillArtifactRow.content_hash == certification.content_hash,
            SkillArtifactRow.build_id == certification.build_id,
            SkillArtifactRow.skill_id == certification.skill_id,
            SkillArtifactRow.artifact_sha256 == certification.artifact_sha256,
        ), for_update)
    )
    policy = (
        await session.scalar(
            _locked(select(BuildPolicyRow).where(
                BuildPolicyRow.tenant_id == certification.tenant_id,
                BuildPolicyRow.actor_id == certification.actor_id,
                BuildPolicyRow.content_hash == certification.content_hash,
                BuildPolicyRow.build_policy_id == wire.get("build_policy_id"),
                BuildPolicyRow.policy_sha256 == wire.get("policy_sha256"),
            ), for_update)
        )
        if isinstance(wire, Mapping)
        else None
    )
    provenance = (
        await session.scalar(
            _locked(select(SkillBuildProvenanceRow).where(
                SkillBuildProvenanceRow.build_id == certification.build_id,
                SkillBuildProvenanceRow.tenant_id == certification.tenant_id,
                SkillBuildProvenanceRow.actor_id == certification.actor_id,
                SkillBuildProvenanceRow.skill_id == certification.skill_id,
                SkillBuildProvenanceRow.source_bundle_sha256 == artifact.source_sha256,
            ), for_update)
        )
        if artifact is not None
        else None
    )
    build = await session.scalar(
        _locked(select(SkillBuildRow).where(
            SkillBuildRow.build_id == certification.build_id,
            SkillBuildRow.tenant_id == certification.tenant_id,
            SkillBuildRow.actor_id == certification.actor_id,
            SkillBuildRow.skill_id == certification.skill_id,
        ), for_update)
    )
    sealed = await session.scalar(
        _locked(select(SkillCertificationProvenanceRow).where(
            SkillCertificationProvenanceRow.certification_id
            == certification.certification_id,
            SkillCertificationProvenanceRow.tenant_id == certification.tenant_id,
            SkillCertificationProvenanceRow.actor_id == certification.actor_id,
            SkillCertificationProvenanceRow.build_id == certification.build_id,
        ), for_update)
    )
    workflow = (
        await session.scalar(
            _locked(select(WorkflowJobRow).where(
                WorkflowJobRow.job_id == sealed.workflow_job_id,
                WorkflowJobRow.tenant_id == sealed.tenant_id,
                WorkflowJobRow.operation == "CREATE_SKILL_BUILD",
                WorkflowJobRow.subject_type == "SKILL_BUILD",
                WorkflowJobRow.subject_id == sealed.build_id,
            ), for_update)
        )
        if sealed is not None
        else None
    )
    build_receipt = (
        await session.scalar(
            _locked(select(JobStepReceiptRow).where(
                JobStepReceiptRow.receipt_id == sealed.build_receipt_id,
                JobStepReceiptRow.tenant_id == sealed.tenant_id,
                JobStepReceiptRow.job_id == sealed.workflow_job_id,
                JobStepReceiptRow.step_name == "BUILD_CERTIFIED",
            ), for_update)
        )
        if sealed is not None
        else None
    )
    command = (
        await session.scalar(
            _locked(
                select(CommandRow).where(
                    CommandRow.command_id == build.command_id,
                    CommandRow.tenant_id == build.tenant_id,
                    CommandRow.actor_id == build.actor_id,
                ),
                for_update,
            )
        )
        if build is not None
        else None
    )
    if command is not None:
        # Import lazily because CommandStore's resource validator delegates
        # Build bytes back to skill_builds.  This closes the exact Command,
        # workflow job JSON, receipts, Artifact, Certification and Evidence.
        from .command_store import validated_command_record

        command_record = await validated_command_record(session, command)
    else:
        command_record = None
    capabilities = wire.get("capabilities") if isinstance(wire, Mapping) else None
    issued_at = _timestamp(wire.get("issued_at")) if isinstance(wire, Mapping) else None
    artifact_authority_sha256 = (
        canonical_json_sha256(
            {
                "tenant_id": artifact.tenant_id,
                "actor_id": artifact.actor_id,
                "content_hash": artifact.content_hash,
                "build_id": artifact.build_id,
                "skill_id": artifact.skill_id,
                "artifact_sha256": artifact.artifact_sha256,
                "source_sha256": artifact.source_sha256,
                "artifact_uri": artifact.artifact_uri,
                "metadata": artifact.metadata_json,
            }
        )
        if artifact is not None
        else None
    )
    if (
        artifact is None
        or build is None
        or policy is None
        or provenance is None
        or sealed is None
        or workflow is None
        or build_receipt is None
        or command is None
        or command_record is None
        or sealed.authority_sha256 != certification_provenance_sha256(sealed)
        or sealed.build_authority_sha256 != provenance.authority_sha256
        or sealed.build_request_sha256 != canonical_json_sha256(build.request_json)
        or sealed.workflow_request_sha256 != workflow.request_sha256
        or sealed.workflow_job_sha256 != certification_workflow_job_sha256(workflow)
        or sealed.command_authority_sha256
        != certification_command_authority_sha256(command)
        or sealed.build_receipt_authority_sha256
        != certification_receipt_authority_sha256(build_receipt)
        or build_receipt.input_sha256 != sealed.workflow_request_sha256
        or build_receipt.output_sha256 != sealed.build_receipt_sha256
        or build_receipt.receipt_id
        != workflow_step_receipt_id(
            sealed.tenant_id, sealed.workflow_job_id, "BUILD_CERTIFIED"
        )
        or build_receipt.fencing_token != workflow.fencing_token
        or build_receipt.completed_at > workflow.updated_at
        or build_receipt.output_sha256
        != workflow_receipt_sha256(build_receipt.receipt_json)
        or workflow.status != "SUCCEEDED"
        or build_receipt.receipt_json.get("build_id") != sealed.build_id
        or build_receipt.receipt_json.get("certification_id")
        != sealed.certification_id
        or build_receipt.receipt_json.get("artifact_sha256")
        != sealed.artifact_sha256
        or sealed.policy_sha256 != wire.get("policy_sha256")
        or sealed.artifact_sha256 != certification.artifact_sha256
        or sealed.artifact_authority_sha256 != artifact_authority_sha256
        or sealed.certification_sha256 != certification.certification_sha256
        or not isinstance(wire, Mapping)
        or canonical_json_sha256(wire) != certification.certification_sha256
        or (
            expected_certification_sha256 is not None
            and certification.certification_sha256 != expected_certification_sha256
        )
        or (
            expected_artifact_authority_sha256 is not None
            and artifact_authority_sha256 != expected_artifact_authority_sha256
        )
        or (
            expected_build_provenance_sha256 is not None
            and provenance.authority_sha256 != expected_build_provenance_sha256
        )
        or wire.get("schema_version") != "1.0.0"
        or set(policy.policy_json)
        != {
            "schema_version",
            "compiler_image",
            "compiler_profile",
            "compiler_version",
            "test_suite_version",
            "compile_flags",
            "public_tests",
            "hidden_tests",
            "limits",
            "parameter_schema",
        }
        or policy.policy_json.get("schema_version") != "1.0.0"
        or canonical_json_sha256(policy.policy_json) != policy.policy_sha256
        or policy.policy_json.get("compiler_profile") != policy.compiler_profile
        or policy.policy_json.get("compiler_version") != policy.compiler_version
        or policy.policy_json.get("test_suite_version") != policy.test_suite_version
        or not isinstance(policy.policy_json.get("compiler_image"), str)
        or not policy.policy_json["compiler_image"].endswith(
            f"@{policy.sandbox_image_digest}"
        )
        or wire.get("certification_id") != certification.certification_id
        or wire.get("build_id") != certification.build_id
        or wire.get("skill_id") != certification.skill_id
        or wire.get("skill_version_id") != certification.skill_version_id
        or wire.get("artifact_sha256") != certification.artifact_sha256
        or wire.get("source_sha256") != artifact.source_sha256
        or wire.get("actor_id") != certification.actor_id
        or wire.get("content_hash") != certification.content_hash
        or not isinstance(capabilities, list)
        or capabilities != build.request_json.get("requested_capabilities")
        or len(set(capabilities)) != len(capabilities)
        or any(
            not isinstance(capability, str)
            or capability not in policy.allowed_capabilities
            for capability in capabilities
        )
        or issued_at != certification.certified_at
        or not await validate_build_provenance(session, provenance)
    ):
        return None
    try:
        validated_certified_parameter_schema(
            policy.policy_json,
            artifact.metadata_json,
            wire,
            policy_sha256=policy.policy_sha256,
            build_id=certification.build_id,
            skill_id=certification.skill_id,
            skill_version_id=certification.skill_version_id,
            source_sha256=artifact.source_sha256,
            artifact_sha256=certification.artifact_sha256,
            certification_id=certification.certification_id,
            build_policy_id=policy.build_policy_id,
            actor_id=certification.actor_id,
            content_hash=certification.content_hash,
            capabilities=capabilities,
        )
    except CertifiedSkillSchemaError:
        return None
    return artifact, provenance


def artifact_authority_sha256(artifact: SkillArtifactRow) -> str:
    return canonical_json_sha256(
        {
            "tenant_id": artifact.tenant_id,
            "actor_id": artifact.actor_id,
            "content_hash": artifact.content_hash,
            "build_id": artifact.build_id,
            "skill_id": artifact.skill_id,
            "artifact_sha256": artifact.artifact_sha256,
            "source_sha256": artifact.source_sha256,
            "artifact_uri": artifact.artifact_uri,
            "metadata": artifact.metadata_json,
        }
    )


def certification_provenance_authority(
    row: SkillCertificationProvenanceRow,
) -> dict[str, object]:
    return {
        "authority_type": "SKILL_CERTIFICATION_PROVENANCE",
        "authority_version": "1.0.0",
        "certification_id": row.certification_id,
        "tenant_id": row.tenant_id,
        "actor_id": row.actor_id,
        "build_id": row.build_id,
        "build_authority_sha256": row.build_authority_sha256,
        "build_request_sha256": row.build_request_sha256,
        "workflow_job_id": row.workflow_job_id,
        "workflow_request_sha256": row.workflow_request_sha256,
        "workflow_job_sha256": row.workflow_job_sha256,
        "command_authority_sha256": row.command_authority_sha256,
        "build_receipt_id": row.build_receipt_id,
        "build_receipt_sha256": row.build_receipt_sha256,
        "build_receipt_authority_sha256": row.build_receipt_authority_sha256,
        "policy_sha256": row.policy_sha256,
        "artifact_sha256": row.artifact_sha256,
        "artifact_authority_sha256": row.artifact_authority_sha256,
        "certification_sha256": row.certification_sha256,
    }


def certification_provenance_sha256(row: SkillCertificationProvenanceRow) -> str:
    return canonical_json_sha256(certification_provenance_authority(row))


def certification_workflow_job_sha256(row: WorkflowJobRow) -> str:
    """Seal the exact terminal Build workflow row used by Certification."""

    return canonical_json_sha256(
        {
            "authority_type": "SKILL_CERTIFICATION_WORKFLOW_JOB",
            "authority_version": "1.0.0",
            "job_id": row.job_id,
            "tenant_id": row.tenant_id,
            "command_id": row.command_id,
            "operation": row.operation,
            "subject_type": row.subject_type,
            "subject_id": row.subject_id,
            "phase": row.phase,
            "status": row.status,
            "attempt": row.attempt,
            "fencing_token": row.fencing_token,
            "lease_owner": row.lease_owner,
            "lease_expires_at": _authority_timestamp(row.lease_expires_at),
            "next_attempt_at": _authority_timestamp(row.next_attempt_at),
            "request_sha256": row.request_sha256,
            "job": row.job_json,
            "last_error": row.last_error_json,
            "created_at": _authority_timestamp(row.created_at),
            "updated_at": _authority_timestamp(row.updated_at),
        }
    )


def certification_command_authority_sha256(row: CommandRow) -> str:
    """Seal the exact terminal Build Command row, including its public bytes."""

    return canonical_json_sha256(
        {
            "authority_type": "SKILL_CERTIFICATION_COMMAND",
            "authority_version": "1.0.0",
            "command_id": row.command_id,
            "tenant_id": row.tenant_id,
            "actor_id": row.actor_id,
            "command_type": row.command_type,
            "status": row.status,
            "revision": row.revision,
            "terminal": row.terminal,
            "accepted_at": _authority_timestamp(row.accepted_at),
            "updated_at": _authority_timestamp(row.updated_at),
            "record": row.record_json,
        }
    )


def certification_receipt_authority_sha256(row: JobStepReceiptRow) -> str:
    """Seal the full deterministic BUILD_CERTIFIED receipt row."""

    return canonical_json_sha256(
        {
            "authority_type": "SKILL_CERTIFICATION_RECEIPT",
            "authority_version": "1.0.0",
            "receipt_id": row.receipt_id,
            "tenant_id": row.tenant_id,
            "job_id": row.job_id,
            "step_name": row.step_name,
            "fencing_token": row.fencing_token,
            "input_sha256": row.input_sha256,
            "output_sha256": row.output_sha256,
            "receipt": row.receipt_json,
            "completed_at": _authority_timestamp(row.completed_at),
        }
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _authority_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("authority timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _locked(statement: Any, for_update: bool) -> Any:
    return statement.with_for_update() if for_update else statement


__all__ = [
    "artifact_authority_sha256",
    "certification_command_authority_sha256",
    "certification_provenance_authority",
    "certification_provenance_sha256",
    "certification_receipt_authority_sha256",
    "certification_workflow_job_sha256",
    "validate_certification_authority",
]
