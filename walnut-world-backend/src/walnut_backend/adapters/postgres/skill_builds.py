"""Atomic Skill Build acceptance and authorized read model."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_build import canonical_source_bundle_sha256
from yaya_agent_contracts import (
    CommandCreateReceipt,
    CommandRecord,
    CommandStatus,
    ContentRef,
    Failure,
    NewCommand,
    OperationContext,
    Result,
    Success,
    VersionSet,
    canonical_json_sha256,
)

from walnut_backend.certified_skill_schema import (
    CertifiedSkillSchemaError,
    policy_parameter_schema,
    validated_certified_parameter_schema,
)

from .command_store import PostgresCommandStore, validated_command_record
from .models import (
    AgentSessionRow,
    BuildPolicyRow,
    CommandRow,
    CurrentSessionBindingRow,
    EvidenceRow,
    IdempotencyReceiptRow,
    JobStepReceiptRow,
    LaunchAuthorityRow,
    ProductDraftRevisionAssistanceRow,
    ProductDraftRevisionRow,
    ProductDraftRow,
    SkillArtifactRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
    SkillCertificationProvenanceRow,
    SkillCertificationRow,
    WorkflowJobRow,
    error_data,
    json_value,
    request_context_data,
)
from .skill_provenance import (
    _validate_draft_lineage,
    build_command_receipt_authority_sha256,
    build_provenance_sha256,
    validate_build_provenance,
    validate_build_terminal_authority,
)
from .workflow_jobs import (
    PostgresWorkflowJobStore,
    workflow_job_id,
    workflow_receipt_sha256,
    workflow_step_receipt_id,
)


class PostgresSkillBuildStore:
    """Owns the canonical Build resource created with its Command receipt."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        command_store: PostgresCommandStore,
        workflow_jobs: PostgresWorkflowJobStore | None = None,
    ) -> None:
        self._sessions = session_factory
        self._command_store = command_store
        self._workflow_jobs = workflow_jobs or PostgresWorkflowJobStore(session_factory)

    async def accept(
        self,
        command: NewCommand,
        request_body: Mapping[str, Any],
        context: OperationContext,
    ) -> Result[tuple[dict[str, Any], CommandCreateReceipt]]:
        async with self._sessions() as session, session.begin():
            tenant_id, actor_id, operation, idempotency_key = command.idempotency_scope(context)
            replay = await session.scalar(
                select(IdempotencyReceiptRow).where(
                    IdempotencyReceiptRow.tenant_id == tenant_id,
                    IdempotencyReceiptRow.actor_id == actor_id,
                    IdempotencyReceiptRow.operation == operation,
                    IdempotencyReceiptRow.idempotency_key == idempotency_key,
                )
            )
            effective_command = command
            effective_context = context
            if replay is None:
                authority = await session.scalar(
                    select(LaunchAuthorityRow).where(
                        LaunchAuthorityRow.tenant_id == tenant_id,
                        LaunchAuthorityRow.actor_id == actor_id,
                        LaunchAuthorityRow.active.is_(True),
                    )
                )
                if authority is None:
                    return Failure(
                        _invariant("POLICY", "active server launch authority is missing")
                    )
                policy = await session.scalar(
                    select(BuildPolicyRow).where(
                        BuildPolicyRow.tenant_id == tenant_id,
                        BuildPolicyRow.build_policy_id == authority.build_policy_id,
                        BuildPolicyRow.actor_id == actor_id,
                        BuildPolicyRow.content_hash == authority.content_hash,
                        BuildPolicyRow.active.is_(True),
                    )
                )
                if policy is None:
                    return Failure(_invariant("POLICY", "active server Build policy is missing"))
                try:
                    policy_parameter_schema(policy.policy_json, policy_sha256=policy.policy_sha256)
                except CertifiedSkillSchemaError as error:
                    return Failure(_invariant("POLICY", str(error)))
                requested = request_body.get("requested_capabilities", [])
                if (
                    request_body.get("compiler_profile") != policy.compiler_profile
                    or request_body.get("test_suite_version") != policy.test_suite_version
                    or not isinstance(requested, list)
                    or any(
                        not isinstance(item, str) or item not in policy.allowed_capabilities
                        for item in requested
                    )
                    or len(set(requested)) != len(requested)
                ):
                    return Failure(_mismatch("Build request differs from active server policy"))
                effective_command = replace(
                    command,
                    versions=replace(
                        command.versions,
                        policy_version=policy.build_policy_id,
                        compiler_version=policy.compiler_version,
                        sandbox_image_digest=policy.sandbox_image_digest,
                        test_suite_version=policy.test_suite_version,
                    ),
                )
                effective_context = replace(
                    context,
                    content_ref=ContentRef(
                        unit_id=authority.content_unit_id,
                        version=authority.content_version,
                        content_hash=authority.content_hash,
                    ),
                )
                provenance = await _resolve_draft_provenance(
                    session,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    skill_id=str(request_body.get("skill_id", "")),
                    draft_revision=request_body.get("client_draft_revision"),
                    source_bundle=request_body.get("source_bundle"),
                    authority_id=authority.authority_id,
                    content_hash=authority.content_hash,
                )
                if isinstance(provenance, Failure):
                    return provenance
            else:
                provenance = None
            command_result = await self._command_store.accept_once_in_session(
                session, effective_command, effective_context
            )
            if isinstance(command_result, Failure):
                return command_result
            receipt = command_result.value
            if receipt.created:
                build = _initial_build(receipt, request_body)
                session.add(
                    SkillBuildRow(
                        build_id=build["build_id"],
                        tenant_id=context.actor.tenant_id,
                        actor_id=context.actor.actor_id,
                        command_id=receipt.command.command_id,
                        skill_id=build["skill_id"],
                        status=build["status"],
                        terminal=build["terminal"],
                        created_at=receipt.command.accepted_at,
                        updated_at=receipt.command.updated_at,
                        build_json=build,
                        request_json=dict(request_body),
                    )
                )
                await session.flush()
                if provenance is None:
                    return Failure(
                        _invariant("ACCEPT", "new Build has no immutable Draft authority")
                    )
                draft_revision, assistance = provenance.value
                command_receipt = await session.scalar(
                    select(IdempotencyReceiptRow).where(
                        IdempotencyReceiptRow.tenant_id == tenant_id,
                        IdempotencyReceiptRow.actor_id == actor_id,
                        IdempotencyReceiptRow.operation
                        == effective_command.command_type,
                        IdempotencyReceiptRow.command_id
                        == receipt.command.command_id,
                    )
                )
                if command_receipt is None:
                    return Failure(
                        _invariant("ACCEPT", "new Build has no Command receipt authority")
                    )
                persisted_provenance = SkillBuildProvenanceRow(
                    build_id=build["build_id"],
                    provenance_kind="IMMUTABLE_DRAFT",
                    legacy_marker_id=None,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    build_request_sha256=canonical_json_sha256(request_body),
                    command_receipt_id=command_receipt.receipt_id,
                    command_receipt_authority_sha256=(
                        build_command_receipt_authority_sha256(command_receipt)
                    ),
                    workflow_job_id=workflow_job_id(
                        tenant_id, receipt.command.command_id
                    ),
                    workflow_request_sha256=effective_command.request_sha256,
                    session_id=draft_revision.session_id,
                    draft_id=draft_revision.draft_id,
                    skill_id=draft_revision.skill_id,
                    draft_revision_row_id=draft_revision.draft_revision_row_id,
                    draft_revision=draft_revision.revision,
                    draft_sha256=draft_revision.draft_sha256,
                    source_bundle_sha256=draft_revision.source_bundle_sha256,
                    origin_accepted_revision_row_id=(
                        assistance.origin_accepted_revision_row_id
                        if assistance is not None
                        else None
                    ),
                    patch_id=(assistance.patch_id if assistance is not None else None),
                    patch_decision_id=(
                        assistance.patch_decision_id if assistance is not None else None
                    ),
                    assistance_authority=(
                        "SKILL_PATCH" if assistance is not None else "NONE"
                    ),
                    authority_sha256="0" * 64,
                    created_at=receipt.command.accepted_at,
                )
                persisted_provenance.authority_sha256 = build_provenance_sha256(
                    persisted_provenance
                )
                await self._workflow_jobs.enqueue_in_session(
                    session,
                    tenant_id=context.actor.tenant_id,
                    command_id=receipt.command.command_id,
                    operation=effective_command.command_type,
                    subject_type="SKILL_BUILD",
                    subject_id=build["build_id"],
                    request_sha256=effective_command.request_sha256,
                    job={
                        "schema_version": "1.0.0",
                        "request_context": request_context_data(receipt.command.request_context),
                        "build_id": build["build_id"],
                        "build_provenance_sha256": persisted_provenance.authority_sha256,
                        "request": dict(request_body),
                    },
                )
                session.add(persisted_provenance)
                return Success((build, receipt))
            row = await session.scalar(
                select(SkillBuildRow).where(
                    SkillBuildRow.tenant_id == context.actor.tenant_id,
                    SkillBuildRow.actor_id == context.actor.actor_id,
                    SkillBuildRow.command_id == receipt.command.command_id,
                )
            )
            if row is None:
                return Failure(_invariant("ACCEPT", "accepted build command has no durable Build"))
            persisted_provenance = await session.scalar(
                select(SkillBuildProvenanceRow).where(
                    SkillBuildProvenanceRow.build_id == row.build_id
                )
            )
            command_row = await session.scalar(
                select(CommandRow).where(
                    CommandRow.tenant_id == row.tenant_id,
                    CommandRow.actor_id == row.actor_id,
                    CommandRow.command_id == row.command_id,
                )
            )
            job = await session.scalar(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == row.tenant_id,
                    WorkflowJobRow.job_id
                    == (
                        persisted_provenance.workflow_job_id
                        if persisted_provenance is not None
                        else ""
                    ),
                )
            )
            if (
                persisted_provenance is None
                or command_row is None
                or job is None
                or not await validate_build_provenance(session, persisted_provenance)
                or not await validate_historical_build_authority(
                    session,
                    row,
                    command_row,
                    receipt.command,
                    job,
                )
            ):
                return Failure(
                    _invariant("ACCEPT", "accepted Build has no immutable Draft provenance")
                )
            return Success((row.build_json, receipt))

    async def get(self, build_id: str, context: OperationContext) -> Result[dict[str, Any]]:
        async with self._sessions() as session:
            row = await session.scalar(
                select(SkillBuildRow).where(
                    SkillBuildRow.build_id == build_id,
                    SkillBuildRow.tenant_id == context.actor.tenant_id,
                    SkillBuildRow.actor_id == context.actor.actor_id,
                )
            )
            command = (
                await session.scalar(
                    select(CommandRow).where(
                        CommandRow.command_id == row.command_id,
                        CommandRow.tenant_id == row.tenant_id,
                        CommandRow.actor_id == row.actor_id,
                    )
                )
                if row is not None
                else None
            )
            record = (
                await validated_command_record(session, command) if command is not None else None
            )
            job = (
                await session.scalar(
                    select(WorkflowJobRow).where(
                        WorkflowJobRow.tenant_id == row.tenant_id,
                        WorkflowJobRow.command_id == row.command_id,
                    )
                )
                if row is not None
                else None
            )
            receipts = (
                list(
                    (
                        await session.scalars(
                            select(JobStepReceiptRow).where(
                                JobStepReceiptRow.tenant_id == row.tenant_id,
                                JobStepReceiptRow.job_id == job.job_id,
                            )
                        )
                    ).all()
                )
                if row is not None and job is not None
                else []
            )
            artifacts = (
                list(
                    (
                        await session.scalars(
                            select(SkillArtifactRow).where(
                                SkillArtifactRow.tenant_id == row.tenant_id,
                                SkillArtifactRow.build_id == row.build_id,
                            )
                        )
                    ).all()
                )
                if row is not None
                else []
            )
            certifications = (
                list(
                    (
                        await session.scalars(
                            select(SkillCertificationRow).where(
                                SkillCertificationRow.tenant_id == row.tenant_id,
                                SkillCertificationRow.build_id == row.build_id,
                            )
                        )
                    ).all()
                )
                if row is not None
                else []
            )
            evidence = (
                list(
                    (
                        await session.scalars(
                            select(EvidenceRow).where(
                                EvidenceRow.tenant_id == row.tenant_id,
                                EvidenceRow.command_id == row.command_id,
                            )
                        )
                    ).all()
                )
                if row is not None
                else []
            )
            authority = (
                await session.scalar(
                    select(LaunchAuthorityRow).where(
                        LaunchAuthorityRow.tenant_id == row.tenant_id,
                        LaunchAuthorityRow.actor_id == row.actor_id,
                        LaunchAuthorityRow.content_unit_id
                        == record.request_context.content_ref.unit_id,
                        LaunchAuthorityRow.content_version
                        == record.request_context.content_ref.version,
                        LaunchAuthorityRow.content_hash
                        == record.request_context.content_ref.content_hash,
                        LaunchAuthorityRow.active.is_(True),
                    )
                )
                if row is not None and record is not None
                else None
            )
            policy = (
                await session.scalar(
                    select(BuildPolicyRow).where(
                        BuildPolicyRow.tenant_id == authority.tenant_id,
                        BuildPolicyRow.build_policy_id == authority.build_policy_id,
                        BuildPolicyRow.actor_id == authority.actor_id,
                        BuildPolicyRow.content_hash == authority.content_hash,
                        BuildPolicyRow.active.is_(True),
                    )
                )
                if authority is not None
                else None
            )
            provenance = (
                await session.scalar(
                    select(SkillBuildProvenanceRow).where(
                        SkillBuildProvenanceRow.build_id == row.build_id,
                        SkillBuildProvenanceRow.tenant_id == row.tenant_id,
                        SkillBuildProvenanceRow.actor_id == row.actor_id,
                    )
                )
                if row is not None
                else None
            )
            provenance_valid = (
                await validate_build_provenance(session, provenance)
                if provenance is not None
                else False
            )
            terminal_authority_valid = (
                await validate_build_terminal_authority(session, row, provenance)
                if row is not None and provenance is not None
                else False
            )
            certification_seal_valid = await _certification_seal_matches(
                session,
                row=row,
                command=command,
                job=job,
                receipts=receipts,
                certifications=certifications,
            )
        if row is None:
            return Failure(_not_found())
        if (
            command is None
            or record is None
            or job is None
            or authority is None
            or policy is None
            or provenance is None
            or not provenance_valid
            or not terminal_authority_valid
            or not certification_seal_valid
            or not _build_authority_matches(
                row,
                command,
                record,
                job,
                authority,
                policy,
                provenance,
                receipts,
                artifacts,
                certifications,
                evidence,
            )
        ):
            return Failure(_invariant("READ", "Build durable authority drifted"))
        return Success(row.build_json)


