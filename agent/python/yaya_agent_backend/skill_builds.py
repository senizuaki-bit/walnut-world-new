"""Durable PostgreSQL integration for the pinned student Skill Build pipeline.

The Docker adapter owns external execution and filesystem receipts.  This
module owns the other half of the production boundary: policy authority,
worker heartbeats, immutable database receipts, content-addressed publication,
Certification/SkillVersion/Evidence closure, and atomic Command finalization.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import psycopg
from jsonschema.validators import validator_for
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from yaya_agent_build import (
    CPP20_SAFE_V1_FLAGS,
    CPP20_SAFE_V1_PROFILE,
    ArtifactIntegrityError,
    ArtifactPublicationError,
    BuildDiagnostic,
    BuildResourceLimits,
    ContentAddressedArtifactPublisher,
    CppTestCase,
    CppTestSuite,
    DigestPinnedDockerCppBuilder,
    DockerBuildFailure,
    DockerBuildResult,
    PublishedArtifact,
    canonical_source_bundle_sha256,
)
from yaya_agent_contracts import (
    BuildArtifact,
    CertifiedSkill,
    CommandRecord,
    CommandStatus,
    CompileAndTestRequest,
    ContractError,
    ErrorCategory,
    EvidenceRef,
    EvidenceType,
    FrozenJsonObject,
    SandboxLimits,
    SkillRef,
    SkillSourceBundle,
    SkillSourceFile,
    VersionSet,
    canonical_json_sha256,
)
from yaya_agent_runtime import CompileResultSnapshot, SkillSnapshot

from .application import BackendApplicationError
from .codec import decode_as, encode, plain
from .database import PostgresCommitStateUnknown, PostgresDatabase
from .student_skill_chain import BuildJobClaim, StudentSkillChainWorker
from .wire import ContractSchemaValidator

_PHASES = ("VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST", "CERTIFY")
_SUPPORTED_RUNTIME_ABI = "yaya-skill-json-stdio-v1"
_ARTIFACT_URI_PREFIX = "artifact://sha256/"
_PINNED_IMAGE = re.compile(r"^[a-z0-9./:_-]+@sha256:[a-f0-9]{64}$")
_RESOURCE_LIMIT_CODES = frozenset(
    {
        "COMPILE_MEMORY_LIMIT",
        "COMPILE_OUTPUT_LIMIT",
        "COMPILE_TIMEOUT",
        "HIDDEN_TEST_MEMORY_LIMIT",
        "HIDDEN_TEST_OUTPUT_LIMIT",
        "HIDDEN_TEST_TIMEOUT",
        "PUBLIC_TEST_MEMORY_LIMIT",
        "PUBLIC_TEST_OUTPUT_LIMIT",
        "PUBLIC_TEST_TIMEOUT",
        "STAGED_ARTIFACT_SIZE_LIMIT",
    }
)
_USER_REJECTION_CODES = frozenset(
    {
        "COMPILE_ERROR",
        "HIDDEN_TEST_FAILED",
        "HIDDEN_TEST_OUTPUT_MISMATCH",
        "PUBLIC_TEST_FAILED",
        "PUBLIC_TEST_OUTPUT_MISMATCH",
    }
)
_DEPENDENCY_CODES = frozenset(
    {
        "ARTIFACT_COPY_FAILED",
        "COMPILER_PROBE_FAILED",
        "COMPILER_IMAGE_UNAVAILABLE",
        "COMPILER_IMAGE_INSPECT_FAILED",
        "DOCKER_CLEANUP_FAILED",
        "DOCKER_CONTROL_TIMEOUT",
        "DOCKER_CONTROL_OUTPUT_LIMIT",
        "DOCKER_CREATE_FAILED",
        "DOCKER_CREATE_RECONCILIATION_FAILED",
        "DOCKER_LOG_RECONCILIATION_FAILED",
        "DOCKER_UNAVAILABLE",
        "DOCKER_WAIT_FAILED",
    }
)
_INVARIANT_CODES = frozenset(
    {
        "ARTIFACT_PATH_ESCAPE",
        "BUILD_WORKSPACE_DRIFT",
        "COMPILER_IMAGE_DIGEST_DRIFT",
        "COMPILER_IMAGE_PLATFORM_MISMATCH",
        "COMPILER_VERSION_DRIFT",
        "CONTAINER_IDENTITY_CONFLICT",
        "CONTAINER_INSPECT_INVALID",
        "CONTAINER_STATE_INVALID",
        "CONTAINER_STATE_MISSING",
        "CONTAINER_STATE_UNRECOVERABLE",
        "EXISTING_ARTIFACT_DRIFT",
        "INVALID_ARTIFACT_PATH",
        "INVALID_ARTIFACT_SOURCE",
        "INVALID_BUILD_IDENTITY",
        "PUBLISHED_ARTIFACT_DRIFT",
        "SOURCE_PATH_MATERIALIZATION_COLLISION",
        "SOURCE_PATH_NOT_MATERIALIZABLE",
        "STAGED_ARTIFACT_DRIFT",
        "STAGED_ARTIFACT_MISSING",
        "STEP_RECEIPT_CORRUPT",
        "UNKNOWN_TEST_SUITE_VERSION",
        "UNSUPPORTED_COMPILER_PROFILE",
        "WRITABLE_ARTIFACT",
    }
)


def _identifier(prefix: str, *parts: str) -> str:
    framed = "".join(f"{len(part)}:{part}" for part in parts)
    return f"{prefix}_{hashlib.sha256(framed.encode('utf-8')).hexdigest()[:24]}"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    source = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in source):
        raise ValueError(f"{label} contains a non-string key")
    return {cast(str, key): item for key, item in source.items()}


def _sequence(value: object, label: str) -> list[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be an array")
    return list(cast(Sequence[object], value))


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} does not have the exact production authority fields")


def _context_wire(claim: BuildJobClaim) -> dict[str, object]:
    context = claim.context
    return {
        "schema_version": context.schema_version,
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "requested_at": _iso(context.requested_at),
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
    }


def _version_wire(versions: VersionSet) -> dict[str, object]:
    value = _mapping(plain(versions), "VersionSet")
    return {key: item for key, item in value.items() if item is not None}


def _evidence_wire(reference: EvidenceRef) -> dict[str, object]:
    value: dict[str, object] = {
        "evidence_id": reference.evidence_id,
        "evidence_type": reference.evidence_type.value,
        "created_at": _iso(reference.created_at),
    }
    if reference.sha256 is not None:
        value["sha256"] = reference.sha256
    if reference.uri is not None:
        value["uri"] = reference.uri
    return value


def _backend_invariant(stage: str, message: str) -> BackendApplicationError:
    return BackendApplicationError("INVARIANT_VIOLATION", 500, stage, message)


@dataclass(frozen=True, slots=True)
class _BuildAuthority:
    build_id: str
    skill_id: str
    created_at: str
    compile_started_at: datetime
    client_draft_revision: int
    display_name: str
    source_json: Mapping[str, object]
    source_bundle: SkillSourceBundle
    source_sha256: str
    requested_capabilities: tuple[str, ...]
    approved_capabilities: tuple[str, ...]
    build_policy_id: str
    policy_sha256: str
    compiler_profile: str
    compiler_version: str
    compiler_image: str
    test_suite_version: str
    semantic_version: str
    runtime_abi_version: str
    parameter_schema: FrozenJsonObject
    learner_id: str
    world_id: str
    versions: VersionSet
    test_suite: CppTestSuite
    builder: DigestPinnedDockerCppBuilder
    request: CompileAndTestRequest
    build_identity: str


class PostgresSkillBuildExecutor:
    """Execute and atomically materialize one fenced public Skill Build."""

    def __init__(
        self,
        *,
        database: PostgresDatabase,
        validator: ContractSchemaValidator,
        artifact_root: Path,
        workspace_root: Path,
        runtime_image: str,
        docker_executable: str = "docker",
    ) -> None:
        artifact = artifact_root.expanduser().resolve()
        workspace = workspace_root.expanduser().resolve()
        if not artifact.is_dir() or artifact_root.is_symlink():
            raise ValueError("artifact_root must be an existing non-symlink directory")
        if not workspace.is_dir() or workspace_root.is_symlink():
            raise ValueError("workspace_root must be an existing non-symlink directory")
        if _PINNED_IMAGE.fullmatch(runtime_image) is None:
            raise ValueError("runtime_image must be pinned by an exact sha256 digest")
        self._database = database
        self._validator = validator
        self._artifact_root = artifact
        self._workspace_root = workspace
        self._runtime_image = runtime_image
        self._docker_executable = docker_executable
        self._publisher = ContentAddressedArtifactPublisher(artifact)

    async def execute(
        self,
        claim: BuildJobClaim,
        worker: StudentSkillChainWorker,
    ) -> None:
        try:
            authority = await self._prepare(claim, worker)
        except BackendApplicationError as error:
            await self._finalize_setup_failure(claim, worker, error)
            return
        except (ValueError, TypeError, KeyError, binascii.Error) as error:
            wrapped = _backend_invariant(
                "VALIDATE_SOURCE",
                f"Build authority could not be decoded: {type(error).__name__}",
            )
            await self._finalize_setup_failure(claim, worker, wrapped)
            return
        try:
            result = await self._run_with_heartbeats(authority, claim, worker)
            result_valid = True
            try:
                self._assert_result_closure(authority, result)
            except BackendApplicationError:
                result_valid = False
                trusted_failure = self._result_invariant_failure(authority, result)
                await self._finalize_failure(claim, worker, authority, trusted_failure)
            if result_valid and result.succeeded:
                try:
                    await self._finalize_success(claim, worker, authority, result)
                except ArtifactIntegrityError as error:
                    await self._finalize_external_failure(
                        claim,
                        worker,
                        authority,
                        result,
                        pipeline_code=error.code,
                        contract_code="INVARIANT_VIOLATION",
                        category=ErrorCategory.INVARIANT,
                        message="Artifact publication integrity validation failed",
                    )
                except ArtifactPublicationError as error:
                    await self._finalize_external_failure(
                        claim,
                        worker,
                        authority,
                        result,
                        pipeline_code=error.code,
                        contract_code="INTERNAL_ERROR",
                        category=ErrorCategory.INTERNAL,
                        message="Artifact publication failed",
                    )
                except OSError as error:
                    await self._finalize_external_failure(
                        claim,
                        worker,
                        authority,
                        result,
                        pipeline_code="ARTIFACT_STORAGE_UNAVAILABLE",
                        contract_code="DEPENDENCY_UNAVAILABLE",
                        category=ErrorCategory.DEPENDENCY,
                        message=f"Artifact storage failed: {type(error).__name__}",
                        retryable=True,
                    )
                except (BackendApplicationError, ValueError, TypeError, KeyError) as error:
                    await self._finalize_external_failure(
                        claim,
                        worker,
                        authority,
                        result,
                        pipeline_code="CERTIFICATION_MATERIALIZATION_FAILED",
                        contract_code="INVARIANT_VIOLATION",
                        category=ErrorCategory.INVARIANT,
                        message=f"Certification materialization failed: {type(error).__name__}",
                    )
            elif result_valid:
                await self._finalize_failure(claim, worker, authority, result)
        except (PostgresCommitStateUnknown, psycopg.Error):
            # Database uncertainty after an external Build side effect is not a
            # student rejection.  Keep the deterministic workspace and fenced
            # lease state for expiry/takeover; the same build identity will
            # reconcile rather than trusting an uncommitted outcome.
            return
        try:
            await asyncio.to_thread(authority.builder.discard_workspace, result)
        except (OSError, RuntimeError):
            # A retained deterministic workspace is safe and reconcilable; it
            # must never roll back an already durable terminal Build.
            pass

    async def _publish_with_heartbeats(
        self,
        source: Path,
        claim: BuildJobClaim,
        worker: StudentSkillChainWorker,
    ) -> PublishedArtifact:
        await worker.heartbeat(claim)
        task = asyncio.create_task(asyncio.to_thread(self._publisher.publish, source))
        while True:
            try:
                published = await asyncio.wait_for(
                    asyncio.shield(task), timeout=worker.build_heartbeat_seconds
                )
                return published
            except TimeoutError:
                await worker.heartbeat(claim)

    async def _finalize_setup_failure(
        self,
        claim: BuildJobClaim,
        worker: StudentSkillChainWorker,
        source: BackendApplicationError,
    ) -> None:
        now = datetime.now(UTC)
        contract = ContractError(
            code="INVARIANT_VIOLATION",
            category=ErrorCategory.INVARIANT,
            retryable=False,
            user_message_key="system.invariant_violation",
            stage="VALIDATE_SOURCE",
            message=str(source)[:512] or "Build authority validation failed",
            details=cast(
                FrozenJsonObject,
                {"source_code": source.code, "source_stage": source.stage},
            ),
        )
        async with self._database.transaction_with_commit_boundary() as connection:
            locked = await worker.lock_build_claim(connection, claim)
            cursor = await connection.execute(
                """
                SELECT skill_id,status,terminal,resource_json,resource_sha256
                FROM yaya_skill_builds
                WHERE tenant_id=%s AND build_id=%s AND command_id=%s
                  AND actor_id=%s AND content_hash=%s
                FOR UPDATE
                """,
                (
                    claim.tenant_id,
                    claim.resource_id,
                    claim.command_id,
                    claim.actor_id,
                    claim.content_hash,
                ),
            )
            row = await cursor.fetchone()
            if (
                row is None
                or row["status"] not in {"ACCEPTED", "COMPILING"}
                or row["terminal"] is not False
            ):
                raise source
            current_status = cast(str, row["status"])
            accepted = _mapping(row["resource_json"], "Build resource")
            if canonical_json_sha256(accepted) != row["resource_sha256"]:
                raise source
            resource = dict(accepted)
            resource.update(
                {
                    "status": "FAILED",
                    "terminal": True,
                    "updated_at": _iso(now),
                    "phases": [
                        {
                            "name": phase,
                            "status": "FAILED" if phase == "VALIDATE_SOURCE" else "SKIPPED",
                            "started_at": _iso(now) if phase == "VALIDATE_SOURCE" else None,
                            "finished_at": _iso(now) if phase == "VALIDATE_SOURCE" else None,
                            "diagnostic_codes": (
                                ["BUILD_AUTHORITY_INVALID"] if phase == "VALIDATE_SOURCE" else []
                            ),
                        }
                        for phase in _PHASES
                    ],
                    "failure": plain(contract),
                }
            )
            self._validator.validate("schemas/game/skill-build.schema.json", resource)
            digest = canonical_json_sha256(resource)
            receipt = {
                "build_id": claim.resource_id,
                "step": "VALIDATE_SOURCE",
                "attempt": claim.attempt,
                "outcome": "FAILED",
                "source_code": source.code,
                "source_stage": source.stage,
            }
            await connection.execute(
                """
                INSERT INTO yaya_build_step_receipts(
                    tenant_id,build_id,step,attempt,input_sha256,output_sha256,
                    outcome,receipt_json,completed_at
                ) VALUES (%s,%s,'VALIDATE_SOURCE',%s,%s,%s,'FAILED',%s,%s)
                """,
                (
                    claim.tenant_id,
                    claim.resource_id,
                    claim.attempt,
                    hashlib.sha256(claim.request_body).hexdigest(),
                    canonical_json_sha256(receipt),
                    Jsonb(receipt),
                    now,
                ),
            )
            await self._write_terminal_build(
                connection,
                claim,
                expected_status=current_status,
                status="FAILED",
                resource=resource,
                resource_sha256=digest,
                recorded_at=now,
                history_sequence=2 if current_status == "ACCEPTED" else 3,
            )
            await worker.complete_build_claim(connection, claim, locked)

    async def _run_with_heartbeats(
        self,
        authority: _BuildAuthority,
        claim: BuildJobClaim,
        worker: StudentSkillChainWorker,
    ) -> DockerBuildResult:
        await worker.heartbeat(claim)
        task = asyncio.create_task(asyncio.to_thread(authority.builder.build, authority.request))
        while True:
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(task), timeout=worker.build_heartbeat_seconds
                )
                await worker.heartbeat(claim)
                return result
            except TimeoutError:
                await worker.heartbeat(claim)

    async def _prepare(
        self,
        claim: BuildJobClaim,
        worker: StudentSkillChainWorker,
    ) -> _BuildAuthority:
        async with self._database.transaction_with_commit_boundary() as connection:
            await worker.lock_build_claim(connection, claim)
            row = await self._load_build_row(connection, claim, for_update=True)
            authority = self._authority_from_row(claim, row)
            status = row["status"]
            if row["terminal"] is not False or status not in {"ACCEPTED", "COMPILING"}:
                raise _backend_invariant("VALIDATE_SOURCE", "Build state cannot be executed")
            if status == "ACCEPTED":
                now = datetime.now(UTC)
                resource = _mapping(row["resource_json"], "Build resource")
                phases = self._phase_wire(
                    result=None,
                    started_at=now,
                    finished_at=None,
                )
                resource.update(
                    {
                        "status": "COMPILING",
                        "updated_at": _iso(now),
                        "phases": phases,
                    }
                )
                self._validator.validate("schemas/game/skill-build.schema.json", resource)
                digest = canonical_json_sha256(resource)
                updated = await connection.execute(
                    """
                    UPDATE yaya_skill_builds
                    SET status='COMPILING',resource_json=%s,resource_sha256=%s,updated_at=%s
                    WHERE tenant_id=%s AND build_id=%s AND status='ACCEPTED' AND terminal=FALSE
                    """,
                    (Jsonb(resource), digest, now, claim.tenant_id, claim.resource_id),
                )
                if updated.rowcount != 1:
                    raise _backend_invariant("VALIDATE_SOURCE", "Build start CAS was lost")
                await connection.execute(
                    """
                    INSERT INTO yaya_skill_build_history(
                        tenant_id,build_id,sequence,status,record_sha256,record_json,recorded_at
                    ) VALUES (%s,%s,2,'COMPILING',%s,%s,%s)
                    """,
                    (claim.tenant_id, claim.resource_id, digest, Jsonb(resource), now),
                )
                job_update = await connection.execute(
                    """
                    UPDATE yaya_control_jobs SET phase='COMPILE',updated_at=%s
                    WHERE tenant_id=%s AND job_id=%s AND state='LEASED'
                      AND worker_id=%s AND lease_id=%s AND fencing_token=%s
                    """,
                    (
                        now,
                        claim.tenant_id,
                        claim.job_id,
                        claim.worker_id,
                        claim.lease_id,
                        claim.fencing_token,
                    ),
                )
                if job_update.rowcount != 1:
                    raise _backend_invariant("COMPILE", "Build phase fencing was lost")
                authority = replace(authority, compile_started_at=now)
            return authority

    async def _load_build_row(
        self,
        connection: AsyncConnection[dict[str, object]],
        claim: BuildJobClaim,
        *,
        for_update: bool,
    ) -> dict[str, object]:
        suffix = " FOR UPDATE OF b" if for_update else ""
        cursor = await connection.execute(
            """
            SELECT b.*,p.compiler_profile AS policy_compiler_profile,
                   p.test_suite_version AS policy_test_suite_version,
                   p.compiler_image,p.compiler_version,p.compile_flags_json,
                   p.public_tests_json,p.hidden_tests_json,p.approved_capabilities_json,
                   p.limits_json,p.parameter_schema_json,p.semantic_version_major,
                   p.semantic_version_minor,p.runtime_abi_version,
                   p.policy_sha256 AS current_policy_sha256,
                   p.active AS policy_active,
                   a.learner_id,a.world_id,a.agent_profile_id,a.task_id,
                   a.content_unit_id,a.content_version,a.versions_json,
                   a.snapshot_sha256 AS authority_sha256,a.active AS authority_active,
                   COALESCE((
                       SELECT jsonb_agg(jsonb_build_object(
                           'sequence',h.sequence,'status',h.status,
                           'record_sha256',h.record_sha256,'record_json',h.record_json
                       ) ORDER BY h.sequence)
                       FROM yaya_skill_build_history h
                       WHERE h.tenant_id=b.tenant_id AND h.build_id=b.build_id
                   ),'[]'::jsonb) AS history_json
            FROM yaya_skill_builds b
            JOIN yaya_build_policies p
              ON p.tenant_id=b.tenant_id AND p.build_policy_id=b.build_policy_id
             AND p.actor_id=b.actor_id AND p.content_hash=b.content_hash
            JOIN yaya_launch_authorities a
              ON a.tenant_id=b.tenant_id AND a.authority_id=b.authority_id
             AND a.actor_id=b.actor_id AND a.content_hash=b.content_hash
            WHERE b.tenant_id=%s AND b.build_id=%s AND b.command_id=%s
              AND b.actor_id=%s AND b.content_hash=%s
            """
            + suffix,
            (
                claim.tenant_id,
                claim.resource_id,
                claim.command_id,
                claim.actor_id,
                claim.content_hash,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise _backend_invariant("VALIDATE_SOURCE", "Build authority was not found")
        return row

    @staticmethod
    async def _lock_artifact_digest(
        connection: AsyncConnection[dict[str, object]],
        artifact_sha256: str,
    ) -> None:
        """Serialize publication/finalization and orphan cleanup for one digest."""

        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"yaya-artifact:{artifact_sha256}",),
        )

    async def _terminal_outcome_is_durable(
        self,
        claim: BuildJobClaim,
        *,
        status: Literal["CERTIFIED", "REJECTED", "FAILED"],
        resource: Mapping[str, object],
        resource_sha256: str,
        history_sequence: int,
    ) -> bool:
        """Re-read a lost COMMIT through a fresh connection before cleanup.

        Returning ``True`` is deliberately stricter than observing a terminal
        Build row.  The exact immutable history, receipts, Command, fenced job,
        and success authority must all be visible from the fresh database
        connection before the external workspace may be discarded.
        """

        connection: AsyncConnection[dict[str, object]] | None = None
        try:
            connection = await self._database.connect(autocommit=True)
            cursor = await connection.execute(
                """
                SELECT b.status,b.terminal,b.resource_sha256,b.resource_json,
                       h.status AS history_status,h.record_sha256 AS history_sha256,
                       h.record_json AS history_json,
                       j.state AS job_state,j.phase AS job_phase,j.worker_id,j.lease_id,
                       j.result_json AS job_result,j.operation AS job_operation,
                       c.status AS command_status,c.record_json AS command_json,
                       (SELECT count(*)::integer
                          FROM yaya_build_step_receipts r
                         WHERE r.tenant_id=b.tenant_id AND r.build_id=b.build_id)
                         AS receipt_count,
                       (SELECT count(*)::integer
                          FROM yaya_build_step_receipts r
                         WHERE r.tenant_id=b.tenant_id AND r.build_id=b.build_id
                           AND r.attempt=%s) AS claim_receipt_count,
                       (SELECT count(*)::integer FROM yaya_artifacts a
                         WHERE a.tenant_id=b.tenant_id AND a.build_id=b.build_id)
                         AS artifact_count,
                       (SELECT min(a.artifact_sha256) FROM yaya_artifacts a
                         WHERE a.tenant_id=b.tenant_id AND a.build_id=b.build_id)
                         AS artifact_sha256,
                       (SELECT count(*)::integer FROM yaya_skill_certifications sc
                         WHERE sc.tenant_id=b.tenant_id AND sc.build_id=b.build_id)
                         AS certification_count,
                       (SELECT min(sc.certification_id) FROM yaya_skill_certifications sc
                         WHERE sc.tenant_id=b.tenant_id AND sc.build_id=b.build_id)
                         AS certification_id,
                       (SELECT min(sc.skill_version_id) FROM yaya_skill_certifications sc
                         WHERE sc.tenant_id=b.tenant_id AND sc.build_id=b.build_id)
                         AS skill_version_id,
                       (SELECT count(*)::integer
                          FROM yaya_registry_certifications rc
                          JOIN yaya_skill_certifications sc
                            ON sc.tenant_id=rc.tenant_id
                           AND sc.certification_id=rc.certification_id
                         WHERE sc.tenant_id=b.tenant_id AND sc.build_id=b.build_id)
                         AS legacy_certification_count,
                       (SELECT count(*)::integer
                          FROM yaya_skills s
                          JOIN yaya_skill_certifications sc
                            ON sc.tenant_id=s.tenant_id
                           AND sc.certification_id=s.certification_id
                         WHERE sc.tenant_id=b.tenant_id AND sc.build_id=b.build_id)
                         AS skill_count,
                       (SELECT count(*)::integer FROM yaya_compile_results cr
                         WHERE cr.tenant_id=b.tenant_id AND cr.build_id=b.build_id)
                         AS compile_result_count,
                       (SELECT count(*)::integer FROM yaya_evidence e
                         WHERE e.tenant_id=b.tenant_id
                           AND e.evidence_json #>> '{source,source_id}'=b.build_id)
                         AS evidence_count,
                       (SELECT min(e.evidence_id) FROM yaya_evidence e
                         WHERE e.tenant_id=b.tenant_id
                           AND e.evidence_json #>> '{source,source_id}'=b.build_id)
                         AS evidence_id
                FROM yaya_skill_builds b
                JOIN yaya_control_jobs j
                  ON j.tenant_id=b.tenant_id AND j.command_id=b.command_id
                 AND j.job_id=%s AND j.resource_id=b.build_id
                JOIN yaya_commands c
                  ON c.tenant_id=b.tenant_id AND c.command_id=b.command_id
                LEFT JOIN yaya_skill_build_history h
                  ON h.tenant_id=b.tenant_id AND h.build_id=b.build_id
                 AND h.sequence=%s
                WHERE b.tenant_id=%s AND b.build_id=%s AND b.command_id=%s
                  AND b.actor_id=%s AND b.content_hash=%s
                """,
                (
                    claim.attempt,
                    claim.job_id,
                    history_sequence,
                    claim.tenant_id,
                    claim.resource_id,
                    claim.command_id,
                    claim.actor_id,
                    claim.content_hash,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                return False
            durable_resource = _mapping(row["resource_json"], "Build resource")
            durable_history = _mapping(row["history_json"], "Build terminal history")
            command = decode_as(row["command_json"], CommandRecord)
            expected_result: dict[str, object] = {
                "result_type": "RESOURCE_CREATED",
                "resource_type": "SKILL_BUILD",
                "resource_id": claim.resource_id,
                "resource_url": f"/v1/skill-builds/{claim.resource_id}",
            }
            phases = _sequence(resource.get("phases"), "Build phases")
            expected_receipts = sum(
                1
                for raw_phase in phases
                if _mapping(raw_phase, "Build phase").get("status") in {"PASSED", "FAILED"}
            )
            if (
                row["status"] != status
                or row["terminal"] is not True
                or row["resource_sha256"] != resource_sha256
                or durable_resource != dict(resource)
                or canonical_json_sha256(durable_resource) != resource_sha256
                or row["history_status"] != status
                or row["history_sha256"] != resource_sha256
                or durable_history != durable_resource
                or row["job_state"] != "SUCCEEDED"
                or row["job_phase"] != "COMPLETE"
                or row["worker_id"] is not None
                or row["lease_id"] is not None
                or row["job_operation"] != "CREATE_SKILL_BUILD"
                or row["job_result"] != expected_result
                or row["command_status"] != "APPLIED"
                or command.command_id != claim.command_id
                or command.status is not CommandStatus.APPLIED
                or command.terminal is not True
                or command.result != expected_result
                or row["receipt_count"] != expected_receipts
                or row["claim_receipt_count"] != expected_receipts
            ):
                return False
            authority_counts = (
                row["artifact_count"],
                row["certification_count"],
                row["legacy_certification_count"],
                row["skill_count"],
                row["compile_result_count"],
                row["evidence_count"],
            )
            if status != "CERTIFIED":
                return authority_counts == (0, 0, 0, 0, 0, 0)
            artifact = _mapping(resource.get("artifact"), "Build Artifact")
            certification = _mapping(resource.get("certification"), "Build Certification")
            evidence_refs = _sequence(resource.get("evidence_refs"), "Build evidence refs")
            if len(evidence_refs) != 1:
                return False
            evidence = _mapping(evidence_refs[0], "Build evidence ref")
            return (
                authority_counts == (1, 1, 1, 1, 1, 1)
                and row["artifact_sha256"] == artifact.get("artifact_sha256")
                and row["certification_id"] == certification.get("certification_id")
                and row["skill_version_id"] == resource.get("skill_version_id")
                and row["evidence_id"] == evidence.get("evidence_id")
            )
        except (psycopg.Error, ValueError, TypeError, KeyError):
            return False
        finally:
            if connection is not None:
                try:
                    await connection.close()
                except psycopg.Error:
                    pass

    async def _discard_unreferenced_artifact(self, published: PublishedArtifact) -> bool:
        """Delete a rollback orphan only while holding the digest publication lock."""

        try:
            async with self._database.transaction_with_commit_boundary() as connection:
                await self._lock_artifact_digest(connection, published.artifact_sha256)
                cursor = await connection.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM yaya_artifacts
                        WHERE artifact_sha256=%s)
                      + (SELECT count(*) FROM yaya_skill_certifications
                          WHERE artifact_sha256=%s)
                      + (SELECT count(*) FROM yaya_registry_certifications
                          WHERE artifact_sha256=%s)
                      + (SELECT count(*) FROM yaya_skills WHERE artifact_sha256=%s)
                      + (SELECT count(*) FROM yaya_session_skill_versions
                          WHERE artifact_sha256=%s)
                      + (SELECT count(*) FROM yaya_registry_entries
                          WHERE artifact_sha256=%s)
                      + (SELECT count(*) FROM yaya_skill_activations
                          WHERE artifact_sha256=%s) AS reference_count
                    """,
                    (published.artifact_sha256,) * 7,
                )
                row = await cursor.fetchone()
                if row is None or row["reference_count"] != 0:
                    return False
                await asyncio.to_thread(self._unlink_verified_artifact, published)
                return True
        except (
            ArtifactIntegrityError,
            ArtifactPublicationError,
            OSError,
            PostgresCommitStateUnknown,
            psycopg.Error,
        ):
            # Retaining an immutable CAS file without database authority is
            # safer than deleting bytes whose identity/reference state could
            # not be proven under the digest lock.
            return False

    def _unlink_verified_artifact(self, published: PublishedArtifact) -> None:
        verified = self._publisher.verify(published.artifact_sha256)
        expected_path = self._publisher.artifact_path(published.artifact_sha256).absolute()
        if (
            verified.path != expected_path
            or verified.path != published.path
            or verified.size_bytes != published.size_bytes
            or verified.artifact_uri != published.artifact_uri
        ):
            raise ArtifactIntegrityError(
                "PUBLISHED_ARTIFACT_DRIFT",
                "rollback Artifact no longer matches its publication receipt",
            )
        target = verified.path
        if target.is_symlink() or target.resolve(strict=True) != expected_path:
            raise ArtifactIntegrityError(
                "ARTIFACT_PATH_ESCAPE",
                "rollback Artifact path is no longer canonical",
            )
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        target.unlink()
        self._fsync_directory(target.parent)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(descriptor)
            except OSError:
                pass
        finally:
            os.close(descriptor)

    def _authority_from_row(
        self,
        claim: BuildJobClaim,
        row: Mapping[str, object],
    ) -> _BuildAuthority:
        resource = _mapping(row["resource_json"], "Build resource")
        source = _mapping(row["source_bundle_json"], "Build source bundle")
        request_source = _mapping(claim.request_json.get("source_bundle"), "Build request source")
        source_sha256 = canonical_source_bundle_sha256(source)
        if (
            canonical_json_sha256(resource) != row["resource_sha256"]
            or source != request_source
            or source_sha256 != row["source_bundle_sha256"]
            or resource.get("build_id") != claim.resource_id
            or resource.get("skill_id") != row["skill_id"]
            or resource.get("status") != row["status"]
            or resource.get("terminal") != row["terminal"]
            or row["authority_id"] != claim.authority_id
        ):
            raise _backend_invariant("VALIDATE_SOURCE", "Build persisted authority drifted")
        self._validate_history(row["history_json"], resource, cast(str, row["resource_sha256"]))
        created_at = resource.get("created_at")
        if not isinstance(created_at, str):
            raise _backend_invariant("VALIDATE_SOURCE", "Build creation time drifted")
        if (
            claim.request_json.get("skill_id") != row["skill_id"]
            or claim.request_json.get("client_draft_revision") != row["client_draft_revision"]
            or claim.request_json.get("compiler_profile") != row["compiler_profile"]
            or claim.request_json.get("test_suite_version") != row["test_suite_version"]
        ):
            raise _backend_invariant("VALIDATE_SOURCE", "Build request identity drifted")

        versions = self._decode_versions(row["versions_json"])
        if (
            row["compiler_image"] != self._runtime_image
            or versions.sandbox_image_digest != self._runtime_image
        ):
            raise _backend_invariant(
                "VALIDATE_SOURCE", "Compiler and runtime image authority drifted"
            )
        authority_projection = {
            "authority_id": row["authority_id"],
            "learner_id": row["learner_id"],
            "agent_profile_id": row["agent_profile_id"],
            "world_id": row["world_id"],
            "task_id": row["task_id"],
            "content_unit_id": row["content_unit_id"],
            "content_version": row["content_version"],
            "content_hash": row["content_hash"],
            "versions": row["versions_json"],
        }
        if canonical_json_sha256(authority_projection) != row["authority_sha256"]:
            raise _backend_invariant("VALIDATE_SOURCE", "Launch authority hash drifted")

        requested = self._string_tuple(row["requested_capabilities_json"], "requested capabilities")
        approved = self._string_tuple(row["approved_capabilities_json"], "approved capabilities")
        if (
            len(set(requested)) != len(requested)
            or len(set(approved)) != len(approved)
            or approved != tuple(sorted(approved))
            or any(item not in approved for item in requested)
        ):
            raise _backend_invariant("VALIDATE_SOURCE", "Build capabilities drifted")
        policy_projection = {
            "build_policy_id": row["build_policy_id"],
            "actor_id": row["actor_id"],
            "content_hash": row["content_hash"],
            "compiler_profile": row["policy_compiler_profile"],
            "test_suite_version": row["policy_test_suite_version"],
            "compiler_image": row["compiler_image"],
            "compiler_version": row["compiler_version"],
            "compile_flags": row["compile_flags_json"],
            "public_tests": row["public_tests_json"],
            "hidden_tests": row["hidden_tests_json"],
            "approved_capabilities": list(approved),
            "limits": row["limits_json"],
            "parameter_schema": row["parameter_schema_json"],
            "semantic_version_major": row["semantic_version_major"],
            "semantic_version_minor": row["semantic_version_minor"],
            "runtime_abi_version": row["runtime_abi_version"],
        }
        policy_sha256 = canonical_json_sha256(policy_projection)
        if (
            policy_sha256 != row["current_policy_sha256"]
            or row["compiler_profile"] != row["policy_compiler_profile"]
            or row["test_suite_version"] != row["policy_test_suite_version"]
        ):
            raise _backend_invariant("VALIDATE_SOURCE", "Build policy hash drifted")
        flags = self._string_tuple(row["compile_flags_json"], "compile flags")
        if flags != CPP20_SAFE_V1_FLAGS or row["compiler_profile"] != CPP20_SAFE_V1_PROFILE:
            raise _backend_invariant("VALIDATE_SOURCE", "Compiler profile flags drifted")

        parameter_schema = _mapping(row["parameter_schema_json"], "parameter schema")
        if "x-yaya-certification" in parameter_schema:
            raise _backend_invariant(
                "VALIDATE_SOURCE", "Policy parameter schema reserved metadata drifted"
            )
        try:
            validator_for(parameter_schema).check_schema(parameter_schema)
        except Exception as error:
            raise _backend_invariant("VALIDATE_SOURCE", "Parameter schema is invalid") from error
        semantic_major = row["semantic_version_major"]
        semantic_minor = row["semantic_version_minor"]
        runtime_abi = row["runtime_abi_version"]
        if (
            isinstance(semantic_major, bool)
            or not isinstance(semantic_major, int)
            or isinstance(semantic_minor, bool)
            or not isinstance(semantic_minor, int)
            or not isinstance(runtime_abi, str)
            or runtime_abi != _SUPPORTED_RUNTIME_ABI
        ):
            raise _backend_invariant("VALIDATE_SOURCE", "Version policy drifted")
        semantic_version = (
            f"{semantic_major}.{semantic_minor}.{cast(int, row['client_draft_revision'])}"
        )
        parameter_schema["x-yaya-certification"] = {
            "semantic_version": semantic_version,
            "capabilities": list(requested),
            "runtime_abi_version": runtime_abi,
        }

        source_bundle = self._source_bundle(source)
        limits = self._limits(row["limits_json"])
        suite = CppTestSuite(
            version=cast(str, row["test_suite_version"]),
            public_tests=self._tests(row["public_tests_json"], "PUBLIC"),
            hidden_tests=self._tests(row["hidden_tests_json"], "HIDDEN"),
        )
        builder = DigestPinnedDockerCppBuilder(
            self._workspace_root,
            image=cast(str, row["compiler_image"]),
            compiler_version=cast(str, row["compiler_version"]),
            test_suites=(suite,),
            docker_executable=self._docker_executable,
            limits=limits,
        )
        request = CompileAndTestRequest(
            build_id=claim.resource_id,
            skill_id=cast(str, row["skill_id"]),
            source_bundle=source_bundle,
            compiler_profile=cast(str, row["compiler_profile"]),
            test_suite_version=cast(str, row["test_suite_version"]),
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
        build_identity = builder.build_identity(request)
        return _BuildAuthority(
            build_id=claim.resource_id,
            skill_id=cast(str, row["skill_id"]),
            created_at=created_at,
            compile_started_at=self._compile_start(resource, cast(str, row["status"])),
            client_draft_revision=cast(int, row["client_draft_revision"]),
            display_name=cast(str, claim.request_json["display_name"]),
            source_json=source,
            source_bundle=source_bundle,
            source_sha256=source_sha256,
            requested_capabilities=requested,
            approved_capabilities=approved,
            build_policy_id=cast(str, row["build_policy_id"]),
            policy_sha256=policy_sha256,
            compiler_profile=cast(str, row["compiler_profile"]),
            compiler_version=cast(str, row["compiler_version"]),
            compiler_image=cast(str, row["compiler_image"]),
            test_suite_version=cast(str, row["test_suite_version"]),
            semantic_version=semantic_version,
            runtime_abi_version=runtime_abi,
            parameter_schema=cast(FrozenJsonObject, parameter_schema),
            learner_id=cast(str, row["learner_id"]),
            world_id=cast(str, row["world_id"]),
            versions=versions,
            test_suite=suite,
            builder=builder,
            request=request,
            build_identity=build_identity,
        )

    def _validate_history(
        self,
        raw_history: object,
        current: Mapping[str, object],
        current_sha256: str,
    ) -> None:
        if not isinstance(raw_history, list):
            raise _backend_invariant("VALIDATE_SOURCE", "Build history is invalid")
        rows = cast(list[object], raw_history)
        current_status = current.get("status")
        if not isinstance(current_status, str):
            raise _backend_invariant("VALIDATE_SOURCE", "Build history status drifted")
        expected = {
            "ACCEPTED": ("ACCEPTED",),
            "COMPILING": ("ACCEPTED", "COMPILING"),
        }.get(current_status)
        if expected is None or len(rows) != len(expected):
            raise _backend_invariant("VALIDATE_SOURCE", "Build history length drifted")
        last_record: Mapping[str, object] | None = None
        last_sha256: object = None
        for sequence, (raw_row, expected_status) in enumerate(
            zip(rows, expected, strict=True), start=1
        ):
            history = _mapping(raw_row, "Build history row")
            record = _mapping(history.get("record_json"), "Build history resource")
            record_sha256 = canonical_json_sha256(record)
            if (
                history.get("sequence") != sequence
                or history.get("status") != expected_status
                or record.get("status") != expected_status
                or history.get("record_sha256") != record_sha256
            ):
                raise _backend_invariant("VALIDATE_SOURCE", "Build history drifted")
            self._validator.validate("schemas/game/skill-build.schema.json", record)
            last_record = record
            last_sha256 = history.get("record_sha256")
        if last_record != current or last_sha256 != current_sha256:
            raise _backend_invariant("VALIDATE_SOURCE", "Build head/history drifted")

    @staticmethod
    def _decode_versions(value: object) -> VersionSet:
        return decode_as(value, VersionSet)

    @staticmethod
    def _compile_start(resource: Mapping[str, object], status: str) -> datetime:
        raw_timestamp: object = resource.get("created_at")
        if status == "COMPILING":
            for raw_phase in _sequence(resource.get("phases"), "Build phases"):
                phase = _mapping(raw_phase, "Build phase")
                if phase.get("name") == "COMPILE":
                    raw_timestamp = phase.get("started_at")
                    break
        if not isinstance(raw_timestamp, str):
            raise _backend_invariant("VALIDATE_SOURCE", "Build compile timestamp drifted")
        try:
            parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError as error:
            raise _backend_invariant(
                "VALIDATE_SOURCE", "Build compile timestamp drifted"
            ) from error
        if parsed.tzinfo is None:
            raise _backend_invariant("VALIDATE_SOURCE", "Build compile timestamp is not UTC")
        return parsed.astimezone(UTC)

    @staticmethod
    def _source_bundle(value: Mapping[str, object]) -> SkillSourceBundle:
        files = tuple(
            SkillSourceFile(
                path=cast(str, item["path"]),
                content=cast(str, item["content"]),
                content_sha256=cast(str, item["content_sha256"]),
            )
            for item in (
                _mapping(raw, "source file")
                for raw in _sequence(value.get("files"), "source files")
            )
        )
        return SkillSourceBundle(
            language=cast(Literal["CPP20"], value["language"]),
            entrypoint=cast(str, value["entrypoint"]),
            files=files,
        )

    @staticmethod
    def _string_tuple(value: object, label: str) -> tuple[str, ...]:
        items = _sequence(value, label)
        if any(not isinstance(item, str) for item in items):
            raise ValueError(f"{label} must contain only strings")
        return tuple(cast(str, item) for item in items)

    @staticmethod
    def _tests(
        value: object,
        visibility: Literal["PUBLIC", "HIDDEN"],
    ) -> tuple[CppTestCase, ...]:
        result: list[CppTestCase] = []
        for index, raw in enumerate(_sequence(value, f"{visibility} tests")):
            item = _mapping(raw, f"{visibility} test {index}")
            _exact_keys(
                item,
                {
                    "test_case_id",
                    "visibility",
                    "arguments",
                    "stdin_base64",
                    "expected_stdout_sha256",
                },
                f"{visibility} test {index}",
            )
            arguments = PostgresSkillBuildExecutor._string_tuple(
                item["arguments"], f"{visibility} test arguments"
            )
            if item["visibility"] != visibility:
                raise ValueError("test visibility does not match its policy collection")
            raw_stdin = item["stdin_base64"]
            if not isinstance(raw_stdin, str):
                raise ValueError("test stdin_base64 must be a string")
            try:
                stdin = base64.b64decode(raw_stdin, validate=True)
            except ValueError as error:
                raise ValueError("test stdin_base64 is invalid") from error
            expected = item["expected_stdout_sha256"]
            if expected is not None and not isinstance(expected, str):
                raise ValueError("test expected_stdout_sha256 must be a string or null")
            result.append(
                CppTestCase(
                    test_case_id=cast(str, item["test_case_id"]),
                    visibility=visibility,
                    arguments=arguments,
                    stdin=stdin,
                    expected_stdout_sha256=expected,
                )
            )
        return tuple(result)

    @staticmethod
    def _limits(value: object) -> BuildResourceLimits:
        item = _mapping(value, "Build limits")
        keys = {
            "compile_wall_ms",
            "test_wall_ms",
            "memory_bytes",
            "max_processes",
            "cpu_millis",
            "tmpfs_bytes",
            "max_output_bytes",
            "max_artifact_bytes",
        }
        _exact_keys(item, keys, "Build limits")
        return BuildResourceLimits(
            compile_wall_ms=cast(int, item["compile_wall_ms"]),
            test_wall_ms=cast(int, item["test_wall_ms"]),
            memory_bytes=cast(int, item["memory_bytes"]),
            max_processes=cast(int, item["max_processes"]),
            cpus=cast(int, item["cpu_millis"]) / 1000,
            tmpfs_bytes=cast(int, item["tmpfs_bytes"]),
            max_output_bytes=cast(int, item["max_output_bytes"]),
            max_artifact_bytes=cast(int, item["max_artifact_bytes"]),
        )

    async def _finalize_success(
        self,
        claim: BuildJobClaim,
        worker: StudentSkillChainWorker,
        authority: _BuildAuthority,
        result: DockerBuildResult,
    ) -> None:
        if (
            result.staged_artifact is None
            or result.artifact_sha256 is None
            or result.build_identity is None
        ):
            raise _backend_invariant("CERTIFY", "Successful Build has no staged Artifact")
        staged_metadata = result.staged_artifact.stat()
        published = PublishedArtifact(
            artifact_sha256=result.artifact_sha256,
            path=result.staged_artifact,
            size_bytes=staged_metadata.st_size,
            artifact_uri=f"{_ARTIFACT_URI_PREFIX}{result.artifact_sha256}",
        )
        now = datetime.now(UTC)
        skill_version_id = _identifier("skillver", authority.build_id, published.artifact_sha256)
        certification_id = _identifier("cert", authority.build_id, published.artifact_sha256)
        evidence_id = _identifier("evidence_build", authority.build_id)
        versions = replace(
            authority.versions,
            skill_version=skill_version_id,
            artifact_sha256=published.artifact_sha256,
            compiler_version=authority.compiler_version,
            sandbox_image_digest=authority.compiler_image,
            test_suite_version=authority.test_suite_version,
        )
        payload: dict[str, object] = {
            "evidence_kind": "BUILD_CERTIFICATION",
            "build_id": authority.build_id,
            "skill_id": authority.skill_id,
            "skill_version_id": skill_version_id,
            "artifact_sha256": published.artifact_sha256,
            "test_suite_version": authority.test_suite_version,
            "outcome": "CERTIFIED",
        }
        evidence = EvidenceRef(
            evidence_id=evidence_id,
            evidence_type=EvidenceType.TEST_REPORT,
            created_at=now,
            sha256=canonical_json_sha256(payload),
        )
        artifact = BuildArtifact(
            artifact_sha256=published.artifact_sha256,
            source_sha256=authority.source_sha256,
            compiler_profile=authority.compiler_profile,
            compiler_version=authority.compiler_version,
            sandbox_image_digest=authority.compiler_image,
            test_suite_version=authority.test_suite_version,
            artifact_uri=published.artifact_uri,
        )
        certified = CertifiedSkill(
            certification_id=certification_id,
            skill_id=authority.skill_id,
            skill_version_id=skill_version_id,
            semantic_version=authority.semantic_version,
            artifact=artifact,
            capabilities=authority.requested_capabilities,
            certified_at=now,
            revoked_at=None,
            metadata=cast(
                FrozenJsonObject,
                {
                    "build_id": authority.build_id,
                    "client_draft_revision": authority.client_draft_revision,
                    "display_name": authority.display_name,
                    "evidence_id": evidence_id,
                    "source_bundle_sha256": authority.source_sha256,
                    "build_policy_id": authority.build_policy_id,
                    "policy_sha256": authority.policy_sha256,
                },
            ),
        )
        entrypoint_file = next(
            item
            for item in authority.source_bundle.files
            if item.path == authority.source_bundle.entrypoint
        )
        skill_ref = SkillRef(
            authority.skill_id,
            skill_version_id,
            published.artifact_sha256,
            certification_id,
        )
        skill = SkillSnapshot(
            ref=skill_ref,
            source_code=entrypoint_file.content,
            source_sha256=entrypoint_file.content_sha256,
            entrypoint=authority.source_bundle.entrypoint,
            parameter_schema=authority.parameter_schema,
            request_context=claim.context,
        )
        compile_result = CompileResultSnapshot(
            build_id=authority.build_id,
            skill_ref=skill_ref,
            succeeded=True,
            diagnostics=(),
            evidence_refs=(evidence,),
            request_context=claim.context,
        )
        evidence_document: dict[str, object] = {
            "request_context": _context_wire(claim),
            "evidence_ref": _evidence_wire(evidence),
            "subject": {"learner_id": authority.learner_id},
            "source": {
                "source_type": "SKILL_BUILD",
                "source_id": authority.build_id,
                "command_id": claim.command_id,
                "world_id": authority.world_id,
            },
            "occurred_at": _iso(now),
            "recorded_at": _iso(now),
            "integrity": {
                "payload_sha256": evidence.sha256,
                "previous_evidence_sha256": None,
            },
            "payload": payload,
            "related_evidence": cast(list[object], []),
            "versions": _version_wire(versions),
        }
        self._validator.validate("schemas/game/evidence.schema.json", evidence_document)
        tests_wire = [
            {
                "test_case_id": item.test_case_id,
                "visibility": item.visibility,
                "status": item.status,
                "diagnostic_codes": list(item.diagnostic_codes),
            }
            for item in result.tests
        ]
        certification_record: dict[str, object] = {
            "request_context": _context_wire(claim),
            "certification_id": certification_id,
            "build_id": authority.build_id,
            "command_id": claim.command_id,
            "skill_id": authority.skill_id,
            "skill_version_id": skill_version_id,
            "learner_id": authority.learner_id,
            "world_id": authority.world_id,
            "source_bundle_sha256": authority.source_sha256,
            "build_policy_id": authority.build_policy_id,
            "policy_sha256": authority.policy_sha256,
            "client_draft_revision": authority.client_draft_revision,
            "display_name": authority.display_name,
            "parameter_schema": dict(authority.parameter_schema),
            "artifact_sha256": published.artifact_sha256,
            "compiler_profile": authority.compiler_profile,
            "compiler_version": authority.compiler_version,
            "compiler_image": authority.compiler_image,
            "test_suite_version": authority.test_suite_version,
            "semantic_version": authority.semantic_version,
            "runtime_abi_version": authority.runtime_abi_version,
            "tests": tests_wire,
            "requested_capabilities": list(authority.requested_capabilities),
            "approved_capabilities": list(authority.approved_capabilities),
            "evidence_ref": _evidence_wire(evidence),
            "certified_at": _iso(now),
            "versions": _version_wire(versions),
        }
        certification_sha256 = canonical_json_sha256(certification_record)
        artifact_metadata = {
            "artifact_sha256": published.artifact_sha256,
            "artifact_uri": published.artifact_uri,
            "size_bytes": published.size_bytes,
            "source_sha256": authority.source_sha256,
            "build_policy_id": authority.build_policy_id,
            "policy_sha256": authority.policy_sha256,
            "compiler_profile": authority.compiler_profile,
            "compiler_version": authority.compiler_version,
            "compiler_image": authority.compiler_image,
            "test_suite_version": authority.test_suite_version,
            "build_identity": result.build_identity,
        }
        resource = self._success_resource(
            claim,
            authority,
            result,
            evidence,
            skill_version_id,
            certification_id,
            published.artifact_sha256,
            now,
            versions,
        )
        resource_sha256 = canonical_json_sha256(resource)

        durable: PublishedArtifact | None = None
        try:
            async with self._database.transaction_with_commit_boundary() as connection:
                await self._lock_artifact_digest(connection, published.artifact_sha256)
                durable = await self._publish_with_heartbeats(
                    result.staged_artifact,
                    claim,
                    worker,
                )
                if (
                    durable.artifact_sha256 != published.artifact_sha256
                    or durable.size_bytes != published.size_bytes
                    or durable.artifact_uri != published.artifact_uri
                ):
                    raise ArtifactIntegrityError(
                        "PUBLISHED_ARTIFACT_DRIFT",
                        "published Artifact does not match the certified materialization",
                    )
                published = durable
                locked = await worker.lock_build_claim(connection, claim)
                row = await self._load_build_row(connection, claim, for_update=True)
                self._assert_finalizable(claim, row, authority, result)
                await self._insert_step_receipts(
                    connection,
                    claim,
                    authority,
                    result,
                    include_certify=True,
                    completed_at=now,
                )
                await connection.execute(
                    """
                INSERT INTO yaya_artifacts(
                    tenant_id,artifact_sha256,build_id,skill_id,actor_id,content_hash,
                    source_sha256,artifact_uri,metadata_json,created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                    (
                        claim.tenant_id,
                        published.artifact_sha256,
                        authority.build_id,
                        authority.skill_id,
                        claim.actor_id,
                        claim.content_hash,
                        authority.source_sha256,
                        published.artifact_uri,
                        Jsonb(artifact_metadata),
                        now,
                    ),
                )
                await connection.execute(
                    """
                INSERT INTO yaya_skill_certifications(
                    tenant_id,certification_id,build_id,skill_id,skill_version_id,
                    artifact_sha256,actor_id,content_hash,certification_sha256,
                    record_json,issued_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                    (
                        claim.tenant_id,
                        certification_id,
                        authority.build_id,
                        authority.skill_id,
                        skill_version_id,
                        published.artifact_sha256,
                        claim.actor_id,
                        claim.content_hash,
                        certification_sha256,
                        Jsonb(certification_record),
                        now,
                    ),
                )
                await connection.execute(
                    """
                INSERT INTO yaya_registry_certifications(
                    tenant_id,certification_id,skill_id,skill_version_id,
                    artifact_sha256,record_json,rejected
                ) VALUES (%s,%s,%s,%s,%s,%s,FALSE)
                """,
                    (
                        claim.tenant_id,
                        certification_id,
                        authority.skill_id,
                        skill_version_id,
                        published.artifact_sha256,
                        Jsonb(encode(certified)),
                    ),
                )
                await connection.execute(
                    """
                INSERT INTO yaya_skills(
                    tenant_id,skill_id,skill_version_id,certification_id,actor_id,
                    session_id,content_hash,artifact_sha256,snapshot_json,active,created_at
                ) VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,%s,FALSE,%s)
                """,
                    (
                        claim.tenant_id,
                        authority.skill_id,
                        skill_version_id,
                        certification_id,
                        claim.actor_id,
                        claim.content_hash,
                        published.artifact_sha256,
                        Jsonb(encode(skill)),
                        now,
                    ),
                )
                await connection.execute(
                    """
                INSERT INTO yaya_compile_results(
                    tenant_id,build_id,actor_id,content_hash,snapshot_json
                ) VALUES (%s,%s,%s,%s,%s)
                """,
                    (
                        claim.tenant_id,
                        authority.build_id,
                        claim.actor_id,
                        claim.content_hash,
                        Jsonb(encode(compile_result)),
                    ),
                )
                await connection.execute(
                    """
                INSERT INTO yaya_evidence(
                    tenant_id,evidence_id,actor_id,content_hash,evidence_type,
                    payload_sha256,evidence_json,recorded_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                    (
                        claim.tenant_id,
                        evidence.evidence_id,
                        claim.actor_id,
                        claim.content_hash,
                        evidence.evidence_type.value,
                        evidence.sha256,
                        Jsonb(evidence_document),
                        now,
                    ),
                )
                await self._write_terminal_build(
                    connection,
                    claim,
                    expected_status="COMPILING",
                    status="CERTIFIED",
                    resource=resource,
                    resource_sha256=resource_sha256,
                    recorded_at=now,
                )
                await worker.complete_build_claim(
                    connection,
                    claim,
                    locked,
                    evidence_refs=(evidence,),
                )
        except PostgresCommitStateUnknown:
            if await self._terminal_outcome_is_durable(
                claim,
                status="CERTIFIED",
                resource=resource,
                resource_sha256=resource_sha256,
                history_sequence=3,
            ):
                return
            raise
        except Exception:
            # Any ordinary exception leaving the transaction is a known
            # rollback.  Once publication returned its exact receipt, remove
            # only a still-unreferenced CAS orphan under the same digest lock.
            # Commit uncertainty is handled above and deliberately retains
            # both Artifact and workspace for takeover.
            if durable is not None:
                await self._discard_unreferenced_artifact(durable)
            raise

    async def _finalize_external_failure(
        self,
        claim: BuildJobClaim,
        worker: StudentSkillChainWorker,
        authority: _BuildAuthority,
        result: DockerBuildResult,
        *,
        pipeline_code: str,
        contract_code: str,
        category: ErrorCategory,
        message: str,
        retryable: bool = False,
    ) -> None:
        """Make post-compile infrastructure failures durable and queryable."""

        now = datetime.now(UTC)
        key = {
            ErrorCategory.DEPENDENCY: "dependency.temporarily_unavailable",
            ErrorCategory.INVARIANT: "system.invariant_violation",
            ErrorCategory.INTERNAL: "system.internal_error",
        }.get(category, "system.internal_error")
        contract = ContractError(
            code=contract_code,
            category=category,
            retryable=retryable,
            user_message_key=key,
            stage="CERTIFY",
            message=message[:512],
            details=cast(FrozenJsonObject, {"pipeline_code": pipeline_code}),
        )
        phases = [
            {
                "name": phase,
                "status": "FAILED" if phase == "CERTIFY" else "PASSED",
                "started_at": _iso(authority.compile_started_at),
                "finished_at": _iso(now),
                "diagnostic_codes": [pipeline_code] if phase == "CERTIFY" else [],
            }
            for phase in _PHASES
        ]
        resource: dict[str, object] = {
            "request_context": _context_wire(claim),
            "build_id": authority.build_id,
            "skill_id": authority.skill_id,
            "skill_version_id": None,
            "status": "FAILED",
            "terminal": True,
            "created_at": authority.created_at,
            "updated_at": _iso(now),
            "artifact": None,
            "certification": None,
            "phases": phases,
            "failure": plain(contract),
            "evidence_refs": [],
            "versions": _version_wire(authority.versions),
        }
        self._validator.validate("schemas/game/skill-build.schema.json", resource)
        resource_sha256 = canonical_json_sha256(resource)
        await worker.heartbeat(claim)
        try:
            async with self._database.transaction_with_commit_boundary() as connection:
                locked = await worker.lock_build_claim(connection, claim)
                row = await self._load_build_row(connection, claim, for_update=True)
                self._assert_finalizable(claim, row, authority, result)
                await self._insert_step_receipts(
                    connection,
                    claim,
                    authority,
                    result,
                    include_certify=True,
                    completed_at=now,
                    terminal_failure_code=pipeline_code,
                )
                await self._write_terminal_build(
                    connection,
                    claim,
                    expected_status="COMPILING",
                    status="FAILED",
                    resource=resource,
                    resource_sha256=resource_sha256,
                    recorded_at=now,
                )
                await worker.complete_build_claim(connection, claim, locked)
        except PostgresCommitStateUnknown:
            if await self._terminal_outcome_is_durable(
                claim,
                status="FAILED",
                resource=resource,
                resource_sha256=resource_sha256,
                history_sequence=3,
            ):
                return
            raise

    async def _finalize_failure(
        self,
        claim: BuildJobClaim,
        worker: StudentSkillChainWorker,
        authority: _BuildAuthority,
        result: DockerBuildResult,
    ) -> None:
        if result.failure is None:
            raise _backend_invariant("COMPILE", "Failed Build lacks a failure authority")
        now = datetime.now(UTC)
        contract = self._failure_contract(result)
        status: Literal["REJECTED", "FAILED"] = (
            "FAILED"
            if contract.code in {"DEPENDENCY_UNAVAILABLE", "INVARIANT_VIOLATION", "INTERNAL_ERROR"}
            else "REJECTED"
        )
        resource: dict[str, object] = {
            "request_context": _context_wire(claim),
            "build_id": authority.build_id,
            "skill_id": authority.skill_id,
            "skill_version_id": None,
            "status": status,
            "terminal": True,
            "created_at": authority.created_at,
            "updated_at": _iso(now),
            "artifact": None,
            "certification": None,
            "phases": self._phase_wire(
                result=result,
                started_at=authority.compile_started_at,
                finished_at=now,
            ),
            "failure": plain(contract),
            "evidence_refs": [],
            "versions": _version_wire(authority.versions),
        }
        self._validator.validate("schemas/game/skill-build.schema.json", resource)
        resource_sha256 = canonical_json_sha256(resource)
        await worker.heartbeat(claim)
        try:
            async with self._database.transaction_with_commit_boundary() as connection:
                locked = await worker.lock_build_claim(connection, claim)
                row = await self._load_build_row(connection, claim, for_update=True)
                self._assert_finalizable(claim, row, authority, result)
                await self._insert_step_receipts(
                    connection,
                    claim,
                    authority,
                    result,
                    include_certify=False,
                    completed_at=now,
                )
                await self._write_terminal_build(
                    connection,
                    claim,
                    expected_status="COMPILING",
                    status=status,
                    resource=resource,
                    resource_sha256=resource_sha256,
                    recorded_at=now,
                )
                await worker.complete_build_claim(connection, claim, locked)
        except PostgresCommitStateUnknown:
            if await self._terminal_outcome_is_durable(
                claim,
                status=status,
                resource=resource,
                resource_sha256=resource_sha256,
                history_sequence=3,
            ):
                return
            raise

    def _success_resource(
        self,
        claim: BuildJobClaim,
        authority: _BuildAuthority,
        result: DockerBuildResult,
        evidence: EvidenceRef,
        skill_version_id: str,
        certification_id: str,
        artifact_sha256: str,
        now: datetime,
        versions: VersionSet,
    ) -> dict[str, object]:
        resource: dict[str, object] = {
            "request_context": _context_wire(claim),
            "build_id": authority.build_id,
            "skill_id": authority.skill_id,
            "skill_version_id": skill_version_id,
            "status": "CERTIFIED",
            "terminal": True,
            "created_at": authority.created_at,
            "updated_at": _iso(now),
            "artifact": {
                "artifact_sha256": artifact_sha256,
                "source_sha256": authority.source_sha256,
                "compiler_profile": authority.compiler_profile,
                "compiler_version": authority.compiler_version,
                "test_suite_version": authority.test_suite_version,
            },
            "certification": {
                "certification_id": certification_id,
                "issued_at": _iso(now),
                "capabilities": list(authority.requested_capabilities),
            },
            "phases": self._phase_wire(
                result=result,
                started_at=authority.compile_started_at,
                finished_at=now,
            ),
            "failure": None,
            "evidence_refs": [_evidence_wire(evidence)],
            "versions": _version_wire(versions),
        }
        self._validator.validate("schemas/game/skill-build.schema.json", resource)
        return resource

    @staticmethod
    def _failure_contract(result: DockerBuildResult) -> ContractError:
        failure = result.failure
        if failure is None:
            raise ValueError("failure is required")
        pipeline_code = failure.code
        if pipeline_code in _DEPENDENCY_CODES:
            code = "DEPENDENCY_UNAVAILABLE"
            category = ErrorCategory.DEPENDENCY
            retryable = True
            key = "dependency.temporarily_unavailable"
        elif pipeline_code in _RESOURCE_LIMIT_CODES:
            code = "SANDBOX_RESOURCE_LIMIT"
            category = ErrorCategory.SANDBOX
            retryable = False
            key = "sandbox.resource_limit"
        elif pipeline_code in _USER_REJECTION_CODES:
            code = "SANDBOX_COMPILE_ERROR"
            category = ErrorCategory.SANDBOX
            retryable = False
            key = "sandbox.compile_error"
        elif pipeline_code in _INVARIANT_CODES or failure.stage == "VALIDATE_SOURCE":
            code = "INVARIANT_VIOLATION"
            category = ErrorCategory.INVARIANT
            retryable = False
            key = "system.invariant_violation"
        else:
            # Unknown pipeline codes are production infrastructure failures,
            # never student rejections.  Adding a new builder failure must not
            # accidentally blame user source or produce a certifiable result.
            code = "INTERNAL_ERROR"
            category = ErrorCategory.INTERNAL
            retryable = False
            key = "system.internal_error"
        diagnostics = [{"code": item.code, "message": item.message} for item in result.diagnostics]
        return ContractError(
            code=code,
            category=category,
            retryable=retryable,
            user_message_key=key,
            stage=failure.stage,
            message=f"Build pipeline ended with {pipeline_code}"[:512],
            details=cast(
                FrozenJsonObject,
                {"pipeline_code": pipeline_code, "diagnostics": diagnostics},
            ),
        )

    @staticmethod
    def _phase_wire(
        *,
        result: DockerBuildResult | None,
        started_at: datetime,
        finished_at: datetime | None,
    ) -> list[dict[str, object]]:
        if result is None:
            return [
                {
                    "name": phase,
                    "status": (
                        "PASSED"
                        if phase == "VALIDATE_SOURCE"
                        else ("RUNNING" if phase == "COMPILE" else "PENDING")
                    ),
                    "started_at": _iso(started_at)
                    if phase in {"VALIDATE_SOURCE", "COMPILE"}
                    else None,
                    "finished_at": _iso(started_at) if phase == "VALIDATE_SOURCE" else None,
                    "diagnostic_codes": [],
                }
                for phase in _PHASES
            ]
        failed_stage = None if result.failure is None else result.failure.stage
        failed_index = None if failed_stage is None else _PHASES.index(failed_stage)
        phases: list[dict[str, object]] = []
        for index, phase in enumerate(_PHASES):
            if failed_index is None:
                status = "PASSED"
            elif index < failed_index:
                status = "PASSED"
            elif index == failed_index:
                status = "FAILED"
            else:
                status = "SKIPPED"
            codes: list[str] = []
            if status == "FAILED" and result.failure is not None:
                codes = sorted({result.failure.code, *(item.code for item in result.diagnostics)})[
                    :100
                ]
            phases.append(
                {
                    "name": phase,
                    "status": status,
                    "started_at": _iso(started_at) if status != "SKIPPED" else None,
                    "finished_at": (
                        _iso(finished_at)
                        if finished_at is not None and status != "SKIPPED"
                        else None
                    ),
                    "diagnostic_codes": codes,
                }
            )
        return phases

    def _assert_finalizable(
        self,
        claim: BuildJobClaim,
        row: Mapping[str, object],
        authority: _BuildAuthority,
        result: DockerBuildResult,
    ) -> None:
        if (
            row["status"] != "COMPILING"
            or row["terminal"] is not False
            or row["source_bundle_sha256"] != authority.source_sha256
            or row["current_policy_sha256"] != authority.policy_sha256
        ):
            raise _backend_invariant("COMPLETE", "Build finalization authority drifted")
        reloaded = self._authority_from_row(claim, row)
        if replace(reloaded, builder=authority.builder) != authority:
            raise _backend_invariant("COMPLETE", "Build frozen authority changed during execution")
        self._assert_result_closure(reloaded, result)

    @staticmethod
    def _assert_result_closure(
        authority: _BuildAuthority,
        result: DockerBuildResult,
    ) -> None:
        if (
            result.build_id != authority.build_id
            or result.source_sha256 != authority.source_sha256
            or result.compiler_profile != authority.compiler_profile
            or result.compiler_version != authority.compiler_version
            or result.test_suite_version != authority.test_suite_version
            or result.build_identity != authority.build_identity
        ):
            raise _backend_invariant("COMPLETE", "Docker Build result identity drifted")

        expected = (*authority.test_suite.public_tests, *authority.test_suite.hidden_tests)
        actual_identity = tuple((item.test_case_id, item.visibility) for item in result.tests)
        expected_identity = tuple((item.test_case_id, item.visibility) for item in expected)
        if (
            len(actual_identity) > len(expected_identity)
            or actual_identity != expected_identity[: len(actual_identity)]
        ):
            raise _backend_invariant("COMPLETE", "Docker test result authority drifted")

        if result.succeeded:
            if (
                result.status != "SUCCEEDED"
                or result.failure is not None
                or result.diagnostics
                or result.workspace is None
                or result.staged_artifact is None
                or result.artifact_sha256 is None
                or re.fullmatch(r"[a-f0-9]{64}", result.artifact_sha256) is None
                or len(result.tests) != len(expected)
                or any(item.status != "PASSED" or item.diagnostic_codes for item in result.tests)
            ):
                raise _backend_invariant("COMPLETE", "Successful Docker result is incomplete")
            return

        failure = result.failure
        if (
            result.status != "FAILED"
            or failure is None
            or result.staged_artifact is not None
            or result.artifact_sha256 is not None
            or result.diagnostics != failure.diagnostics
        ):
            raise _backend_invariant("COMPLETE", "Failed Docker result is inconsistent")
        stage = failure.stage
        public_count = len(authority.test_suite.public_tests)
        if stage in {"VALIDATE_SOURCE", "COMPILE"}:
            valid_length = len(result.tests) == 0
        elif stage == "PUBLIC_TEST":
            valid_length = 1 <= len(result.tests) <= public_count
        elif stage == "HIDDEN_TEST":
            valid_length = public_count < len(result.tests) <= len(expected)
        else:
            valid_length = False
        if not valid_length:
            raise _backend_invariant("COMPLETE", "Failed Docker test sequence drifted")
        if result.tests:
            if (
                any(item.status != "PASSED" for item in result.tests[:-1])
                or result.tests[-1].status == "PASSED"
                or failure.code not in result.tests[-1].diagnostic_codes
            ):
                raise _backend_invariant("COMPLETE", "Docker test failure closure drifted")

    @staticmethod
    def _result_invariant_failure(
        authority: _BuildAuthority,
        untrusted: DockerBuildResult,
    ) -> DockerBuildResult:
        diagnostic = BuildDiagnostic(
            "BUILD_RESULT_AUTHORITY_DRIFT",
            "Build pipeline returned a result outside the frozen server authority",
        )
        return DockerBuildResult(
            build_id=authority.build_id,
            status="FAILED",
            source_sha256=authority.source_sha256,
            compiler_profile=authority.compiler_profile,
            compiler_version=authority.compiler_version,
            test_suite_version=authority.test_suite_version,
            build_identity=authority.build_identity,
            workspace=untrusted.workspace,
            staged_artifact=None,
            artifact_sha256=None,
            tests=(),
            diagnostics=(diagnostic,),
            failure=DockerBuildFailure(
                code="BUILD_RESULT_AUTHORITY_DRIFT",
                stage="COMPILE",
                diagnostics=(diagnostic,),
            ),
        )

    async def _insert_step_receipts(
        self,
        connection: AsyncConnection[dict[str, object]],
        claim: BuildJobClaim,
        authority: _BuildAuthority,
        result: DockerBuildResult,
        *,
        include_certify: bool,
        completed_at: datetime,
        terminal_failure_code: str | None = None,
    ) -> None:
        if include_certify:
            terminal_phase = "CERTIFY"
        else:
            failure = result.failure
            if failure is None:
                raise _backend_invariant("COMPLETE", "Failed receipt lacks failure authority")
            terminal_phase = failure.stage
        terminal_index = _PHASES.index(terminal_phase)
        for phase in _PHASES[: terminal_index + 1]:
            outcome = "PASSED"
            if result.failure is not None and phase == result.failure.stage:
                outcome = "FAILED"
            if phase == "CERTIFY" and terminal_failure_code is not None:
                outcome = "FAILED"
            receipt = {
                "build_id": authority.build_id,
                "build_identity": result.build_identity,
                "step": phase,
                "attempt": claim.attempt,
                "source_sha256": authority.source_sha256,
                "build_policy_id": authority.build_policy_id,
                "policy_sha256": authority.policy_sha256,
                "outcome": outcome,
                "pipeline_status": result.status,
                "terminal_failure_code": (terminal_failure_code if phase == "CERTIFY" else None),
                "artifact_sha256": result.artifact_sha256,
                "test_results": [
                    {
                        "test_case_id": item.test_case_id,
                        "visibility": item.visibility,
                        "status": item.status,
                        "diagnostic_codes": list(item.diagnostic_codes),
                    }
                    for item in result.tests
                    if (phase == "PUBLIC_TEST" and item.visibility == "PUBLIC")
                    or (phase == "HIDDEN_TEST" and item.visibility == "HIDDEN")
                ],
            }
            input_sha256 = canonical_json_sha256(
                {
                    "build_id": authority.build_id,
                    "step": phase,
                    "source_sha256": authority.source_sha256,
                    "build_policy_id": authority.build_policy_id,
                    "policy_sha256": authority.policy_sha256,
                }
            )
            output_sha256 = canonical_json_sha256(receipt)
            await connection.execute(
                """
                INSERT INTO yaya_build_step_receipts(
                    tenant_id,build_id,step,attempt,input_sha256,output_sha256,
                    outcome,receipt_json,completed_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    claim.tenant_id,
                    authority.build_id,
                    phase,
                    claim.attempt,
                    input_sha256,
                    output_sha256,
                    outcome,
                    Jsonb(receipt),
                    completed_at,
                ),
            )

    @staticmethod
    async def _write_terminal_build(
        connection: AsyncConnection[dict[str, object]],
        claim: BuildJobClaim,
        *,
        expected_status: str,
        status: Literal["CERTIFIED", "REJECTED", "FAILED"],
        resource: Mapping[str, object],
        resource_sha256: str,
        recorded_at: datetime,
        history_sequence: int = 3,
    ) -> None:
        updated = await connection.execute(
            """
            UPDATE yaya_skill_builds
            SET status=%s,terminal=TRUE,resource_sha256=%s,resource_json=%s,updated_at=%s
            WHERE tenant_id=%s AND build_id=%s AND status=%s AND terminal=FALSE
            """,
            (
                status,
                resource_sha256,
                Jsonb(resource),
                recorded_at,
                claim.tenant_id,
                claim.resource_id,
                expected_status,
            ),
        )
        if updated.rowcount != 1:
            raise _backend_invariant("COMPLETE", "Build terminal CAS was lost")
        await connection.execute(
            """
            INSERT INTO yaya_skill_build_history(
                tenant_id,build_id,sequence,status,record_sha256,record_json,recorded_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                claim.tenant_id,
                claim.resource_id,
                history_sequence,
                status,
                resource_sha256,
                Jsonb(resource),
                recorded_at,
            ),
        )


__all__ = ["PostgresSkillBuildExecutor"]
