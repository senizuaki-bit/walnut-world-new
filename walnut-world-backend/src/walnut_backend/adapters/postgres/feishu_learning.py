"""Read-only PostgreSQL adapter for Feishu teacher learning projections."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    AuditRecord,
    ContentRef,
    ContractError,
    ErrorCategory,
    Failure,
    OperationContext,
    Result,
    RuntimeEventType,
    Success,
    canonical_json_sha256,
)

from walnut_backend.application.feishu.learning_queries import (
    EvidenceAuthority,
    EvidenceLearningBundle,
    LearnerLearningBundle,
    LearnerProfileAuthority,
    LearningProjectionAuthority,
    stable_learner_ref,
)
from walnut_backend.application.feishu.learning_sync import (
    LearnerSyncBundle,
    TenantLearningSnapshot,
)

from .audit import PostgresAudit
from .models import (
    EventRow,
    EvidenceRow,
    JobStepReceiptRow,
    LearnerProfileRow,
    LearnerProjectionJobRow,
    ProductContentUnitRow,
    RunRow,
    SkillBuildProvenanceRow,
    SkillRunProvenanceRow,
    WorkflowJobRow,
)
from .run_evidence import validate_evidence_document_authority
from .skill_provenance import (
    build_provenance_sha256,
    run_provenance_sha256,
    validate_run_provenance,
)
from .workflow_jobs import (
    WorkflowInvariantError,
    workflow_receipt_sha256,
    workflow_step_receipt_id,
)

_MAX_AUTHORITY_ROWS = 10_000


class PostgresFeishuLearningStore:
    """Select only Learner/Projection/Evidence authority; append only access audit."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        pseudonym_secret: str,
    ) -> None:
        self._sessions = session_factory
        self._secret = pseudonym_secret
        self._audit = PostgresAudit(session_factory)

    async def learner_content_refs(
        self, tenant_id: str, learner_ref: str
    ) -> Result[tuple[ContentRef, ...]]:
        """Read content pins for one opaque learner from Profile authority only."""
        try:
            profiles = await self._tenant_profiles(tenant_id)
            matches = [
                profile
                for profile in profiles
                if hmac.compare_digest(profile.learner_ref, learner_ref)
            ]
            return Success(_unique_content_refs(matches))
        except SQLAlchemyError:
            return Failure(_internal("PostgreSQL learner content read failed"))
        except (TypeError, ValueError) as error:
            return Failure(_invariant(str(error)))

    async def tenant_content_refs(self, tenant_id: str) -> Result[tuple[ContentRef, ...]]:
        """Read the distinct current Profile content pins within one tenant."""
        try:
            return Success(_unique_content_refs(await self._tenant_profiles(tenant_id)))
        except SQLAlchemyError:
            return Failure(_internal("PostgreSQL tenant content read failed"))
        except (TypeError, ValueError) as error:
            return Failure(_invariant(str(error)))

    async def _tenant_profiles(self, tenant_id: str) -> tuple[LearnerProfileAuthority, ...]:
        async with self._sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(LearnerProfileRow)
                        .where(LearnerProfileRow.tenant_id == tenant_id)
                        .order_by(LearnerProfileRow.learner_id)
                        .limit(_MAX_AUTHORITY_ROWS + 1)
                    )
                ).all()
            )
        if len(rows) > _MAX_AUTHORITY_ROWS:
            raise ValueError("tenant learner cohort exceeds query safety bound")
        return tuple(_profile(self._secret, row) for row in rows)

    async def learner_bundle(
        self,
        tenant_id: str,
        learner_ref: str,
        occurred_from: datetime | None,
        occurred_to: datetime | None,
    ) -> Result[LearnerLearningBundle]:
        try:
            async with self._sessions() as session:
                profiles = list(
                    (
                        await session.scalars(
                            select(LearnerProfileRow)
                            .where(LearnerProfileRow.tenant_id == tenant_id)
                            .order_by(LearnerProfileRow.learner_id)
                            .limit(_MAX_AUTHORITY_ROWS + 1)
                        )
                    ).all()
                )
                if len(profiles) > _MAX_AUTHORITY_ROWS:
                    return Failure(_invariant("tenant learner cohort exceeds query safety bound"))
                matches = [
                    row
                    for row in profiles
                    if hmac.compare_digest(
                        stable_learner_ref(self._secret, tenant_id, row.learner_id), learner_ref
                    )
                ]
                if not matches:
                    return Failure(_not_found("learner profile not found"))
                if len(matches) != 1:
                    return Failure(_invariant("opaque learner reference is ambiguous"))
                profile = matches[0]
                statement = select(LearnerProjectionJobRow).where(
                    LearnerProjectionJobRow.tenant_id == tenant_id,
                    LearnerProjectionJobRow.learner_id == profile.learner_id,
                    LearnerProjectionJobRow.actor_id == profile.actor_id,
                    LearnerProjectionJobRow.content_hash == profile.content_hash,
                    LearnerProjectionJobRow.status == "SUCCEEDED",
                )
                if occurred_from is not None:
                    statement = statement.where(
                        LearnerProjectionJobRow.completed_at >= occurred_from
                    )
                if occurred_to is not None:
                    statement = statement.where(LearnerProjectionJobRow.completed_at <= occurred_to)
                rows = list(
                    (
                        await session.scalars(
                            statement.order_by(
                                LearnerProjectionJobRow.through_sequence,
                                LearnerProjectionJobRow.job_id,
                            ).limit(_MAX_AUTHORITY_ROWS + 1)
                        )
                    ).all()
                )
                if len(rows) > _MAX_AUTHORITY_ROWS:
                    return Failure(_invariant("learner history exceeds query safety bound"))
                await _validate_projection_runs(session, rows)
                return Success(
                    LearnerLearningBundle(
                        profile=_profile(self._secret, profile),
                        projections=tuple(_projection(row) for row in rows),
                    )
                )
        except SQLAlchemyError:
            return Failure(_internal("PostgreSQL learner read failed"))
        except (TypeError, ValueError) as error:
            return Failure(_invariant(str(error)))

    async def class_bundles(
        self,
        tenant_id: str,
        content_hash: str,
        occurred_from: datetime,
        occurred_to: datetime,
    ) -> Result[tuple[LearnerLearningBundle, ...]]:
        try:
            async with self._sessions() as session:
                profiles = list(
                    (
                        await session.scalars(
                            select(LearnerProfileRow)
                            .where(
                                LearnerProfileRow.tenant_id == tenant_id,
                                LearnerProfileRow.content_hash == content_hash,
                            )
                            .order_by(LearnerProfileRow.learner_id)
                            .limit(_MAX_AUTHORITY_ROWS + 1)
                        )
                    ).all()
                )
                if len(profiles) > _MAX_AUTHORITY_ROWS:
                    return Failure(_invariant("class cohort exceeds query safety bound"))
                rows = list(
                    (
                        await session.scalars(
                            select(LearnerProjectionJobRow)
                            .where(
                                LearnerProjectionJobRow.tenant_id == tenant_id,
                                LearnerProjectionJobRow.content_hash == content_hash,
                                LearnerProjectionJobRow.status == "SUCCEEDED",
                                LearnerProjectionJobRow.completed_at >= occurred_from,
                                LearnerProjectionJobRow.completed_at <= occurred_to,
                            )
                            .order_by(
                                LearnerProjectionJobRow.learner_id,
                                LearnerProjectionJobRow.through_sequence,
                                LearnerProjectionJobRow.job_id,
                            )
                            .limit(_MAX_AUTHORITY_ROWS + 1)
                        )
                    ).all()
                )
                if len(rows) > _MAX_AUTHORITY_ROWS:
                    return Failure(_invariant("class history exceeds query safety bound"))
                await _validate_projection_runs(session, rows)
                profile_keys = {
                    (profile.learner_id, profile.actor_id, profile.content_hash)
                    for profile in profiles
                }
                if any(
                    (row.learner_id, row.actor_id, row.content_hash) not in profile_keys
                    for row in rows
                ):
                    return Failure(_invariant("class projection has no exact profile authority"))
                by_learner: dict[str, list[LearningProjectionAuthority]] = {}
                for row in rows:
                    by_learner.setdefault(row.learner_id, []).append(_projection(row))
                return Success(
                    tuple(
                        LearnerLearningBundle(
                            profile=_profile(self._secret, profile),
                            projections=tuple(by_learner.get(profile.learner_id, ())),
                        )
                        for profile in profiles
                    )
                )
        except SQLAlchemyError:
            return Failure(_internal("PostgreSQL class read failed"))
        except (TypeError, ValueError) as error:
            return Failure(_invariant(str(error)))

    async def evidence_bundle(
        self, tenant_id: str, evidence_id: str
    ) -> Result[EvidenceLearningBundle]:
        try:
            async with self._sessions() as session:
                evidence = await session.scalar(
                    select(EvidenceRow).where(
                        EvidenceRow.tenant_id == tenant_id,
                        EvidenceRow.evidence_id == evidence_id,
                    )
                )
                if evidence is None:
                    return Failure(_not_found("evidence not found"))
                candidates = list(
                    (
                        await session.scalars(
                            select(LearnerProjectionJobRow)
                            .where(
                                LearnerProjectionJobRow.tenant_id == tenant_id,
                                LearnerProjectionJobRow.actor_id == evidence.actor_id,
                                LearnerProjectionJobRow.status == "SUCCEEDED",
                            )
                            .order_by(
                                LearnerProjectionJobRow.through_sequence,
                                LearnerProjectionJobRow.job_id,
                            )
                            .limit(_MAX_AUTHORITY_ROWS + 1)
                        )
                    ).all()
                )
                if len(candidates) > _MAX_AUTHORITY_ROWS:
                    return Failure(_invariant("evidence history exceeds query safety bound"))
                linked = [row for row in candidates if _links_evidence(row, evidence_id)]
                if not linked:
                    # Do not reveal whether an unlinked tenant evidence row exists.
                    return Failure(_not_found("evidence not found"))
                learner_ids = {row.learner_id for row in linked}
                if len(learner_ids) != 1:
                    return Failure(_invariant("evidence is linked to multiple learners"))
                for row in linked:
                    _validate_evidence_projection(row, evidence)
                selected = linked[-1]
                profile = await session.scalar(
                    select(LearnerProfileRow).where(
                        LearnerProfileRow.tenant_id == tenant_id,
                        LearnerProfileRow.learner_id == selected.learner_id,
                        LearnerProfileRow.actor_id == selected.actor_id,
                        LearnerProfileRow.content_hash == selected.content_hash,
                    )
                )
                if profile is None:
                    return Failure(_invariant("linked learner profile is missing"))
                history = [
                    row
                    for row in candidates
                    if row.learner_id == selected.learner_id
                    and row.content_hash == selected.content_hash
                ]
                await _validate_projection_runs(session, history)
                return Success(
                    EvidenceLearningBundle(
                        evidence=EvidenceAuthority(
                            evidence_id=evidence.evidence_id,
                            command_id=evidence.command_id,
                            document=dict(evidence.evidence_json),
                            recorded_at=evidence.recorded_at,
                        ),
                        profile=_profile(self._secret, profile),
                        projection=_projection(selected),
                        learner_projections=tuple(_projection(row) for row in history),
                    )
                )
        except SQLAlchemyError:
            return Failure(_internal("PostgreSQL evidence read failed"))
        except (TypeError, ValueError) as error:
            return Failure(_invariant(str(error)))

    async def append_access_audit(
        self,
        *,
        context: OperationContext,
        operation: str,
        outcome: Literal["ALLOWED", "DENIED", "FAILED"],
        resource_type: str,
        resource_id: str,
        purpose: str | None,
        evidence_ids: tuple[str, ...],
        error_code: str | None,
        details: Mapping[str, Any],
    ) -> Result[None]:
        subject_hash = hashlib.sha256(
            f"{context.actor.tenant_id}\x1f{resource_id}".encode()
        ).hexdigest()
        try:
            result = await self._audit.append(
                AuditRecord(
                    audit_id=f"audit_{uuid4().hex}",
                    occurred_at=context.requested_at,
                    operation=operation,
                    outcome=outcome,
                    actor=context.actor,
                    request_id=context.request_id,
                    correlation_id=context.correlation_id,
                    trace_id=context.trace_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    purpose=purpose,
                    subject_hash=subject_hash,
                    evidence_ids=evidence_ids,
                    error_code=error_code,
                    details=dict(details),
                ),
                context,
            )
        except SQLAlchemyError:
            return Failure(_internal("PostgreSQL access audit append failed"))
        if isinstance(result, Failure):
            return Failure(result.error)
        return Success(None)


