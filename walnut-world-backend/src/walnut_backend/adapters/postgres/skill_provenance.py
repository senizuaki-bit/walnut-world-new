"""Strict immutable Build/Run/Draft assistance provenance validators for INT2."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yaya_agent_build import canonical_source_bundle_sha256
from yaya_agent_contracts import canonical_json_sha256

from .models import (
    CommandRow,
    IdempotencyReceiptRow,
    Int2LegacyBuildMarkerRow,
    JobStepReceiptRow,
    ProductDraftRevisionAssistanceRow,
    ProductDraftRevisionRow,
    ProductDraftRow,
    ProductSkillPatchDecisionRow,
    ProductSkillPatchProposalRow,
    RegistryEntryRow,
    RunRow,
    SkillActivationProvenanceRow,
    SkillActivationRow,
    SkillArtifactRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
    SkillBuildTerminalAuthorityRow,
    SkillCertificationProvenanceRow,
    SkillCertificationRow,
    SkillRunProvenanceRow,
    WorkflowJobRow,
)
from .workflow_jobs import workflow_job_id, workflow_step_receipt_id


def build_provenance_authority(row: SkillBuildProvenanceRow) -> dict[str, Any]:
    return {
        "authority_type": "SKILL_BUILD_PROVENANCE",
        "authority_version": "1.0.0",
        "build_id": row.build_id,
        "provenance_kind": row.provenance_kind,
        "legacy_marker_id": row.legacy_marker_id,
        "tenant_id": row.tenant_id,
        "actor_id": row.actor_id,
        "build_request_sha256": row.build_request_sha256,
        "command_receipt_id": row.command_receipt_id,
        "command_receipt_authority_sha256": row.command_receipt_authority_sha256,
        "workflow_job_id": row.workflow_job_id,
        "workflow_request_sha256": row.workflow_request_sha256,
        "session_id": row.session_id,
        "draft_id": row.draft_id,
        "skill_id": row.skill_id,
        "draft_revision_row_id": row.draft_revision_row_id,
        "draft_revision": row.draft_revision,
        "draft_sha256": row.draft_sha256,
        "source_bundle_sha256": row.source_bundle_sha256,
        "origin_accepted_revision_row_id": row.origin_accepted_revision_row_id,
        "patch_id": row.patch_id,
        "patch_decision_id": row.patch_decision_id,
        "assistance_authority": row.assistance_authority,
    }


def build_provenance_sha256(row: SkillBuildProvenanceRow) -> str:
    return canonical_json_sha256(build_provenance_authority(row))


def build_command_receipt_authority(
    row: IdempotencyReceiptRow,
) -> dict[str, Any]:
    return {
        "authority_type": "SKILL_BUILD_COMMAND_RECEIPT",
        "authority_version": "1.0.0",
        "receipt_id": row.receipt_id,
        "tenant_id": row.tenant_id,
        "actor_id": row.actor_id,
        "operation": row.operation,
        "idempotency_key": row.idempotency_key,
        "request_sha256": row.request_sha256,
        "command_id": row.command_id,
        "accepted_at": _authority_timestamp(row.accepted_at),
    }


def build_command_receipt_authority_sha256(row: IdempotencyReceiptRow) -> str:
    return canonical_json_sha256(build_command_receipt_authority(row))


def build_terminal_command_authority(row: CommandRow) -> dict[str, Any]:
    return {
        "authority_type": "SKILL_BUILD_TERMINAL_COMMAND",
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


def build_terminal_command_authority_sha256(row: CommandRow) -> str:
    return canonical_json_sha256(build_terminal_command_authority(row))


def build_terminal_workflow_authority(row: WorkflowJobRow) -> dict[str, Any]:
    return {
        "authority_type": "SKILL_BUILD_TERMINAL_WORKFLOW",
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


def build_terminal_workflow_authority_sha256(row: WorkflowJobRow) -> str:
    return canonical_json_sha256(build_terminal_workflow_authority(row))


def build_terminal_receipt_authority(row: JobStepReceiptRow) -> dict[str, Any]:
    return {
        "authority_type": "SKILL_BUILD_TERMINAL_RECEIPT",
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


def build_terminal_receipt_authority_sha256(row: JobStepReceiptRow) -> str:
    return canonical_json_sha256(build_terminal_receipt_authority(row))


def build_terminal_authority(
    row: SkillBuildTerminalAuthorityRow,
) -> dict[str, Any]:
    return {
        "authority_type": "SKILL_BUILD_TERMINAL_AUTHORITY",
        "authority_version": "1.0.0",
        "build_id": row.build_id,
        "tenant_id": row.tenant_id,
        "actor_id": row.actor_id,
        "build_authority_sha256": row.build_authority_sha256,
        "terminal_status": row.terminal_status,
        "command_id": row.command_id,
        "command_authority_sha256": row.command_authority_sha256,
        "workflow_job_id": row.workflow_job_id,
        "workflow_job_sha256": row.workflow_job_sha256,
        "terminal_receipt_id": row.terminal_receipt_id,
        "terminal_receipt_authority_sha256": (
            row.terminal_receipt_authority_sha256
        ),
        "certification_id": row.certification_id,
        "certification_authority_sha256": row.certification_authority_sha256,
    }


def build_terminal_authority_sha256(row: SkillBuildTerminalAuthorityRow) -> str:
    return canonical_json_sha256(build_terminal_authority(row))


def run_provenance_authority(row: SkillRunProvenanceRow) -> dict[str, Any]:
    return {
        "authority_type": "SKILL_RUN_PROVENANCE",
        "authority_version": "1.0.0",
        "run_id": row.run_id,
        "build_id": row.build_id,
        "provenance_kind": row.provenance_kind,
        "build_authority_sha256": row.build_authority_sha256,
        "tenant_id": row.tenant_id,
        "actor_id": row.actor_id,
        "session_id": row.session_id,
        "activation_id": row.activation_id,
        "activation_sha256": row.activation_sha256,
        "activation_authority_sha256": row.activation_authority_sha256,
        "registry_revision": row.registry_revision,
        "certification_id": row.certification_id,
        "certification_sha256": row.certification_sha256,
        "certification_authority_sha256": row.certification_authority_sha256,
        "artifact_sha256": row.artifact_sha256,
        "artifact_authority_sha256": row.artifact_authority_sha256,
        "draft_revision_row_id": row.draft_revision_row_id,
        "draft_sha256": row.draft_sha256,
        "assistance_authority": row.assistance_authority,
    }


def run_provenance_sha256(row: SkillRunProvenanceRow) -> str:
    return canonical_json_sha256(run_provenance_authority(row))


def activation_provenance_authority(
    row: SkillActivationProvenanceRow,
) -> dict[str, Any]:
    return {
        "authority_type": "SKILL_ACTIVATION_PROVENANCE",
        "authority_version": "1.0.0",
        "activation_id": row.activation_id,
        "tenant_id": row.tenant_id,
        "actor_id": row.actor_id,
        "build_id": row.build_id,
        "build_authority_sha256": row.build_authority_sha256,
        "certification_id": row.certification_id,
        "certification_sha256": row.certification_sha256,
        "certification_authority_sha256": row.certification_authority_sha256,
        "artifact_sha256": row.artifact_sha256,
        "artifact_authority_sha256": row.artifact_authority_sha256,
        "registry_revision": row.registry_revision,
        "activation_sha256": row.activation_sha256,
        "launch_authority_id": row.launch_authority_id,
        "entry_sha256": row.entry_sha256,
        "workflow_job_id": row.workflow_job_id,
        "workflow_request_sha256": row.workflow_request_sha256,
        "workflow_job_sha256": row.workflow_job_sha256,
        "activation_receipt_id": row.activation_receipt_id,
        "activation_receipt_sha256": row.activation_receipt_sha256,
    }


def activation_provenance_sha256(row: SkillActivationProvenanceRow) -> str:
    return canonical_json_sha256(activation_provenance_authority(row))


def activation_workflow_job_authority(row: WorkflowJobRow) -> dict[str, Any]:
    """Freeze the exact terminal ACTIVATE_SKILL_VERSION workflow row."""

    return {
        "authority_type": "SKILL_ACTIVATION_WORKFLOW_JOB",
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


def activation_workflow_job_sha256(row: WorkflowJobRow) -> str:
    return canonical_json_sha256(activation_workflow_job_authority(row))


def activation_receipt_authority(row: JobStepReceiptRow) -> dict[str, Any]:
    """Freeze the full deterministic REGISTRY_ACTIVATED receipt, not just output."""

    return {
        "authority_type": "SKILL_ACTIVATION_RECEIPT",
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


def activation_receipt_authority_sha256(row: JobStepReceiptRow) -> str:
    return canonical_json_sha256(activation_receipt_authority(row))


def _authority_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("authority timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def validate_build_provenance(
    session: AsyncSession,
    row: SkillBuildProvenanceRow,
    *,
    require_immutable: bool = False,
) -> bool:
    """Close one Build to either explicit v0.4 legacy or immutable Draft authority."""

    build = await session.scalar(
        select(SkillBuildRow).where(
            SkillBuildRow.build_id == row.build_id,
            SkillBuildRow.tenant_id == row.tenant_id,
            SkillBuildRow.actor_id == row.actor_id,
            SkillBuildRow.skill_id == row.skill_id,
        )
    )
    command_receipt = await session.scalar(
        select(IdempotencyReceiptRow).where(
            IdempotencyReceiptRow.receipt_id == row.command_receipt_id,
            IdempotencyReceiptRow.tenant_id == row.tenant_id,
            IdempotencyReceiptRow.actor_id == row.actor_id,
            IdempotencyReceiptRow.operation == "CREATE_SKILL_BUILD",
        )
    )
    workflow = await session.scalar(
        select(WorkflowJobRow).where(
            WorkflowJobRow.tenant_id == row.tenant_id,
            WorkflowJobRow.job_id == row.workflow_job_id,
            WorkflowJobRow.operation == "CREATE_SKILL_BUILD",
            WorkflowJobRow.subject_type == "SKILL_BUILD",
            WorkflowJobRow.subject_id == row.build_id,
        )
    )
    source = build.request_json.get("source_bundle") if build is not None else None
    if (
        build is None
        or command_receipt is None
        or workflow is None
        or not isinstance(source, Mapping)
        or canonical_json_sha256(build.request_json) != row.build_request_sha256
        or canonical_source_bundle_sha256(source) != row.source_bundle_sha256
        or command_receipt.command_id != build.command_id
        or command_receipt.request_sha256 != row.workflow_request_sha256
        or command_receipt.accepted_at != build.created_at
        or build_command_receipt_authority_sha256(command_receipt)
        != row.command_receipt_authority_sha256
        or workflow.command_id != build.command_id
        or workflow.job_id != workflow_job_id(row.tenant_id, build.command_id)
        or workflow.request_sha256 != row.workflow_request_sha256
        or row.authority_sha256 != build_provenance_sha256(row)
    ):
        return False
    if row.provenance_kind == "LEGACY_V04":
        marker = await session.scalar(
            select(Int2LegacyBuildMarkerRow).where(
                Int2LegacyBuildMarkerRow.marker_id == row.legacy_marker_id,
                Int2LegacyBuildMarkerRow.build_id == row.build_id,
                Int2LegacyBuildMarkerRow.tenant_id == row.tenant_id,
                Int2LegacyBuildMarkerRow.actor_id == row.actor_id,
                Int2LegacyBuildMarkerRow.build_authority_sha256
                == row.authority_sha256,
            )
        )
        marker_authority = (
            {
                "authority_type": "INT2_LEGACY_BUILD_MARKER",
                "authority_version": "1.0.0",
                "marker_id": marker.marker_id,
                "build_id": marker.build_id,
                "tenant_id": marker.tenant_id,
                "actor_id": marker.actor_id,
                "build_authority_sha256": marker.build_authority_sha256,
            }
            if marker is not None
            else None
        )
        return (
            not require_immutable
            and marker is not None
            and marker_authority is not None
            and marker.marker_sha256 == canonical_json_sha256(marker_authority)
            and row.session_id is None
            and row.draft_id is None
            and row.draft_revision_row_id is None
            and row.draft_revision is None
            and row.draft_sha256 is None
            and row.origin_accepted_revision_row_id is None
            and row.patch_id is None
            and row.patch_decision_id is None
            and row.assistance_authority == "NONE"
        )
    if row.provenance_kind != "IMMUTABLE_DRAFT":
        return False
    if (
        row.legacy_marker_id is not None
        or row.session_id is None
        or row.draft_id is None
        or row.draft_revision_row_id is None
        or row.draft_revision is None
        or row.draft_sha256 is None
    ):
        return False
    draft = await session.scalar(
        select(ProductDraftRevisionRow).where(
            ProductDraftRevisionRow.draft_revision_row_id
            == row.draft_revision_row_id,
            ProductDraftRevisionRow.tenant_id == row.tenant_id,
            ProductDraftRevisionRow.actor_id == row.actor_id,
            ProductDraftRevisionRow.session_id == row.session_id,
            ProductDraftRevisionRow.draft_id == row.draft_id,
            ProductDraftRevisionRow.skill_id == row.skill_id,
            ProductDraftRevisionRow.revision == row.draft_revision,
            ProductDraftRevisionRow.draft_sha256 == row.draft_sha256,
            ProductDraftRevisionRow.source_bundle_sha256
            == row.source_bundle_sha256,
        )
    )
    if draft is None or draft.draft_json.get("source_bundle") != dict(source):
        return False
    assistance = await _validate_draft_lineage(session, draft)
    if assistance is False:
        return False
    if assistance is None:
        return (
            row.assistance_authority == "NONE"
            and row.origin_accepted_revision_row_id is None
            and row.patch_id is None
            and row.patch_decision_id is None
        )
    return (
        row.assistance_authority == "SKILL_PATCH"
        and row.origin_accepted_revision_row_id
        == assistance.origin_accepted_revision_row_id
        and row.patch_id == assistance.patch_id
        and row.patch_decision_id == assistance.patch_decision_id
    )


async def validate_build_terminal_authority(
    session: AsyncSession,
    build: SkillBuildRow,
    provenance: SkillBuildProvenanceRow,
) -> bool:
    """Require one sealed terminal execution exactly when the Build is terminal."""

    sealed = await session.scalar(
        select(SkillBuildTerminalAuthorityRow).where(
            SkillBuildTerminalAuthorityRow.build_id == build.build_id,
            SkillBuildTerminalAuthorityRow.tenant_id == build.tenant_id,
            SkillBuildTerminalAuthorityRow.actor_id == build.actor_id,
            SkillBuildTerminalAuthorityRow.build_authority_sha256
            == provenance.authority_sha256,
        )
    )
    if not build.terminal:
        # COMPILING is a state the Build worker writes itself, on its way from
        # ACCEPTED to a terminal status. Admitting only ACCEPTED meant any Build
        # observed mid-compile read as corrupt -- and a Build abandoned there,
        # when its workflow exhausted its retries, stayed unreadable for good.
        # That is what left a learner unable to build at all: the client replays
        # its request under the original Idempotency-Key, the replay validates
        # the existing Build, and this check failed every single time.
        #
        # What actually matters is unchanged: a Build that has not reached a
        # terminal status must not have sealed a terminal execution.
        return build.status in {"ACCEPTED", "COMPILING"} and sealed is None
    if sealed is None or build.status not in {"REJECTED", "CERTIFIED"}:
        return False
    command = await session.scalar(
        select(CommandRow).where(
            CommandRow.command_id == sealed.command_id,
            CommandRow.tenant_id == build.tenant_id,
            CommandRow.actor_id == build.actor_id,
        )
    )
    workflow = await session.scalar(
        select(WorkflowJobRow).where(
            WorkflowJobRow.tenant_id == build.tenant_id,
            WorkflowJobRow.job_id == sealed.workflow_job_id,
            WorkflowJobRow.command_id == sealed.command_id,
            WorkflowJobRow.operation == "CREATE_SKILL_BUILD",
            WorkflowJobRow.subject_type == "SKILL_BUILD",
            WorkflowJobRow.subject_id == build.build_id,
        )
    )
    receipt = await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.receipt_id == sealed.terminal_receipt_id,
            JobStepReceiptRow.tenant_id == build.tenant_id,
            JobStepReceiptRow.job_id == sealed.workflow_job_id,
        )
    )
    certification = (
        await session.scalar(
            select(SkillCertificationProvenanceRow).where(
                SkillCertificationProvenanceRow.certification_id
                == sealed.certification_id,
                SkillCertificationProvenanceRow.tenant_id == build.tenant_id,
                SkillCertificationProvenanceRow.actor_id == build.actor_id,
                SkillCertificationProvenanceRow.build_id == build.build_id,
                SkillCertificationProvenanceRow.authority_sha256
                == sealed.certification_authority_sha256,
            )
        )
        if sealed.certification_id is not None
        else None
    )
    expected_command_status = "REJECTED" if build.status == "REJECTED" else "APPLIED"
    expected_job_status = "FAILED" if build.status == "REJECTED" else "SUCCEEDED"
    expected_receipt_step = (
        "BUILD_REJECTED" if build.status == "REJECTED" else "BUILD_CERTIFIED"
    )
    return (
        command is not None
        and workflow is not None
        and receipt is not None
        and sealed.terminal_status == build.status
        and sealed.command_id == build.command_id
        and sealed.workflow_job_id == provenance.workflow_job_id
        and sealed.workflow_job_id
        == workflow_job_id(build.tenant_id, build.command_id)
        and sealed.terminal_receipt_id
        == workflow_step_receipt_id(
            build.tenant_id, sealed.workflow_job_id, expected_receipt_step
        )
        and sealed.authority_sha256 == build_terminal_authority_sha256(sealed)
        and command.status == expected_command_status
        and command.terminal is True
        and sealed.command_authority_sha256
        == build_terminal_command_authority_sha256(command)
        and workflow.status == expected_job_status
        and workflow.request_sha256 == provenance.workflow_request_sha256
        and sealed.workflow_job_sha256
        == build_terminal_workflow_authority_sha256(workflow)
        and receipt.step_name == expected_receipt_step
        and receipt.fencing_token == workflow.fencing_token
        and receipt.input_sha256 == workflow.request_sha256
        and receipt.completed_at <= workflow.updated_at
        and sealed.terminal_receipt_authority_sha256
        == build_terminal_receipt_authority_sha256(receipt)
        and (
            (
                build.status == "REJECTED"
                and sealed.certification_id is None
                and sealed.certification_authority_sha256 is None
                and certification is None
            )
            or (
                build.status == "CERTIFIED"
                and certification is not None
                and sealed.certification_id == certification.certification_id
                and sealed.certification_authority_sha256
                == certification.authority_sha256
            )
        )
    )


async def validate_run_provenance(
    session: AsyncSession,
    row: SkillRunProvenanceRow,
    *,
    require_immutable: bool = False,
) -> SkillBuildProvenanceRow | None:
    """Return the exact Build authority only when the whole Run chain closes."""

    build = await session.scalar(
        select(SkillBuildProvenanceRow).where(
            SkillBuildProvenanceRow.build_id == row.build_id,
            SkillBuildProvenanceRow.authority_sha256
            == row.build_authority_sha256,
            SkillBuildProvenanceRow.tenant_id == row.tenant_id,
            SkillBuildProvenanceRow.actor_id == row.actor_id,
        )
    )
    run = await session.scalar(
        select(RunRow).where(
            RunRow.run_id == row.run_id,
            RunRow.tenant_id == row.tenant_id,
            RunRow.actor_id == row.actor_id,
            RunRow.session_id == row.session_id,
        )
    )
    if (
        build is None
        or run is None
        or run.run_json.get("run_id") != row.run_id
        or run.run_json.get("session_id") != row.session_id
        or row.authority_sha256 != run_provenance_sha256(row)
        or not (
            row.provenance_kind == build.provenance_kind
            or (
                row.provenance_kind == "LEGACY_V04_ACTIVE"
                and build.provenance_kind == "LEGACY_V04"
            )
        )
        or row.session_id != (build.session_id or row.session_id)
        or row.draft_revision_row_id != build.draft_revision_row_id
        or row.draft_sha256 != build.draft_sha256
        or row.assistance_authority != build.assistance_authority
        or not await _validate_run_activation(session, row, build)
        or not await validate_build_provenance(
            session, build, require_immutable=require_immutable
        )
    ):
        return None
    return build


async def active_build_matches_current_patch_origin(
    session: AsyncSession,
    build: SkillBuildProvenanceRow,
    *,
    tenant_id: str,
    actor_id: str,
    session_id: str,
    skill_id: str,
) -> bool:
    """Block an old Activation only after ACCEPT creates a patched lineage."""

    head = await session.scalar(
        select(ProductDraftRow).where(
            ProductDraftRow.tenant_id == tenant_id,
            ProductDraftRow.actor_id == actor_id,
            ProductDraftRow.session_id == session_id,
            ProductDraftRow.skill_id == skill_id,
        )
    )
    if head is None:
        return False
    revision = await session.scalar(
        select(ProductDraftRevisionRow).where(
            ProductDraftRevisionRow.tenant_id == tenant_id,
            ProductDraftRevisionRow.actor_id == actor_id,
            ProductDraftRevisionRow.session_id == session_id,
            ProductDraftRevisionRow.draft_id == head.draft_id,
            ProductDraftRevisionRow.skill_id == skill_id,
            ProductDraftRevisionRow.revision == head.revision,
            ProductDraftRevisionRow.draft_sha256 == head.draft_sha256,
        )
    )
    if revision is None or revision.draft_json != head.draft_json:
        return False
    assistance = await _validate_draft_lineage(session, revision)
    if assistance is False:
        return False
    if assistance is None:
        return True
    return (
        build.provenance_kind == "IMMUTABLE_DRAFT"
        and build.assistance_authority == "SKILL_PATCH"
        and build.origin_accepted_revision_row_id
        == assistance.origin_accepted_revision_row_id
        and build.patch_id == assistance.patch_id
        and build.patch_decision_id == assistance.patch_decision_id
    )


async def _validate_run_activation(
    session: AsyncSession,
    row: SkillRunProvenanceRow,
    build: SkillBuildProvenanceRow,
) -> bool:
    """Bind new Runs to one historical Activation, never the latest head."""

    fields = (
        row.activation_id,
        row.activation_sha256,
        row.activation_authority_sha256,
        row.registry_revision,
    )
    if all(value is None for value in fields):
        # v0.4 Run bytes never froze which of multiple valid Activations ran.
        # Its sealed migration marker can prove only the exact
        # Certification/Artifact/Build tuple, never a guessed Activation.
        if row.provenance_kind != "LEGACY_V04":
            return False
        certification = await session.scalar(
            select(SkillCertificationRow).where(
                SkillCertificationRow.tenant_id == row.tenant_id,
                SkillCertificationRow.actor_id == row.actor_id,
                SkillCertificationRow.certification_id == row.certification_id,
                SkillCertificationRow.build_id == build.build_id,
                SkillCertificationRow.skill_id == build.skill_id,
                SkillCertificationRow.artifact_sha256 == row.artifact_sha256,
                SkillCertificationRow.certification_sha256
                == row.certification_sha256,
            )
        )
        if certification is None:
            return False
        from .certification_authority import validate_certification_authority

        closed = await validate_certification_authority(
            session,
            certification,
            expected_certification_sha256=row.certification_sha256,
            expected_artifact_authority_sha256=row.artifact_authority_sha256,
            expected_build_provenance_sha256=build.authority_sha256,
        )
        certification_provenance = await session.scalar(
            select(SkillCertificationProvenanceRow).where(
                SkillCertificationProvenanceRow.certification_id
                == certification.certification_id,
                SkillCertificationProvenanceRow.authority_sha256
                == row.certification_authority_sha256,
                SkillCertificationProvenanceRow.tenant_id == row.tenant_id,
                SkillCertificationProvenanceRow.actor_id == row.actor_id,
                SkillCertificationProvenanceRow.build_id == build.build_id,
            )
        )
        run = await session.scalar(
            select(RunRow).where(
                RunRow.run_id == row.run_id,
                RunRow.tenant_id == row.tenant_id,
                RunRow.actor_id == row.actor_id,
                RunRow.session_id == row.session_id,
            )
        )
        skill = run.run_json.get("skill") if run is not None else None
        return (
            closed is not None
            and certification_provenance is not None
            and isinstance(skill, Mapping)
            and dict(skill)
            == {
                "skill_id": certification.skill_id,
                "skill_version_id": certification.skill_version_id,
                "artifact_sha256": certification.artifact_sha256,
                "certification_id": certification.certification_id,
            }
        )
    if any(value is None for value in fields):
        return False
    activation_provenance = await session.scalar(
        select(SkillActivationProvenanceRow).where(
            SkillActivationProvenanceRow.activation_id == row.activation_id,
            SkillActivationProvenanceRow.authority_sha256
            == row.activation_authority_sha256,
            SkillActivationProvenanceRow.tenant_id == row.tenant_id,
            SkillActivationProvenanceRow.actor_id == row.actor_id,
            SkillActivationProvenanceRow.build_id == build.build_id,
            SkillActivationProvenanceRow.build_authority_sha256
            == build.authority_sha256,
            SkillActivationProvenanceRow.certification_id == row.certification_id,
            SkillActivationProvenanceRow.certification_sha256
            == row.certification_sha256,
            SkillActivationProvenanceRow.certification_authority_sha256
            == row.certification_authority_sha256,
            SkillActivationProvenanceRow.artifact_sha256 == row.artifact_sha256,
            SkillActivationProvenanceRow.artifact_authority_sha256
            == row.artifact_authority_sha256,
            SkillActivationProvenanceRow.registry_revision == row.registry_revision,
            SkillActivationProvenanceRow.activation_sha256 == row.activation_sha256,
        )
    )
    if (
        activation_provenance is None
        or activation_provenance.authority_sha256
        != activation_provenance_sha256(activation_provenance)
    ):
        return False
    activation = await session.scalar(
        select(SkillActivationRow).where(
            SkillActivationRow.activation_id == row.activation_id,
            SkillActivationRow.tenant_id == row.tenant_id,
            SkillActivationRow.actor_id == row.actor_id,
            SkillActivationRow.skill_id == build.skill_id,
            SkillActivationRow.registry_revision == row.registry_revision,
            SkillActivationRow.certification_id == row.certification_id,
            SkillActivationRow.artifact_sha256 == row.artifact_sha256,
            SkillActivationRow.activation_sha256 == row.activation_sha256,
        )
    )
    # A Run must close over the same immutable Activation authority exposed by
    # historical Activation reads.  Repeating only selected mirrored columns
    # here would let a damaged RegistryEntry/job/receipt remain visible through
    # the Run API even though the Activation itself correctly fails closed.
    if activation is None:
        return False
    from .activation_authority import validate_historical_activation_authority

    if not await validate_historical_activation_authority(session, activation):
        return False
    entry = (
        await session.scalar(
            select(RegistryEntryRow).where(
                RegistryEntryRow.tenant_id == activation.tenant_id,
                RegistryEntryRow.actor_id == activation.actor_id,
                RegistryEntryRow.content_hash == activation.content_hash,
                RegistryEntryRow.world_id == activation.world_id,
                RegistryEntryRow.agent_profile_id == activation.agent_profile_id,
                RegistryEntryRow.revision == activation.registry_revision,
                RegistryEntryRow.skill_id == activation.skill_id,
                RegistryEntryRow.skill_version_id == activation.skill_version_id,
                RegistryEntryRow.certification_id == activation.certification_id,
                RegistryEntryRow.artifact_sha256 == activation.artifact_sha256,
            )
        )
        if activation is not None
        else None
    )
    certification = (
        await session.scalar(
            select(SkillCertificationRow).where(
                SkillCertificationRow.tenant_id == activation.tenant_id,
                SkillCertificationRow.actor_id == activation.actor_id,
                SkillCertificationRow.certification_id == activation.certification_id,
                SkillCertificationRow.build_id == build.build_id,
                SkillCertificationRow.skill_id == activation.skill_id,
                SkillCertificationRow.skill_version_id == activation.skill_version_id,
                SkillCertificationRow.artifact_sha256 == activation.artifact_sha256,
            )
        )
        if activation is not None
        else None
    )
    artifact = (
        await session.scalar(
            select(SkillArtifactRow).where(
                SkillArtifactRow.tenant_id == certification.tenant_id,
                SkillArtifactRow.actor_id == certification.actor_id,
                SkillArtifactRow.build_id == certification.build_id,
                SkillArtifactRow.skill_id == certification.skill_id,
                SkillArtifactRow.artifact_sha256 == certification.artifact_sha256,
                SkillArtifactRow.source_sha256 == build.source_bundle_sha256,
            )
        )
        if certification is not None
        else None
    )
    if activation is None or entry is None or certification is None or artifact is None:
        return False
    activation_job = await session.scalar(
        select(WorkflowJobRow).where(
            WorkflowJobRow.tenant_id == activation.tenant_id,
            WorkflowJobRow.operation == "ACTIVATE_SKILL_VERSION",
            WorkflowJobRow.subject_type == "SKILL_ACTIVATION",
            WorkflowJobRow.subject_id == activation.activation_id,
            WorkflowJobRow.status == "SUCCEEDED",
        )
    )
    if activation_job is None:
        return False
    # Local import avoids a module cycle: the certification validator reuses
    # validate_build_provenance from this module.
    from .certification_authority import validate_certification_authority

    if await validate_certification_authority(
        session,
        certification,
        expected_certification_sha256=activation_job.job_json.get(
            "certification_sha256"
        ),
        expected_artifact_authority_sha256=activation_job.job_json.get(
            "artifact_authority_sha256"
        ),
        expected_build_provenance_sha256=activation_job.job_json.get(
            "build_provenance_sha256"
        ),
    ) is None:
        return False
    certification_provenance = await session.scalar(
        select(SkillCertificationProvenanceRow).where(
            SkillCertificationProvenanceRow.certification_id
            == certification.certification_id,
            SkillCertificationProvenanceRow.tenant_id == certification.tenant_id,
            SkillCertificationProvenanceRow.actor_id == certification.actor_id,
            SkillCertificationProvenanceRow.build_id == certification.build_id,
        )
    )
    if (
        certification_provenance is None
        or activation_provenance.certification_authority_sha256
        != certification_provenance.authority_sha256
    ):
        return False
    wire = activation.activation_json
    row_run_wire = (
        await session.scalar(
            select(RunRow.run_json).where(
                RunRow.run_id == row.run_id,
                RunRow.tenant_id == row.tenant_id,
                RunRow.actor_id == row.actor_id,
                RunRow.session_id == row.session_id,
            )
        )
    )
    run_skill = row_run_wire.get("skill") if isinstance(row_run_wire, Mapping) else None
    return (
        isinstance(run_skill, Mapping)
        and dict(run_skill)
        == {
            "skill_id": activation.skill_id,
            "skill_version_id": activation.skill_version_id,
            "artifact_sha256": activation.artifact_sha256,
            "certification_id": activation.certification_id,
        }
        and activation.activation_sha256 == canonical_json_sha256(wire)
        and wire.get("activation_id") == activation.activation_id
        and wire.get("skill_id") == activation.skill_id
        and wire.get("skill_version_id") == activation.skill_version_id
        and wire.get("certification_id") == activation.certification_id
        and wire.get("artifact_sha256") == activation.artifact_sha256
        and wire.get("previous_registry_revision")
        == activation.previous_registry_revision
        and wire.get("registry_revision") == activation.registry_revision
        and entry.entry_json.get("activation_id") == activation.activation_id
    )


async def _validate_draft_lineage(
    session: AsyncSession,
    head: ProductDraftRevisionRow,
) -> ProductDraftRevisionAssistanceRow | None | Literal[False]:
    """Walk every parent; False means corruption, None means proven independent."""

    current = head
    expected_assistance = await session.scalar(
        select(ProductDraftRevisionAssistanceRow).where(
            ProductDraftRevisionAssistanceRow.draft_revision_row_id
            == head.draft_revision_row_id
        )
    )
    accepted_origin: ProductDraftRevisionRow | None = None
    seen: set[int] = set()
    while True:
        if current.draft_revision_row_id in seen:
            return False
        seen.add(current.draft_revision_row_id)
        assistance = await session.scalar(
            select(ProductDraftRevisionAssistanceRow).where(
                ProductDraftRevisionAssistanceRow.draft_revision_row_id
                == current.draft_revision_row_id
            )
        )
        if expected_assistance is None:
            if (
                assistance is not None
                or current.source_kind == "SKILL_PATCH"
                or current.patch_id is not None
            ):
                return False
        elif (
            assistance is None
            or assistance.origin_accepted_revision_row_id
            != expected_assistance.origin_accepted_revision_row_id
            or assistance.patch_id != expected_assistance.patch_id
            or assistance.patch_decision_id
            != expected_assistance.patch_decision_id
        ):
            return False
        if expected_assistance is not None:
            is_origin = (
                current.draft_revision_row_id
                == expected_assistance.origin_accepted_revision_row_id
            )
            if assistance is None or assistance.inherited is is_origin:
                return False
            if is_origin:
                if (
                    current.source_kind != "SKILL_PATCH"
                    or current.patch_id != expected_assistance.patch_id
                ):
                    return False
                accepted_origin = current
                break
            if current.source_kind != "STUDENT" or current.patch_id is not None:
                return False
        if current.parent_revision_row_id is None:
            break
        parent = await session.scalar(
            select(ProductDraftRevisionRow).where(
                ProductDraftRevisionRow.draft_revision_row_id
                == current.parent_revision_row_id
            )
        )
        if (
            parent is None
            or parent.tenant_id != head.tenant_id
            or parent.actor_id != head.actor_id
            or parent.session_id != head.session_id
            or parent.draft_id != head.draft_id
            or parent.skill_id != head.skill_id
            or parent.revision != current.revision - 1
        ):
            return False
        current = parent
    if expected_assistance is None:
        return None
    origin = accepted_origin
    decision = await session.scalar(
        select(ProductSkillPatchDecisionRow).where(
            ProductSkillPatchDecisionRow.decision_id
            == expected_assistance.patch_decision_id,
            ProductSkillPatchDecisionRow.patch_id == expected_assistance.patch_id,
            ProductSkillPatchDecisionRow.accepted_draft_revision_row_id
            == expected_assistance.origin_accepted_revision_row_id,
            ProductSkillPatchDecisionRow.decision == "ACCEPT",
            ProductSkillPatchDecisionRow.tenant_id == head.tenant_id,
            ProductSkillPatchDecisionRow.actor_id == head.actor_id,
            ProductSkillPatchDecisionRow.session_id == head.session_id,
            ProductSkillPatchDecisionRow.draft_id == head.draft_id,
        )
    )
    proposal = (
        await session.scalar(
            select(ProductSkillPatchProposalRow).where(
                ProductSkillPatchProposalRow.patch_id
                == expected_assistance.patch_id,
                ProductSkillPatchProposalRow.tenant_id == head.tenant_id,
                ProductSkillPatchProposalRow.actor_id == head.actor_id,
                ProductSkillPatchProposalRow.session_id == head.session_id,
                ProductSkillPatchProposalRow.draft_id == head.draft_id,
                ProductSkillPatchProposalRow.skill_id == head.skill_id,
            )
        )
        if decision is not None
        else None
    )
    if (
        origin is None
        or decision is None
        or proposal is None
        or decision.base_draft_revision_row_id
        != proposal.base_draft_revision_row_id
        or proposal.base_draft_revision != origin.revision - 1
        or origin.parent_revision_row_id != proposal.base_draft_revision_row_id
        or origin.draft_sha256 != proposal.result_draft_sha256
    ):
        return False
    if head.draft_revision_row_id != origin.draft_revision_row_id:
        head_assistance = await session.scalar(
            select(ProductDraftRevisionAssistanceRow).where(
                ProductDraftRevisionAssistanceRow.draft_revision_row_id
                == head.draft_revision_row_id
            )
        )
        if head_assistance is None:
            return False
    base = await session.scalar(
        select(ProductDraftRevisionRow).where(
            ProductDraftRevisionRow.draft_revision_row_id
            == proposal.base_draft_revision_row_id,
            ProductDraftRevisionRow.tenant_id == head.tenant_id,
            ProductDraftRevisionRow.actor_id == head.actor_id,
            ProductDraftRevisionRow.session_id == head.session_id,
            ProductDraftRevisionRow.draft_id == head.draft_id,
            ProductDraftRevisionRow.skill_id == head.skill_id,
            ProductDraftRevisionRow.revision == proposal.base_draft_revision,
            ProductDraftRevisionRow.draft_sha256 == proposal.base_draft_sha256,
            ProductDraftRevisionRow.source_bundle_sha256
            == proposal.source_bundle_sha256,
        )
    )
    if base is None or await _validate_draft_lineage(session, base) is False:
        return False
    return expected_assistance


__all__ = [
    "active_build_matches_current_patch_origin",
    "activation_provenance_authority",
    "activation_provenance_sha256",
    "activation_receipt_authority",
    "activation_receipt_authority_sha256",
    "activation_workflow_job_authority",
    "activation_workflow_job_sha256",
    "build_command_receipt_authority",
    "build_command_receipt_authority_sha256",
    "build_provenance_authority",
    "build_provenance_sha256",
    "build_terminal_authority",
    "build_terminal_authority_sha256",
    "build_terminal_command_authority",
    "build_terminal_command_authority_sha256",
    "build_terminal_receipt_authority",
    "build_terminal_receipt_authority_sha256",
    "build_terminal_workflow_authority",
    "build_terminal_workflow_authority_sha256",
    "run_provenance_authority",
    "run_provenance_sha256",
    "validate_build_provenance",
    "validate_build_terminal_authority",
    "validate_run_provenance",
]
