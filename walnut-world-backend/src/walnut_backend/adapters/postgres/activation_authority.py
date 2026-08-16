"""Exact full-scope Activation and RegistryEntry authority validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yaya_agent_contracts import SkillRef, canonical_json_sha256

from .certification_authority import validate_certification_authority
from .models import (
    JobStepReceiptRow,
    RegistryEntryRow,
    RegistryHeadRow,
    SkillActivationProvenanceRow,
    SkillActivationRow,
    SkillCertificationProvenanceRow,
    SkillCertificationRow,
    WorkflowJobRow,
    request_context_data,
    request_context_from_data,
)
from .skill_provenance import (
    activation_provenance_sha256,
    activation_receipt_authority_sha256,
    activation_workflow_job_sha256,
)
from .workflow_jobs import (
    WorkflowInvariantError,
    workflow_job_id,
    workflow_receipt_sha256,
    workflow_step_receipt_id,
)


class ActivationAuthorityNotFound(WorkflowInvariantError):
    """The requested Skill is not the one selected by the current Registry head."""


@dataclass(frozen=True, slots=True)
class ValidatedActivationAuthority:
    head: RegistryHeadRow
    entry: RegistryEntryRow
    activation: SkillActivationRow


async def validate_historical_activation_authority(
    session: AsyncSession,
    activation: SkillActivationRow,
) -> bool:
    """Close one historical Activation without consulting the mutable head."""

    entry = await session.scalar(
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
    certification = await session.scalar(
        select(SkillCertificationRow).where(
            SkillCertificationRow.tenant_id == activation.tenant_id,
            SkillCertificationRow.actor_id == activation.actor_id,
            SkillCertificationRow.certification_id == activation.certification_id,
            SkillCertificationRow.skill_id == activation.skill_id,
            SkillCertificationRow.skill_version_id == activation.skill_version_id,
            SkillCertificationRow.artifact_sha256 == activation.artifact_sha256,
        )
    )
    provenance = await session.scalar(
        select(SkillActivationProvenanceRow).where(
            SkillActivationProvenanceRow.activation_id == activation.activation_id,
            SkillActivationProvenanceRow.tenant_id == activation.tenant_id,
            SkillActivationProvenanceRow.actor_id == activation.actor_id,
            SkillActivationProvenanceRow.certification_id
            == activation.certification_id,
            SkillActivationProvenanceRow.registry_revision
            == activation.registry_revision,
            SkillActivationProvenanceRow.activation_sha256
            == activation.activation_sha256,
        )
    )
    job = (
        await session.scalar(
            select(WorkflowJobRow).where(
                WorkflowJobRow.tenant_id == activation.tenant_id,
                WorkflowJobRow.job_id == provenance.workflow_job_id,
                WorkflowJobRow.request_sha256
                == provenance.workflow_request_sha256,
                WorkflowJobRow.operation == "ACTIVATE_SKILL_VERSION",
                WorkflowJobRow.subject_type == "SKILL_ACTIVATION",
                WorkflowJobRow.subject_id == activation.activation_id,
            )
        )
        if provenance is not None
        else None
    )
    receipt = (
        await session.scalar(
            select(JobStepReceiptRow).where(
                JobStepReceiptRow.tenant_id == activation.tenant_id,
                JobStepReceiptRow.receipt_id == provenance.activation_receipt_id,
                JobStepReceiptRow.job_id == provenance.workflow_job_id,
                JobStepReceiptRow.step_name == "REGISTRY_ACTIVATED",
            )
        )
        if provenance is not None
        else None
    )
    if (
        entry is None
        or certification is None
        or provenance is None
        or job is None
        or receipt is None
    ):
        return False
    try:
        validate_activation_registry_authority(
            head=_historical_head(entry),
            entry=entry,
            activation=activation,
            authority_id=provenance.launch_authority_id,
        )
    except (TypeError, WorkflowInvariantError):
        return False
    frozen_certification = job.job_json.get("certification_sha256")
    frozen_artifact = job.job_json.get("artifact_authority_sha256")
    frozen_build = job.job_json.get("build_provenance_sha256")
    if not all(
        isinstance(value, str)
        for value in (frozen_certification, frozen_artifact, frozen_build)
    ):
        return False
    closed = await validate_certification_authority(
        session,
        certification,
        expected_certification_sha256=frozen_certification,
        expected_artifact_authority_sha256=frozen_artifact,
        expected_build_provenance_sha256=frozen_build,
    )
    if closed is None:
        return False
    _, build = closed
    expected = {
        "activation_id": activation.activation_id,
        "previous_registry_revision": activation.previous_registry_revision,
        "registry_revision": activation.registry_revision,
        "entry_sha256": entry.entry_sha256,
        "activation_sha256": activation.activation_sha256,
        "certification_sha256": certification.certification_sha256,
        "artifact_authority_sha256": job.job_json.get(
            "artifact_authority_sha256"
        ),
        "build_provenance_sha256": build.authority_sha256,
    }
    expected_job_keys = {
        "schema_version",
        "request_context",
        "activation_id",
        "authority_id",
        "expected_registry_revision",
        "activation_scope",
        "skill",
        "build_provenance_sha256",
        "certification_sha256",
        "artifact_authority_sha256",
        "reason",
    }
    activation_wire = activation.activation_json
    return (
        provenance.authority_sha256 == activation_provenance_sha256(provenance)
        and provenance.build_id == certification.build_id
        and provenance.build_authority_sha256 == build.authority_sha256
        and provenance.certification_sha256 == certification.certification_sha256
        and provenance.artifact_sha256 == activation.artifact_sha256
        and provenance.artifact_authority_sha256
        == job.job_json.get("artifact_authority_sha256")
        and provenance.entry_sha256 == entry.entry_sha256
        and provenance.workflow_job_sha256 == activation_workflow_job_sha256(job)
        and provenance.activation_receipt_sha256
        == activation_receipt_authority_sha256(receipt)
        and job.job_id == workflow_job_id(job.tenant_id, job.command_id)
        and receipt.receipt_id
        == workflow_step_receipt_id(job.tenant_id, job.job_id, "REGISTRY_ACTIVATED")
        and job.status == "SUCCEEDED"
        and job.phase == "COMPLETE"
        and job.attempt >= 1
        and job.fencing_token >= 1
        and job.lease_owner is None
        and job.lease_expires_at is None
        and job.next_attempt_at is None
        and job.last_error_json is None
        and set(job.job_json) == expected_job_keys
        and job.job_json.get("schema_version") == "1.0.0"
        and job.job_json.get("request_context")
        == activation_wire.get("request_context")
        and job.job_json.get("activation_id") == activation.activation_id
        and job.job_json.get("authority_id") == provenance.launch_authority_id
        and job.job_json.get("expected_registry_revision")
        == activation.previous_registry_revision
        and job.job_json.get("activation_scope")
        == activation_wire.get("activation_scope")
        and job.job_json.get("skill")
        == {
            "skill_id": activation.skill_id,
            "skill_version_id": activation.skill_version_id,
            "certification_id": activation.certification_id,
            "artifact_sha256": activation.artifact_sha256,
        }
        and job.job_json.get("build_provenance_sha256")
        == build.authority_sha256
        and job.job_json.get("certification_sha256")
        == certification.certification_sha256
        and receipt.input_sha256 == job.request_sha256
        and receipt.fencing_token == job.fencing_token
        and receipt.receipt_json == expected
        and receipt.output_sha256 == workflow_receipt_sha256(expected)
        and receipt.completed_at <= job.updated_at
    )


def _historical_head(entry: RegistryEntryRow) -> RegistryHeadRow:
    """Build the exact revision head needed by the shared row validator."""

    return RegistryHeadRow(
        tenant_id=entry.tenant_id,
        actor_id=entry.actor_id,
        content_hash=entry.content_hash,
        world_id=entry.world_id,
        agent_profile_id=entry.agent_profile_id,
        authority_id=str(entry.entry_json.get("authority_id")),
        revision=entry.revision,
        updated_at=entry.activated_at,
    )


async def load_current_activation_authority(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_id: str,
    content_hash: str,
    world_id: str,
    agent_profile_id: str,
    authority_id: str,
    skill_ref: SkillRef,
    for_update: bool = False,
) -> ValidatedActivationAuthority:
    """Load the current tuple, then validate every duplicated column and JSON byte."""

    head_statement = select(RegistryHeadRow).where(
        RegistryHeadRow.tenant_id == tenant_id,
        RegistryHeadRow.actor_id == actor_id,
        RegistryHeadRow.content_hash == content_hash,
        RegistryHeadRow.world_id == world_id,
        RegistryHeadRow.agent_profile_id == agent_profile_id,
        RegistryHeadRow.authority_id == authority_id,
    )
    if for_update:
        head_statement = head_statement.with_for_update()
    head = await session.scalar(head_statement)
    if head is None or head.revision < 1:
        raise ActivationAuthorityNotFound("Registry has no active Skill")

    entry_statement = select(RegistryEntryRow).where(
        RegistryEntryRow.tenant_id == tenant_id,
        RegistryEntryRow.actor_id == actor_id,
        RegistryEntryRow.content_hash == content_hash,
        RegistryEntryRow.world_id == world_id,
        RegistryEntryRow.agent_profile_id == agent_profile_id,
        RegistryEntryRow.revision == head.revision,
    )
    activation_statement = select(SkillActivationRow).where(
        SkillActivationRow.tenant_id == tenant_id,
        SkillActivationRow.actor_id == actor_id,
        SkillActivationRow.content_hash == content_hash,
        SkillActivationRow.world_id == world_id,
        SkillActivationRow.agent_profile_id == agent_profile_id,
        SkillActivationRow.registry_revision == head.revision,
    )
    if for_update:
        entry_statement = entry_statement.with_for_update()
        activation_statement = activation_statement.with_for_update()
    entry = await session.scalar(entry_statement)
    activations = list((await session.scalars(activation_statement)).all())
    if entry is None or not activations:
        raise WorkflowInvariantError(
            "current Registry head has no complete Activation/RegistryEntry authority"
        )
    if len(activations) != 1:
        raise WorkflowInvariantError("current Registry revision has ambiguous Activations")
    activation = activations[0]
    validate_activation_registry_authority(
        head=head,
        entry=entry,
        activation=activation,
        authority_id=authority_id,
    )
    if not await validate_historical_activation_authority(session, activation):
        raise WorkflowInvariantError("active Skill Activation authority is corrupt")
    certification = await session.scalar(
        select(SkillCertificationRow).where(
            SkillCertificationRow.tenant_id == activation.tenant_id,
            SkillCertificationRow.actor_id == activation.actor_id,
            SkillCertificationRow.certification_id == activation.certification_id,
            SkillCertificationRow.skill_id == activation.skill_id,
            SkillCertificationRow.skill_version_id == activation.skill_version_id,
            SkillCertificationRow.artifact_sha256 == activation.artifact_sha256,
        )
    )
    activation_job = await session.scalar(
        select(WorkflowJobRow).where(
            WorkflowJobRow.tenant_id == activation.tenant_id,
            WorkflowJobRow.operation == "ACTIVATE_SKILL_VERSION",
            WorkflowJobRow.subject_type == "SKILL_ACTIVATION",
            WorkflowJobRow.subject_id == activation.activation_id,
        )
    )
    job_json = activation_job.job_json if activation_job is not None else {}
    closed_certification = (
        await validate_certification_authority(
            session,
            certification,
            expected_certification_sha256=job_json.get("certification_sha256"),
            expected_artifact_authority_sha256=job_json.get(
                "artifact_authority_sha256"
            ),
            expected_build_provenance_sha256=job_json.get(
                "build_provenance_sha256"
            ),
        )
        if certification is not None
        and isinstance(job_json.get("certification_sha256"), str)
        and isinstance(job_json.get("artifact_authority_sha256"), str)
        and isinstance(job_json.get("build_provenance_sha256"), str)
        else None
    )
    if certification is None or closed_certification is None:
        raise WorkflowInvariantError("active Skill Build provenance is missing or corrupt")
    _artifact, build_provenance = closed_certification
    certification_provenance = await session.scalar(
        select(SkillCertificationProvenanceRow).where(
            SkillCertificationProvenanceRow.certification_id
            == certification.certification_id,
            SkillCertificationProvenanceRow.tenant_id == certification.tenant_id,
            SkillCertificationProvenanceRow.actor_id == certification.actor_id,
            SkillCertificationProvenanceRow.build_id == certification.build_id,
        )
    )
    activation_provenance = await session.scalar(
        select(SkillActivationProvenanceRow).where(
            SkillActivationProvenanceRow.activation_id == activation.activation_id,
            SkillActivationProvenanceRow.tenant_id == activation.tenant_id,
            SkillActivationProvenanceRow.actor_id == activation.actor_id,
            SkillActivationProvenanceRow.build_id == certification.build_id,
            SkillActivationProvenanceRow.build_authority_sha256
            == build_provenance.authority_sha256,
            SkillActivationProvenanceRow.certification_id
            == certification.certification_id,
            SkillActivationProvenanceRow.certification_sha256
            == certification.certification_sha256,
            SkillActivationProvenanceRow.artifact_sha256
            == certification.artifact_sha256,
            SkillActivationProvenanceRow.artifact_authority_sha256
            == job_json.get("artifact_authority_sha256"),
            SkillActivationProvenanceRow.registry_revision
            == activation.registry_revision,
            SkillActivationProvenanceRow.activation_sha256
            == activation.activation_sha256,
        )
    )
    if (
        activation_job is None
        or activation_job.status != "SUCCEEDED"
        or activation_provenance is None
        or certification_provenance is None
        or activation_provenance.certification_authority_sha256
        != certification_provenance.authority_sha256
        or activation_provenance.authority_sha256
        != activation_provenance_sha256(activation_provenance)
        or activation_job.job_json.get("build_provenance_sha256")
        != build_provenance.authority_sha256
    ):
        raise WorkflowInvariantError(
            "active Skill Activation did not retain its frozen Build provenance"
        )
    activation_receipt = await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == activation.tenant_id,
            JobStepReceiptRow.job_id == activation_job.job_id,
            JobStepReceiptRow.step_name == "REGISTRY_ACTIVATED",
        )
    )
    expected_receipt = {
        "activation_id": activation.activation_id,
        "previous_registry_revision": activation.previous_registry_revision,
        "registry_revision": activation.registry_revision,
        "entry_sha256": entry.entry_sha256,
        "activation_sha256": activation.activation_sha256,
        "certification_sha256": certification.certification_sha256,
        "artifact_authority_sha256": job_json.get("artifact_authority_sha256"),
        "build_provenance_sha256": build_provenance.authority_sha256,
    }
    if (
        activation_receipt is None
        or activation_receipt.input_sha256 != activation_job.request_sha256
        or activation_receipt.fencing_token != activation_job.fencing_token
        or activation_receipt.completed_at > activation_job.updated_at
        or activation_receipt.output_sha256
        != workflow_receipt_sha256(expected_receipt)
        or activation_receipt.receipt_json != expected_receipt
    ):
        raise WorkflowInvariantError("active Skill provenance receipt drifted")
    active_ref = SkillRef(
        skill_id=activation.skill_id,
        skill_version_id=activation.skill_version_id,
        artifact_sha256=activation.artifact_sha256,
        certification_id=activation.certification_id,
    )
    if active_ref != skill_ref:
        raise ActivationAuthorityNotFound("requested Skill differs from current Activation")
    return ValidatedActivationAuthority(head, entry, activation)


def validate_activation_registry_authority(
    *,
    head: RegistryHeadRow,
    entry: RegistryEntryRow,
    activation: SkillActivationRow,
    authority_id: str,
) -> None:
    """Fail closed on JSON/hash/column drift across the full Activation scope."""

    activation_wire = _object(activation.activation_json, "Activation")
    entry_wire = _object(entry.entry_json, "RegistryEntry")
    origin_wire = _object(activation_wire.get("request_context"), "Activation context")
    if set(origin_wire) != {
        "request_id",
        "correlation_id",
        "trace_id",
        "requested_at",
        "actor",
        "content_ref",
        "schema_version",
    }:
        raise WorkflowInvariantError("Activation request_context schema drifted")
    try:
        origin = request_context_from_data(origin_wire)
    except (KeyError, TypeError, ValueError) as error:
        raise WorkflowInvariantError("Activation request_context is invalid") from error
    if request_context_data(origin) != origin_wire:
        raise WorkflowInvariantError("Activation request_context is not canonical")

    scope = _object(activation_wire.get("activation_scope"), "Activation scope")
    if set(scope) != {"world_id", "agent_profile_id"}:
        raise WorkflowInvariantError("Activation scope schema drifted")
    expected_scope = (
        activation.tenant_id,
        activation.actor_id,
        activation.content_hash,
        activation.world_id,
        activation.agent_profile_id,
        activation.registry_revision,
    )
    if (
        entry.tenant_id,
        entry.actor_id,
        entry.content_hash,
        entry.world_id,
        entry.agent_profile_id,
        entry.revision,
    ) != expected_scope or (
        head.tenant_id,
        head.actor_id,
        head.content_hash,
        head.world_id,
        head.agent_profile_id,
        head.revision,
    ) != expected_scope:
        raise WorkflowInvariantError("Activation, RegistryEntry and head scopes differ")
    if head.authority_id != authority_id:
        raise WorkflowInvariantError("Registry head launch authority drifted")
    if (
        origin.actor.tenant_id != activation.tenant_id
        or origin.actor.actor_id != activation.actor_id
        or origin.content_ref.content_hash != activation.content_hash
    ):
        raise WorkflowInvariantError("Activation origin differs from its durable scope")
    if (
        activation.activated_at.tzinfo is None
        or entry.activated_at.tzinfo is None
        or head.updated_at.tzinfo is None
    ):
        raise WorkflowInvariantError("Activation timestamp is not timezone-aware")

    activated_at = _iso(activation.activated_at)
    expected_activation = {
        "request_context": origin_wire,
        "activation_id": activation.activation_id,
        "skill_id": activation.skill_id,
        "skill_version_id": activation.skill_version_id,
        "certification_id": activation.certification_id,
        "artifact_sha256": activation.artifact_sha256,
        "activation_scope": {
            "world_id": activation.world_id,
            "agent_profile_id": activation.agent_profile_id,
        },
        "previous_registry_revision": activation.previous_registry_revision,
        "registry_revision": activation.registry_revision,
        "activated_at": activated_at,
    }
    expected_entry = {
        "authority_id": authority_id,
        "activation_id": activation.activation_id,
        "actor_id": activation.actor_id,
        "content_hash": activation.content_hash,
        "world_id": activation.world_id,
        "agent_profile_id": activation.agent_profile_id,
        "skill_id": activation.skill_id,
        "skill_version_id": activation.skill_version_id,
        "certification_id": activation.certification_id,
        "artifact_sha256": activation.artifact_sha256,
        "previous_revision": activation.previous_registry_revision,
        "revision": activation.registry_revision,
        "activated_at": activated_at,
    }
    if (
        activation_wire != expected_activation
        or entry_wire != expected_entry
        or activation.activation_sha256 != canonical_json_sha256(expected_activation)
        or entry.entry_sha256 != canonical_json_sha256(expected_entry)
        or entry.activated_at != activation.activated_at
        or head.updated_at != activation.activated_at
        or entry.previous_revision != activation.previous_registry_revision
        or entry.skill_id != activation.skill_id
        or entry.skill_version_id != activation.skill_version_id
        or entry.certification_id != activation.certification_id
        or entry.artifact_sha256 != activation.artifact_sha256
        or activation.registry_revision != activation.previous_registry_revision + 1
    ):
        raise WorkflowInvariantError("Activation/RegistryEntry JSON or hash authority drifted")


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowInvariantError(f"{label} is not an object")
    return dict(value)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ActivationAuthorityNotFound",
    "ValidatedActivationAuthority",
    "load_current_activation_authority",
    "validate_historical_activation_authority",
    "validate_activation_registry_authority",
]