class PostgresFeishuLearningSyncReader:
    """Bulk-select the same authority used by the read API, without any write port."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        pseudonym_secret: str,
    ) -> None:
        self._sessions = session_factory
        self._secret = pseudonym_secret

    async def load_tenant(self, tenant_id: str) -> TenantLearningSnapshot:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        async with self._sessions() as session:
            profiles = list(
                (
                    await session.scalars(
                        select(LearnerProfileRow)
                        .where(LearnerProfileRow.tenant_id == tenant_id)
                        .order_by(LearnerProfileRow.learner_id)
                        .limit(_MAX_AUTHORITY_ROWS + 1)
                    )
                ).all()
            )
            jobs = list(
                (
                    await session.scalars(
                        select(LearnerProjectionJobRow)
                        .where(
                            LearnerProjectionJobRow.tenant_id == tenant_id,
                            LearnerProjectionJobRow.status == "SUCCEEDED",
                        )
                        .order_by(
                            LearnerProjectionJobRow.learner_id,
                            LearnerProjectionJobRow.through_sequence,
                            LearnerProjectionJobRow.job_id,
                        )
                        .limit(_MAX_AUTHORITY_ROWS + 1)
                    )
                ).all()
            )
            evidence_rows = list(
                (
                    await session.scalars(
                        select(EvidenceRow)
                        .where(EvidenceRow.tenant_id == tenant_id)
                        .order_by(EvidenceRow.recorded_at, EvidenceRow.evidence_id)
                        .limit(_MAX_AUTHORITY_ROWS + 1)
                    )
                ).all()
            )
            if any(
                len(rows) > _MAX_AUTHORITY_ROWS
                for rows in (profiles, jobs, evidence_rows)
            ):
                raise ValueError("tenant learning authority exceeds sync safety bound")
            await _validate_projection_runs(session, jobs)

        profile_rows = {
            (row.learner_id, row.actor_id, row.content_hash): row for row in profiles
        }
        profile_authority = {
            key: _profile(self._secret, row) for key, row in profile_rows.items()
        }
        projection_rows: dict[tuple[str, str, str], list[LearnerProjectionJobRow]] = {}
        for row in jobs:
            key = (row.learner_id, row.actor_id, row.content_hash)
            if key not in profile_rows:
                raise ValueError("SUCCEEDED learner projection has no profile authority")
            projection_rows.setdefault(key, []).append(row)

        evidence_by_id = {row.evidence_id: row for row in evidence_rows}
        selected: dict[str, LearnerProjectionJobRow] = {}
        for row in jobs:
            for evidence_id in _linked_evidence_ids(row):
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None:
                    raise ValueError("SUCCEEDED learner projection references missing evidence")
                _validate_evidence_projection(row, evidence)
                previous = selected.get(evidence_id)
                if previous is None or (row.through_sequence, row.job_id) > (
                    previous.through_sequence,
                    previous.job_id,
                ):
                    selected[evidence_id] = row

        linked_by_profile: dict[tuple[str, str, str], list[EvidenceLearningBundle]] = {}
        for evidence_id, row in sorted(selected.items()):
            key = (row.learner_id, row.actor_id, row.content_hash)
            history = tuple(_projection(item) for item in projection_rows.get(key, ()))
            linked_by_profile.setdefault(key, []).append(
                EvidenceLearningBundle(
                    evidence=EvidenceAuthority(
                        evidence_id=evidence_id,
                        command_id=evidence_by_id[evidence_id].command_id,
                        document=dict(evidence_by_id[evidence_id].evidence_json),
                        recorded_at=evidence_by_id[evidence_id].recorded_at,
                    ),
                    profile=profile_authority[key],
                    projection=_projection(row),
                    learner_projections=history,
                )
            )

        learners = []
        for key, row in profile_rows.items():
            projections = tuple(_projection(item) for item in projection_rows.get(key, ()))
            learners.append(
                LearnerSyncBundle(
                    learning=LearnerLearningBundle(
                        profile=profile_authority[key],
                        projections=projections,
                    ),
                    evidence=tuple(linked_by_profile.get(key, ())),
                )
            )
        return TenantLearningSnapshot(tenant_id=tenant_id, learners=tuple(learners))


def _profile(secret: str, row: LearnerProfileRow) -> LearnerProfileAuthority:
    content = row.profile_json.get("content")
    if not hmac.compare_digest(
        row.profile_sha256,
        canonical_json_sha256(row.profile_json),
    ) or (
        row.profile_json.get("learner_id") != row.learner_id
        or row.profile_json.get("actor_id") != row.actor_id
        or not isinstance(content, Mapping)
        or content.get("content_hash") != row.content_hash
    ):
        raise ValueError("Learner Profile authority hash drifted")
    return LearnerProfileAuthority(
        learner_ref=stable_learner_ref(secret, row.tenant_id, row.learner_id),
        tenant_id=row.tenant_id,
        learner_id=row.learner_id,
        actor_id=row.actor_id,
        content_hash=row.content_hash,
        profile=dict(row.profile_json),
        updated_at=row.updated_at,
    )


def _unique_content_refs(
    profiles: Sequence[LearnerProfileAuthority],
) -> tuple[ContentRef, ...]:
    values: dict[tuple[str, str, str], ContentRef] = {}
    for profile in profiles:
        content = profile.profile.get("content")
        if not isinstance(content, Mapping) or set(content) != {
            "unit_id",
            "version",
            "content_hash",
        }:
            raise ValueError("Learner Profile content authority is malformed")
        reference = ContentRef(
            unit_id=content["unit_id"],
            version=content["version"],
            content_hash=content["content_hash"],
        )
        values[(reference.unit_id, reference.version, reference.content_hash)] = reference
    return tuple(values[key] for key in sorted(values))


async def _validate_projection_runs(
    session: AsyncSession,
    rows: Sequence[LearnerProjectionJobRow],
) -> None:
    """Close every released learner projection over its immutable Game Run."""

    if not rows:
        return
    tenant_ids = {row.tenant_id for row in rows}
    if len(tenant_ids) != 1:
        raise ValueError("learner projection batch crosses tenants")
    run_ids = {row.run_id for row in rows}
    runs = list(
        (
            await session.scalars(
                select(RunRow)
                .where(
                    RunRow.tenant_id == rows[0].tenant_id,
                    RunRow.run_id.in_(run_ids),
                )
                .limit(_MAX_AUTHORITY_ROWS + 1)
            )
        ).all()
    )
    if len(runs) > _MAX_AUTHORITY_ROWS:
        raise ValueError("Game Run authority exceeds query safety bound")
    run_by_id = {run.run_id: run for run in runs}
    if len(run_by_id) != len(run_ids):
        raise ValueError("SUCCEEDED learner projection has no exact Game Run authority")

    run_provenance = list(
        (
            await session.scalars(
                select(SkillRunProvenanceRow)
                .where(
                    SkillRunProvenanceRow.tenant_id == rows[0].tenant_id,
                    SkillRunProvenanceRow.run_id.in_(run_ids),
                )
                .limit(_MAX_AUTHORITY_ROWS + 1)
            )
        ).all()
    )
    if len(run_provenance) > _MAX_AUTHORITY_ROWS:
        raise ValueError("Run assistance authority exceeds query safety bound")
    provenance_by_run = {item.run_id: item for item in run_provenance}
    if len(provenance_by_run) != len(run_ids):
        raise ValueError("SUCCEEDED learner projection has no exact Run assistance authority")
    validated_build_by_run: dict[str, SkillBuildProvenanceRow] = {}
    for provenance in run_provenance:
        build = await validate_run_provenance(session, provenance)
        if build is None:
            raise ValueError("SUCCEEDED learner projection Run assistance graph is corrupt")
        validated_build_by_run[provenance.run_id] = build

    job_ids = {row.job_id for row in rows}
    receipts = list(
        (
            await session.scalars(
                select(JobStepReceiptRow)
                .where(
                    JobStepReceiptRow.tenant_id == rows[0].tenant_id,
                    JobStepReceiptRow.job_id.in_(job_ids),
                    JobStepReceiptRow.step_name == "LEARNER_PROJECTION_COMMITTED",
                )
                .limit(_MAX_AUTHORITY_ROWS + 1)
            )
        ).all()
    )
    if len(receipts) > _MAX_AUTHORITY_ROWS:
        raise ValueError("Learner projection receipt authority exceeds query safety bound")
    receipt_by_job = {item.job_id: item for item in receipts}
    if len(receipt_by_job) != len(job_ids):
        raise ValueError("SUCCEEDED learner projection has no exact commit receipt authority")

    parent_jobs = list(
        (
            await session.scalars(
                select(WorkflowJobRow)
                .where(
                    WorkflowJobRow.tenant_id == rows[0].tenant_id,
                    WorkflowJobRow.job_id.in_(job_ids),
                )
                .limit(_MAX_AUTHORITY_ROWS + 1)
            )
        ).all()
    )
    if len(parent_jobs) > _MAX_AUTHORITY_ROWS:
        raise ValueError("Learner projection parent authority exceeds query safety bound")
    parent_by_job = {item.job_id: item for item in parent_jobs}
    if len(parent_by_job) != len(job_ids):
        raise ValueError("SUCCEEDED learner projection has no exact parent Workflow authority")

    source_event_ids = {row.source_event_id for row in rows}
    source_events = list(
        (
            await session.scalars(
                select(EventRow)
                .where(
                    EventRow.tenant_id == rows[0].tenant_id,
                    EventRow.event_id.in_(source_event_ids),
                )
                .limit(_MAX_AUTHORITY_ROWS + 1)
            )
        ).all()
    )
    if len(source_events) > _MAX_AUTHORITY_ROWS:
        raise ValueError("Learner source Event authority exceeds query safety bound")
    source_event_by_id = {item.event_id: item for item in source_events}
    if len(source_event_by_id) != len(source_event_ids):
        raise ValueError("SUCCEEDED learner projection has no exact source Event authority")

    content_keys = {_projection_content_key(row) for row in rows}
    content_rows = list(
        (
            await session.scalars(
                select(ProductContentUnitRow)
                .where(
                    ProductContentUnitRow.tenant_id == rows[0].tenant_id,
                    ProductContentUnitRow.unit_id.in_({key[0] for key in content_keys}),
                    ProductContentUnitRow.version.in_({key[1] for key in content_keys}),
                    ProductContentUnitRow.content_hash.in_({key[2] for key in content_keys}),
                )
                .limit(_MAX_AUTHORITY_ROWS + 1)
            )
        ).all()
    )
    if len(content_rows) > _MAX_AUTHORITY_ROWS:
        raise ValueError("Content task authority exceeds query safety bound")
    content_by_key = {
        (item.unit_id, item.version, item.content_hash): item for item in content_rows
    }
    if any(key not in content_by_key for key in content_keys):
        raise ValueError("SUCCEEDED learner projection has no exact Content task authority")

    evidence_ids = {
        evidence_id
        for row in rows
        for evidence_id in (
            *_game_run_evidence_ids(run_by_id[row.run_id].run_json),
            _learner_evidence_binding(row)[0],
        )
    }
    evidence_rows = list(
        (
            await session.scalars(
                select(EvidenceRow)
                .where(
                    EvidenceRow.tenant_id == rows[0].tenant_id,
                    EvidenceRow.evidence_id.in_(evidence_ids),
                )
                .limit(_MAX_AUTHORITY_ROWS + 1)
            )
        ).all()
    )
    if len(evidence_rows) > _MAX_AUTHORITY_ROWS:
        raise ValueError("Run Evidence authority exceeds query safety bound")
    evidence_by_id = {item.evidence_id: item for item in evidence_rows}
    if len(evidence_by_id) != len(evidence_ids):
        raise ValueError("SUCCEEDED learner projection references missing Run Evidence authority")

    for row in rows:
        run = run_by_id.get(row.run_id)
        if run is None:
            raise ValueError("SUCCEEDED learner projection has no exact Game Run authority")
        _validate_projection_run_authority(row, run)
        provenance = provenance_by_run[row.run_id]
        build = validated_build_by_run.get(row.run_id)
        if build is None:
            raise ValueError("SUCCEEDED learner projection has no exact Build assistance authority")
        _validate_projection_assistance(row, provenance, build)
        _validate_projection_receipt(
            row,
            receipt_by_job[row.job_id],
            parent_by_job[row.job_id],
        )
        _validate_projection_source_event(row, source_event_by_id[row.source_event_id])
        _validate_projection_task(row, content_by_key[_projection_content_key(row)])
        _validate_projection_evidence_authority(row, run, evidence_by_id)
        _validate_projection_learner_evidence_authority(row, run, evidence_by_id)


def _validate_projection_run_authority(
    projection: LearnerProjectionJobRow,
    run: RunRow,
) -> None:
    """Reject a projection unless row, frozen objective and Run bytes are identical."""

    identity = projection.projection_json.get("identity")
    projected_run = projection.projection_json.get("run")
    assistance = projection.projection_json.get("assistance")
    request_context = run.run_json.get("request_context")
    command = projection.projection_json.get("command")
    command_context = command.get("request_context") if isinstance(command, Mapping) else None
    source_event = projection.projection_json.get("source_feedback_event")
    actor = request_context.get("actor") if isinstance(request_context, Mapping) else None
    content = (
        request_context.get("content_ref")
        if isinstance(request_context, Mapping)
        else None
    )
    immutable_run = dict(run.run_json)
    immutable_run.pop("agent_feedback", None)
    immutable_run.pop("updated_at", None)
    result = projection.result_json
    task_success, failure_key = _game_run_outcome(run.run_json)
    evidence_ids = _game_run_evidence_ids(run.run_json)
    if (
        projection.status != "SUCCEEDED"
        or projection.completed_at is None
        or result is None
        or projection.result_sha256 is None
        or projection.request_sha256 != _workflow_json_sha256(projection.projection_json)
        or projection.result_sha256 != _workflow_json_sha256(result)
        or run.tenant_id != projection.tenant_id
        or run.actor_id != projection.actor_id
        or run.content_hash != projection.content_hash
        or run.session_id != projection.session_id
        or run.turn_id != projection.turn_id
        or run.command_id != projection.command_id
        or run.run_id != projection.run_id
        or run.run_json.get("run_id") != projection.run_id
        or run.run_json.get("session_id") != projection.session_id
        or run.run_json.get("turn_id") != projection.turn_id
        or run.run_json.get("command_id") != projection.command_id
        or not isinstance(request_context, Mapping)
        or not isinstance(actor, Mapping)
        or actor.get("tenant_id") != projection.tenant_id
        or actor.get("actor_id") != projection.actor_id
        or not isinstance(content, Mapping)
        or content.get("content_hash") != projection.content_hash
        or not isinstance(command_context, Mapping)
        or dict(command_context) != dict(request_context)
        or not isinstance(identity, Mapping)
        or identity.get("tenant_id") != projection.tenant_id
        or identity.get("job_id") != projection.job_id
        or identity.get("command_id") != projection.command_id
        or identity.get("session_id") != projection.session_id
        or identity.get("turn_id") != projection.turn_id
        or identity.get("run_id") != projection.run_id
        or identity.get("learner_id") != projection.learner_id
        or identity.get("actor_id") != projection.actor_id
        or identity.get("content_hash") != projection.content_hash
        or not isinstance(projected_run, Mapping)
        or projected_run.get("run_id") != projection.run_id
        or projected_run.get("task_success") is not task_success
        or projected_run.get("failure_key") != failure_key
        or projected_run.get("run_authority_sha256")
        != canonical_json_sha256(immutable_run)
        or projected_run.get("run_feedback_sha256")
        != canonical_json_sha256(run.run_json)
        or not isinstance(assistance, Mapping)
        or assistance.get("run_id") != projection.run_id
        or projection.projection_json.get("source_evidence_ids") != list(evidence_ids)
        or projection.projection_json.get("source_feedback_event_id")
        != projection.source_event_id
        or not isinstance(source_event, Mapping)
        or source_event.get("event_id") != projection.source_event_id
        or projection.projection_json.get("source_feedback_event_sha256")
        != canonical_json_sha256(source_event)
    ):
        raise ValueError("learner projection Game Run authority drifted")


def _game_run_outcome(run: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Recover the writer's task outcome from the immutable Game Run document."""

    status = run.get("status")
    sandbox = run.get("sandbox")
    world = run.get("world_application")
    if run.get("terminal") is not True or not isinstance(sandbox, Mapping) or not isinstance(
        world, Mapping
    ):
        raise ValueError("Game Run terminal outcome is malformed")
    if (
        status == "SUCCEEDED"
        and sandbox.get("status") == "SUCCEEDED"
        and sandbox.get("failure") is None
        and world.get("status") == "COMMITTED"
        and isinstance(world.get("receipt"), Mapping)
        and world.get("failure") is None
    ):
        return True, None
    failure = world.get("failure")
    details = failure.get("details") if isinstance(failure, Mapping) else None
    if (
        status == "REJECTED"
        and sandbox.get("status") == "SUCCEEDED"
        and sandbox.get("failure") is None
        and world.get("status") == "REJECTED"
        and world.get("receipt") is None
        and isinstance(failure, Mapping)
        and failure.get("code") == "WORLD_RULE_REJECTED"
        and isinstance(details, Mapping)
        and details.get("reason") == "TASK_INCOMPLETE"
    ):
        return False, "task_incomplete"
    if (
        status == "FAILED"
        and sandbox.get("status") in {"FAILED", "TIMED_OUT"}
        and isinstance(sandbox.get("failure"), Mapping)
        and world.get("status") == "NOT_ATTEMPTED"
        and world.get("receipt") is None
        and world.get("failure") is None
    ):
        return False, "sandbox_execution_failed"
    raise ValueError("Game Run terminal outcome is malformed")