async def validate_historical_build_authority(
    session: AsyncSession,
    row: SkillBuildRow,
    command: CommandRow,
    record: CommandRecord,
    job: WorkflowJobRow,
) -> bool:
    """Close one Build resource without recursively re-reading its Command."""

    receipts = list(
        (
            await session.scalars(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == row.tenant_id,
                    JobStepReceiptRow.job_id == job.job_id,
                )
            )
        ).all()
    )
    artifacts = list(
        (
            await session.scalars(
                select(SkillArtifactRow).where(
                    SkillArtifactRow.tenant_id == row.tenant_id,
                    SkillArtifactRow.build_id == row.build_id,
                )
            )
        ).all()
    )
    certifications = list(
        (
            await session.scalars(
                select(SkillCertificationRow).where(
                    SkillCertificationRow.tenant_id == row.tenant_id,
                    SkillCertificationRow.build_id == row.build_id,
                )
            )
        ).all()
    )
    evidence = list(
        (
            await session.scalars(
                select(EvidenceRow).where(
                    EvidenceRow.tenant_id == row.tenant_id,
                    EvidenceRow.command_id == row.command_id,
                )
            )
        ).all()
    )
    authority = await session.scalar(
        select(LaunchAuthorityRow).where(
            LaunchAuthorityRow.tenant_id == row.tenant_id,
            LaunchAuthorityRow.actor_id == row.actor_id,
            LaunchAuthorityRow.content_unit_id
            == record.request_context.content_ref.unit_id,
            LaunchAuthorityRow.content_version
            == record.request_context.content_ref.version,
            LaunchAuthorityRow.content_hash
            == record.request_context.content_ref.content_hash,
            LaunchAuthorityRow.active.is_(True),
        )
    )
    policy = (
        await session.scalar(
            select(BuildPolicyRow).where(
                BuildPolicyRow.tenant_id == authority.tenant_id,
                BuildPolicyRow.build_policy_id == authority.build_policy_id,
                BuildPolicyRow.actor_id == authority.actor_id,
                BuildPolicyRow.content_hash == authority.content_hash,
                BuildPolicyRow.active.is_(True),
            )
        )
        if authority is not None
        else None
    )
    provenance = await session.scalar(
        select(SkillBuildProvenanceRow).where(
            SkillBuildProvenanceRow.build_id == row.build_id,
            SkillBuildProvenanceRow.tenant_id == row.tenant_id,
            SkillBuildProvenanceRow.actor_id == row.actor_id,
        )
    )
    seal_valid = await _certification_seal_matches(
        session,
        row=row,
        command=command,
        job=job,
        receipts=receipts,
        certifications=certifications,
    )
    terminal_authority_valid = (
        await validate_build_terminal_authority(session, row, provenance)
        if provenance is not None
        else False
    )
    return (
        authority is not None
        and policy is not None
        and provenance is not None
        and await validate_build_provenance(session, provenance)
        and terminal_authority_valid
        and seal_valid
        and _build_authority_matches(
            row,
            command,
            record,
            job,
            authority,
            policy,
            provenance,
            receipts,
            artifacts,
            certifications,
            evidence,
        )
    )


