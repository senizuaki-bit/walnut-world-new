"""Pinned Docker Build, artifact publication, and certification workflow."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_build import (
    BuildResourceLimits,
    ContentAddressedArtifactPublisher,
    CppTestCase,
    CppTestSuite,
    DigestPinnedDockerCppBuilder,
    DockerBuildResult,
)
from yaya_agent_contracts import (
    CommandRecord,
    CommandStatus,
    CommandTransition,
    CompileAndTestRequest,
    ContractError,
    ErrorCategory,
    EvidenceRef,
    EvidenceType,
    Failure,
    OperationContext,
    SandboxLimits,
    SkillSourceBundle,
    SkillSourceFile,
    canonical_json_sha256,
)

from walnut_backend.adapters.postgres.certification_authority import (
    artifact_authority_sha256,
    certification_command_authority_sha256,
    certification_provenance_sha256,
    certification_receipt_authority_sha256,
    certification_workflow_job_sha256,
)
from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.models import (
    BuildPolicyRow,
    CommandRow,
    EvidenceRow,
    JobStepReceiptRow,
    LaunchAuthorityRow,
    SkillArtifactRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
    SkillBuildTerminalAuthorityRow,
    SkillCertificationProvenanceRow,
    SkillCertificationRow,
    WorkflowJobRow,
    command_record_from_data,
    error_data,
    request_context_data,
)
from walnut_backend.adapters.postgres.skill_provenance import (
    build_terminal_authority_sha256,
    build_terminal_command_authority_sha256,
    build_terminal_receipt_authority_sha256,
    build_terminal_workflow_authority_sha256,
    validate_build_provenance,
)
from walnut_backend.adapters.postgres.workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
    WorkflowInvariantError,
)
from walnut_backend.certified_skill_schema import (
    CertifiedSkillSchemaError,
    certified_parameter_schema,
    policy_parameter_schema,
)


class BuildInfrastructureRetry(RuntimeError):
    """A retryable Docker control-plane outcome, never a student-code rejection."""


@dataclass(frozen=True, slots=True)
class _BuildAuthority:
    claim: ClaimedWorkflowJob
    command: CommandRecord
    context: OperationContext
    build_id: str
    build_provenance_sha256: str
    skill_id: str
    learner_id: str
    world_id: str
    request: Mapping[str, Any]
    requested_capabilities: tuple[str, ...]
    policy_id: str
    policy_sha256: str
    policy_json: Mapping[str, Any]
    compiler_profile: str
    compiler_version: str
    compiler_image: str
    test_suite_version: str
    source_bundle: SkillSourceBundle
    compile_started_at: datetime
    builder: DigestPinnedDockerCppBuilder
    compile_request: CompileAndTestRequest


class BuildWorkflowHandler:
    """Execute a server-policy Build and certify only exact published bytes."""

    operations = frozenset({"CREATE_SKILL_BUILD"})

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        command_store: PostgresCommandStore,
        workflow_jobs: PostgresWorkflowJobStore,
        workspace_root: Path,
        artifact_root: Path,
        docker_executable: str = "docker",
        lease_seconds: int = 900,
    ) -> None:
        if not 30 <= lease_seconds <= 3600:
            raise ValueError("Build lease_seconds must be between 30 and 3600")
        self._sessions = session_factory
        self._commands = command_store
        self._jobs = workflow_jobs
        self._workspace_root = _existing_directory(workspace_root, "workspace_root")
        self._publisher = ContentAddressedArtifactPublisher(
            _existing_directory(artifact_root, "artifact_root")
        )
        self._docker_executable = docker_executable
        self._lease_seconds = lease_seconds

    async def execute(self, claim: ClaimedWorkflowJob) -> None:
        if claim.operation not in self.operations:
            raise ValueError(f"unsupported Build operation {claim.operation}")
        try:
            authority = await self._prepare(claim)
        except WorkflowInvariantError as error:
            print(f"BUILD_WORKER_INVARIANT_ERROR: {error}", flush=True)
            raise
        result, owned = await self._build_with_heartbeat(authority)
        if result.succeeded:
            if result.staged_artifact is None:
                raise WorkflowInvariantError("successful Build has no staged artifact")
            published = await asyncio.to_thread(self._publisher.publish, result.staged_artifact)
            if published.artifact_sha256 != result.artifact_sha256:
                raise WorkflowInvariantError("published Artifact digest differs from Build")
            await self._finish_success(authority, owned, result, published)
        else:
            if result.failure is None:
                raise WorkflowInvariantError("failed Build has no failure authority")
            if result.failure.retryable:
                raise BuildInfrastructureRetry(f"{result.failure.stage}:{result.failure.code}")
            await self._finish_rejected(authority, owned, result)

    async def _prepare(self, claim: ClaimedWorkflowJob) -> _BuildAuthority:
        async with self._sessions() as session, session.begin():
            owned = await self._jobs.start_step_in_session(
                session,
                claim,
                phase="POLICY",
                lease_seconds=self._lease_seconds,
            )
            command = await _command(session, owned)
            context = _operation_context(command)
            row = await session.scalar(
                select(SkillBuildRow)
                .where(
                    SkillBuildRow.tenant_id == owned.tenant_id,
                    SkillBuildRow.build_id == owned.subject_id,
                    SkillBuildRow.actor_id == command.request_context.actor.actor_id,
                    SkillBuildRow.command_id == command.command_id,
                )
                .with_for_update()
            )
            if row is None:
                raise WorkflowInvariantError("Build resource disappeared")
            provenance = await session.scalar(
                select(SkillBuildProvenanceRow).where(
                    SkillBuildProvenanceRow.build_id == row.build_id,
                    SkillBuildProvenanceRow.tenant_id == row.tenant_id,
                    SkillBuildProvenanceRow.actor_id == row.actor_id,
                )
            )
            if provenance is None or not await validate_build_provenance(
                session, provenance
            ):
                raise WorkflowInvariantError("Build provenance is missing or corrupt")
            job = _object(owned.job, "job")
            if job.get("build_provenance_sha256") != provenance.authority_sha256:
                raise WorkflowInvariantError("Build job provenance authority drifted")
            request = _object(job.get("request"), "job.request")
            if job.get("build_id") != row.build_id or request != row.request_json:
                raise WorkflowInvariantError("Build job/request bytes drifted")
            authority = await session.scalar(
                select(LaunchAuthorityRow).where(
                    LaunchAuthorityRow.tenant_id == owned.tenant_id,
                    LaunchAuthorityRow.actor_id == command.request_context.actor.actor_id,
                    LaunchAuthorityRow.content_hash
                    == command.request_context.content_ref.content_hash,
                    LaunchAuthorityRow.active.is_(True),
                )
            )
            if authority is None:
                raise WorkflowInvariantError("Build has no active launch authority")
            policy = await session.scalar(
                select(BuildPolicyRow).where(
                    BuildPolicyRow.tenant_id == owned.tenant_id,
                    BuildPolicyRow.build_policy_id == authority.build_policy_id,
                    BuildPolicyRow.actor_id == authority.actor_id,
                    BuildPolicyRow.content_hash == authority.content_hash,
                    BuildPolicyRow.active.is_(True),
                )
            )
            if policy is None:
                raise WorkflowInvariantError("launch authority Build policy is missing")
            parsed = _parse_policy(policy)
            requested = _string_tuple(
                request.get("requested_capabilities", []), "requested_capabilities"
            )
            allowed = tuple(policy.allowed_capabilities)
            if len(set(requested)) != len(requested) or any(
                item not in allowed for item in requested
            ):
                raise WorkflowInvariantError("Build requested capabilities violate policy")
            if (
                request.get("compiler_profile") != policy.compiler_profile
                or request.get("test_suite_version") != policy.test_suite_version
            ):
                raise WorkflowInvariantError("Build request differs from server policy")
            source = _source_bundle(request.get("source_bundle"))
            limits = parsed["limits"]
            compile_request = CompileAndTestRequest(
                build_id=row.build_id,
                skill_id=row.skill_id,
                source_bundle=source,
                compiler_profile=policy.compiler_profile,
                test_suite_version=policy.test_suite_version,
                limits=SandboxLimits(
                    cpu_ms=limits.compile_wall_ms,
                    wall_ms=limits.compile_wall_ms,
                    memory_bytes=limits.memory_bytes,
                    max_intents=1,
                    max_output_bytes=limits.max_output_bytes,
                    max_processes=limits.max_processes,
                    network_access=False,
                ),
            )
            builder = DigestPinnedDockerCppBuilder(
                self._workspace_root,
                image=parsed["compiler_image"],
                compiler_version=policy.compiler_version,
                test_suites=(parsed["test_suite"],),
                docker_executable=self._docker_executable,
                limits=limits,
            )
            now = await _database_now(session)
            current = command
            if current.status is CommandStatus.ACCEPTED:
                validating = replace(
                    current,
                    status=CommandStatus.VALIDATING,
                    stage="POLICY",
                    revision=current.revision + 1,
                    updated_at=now,
                )
                transitioned = await self._commands.transition_in_session(
                    session, CommandTransition(current, validating), context
                )
                if isinstance(transitioned, Failure):
                    raise WorkflowInvariantError("Build Command policy CAS was lost")
                current = transitioned.value
            elif current.status is not CommandStatus.VALIDATING:
                raise WorkflowInvariantError("Build Command is not recoverable")
            build = dict(row.build_json)
            started = _compile_start(build, now)
            build.update(
                {
                    "status": "COMPILING",
                    "terminal": False,
                    "updated_at": _iso(now),
                    "phases": _running_phases(started),
                    "failure": None,
                }
            )
            row.status = "COMPILING"
            row.terminal = False
            row.updated_at = now
            row.build_json = build
            owned = await self._jobs.start_step_in_session(
                session,
                owned,
                phase="COMPILE",
                lease_seconds=self._lease_seconds,
            )
            return _BuildAuthority(
                claim=owned,
                command=current,
                context=context,
                build_id=row.build_id,
                build_provenance_sha256=provenance.authority_sha256,
                skill_id=row.skill_id,
                learner_id=authority.learner_id,
                world_id=authority.world_id,
                request=request,
                requested_capabilities=requested,
                policy_id=policy.build_policy_id,
                policy_sha256=policy.policy_sha256,
                policy_json=parsed["policy_json"],
                compiler_profile=policy.compiler_profile,
                compiler_version=policy.compiler_version,
                compiler_image=parsed["compiler_image"],
                test_suite_version=policy.test_suite_version,
                source_bundle=source,
                compile_started_at=started,
                builder=builder,
                compile_request=compile_request,
            )

    async def _build_with_heartbeat(
        self, authority: _BuildAuthority
    ) -> tuple[DockerBuildResult, ClaimedWorkflowJob]:
        task = asyncio.create_task(
            asyncio.to_thread(authority.builder.build, authority.compile_request)
        )
        claim = authority.claim
        heartbeat_seconds = max(10.0, min(30.0, self._lease_seconds / 3))
        while True:
            done, _pending = await asyncio.wait({task}, timeout=heartbeat_seconds)
            if task in done:
                return task.result(), claim
            claim = await self._jobs.renew(claim, lease_seconds=self._lease_seconds)

    async def _finish_success(
        self,
        authority: _BuildAuthority,
        claim: ClaimedWorkflowJob,
        result: DockerBuildResult,
        published: Any,
    ) -> None:
        if (
            result.source_sha256 is None
            or result.artifact_sha256 is None
            or result.build_identity is None
        ):
            raise WorkflowInvariantError("successful Build closure is incomplete")
        async with self._sessions() as session, session.begin():
            row = await _build_row(session, authority, claim)
            command = await _command(session, claim)
            now = await _database_now(session)
            skill_version_id = _identifier("skillver", authority.build_id, result.artifact_sha256)
            certification_id = _identifier("cert", authority.build_id, result.artifact_sha256)
            certified_schema, certified_schema_sha256 = certified_parameter_schema(
                authority.policy_json,
                policy_sha256=authority.policy_sha256,
                build_id=authority.build_id,
                skill_id=authority.skill_id,
                skill_version_id=skill_version_id,
                source_sha256=result.source_sha256,
                artifact_sha256=result.artifact_sha256,
                certification_id=certification_id,
                build_policy_id=authority.policy_id,
                actor_id=command.request_context.actor.actor_id,
                content_hash=command.request_context.content_ref.content_hash,
                capabilities=authority.requested_capabilities,
            )
            evidence_id = _identifier("evidence", "build", authority.build_id)
            evidence_payload = {
                "evidence_kind": "BUILD_CERTIFICATION",
                "build_id": authority.build_id,
                "skill_id": authority.skill_id,
                "skill_version_id": skill_version_id,
                "artifact_sha256": result.artifact_sha256,
                "test_suite_version": authority.test_suite_version,
                "outcome": "CERTIFIED",
            }
            evidence_sha256 = canonical_json_sha256(evidence_payload)
            evidence_ref_wire = {
                "evidence_id": evidence_id,
                "evidence_type": "TEST_REPORT",
                "created_at": _iso(now),
                "sha256": evidence_sha256,
                "uri": f"/v1/evidence/{evidence_id}",
            }
            versions = dict(cast(Mapping[str, Any], row.build_json["versions"]))
            versions.update(
                {
                    "policy_version": authority.policy_id,
                    "skill_version": skill_version_id,
                    "artifact_sha256": result.artifact_sha256,
                    "compiler_version": authority.compiler_version,
                    "sandbox_image_digest": authority.compiler_image,
                    "test_suite_version": authority.test_suite_version,
                }
            )
            evidence = {
                "request_context": request_context_data(command.request_context),
                "evidence_ref": evidence_ref_wire,
                "subject": {"learner_id": authority.learner_id},
                "source": {
                    "source_type": "SKILL_BUILD",
                    "source_id": authority.build_id,
                    "command_id": command.command_id,
                    "world_id": authority.world_id,
                },
                "occurred_at": _iso(now),
                "recorded_at": _iso(now),
                "integrity": {
                    "payload_sha256": evidence_sha256,
                    "previous_evidence_sha256": None,
                },
                "payload": evidence_payload,
                "related_evidence": [],
                "versions": versions,
            }
            artifact_metadata = {
                "schema_version": "1.0.0",
                "artifact_sha256": result.artifact_sha256,
                "source_sha256": result.source_sha256,
                "build_identity": result.build_identity,
                "size_bytes": published.size_bytes,
                "compiler_profile": authority.compiler_profile,
                "compiler_version": authority.compiler_version,
                "compiler_image": authority.compiler_image,
                "test_suite_version": authority.test_suite_version,
                "policy_sha256": authority.policy_sha256,
                "parameter_schema": certified_schema,
                "parameter_schema_sha256": certified_schema_sha256,
            }
            certification = {
                "schema_version": "1.0.0",
                "certification_id": certification_id,
                "build_id": authority.build_id,
                "skill_id": authority.skill_id,
                "skill_version_id": skill_version_id,
                "artifact_sha256": result.artifact_sha256,
                "source_sha256": result.source_sha256,
                "actor_id": command.request_context.actor.actor_id,
                "content_hash": command.request_context.content_ref.content_hash,
                "build_policy_id": authority.policy_id,
                "policy_sha256": authority.policy_sha256,
                "capabilities": list(authority.requested_capabilities),
                "issued_at": _iso(now),
                "parameter_schema": certified_schema,
                "parameter_schema_sha256": certified_schema_sha256,
            }
            artifact_row = SkillArtifactRow(
                    tenant_id=claim.tenant_id,
                    artifact_sha256=result.artifact_sha256,
                    build_id=authority.build_id,
                    actor_id=command.request_context.actor.actor_id,
                    content_hash=command.request_context.content_ref.content_hash,
                    skill_id=authority.skill_id,
                    source_sha256=result.source_sha256,
                    artifact_uri=published.artifact_uri,
                    metadata_json=artifact_metadata,
                    created_at=now,
                )
            session.add(artifact_row)
            certification_row = SkillCertificationRow(
                    certification_id=certification_id,
                    tenant_id=claim.tenant_id,
                    build_id=authority.build_id,
                    skill_id=authority.skill_id,
                    skill_version_id=skill_version_id,
                    artifact_sha256=result.artifact_sha256,
                    actor_id=command.request_context.actor.actor_id,
                    content_hash=command.request_context.content_ref.content_hash,
                    certification_sha256=canonical_json_sha256(certification),
                    certification_json=certification,
                    certified_at=now,
                )
            session.add(certification_row)
            session.add(
                EvidenceRow(
                    evidence_id=evidence_id,
                    tenant_id=claim.tenant_id,
                    actor_id=command.request_context.actor.actor_id,
                    content_hash=command.request_context.content_ref.content_hash,
                    command_id=command.command_id,
                    recorded_at=now,
                    evidence_json=evidence,
                )
            )
            build = dict(row.build_json)
            build.update(
                {
                    "skill_version_id": skill_version_id,
                    "status": "CERTIFIED",
                    "terminal": True,
                    "updated_at": _iso(now),
                    "artifact": {
                        "artifact_sha256": result.artifact_sha256,
                        "source_sha256": result.source_sha256,
                        "compiler_profile": authority.compiler_profile,
                        "compiler_version": authority.compiler_version,
                        "test_suite_version": authority.test_suite_version,
                    },
                    "certification": {
                        "certification_id": certification_id,
                        "issued_at": _iso(now),
                        "capabilities": list(authority.requested_capabilities),
                    },
                    "phases": _terminal_phases(authority.compile_started_at, now, None, ()),
                    "failure": None,
                    "evidence_refs": [evidence_ref_wire],
                    "versions": versions,
                }
            )
            row.status = "CERTIFIED"
            row.terminal = True
            row.updated_at = now
            row.build_json = build
            await session.flush()
            build_receipt = await self._jobs.record_step_in_session(
                session,
                claim,
                step_name="BUILD_CERTIFIED",
                input_sha256=claim.request_sha256,
                output={
                    "build_id": authority.build_id,
                    "skill_version_id": skill_version_id,
                    "artifact_sha256": result.artifact_sha256,
                    "certification_id": certification_id,
                    "evidence_id": evidence_id,
                    "build_identity": result.build_identity,
                },
            )
            reference = EvidenceRef(
                evidence_id=evidence_id,
                evidence_type=EvidenceType.TEST_REPORT,
                created_at=now,
                sha256=evidence_sha256,
                uri=f"/v1/evidence/{evidence_id}",
            )
            applied = replace(
                command,
                status=CommandStatus.APPLIED,
                stage="COMPLETE",
                terminal=True,
                result={
                    "result_type": "RESOURCE_CREATED",
                    "resource_type": "SKILL_BUILD",
                    "resource_id": authority.build_id,
                    "resource_url": f"/v1/skill-builds/{authority.build_id}",
                },
                error=None,
                evidence_refs=(reference,),
                revision=command.revision + 1,
                updated_at=now,
            )
            transitioned = await self._commands.transition_in_session(
                session,
                CommandTransition(command, applied),
                _operation_context(command),
            )
            if isinstance(transitioned, Failure):
                raise WorkflowInvariantError("Build Command completion CAS was lost")
            await self._jobs.finish_in_session(session, claim, status="SUCCEEDED")
            terminal_command = await session.scalar(
                select(CommandRow).where(
                    CommandRow.tenant_id == claim.tenant_id,
                    CommandRow.command_id == claim.command_id,
                )
            )
            terminal_job = await session.scalar(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == claim.tenant_id,
                    WorkflowJobRow.job_id == claim.job_id,
                )
            )
            receipt_row = await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == claim.tenant_id,
                    JobStepReceiptRow.receipt_id == build_receipt.receipt_id,
                )
            )
            if terminal_command is None or terminal_job is None or receipt_row is None:
                raise WorkflowInvariantError(
                    "Certification terminal workflow authority did not materialize"
                )
            certification_provenance = SkillCertificationProvenanceRow(
                certification_id=certification_id,
                tenant_id=claim.tenant_id,
                actor_id=command.request_context.actor.actor_id,
                build_id=authority.build_id,
                build_authority_sha256=authority.build_provenance_sha256,
                build_request_sha256=canonical_json_sha256(row.request_json),
                workflow_job_id=terminal_job.job_id,
                workflow_request_sha256=terminal_job.request_sha256,
                workflow_job_sha256=certification_workflow_job_sha256(terminal_job),
                command_authority_sha256=certification_command_authority_sha256(
                    terminal_command
                ),
                build_receipt_id=receipt_row.receipt_id,
                build_receipt_sha256=receipt_row.output_sha256,
                build_receipt_authority_sha256=(
                    certification_receipt_authority_sha256(receipt_row)
                ),
                policy_sha256=authority.policy_sha256,
                artifact_sha256=result.artifact_sha256,
                artifact_authority_sha256=artifact_authority_sha256(artifact_row),
                certification_sha256=certification_row.certification_sha256,
                authority_sha256="0" * 64,
                created_at=now,
            )
            certification_provenance.authority_sha256 = (
                certification_provenance_sha256(certification_provenance)
            )
            session.add(certification_provenance)
            # The terminal seal has a composite FK to this exact Certification
            # authority.  Materialize the parent before adding the child so the
            # write order does not depend on ORM unit-of-work inference.
            await session.flush()
            session.add(
                _build_terminal_authority_row(
                    build=row,
                    build_authority_sha256=authority.build_provenance_sha256,
                    command=terminal_command,
                    workflow=terminal_job,
                    receipt=receipt_row,
                    certification_id=certification_id,
                    certification_authority_sha256=(
                        certification_provenance.authority_sha256
                    ),
                    created_at=now,
                )
            )

    async def _finish_rejected(
        self,
        authority: _BuildAuthority,
        claim: ClaimedWorkflowJob,
        result: DockerBuildResult,
    ) -> None:
        if result.failure is None:
            raise WorkflowInvariantError("failed Build has no failure closure")
        async with self._sessions() as session, session.begin():
            row = await _build_row(session, authority, claim)
            command = await _command(session, claim)
            now = await _database_now(session)
            diagnostic_codes = tuple(sorted({item.code for item in result.failure.diagnostics}))
            build_error, command_error = _build_errors(
                result.failure.stage,
                result.failure.code,
                diagnostic_codes,
            )
            build = dict(row.build_json)
            build.update(
                {
                    "status": "REJECTED",
                    "terminal": True,
                    "updated_at": _iso(now),
                    "phases": _terminal_phases(
                        authority.compile_started_at,
                        now,
                        result.failure.stage,
                        diagnostic_codes,
                    ),
                    "failure": error_data(build_error),
                }
            )
            row.status = "REJECTED"
            row.terminal = True
            row.updated_at = now
            row.build_json = build

            # A rejected Build is teaching evidence, not just a failed job. The
            # certified path has always recorded Evidence; the rejected path
            # recorded none, so a learner stuck on compiler errors produced no
            # evidence at all -- the pedagogy policy saw failure_count 0, could
            # never leave REVIEW/HEURISTIC, and 叮当 kept re-asking the same
            # opening question because it had no failure to talk about.
            #
            # This Evidence owns no Run, and does not need one: a compile
            # rejection is settled by the Build's own terminal authority, which
            # this transaction has just written.
            evidence_id = _identifier("evidence", "buildreject", authority.build_id)
            evidence_payload = {
                "evidence_kind": "BUILD_REJECTION",
                "build_id": authority.build_id,
                "skill_id": authority.skill_id,
                "test_suite_version": authority.test_suite_version,
                "outcome": "REJECTED",
                "failure_stage": result.failure.stage,
                "failure_code": result.failure.code,
                "diagnostic_codes": list(diagnostic_codes),
            }
            evidence_sha256 = canonical_json_sha256(evidence_payload)
            versions = dict(cast(Mapping[str, Any], row.build_json["versions"]))
            versions.update(
                {
                    "policy_version": authority.policy_id,
                    "compiler_version": authority.compiler_version,
                    "sandbox_image_digest": authority.compiler_image,
                    "test_suite_version": authority.test_suite_version,
                }
            )
            session.add(
                EvidenceRow(
                    evidence_id=evidence_id,
                    tenant_id=claim.tenant_id,
                    actor_id=command.request_context.actor.actor_id,
                    content_hash=command.request_context.content_ref.content_hash,
                    command_id=command.command_id,
                    recorded_at=now,
                    evidence_json={
                        "request_context": request_context_data(command.request_context),
                        "evidence_ref": {
                            "evidence_id": evidence_id,
                            "evidence_type": "TEST_REPORT",
                            "created_at": _iso(now),
                            "sha256": evidence_sha256,
                            "uri": f"/v1/evidence/{evidence_id}",
                        },
                        "subject": {"learner_id": authority.learner_id},
                        "source": {
                            "source_type": "SKILL_BUILD",
                            "source_id": authority.build_id,
                            "command_id": command.command_id,
                            "world_id": authority.world_id,
                        },
                        "occurred_at": _iso(now),
                        "recorded_at": _iso(now),
                        "integrity": {
                            "payload_sha256": evidence_sha256,
                            "previous_evidence_sha256": None,
                        },
                        "payload": evidence_payload,
                        "related_evidence": [],
                        "versions": versions,
                    },
                )
            )
            rejected_receipt = await self._jobs.record_step_in_session(
                session,
                claim,
                step_name="BUILD_REJECTED",
                input_sha256=claim.request_sha256,
                output={
                    "build_id": authority.build_id,
                    "evidence_id": evidence_id,
                    "failure_code": result.failure.code,
                    "failure_stage": result.failure.stage,
                    "diagnostic_codes": list(diagnostic_codes),
                    "source_sha256": result.source_sha256,
                    "build_identity": result.build_identity,
                },
            )
            rejected = replace(
                command,
                status=CommandStatus.REJECTED,
                stage="VALIDATE",
                terminal=True,
                result=None,
                error=command_error,
                revision=command.revision + 1,
                updated_at=now,
            )
            transitioned = await self._commands.transition_in_session(
                session,
                CommandTransition(command, rejected),
                _operation_context(command),
            )
            if isinstance(transitioned, Failure):
                raise WorkflowInvariantError("Build rejection Command CAS was lost")
            await self._jobs.finish_in_session(
                session,
                claim,
                status="FAILED",
                phase=result.failure.stage,
                error=cast(dict[str, Any], error_data(build_error)),
            )
            terminal_command = await session.scalar(
                select(CommandRow).where(
                    CommandRow.tenant_id == claim.tenant_id,
                    CommandRow.command_id == claim.command_id,
                )
            )
            terminal_job = await session.scalar(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == claim.tenant_id,
                    WorkflowJobRow.job_id == claim.job_id,
                )
            )
            receipt_row = await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == claim.tenant_id,
                    JobStepReceiptRow.receipt_id == rejected_receipt.receipt_id,
                )
            )
            if terminal_command is None or terminal_job is None or receipt_row is None:
                raise WorkflowInvariantError(
                    "rejected Build terminal authority did not materialize"
                )
            session.add(
                _build_terminal_authority_row(
                    build=row,
                    build_authority_sha256=authority.build_provenance_sha256,
                    command=terminal_command,
                    workflow=terminal_job,
                    receipt=receipt_row,
                    certification_id=None,
                    certification_authority_sha256=None,
                    created_at=now,
                )
            )


def _build_terminal_authority_row(
    *,
    build: SkillBuildRow,
    build_authority_sha256: str,
    command: CommandRow,
    workflow: WorkflowJobRow,
    receipt: JobStepReceiptRow,
    certification_id: str | None,
    certification_authority_sha256: str | None,
    created_at: datetime,
) -> SkillBuildTerminalAuthorityRow:
    row = SkillBuildTerminalAuthorityRow(
        build_id=build.build_id,
        tenant_id=build.tenant_id,
        actor_id=build.actor_id,
        build_authority_sha256=build_authority_sha256,
        terminal_status=build.status,
        command_id=command.command_id,
        command_authority_sha256=build_terminal_command_authority_sha256(command),
        workflow_job_id=workflow.job_id,
        workflow_job_sha256=build_terminal_workflow_authority_sha256(workflow),
        terminal_receipt_id=receipt.receipt_id,
        terminal_receipt_authority_sha256=(
            build_terminal_receipt_authority_sha256(receipt)
        ),
        certification_id=certification_id,
        certification_authority_sha256=certification_authority_sha256,
        authority_sha256="0" * 64,
        created_at=created_at,
    )
    row.authority_sha256 = build_terminal_authority_sha256(row)
    return row


def _parse_policy(policy: BuildPolicyRow) -> dict[str, Any]:
    value = _object(policy.policy_json, "Build policy")
    expected = {
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
    if set(value) != expected or value.get("schema_version") != "1.0.0":
        raise WorkflowInvariantError("Build policy is not a closed v1 object")
    if canonical_json_sha256(value) != policy.policy_sha256:
        raise WorkflowInvariantError("Build policy hash drifted")
    try:
        policy_parameter_schema(value, policy_sha256=policy.policy_sha256)
    except CertifiedSkillSchemaError as error:
        raise WorkflowInvariantError(str(error)) from error
    compiler_image = value.get("compiler_image")
    if (
        not isinstance(compiler_image, str)
        or not compiler_image.endswith(f"@{policy.sandbox_image_digest}")
        or value.get("compiler_profile") != policy.compiler_profile
        or value.get("compiler_version") != policy.compiler_version
        or value.get("test_suite_version") != policy.test_suite_version
    ):
        raise WorkflowInvariantError("Build policy columns drifted from policy bytes")
    from yaya_agent_build import CPP20_SAFE_V1_FLAGS

    if _string_tuple(value.get("compile_flags"), "compile_flags") != CPP20_SAFE_V1_FLAGS:
        raise WorkflowInvariantError("Build compile flags differ from the certified profile")
    limits_value = _object(value.get("limits"), "Build limits")
    if set(limits_value) != {
        "compile_wall_ms",
        "test_wall_ms",
        "memory_bytes",
        "max_processes",
        "cpu_millis",
        "tmpfs_bytes",
        "max_output_bytes",
        "max_artifact_bytes",
    }:
        raise WorkflowInvariantError("Build limits are not closed")
    limits = BuildResourceLimits(
        compile_wall_ms=_integer(limits_value, "compile_wall_ms"),
        test_wall_ms=_integer(limits_value, "test_wall_ms"),
        memory_bytes=_integer(limits_value, "memory_bytes"),
        max_processes=_integer(limits_value, "max_processes"),
        cpus=_integer(limits_value, "cpu_millis") / 1000,
        tmpfs_bytes=_integer(limits_value, "tmpfs_bytes"),
        max_output_bytes=_integer(limits_value, "max_output_bytes"),
        max_artifact_bytes=_integer(limits_value, "max_artifact_bytes"),
    )
    suite = CppTestSuite(
        version=policy.test_suite_version,
        public_tests=_tests(value.get("public_tests"), "PUBLIC"),
        hidden_tests=_tests(value.get("hidden_tests"), "HIDDEN"),
    )
    return {
        "compiler_image": compiler_image,
        "limits": limits,
        "test_suite": suite,
        "policy_json": value,
    }


def _tests(value: object, visibility: Literal["PUBLIC", "HIDDEN"]) -> tuple[CppTestCase, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise WorkflowInvariantError(f"{visibility} tests must be an array")
    result: list[CppTestCase] = []
    for raw in value:
        item = _object(raw, f"{visibility} test")
        if (
            set(item)
            != {
                "test_case_id",
                "visibility",
                "arguments",
                "stdin_base64",
                "expected_stdout_sha256",
            }
            or item.get("visibility") != visibility
        ):
            raise WorkflowInvariantError(f"{visibility} test is not closed")
        encoded = item.get("stdin_base64")
        if not isinstance(encoded, str):
            raise WorkflowInvariantError("test stdin is not base64 text")
        try:
            stdin = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise WorkflowInvariantError("test stdin base64 is invalid") from error
        expected = item.get("expected_stdout_sha256")
        if expected is not None and not isinstance(expected, str):
            raise WorkflowInvariantError("test stdout hash is invalid")
        result.append(
            CppTestCase(
                test_case_id=cast(str, item.get("test_case_id")),
                visibility=visibility,
                arguments=_string_tuple(item.get("arguments"), "test arguments"),
                stdin=stdin,
                expected_stdout_sha256=expected,
            )
        )
    return tuple(result)


def _source_bundle(value: object) -> SkillSourceBundle:
    item = _object(value, "source_bundle")
    files_value = item.get("files")
    if isinstance(files_value, str | bytes | bytearray) or not isinstance(files_value, Sequence):
        raise WorkflowInvariantError("source_bundle.files is not an array")
    files = tuple(
        SkillSourceFile(
            path=cast(str, source["path"]),
            content=cast(str, source["content"]),
            content_sha256=cast(str, source["content_sha256"]),
        )
        for source in (_object(raw, "source file") for raw in files_value)
    )
    return SkillSourceBundle(
        language=cast(Literal["CPP20"], item.get("language")),
        entrypoint=cast(str, item.get("entrypoint")),
        files=files,
    )


async def _build_row(
    session: AsyncSession,
    authority: _BuildAuthority,
    claim: ClaimedWorkflowJob,
) -> SkillBuildRow:
    row = await session.scalar(
        select(SkillBuildRow)
        .where(
            SkillBuildRow.tenant_id == claim.tenant_id,
            SkillBuildRow.build_id == authority.build_id,
            SkillBuildRow.command_id == claim.command_id,
        )
        .with_for_update()
    )
    if row is None or row.status != "COMPILING" or row.terminal:
        raise WorkflowInvariantError("Build resource is not finalizable")
    provenance = await session.scalar(
        select(SkillBuildProvenanceRow).where(
            SkillBuildProvenanceRow.build_id == row.build_id,
            SkillBuildProvenanceRow.tenant_id == row.tenant_id,
            SkillBuildProvenanceRow.actor_id == row.actor_id,
        )
    )
    if (
        provenance is None
        or provenance.authority_sha256 != authority.build_provenance_sha256
        or not await validate_build_provenance(session, provenance)
    ):
        raise WorkflowInvariantError("Build provenance drifted before finalization")
    return row


async def _command(session: AsyncSession, claim: ClaimedWorkflowJob) -> CommandRecord:
    row = await session.scalar(
        select(CommandRow)
        .where(
            CommandRow.tenant_id == claim.tenant_id,
            CommandRow.command_id == claim.command_id,
        )
        .with_for_update()
    )
    if row is None:
        raise WorkflowInvariantError("Build Command disappeared")
    command = command_record_from_data(row.record_json)
    if command.command_type != claim.operation or command.terminal:
        raise WorkflowInvariantError("Build Command identity or state drifted")
    return command


def _operation_context(command: CommandRecord) -> OperationContext:
    origin = command.request_context
    return OperationContext(
        request_id=origin.request_id,
        correlation_id=origin.correlation_id,
        trace_id=origin.trace_id,
        requested_at=origin.requested_at,
        actor=origin.actor,
        content_ref=origin.content_ref,
        schema_version=origin.schema_version,
        command_id=command.command_id,
        causation_id=None,
        deadline_at=None,
    )


def _build_errors(
    stage: str, code: str, diagnostics: tuple[str, ...]
) -> tuple[ContractError, ContractError]:
    if stage == "VALIDATE_SOURCE":
        contract_code = "INVALID_REQUEST"
        category = ErrorCategory.VALIDATION
        key = "request.invalid"
    elif "TIMEOUT" in code or "LIMIT" in code or "OOM" in code:
        contract_code = "SANDBOX_RESOURCE_LIMIT"
        category = ErrorCategory.SANDBOX
        key = "sandbox.resource_limit"
    elif stage == "COMPILE":
        contract_code = "SANDBOX_COMPILE_ERROR"
        category = ErrorCategory.SANDBOX
        key = "sandbox.compile_error"
    else:
        contract_code = "SANDBOX_RUNTIME_ERROR"
        category = ErrorCategory.SANDBOX
        key = "sandbox.runtime_error"
    details = {"diagnostic_codes": list(diagnostics), "pipeline_code": code}
    build_error = ContractError(
        contract_code,
        category,
        False,
        key,
        stage,
        "Skill Build did not satisfy the server certification policy.",
        details,
    )
    command_error = replace(build_error, stage="VALIDATE")
    return build_error, command_error


def _running_phases(started: datetime) -> list[dict[str, Any]]:
    return [
        {
            "name": phase,
            "status": "RUNNING" if phase == "VALIDATE_SOURCE" else "PENDING",
            "started_at": _iso(started) if phase == "VALIDATE_SOURCE" else None,
            "finished_at": None,
            "diagnostic_codes": [],
        }
        for phase in (
            "VALIDATE_SOURCE",
            "COMPILE",
            "PUBLIC_TEST",
            "HIDDEN_TEST",
            "CERTIFY",
        )
    ]


def _terminal_phases(
    started: datetime,
    finished: datetime,
    failed_stage: str | None,
    diagnostics: tuple[str, ...],
) -> list[dict[str, Any]]:
    phases = (
        "VALIDATE_SOURCE",
        "COMPILE",
        "PUBLIC_TEST",
        "HIDDEN_TEST",
        "CERTIFY",
    )
    failure_index = phases.index(failed_stage) if failed_stage in phases else None
    result: list[dict[str, Any]] = []
    for index, phase in enumerate(phases):
        if failure_index is None or index < failure_index:
            status = "PASSED"
            phase_started: str | None = _iso(started)
            phase_finished: str | None = _iso(finished)
            codes: list[str] = []
        elif index == failure_index:
            status = "FAILED"
            phase_started = _iso(started)
            phase_finished = _iso(finished)
            codes = list(diagnostics) or ["BUILD_POLICY_REJECTED"]
        else:
            status = "SKIPPED"
            phase_started = None
            phase_finished = None
            codes = []
        result.append(
            {
                "name": phase,
                "status": status,
                "started_at": phase_started,
                "finished_at": phase_finished,
                "diagnostic_codes": codes,
            }
        )
    return result


def _compile_start(build: Mapping[str, Any], fallback: datetime) -> datetime:
    for raw in cast(Sequence[object], build.get("phases", [])):
        phase = _object(raw, "Build phase")
        if phase.get("name") == "VALIDATE_SOURCE" and isinstance(phase.get("started_at"), str):
            parsed = datetime.fromisoformat(cast(str, phase["started_at"]).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
    return fallback


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowInvariantError(f"{label} must be an object")
    return dict(value)


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise WorkflowInvariantError(f"{label} must be an array")
    if any(not isinstance(item, str) for item in value):
        raise WorkflowInvariantError(f"{label} must contain strings")
    return tuple(cast(Sequence[str], value))


def _integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or item < 1:
        raise WorkflowInvariantError(f"Build policy {key} must be positive")
    return item


def _identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join((prefix, *parts)).encode()).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _existing_directory(path: Path, name: str) -> Path:
    resolved = path.expanduser().resolve()
    if path.is_symlink() or not resolved.is_dir():
        raise ValueError(f"{name} must be an existing non-symlink directory")
    return resolved


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkflowInvariantError("PostgreSQL returned an invalid timestamp")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["BuildWorkflowHandler"]