def _game_run_evidence_refs(run: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = run.get("evidence_refs")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Game Run Evidence references are malformed")
    refs: list[Mapping[str, Any]] = []
    evidence_ids: set[str] = set()
    for value in raw:
        if not isinstance(value, Mapping):
            raise ValueError("Game Run Evidence references are malformed")
        evidence_id = value.get("evidence_id")
        digest = value.get("sha256")
        if (
            not isinstance(evidence_id, str)
            or not evidence_id
            or evidence_id in evidence_ids
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("Game Run Evidence references are malformed")
        evidence_ids.add(evidence_id)
        refs.append(value)
    return tuple(refs)


def _game_run_evidence_ids(run: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(value["evidence_id"]) for value in _game_run_evidence_refs(run))


def _validate_projection_assistance(
    projection: LearnerProjectionJobRow,
    run: SkillRunProvenanceRow,
    build: SkillBuildProvenanceRow,
) -> None:
    assistance = projection.projection_json.get("assistance")
    expected = {
        "authority_version": "1.0.0",
        "provenance_kind": run.provenance_kind,
        "run_id": run.run_id,
        "run_authority_sha256": run.authority_sha256,
        "build_id": build.build_id,
        "build_authority_sha256": build.authority_sha256,
        "activation_id": run.activation_id,
        "activation_sha256": run.activation_sha256,
        "activation_authority_sha256": run.activation_authority_sha256,
        "registry_revision": run.registry_revision,
        "certification_id": run.certification_id,
        "certification_sha256": run.certification_sha256,
        "certification_authority_sha256": run.certification_authority_sha256,
        "artifact_sha256": run.artifact_sha256,
        "artifact_authority_sha256": run.artifact_authority_sha256,
        "draft_revision_row_id": run.draft_revision_row_id,
        "origin_accepted_revision_row_id": build.origin_accepted_revision_row_id,
        "draft_sha256": run.draft_sha256,
        "assistance_authority": run.assistance_authority,
        "patch_id": build.patch_id,
        "patch_decision_id": build.patch_decision_id,
        "used_skill_patch": run.assistance_authority == "SKILL_PATCH",
    }
    if (
        projection.tenant_id != run.tenant_id
        or projection.actor_id != run.actor_id
        or projection.session_id != run.session_id
        or projection.run_id != run.run_id
        or run.build_id != build.build_id
        or run.tenant_id != build.tenant_id
        or run.actor_id != build.actor_id
        or not (
            run.provenance_kind == build.provenance_kind
            or (
                run.provenance_kind == "LEGACY_V04_ACTIVE"
                and build.provenance_kind == "LEGACY_V04"
            )
        )
        or run.build_authority_sha256 != build.authority_sha256
        or run.draft_revision_row_id != build.draft_revision_row_id
        or run.draft_sha256 != build.draft_sha256
        or run.assistance_authority != build.assistance_authority
        or run.authority_sha256 != run_provenance_sha256(run)
        or build.authority_sha256 != build_provenance_sha256(build)
        or assistance != expected
    ):
        raise ValueError("learner projection assistance authority drifted")


def _projection_content_key(
    projection: LearnerProjectionJobRow,
) -> tuple[str, str, str]:
    command = projection.projection_json.get("command")
    context = command.get("request_context") if isinstance(command, Mapping) else None
    reference = context.get("content_ref") if isinstance(context, Mapping) else None
    if not isinstance(reference, Mapping):
        raise ValueError("learner projection Content reference is malformed")
    unit_id = reference.get("unit_id")
    version = reference.get("version")
    content_hash = reference.get("content_hash")
    if (
        not isinstance(unit_id, str)
        or not unit_id
        or not isinstance(version, str)
        or not version
        or content_hash != projection.content_hash
    ):
        raise ValueError("learner projection Content reference is malformed")
    return unit_id, version, projection.content_hash


def _validate_projection_task(
    projection: LearnerProjectionJobRow,
    content: ProductContentUnitRow,
) -> None:
    unit_id, version, content_hash = _projection_content_key(projection)
    reference = content.content_json.get("content_ref")
    task = projection.projection_json.get("task")
    durable_task = content.content_json.get("task")
    knowledge_points = (
        durable_task.get("knowledge_points") if isinstance(durable_task, Mapping) else None
    )
    concept = task.get("concept") if isinstance(task, Mapping) else None
    if (
        content.tenant_id != projection.tenant_id
        or content.unit_id != unit_id
        or content.version != version
        or content.content_hash != content_hash
        or not isinstance(reference, Mapping)
        or reference.get("unit_id") != content.unit_id
        or reference.get("version") != content.version
        or reference.get("content_hash") != content.content_hash
        or content.content_json.get("audiences") != content.audiences
        or not isinstance(task, Mapping)
        or set(task) != {"task_id", "concept", "task_sha256"}
        or not isinstance(durable_task, Mapping)
        or task.get("task_id") != durable_task.get("task_id")
        or task.get("task_sha256") != canonical_json_sha256(durable_task)
        or not isinstance(concept, str)
        or not concept
        or isinstance(knowledge_points, str | bytes | bytearray)
        or not isinstance(knowledge_points, Sequence)
        or concept not in knowledge_points
    ):
        raise ValueError("learner projection Content task authority drifted")


def _projection_receipt_wire(receipt: JobStepReceiptRow) -> dict[str, Any]:
    if receipt.completed_at.tzinfo is None:
        raise ValueError("learner projection receipt timestamp is not timezone-aware")
    return {
        "receipt_id": receipt.receipt_id,
        "step_name": receipt.step_name,
        "fencing_token": receipt.fencing_token,
        "input_sha256": receipt.input_sha256,
        "output_sha256": receipt.output_sha256,
        "receipt_json": dict(receipt.receipt_json),
        "completed_at": receipt.completed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


def _validate_projection_receipt(
    projection: LearnerProjectionJobRow,
    receipt: JobStepReceiptRow,
    parent: WorkflowJobRow,
) -> None:
    result = projection.result_json
    commit = receipt.receipt_json
    learner_commit = commit.get("learner")
    committed_profile = (
        learner_commit.get("profile") if isinstance(learner_commit, Mapping) else None
    )
    committed_projection = (
        learner_commit.get("projection") if isinstance(learner_commit, Mapping) else None
    )
    terminal_learner = result.get("learner") if isinstance(result, Mapping) else None
    learner_update = (
        committed_projection.get("learner_update")
        if isinstance(committed_projection, Mapping)
        else None
    )
    profile_content = (
        committed_profile.get("content") if isinstance(committed_profile, Mapping) else None
    )
    objective_projection = projection.projection_json.get("projection")
    task = projection.projection_json.get("task")
    command_commit = commit.get("command")
    committed_command = (
        command_commit.get("record") if isinstance(command_commit, Mapping) else None
    )
    command_context = (
        committed_command.get("request_context")
        if isinstance(committed_command, Mapping)
        else None
    )
    command_actor = (
        command_context.get("actor") if isinstance(command_context, Mapping) else None
    )
    command_content = (
        command_context.get("content_ref")
        if isinstance(command_context, Mapping)
        else None
    )
    update_refs = learner_update.get("evidence_refs") if isinstance(learner_update, Mapping) else None
    update_evidence_ids = (
        [item.get("evidence_id") for item in update_refs if isinstance(item, Mapping)]
        if isinstance(update_refs, list)
        else None
    )
    if (
        not isinstance(result, Mapping)
        or parent.tenant_id != projection.tenant_id
        or parent.job_id != projection.job_id
        or parent.command_id != projection.command_id
        or parent.operation != "EXECUTE_AGENT_TURN"
        or parent.subject_type != "AGENT_TURN"
        or parent.subject_id != projection.turn_id
        or parent.status != "SUCCEEDED"
        or parent.phase != "COMPLETE"
        or receipt.tenant_id != projection.tenant_id
        or receipt.job_id != projection.job_id
        or receipt.step_name != "LEARNER_PROJECTION_COMMITTED"
        or receipt.receipt_id
        != workflow_step_receipt_id(
            projection.tenant_id,
            projection.job_id,
            "LEARNER_PROJECTION_COMMITTED",
        )
        or receipt.input_sha256 != projection.request_sha256
        or receipt.fencing_token != parent.fencing_token
        or receipt.output_sha256 != workflow_receipt_sha256(commit)
        or result.get("projection_receipt") != _projection_receipt_wire(receipt)
        or set(commit) != {"schema_version", "learner", "interaction", "workspace", "command"}
        or commit.get("schema_version") != "1.0.0"
        or not isinstance(learner_commit, Mapping)
        or set(learner_commit) != {
            "profile_sha256",
            "profile",
            "projection_sha256",
            "projection",
        }
        or not isinstance(committed_profile, Mapping)
        or committed_profile.get("learner_id") != projection.learner_id
        or committed_profile.get("actor_id") != projection.actor_id
        or not isinstance(profile_content, Mapping)
        or profile_content.get("content_hash") != projection.content_hash
        or committed_profile.get("revision") != projection.expected_revision + 1
        or committed_profile.get("projected_through_sequence") != projection.through_sequence
        or learner_commit.get("profile_sha256") != canonical_json_sha256(committed_profile)
        or not isinstance(committed_projection, Mapping)
        or learner_commit.get("projection_sha256")
        != canonical_json_sha256(committed_projection)
        or committed_projection.get("source_evidence_ids")
        != projection.projection_json.get("source_evidence_ids")
        or committed_projection.get("source_feedback_event_id") != projection.source_event_id
        or committed_projection.get("profile_sha256") != learner_commit.get("profile_sha256")
        or not isinstance(learner_update, Mapping)
        or learner_update.get("learner_id") != projection.learner_id
        or learner_update.get("previous_revision") != projection.expected_revision
        or learner_update.get("learner_revision") != projection.expected_revision + 1
        or learner_update.get("projected_through_sequence") != projection.through_sequence
        or not isinstance(objective_projection, Mapping)
        or learner_update.get("updated_at") != objective_projection.get("recorded_at")
        or update_evidence_ids != projection.projection_json.get("source_evidence_ids")
        or not isinstance(learner_update.get("changed_competency_ids"), list)
        or not isinstance(task, Mapping)
        or task.get("concept") not in learner_update.get("changed_competency_ids", ())
        or not isinstance(terminal_learner, Mapping)
        or terminal_learner.get("learner_id") != projection.learner_id
        or terminal_learner.get("revision") != projection.expected_revision + 1
        or terminal_learner.get("projected_through_sequence") != projection.through_sequence
        or terminal_learner.get("profile_sha256") != learner_commit.get("profile_sha256")
        or terminal_learner.get("evidence_id") != committed_projection.get("evidence_id")
        or terminal_learner.get("evidence_sha256")
        != committed_projection.get("evidence_sha256")
        or terminal_learner.get("event_id") != committed_projection.get("learner_event_id")
        or terminal_learner.get("event_sha256")
        != committed_projection.get("learner_event_sha256")
        or terminal_learner.get("event_payload_sha256")
        != canonical_json_sha256(learner_update)
        or not isinstance(command_commit, Mapping)
        or not isinstance(committed_command, Mapping)
        or command_commit.get("record_sha256") != canonical_json_sha256(committed_command)
        or committed_command.get("command_id") != projection.command_id
        or not isinstance(command_actor, Mapping)
        or command_actor.get("tenant_id") != projection.tenant_id
        or command_actor.get("actor_id") != projection.actor_id
        or not isinstance(command_content, Mapping)
        or command_content.get("content_hash") != projection.content_hash
    ):
        raise ValueError("learner projection commit receipt authority drifted")


def _validate_projection_source_event(
    projection: LearnerProjectionJobRow,
    event: EventRow,
) -> None:
    embedded = projection.projection_json.get("source_feedback_event")
    content = event.event_json.get("content_ref")
    if (
        event.tenant_id != projection.tenant_id
        or event.event_id != projection.source_event_id
        or not isinstance(embedded, Mapping)
        or event.event_json != dict(embedded)
        or projection.projection_json.get("source_feedback_event_sha256")
        != canonical_json_sha256(event.event_json)
        or event.event_json.get("event_id") != projection.source_event_id
        or event.event_json.get("event_type")
        != RuntimeEventType.AGENT_TURN_FEEDBACK_READY.value
        or event.event_json.get("command_id") != projection.command_id
        or event.event_json.get("payload") != projection.projection_json.get("feedback")
        or not isinstance(content, Mapping)
        or content.get("content_hash") != projection.content_hash
    ):
        raise ValueError("learner projection source Event authority drifted")


def _learner_evidence_binding(projection: LearnerProjectionJobRow) -> tuple[str, str]:
    result = projection.result_json
    learner = result.get("learner") if isinstance(result, Mapping) else None
    evidence_id = learner.get("evidence_id") if isinstance(learner, Mapping) else None
    evidence_sha256 = (
        learner.get("evidence_sha256") if isinstance(learner, Mapping) else None
    )
    if (
        not isinstance(evidence_id, str)
        or not evidence_id
        or not isinstance(evidence_sha256, str)
        or len(evidence_sha256) != 64
        or any(character not in "0123456789abcdef" for character in evidence_sha256)
    ):
        raise ValueError("learner projection derived Evidence binding is malformed")
    return evidence_id, evidence_sha256


def _validate_projection_learner_evidence_authority(
    projection: LearnerProjectionJobRow,
    run: RunRow,
    evidence_by_id: Mapping[str, EvidenceRow],
) -> None:
    evidence_id, evidence_sha256 = _learner_evidence_binding(projection)
    evidence = evidence_by_id.get(evidence_id)
    if evidence is None:
        raise ValueError("learner projection derived Evidence authority is missing")
    _validate_evidence_projection(projection, evidence)
    document = evidence.evidence_json
    reference = document.get("evidence_ref")
    request_context = document.get("request_context")
    subject = document.get("subject")
    source = document.get("source")
    integrity = document.get("integrity")
    payload = document.get("payload")
    related = document.get("related_evidence")
    task = projection.projection_json.get("task")
    assistance = projection.projection_json.get("assistance")
    session = projection.projection_json.get("session")
    objective_projection = projection.projection_json.get("projection")
    source_event = projection.projection_json.get("source_feedback_event")
    command = projection.projection_json.get("command")
    command_versions = command.get("versions") if isinstance(command, Mapping) else None
    expected_versions = (
        {key: value for key, value in command_versions.items() if value is not None}
        if isinstance(command_versions, Mapping)
        else None
    )
    recorded_at = document.get("recorded_at")
    occurred_at = document.get("occurred_at")
    if not isinstance(source_event, Mapping):
        raise ValueError("learner projection derived Evidence authority drifted")
    try:
        recorded_time = _evidence_time(recorded_at)
        occurred_time = _evidence_time(occurred_at)
        source_event_time = _evidence_time(source_event.get("occurred_at"))
    except ValueError as error:
        raise ValueError("learner projection derived Evidence authority drifted") from error
    task_success, failure_key = _game_run_outcome(run.run_json)
    expected_payload = {
        "evidence_kind": "LEARNER_OBSERVATION",
        "observation_type": "TASK_COMPLETION" if task_success else "CODE_ATTEMPT",
        "task_id": task.get("task_id") if isinstance(task, Mapping) else None,
        "outcome": (
            "SUCCESS"
            if task_success
            else "FAILED"
            if failure_key == "sandbox_execution_failed"
            else "PARTIAL"
        ),
        "assistance_level": (
            4
            if isinstance(assistance, Mapping)
            and assistance.get("used_skill_patch") is True
            else 0
        ),
    }
    if (
        set(document)
        != {
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
        or canonical_json_sha256(document) != evidence_sha256
        or not isinstance(evidence.command_id, str)
        or not evidence.command_id
        or evidence.command_id != projection.command_id
        or evidence.recorded_at.tzinfo is None
        or evidence.recorded_at.astimezone(UTC) != recorded_time.astimezone(UTC)
        or not isinstance(request_context, Mapping)
        or dict(request_context) != dict(run.run_json["request_context"])
        or subject != {"learner_id": projection.learner_id}
        or not isinstance(source, Mapping)
        or set(source) != {"source_type", "source_id", "command_id", "world_id"}
        or source.get("source_type") != "LEARNER_PROJECTOR"
        or source.get("source_id") != projection.learner_id
        or source.get("command_id") != projection.command_id
        or not isinstance(session, Mapping)
        or source.get("world_id") != session.get("world_id")
        or not isinstance(objective_projection, Mapping)
        or recorded_at != objective_projection.get("recorded_at")
        or occurred_time.astimezone(UTC) != source_event_time.astimezone(UTC)
        or document.get("versions") != expected_versions
        or not isinstance(payload, Mapping)
        or dict(payload) != expected_payload
        or not isinstance(integrity, Mapping)
        or integrity
        != {
            "payload_sha256": canonical_json_sha256(expected_payload),
            "previous_evidence_sha256": None,
        }
        or not isinstance(reference, Mapping)
        or set(reference)
        != {"evidence_id", "evidence_type", "created_at", "sha256", "uri"}
        or reference.get("evidence_id") != evidence_id
        or reference.get("evidence_type") != "LEARNER_UPDATE"
        or not isinstance(reference.get("created_at"), str)
        or reference.get("created_at") != recorded_at
        or reference.get("sha256") != canonical_json_sha256(expected_payload)
        or reference.get("uri") != f"/v1/evidence/{evidence_id}"
        or related != [dict(item) for item in _game_run_evidence_refs(run.run_json)]
    ):
        raise ValueError("learner projection derived Evidence authority drifted")


def _evidence_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Evidence timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Evidence timestamp is malformed") from error
    if parsed.tzinfo is None:
        raise ValueError("Evidence timestamp is not timezone-aware")
    return parsed


def _validate_projection_evidence_authority(
    projection: LearnerProjectionJobRow,
    run: RunRow,
    evidence_by_id: Mapping[str, EvidenceRow],
) -> None:
    for reference in _game_run_evidence_refs(run.run_json):
        evidence_id = str(reference["evidence_id"])
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            raise ValueError("learner projection Run Evidence authority is missing")
        try:
            validate_evidence_document_authority(evidence)
        except WorkflowInvariantError as error:
            raise ValueError("learner projection Run Evidence authority drifted") from error
        _validate_evidence_projection(projection, evidence)
        document_ref = evidence.evidence_json.get("evidence_ref")
        request_context = evidence.evidence_json.get("request_context")
        subject = evidence.evidence_json.get("subject")
        source = evidence.evidence_json.get("source")
        integrity = evidence.evidence_json.get("integrity")
        payload = evidence.evidence_json.get("payload")
        command = projection.projection_json.get("command")
        versions = command.get("versions") if isinstance(command, Mapping) else None
        expected_versions = (
            {key: value for key, value in versions.items() if value is not None}
            if isinstance(versions, Mapping)
            else None
        )
        session = projection.projection_json.get("session")
        world_id = session.get("world_id") if isinstance(session, Mapping) else None
        evidence_kind = payload.get("evidence_kind") if isinstance(payload, Mapping) else None
        expected_source = (
            {
                "source_type": "SKILL_RUN",
                "source_id": projection.run_id,
                "command_id": projection.command_id,
                "world_id": world_id,
            }
            if evidence_kind == "SKILL_RUN"
            else {
                "source_type": "WORLD",
                "source_id": world_id,
                "command_id": projection.command_id,
                "world_id": world_id,
            }
            if evidence_kind == "WORLD_COMMIT"
            else None
        )
        if (
            not isinstance(document_ref, Mapping)
            or dict(document_ref) != dict(reference)
            or not isinstance(request_context, Mapping)
            or dict(request_context) != dict(run.run_json["request_context"])
            or subject != {"learner_id": projection.actor_id}
            or source != expected_source
            or evidence.evidence_json.get("occurred_at") != reference.get("created_at")
            or evidence.evidence_json.get("related_evidence") != []
            or evidence.evidence_json.get("versions") != expected_versions
            or not isinstance(integrity, Mapping)
            or not isinstance(payload, Mapping)
            or integrity.get("payload_sha256") != reference.get("sha256")
            or reference.get("sha256") != canonical_json_sha256(payload)
        ):
            raise ValueError("learner projection Run Evidence authority drifted")


def _workflow_json_sha256(value: Mapping[str, Any]) -> str:
    """Use the learner-job writer's finite internal JSON hashing contract."""

    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("learner projection JSON is not finite") from error
    return hashlib.sha256(payload).hexdigest()


def _validate_evidence_projection(
    projection: LearnerProjectionJobRow,
    evidence: EvidenceRow,
) -> None:
    if (
        projection.tenant_id != evidence.tenant_id
        or projection.actor_id != evidence.actor_id
        or projection.content_hash != evidence.content_hash
        or (
            evidence.command_id is not None
            and projection.command_id != evidence.command_id
        )
    ):
        raise ValueError("linked evidence authority drifted")


def _projection(row: LearnerProjectionJobRow) -> LearningProjectionAuthority:
    if row.completed_at is None or row.result_json is None:
        raise ValueError("SUCCEEDED learner projection lacks terminal authority")
    return LearningProjectionAuthority(
        job_id=row.job_id,
        command_id=row.command_id,
        session_id=row.session_id,
        turn_id=row.turn_id,
        run_id=row.run_id,
        learner_id=row.learner_id,
        source_event_id=row.source_event_id,
        through_sequence=row.through_sequence,
        projection=dict(row.projection_json),
        result=dict(row.result_json),
        completed_at=row.completed_at,
    )


def _links_evidence(row: LearnerProjectionJobRow, evidence_id: str) -> bool:
    source_ids = row.projection_json.get("source_evidence_ids")
    if isinstance(source_ids, list) and evidence_id in source_ids:
        return True
    result = row.result_json
    if not isinstance(result, Mapping):
        return False
    learner = result.get("learner")
    return isinstance(learner, Mapping) and learner.get("evidence_id") == evidence_id


def _linked_evidence_ids(row: LearnerProjectionJobRow) -> tuple[str, ...]:
    values: list[str] = []
    source_ids = row.projection_json.get("source_evidence_ids")
    if isinstance(source_ids, list):
        values.extend(value for value in source_ids if isinstance(value, str) and value)
    result = row.result_json
    learner = result.get("learner") if isinstance(result, Mapping) else None
    derived = learner.get("evidence_id") if isinstance(learner, Mapping) else None
    if isinstance(derived, str) and derived:
        values.append(derived)
    return tuple(dict.fromkeys(values))


def _not_found(message: str) -> ContractError:
    return _error("NOT_FOUND", ErrorCategory.VALIDATION, "resource.not_found", "READ", message)


def _invariant(message: str) -> ContractError:
    return _error(
        "INVARIANT_VIOLATION",
        ErrorCategory.INVARIANT,
        "system.invariant_violation",
        "READ",
        message,
    )


def _internal(message: str) -> ContractError:
    return _error(
        "INTERNAL_ERROR", ErrorCategory.INTERNAL, "system.internal_error", "READ", message
    )


def _error(
    code: str,
    category: ErrorCategory,
    user_message_key: str,
    stage: str,
    message: str,
) -> ContractError:
    return ContractError(
        code=code,
        category=category,
        retryable=False,
        user_message_key=user_message_key,
        stage=stage,
        message=message[:512] or code,
    )


__all__ = ["PostgresFeishuLearningStore", "PostgresFeishuLearningSyncReader"]