async def _certification_seal_matches(
    session: AsyncSession,
    *,
    row: SkillBuildRow | None,
    command: CommandRow | None,
    job: WorkflowJobRow | None,
    receipts: Sequence[JobStepReceiptRow],
    certifications: Sequence[SkillCertificationRow],
) -> bool:
    if row is None:
        return False
    if row.status != "CERTIFIED":
        return True
    if command is None or job is None or len(certifications) != 1:
        return False
    terminal = _terminal_receipts(receipts)
    if len(terminal) != 1:
        return False
    certification = certifications[0]
    sealed = await session.scalar(
        select(SkillCertificationProvenanceRow).where(
            SkillCertificationProvenanceRow.certification_id
            == certification.certification_id,
            SkillCertificationProvenanceRow.tenant_id == row.tenant_id,
            SkillCertificationProvenanceRow.actor_id == row.actor_id,
            SkillCertificationProvenanceRow.build_id == row.build_id,
        )
    )
    if sealed is None:
        return False
    from .certification_authority import (
        certification_command_authority_sha256,
        certification_provenance_sha256,
        certification_receipt_authority_sha256,
        certification_workflow_job_sha256,
    )

    return (
        sealed.authority_sha256 == certification_provenance_sha256(sealed)
        and sealed.workflow_job_id == job.job_id
        and sealed.workflow_job_sha256 == certification_workflow_job_sha256(job)
        and sealed.command_authority_sha256
        == certification_command_authority_sha256(command)
        and sealed.build_receipt_id == terminal[0].receipt_id
        and sealed.build_receipt_authority_sha256
        == certification_receipt_authority_sha256(terminal[0])
    )


