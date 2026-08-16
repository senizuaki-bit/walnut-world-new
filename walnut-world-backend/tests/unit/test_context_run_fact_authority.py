"""One validated Run must expose one exact context/public fact set."""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    EvidenceRef,
    EvidenceType,
    OperationContext,
    RequestContext,
    SkillRef,
)
from yaya_agent_runtime import RunResultSnapshot

from walnut_backend.adapters.postgres.models import SkillRunProvenanceRow
from walnut_backend.adapters.postgres.run_outcomes import ValidatedRunAuthority
from walnut_backend.adapters.postgres.workflow_jobs import WorkflowInvariantError

NOW = datetime(2026, 8, 14, 20, 40, tzinfo=UTC)
BUILD_ID = "build_context_run_fact_0001"


def test_validated_run_context_view_adds_only_the_proven_build_id() -> None:
    raw = _raw_run()
    authority = _authority(raw, _provenance(raw))

    public = authority.run
    differing_fields = [
        field.name
        for field in fields(RunResultSnapshot)
        if getattr(raw, field.name) != getattr(public, field.name)
    ]

    assert authority.result.run is raw
    assert raw.build_id is None
    assert public.build_id == BUILD_ID
    assert differing_fields == ["build_id"]
    assert public == replace(raw, build_id=BUILD_ID)


@pytest.mark.parametrize(
    "provenance",
    (
        None,
        SimpleNamespace(
            run_id="run_context_run_fact_other",
            build_id=BUILD_ID,
            tenant_id="tenant_walnut",
            actor_id="student_0001",
            session_id="session_context_run_fact_0001",
            certification_id="cert_context_run_fact_0001",
            artifact_sha256="b" * 64,
        ),
    ),
)
def test_validated_run_context_view_rejects_missing_or_unvalidated_provenance(
    provenance: object,
) -> None:
    raw = _raw_run()

    with pytest.raises(WorkflowInvariantError, match="Run provenance"):
        _authority(raw, cast(Any, provenance))


def test_validated_run_context_view_rejects_a_conflicting_existing_build_fact() -> None:
    raw = replace(_raw_run(), build_id="build_context_run_fact_other")

    with pytest.raises(WorkflowInvariantError, match="Run provenance"):
        _authority(raw, _provenance(raw))


def _raw_run() -> RunResultSnapshot:
    actor = ActorRef(
        tenant_id="tenant_walnut",
        actor_id="student_0001",
        actor_type=ActorType.STUDENT,
        roles=("game:player",),
    )
    content = ContentRef("YAYA_FARM_001", "1.0.0", "a" * 64)
    request_context = RequestContext(
        request_id="req_context_run_fact_0001",
        correlation_id="corr_context_run_fact_0001",
        trace_id="trace_context_run_fact_0001",
        requested_at=NOW,
        actor=actor,
        content_ref=content,
    )
    return RunResultSnapshot(
        run_id="run_context_run_fact_0001",
        session_id="session_context_run_fact_0001",
        turn_id="turn_context_run_fact_0001",
        command_id="cmd_context_run_fact_0001",
        world_id="world_watering_0001",
        skill_ref=SkillRef(
            skill_id="skill_watering_0001",
            skill_version_id="skill_version_context_run_fact_0001",
            artifact_sha256="b" * 64,
            certification_id="cert_context_run_fact_0001",
        ),
        task_success=False,
        world_revision_before=0,
        world_revision_after=0,
        world_difference={"score": 7, "intent_count": 7},
        failed_actions=({"reason": "task_incomplete"},),
        failure_key="task_incomplete",
        evidence_refs=(
            EvidenceRef(
                evidence_id="evidence_context_run_fact_0001",
                evidence_type=EvidenceType.SANDBOX_LOG,
                created_at=NOW + timedelta(seconds=1),
                sha256="c" * 64,
            ),
        ),
        world_commit=None,
        request_context=request_context,
    )


def _provenance(run: RunResultSnapshot) -> SkillRunProvenanceRow:
    return SkillRunProvenanceRow(
        run_id=run.run_id,
        build_id=BUILD_ID,
        tenant_id=run.request_context.actor.tenant_id,
        actor_id=run.request_context.actor.actor_id,
        session_id=run.session_id,
        certification_id=run.skill_ref.certification_id,
        artifact_sha256=run.skill_ref.artifact_sha256,
    )


def _authority(
    run: RunResultSnapshot,
    provenance: SkillRunProvenanceRow,
) -> ValidatedRunAuthority:
    actor = run.request_context.actor
    context = OperationContext(
        request_id=run.request_context.request_id,
        correlation_id=run.request_context.correlation_id,
        trace_id=run.request_context.trace_id,
        requested_at=run.request_context.requested_at,
        actor=actor,
        content_ref=run.request_context.content_ref,
        command_id=run.command_id,
        causation_id=None,
        deadline_at=NOW + timedelta(minutes=3),
    )
    return ValidatedRunAuthority(
        result=cast(Any, SimpleNamespace(run=run)),
        run_row=cast(Any, SimpleNamespace()),
        command=cast(Any, SimpleNamespace()),
        job=cast(Any, SimpleNamespace()),
        turn=cast(Any, SimpleNamespace()),
        context=context,
        run_provenance=provenance,
    )
