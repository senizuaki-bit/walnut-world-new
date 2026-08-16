"""Production Agent-turn acceptance, worker leasing, and reconciliation queries.

This module is the database-facing application layer.  It intentionally does
not contain HTTP parsing or provider-specific code: the HTTP adapter supplies
an authenticated actor plus immutable attempt headers, while the worker owns
only durable Command jobs and calls the provider-neutral ``AgentHub``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from yaya_agent_build import canonical_source_bundle_sha256
from yaya_agent_contracts import (
    ActiveSkill,
    ActorRef,
    CertifiedSkill,
    CommandRecord,
    CommandStatus,
    CommandTransition,
    ContractError,
    ErrorCategory,
    EvidenceRef,
    NewCommand,
    OperationContext,
    RequestContext,
    SkillRef,
    VersionSet,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    CommittedAgentTurn,
    GameEvent,
    RunResultSnapshot,
    SessionSnapshot,
    SkillSnapshot,
)
from yaya_agent_runtime.errors import (
    AgentContextError,
    AgentDependencyError,
    AgentPersistenceError,
)
from yaya_agent_runtime.hub import AgentHub, AgentHubResult
from yaya_agent_runtime.pedagogy_policy import TeachingPhase

from .codec import decode_as, encode, plain
from .database import PostgresCommitStateUnknown, PostgresDatabase
from .outcome_authority import PostgresRunOutcomeAuthority
from .wire import ContractSchemaValidator

_MAX_HTTP_BODY_BYTES = 8 * 1024 * 1024
_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9_-]{8,96}$")
_TRACE_ID = re.compile(r"^trace_[A-Za-z0-9_-]{8,96}$")
_CORRELATION_ID = re.compile(r"^corr_[A-Za-z0-9_-]{8,96}$")
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_NON_TERMINAL = frozenset(
    {
        CommandStatus.ACCEPTED,
        CommandStatus.VALIDATING,
        CommandStatus.RUNNING_SANDBOX,
        CommandStatus.APPLYING_WORLD,
    }
)
_PERMANENT_WORKER_CODES = frozenset(
    {
        "AGENT_JOB_RESULT_INVALID",
        "AGENT_JOB_IDENTITY_MISMATCH",
        "AGENT_TURN_IDENTITY_MISMATCH",
        "AGENT_SESSION_IDENTITY_MISMATCH",
        "AGENT_RUN_NOT_COMMITTED",
        "AGENT_RUN_EVIDENCE_MISMATCH",
        "AGENT_RUN_IDENTITY_MISMATCH",
    }
)


def _stable_actor(left: ActorRef, right: ActorRef) -> bool:
    return (
        left.tenant_id,
        left.actor_id,
        left.actor_type,
    ) == (
        right.tenant_id,
        right.actor_id,
        right.actor_type,
    )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    source = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in source):
        raise ValueError(f"{label} contains a non-string key")
    return {cast(str, key): item for key, item in source.items()}


def _command_wire(record: CommandRecord) -> dict[str, object]:
    """Render the frozen Command wire without inventing optional null fields."""

    value = _mapping(plain(record), "Command wire")
    versions = _mapping(value.get("versions"), "Command versions")
    value["versions"] = {key: item for key, item in versions.items() if item is not None}
    value["evidence_refs"] = [_evidence_ref_wire(reference) for reference in record.evidence_refs]
    return value


def _request_context_wire(context: RequestContext | OperationContext) -> dict[str, object]:
    """Render the frozen six-field RequestContext projection.

    Run snapshots retain the richer internal OperationContext so recovery can
    fence the originating Command.  The public Run wire deliberately excludes
    those operation-only fields, so comparisons must use this same projection
    instead of generic dataclass serialization.
    """

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


def _evidence_ref_wire(reference: object) -> dict[str, object]:
    """Render an EvidenceRef with optional wire fields omitted, not null."""

    from yaya_agent_contracts import EvidenceRef

    if not isinstance(reference, EvidenceRef):
        raise TypeError("Run snapshot evidence contains an invalid reference")
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


def _strict_json_body(raw_body: bytes) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number: {value}")

    decoded = raw_body.decode("utf-8", errors="strict")
    value = json.loads(
        decoded,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    return _mapping(value, "Agent turn body")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sequence(value: object, label: str) -> list[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be an array")
    return list(cast(Sequence[object], value))


def _certified_test_results(
    public_tests: object,
    hidden_tests: object,
) -> list[dict[str, object]]:
    expected: list[dict[str, object]] = []
    for visibility, raw_tests in (("PUBLIC", public_tests), ("HIDDEN", hidden_tests)):
        for raw_test in _sequence(raw_tests, f"{visibility} policy tests"):
            test = _mapping(raw_test, f"{visibility} policy test")
            if test.get("visibility") != visibility:
                raise ValueError(f"{visibility} policy test visibility drifted")
            expected.append(
                {
                    "test_case_id": test.get("test_case_id"),
                    "visibility": visibility,
                    "status": "PASSED",
                    "diagnostic_codes": [],
                }
            )
    return expected


def _validate_public_certification_closure(
    row: Mapping[str, object],
    *,
    skill: SkillSnapshot,
    active: ActiveSkill,
    certification: CertifiedSkill,
    skill_ref: SkillRef,
    context: OperationContext,
) -> None:
    """Validate the immutable A8 authority behind one public Registry entry.

    The legacy ``yaya_skills`` and ``yaya_registry_certifications`` rows are
    compatibility projections.  A public Turn must therefore re-bind them to
    the content-addressed Artifact and the canonical Certification emitted by
    the exact terminal Build and its frozen policy before persisting a Session
    binding.  Pre-A8 Sessions never call this validator.
    """

    certification_record = _mapping(row["full_certification_json"], "full Certification record")
    build_resource = _mapping(row["build_json"], "certified Build resource")
    source_bundle = _mapping(row["source_bundle_json"], "certified source bundle")
    artifact_metadata = _mapping(row["artifact_metadata_json"], "Artifact metadata")
    build_artifact = _mapping(build_resource.get("artifact"), "Build Artifact")
    build_certification = _mapping(build_resource.get("certification"), "Build Certification")
    evidence_ref = _mapping(certification_record.get("evidence_ref"), "Certification Evidence")
    requested_capabilities = _sequence(
        row["requested_capabilities_json"], "Build requested capabilities"
    )
    approved_capabilities = _sequence(
        row["approved_capabilities_json"], "policy approved capabilities"
    )
    expected_tests = _certified_test_results(row["public_tests_json"], row["hidden_tests_json"])
    policy_projection: dict[str, object] = {
        "build_policy_id": row["build_policy_id"],
        "actor_id": row["certification_actor_id"],
        "content_hash": row["certification_content_hash"],
        "compiler_profile": row["policy_compiler_profile"],
        "test_suite_version": row["policy_test_suite_version"],
        "compiler_image": row["compiler_image"],
        "compiler_version": row["compiler_version"],
        "compile_flags": row["compile_flags_json"],
        "public_tests": row["public_tests_json"],
        "hidden_tests": row["hidden_tests_json"],
        "approved_capabilities": row["approved_capabilities_json"],
        "limits": row["limits_json"],
        "parameter_schema": row["parameter_schema_json"],
        "semantic_version_major": row["semantic_version_major"],
        "semantic_version_minor": row["semantic_version_minor"],
        "runtime_abi_version": row["runtime_abi_version"],
    }
    policy_sha256 = canonical_json_sha256(policy_projection)
    source_sha256 = canonical_source_bundle_sha256(source_bundle)
    issued_at = row["issued_at"]
    if not isinstance(issued_at, datetime):
        raise ValueError("Certification issued_at is not a datetime")
    client_draft_revision = row["client_draft_revision"]
    semantic_major = row["semantic_version_major"]
    semantic_minor = row["semantic_version_minor"]
    if (
        isinstance(client_draft_revision, bool)
        or not isinstance(client_draft_revision, int)
        or isinstance(semantic_major, bool)
        or not isinstance(semantic_major, int)
        or isinstance(semantic_minor, bool)
        or not isinstance(semantic_minor, int)
    ):
        raise ValueError("Certification semantic version authority is invalid")
    semantic_version = f"{semantic_major}.{semantic_minor}.{client_draft_revision}"
    parameter_schema = _mapping(row["parameter_schema_json"], "policy parameter schema")
    if "x-yaya-certification" in parameter_schema:
        raise ValueError("policy parameter schema contains certification metadata")
    expected_parameter_schema = dict(parameter_schema)
    expected_parameter_schema["x-yaya-certification"] = {
        "semantic_version": semantic_version,
        "capabilities": requested_capabilities,
        "runtime_abi_version": row["runtime_abi_version"],
    }
    entrypoint = source_bundle.get("entrypoint")
    entrypoint_files = [
        _mapping(raw_file, "source file")
        for raw_file in _sequence(source_bundle.get("files"), "source files")
        if _mapping(raw_file, "source file").get("path") == entrypoint
    ]
    if len(entrypoint_files) != 1:
        raise ValueError("certified source entrypoint drifted")
    entrypoint_file = entrypoint_files[0]
    expected_certification_metadata: dict[str, object] = {
        "build_id": row["build_id"],
        "client_draft_revision": client_draft_revision,
        "display_name": certification_record.get("display_name"),
        "evidence_id": evidence_ref.get("evidence_id"),
        "source_bundle_sha256": source_sha256,
        "build_policy_id": row["build_policy_id"],
        "policy_sha256": policy_sha256,
    }
    expected_artifact_uri = f"artifact://sha256/{skill_ref.artifact_sha256}"
    size_bytes = artifact_metadata.get("size_bytes")
    valid_size = not isinstance(size_bytes, bool) and isinstance(size_bytes, int) and size_bytes > 0

    drifts: list[str] = []

    def check(label: str, condition: bool) -> None:
        if condition:
            drifts.append(label)

    active_wire = _mapping(plain(active), "scoped ActiveSkill")
    check(
        "registry.record_json",
        active_wire != _mapping(row["active_json"], "stored scoped ActiveSkill"),
    )
    check("registry.entry_sha256", canonical_json_sha256(active_wire) != row["entry_sha256"])
    check("registry.revision", active.registry_revision != row["active_revision"])
    check("registry.skill_ref", skill.ref != skill_ref)
    check("registry.certification_mirror", active.skill != certification)
    check("registry.active_skill_id", active.skill.skill_id != skill_ref.skill_id)
    check(
        "registry.active_skill_version_id",
        active.skill.skill_version_id != skill_ref.skill_version_id,
    )
    check(
        "registry.active_certification_id",
        active.skill.certification_id != skill_ref.certification_id,
    )
    check(
        "registry.active_artifact_sha256",
        active.skill.artifact.artifact_sha256 != skill_ref.artifact_sha256,
    )
    check("scope.actor", not _stable_actor(skill.request_context.actor, context.actor))
    check("scope.content", skill.request_context.content_ref != context.content_ref)
    check("scope.world", certification_record.get("world_id") != row["public_world_id"])
    check("scope.learner", certification_record.get("learner_id") != row["public_learner_id"])
    check("certification.actor", row["certification_actor_id"] != context.actor.actor_id)
    check(
        "certification.content",
        row["certification_content_hash"] != context.content_ref.content_hash,
    )
    check(
        "certification.sha256",
        canonical_json_sha256(certification_record) != row["certification_sha256"],
    )
    check(
        "certification.certification_id",
        certification_record.get("certification_id") != skill_ref.certification_id,
    )
    check("certification.build_id", certification_record.get("build_id") != row["build_id"])
    check(
        "certification.command_id",
        certification_record.get("command_id") != row["build_command_id"],
    )
    check("certification.skill_id", certification_record.get("skill_id") != skill_ref.skill_id)
    check(
        "certification.skill_version_id",
        certification_record.get("skill_version_id") != skill_ref.skill_version_id,
    )
    check(
        "certification.artifact_sha256",
        certification_record.get("artifact_sha256") != skill_ref.artifact_sha256,
    )
    check(
        "certification.source_bundle_sha256",
        certification_record.get("source_bundle_sha256") != source_sha256,
    )
    check(
        "certification.build_policy_id",
        certification_record.get("build_policy_id") != row["build_policy_id"],
    )
    check(
        "certification.policy_sha256",
        certification_record.get("policy_sha256") != policy_sha256,
    )
    check(
        "certification.client_draft_revision",
        certification_record.get("client_draft_revision") != client_draft_revision,
    )
    check(
        "certification.compiler_profile",
        certification_record.get("compiler_profile") != row["build_compiler_profile"],
    )
    check(
        "certification.compiler_version",
        certification_record.get("compiler_version") != row["compiler_version"],
    )
    check(
        "certification.compiler_image",
        certification_record.get("compiler_image") != row["compiler_image"],
    )
    check(
        "certification.test_suite_version",
        certification_record.get("test_suite_version") != row["build_test_suite_version"],
    )
    check(
        "certification.runtime_abi_version",
        certification_record.get("runtime_abi_version") != row["runtime_abi_version"],
    )
    check(
        "certification.semantic_version",
        certification_record.get("semantic_version") != semantic_version,
    )
    check(
        "certification.parameter_schema",
        certification_record.get("parameter_schema") != expected_parameter_schema,
    )
    check("certification.tests", certification_record.get("tests") != expected_tests)
    check(
        "certification.requested_capabilities",
        certification_record.get("requested_capabilities") != requested_capabilities,
    )
    check(
        "certification.approved_capabilities",
        certification_record.get("approved_capabilities") != approved_capabilities,
    )
    check("certification.certified_at", certification_record.get("certified_at") != _iso(issued_at))
    check(
        "certification.request_context",
        certification_record.get("request_context") != _request_context_wire(skill.request_context),
    )
    check("build.status", row["build_status"] != "CERTIFIED")
    check("build.terminal", row["build_terminal"] is not True)
    check(
        "build.resource_sha256",
        canonical_json_sha256(build_resource) != row["build_resource_sha256"],
    )
    check("build.resource_status", build_resource.get("status") != "CERTIFIED")
    check("build.resource_terminal", build_resource.get("terminal") is not True)
    check("build.resource_build_id", build_resource.get("build_id") != row["build_id"])
    check("build.resource_skill_id", build_resource.get("skill_id") != skill_ref.skill_id)
    check(
        "build.resource_skill_version_id",
        build_resource.get("skill_version_id") != skill_ref.skill_version_id,
    )
    check(
        "build.request_context",
        build_resource.get("request_context") != certification_record.get("request_context"),
    )
    check("build.source_bundle_sha256", source_sha256 != row["source_bundle_sha256"])
    check("build.compiler_profile", row["build_compiler_profile"] != row["policy_compiler_profile"])
    check(
        "build.test_suite_version",
        row["build_test_suite_version"] != row["policy_test_suite_version"],
    )
    check(
        "build.artifact_sha256", build_artifact.get("artifact_sha256") != skill_ref.artifact_sha256
    )
    check("build.artifact_source", build_artifact.get("source_sha256") != source_sha256)
    check(
        "build.artifact_compiler_profile",
        build_artifact.get("compiler_profile") != row["build_compiler_profile"],
    )
    check(
        "build.artifact_compiler_version",
        build_artifact.get("compiler_version") != row["compiler_version"],
    )
    check(
        "build.artifact_test_suite",
        build_artifact.get("test_suite_version") != row["build_test_suite_version"],
    )
    check(
        "build.certification_id",
        build_certification.get("certification_id") != skill_ref.certification_id,
    )
    check(
        "build.certification_issued_at",
        build_certification.get("issued_at") != certification_record.get("certified_at"),
    )
    check(
        "build.certification_capabilities",
        build_certification.get("capabilities") != requested_capabilities,
    )
    check("build.versions", build_resource.get("versions") != certification_record.get("versions"))
    check("policy.sha256", policy_sha256 != row["policy_sha256"])
    check("artifact.source_sha256", row["artifact_source_sha256"] != source_sha256)
    check("artifact.uri", row["artifact_uri"] != expected_artifact_uri)
    check("artifact.metadata_size", not valid_size)
    for field, expected in (
        ("artifact_sha256", skill_ref.artifact_sha256),
        ("artifact_uri", expected_artifact_uri),
        ("source_sha256", source_sha256),
        ("build_policy_id", row["build_policy_id"]),
        ("policy_sha256", policy_sha256),
        ("compiler_profile", row["build_compiler_profile"]),
        ("compiler_version", row["compiler_version"]),
        ("compiler_image", row["compiler_image"]),
        ("test_suite_version", row["build_test_suite_version"]),
    ):
        check(f"artifact.metadata_{field}", artifact_metadata.get(field) != expected)
    check("legacy.certification_id", certification.certification_id != skill_ref.certification_id)
    check("legacy.skill_id", certification.skill_id != skill_ref.skill_id)
    check("legacy.skill_version_id", certification.skill_version_id != skill_ref.skill_version_id)
    check("legacy.semantic_version", certification.semantic_version != semantic_version)
    check(
        "legacy.artifact_sha256",
        certification.artifact.artifact_sha256 != skill_ref.artifact_sha256,
    )
    check("legacy.artifact_source", certification.artifact.source_sha256 != source_sha256)
    check(
        "legacy.artifact_compiler_profile",
        certification.artifact.compiler_profile != row["build_compiler_profile"],
    )
    check(
        "legacy.artifact_compiler_version",
        certification.artifact.compiler_version != row["compiler_version"],
    )
    check(
        "legacy.artifact_compiler_image",
        certification.artifact.sandbox_image_digest != row["compiler_image"],
    )
    check(
        "legacy.artifact_test_suite",
        certification.artifact.test_suite_version != row["build_test_suite_version"],
    )
    check("legacy.artifact_uri", certification.artifact.artifact_uri != expected_artifact_uri)
    check("legacy.capabilities", list(certification.capabilities) != requested_capabilities)
    check("legacy.certified_at", _iso(certification.certified_at) != _iso(issued_at))
    check("legacy.revoked_at", certification.revoked_at is not None)
    check("legacy.metadata", dict(certification.metadata) != expected_certification_metadata)
    check("skill.entrypoint", skill.entrypoint != entrypoint)
    check("skill.source_code", skill.source_code != entrypoint_file.get("content"))
    check("skill.source_sha256", skill.source_sha256 != entrypoint_file.get("content_sha256"))
    check(
        "skill.parameter_schema",
        _mapping(plain(skill.parameter_schema), "Skill parameter schema")
        != expected_parameter_schema,
    )
    check("skill.operation_context", not isinstance(skill.request_context, OperationContext))
    if isinstance(skill.request_context, OperationContext):
        check("skill.command_id", skill.request_context.command_id != row["build_command_id"])

    if drifts:
        raise ValueError(f"public Certification closure drifted: {', '.join(drifts)}")


def _scoped_identifier(prefix: str, *parts: str) -> str:
    framed = "".join(f"{len(part)}:{part}" for part in parts)
    return f"{prefix}_{hashlib.sha256(framed.encode('utf-8')).hexdigest()[:24]}"


def _retryable_context_failure(error: AgentContextError) -> bool:
    """Only dependency failures wrapped by AgentHub may be retried."""

    cause: BaseException | None = error.__cause__
    while cause is not None:
        if isinstance(cause, (psycopg.Error, AgentDependencyError, AgentPersistenceError)):
            return True
        cause = cause.__cause__
    return False


def _permanent_worker_failure(error: AgentPersistenceError) -> bool:
    if (
        error.code in _PERMANENT_WORKER_CODES
        or "MISMATCH" in error.code
        or "INVALID" in error.code
        or "INVARIANT" in error.code
    ):
        return True
    # AgentHub deliberately wraps port implementations behind stable runtime
    # errors.  A durable authority collision is nevertheless deterministic:
    # retrying the same poisoned event can never repair the stored identity and
    # would otherwise leave an ACCEPTED Command in an infinite READY loop.
    cause: BaseException | None = error.__cause__
    while cause is not None:
        cause_type = type(cause)
        if (
            cause_type.__name__ == "RepositoryAuthorityError"
            and cause_type.__module__ == "yaya_agent_backend.repositories"
        ):
            return True
        cause = cause.__cause__
    return False


def _validate_final_role_for_terminalization(record: CommittedAgentTurn) -> None:
    decision = record.decision
    if decision.role not in {"bug_agent", "book_agent"}:
        return
    directive = decision.teaching_directive
    valid = (
        decision.source == "provider"
        and not decision.degraded
        and decision.fallback_reason is None
        and directive is not None
        and not directive.patch_eligible
        and not directive.full_solution_eligible
        and decision.draft.skill_patch is None
        and not decision.draft.requires_student_confirmation
    )
    if directive is not None and decision.role == "bug_agent":
        valid = (
            valid
            and record.event.event_type in {"run_failed", "hint_requested"}
            and record.event.failure_count >= 3
            and decision.response_type == "question"
            and directive.phase is TeachingPhase.RECTIFICATION
            and directive.allowed_response_types == ("question",)
        )
    elif directive is not None:
        valid = (
            valid
            and record.event.event_type == "task_completed"
            and decision.response_type == "growth_summary"
            and directive.phase is TeachingPhase.SUMMARIZATION
            and directive.allowed_response_types == ("growth_summary",)
        )
    if not valid:
        raise AgentPersistenceError(
            "AGENT_FINAL_ROLE_INVARIANT_VIOLATION",
            "Bug/Book AgentTurn cannot be used to terminalize its Command",
        )


@dataclass(frozen=True, slots=True)
class HttpAttempt:
    request_id: str
    trace_id: str
    correlation_id: str
    requested_at: datetime
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        # RequestContext is the frozen authority for these exact formats.  A
        # temporary ContentRef is deliberately not fabricated here; the real
        # pinned value is resolved from the locked Session by accept().
        if self.requested_at.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware")
        if self.schema_version != "1.0.0":
            raise ValueError("unsupported schema_version")
        if _REQUEST_ID.fullmatch(self.request_id) is None:
            raise ValueError("request_id is invalid")
        if _TRACE_ID.fullmatch(self.trace_id) is None:
            raise ValueError("trace_id is invalid")
        if _CORRELATION_ID.fullmatch(self.correlation_id) is None:
            raise ValueError("correlation_id is invalid")


@dataclass(frozen=True, slots=True)
class AcceptedTurn:
    receipt: Mapping[str, object]
    command: CommandRecord
    operation_context: OperationContext
    replayed: bool


@dataclass(frozen=True, slots=True)
class ResourceResult:
    payload: Mapping[str, object]
    headers: Mapping[str, str]


class BackendApplicationError(RuntimeError):
    def __init__(
        self,
        code: str,
        http_status: int,
        stage: str,
        message: str,
        details: Mapping[str, object] | None = None,
        *,
        command_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.stage = stage
        self.details = {} if details is None else dict(details)
        self.command_id = command_id


def _error(
    code: str,
    stage: str,
    message: str,
    details: Mapping[str, object] | None = None,
    *,
    command_id: str | None = None,
) -> BackendApplicationError:
    statuses = {
        "INVALID_REQUEST": 400,
        "SCHEMA_VERSION_UNSUPPORTED": 409,
        "CONTENT_VERSION_MISMATCH": 409,
        "AUTHENTICATION_REQUIRED": 401,
        "AUTHORIZATION_DENIED": 403,
        "NOT_FOUND": 404,
        "PAYLOAD_TOO_LARGE": 413,
        "IDEMPOTENCY_KEY_REUSED": 409,
        "WORLD_REVISION_CONFLICT": 409,
        "EVENT_SEQUENCE_GAP": 409,
        "SKILL_NOT_CERTIFIED": 422,
        "SKILL_VERSION_MISMATCH": 409,
        "DEPENDENCY_UNAVAILABLE": 503,
        "UNKNOWN_COMMIT_STATE": 503,
        "INVARIANT_VIOLATION": 500,
        "INTERNAL_ERROR": 500,
    }
    return BackendApplicationError(
        code,
        statuses[code],
        stage,
        message,
        details,
        command_id=command_id,
    )


class AgentTurnApplication:
    """Atomically accept Agent turns and expose canonical query resources."""

    def __init__(
        self,
        database: PostgresDatabase,
        contracts_root: Path,
        versions: VersionSet,
    ) -> None:
        self._database = database
        self._validator = ContractSchemaValidator(contracts_root)
        self._versions = versions

    async def accept(
        self,
        *,
        actor: ActorRef,
        attempt: HttpAttempt,
        session_id: str,
        idempotency_key: str,
        raw_body: bytes,
        body: Mapping[str, object],
    ) -> AcceptedTurn:
        if not 2 <= len(raw_body) <= _MAX_HTTP_BODY_BYTES:
            code = (
                "PAYLOAD_TOO_LARGE" if len(raw_body) > _MAX_HTTP_BODY_BYTES else "INVALID_REQUEST"
            )
            raise _error(code, "ACCEPT", "Agent turn request body size is invalid")
        if _RESOURCE_ID.fullmatch(session_id) is None:
            raise _error("INVALID_REQUEST", "ACCEPT", "session_id is invalid")
        if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise _error("INVALID_REQUEST", "ACCEPT", "Idempotency-Key is invalid")
        try:
            parsed_body = _strict_json_body(raw_body)
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise _error("INVALID_REQUEST", "ACCEPT", "Request is not strict UTF-8 JSON") from error
        body_value = _mapping(body, "Agent turn body")
        if parsed_body != body_value:
            raise _error(
                "INVALID_REQUEST",
                "ACCEPT",
                "Parsed Agent turn body differs from the hashed request bytes",
            )
        try:
            self._validator.validate(
                "schemas/game/agent-turn-create-request.schema.json",
                body_value,
            )
        except ValueError as error:
            raise _error(
                "INVALID_REQUEST", "ACCEPT", "Agent turn body violates its schema"
            ) from error
        request_sha256 = hashlib.sha256(raw_body).hexdigest()
        command_id = _scoped_identifier(
            "cmd",
            actor.tenant_id,
            actor.actor_id,
            "EXECUTE_AGENT_TURN",
            idempotency_key,
        )

        # A fast replay lookup avoids taking the Session lock for ordinary
        # network retries.  The same lookup is repeated after locking Session
        # to close the absent-row race between two first attempts.
        replay = await self._lookup_acceptance(
            actor,
            session_id,
            idempotency_key,
            request_sha256,
            raw_body,
        )
        if replay is not None:
            return replay

        try:
            async with self._database.transaction_with_commit_boundary() as connection:
                session = await self._lock_session(connection, actor, session_id)
                replay = await self._lookup_acceptance_on(
                    connection,
                    actor,
                    session_id,
                    idempotency_key,
                    request_sha256,
                    raw_body,
                )
                if replay is not None:
                    return replay
                content_ref = session.request_context.content_ref
                context = OperationContext(
                    request_id=attempt.request_id,
                    correlation_id=attempt.correlation_id,
                    trace_id=attempt.trace_id,
                    requested_at=attempt.requested_at,
                    actor=actor,
                    content_ref=content_ref,
                    schema_version=attempt.schema_version,
                    command_id=command_id,
                    causation_id=None,
                )
                world_revision, world_sequence = await self._lock_world(
                    connection,
                    session,
                    context,
                )
                expected_revision = cast(int, body_value["expected_world_revision"])
                client_state = _mapping(body_value["client_state"], "client_state")
                expected_sequence = cast(int, client_state["last_event_sequence"])
                client_turn_sequence = cast(int, client_state["client_turn_sequence"])
                if expected_revision != world_revision:
                    raise _error(
                        "WORLD_REVISION_CONFLICT",
                        "WORLD_VALIDATE",
                        "Expected World revision is stale",
                        {"expected": expected_revision, "actual": world_revision},
                    )
                if expected_sequence != world_sequence:
                    raise _error(
                        "EVENT_SEQUENCE_GAP",
                        "WORLD_VALIDATE",
                        "Client World event cursor is stale",
                        {"expected": expected_sequence, "actual": world_sequence},
                    )
                sequence_cursor = await connection.execute(
                    """
                    SELECT client_turn_sequence FROM yaya_agent_sessions
                    WHERE tenant_id=%s AND session_id=%s
                    """,
                    (actor.tenant_id, session_id),
                )
                sequence_row = await sequence_cursor.fetchone()
                if sequence_row is None:
                    raise _error("NOT_FOUND", "ACCEPT", "Agent Session was not found")
                stored_sequence = cast(int, sequence_row["client_turn_sequence"])
                if client_turn_sequence != stored_sequence + 1:
                    raise _error(
                        "EVENT_SEQUENCE_GAP",
                        "ACCEPT",
                        "Client turn sequence is not the next accepted value",
                        {
                            "supplied": client_turn_sequence,
                            "expected": stored_sequence + 1,
                        },
                    )
                turn_id = cast(str, body_value["turn_id"])
                input_value = _mapping(body_value["input"], "input")
                if (
                    input_value.get("type") == "ASSIGNED_TASK"
                    and input_value.get("task_id") != session.task_id
                ):
                    raise _error(
                        "CONTENT_VERSION_MISMATCH",
                        "VALIDATE",
                        "Assigned task does not match the pinned Session task",
                    )
                bindings_value = body_value["skill_bindings"]
                if not isinstance(bindings_value, list):
                    raise _error(
                        "INVALID_REQUEST",
                        "REGISTRY",
                        "Skill bindings must be an array",
                    )
                bindings = cast(list[object], bindings_value)
                if len(bindings) != 1:
                    raise _error(
                        "SKILL_NOT_CERTIFIED",
                        "REGISTRY",
                        "This Agent turn requires exactly one certified Skill binding",
                    )
                binding = _mapping(bindings[0], "skill binding")
                skill_ref = SkillRef(
                    skill_id=cast(str, binding["skill_id"]),
                    skill_version_id=cast(str, binding["skill_version_id"]),
                    artifact_sha256=cast(str, binding["artifact_sha256"]),
                    certification_id=cast(str, binding["certification_id"]),
                )
                await self._validate_active_skill(
                    connection,
                    session,
                    skill_ref,
                    context,
                )
                clock_cursor = await connection.execute("SELECT clock_timestamp() AS value")
                clock_row = await clock_cursor.fetchone()
                if clock_row is None:
                    raise RuntimeError("PostgreSQL clock query returned no row")
                accepted_at = cast(datetime, clock_row["value"])
                command_versions = replace(
                    self._versions,
                    skill_version=skill_ref.skill_version_id,
                    artifact_sha256=skill_ref.artifact_sha256,
                )
                command = NewCommand(
                    command_type="EXECUTE_AGENT_TURN",
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    versions=command_versions,
                ).initial_record(context, accepted_at)
                self._validator.validate(
                    "schemas/game/command.schema.json",
                    _command_wire(command),
                )
                event = GameEvent(
                    event_id=_scoped_identifier("evt", command_id, turn_id),
                    event_type="run_skill_requested",
                    student_id=actor.actor_id,
                    task_id=session.task_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    command_id=command_id,
                    occurred_at=accepted_at,
                    expected_world_revision=expected_revision,
                    skill_ref=skill_ref,
                    payload=input_value,
                )
                job_id = _scoped_identifier("job", command_id)
                receipt: dict[str, object] = {
                    "job_id": job_id,
                    "job_type": "EXECUTE_AGENT_TURN",
                    "status": "ACCEPTED",
                    "created_at": _iso(accepted_at),
                    "updated_at": _iso(accepted_at),
                    "command_id": command_id,
                    "trace_id": attempt.trace_id,
                    "error": None,
                }
                self._validator.validate(
                    "schemas/game/accepted-game-job.schema.json",
                    receipt,
                )
                advanced = await connection.execute(
                    """
                    UPDATE yaya_agent_sessions SET client_turn_sequence=%s
                    WHERE tenant_id=%s AND session_id=%s AND actor_id=%s
                      AND content_hash=%s AND client_turn_sequence=%s
                    """,
                    (
                        client_turn_sequence,
                        actor.tenant_id,
                        session_id,
                        actor.actor_id,
                        content_ref.content_hash,
                        stored_sequence,
                    ),
                )
                if advanced.rowcount != 1:
                    raise _error(
                        "EVENT_SEQUENCE_GAP",
                        "ACCEPT",
                        "Session turn sequence CAS was lost",
                    )
                await connection.execute(
                    """
                    INSERT INTO yaya_commands(
                        tenant_id,actor_id,operation,idempotency_key,command_id,
                        session_id,turn_id,client_turn_sequence,request_sha256,
                        content_hash,revision,status,updated_at,record_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        actor.tenant_id,
                        actor.actor_id,
                        "EXECUTE_AGENT_TURN",
                        idempotency_key,
                        command_id,
                        session_id,
                        turn_id,
                        client_turn_sequence,
                        request_sha256,
                        content_ref.content_hash,
                        command.revision,
                        command.status.value,
                        command.updated_at,
                        Jsonb(encode(command)),
                    ),
                )
                await connection.execute(
                    """
                    INSERT INTO yaya_command_jobs(
                        tenant_id,command_id,job_id,actor_id,content_hash,session_id,
                        turn_id,client_turn_sequence,event_json,operation_context_json,
                        request_body,accepted_receipt_json,created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        actor.tenant_id,
                        command_id,
                        job_id,
                        actor.actor_id,
                        content_ref.content_hash,
                        session_id,
                        turn_id,
                        client_turn_sequence,
                        Jsonb(encode(event)),
                        Jsonb(encode(context)),
                        raw_body,
                        Jsonb(receipt),
                        accepted_at,
                    ),
                )
                return AcceptedTurn(receipt, command, context, False)
        except BackendApplicationError:
            raise
        except psycopg.errors.UniqueViolation as error:
            replay = await self._lookup_acceptance(
                actor,
                session_id,
                idempotency_key,
                request_sha256,
                raw_body,
            )
            if replay is not None:
                return replay
            raise _error(
                "EVENT_SEQUENCE_GAP",
                "ACCEPT",
                "Another request accepted this turn identity",
            ) from error
        except PostgresCommitStateUnknown as error:
            try:
                replay = await self._lookup_acceptance(
                    actor,
                    session_id,
                    idempotency_key,
                    request_sha256,
                    raw_body,
                )
            except BackendApplicationError:
                replay = None
            if replay is not None:
                # The current attempt originated the persisted acceptance;
                # preserve its first-attempt semantics even though COMMIT's
                # transport acknowledgement was lost.
                return replace(replay, replayed=False)
            raise _error(
                "UNKNOWN_COMMIT_STATE",
                "WORLD_COMMIT",
                "Command acceptance outcome requires reconciliation",
                {"exception_type": type(error.__cause__).__name__},
                command_id=command_id,
            ) from error
        except psycopg.IntegrityError as error:
            raise _error(
                "INVARIANT_VIOLATION",
                "ACCEPT",
                "PostgreSQL rejected an invalid durable Agent-turn record",
                {"sqlstate": error.sqlstate or "UNKNOWN"},
                command_id=command_id,
            ) from error
        except psycopg.Error as error:
            raise _error(
                "DEPENDENCY_UNAVAILABLE",
                "ACCEPT",
                "PostgreSQL is unavailable while accepting the Agent turn",
            ) from error

    async def _lookup_acceptance(
        self,
        actor: ActorRef,
        session_id: str,
        idempotency_key: str,
        request_sha256: str,
        raw_body: bytes,
    ) -> AcceptedTurn | None:
        connection: AsyncConnection[dict[str, object]] | None = None
        try:
            connection = await self._database.connect(autocommit=True)
            return await self._lookup_acceptance_on(
                connection,
                actor,
                session_id,
                idempotency_key,
                request_sha256,
                raw_body,
            )
        except BackendApplicationError:
            raise
        except psycopg.Error as error:
            raise _error(
                "DEPENDENCY_UNAVAILABLE",
                "ACCEPT",
                "PostgreSQL is unavailable during idempotency lookup",
            ) from error
        finally:
            if connection is not None:
                await connection.close()

    async def _lookup_acceptance_on(
        self,
        connection: AsyncConnection[dict[str, object]],
        actor: ActorRef,
        session_id: str,
        idempotency_key: str,
        request_sha256: str,
        raw_body: bytes,
    ) -> AcceptedTurn | None:
        cursor = await connection.execute(
            """
            SELECT c.command_id,c.content_hash,c.session_id AS command_session_id,
                   c.turn_id AS command_turn_id,
                   c.client_turn_sequence AS command_turn_sequence,
                   c.operation,c.request_sha256,c.record_json,
                   j.job_id,j.actor_id AS job_actor_id,j.content_hash AS job_content_hash,
                   j.session_id AS job_session_id,j.turn_id AS job_turn_id,
                   j.client_turn_sequence AS job_turn_sequence,j.event_json,
                   j.request_body,j.operation_context_json,j.accepted_receipt_json
            FROM yaya_commands c
            JOIN yaya_command_jobs j
              ON j.tenant_id=c.tenant_id AND j.command_id=c.command_id
            WHERE c.tenant_id=%s AND c.actor_id=%s
              AND c.operation='EXECUTE_AGENT_TURN' AND c.idempotency_key=%s
            """,
            (actor.tenant_id, actor.actor_id, idempotency_key),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if (
            row["request_sha256"] != request_sha256
            or row["command_session_id"] != session_id
            or row["request_body"] != raw_body
        ):
            raise _error(
                "IDEMPOTENCY_KEY_REUSED",
                "ACCEPT",
                "Idempotency key belongs to a different request",
            )
        stored_body = row["request_body"]
        if (
            not isinstance(stored_body, bytes)
            or hashlib.sha256(stored_body).hexdigest() != row["request_sha256"]
        ):
            raise _error(
                "INVARIANT_VIOLATION",
                "ACCEPT",
                "Persisted idempotency request bytes drifted",
            )
        command = decode_as(row["record_json"], CommandRecord)
        context = decode_as(row["operation_context_json"], OperationContext)
        event = decode_as(row["event_json"], GameEvent)
        if not _stable_actor(command.request_context.actor, actor) or not _stable_actor(
            context.actor, actor
        ):
            raise _error("NOT_FOUND", "ACCEPT", "Command was not found")
        receipt = _mapping(row["accepted_receipt_json"], "accepted receipt")
        if (
            row["operation"] != "EXECUTE_AGENT_TURN"
            or command.command_type != "EXECUTE_AGENT_TURN"
            or command.command_id != row["command_id"]
            or context.command_id != row["command_id"]
            or command.request_context.content_ref != context.content_ref
            or context.content_ref.content_hash != row["content_hash"]
            or row["job_actor_id"] != actor.actor_id
            or row["job_content_hash"] != row["content_hash"]
            or row["job_session_id"] != row["command_session_id"]
            or row["job_turn_id"] != row["command_turn_id"]
            or row["job_turn_sequence"] != row["command_turn_sequence"]
            or event.command_id != row["command_id"]
            or event.session_id != row["command_session_id"]
            or event.turn_id != row["command_turn_id"]
            or event.student_id != actor.actor_id
            or receipt.get("job_id") != row["job_id"]
            or receipt.get("command_id") != row["command_id"]
            or receipt.get("trace_id") != context.trace_id
        ):
            raise _error(
                "INVARIANT_VIOLATION",
                "ACCEPT",
                "Persisted accepted receipt identity drifted",
            )
        self._validator.validate("schemas/game/accepted-game-job.schema.json", receipt)
        return AcceptedTurn(receipt, command, context, True)

    @staticmethod
    async def _lock_session(
        connection: AsyncConnection[dict[str, object]],
        actor: ActorRef,
        session_id: str,
    ) -> SessionSnapshot:
        cursor = await connection.execute(
            """
            SELECT task_id,world_id,actor_id,content_hash,snapshot_json
            FROM yaya_agent_sessions
            WHERE tenant_id=%s AND session_id=%s AND actor_id=%s FOR UPDATE
            """,
            (actor.tenant_id, session_id, actor.actor_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise _error("NOT_FOUND", "ACCEPT", "Agent Session was not found")
        session = decode_as(row["snapshot_json"], SessionSnapshot)
        if (
            session.session_id != session_id
            or session.student_id != actor.actor_id
            or not _stable_actor(session.request_context.actor, actor)
        ):
            raise _error("NOT_FOUND", "ACCEPT", "Agent Session was not found")
        if (
            session.task_id != row["task_id"]
            or session.world_id != row["world_id"]
            or session.student_id != row["actor_id"]
            or session.request_context.content_ref.content_hash != row["content_hash"]
        ):
            raise _error(
                "INVARIANT_VIOLATION",
                "ACCEPT",
                "Session durable identity drifted",
            )
        return session

    async def _lock_world(
        self,
        connection: AsyncConnection[dict[str, object]],
        session: SessionSnapshot,
        context: OperationContext,
    ) -> tuple[int, int]:
        cursor = await connection.execute(
            """
            SELECT revision,last_event_sequence,world_rules_version,request_context_json
            FROM yaya_worlds
            WHERE tenant_id=%s AND world_id=%s AND actor_id=%s AND content_hash=%s
            FOR UPDATE
            """,
            (
                context.actor.tenant_id,
                session.world_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise _error("NOT_FOUND", "WORLD_VALIDATE", "Session World was not found")
        origin = decode_as(row["request_context_json"], RequestContext)
        if (
            not _stable_actor(origin.actor, context.actor)
            or origin.content_ref != context.content_ref
        ):
            raise _error("NOT_FOUND", "WORLD_VALIDATE", "Session World was not found")
        if row["world_rules_version"] != self._versions.world_rules_version:
            raise _error(
                "CONTENT_VERSION_MISMATCH",
                "WORLD_VALIDATE",
                "World rules differ from the production VersionSet",
            )
        return cast(int, row["revision"]), cast(int, row["last_event_sequence"])

    @staticmethod
    async def _validate_active_skill(
        connection: AsyncConnection[dict[str, object]],
        session: SessionSnapshot,
        skill_ref: SkillRef,
        context: OperationContext,
    ) -> None:
        scoped_cursor = await connection.execute(
            """
            SELECT s.snapshot_json,e.record_json AS active_json,
                   e.entry_sha256,e.revision AS active_revision,
                   e.activated_at AS active_activated_at,
                   c.record_json AS certification_json,
                   p.agent_profile_id,p.world_id AS public_world_id,
                   p.learner_id AS public_learner_id,
                   full_c.actor_id AS certification_actor_id,
                   full_c.content_hash AS certification_content_hash,
                   full_c.build_id,full_c.certification_sha256,
                   full_c.record_json AS full_certification_json,full_c.issued_at,
                   b.command_id AS build_command_id,b.status AS build_status,
                   b.terminal AS build_terminal,b.resource_json AS build_json,
                   b.resource_sha256 AS build_resource_sha256,
                   b.source_bundle_json,b.source_bundle_sha256,b.build_policy_id,
                   b.client_draft_revision,
                   b.compiler_profile AS build_compiler_profile,
                   b.test_suite_version AS build_test_suite_version,
                   b.requested_capabilities_json,
                   bp.compiler_profile AS policy_compiler_profile,
                   bp.test_suite_version AS policy_test_suite_version,
                   bp.compiler_image,bp.compiler_version,bp.compile_flags_json,
                   bp.public_tests_json,bp.hidden_tests_json,
                   bp.approved_capabilities_json,bp.limits_json,
                   bp.parameter_schema_json,bp.semantic_version_major,
                   bp.semantic_version_minor,bp.runtime_abi_version,bp.policy_sha256,
                   a.source_sha256 AS artifact_source_sha256,a.artifact_uri,
                   a.metadata_json AS artifact_metadata_json
            FROM yaya_public_agent_sessions p
            JOIN yaya_registry_heads h
              ON h.tenant_id=p.tenant_id AND h.actor_id=p.actor_id
             AND h.content_hash=p.content_hash AND h.world_id=p.world_id
             AND h.agent_profile_id=p.agent_profile_id AND h.skill_id=%s
            JOIN yaya_registry_entries e
              ON e.tenant_id=h.tenant_id AND e.actor_id=h.actor_id
             AND e.content_hash=h.content_hash AND e.world_id=h.world_id
             AND e.agent_profile_id=h.agent_profile_id AND e.skill_id=h.skill_id
             AND e.revision=h.revision
            JOIN yaya_skills s
              ON s.tenant_id=e.tenant_id AND s.actor_id=e.actor_id
             AND s.content_hash=e.content_hash AND s.skill_id=e.skill_id
             AND s.skill_version_id=e.skill_version_id
             AND s.certification_id=e.certification_id
             AND s.artifact_sha256=e.artifact_sha256
            JOIN yaya_registry_certifications c
              ON c.tenant_id=s.tenant_id AND c.certification_id=s.certification_id
             AND c.skill_id=s.skill_id AND c.skill_version_id=s.skill_version_id
             AND c.artifact_sha256=s.artifact_sha256 AND c.rejected=FALSE
            JOIN yaya_skill_certifications full_c
              ON full_c.tenant_id=s.tenant_id
             AND full_c.certification_id=s.certification_id
             AND full_c.skill_id=s.skill_id
             AND full_c.skill_version_id=s.skill_version_id
             AND full_c.artifact_sha256=s.artifact_sha256
             AND full_c.actor_id=s.actor_id AND full_c.content_hash=s.content_hash
            JOIN yaya_skill_builds b
              ON b.tenant_id=full_c.tenant_id AND b.build_id=full_c.build_id
             AND b.skill_id=full_c.skill_id AND b.actor_id=full_c.actor_id
             AND b.content_hash=full_c.content_hash
            JOIN yaya_build_policies bp
              ON bp.tenant_id=b.tenant_id AND bp.build_policy_id=b.build_policy_id
             AND bp.actor_id=b.actor_id AND bp.content_hash=b.content_hash
            JOIN yaya_artifacts a
              ON a.tenant_id=full_c.tenant_id
             AND a.artifact_sha256=full_c.artifact_sha256
             AND a.build_id=full_c.build_id AND a.skill_id=full_c.skill_id
             AND a.actor_id=full_c.actor_id AND a.content_hash=full_c.content_hash
            LEFT JOIN yaya_certification_revocations r
              ON r.tenant_id=full_c.tenant_id
             AND r.certification_id=full_c.certification_id
            WHERE p.tenant_id=%s AND p.session_id=%s AND p.actor_id=%s
              AND p.content_hash=%s AND p.world_id=%s AND p.task_id=%s
              AND p.status='ACTIVE'
              AND e.skill_id=%s AND e.skill_version_id=%s
              AND e.certification_id=%s AND e.artifact_sha256=%s
              AND r.certification_id IS NULL
            FOR NO KEY UPDATE OF p
            FOR KEY SHARE OF h,e,s,c,full_c,b,bp,a
            """,
            (
                skill_ref.skill_id,
                context.actor.tenant_id,
                session.session_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                session.world_id,
                session.task_id,
                skill_ref.skill_id,
                skill_ref.skill_version_id,
                skill_ref.certification_id,
                skill_ref.artifact_sha256,
            ),
        )
        scoped_row = await scoped_cursor.fetchone()
        if scoped_row is not None:
            try:
                skill = decode_as(scoped_row["snapshot_json"], SkillSnapshot)
                certification = decode_as(scoped_row["certification_json"], CertifiedSkill)
                active_revision = scoped_row["active_revision"]
                active_activated_at = scoped_row["active_activated_at"]
                if (
                    isinstance(active_revision, bool)
                    or not isinstance(active_revision, int)
                    or not isinstance(active_activated_at, datetime)
                    or active_activated_at.tzinfo is None
                    or active_activated_at.utcoffset() is None
                ):
                    raise ValueError("public Registry entry authority is invalid")
                active = ActiveSkill(
                    skill=certification,
                    registry_revision=active_revision,
                    activated_at=active_activated_at.astimezone(UTC),
                )
                _validate_public_certification_closure(
                    scoped_row,
                    skill=skill,
                    active=active,
                    certification=certification,
                    skill_ref=skill_ref,
                    context=context,
                )
            except (TypeError, ValueError) as error:
                raise _error(
                    "SKILL_VERSION_MISMATCH",
                    "REGISTRY",
                    "Full-scope Skill Build and Certification authority drifted",
                ) from error
            binding_id = _scoped_identifier(
                "binding",
                context.actor.tenant_id,
                session.session_id,
                skill_ref.skill_id,
                skill_ref.skill_version_id,
            )
            binding_projection: dict[str, object] = {
                "binding_id": binding_id,
                "session_id": session.session_id,
                "skill_id": skill_ref.skill_id,
                "skill_version_id": skill_ref.skill_version_id,
                "certification_id": skill_ref.certification_id,
                "artifact_sha256": skill_ref.artifact_sha256,
                "actor_id": context.actor.actor_id,
                "content_hash": context.content_ref.content_hash,
            }
            binding_sha256 = canonical_json_sha256(binding_projection)
            await connection.execute(
                """
                INSERT INTO yaya_session_skill_versions(
                    tenant_id,binding_id,session_id,skill_id,skill_version_id,
                    certification_id,artifact_sha256,actor_id,content_hash,
                    binding_sha256,bound_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id,session_id,skill_id,skill_version_id)
                DO NOTHING
                """,
                (
                    context.actor.tenant_id,
                    binding_id,
                    session.session_id,
                    skill_ref.skill_id,
                    skill_ref.skill_version_id,
                    skill_ref.certification_id,
                    skill_ref.artifact_sha256,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    binding_sha256,
                    context.requested_at,
                ),
            )
            binding_cursor = await connection.execute(
                """
                SELECT binding_id,certification_id,artifact_sha256,actor_id,
                       content_hash,binding_sha256
                FROM yaya_session_skill_versions
                WHERE tenant_id=%s AND session_id=%s AND skill_id=%s
                  AND skill_version_id=%s
                """,
                (
                    context.actor.tenant_id,
                    session.session_id,
                    skill_ref.skill_id,
                    skill_ref.skill_version_id,
                ),
            )
            binding = await binding_cursor.fetchone()
            if (
                binding is None
                or binding["binding_id"] != binding_id
                or binding["certification_id"] != skill_ref.certification_id
                or binding["artifact_sha256"] != skill_ref.artifact_sha256
                or binding["actor_id"] != context.actor.actor_id
                or binding["content_hash"] != context.content_ref.content_hash
                or binding["binding_sha256"] != binding_sha256
            ):
                raise _error(
                    "INVARIANT_VIOLATION",
                    "REGISTRY",
                    "Session SkillVersion binding drifted",
                )
            return

        public_cursor = await connection.execute(
            """
            SELECT 1 FROM yaya_public_agent_sessions
            WHERE tenant_id=%s AND session_id=%s
            """,
            (
                context.actor.tenant_id,
                session.session_id,
            ),
        )
        if await public_cursor.fetchone() is not None:
            raise _error(
                "SKILL_NOT_CERTIFIED",
                "REGISTRY",
                "Skill binding is not active in the Session full-scope Registry",
            )

        # Explicit compatibility path for pre-A8 A6 fixtures.  Public-chain
        # Sessions always have yaya_public_agent_sessions and can never fall
        # back to this actor-only legacy projection.
        cursor = await connection.execute(
            """
            SELECT s.snapshot_json,a.record_json AS active_json,a.revision AS active_revision,
                   c.record_json AS certification_json
            FROM yaya_skills s
            JOIN yaya_registry_active a
              ON a.tenant_id=s.tenant_id AND a.actor_id=s.actor_id
             AND a.skill_id=s.skill_id
            JOIN yaya_registry_certifications c
              ON c.tenant_id=s.tenant_id AND c.certification_id=s.certification_id
             AND c.skill_id=s.skill_id AND c.skill_version_id=s.skill_version_id
             AND c.artifact_sha256=s.artifact_sha256 AND c.rejected=FALSE
            WHERE s.tenant_id=%s AND s.actor_id=%s AND s.content_hash=%s
              AND s.session_id=%s AND s.skill_id=%s AND s.skill_version_id=%s
              AND s.certification_id=%s AND s.artifact_sha256=%s
            FOR KEY SHARE OF s, a, c
            """,
            (
                context.actor.tenant_id,
                context.actor.actor_id,
                context.content_ref.content_hash,
                session.session_id,
                skill_ref.skill_id,
                skill_ref.skill_version_id,
                skill_ref.certification_id,
                skill_ref.artifact_sha256,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise _error(
                "SKILL_NOT_CERTIFIED",
                "REGISTRY",
                "Skill binding is not active and certified for this Session",
            )
        skill = decode_as(row["snapshot_json"], SkillSnapshot)
        active = decode_as(row["active_json"], ActiveSkill)
        certification = decode_as(row["certification_json"], CertifiedSkill)
        if (
            skill.ref != skill_ref
            or active.skill != certification
            or active.registry_revision != row["active_revision"]
            or active.skill.skill_id != skill_ref.skill_id
            or active.skill.skill_version_id != skill_ref.skill_version_id
            or active.skill.certification_id != skill_ref.certification_id
            or active.skill.artifact.artifact_sha256 != skill_ref.artifact_sha256
            or not _stable_actor(skill.request_context.actor, context.actor)
            or skill.request_context.content_ref != context.content_ref
        ):
            raise _error(
                "SKILL_VERSION_MISMATCH",
                "REGISTRY",
                "Skill registry records do not match the requested binding",
            )

    async def get_command(self, command_id: str, actor: ActorRef) -> ResourceResult:
        row = await self._read_scoped_row(
            """
            SELECT revision,status,content_hash,record_json FROM yaya_commands
            WHERE tenant_id=%s AND command_id=%s AND actor_id=%s
            """,
            (actor.tenant_id, command_id, actor.actor_id),
        )
        record = decode_as(row["record_json"], CommandRecord)
        if record.command_id != command_id or not _stable_actor(
            record.request_context.actor, actor
        ):
            raise _error("NOT_FOUND", "VALIDATE", "Command was not found")
        if (
            record.revision != row["revision"]
            or record.status.value != row["status"]
            or record.request_context.content_ref.content_hash != row["content_hash"]
            or record.links.get("self") != f"/v1/commands/{command_id}"
        ):
            raise _error(
                "INVARIANT_VIOLATION",
                "VALIDATE",
                "Command durable identity drifted",
            )
        payload = _command_wire(record)
        self._validator.validate("schemas/game/command.schema.json", payload)
        return ResourceResult(payload, {})

    async def get_run(self, run_id: str, actor: ActorRef) -> ResourceResult:
        row = await self._read_scoped_row(
            """
            SELECT actor_id,content_hash,session_id,turn_id,command_id,world_id,
                   skill_version_id,failure_key,task_success,snapshot_json,wire_json
            FROM yaya_runs
            WHERE tenant_id=%s AND run_id=%s AND actor_id=%s
            """,
            (actor.tenant_id, run_id, actor.actor_id),
        )
        payload = _mapping(row["wire_json"], "Run wire")
        from yaya_agent_runtime import RunResultSnapshot

        snapshot = decode_as(row["snapshot_json"], RunResultSnapshot)
        skill_wire = _mapping(payload.get("skill"), "Run skill")
        snapshot_skill = _mapping(plain(snapshot.skill_ref), "Run snapshot skill")
        snapshot_origin = _request_context_wire(snapshot.request_context)
        origin = _mapping(payload.get("request_context"), "Run request_context")
        if (
            payload.get("run_id") != run_id
            or snapshot.run_id != run_id
            or snapshot.session_id != row["session_id"]
            or snapshot.turn_id != row["turn_id"]
            or snapshot.command_id != row["command_id"]
            or snapshot.world_id != row["world_id"]
            or snapshot.skill_ref.skill_version_id != row["skill_version_id"]
            or snapshot.task_success != row["task_success"]
            or snapshot.failure_key != row["failure_key"]
            or snapshot.request_context.content_ref.content_hash != row["content_hash"]
            or payload.get("session_id") != row["session_id"]
            or payload.get("turn_id") != row["turn_id"]
            or payload.get("command_id") != row["command_id"]
            or skill_wire != snapshot_skill
            or origin != snapshot_origin
        ):
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Run identity drifted")
        origin_actor = _mapping(origin.get("actor"), "Run actor")
        if (
            origin_actor.get("tenant_id") != actor.tenant_id
            or origin_actor.get("actor_id") != actor.actor_id
            or origin_actor.get("actor_type") != actor.actor_type.value
        ):
            raise _error("NOT_FOUND", "VALIDATE", "Run was not found")
        wire_evidence = payload.get("evidence_refs")
        snapshot_evidence = [_evidence_ref_wire(reference) for reference in snapshot.evidence_refs]
        if wire_evidence != snapshot_evidence:
            raise _error(
                "INVARIANT_VIOLATION",
                "VALIDATE",
                "Run Evidence differs from its typed snapshot",
            )
        world_application = _mapping(
            payload.get("world_application"),
            "Run world_application",
        )
        wire_receipt = world_application.get("receipt")
        if wire_receipt != plain(snapshot.world_commit):
            raise _error(
                "INVARIANT_VIOLATION",
                "VALIDATE",
                "Run World receipt differs from its typed snapshot",
            )
        if isinstance(wire_receipt, Mapping):
            receipt = _mapping(
                cast(Mapping[object, object], wire_receipt),
                "Run World receipt",
            )
            previous_revision = receipt.get("previous_revision")
            world_revision = receipt.get("world_revision")
            first_sequence = receipt.get("first_event_sequence")
            last_sequence = receipt.get("last_event_sequence")
            if (
                receipt.get("world_id") != snapshot.world_id
                or previous_revision != snapshot.world_revision_before
                or world_revision != snapshot.world_revision_after
                or isinstance(previous_revision, bool)
                or not isinstance(previous_revision, int)
                or isinstance(world_revision, bool)
                or not isinstance(world_revision, int)
                or world_revision != previous_revision + 1
                or isinstance(first_sequence, bool)
                or not isinstance(first_sequence, int)
                or isinstance(last_sequence, bool)
                or not isinstance(last_sequence, int)
                or first_sequence > last_sequence
            ):
                raise _error(
                    "INVARIANT_VIOLATION",
                    "VALIDATE",
                    "Run World receipt violates its executable invariants",
                )
        feedback_value = payload.get("agent_feedback")
        if feedback_value is not None:
            feedback = _mapping(feedback_value, "Run agent_feedback")
            if (
                feedback.get("session_id") != snapshot.session_id
                or feedback.get("turn_id") != snapshot.turn_id
                or feedback.get("command_id") != snapshot.command_id
                or feedback.get("run_id") != snapshot.run_id
                or feedback.get("evidence_refs") != wire_evidence
            ):
                raise _error(
                    "INVARIANT_VIOLATION",
                    "VALIDATE",
                    "Run feedback identity or Evidence set drifted",
                )
        self._validator.validate("schemas/game/run.schema.json", payload)
        return ResourceResult(payload, {})

    async def get_world(self, world_id: str, actor: ActorRef) -> ResourceResult:
        row = await self._read_scoped_row(
            """
            SELECT revision,last_event_sequence,state_hash,world_rules_version,content_hash,
                   state_json,request_context_json,updated_at
            FROM yaya_worlds
            WHERE tenant_id=%s AND world_id=%s AND actor_id=%s
            """,
            (actor.tenant_id, world_id, actor.actor_id),
        )
        origin = decode_as(row["request_context_json"], RequestContext)
        if (
            not _stable_actor(origin.actor, actor)
            or origin.content_ref.content_hash != row["content_hash"]
        ):
            raise _error("NOT_FOUND", "WORLD_VALIDATE", "World was not found")
        state = _mapping(row["state_json"], "World state")
        if canonical_json_sha256(state) != row["state_hash"]:
            raise _error("INVARIANT_VIOLATION", "WORLD_VALIDATE", "World state hash drifted")
        payload: dict[str, object] = {
            "request_context": plain(origin),
            "world_id": world_id,
            "revision": row["revision"],
            "last_event_sequence": row["last_event_sequence"],
            "state_schema_version": "1.0.0",
            "state_hash": row["state_hash"],
            "generated_at": _iso(cast(datetime, row["updated_at"])),
            "world_rules_version": row["world_rules_version"],
            "state": state,
        }
        self._validator.validate("schemas/game/world-snapshot.schema.json", payload)
        etag = f'"{world_id}:{row["revision"]}:{row["state_hash"]}"'
        return ResourceResult(
            payload,
            {"ETag": etag, "X-World-Revision": str(row["revision"])},
        )

    async def get_evidence(self, evidence_id: str, actor: ActorRef) -> ResourceResult:
        row = await self._read_scoped_row(
            """
            SELECT content_hash,evidence_type,payload_sha256,evidence_json FROM yaya_evidence
            WHERE tenant_id=%s AND evidence_id=%s AND actor_id=%s
            """,
            (actor.tenant_id, evidence_id, actor.actor_id),
        )
        payload = _mapping(row["evidence_json"], "Evidence wire")
        reference = _mapping(payload.get("evidence_ref"), "Evidence reference")
        origin = _mapping(payload.get("request_context"), "Evidence request_context")
        origin_actor = _mapping(origin.get("actor"), "Evidence actor")
        if (
            reference.get("evidence_id") != evidence_id
            or origin_actor.get("tenant_id") != actor.tenant_id
            or origin_actor.get("actor_id") != actor.actor_id
            or origin_actor.get("actor_type") != actor.actor_type.value
        ):
            raise _error("NOT_FOUND", "VALIDATE", "Evidence was not found")
        integrity = _mapping(payload.get("integrity"), "Evidence integrity")
        evidence_payload = _mapping(payload.get("payload"), "Evidence payload")
        if (
            integrity.get("payload_sha256") != row["payload_sha256"]
            or reference.get("sha256") != row["payload_sha256"]
            or reference.get("evidence_type") != row["evidence_type"]
            or _mapping(origin.get("content_ref"), "Evidence content_ref").get("content_hash")
            != row["content_hash"]
            or canonical_json_sha256(evidence_payload) != row["payload_sha256"]
        ):
            raise _error("INVARIANT_VIOLATION", "VALIDATE", "Evidence hash drifted")
        if evidence_payload.get("evidence_kind") == "WORLD_COMMIT":
            source = _mapping(payload.get("source"), "Evidence source")
            previous_revision = evidence_payload.get("previous_revision")
            world_revision = evidence_payload.get("world_revision")
            first_sequence = evidence_payload.get("first_event_sequence")
            last_sequence = evidence_payload.get("last_event_sequence")
            if (
                source.get("source_id") != source.get("world_id")
                or source.get("world_id") != evidence_payload.get("world_id")
                or isinstance(previous_revision, bool)
                or not isinstance(previous_revision, int)
                or isinstance(world_revision, bool)
                or not isinstance(world_revision, int)
                or world_revision != previous_revision + 1
                or isinstance(first_sequence, bool)
                or not isinstance(first_sequence, int)
                or isinstance(last_sequence, bool)
                or not isinstance(last_sequence, int)
                or first_sequence > last_sequence
            ):
                raise _error(
                    "INVARIANT_VIOLATION",
                    "VALIDATE",
                    "World commit Evidence violates its executable invariants",
                )
        self._validator.validate("schemas/game/evidence.schema.json", payload)
        return ResourceResult(payload, {"ETag": f'"{row["payload_sha256"]}"'})

    async def list_world_events(
        self,
        world_id: str,
        actor: ActorRef,
        *,
        after_sequence: int,
        limit: int = 100,
    ) -> ResourceResult:
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise _error("INVALID_REQUEST", "VALIDATE", "World event cursor is invalid")
        try:
            async with self._database.transaction() as connection:
                await connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                world_cursor = await connection.execute(
                    """
                    SELECT revision,last_event_sequence,stream_id,content_hash,
                           request_context_json
                    FROM yaya_worlds
                    WHERE tenant_id=%s AND world_id=%s AND actor_id=%s
                    """,
                    (actor.tenant_id, world_id, actor.actor_id),
                )
                world = await world_cursor.fetchone()
                if world is None:
                    raise _error("NOT_FOUND", "VALIDATE", "World was not found")
                origin = decode_as(world["request_context_json"], RequestContext)
                if (
                    not _stable_actor(origin.actor, actor)
                    or origin.content_ref.content_hash != world["content_hash"]
                ):
                    raise _error("NOT_FOUND", "VALIDATE", "World was not found")
                if world["stream_id"] != f"world:{world_id}":
                    raise _error(
                        "INVARIANT_VIOLATION",
                        "VALIDATE",
                        "World stream identity is not canonical",
                    )
                if after_sequence > cast(int, world["last_event_sequence"]):
                    raise _error(
                        "EVENT_SEQUENCE_GAP",
                        "VALIDATE",
                        "World event cursor is ahead of the canonical stream",
                        {
                            "supplied": after_sequence,
                            "actual": world["last_event_sequence"],
                        },
                    )
                event_cursor = await connection.execute(
                    """
                    SELECT sequence,event_json FROM yaya_events
                    WHERE tenant_id=%s AND stream_id=%s AND sequence>%s
                    ORDER BY sequence LIMIT %s
                    """,
                    (actor.tenant_id, world["stream_id"], after_sequence, limit + 1),
                )
                rows = list(await event_cursor.fetchall())
        except BackendApplicationError:
            raise
        except psycopg.Error as error:
            raise _error(
                "DEPENDENCY_UNAVAILABLE",
                "VALIDATE",
                "PostgreSQL is unavailable while reading World events",
            ) from error
        selected = rows[:limit]
        events = [_mapping(row["event_json"], "World event") for row in selected]
        expected_count = min(
            max(0, cast(int, world["last_event_sequence"]) - after_sequence),
            limit,
        )
        if len(events) != expected_count:
            raise _error(
                "EVENT_SEQUENCE_GAP",
                "VALIDATE",
                "World event stream is missing a durable sequence",
            )
        if events:
            expected = after_sequence + 1
            event_ids: set[str] = set()
            for row, event in zip(selected, events, strict=True):
                event_id = event.get("event_id")
                if (
                    not isinstance(event_id, str)
                    or event_id in event_ids
                    or event.get("stream_id") != f"world:{world_id}"
                    or event.get("sequence") != expected
                    or row["sequence"] != expected
                ):
                    raise _error(
                        "EVENT_SEQUENCE_GAP",
                        "VALIDATE",
                        "World event page is not contiguous",
                    )
                event_ids.add(event_id)
                expected += 1
            from_sequence = cast(int, events[0]["sequence"])
            to_sequence = cast(int, events[-1]["sequence"])
            next_sequence = to_sequence
        else:
            from_sequence = after_sequence
            to_sequence = after_sequence
            next_sequence = after_sequence
        payload: dict[str, object] = {
            "request_context": plain(origin),
            "world_id": world_id,
            "snapshot_revision": world["revision"],
            "from_sequence": from_sequence,
            "to_sequence": to_sequence,
            "has_more": len(rows) > limit,
            "next_after_sequence": next_sequence,
            "events": events,
        }
        self._validator.validate("schemas/game/world-event-page.schema.json", payload)
        return ResourceResult(
            payload,
            {"X-World-Revision": str(world["revision"])},
        )

    async def _read_scoped_row(
        self,
        query: str,
        parameters: tuple[object, ...],
    ) -> dict[str, object]:
        connection: AsyncConnection[dict[str, object]] | None = None
        row: dict[str, object] | None = None
        try:
            connection = await self._database.connect(autocommit=True)
            cursor = await connection.execute(query, parameters)  # pyright: ignore[reportArgumentType]
            row = await cursor.fetchone()
        except psycopg.Error as error:
            raise _error(
                "DEPENDENCY_UNAVAILABLE",
                "VALIDATE",
                "PostgreSQL is unavailable while reading a resource",
            ) from error
        finally:
            if connection is not None:
                try:
                    await connection.close()
                except psycopg.Error as error:
                    raise _error(
                        "DEPENDENCY_UNAVAILABLE",
                        "VALIDATE",
                        "PostgreSQL connection could not close cleanly",
                    ) from error
        if row is None:
            raise _error("NOT_FOUND", "VALIDATE", "Resource was not found")
        return row


@dataclass(frozen=True, slots=True)
class WorkerLease:
    tenant_id: str
    command_id: str
    lease_id: str
    lease_seconds: int
    event: GameEvent
    context: OperationContext


class AgentTurnWorker:
    """Persistent, restart-safe job worker with PostgreSQL fencing tokens."""

    def __init__(
        self,
        *,
        database: PostgresDatabase,
        hub: AgentHub,
        validator: ContractSchemaValidator,
        worker_id: str,
        configured_lease_seconds: int,
        poll_ms: int,
        runtime_budget_ms: int,
        outcome_authority: PostgresRunOutcomeAuthority | None = None,
    ) -> None:
        if configured_lease_seconds < 2 or poll_ms < 10:
            raise ValueError("worker lease and poll interval are invalid")
        self._database = database
        self._hub = hub
        self._validator = validator
        self._worker_id = worker_id
        self._poll_ms = poll_ms
        self._outcome_authority = outcome_authority
        # A job must never become takeable while its AgentTurn claim and
        # bounded Runtime are still executing. Heartbeats further reduce crash
        # recovery latency, but this lower bound is the safety invariant.
        self._lease_seconds = max(
            configured_lease_seconds,
            math.ceil(runtime_budget_ms / 1000) + 15,
        )

    async def claim_one(self) -> WorkerLease | None:
        lease_id = f"lease_{uuid.uuid4().hex}"
        try:
            async with self._database.transaction() as connection:
                cursor = await connection.execute(
                    """
                    SELECT j.tenant_id,j.command_id,j.actor_id,j.content_hash,
                           j.session_id,j.turn_id,j.client_turn_sequence,
                           j.event_json,j.operation_context_json,
                           c.record_json AS command_json,c.revision AS command_revision,
                           c.status AS command_status,
                           s.snapshot_json AS session_json,s.world_id AS session_world_id
                    FROM yaya_command_jobs j
                    JOIN yaya_commands c
                      ON c.tenant_id=j.tenant_id AND c.command_id=j.command_id
                     AND c.actor_id=j.actor_id AND c.content_hash=j.content_hash
                     AND c.session_id=j.session_id AND c.turn_id=j.turn_id
                     AND c.client_turn_sequence=j.client_turn_sequence
                    JOIN yaya_agent_sessions s
                      ON s.tenant_id=j.tenant_id AND s.session_id=j.session_id
                     AND s.actor_id=j.actor_id AND s.content_hash=j.content_hash
                    WHERE (
                        (j.state='READY' AND j.available_at<=clock_timestamp())
                        OR (j.state='LEASED' AND j.lease_expires_at<=clock_timestamp())
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM yaya_command_jobs prior_j
                        JOIN yaya_commands prior_c
                          ON prior_c.tenant_id=prior_j.tenant_id
                         AND prior_c.command_id=prior_j.command_id
                         AND prior_c.actor_id=prior_j.actor_id
                         AND prior_c.content_hash=prior_j.content_hash
                         AND prior_c.session_id=prior_j.session_id
                         AND prior_c.turn_id=prior_j.turn_id
                         AND prior_c.client_turn_sequence=prior_j.client_turn_sequence
                        WHERE prior_j.tenant_id=j.tenant_id
                          AND prior_j.session_id=j.session_id
                          AND prior_j.client_turn_sequence<j.client_turn_sequence
                          AND (
                            prior_j.state<>'DONE'
                            OR prior_c.status NOT IN (
                              'APPLIED','REJECTED','FAILED','UNKNOWN','CANCELLED'
                            )
                            OR (
                              prior_c.record_json #>> '{$fields,status,$value}'
                            ) IS DISTINCT FROM prior_c.status
                            OR (
                              prior_c.record_json #>> '{$fields,terminal}'
                            ) IS DISTINCT FROM 'true'
                            OR (
                              prior_c.record_json #>> '{$fields,revision}'
                            ) IS DISTINCT FROM prior_c.revision::text
                          )
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM yaya_command_jobs active_j
                        WHERE active_j.tenant_id=j.tenant_id
                          AND active_j.session_id=j.session_id
                          AND active_j.command_id<>j.command_id
                          AND active_j.state='LEASED'
                          AND active_j.lease_expires_at>clock_timestamp()
                      )
                    ORDER BY j.available_at,j.command_id
                    FOR UPDATE OF j SKIP LOCKED LIMIT 1
                    """
                )
                row = await cursor.fetchone()
                if row is None:
                    return None
                event = decode_as(row["event_json"], GameEvent)
                context = decode_as(row["operation_context_json"], OperationContext)
                command = decode_as(row["command_json"], CommandRecord)
                session = decode_as(row["session_json"], SessionSnapshot)
                if (
                    event.command_id != row["command_id"]
                    or event.session_id != row["session_id"]
                    or event.turn_id != row["turn_id"]
                    or event.student_id != row["actor_id"]
                    or context.command_id != row["command_id"]
                    or context.actor.tenant_id != row["tenant_id"]
                    or context.actor.actor_id != row["actor_id"]
                    or context.content_ref.content_hash != row["content_hash"]
                    or command.command_id != row["command_id"]
                    or command.revision != row["command_revision"]
                    or command.status.value != row["command_status"]
                    or not _stable_actor(command.request_context.actor, context.actor)
                    or command.request_context.content_ref != context.content_ref
                    or session.session_id != row["session_id"]
                    or session.student_id != row["actor_id"]
                    or session.task_id != event.task_id
                    or session.world_id != row["session_world_id"]
                    or not _stable_actor(session.request_context.actor, context.actor)
                    or session.request_context.content_ref != context.content_ref
                ):
                    raise RuntimeError("claimable Job identity drifted")
                updated = await connection.execute(
                    """
                    UPDATE yaya_command_jobs
                    SET state='LEASED',attempt=attempt+1,worker_id=%s,lease_id=%s,
                        lease_expires_at=clock_timestamp()+%s*interval '1 second',
                        last_error_code=NULL
                    WHERE tenant_id=%s AND command_id=%s
                    RETURNING tenant_id
                    """,
                    (
                        self._worker_id,
                        lease_id,
                        self._lease_seconds,
                        row["tenant_id"],
                        row["command_id"],
                    ),
                )
                if await updated.fetchone() is None:
                    raise RuntimeError("leased job disappeared while locked")
                return WorkerLease(
                    tenant_id=cast(str, row["tenant_id"]),
                    command_id=cast(str, row["command_id"]),
                    lease_id=lease_id,
                    lease_seconds=self._lease_seconds,
                    event=event,
                    context=context,
                )
        except psycopg.Error as error:
            raise AgentPersistenceError(
                "AGENT_JOB_CLAIM_FAILED",
                "PostgreSQL could not claim an Agent job",
                {"exception_type": type(error).__name__},
            ) from error

    async def run_once(self) -> bool:
        lease = await self.claim_one()
        if lease is None:
            return False
        stop_heartbeat = asyncio.Event()
        lost_lease = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(lease, stop_heartbeat, lost_lease),
            name=f"agent-job-heartbeat:{lease.command_id}",
        )
        work = asyncio.create_task(
            self._process(lease),
            name=f"agent-job:{lease.command_id}",
        )
        lost_wait = asyncio.create_task(lost_lease.wait())
        try:
            done, _ = await asyncio.wait(
                {work, lost_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_wait in done and lost_lease.is_set() and not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                return True
            await work
            return True
        except asyncio.CancelledError:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise
        except AgentContextError as error:
            if _retryable_context_failure(error):
                try:
                    await self._release_for_retry(lease, error.code)
                except Exception:
                    pass
                return True
            try:
                await self._fail_permanent(lease, error.code)
            except Exception:
                # A lost fence leaves the newer owner responsible for recovery.
                pass
            return True
        except AgentDependencyError as error:
            try:
                await self._fail_permanent(lease, error.code)
            except Exception:
                pass
            return True
        except AgentPersistenceError as error:
            if error.code == "AGENT_JOB_FENCE_LOST":
                return True
            if _permanent_worker_failure(error):
                try:
                    await self._fail_permanent(lease, error.code)
                except Exception:
                    pass
                return True
            try:
                await self._release_for_retry(lease, error.code)
            except Exception:
                pass
            return True
        except (psycopg.Error, TimeoutError, ConnectionError) as error:
            try:
                await self._release_for_retry(lease, type(error).__name__)
            except Exception:
                pass
            return True
        except Exception as error:
            try:
                await self._fail_permanent(lease, type(error).__name__)
            except Exception:
                pass
            return True
        finally:
            lost_wait.cancel()
            stop_heartbeat.set()
            await asyncio.gather(heartbeat, lost_wait, return_exceptions=True)

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except AgentPersistenceError:
                # Claim-time dependency failures occur before a lease exists;
                # bounded polling keeps the durable worker alive until
                # PostgreSQL recovers without fabricating a terminal Command.
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), self._poll_ms / 1000)
                except TimeoutError:
                    pass

    async def _process(self, lease: WorkerLease) -> None:
        if self._outcome_authority is None:
            await self.process_claimed_event(lease, lease.event)
            return
        root_result = await self._process_claimed_event(
            lease,
            lease.event,
            finalize=False,
        )
        if root_result is None:
            return
        derived_event = await self._outcome_authority.derive(
            worker_id=self._worker_id,
            lease_id=lease.lease_id,
            root_event=lease.event,
            context=lease.context,
        )
        await self._process_claimed_event(lease, derived_event, finalize=True)

    async def process_claimed_event(
        self,
        lease: WorkerLease,
        event: GameEvent,
    ) -> AgentHubResult | None:
        """Process a root or derived Agent event under one durable Command lease."""

        return await self._process_claimed_event(lease, event, finalize=True)

    async def _process_claimed_event(
        self,
        lease: WorkerLease,
        event: GameEvent,
        *,
        finalize: bool,
    ) -> AgentHubResult | None:

        if (
            event.command_id != lease.command_id
            or event.student_id != lease.context.actor.actor_id
            or event.session_id != lease.event.session_id
            or event.turn_id != lease.event.turn_id
            or event.task_id != lease.event.task_id
            or event.expected_world_revision != lease.event.expected_world_revision
            or event.skill_ref != lease.event.skill_ref
            or event.occurred_at < lease.event.occurred_at
        ):
            raise AgentPersistenceError(
                "AGENT_JOB_IDENTITY_MISMATCH",
                "Derived Agent event does not belong to the leased command",
            )
        current = await self._advance_to_runtime(lease)
        if current.status.is_terminal:
            await self._mark_done(lease)
            return None
        result = await self._hub.handle(event, lease.context)
        if not isinstance(result, AgentHubResult) or not result.persisted:
            raise AgentPersistenceError(
                "AGENT_JOB_RESULT_INVALID",
                "AgentHub did not persist the accepted Agent turn",
            )
        if finalize:
            await self._finalize(replace(lease, event=event))
        return result

    async def _heartbeat(
        self,
        lease: WorkerLease,
        stop: asyncio.Event,
        lost: asyncio.Event,
    ) -> None:
        interval = max(0.5, lease.lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), interval)
                return
            except TimeoutError:
                pass
            try:
                connection = await self._database.connect(autocommit=True)
                try:
                    cursor = await connection.execute(
                        """
                        UPDATE yaya_command_jobs
                        SET lease_expires_at=clock_timestamp()+%s*interval '1 second'
                        WHERE tenant_id=%s AND command_id=%s AND state='LEASED'
                          AND worker_id=%s AND lease_id=%s
                          AND lease_expires_at>clock_timestamp()
                        RETURNING command_id
                        """,
                        (
                            lease.lease_seconds,
                            lease.tenant_id,
                            lease.command_id,
                            self._worker_id,
                            lease.lease_id,
                        ),
                    )
                    if await cursor.fetchone() is None:
                        lost.set()
                        return
                finally:
                    await connection.close()
            except psycopg.Error:
                lost.set()
                return

    async def _advance_to_runtime(self, lease: WorkerLease) -> CommandRecord:
        async with self._database.transaction() as connection:
            current = await self._lock_fenced_command(connection, lease)
            if current.status.is_terminal:
                return current
            next_record = current
            now = await self._database_time(connection)
            if current.status is CommandStatus.ACCEPTED:
                next_record = replace(
                    current,
                    status=CommandStatus.VALIDATING,
                    stage="VALIDATE",
                    revision=current.revision + 1,
                    updated_at=now,
                )
                await self._write_transition(connection, current, next_record, lease)
                current = next_record
            return next_record

    async def _finalize(self, lease: WorkerLease) -> None:
        async with self._database.transaction() as connection:
            current = await self._lock_fenced_command(connection, lease)
            if current.status.is_terminal:
                await self._finish_job_on(connection, lease)
                return
            turn_cursor = await connection.execute(
                """
                SELECT record_json FROM yaya_agent_turns
                WHERE tenant_id=%s AND event_id=%s FOR KEY SHARE
                """,
                (lease.tenant_id, lease.event.event_id),
            )
            turn_row = await turn_cursor.fetchone()
            if turn_row is None or turn_row["record_json"] is None:
                raise AgentPersistenceError(
                    "AGENT_TURN_NOT_COMMITTED",
                    "Command cannot complete before AgentTurnCommit is durable",
                )
            committed = decode_as(turn_row["record_json"], CommittedAgentTurn)
            if (
                committed.event != lease.event
                or not _stable_actor(committed.actor, lease.context.actor)
                or committed.content_ref != lease.context.content_ref
            ):
                raise AgentPersistenceError(
                    "AGENT_TURN_IDENTITY_MISMATCH",
                    "Committed Agent turn does not belong to the leased command",
                )
            _validate_final_role_for_terminalization(committed)
            session_cursor = await connection.execute(
                """
                SELECT world_id,snapshot_json FROM yaya_agent_sessions
                WHERE tenant_id=%s AND session_id=%s AND actor_id=%s
                  AND content_hash=%s FOR KEY SHARE
                """,
                (
                    lease.tenant_id,
                    lease.event.session_id,
                    lease.context.actor.actor_id,
                    lease.context.content_ref.content_hash,
                ),
            )
            session_row = await session_cursor.fetchone()
            if session_row is None:
                raise AgentPersistenceError(
                    "AGENT_SESSION_NOT_FOUND",
                    "Leased Agent Session is no longer available",
                )
            session = decode_as(session_row["snapshot_json"], SessionSnapshot)
            if (
                session.session_id != lease.event.session_id
                or session.student_id != lease.context.actor.actor_id
                or session.task_id != lease.event.task_id
                or session.world_id != session_row["world_id"]
                or not _stable_actor(session.request_context.actor, lease.context.actor)
                or session.request_context.content_ref != lease.context.content_ref
            ):
                raise AgentPersistenceError(
                    "AGENT_SESSION_IDENTITY_MISMATCH",
                    "Leased Session identity drifted before terminalization",
                )
            run_cursor = await connection.execute(
                """
                SELECT snapshot_json,wire_json FROM yaya_runs
                WHERE tenant_id=%s AND command_id=%s AND actor_id=%s
                  AND content_hash=%s FOR KEY SHARE
                """,
                (
                    lease.tenant_id,
                    lease.command_id,
                    lease.context.actor.actor_id,
                    lease.context.content_ref.content_hash,
                ),
            )
            run_row = await run_cursor.fetchone()

            result: Mapping[str, object] | None
            links: dict[str, str]
            terminal_evidence: tuple[EvidenceRef, ...]
            terminal_status: CommandStatus
            terminal_stage: str
            terminal_error: ContractError | None
            if run_row is None:
                if any(call.name == "invoke_skill" for call in committed.decision.tool_calls):
                    raise AgentPersistenceError(
                        "AGENT_RUN_NOT_COMMITTED",
                        "A dispatched Skill has no durable Run to reconcile",
                    )
                result = {
                    "result_type": "NO_EFFECT",
                    "reason_code": "MODEL_FALLBACK_NO_RUN",
                }
                links = {"self": f"/v1/commands/{lease.command_id}"}
                terminal_evidence = committed.decision.evidence_refs
                terminal_status = CommandStatus.APPLIED
                terminal_stage = "COMPLETE"
                terminal_error = None
            else:
                run = decode_as(run_row["snapshot_json"], RunResultSnapshot)
                run_wire = _mapping(run_row["wire_json"], "Run wire")
                self._validator.validate("schemas/game/run.schema.json", run_wire)
                if (
                    run.command_id != lease.command_id
                    or run.session_id != lease.event.session_id
                    or run.turn_id != lease.event.turn_id
                    or run.world_id != session.world_id
                    or run.skill_ref != lease.event.skill_ref
                    or run.evidence_refs != committed.decision.evidence_refs
                    or (committed.decision.role == "book_agent" and not run.task_success)
                    or (
                        committed.decision.role == "bug_agent"
                        and (
                            run.task_success
                            or run.failure_key is None
                            or run.failure_key != lease.event.failure_key
                        )
                    )
                ):
                    raise AgentPersistenceError(
                        "AGENT_RUN_IDENTITY_MISMATCH",
                        "Durable Run does not belong to the leased command",
                    )
                run_status = run_wire.get("status")
                links = {
                    "self": f"/v1/commands/{lease.command_id}",
                    "run": f"/v1/runs/{run.run_id}",
                }
                if run_status == "SUCCEEDED":
                    if run.world_commit is None:
                        raise AgentPersistenceError(
                            "AGENT_RUN_IDENTITY_MISMATCH",
                            "A succeeded Run has no committed World receipt",
                        )
                    receipt = run.world_commit
                    result = {
                        "result_type": "WORLD_COMMIT",
                        "world_id": receipt.world_id,
                        "previous_revision": receipt.previous_revision,
                        "world_revision": receipt.world_revision,
                        "first_event_sequence": receipt.first_event_sequence,
                        "last_event_sequence": receipt.last_event_sequence,
                    }
                    links = {
                        **links,
                        "world_snapshot": f"/v1/worlds/{run.world_id}/snapshot",
                    }
                    terminal_status = CommandStatus.APPLIED
                    terminal_stage = "COMPLETE"
                    terminal_error = None
                elif run_status == "REJECTED":
                    result = None
                    terminal_status = CommandStatus.REJECTED
                    terminal_stage = "WORLD_VALIDATE"
                    terminal_error = ContractError(
                        code="WORLD_RULE_REJECTED",
                        category=ErrorCategory.WORLD_RULE,
                        retryable=False,
                        user_message_key="world.rule_rejected",
                        stage="WORLD_VALIDATE",
                        message="The certified Skill did not satisfy the pinned World task.",
                    )
                elif run_status == "FAILED":
                    result = None
                    terminal_status = CommandStatus.FAILED
                    terminal_stage = "SANDBOX"
                    terminal_error = ContractError(
                        code="SANDBOX_RUNTIME_ERROR",
                        category=ErrorCategory.SANDBOX,
                        retryable=False,
                        user_message_key="sandbox.runtime_error",
                        stage="SANDBOX",
                        message="The certified Skill execution failed in the Sandbox.",
                    )
                elif run_status == "UNKNOWN":
                    result = None
                    terminal_status = CommandStatus.UNKNOWN
                    terminal_stage = "WORLD_COMMIT"
                    terminal_error = ContractError(
                        code="UNKNOWN_COMMIT_STATE",
                        category=ErrorCategory.DEPENDENCY,
                        retryable=False,
                        user_message_key="command.reconciling",
                        stage="WORLD_COMMIT",
                        message="The World commit outcome requires reconciliation.",
                    )
                else:
                    raise AgentPersistenceError(
                        "AGENT_RUN_IDENTITY_MISMATCH",
                        "Durable Run has an unsupported terminal status",
                    )
                terminal_evidence = run.evidence_refs
            if (
                terminal_status is CommandStatus.UNKNOWN
                and current.status is not CommandStatus.APPLYING_WORLD
            ):
                applying = replace(
                    current,
                    status=CommandStatus.APPLYING_WORLD,
                    stage="WORLD_COMMIT",
                    terminal=False,
                    result=None,
                    error=None,
                    evidence_refs=terminal_evidence,
                    links=links,
                    revision=current.revision + 1,
                    updated_at=await self._database_time(connection),
                )
                await self._write_transition(connection, current, applying, lease)
                current = applying
            now = await self._database_time(connection)
            terminal = replace(
                current,
                status=terminal_status,
                stage=terminal_stage,
                terminal=True,
                result=result,
                error=terminal_error,
                evidence_refs=terminal_evidence,
                links=links,
                revision=current.revision + 1,
                updated_at=now,
            )
            CommandTransition(current, terminal)
            payload = _command_wire(terminal)
            self._validator.validate("schemas/game/command.schema.json", payload)
            updated = await connection.execute(
                """
                UPDATE yaya_commands c SET revision=%s,status=%s,updated_at=%s,record_json=%s
                FROM yaya_command_jobs j
                WHERE c.tenant_id=%s AND c.command_id=%s AND c.actor_id=%s
                  AND c.content_hash=%s AND c.revision=%s AND c.status=%s
                  AND j.tenant_id=c.tenant_id AND j.command_id=c.command_id
                  AND j.state='LEASED' AND j.worker_id=%s AND j.lease_id=%s
                  AND j.lease_expires_at>clock_timestamp()
                """,
                (
                    terminal.revision,
                    terminal.status.value,
                    terminal.updated_at,
                    Jsonb(encode(terminal)),
                    lease.tenant_id,
                    lease.command_id,
                    lease.context.actor.actor_id,
                    lease.context.content_ref.content_hash,
                    current.revision,
                    current.status.value,
                    self._worker_id,
                    lease.lease_id,
                ),
            )
            if updated.rowcount != 1:
                raise AgentPersistenceError(
                    "AGENT_JOB_FENCE_LOST",
                    "Stale worker cannot terminalize Command",
                )
            await self._finish_job_on(connection, lease)

    async def _mark_done(self, lease: WorkerLease) -> None:
        async with self._database.transaction() as connection:
            await self._lock_fenced_command(connection, lease)
            await self._finish_job_on(connection, lease)

    async def _release_for_retry(self, lease: WorkerLease, error_code: str) -> None:
        async with self._database.transaction() as connection:
            result = await connection.execute(
                """
                UPDATE yaya_command_jobs
                SET state='READY',worker_id=NULL,lease_id=NULL,lease_expires_at=NULL,
                    available_at=clock_timestamp()+interval '1 second',last_error_code=%s
                WHERE tenant_id=%s AND command_id=%s AND state='LEASED'
                  AND worker_id=%s AND lease_id=%s
                  AND lease_expires_at>clock_timestamp()
                """,
                (
                    error_code[:96],
                    lease.tenant_id,
                    lease.command_id,
                    self._worker_id,
                    lease.lease_id,
                ),
            )
            if result.rowcount != 1:
                raise AgentPersistenceError(
                    "AGENT_JOB_FENCE_LOST",
                    "Stale worker cannot reschedule Job",
                )

    async def _fail_permanent(self, lease: WorkerLease, cause_code: str) -> None:
        async with self._database.transaction() as connection:
            current = await self._lock_fenced_command(connection, lease)
            if current.status.is_terminal:
                await self._finish_job_on(connection, lease)
                return
            now = await self._database_time(connection)
            failure = ContractError(
                code="INVARIANT_VIOLATION",
                category=ErrorCategory.INVARIANT,
                retryable=False,
                user_message_key="system.invariant_violation",
                stage="VALIDATE",
                message="The accepted Agent turn no longer matches its pinned resources.",
                details={"cause_code": cause_code[:96]},
            )
            terminal = replace(
                current,
                status=CommandStatus.FAILED,
                stage="VALIDATE",
                terminal=True,
                result=None,
                error=failure,
                revision=current.revision + 1,
                updated_at=now,
            )
            CommandTransition(current, terminal)
            payload = _command_wire(terminal)
            self._validator.validate("schemas/game/command.schema.json", payload)
            updated = await connection.execute(
                """
                UPDATE yaya_commands c SET revision=%s,status=%s,updated_at=%s,record_json=%s
                FROM yaya_command_jobs j
                WHERE c.tenant_id=%s AND c.command_id=%s AND c.revision=%s AND c.status=%s
                  AND j.tenant_id=c.tenant_id AND j.command_id=c.command_id
                  AND j.state='LEASED' AND j.worker_id=%s AND j.lease_id=%s
                  AND j.lease_expires_at>clock_timestamp()
                """,
                (
                    terminal.revision,
                    terminal.status.value,
                    terminal.updated_at,
                    Jsonb(encode(terminal)),
                    lease.tenant_id,
                    lease.command_id,
                    current.revision,
                    current.status.value,
                    self._worker_id,
                    lease.lease_id,
                ),
            )
            if updated.rowcount != 1:
                raise AgentPersistenceError(
                    "AGENT_JOB_FENCE_LOST",
                    "Stale worker cannot fail Command",
                )
            await self._finish_job_on(connection, lease)

    async def _lock_fenced_command(
        self,
        connection: AsyncConnection[dict[str, object]],
        lease: WorkerLease,
    ) -> CommandRecord:
        cursor = await connection.execute(
            """
            SELECT c.record_json,c.revision,c.status
            FROM yaya_command_jobs j
            JOIN yaya_commands c
              ON c.tenant_id=j.tenant_id AND c.command_id=j.command_id
             AND c.actor_id=j.actor_id AND c.content_hash=j.content_hash
            WHERE j.tenant_id=%s AND j.command_id=%s AND j.state='LEASED'
              AND j.worker_id=%s AND j.lease_id=%s
              AND j.lease_expires_at>clock_timestamp()
            FOR UPDATE OF j,c
            """,
            (
                lease.tenant_id,
                lease.command_id,
                self._worker_id,
                lease.lease_id,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise AgentPersistenceError(
                "AGENT_JOB_FENCE_LOST",
                "Job lease is stale, expired, or taken over",
            )
        record = decode_as(row["record_json"], CommandRecord)
        if (
            record.command_id != lease.command_id
            or record.revision != row["revision"]
            or record.status.value != row["status"]
            or not _stable_actor(record.request_context.actor, lease.context.actor)
            or record.request_context.content_ref != lease.context.content_ref
        ):
            raise AgentPersistenceError(
                "AGENT_JOB_IDENTITY_MISMATCH",
                "Job and Command durable identities differ",
            )
        return record

    async def _write_transition(
        self,
        connection: AsyncConnection[dict[str, object]],
        previous: CommandRecord,
        next_record: CommandRecord,
        lease: WorkerLease,
    ) -> None:
        CommandTransition(previous, next_record)
        payload = _command_wire(next_record)
        self._validator.validate("schemas/game/command.schema.json", payload)
        cursor = await connection.execute(
            """
            UPDATE yaya_commands c SET revision=%s,status=%s,updated_at=%s,record_json=%s
            FROM yaya_command_jobs j
            WHERE c.tenant_id=%s AND c.command_id=%s AND c.revision=%s AND c.status=%s
              AND j.tenant_id=c.tenant_id AND j.command_id=c.command_id
              AND j.state='LEASED' AND j.worker_id=%s AND j.lease_id=%s
              AND j.lease_expires_at>clock_timestamp()
            """,
            (
                next_record.revision,
                next_record.status.value,
                next_record.updated_at,
                Jsonb(encode(next_record)),
                lease.tenant_id,
                lease.command_id,
                previous.revision,
                previous.status.value,
                self._worker_id,
                lease.lease_id,
            ),
        )
        if cursor.rowcount != 1:
            raise AgentPersistenceError(
                "AGENT_JOB_FENCE_LOST",
                "Stale worker cannot advance Command",
            )

    async def _finish_job_on(
        self,
        connection: AsyncConnection[dict[str, object]],
        lease: WorkerLease,
    ) -> None:
        result = await connection.execute(
            """
            UPDATE yaya_command_jobs
            SET state='DONE',worker_id=NULL,lease_id=NULL,lease_expires_at=NULL,
                last_error_code=NULL
            WHERE tenant_id=%s AND command_id=%s AND state='LEASED'
              AND worker_id=%s AND lease_id=%s
              AND lease_expires_at>clock_timestamp()
            """,
            (
                lease.tenant_id,
                lease.command_id,
                self._worker_id,
                lease.lease_id,
            ),
        )
        if result.rowcount != 1:
            raise AgentPersistenceError(
                "AGENT_JOB_FENCE_LOST",
                "Stale worker cannot finish Job",
            )

    @staticmethod
    async def _database_time(
        connection: AsyncConnection[dict[str, object]],
    ) -> datetime:
        cursor = await connection.execute("SELECT clock_timestamp() AS value")
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL clock query returned no row")
        return cast(datetime, row["value"])


__all__ = [
    "AcceptedTurn",
    "AgentTurnApplication",
    "AgentTurnWorker",
    "BackendApplicationError",
    "HttpAttempt",
    "ResourceResult",
    "WorkerLease",
]