async def _resolve_draft_provenance(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_id: str,
    skill_id: str,
    draft_revision: object,
    source_bundle: object,
    authority_id: str,
    content_hash: str,
) -> Result[
    tuple[ProductDraftRevisionRow, ProductDraftRevisionAssistanceRow | None]
]:
    """Resolve one current immutable Draft; never infer from a recent Patch."""

    if (
        not skill_id
        or isinstance(draft_revision, bool)
        or not isinstance(draft_revision, int)
        or draft_revision < 1
        or not isinstance(source_bundle, Mapping)
    ):
        return Failure(_mismatch("Build requires one current immutable Draft revision"))
    source_sha256 = canonical_source_bundle_sha256(source_bundle)
    rows = list(
        (
            await session.scalars(
                select(ProductDraftRevisionRow)
                .join(
                    ProductDraftRow,
                    (ProductDraftRow.tenant_id == ProductDraftRevisionRow.tenant_id)
                    & (ProductDraftRow.actor_id == ProductDraftRevisionRow.actor_id)
                    & (ProductDraftRow.session_id == ProductDraftRevisionRow.session_id)
                    & (ProductDraftRow.draft_id == ProductDraftRevisionRow.draft_id)
                    & (ProductDraftRow.revision == ProductDraftRevisionRow.revision)
                    & (
                        ProductDraftRow.draft_sha256
                        == ProductDraftRevisionRow.draft_sha256
                    ),
                )
                .join(
                    AgentSessionRow,
                    (AgentSessionRow.tenant_id == ProductDraftRevisionRow.tenant_id)
                    & (AgentSessionRow.actor_id == ProductDraftRevisionRow.actor_id)
                    & (AgentSessionRow.session_id == ProductDraftRevisionRow.session_id)
                    & (AgentSessionRow.status == "ACTIVE"),
                )
                .join(
                    CurrentSessionBindingRow,
                    (CurrentSessionBindingRow.tenant_id == ProductDraftRevisionRow.tenant_id)
                    & (CurrentSessionBindingRow.actor_id == ProductDraftRevisionRow.actor_id)
                    & (CurrentSessionBindingRow.session_id == ProductDraftRevisionRow.session_id)
                    & (CurrentSessionBindingRow.authority_id == authority_id)
                    & (CurrentSessionBindingRow.content_hash == content_hash),
                )
                .where(
                    ProductDraftRevisionRow.tenant_id == tenant_id,
                    ProductDraftRevisionRow.actor_id == actor_id,
                    ProductDraftRevisionRow.skill_id == skill_id,
                    ProductDraftRevisionRow.revision == draft_revision,
                    ProductDraftRevisionRow.source_bundle_sha256 == source_sha256,
                )
                .limit(2)
            )
        ).all()
    )
    if len(rows) != 1:
        return Failure(
            _mismatch("Build Draft authority is missing, stale, or ambiguous")
        )
    row = rows[0]
    if row.draft_json.get("source_bundle") != dict(source_bundle):
        return Failure(_mismatch("Build source bundle differs from current Draft bytes"))
    assistance = await _validate_draft_lineage(session, row)
    if assistance is False:
        return Failure(_invariant("POLICY", "Draft assistance lineage is corrupt"))
    return Success((row, assistance))


def _initial_build(
    receipt: CommandCreateReceipt, request_body: Mapping[str, Any]
) -> dict[str, Any]:
    command = receipt.command
    build_id = f"build_{hashlib.sha256(command.command_id.encode('utf-8')).hexdigest()[:24]}"
    timestamp = command.accepted_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    versions = _versions_data(command.versions)
    return {
        "request_context": request_context_data(command.request_context),
        "build_id": build_id,
        "skill_id": request_body["skill_id"],
        "skill_version_id": None,
        "status": "ACCEPTED",
        "terminal": False,
        "created_at": timestamp,
        "updated_at": timestamp,
        "artifact": None,
        "certification": None,
        "phases": [
            {
                "name": phase,
                "status": "PENDING",
                "started_at": None,
                "finished_at": None,
                "diagnostic_codes": [],
            }
            for phase in ("VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST", "CERTIFY")
        ],
        "failure": None,
        "evidence_refs": [],
        "versions": versions,
    }


