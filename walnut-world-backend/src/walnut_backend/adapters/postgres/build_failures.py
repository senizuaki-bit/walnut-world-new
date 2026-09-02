"""Validated rejected-Build authority shared by hint routing and Agent reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yaya_agent_contracts import (
    EvidenceRef,
    EvidenceType,
    OperationContext,
    canonical_json_sha256,
)
from yaya_agent_runtime import BuildFailureSnapshot

from .models import (
    EvidenceRow,
    JobStepReceiptRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
    request_context_from_data,
)
from .skill_provenance import (
    validate_build_provenance,
    validate_build_terminal_authority,
)
from .workflow_jobs import WorkflowInvariantError


@dataclass(frozen=True, slots=True)
class ValidatedBuildFailureAuthority:
    build: SkillBuildRow
    provenance: SkillBuildProvenanceRow
    evidence: EvidenceRow
    snapshot: BuildFailureSnapshot


def compile_failure_key(payload: Mapping[str, Any]) -> str:
    """Identify one compiler/test failure class independent of diagnostic order."""

    codes = payload.get("diagnostic_codes")
    diagnostics = ",".join(sorted(str(code) for code in codes)) if isinstance(codes, list) else ""
    return f"compile:{payload.get('failure_stage')}:{payload.get('failure_code')}:{diagnostics}"


async def load_validated_build_failure(
    session: AsyncSession,
    *,
    build_id: str,
    session_id: str,
    context: OperationContext,
) -> ValidatedBuildFailureAuthority:
    build = await session.scalar(
        select(SkillBuildRow).where(
            SkillBuildRow.build_id == build_id,
            SkillBuildRow.tenant_id == context.actor.tenant_id,
            SkillBuildRow.actor_id == context.actor.actor_id,
        )
    )
    provenance = await session.scalar(
        select(SkillBuildProvenanceRow).where(
            SkillBuildProvenanceRow.build_id == build_id,
            SkillBuildProvenanceRow.tenant_id == context.actor.tenant_id,
            SkillBuildProvenanceRow.actor_id == context.actor.actor_id,
            SkillBuildProvenanceRow.session_id == session_id,
        )
    )
    if (
        build is None
        or provenance is None
        or build.status != "REJECTED"
        or not build.terminal
        or provenance.skill_id != build.skill_id
        or not await validate_build_provenance(session, provenance, require_immutable=True)
        or not await validate_build_terminal_authority(session, build, provenance)
    ):
        raise WorkflowInvariantError("rejected Build authority is incomplete or corrupt")

    evidence_rows = list(
        (
            await session.scalars(
                select(EvidenceRow).where(
                    EvidenceRow.tenant_id == build.tenant_id,
                    EvidenceRow.actor_id == build.actor_id,
                    EvidenceRow.command_id == build.command_id,
                )
            )
        ).all()
    )
    matches = [
        row
        for row in evidence_rows
        if isinstance(row.evidence_json.get("payload"), Mapping)
        and cast(Mapping[str, Any], row.evidence_json["payload"]).get("evidence_kind")
        == "BUILD_REJECTION"
    ]
    if len(matches) != 1:
        raise WorkflowInvariantError("rejected Build has no unique rejection Evidence")
    evidence = matches[0]
    payload = _object(evidence.evidence_json.get("payload"), "Build rejection payload")
    reference_wire = _object(
        evidence.evidence_json.get("evidence_ref"), "Build rejection Evidence ref"
    )
    source = _object(evidence.evidence_json.get("source"), "Build rejection source")
    integrity = _object(evidence.evidence_json.get("integrity"), "Build rejection integrity")
    origin = request_context_from_data(
        _object(evidence.evidence_json.get("request_context"), "Build rejection context")
    )
    evidence_sha256 = canonical_json_sha256(payload)
    diagnostic_codes = _strings(payload.get("diagnostic_codes"), "diagnostic_codes")
    failure_code = _text(payload, "failure_code")
    diagnostics = diagnostic_codes or (failure_code,)
    reference = EvidenceRef(
        evidence_id=_text(reference_wire, "evidence_id"),
        evidence_type=EvidenceType(_text(reference_wire, "evidence_type")),
        created_at=evidence.recorded_at,
        sha256=cast(str | None, reference_wire.get("sha256")),
        uri=cast(str | None, reference_wire.get("uri")),
    )
    receipt = await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == build.tenant_id,
            JobStepReceiptRow.job_id == provenance.workflow_job_id,
            JobStepReceiptRow.step_name == "BUILD_REJECTED",
        )
    )
    if (
        origin.actor != context.actor
        or origin.content_ref != context.content_ref
        or payload.get("build_id") != build.build_id
        or payload.get("skill_id") != build.skill_id
        or payload.get("evidence_kind") != "BUILD_REJECTION"
        or payload.get("outcome") != "REJECTED"
        or source.get("source_type") != "SKILL_BUILD"
        or source.get("source_id") != build.build_id
        or source.get("command_id") != build.command_id
        or reference.evidence_id != evidence.evidence_id
        or reference.evidence_type.value != "TEST_REPORT"
        or reference.sha256 != evidence_sha256
        or integrity.get("payload_sha256") != evidence_sha256
        or evidence.evidence_json.get("occurred_at") != reference_wire.get("created_at")
        or evidence.evidence_json.get("recorded_at") != reference_wire.get("created_at")
        or receipt is None
        or receipt.receipt_json.get("build_id") != build.build_id
        or receipt.receipt_json.get("evidence_id") != evidence.evidence_id
        or receipt.receipt_json.get("failure_stage") != payload.get("failure_stage")
        or receipt.receipt_json.get("failure_code") != failure_code
        or tuple(receipt.receipt_json.get("diagnostic_codes", ())) != diagnostic_codes
    ):
        raise WorkflowInvariantError("Build rejection Evidence differs from terminal authority")

    snapshot = BuildFailureSnapshot(
        build_id=build.build_id,
        session_id=session_id,
        skill_id=build.skill_id,
        failure_key=compile_failure_key(payload),
        diagnostics=diagnostics,
        evidence_refs=(reference,),
        request_context=origin,
    )
    return ValidatedBuildFailureAuthority(build, provenance, evidence, snapshot)


async def list_current_build_failure_streak(
    session: AsyncSession,
    *,
    session_id: str,
    context: OperationContext,
) -> tuple[ValidatedBuildFailureAuthority, ...]:
    """Return the unresolved same-class rejection suffix, oldest to newest."""

    rows = list(
        (
            await session.execute(
                select(SkillBuildRow, SkillBuildProvenanceRow)
                .join(
                    SkillBuildProvenanceRow,
                    SkillBuildProvenanceRow.build_id == SkillBuildRow.build_id,
                )
                .where(
                    SkillBuildRow.tenant_id == context.actor.tenant_id,
                    SkillBuildRow.actor_id == context.actor.actor_id,
                    SkillBuildProvenanceRow.session_id == session_id,
                )
                .order_by(SkillBuildRow.created_at.desc(), SkillBuildRow.build_id.desc())
            )
        ).all()
    )
    streak: list[ValidatedBuildFailureAuthority] = []
    failure_key: str | None = None
    skill_id: str | None = None
    for build, provenance in rows:
        build_context = request_context_from_data(
            _object(build.build_json.get("request_context"), "Build request context")
        )
        if (
            build_context.actor != context.actor
            or build_context.content_ref != context.content_ref
            or provenance.session_id != session_id
            or not await validate_build_provenance(session, provenance, require_immutable=True)
            or not await validate_build_terminal_authority(session, build, provenance)
        ):
            raise WorkflowInvariantError("Build failure history authority is corrupt")
        if build.status == "CERTIFIED":
            break
        if build.status != "REJECTED" or not build.terminal:
            break
        authority = await load_validated_build_failure(
            session,
            build_id=build.build_id,
            session_id=session_id,
            context=context,
        )
        if failure_key is None:
            failure_key = authority.snapshot.failure_key
            skill_id = authority.snapshot.skill_id
        elif (
            authority.snapshot.failure_key != failure_key or authority.snapshot.skill_id != skill_id
        ):
            break
        streak.append(authority)
    streak.reverse()
    return tuple(streak)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowInvariantError(f"{label} must be an object")
    return dict(value)


def _text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise WorkflowInvariantError(f"{key} must be text")
    return item


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise WorkflowInvariantError(f"{label} must be an array")
    items = tuple(value)
    if any(not isinstance(item, str) or not item for item in items):
        raise WorkflowInvariantError(f"{label} contains invalid text")
    return cast(tuple[str, ...], items)


__all__ = [
    "ValidatedBuildFailureAuthority",
    "compile_failure_key",
    "list_current_build_failure_streak",
    "load_validated_build_failure",
]