def _build_authority_matches(
    row: SkillBuildRow,
    command: CommandRow,
    record: CommandRecord,
    job: WorkflowJobRow,
    authority: LaunchAuthorityRow,
    policy: BuildPolicyRow,
    provenance: SkillBuildProvenanceRow,
    receipts: Sequence[JobStepReceiptRow],
    artifacts: Sequence[SkillArtifactRow],
    certifications: Sequence[SkillCertificationRow],
    evidence: Sequence[EvidenceRow],
) -> bool:
    value = row.build_json
    origin = value.get("request_context")
    actor = origin.get("actor") if isinstance(origin, Mapping) else None
    try:
        created_at = _timestamp(value.get("created_at"))
        updated_at = _timestamp(value.get("updated_at"))
    except (TypeError, ValueError):
        return False
    expected_id = f"build_{hashlib.sha256(row.command_id.encode('utf-8')).hexdigest()[:24]}"
    versions = json_value(record.versions)
    expected_job = {
        "schema_version": "1.0.0",
        "request_context": request_context_data(record.request_context),
        "build_id": row.build_id,
        "build_provenance_sha256": provenance.authority_sha256,
        "request": row.request_json,
    }
    try:
        policy_parameter_schema(policy.policy_json, policy_sha256=policy.policy_sha256)
    except CertifiedSkillSchemaError:
        return False
    base_matches = (
        isinstance(actor, Mapping)
        and actor.get("tenant_id") == row.tenant_id
        and actor.get("actor_id") == row.actor_id
        and origin
        == command.record_json.get("request_context")
        == request_context_data(record.request_context)
        and isinstance(versions, dict)
        and value.get("build_id") == row.build_id == expected_id
        and value.get("skill_id") == row.skill_id
        and row.request_json.get("skill_id") == row.skill_id
        and value.get("status") == row.status
        and value.get("terminal") is row.terminal
        and created_at == row.created_at
        and updated_at == row.updated_at
        and record.command_type == "CREATE_SKILL_BUILD"
        and job.tenant_id == row.tenant_id
        and job.command_id == row.command_id
        and job.operation == record.command_type
        and job.subject_type == "SKILL_BUILD"
        and job.subject_id == row.build_id
        and job.job_json == expected_job
        and authority.tenant_id == row.tenant_id
        and authority.actor_id == row.actor_id
        and authority.content_unit_id == record.request_context.content_ref.unit_id
        and authority.content_version == record.request_context.content_ref.version
        and authority.content_hash == record.request_context.content_ref.content_hash
        and authority.active is True
        and policy.tenant_id == row.tenant_id
        and policy.build_policy_id == authority.build_policy_id
        and policy.actor_id == row.actor_id
        and policy.content_hash == record.request_context.content_ref.content_hash
        and policy.active is True
    )
    if not base_matches:
        return False
    command_versions = {key: item for key, item in versions.items() if item is not None}
    if not row.terminal:
        if job.status == "DEAD_LETTER" and record.terminal:
            # An abandoned attempt, not corruption. The workflow exhausted its
            # retries, so the Command settled while the Build row never did.
            # Reading that pair as corrupt is what left a learner unable to
            # build at all: the client replays its Build under the original
            # Idempotency-Key, the replay re-validated this wreck, and every
            # attempt came back 500 -- forever, because authority rows are
            # append-only and nothing will ever revisit a dead-lettered job.
            #
            # The state is perfectly coherent as long as nothing was published,
            # which is what the rest of this branch still requires. Reporting it
            # honestly lets the client see a failed Build and start a new one.
            return (
                value.get("versions") == command_versions
                and value.get("skill_version_id") is None
                and value.get("artifact") is None
                and value.get("certification") is None
                and value.get("evidence_refs") == []
                and not artifacts
                and not certifications
                and not evidence
            )
        return (
            record.terminal is False
            and job.status not in {"SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTER"}
            and value.get("versions") == command_versions
            and value.get("skill_version_id") is None
            and value.get("artifact") is None
            and value.get("certification") is None
            and value.get("failure") is None
            and value.get("evidence_refs") == []
            and not artifacts
            and not certifications
            and not evidence
            and not _terminal_receipts(receipts)
        )
    if row.status == "CERTIFIED":
        return _certified_build_matches(
            row,
            record,
            job,
            authority,
            policy,
            receipts,
            artifacts,
            certifications,
            evidence,
        )
    if row.status == "REJECTED":
        return _rejected_build_matches(
            row,
            record,
            job,
            authority,
            receipts,
            artifacts,
            certifications,
            evidence,
            command_versions,
        )
    return False


_PHASE_NAMES = (
    "VALIDATE_SOURCE",
    "COMPILE",
    "PUBLIC_TEST",
    "HIDDEN_TEST",
    "CERTIFY",
)


def _terminal_receipts(
    receipts: Sequence[JobStepReceiptRow],
) -> tuple[JobStepReceiptRow, ...]:
    return tuple(
        receipt
        for receipt in receipts
        if receipt.step_name in {"BUILD_CERTIFIED", "BUILD_REJECTED"}
    )


def _certified_build_matches(
    row: SkillBuildRow,
    record: CommandRecord,
    job: WorkflowJobRow,
    authority: LaunchAuthorityRow,
    policy: BuildPolicyRow,
    receipts: Sequence[JobStepReceiptRow],
    artifacts: Sequence[SkillArtifactRow],
    certifications: Sequence[SkillCertificationRow],
    evidence_rows: Sequence[EvidenceRow],
) -> bool:
    terminal = _terminal_receipts(receipts)
    if (
        record.status is not CommandStatus.APPLIED
        or not record.terminal
        or record.stage != "COMPLETE"
        or record.updated_at != row.updated_at
        or record.error is not None
        or job.status != "SUCCEEDED"
        or job.phase != "COMPLETE"
        or job.lease_owner is not None
        or job.lease_expires_at is not None
        or job.next_attempt_at is not None
        or job.last_error_json is not None
        or job.attempt < 1
        or job.fencing_token < 1
        or len(terminal) != 1
        or terminal[0].step_name != "BUILD_CERTIFIED"
        or len(artifacts) != 1
        or len(certifications) != 1
        or len(evidence_rows) != 1
    ):
        return False
    receipt = terminal[0]
    artifact = artifacts[0]
    certification = certifications[0]
    evidence_row = evidence_rows[0]
    output = receipt.receipt_json
    artifact_wire = row.build_json.get("artifact")
    certification_wire = row.build_json.get("certification")
    refs = row.build_json.get("evidence_refs")
    versions = row.build_json.get("versions")
    metadata = artifact.metadata_json
    certification_data = certification.certification_json
    evidence_data = evidence_row.evidence_json
    evidence_ref = evidence_data.get("evidence_ref")
    evidence_source = evidence_data.get("source")
    evidence_payload = evidence_data.get("payload")
    evidence_integrity = evidence_data.get("integrity")
    if not isinstance(artifact_wire, Mapping):
        return False
    if not isinstance(certification_wire, Mapping):
        return False
    if not isinstance(versions, Mapping):
        return False
    if not isinstance(evidence_ref, Mapping):
        return False
    if not isinstance(evidence_source, Mapping):
        return False
    if not isinstance(evidence_payload, Mapping):
        return False
    if not isinstance(evidence_integrity, Mapping):
        return False
    if not isinstance(refs, list) or len(refs) != 1 or refs[0] != evidence_ref:
        return False
    skill_version_id = output.get("skill_version_id")
    artifact_sha256 = output.get("artifact_sha256")
    certification_id = output.get("certification_id")
    evidence_id = output.get("evidence_id")
    expected_output_keys = {
        "build_id",
        "skill_version_id",
        "artifact_sha256",
        "certification_id",
        "evidence_id",
        "build_identity",
    }
    expected_metadata_keys = {
        "schema_version",
        "artifact_sha256",
        "source_sha256",
        "build_identity",
        "size_bytes",
        "compiler_profile",
        "compiler_version",
        "compiler_image",
        "test_suite_version",
        "policy_sha256",
        "parameter_schema",
        "parameter_schema_sha256",
    }
    expected_certification_keys = {
        "schema_version",
        "certification_id",
        "build_id",
        "skill_id",
        "skill_version_id",
        "artifact_sha256",
        "source_sha256",
        "actor_id",
        "content_hash",
        "build_policy_id",
        "policy_sha256",
        "capabilities",
        "issued_at",
        "parameter_schema",
        "parameter_schema_sha256",
    }
    expected_evidence_keys = {
        "request_context",
        "evidence_ref",
        "subject",
        "source",
        "occurred_at",
        "recorded_at",
        "integrity",
        "payload",
        "related_evidence",
        "versions",
    }
    expected_evidence_ref_keys = {
        "evidence_id",
        "evidence_type",
        "created_at",
        "sha256",
        "uri",
    }
    expected_evidence_source_keys = {
        "source_type",
        "source_id",
        "command_id",
        "world_id",
    }
    expected_evidence_payload_keys = {
        "evidence_kind",
        "build_id",
        "skill_id",
        "skill_version_id",
        "artifact_sha256",
        "test_suite_version",
        "outcome",
    }
    if (
        set(output) != expected_output_keys
        or output.get("build_id") != row.build_id
        or receipt.tenant_id != row.tenant_id
        or receipt.job_id != job.job_id
        or receipt.receipt_id
        != workflow_step_receipt_id(row.tenant_id, job.job_id, receipt.step_name)
        or receipt.fencing_token != job.fencing_token
        or receipt.input_sha256 != job.request_sha256
        or receipt.output_sha256 != workflow_receipt_sha256(output)
        or not row.updated_at <= receipt.completed_at <= job.updated_at
        or row.build_json.get("skill_version_id") != skill_version_id
        or artifact.build_id != row.build_id
        or artifact.tenant_id != row.tenant_id
        or artifact.actor_id != row.actor_id
        or artifact.content_hash != record.request_context.content_ref.content_hash
        or artifact.skill_id != row.skill_id
        or artifact.artifact_sha256 != artifact_sha256
        or artifact_wire.get("artifact_sha256") != artifact.artifact_sha256
        or artifact_wire.get("source_sha256") != artifact.source_sha256
        or artifact.artifact_uri != f"artifact://sha256/{artifact.artifact_sha256}"
        or artifact.created_at != row.updated_at
        or set(metadata) != expected_metadata_keys
        or metadata.get("schema_version") != "1.0.0"
        or metadata.get("artifact_sha256") != artifact.artifact_sha256
        or metadata.get("source_sha256") != artifact.source_sha256
        or metadata.get("build_identity") != output.get("build_identity")
        or isinstance(metadata.get("size_bytes"), bool)
        or not isinstance(metadata.get("size_bytes"), int)
        or metadata.get("size_bytes", 0) < 1
        or artifact_wire.get("compiler_profile") != metadata.get("compiler_profile")
        or artifact_wire.get("compiler_version") != metadata.get("compiler_version")
        or artifact_wire.get("test_suite_version") != metadata.get("test_suite_version")
        or metadata.get("compiler_profile") != policy.compiler_profile
        or metadata.get("compiler_version") != policy.compiler_version
        or metadata.get("compiler_image") != policy.policy_json.get("compiler_image")
        or metadata.get("test_suite_version") != policy.test_suite_version
        or metadata.get("policy_sha256") != policy.policy_sha256
        or certification.tenant_id != row.tenant_id
        or certification.build_id != row.build_id
        or certification.actor_id != row.actor_id
        or certification.content_hash != record.request_context.content_ref.content_hash
        or certification.skill_id != row.skill_id
        or certification.skill_version_id != skill_version_id
        or certification.artifact_sha256 != artifact.artifact_sha256
        or certification.certification_id != certification_id
        or certification.certification_sha256 != canonical_json_sha256(certification_data)
        or certification.certified_at != row.updated_at
        or set(certification_data) != expected_certification_keys
        or certification_data.get("schema_version") != "1.0.0"
        or certification_wire.get("certification_id") != certification.certification_id
        or certification_wire.get("capabilities") != certification_data.get("capabilities")
        or certification_data.get("build_id") != row.build_id
        or certification_data.get("skill_id") != row.skill_id
        or certification_data.get("skill_version_id") != certification.skill_version_id
        or certification_data.get("artifact_sha256") != artifact.artifact_sha256
        or certification_data.get("source_sha256") != artifact.source_sha256
        or certification_data.get("actor_id") != row.actor_id
        or certification_data.get("content_hash") != record.request_context.content_ref.content_hash
        or certification_data.get("build_policy_id") != policy.build_policy_id
        or certification_data.get("policy_sha256") != metadata.get("policy_sha256")
        or certification_data.get("capabilities") != certification_wire.get("capabilities")
    ):
        return False
    requested_capabilities = row.request_json.get("requested_capabilities")
    if (
        not isinstance(requested_capabilities, list)
        or any(not isinstance(item, str) or not item for item in requested_capabilities)
        or len(set(requested_capabilities)) != len(requested_capabilities)
        or any(item not in policy.allowed_capabilities for item in requested_capabilities)
        or certification_data.get("capabilities") != requested_capabilities
    ):
        return False
    try:
        validated_certified_parameter_schema(
            policy.policy_json,
            metadata,
            certification_data,
            policy_sha256=policy.policy_sha256,
            build_id=row.build_id,
            skill_id=row.skill_id,
            skill_version_id=certification.skill_version_id,
            source_sha256=artifact.source_sha256,
            artifact_sha256=artifact.artifact_sha256,
            certification_id=certification.certification_id,
            build_policy_id=policy.build_policy_id,
            actor_id=row.actor_id,
            content_hash=record.request_context.content_ref.content_hash,
            capabilities=requested_capabilities,
        )
    except CertifiedSkillSchemaError:
        return False
    try:
        issued_at = _timestamp(certification_wire.get("issued_at"))
        certified_at = _timestamp(certification_data.get("issued_at"))
    except (TypeError, ValueError):
        return False
    if issued_at != certification.certified_at or certified_at != certification.certified_at:
        return False
    if (
        evidence_row.evidence_id != evidence_id
        or evidence_row.tenant_id != row.tenant_id
        or evidence_row.actor_id != row.actor_id
        or evidence_row.content_hash != record.request_context.content_ref.content_hash
        or evidence_row.command_id != row.command_id
        or evidence_row.recorded_at != row.updated_at
        or set(evidence_data) != expected_evidence_keys
        or evidence_data.get("request_context") != request_context_data(record.request_context)
        or evidence_data.get("subject") != {"learner_id": authority.learner_id}
        or set(evidence_ref) != expected_evidence_ref_keys
        or evidence_ref.get("evidence_id") != evidence_id
        or evidence_ref.get("evidence_type") != "TEST_REPORT"
        or evidence_ref.get("created_at") != _iso(row.updated_at)
        or evidence_ref.get("sha256") != canonical_json_sha256(evidence_payload)
        or evidence_ref.get("uri") != f"/v1/evidence/{evidence_id}"
        or set(evidence_source) != expected_evidence_source_keys
        or evidence_source.get("source_type") != "SKILL_BUILD"
        or evidence_source.get("source_id") != row.build_id
        or evidence_source.get("command_id") != row.command_id
        or evidence_source.get("world_id") != authority.world_id
        or set(evidence_payload) != expected_evidence_payload_keys
        or evidence_payload.get("evidence_kind") != "BUILD_CERTIFICATION"
        or evidence_payload.get("build_id") != row.build_id
        or evidence_payload.get("skill_id") != row.skill_id
        or evidence_payload.get("skill_version_id") != skill_version_id
        or evidence_payload.get("artifact_sha256") != artifact.artifact_sha256
        or evidence_payload.get("test_suite_version") != metadata.get("test_suite_version")
        or evidence_payload.get("outcome") != "CERTIFIED"
        or evidence_data.get("occurred_at") != _iso(row.updated_at)
        or evidence_data.get("recorded_at") != _iso(row.updated_at)
        or evidence_integrity
        != {
            "payload_sha256": evidence_ref.get("sha256"),
            "previous_evidence_sha256": None,
        }
        or evidence_data.get("related_evidence") != []
        or evidence_data.get("versions") != versions
        or not _command_evidence_matches(record, evidence_ref)
    ):
        return False
    expected_versions = {
        key: item for key, item in json_value(record.versions).items() if item is not None
    }
    expected_versions.update(
        {
            "policy_version": certification_data.get("build_policy_id"),
            "skill_version": skill_version_id,
            "artifact_sha256": artifact.artifact_sha256,
            "compiler_version": metadata.get("compiler_version"),
            "sandbox_image_digest": metadata.get("compiler_image"),
            "test_suite_version": metadata.get("test_suite_version"),
        }
    )
    if dict(versions) != expected_versions:
        return False
    return (
        _terminal_phases_match(
            row.build_json.get("phases"), row.updated_at, failed_stage=None, diagnostics=()
        )
        and row.build_json.get("failure") is None
    )


def _command_evidence_matches(record: CommandRecord, evidence_ref: Mapping[str, Any]) -> bool:
    if len(record.evidence_refs) != 1:
        return False
    reference = record.evidence_refs[0]
    try:
        created_at = _timestamp(evidence_ref.get("created_at"))
    except (TypeError, ValueError):
        return False
    return (
        reference.evidence_id == evidence_ref.get("evidence_id")
        and reference.evidence_type.value == evidence_ref.get("evidence_type")
        and reference.created_at == created_at
        and reference.sha256 == evidence_ref.get("sha256")
        and reference.uri == evidence_ref.get("uri")
    )


def _rejection_evidence_matches(
    row: SkillBuildRow,
    record: CommandRecord,
    authority: LaunchAuthorityRow,
    evidence_row: EvidenceRow,
    evidence_id: object,
) -> bool:
    """Hold a rejection's Evidence to the same shape the Build worker wrote.

    Rejections became evidence-bearing so that a learner stuck on compiler errors
    is visible to the teaching policy at all. Reading one back must therefore be
    as strict as reading a certification: the row has to describe *this* Build,
    under this Command, with the diagnostics the Build row already settled on.
    """

    failure = row.build_json.get("failure")
    details = failure.get("details") if isinstance(failure, Mapping) else None
    diagnostics = details.get("diagnostic_codes") if isinstance(details, Mapping) else None
    phases = row.build_json.get("phases")
    stage = None
    if isinstance(phases, Sequence):
        for phase in phases:
            if isinstance(phase, Mapping) and phase.get("status") == "FAILED":
                stage = phase.get("name")
                break

    data = evidence_row.evidence_json
    if not isinstance(data, Mapping):
        return False
    payload = data.get("payload")
    source = data.get("source")
    reference = data.get("evidence_ref")
    if (
        not isinstance(payload, Mapping)
        or not isinstance(source, Mapping)
        or not isinstance(reference, Mapping)
    ):
        return False

    expected_payload = {
        "evidence_kind": "BUILD_REJECTION",
        "build_id": row.build_id,
        "skill_id": row.skill_id,
        "test_suite_version": payload.get("test_suite_version"),
        "outcome": "REJECTED",
        "failure_stage": stage,
        "failure_code": payload.get("failure_code"),
        "diagnostic_codes": list(diagnostics) if isinstance(diagnostics, list) else [],
    }
    return (
        evidence_row.evidence_id == evidence_id
        and evidence_row.tenant_id == row.tenant_id
        and evidence_row.actor_id == row.actor_id
        and evidence_row.content_hash == record.request_context.content_ref.content_hash
        and evidence_row.command_id == row.command_id
        and evidence_row.recorded_at == row.updated_at
        and dict(payload) == expected_payload
        and dict(source)
        == {
            "source_type": "SKILL_BUILD",
            "source_id": row.build_id,
            "command_id": row.command_id,
            "world_id": authority.world_id,
        }
        and data.get("subject") == {"learner_id": authority.learner_id}
        and reference.get("evidence_id") == evidence_id
        and reference.get("evidence_type") == "TEST_REPORT"
        and reference.get("created_at") == _iso(row.updated_at)
        and reference.get("sha256") == canonical_json_sha256(dict(payload))
        and reference.get("uri") == f"/v1/evidence/{evidence_id}"
    )


def _rejected_build_matches(
    row: SkillBuildRow,
    record: CommandRecord,
    job: WorkflowJobRow,
    authority: LaunchAuthorityRow,
    receipts: Sequence[JobStepReceiptRow],
    artifacts: Sequence[SkillArtifactRow],
    certifications: Sequence[SkillCertificationRow],
    evidence_rows: Sequence[EvidenceRow],
    command_versions: Mapping[str, Any],
) -> bool:
    terminal = _terminal_receipts(receipts)
    if (
        record.status is not CommandStatus.REJECTED
        or not record.terminal
        or record.stage != "VALIDATE"
        or record.updated_at != row.updated_at
        or record.result is not None
        or record.evidence_refs
        or job.status != "FAILED"
        or job.lease_owner is not None
        or job.lease_expires_at is not None
        or job.next_attempt_at is not None
        or job.attempt < 1
        or job.fencing_token < 1
        or len(terminal) != 1
        or terminal[0].step_name != "BUILD_REJECTED"
        or artifacts
        or certifications
        # What must stay absent either way is an artifact or a certification --
        # nothing was published. Whether Evidence must exist is decided by the
        # receipt below, which is the authority that names it.
        or len(evidence_rows) > 1
    ):
        return False
    receipt = terminal[0]
    output = receipt.receipt_json
    failure = row.build_json.get("failure")
    details = failure.get("details") if isinstance(failure, Mapping) else None
    diagnostics = output.get("diagnostic_codes")
    failed_stage = output.get("failure_stage")
    command_error = error_data(record.error)
    expected_command_error = dict(failure) if isinstance(failure, Mapping) else None
    if expected_command_error is not None:
        expected_command_error["stage"] = "VALIDATE"
    if (
        not isinstance(failure, Mapping)
        or not isinstance(details, Mapping)
        or not isinstance(diagnostics, list)
        or any(not isinstance(item, str) for item in diagnostics)
        or not isinstance(failed_stage, str)
        or failed_stage not in _PHASE_NAMES
        # Rejections only started recording Evidence once a learner stuck on the
        # compiler had to become visible to the teaching policy. Receipts written
        # before that name no Evidence, and the Builds they settled are still
        # perfectly valid history -- authority rows are append-only, and inventing
        # an evidence_id for them would fabricate a record no worker ever wrote.
        or set(output)
        not in (
            {
                "build_id",
                "evidence_id",
                "failure_code",
                "failure_stage",
                "diagnostic_codes",
                "source_sha256",
                "build_identity",
            },
            {
                "build_id",
                "failure_code",
                "failure_stage",
                "diagnostic_codes",
                "source_sha256",
                "build_identity",
            },
        )
        or output.get("build_id") != row.build_id
        or receipt.receipt_id
        != workflow_step_receipt_id(row.tenant_id, job.job_id, receipt.step_name)
        or receipt.tenant_id != row.tenant_id
        or receipt.job_id != job.job_id
        or receipt.fencing_token != job.fencing_token
        or receipt.input_sha256 != job.request_sha256
        or receipt.output_sha256 != workflow_receipt_sha256(output)
        or not row.updated_at <= receipt.completed_at <= job.updated_at
        or job.phase != failed_stage
        or job.last_error_json != failure
        or command_error != expected_command_error
        or failure.get("stage") != failed_stage
        or details.get("pipeline_code") != output.get("failure_code")
        or details.get("diagnostic_codes") != diagnostics
        or row.build_json.get("skill_version_id") is not None
        or row.build_json.get("artifact") is not None
        or row.build_json.get("certification") is not None
        or row.build_json.get("evidence_refs") != []
        or row.build_json.get("versions") != command_versions
    ):
        return False
    evidence_id = output.get("evidence_id")
    if evidence_id is None:
        if evidence_rows:
            return False
    elif len(evidence_rows) != 1 or not _rejection_evidence_matches(
        row, record, authority, evidence_rows[0], evidence_id
    ):
        return False
    return _terminal_phases_match(
        row.build_json.get("phases"),
        row.updated_at,
        failed_stage=failed_stage,
        diagnostics=tuple(diagnostics),
    )


def _terminal_phases_match(
    raw: object,
    finished_at: datetime,
    *,
    failed_stage: str | None,
    diagnostics: tuple[str, ...],
) -> bool:
    if isinstance(raw, str | bytes | bytearray) or not isinstance(raw, Sequence):
        return False
    phases = list(raw)
    if len(phases) != len(_PHASE_NAMES):
        return False
    failure_index = _PHASE_NAMES.index(failed_stage) if failed_stage is not None else None
    started_at: datetime | None = None
    for index, (name, phase) in enumerate(zip(_PHASE_NAMES, phases, strict=True)):
        if not isinstance(phase, Mapping) or set(phase) != {
            "name",
            "status",
            "started_at",
            "finished_at",
            "diagnostic_codes",
        }:
            return False
        if failure_index is None or index < failure_index:
            expected_status = "PASSED"
            expected_codes: list[str] = []
        elif index == failure_index:
            expected_status = "FAILED"
            expected_codes = list(diagnostics) or ["BUILD_POLICY_REJECTED"]
        else:
            if (
                phase.get("name") != name
                or phase.get("status") != "SKIPPED"
                or phase.get("started_at") is not None
                or phase.get("finished_at") is not None
                or phase.get("diagnostic_codes") != []
            ):
                return False
            continue
        try:
            phase_started = _timestamp(phase.get("started_at"))
            phase_finished = _timestamp(phase.get("finished_at"))
        except (TypeError, ValueError):
            return False
        if started_at is None:
            started_at = phase_started
        if (
            phase.get("name") != name
            or phase.get("status") != expected_status
            or phase.get("diagnostic_codes") != expected_codes
            or phase_started != started_at
            or phase_finished != finished_at
            or phase_started > phase_finished
        ):
            return False
    return True


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result


def _versions_data(versions: VersionSet) -> dict[str, Any]:
    return {key: value for key, value in json_value(versions).items() if value is not None}


def _not_found() -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    return ContractError(
        code="NOT_FOUND",
        category=ErrorCategory.VALIDATION,
        retryable=False,
        user_message_key="resource.not_found",
        stage="READ",
        message="skill build not found",
    )


def _invariant(stage: str, message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    return ContractError(
        code="INVARIANT_VIOLATION",
        category=ErrorCategory.INVARIANT,
        retryable=False,
        user_message_key="system.invariant_violation",
        stage=stage,
        message=message,
    )


def _mismatch(message: str) -> Any:
    from yaya_agent_contracts import ContractError, ErrorCategory

    return ContractError(
        code="CONTENT_VERSION_MISMATCH",
        category=ErrorCategory.VALIDATION,
        retryable=False,
        user_message_key="content.version_mismatch",
        stage="POLICY",
        message=message,
    )
