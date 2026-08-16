"""Authority-integrity closure for the PostgreSQL Feishu read adapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from yaya_agent_contracts import RuntimeEventType, canonical_json_sha256

from walnut_backend.adapters.postgres import feishu_learning as feishu_adapter
from walnut_backend.adapters.postgres.feishu_learning import (
    _profile,
    _validate_evidence_projection,
    _validate_projection_assistance,
    _validate_projection_evidence_authority,
    _validate_projection_learner_evidence_authority,
    _validate_projection_receipt,
    _validate_projection_run_authority,
    _validate_projection_runs,
    _validate_projection_source_event,
    _validate_projection_task,
    _workflow_json_sha256,
)
from walnut_backend.adapters.postgres.models import (
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
from walnut_backend.adapters.postgres.skill_provenance import (
    build_provenance_sha256,
    run_provenance_sha256,
)
from walnut_backend.adapters.postgres.workflow_jobs import (
    workflow_receipt_sha256,
    workflow_step_receipt_id,
)

NOW = datetime(2026, 8, 16, 1, 0, tzinfo=UTC)
TENANT = "tenant_feishu_integrity"
ACTOR = "student_actor_integrity"
LEARNER = "learner_integrity"
CONTENT_HASH = "a" * 64
JOB_ID = "job_feishu_integrity"
COMMAND_ID = "cmd_feishu_integrity"
SESSION_ID = "session_feishu_integrity"
TURN_ID = "turn_feishu_integrity"
RUN_ID = "run_feishu_integrity"
EVENT_ID = "event_feishu_integrity"
EVIDENCE_ID = "evidence_feishu_integrity"
EVIDENCE_PAYLOAD = {"evidence_kind": "SKILL_RUN", "run_id": RUN_ID}
EVIDENCE_REF = {
    "evidence_id": EVIDENCE_ID,
    "evidence_type": "SANDBOX_LOG",
    "created_at": "2026-08-16T01:00:00Z",
    "sha256": canonical_json_sha256(EVIDENCE_PAYLOAD),
}
CONTENT_REF = {
    "unit_id": "content_feishu_integrity",
    "version": "1.0.0",
    "content_hash": CONTENT_HASH,
}
REQUEST_CONTEXT = {
    "request_id": "request_feishu_integrity",
    "correlation_id": "correlation_feishu_integrity",
    "trace_id": "trace_feishu_integrity",
    "requested_at": "2026-08-16T01:00:00Z",
    "actor": {"tenant_id": TENANT, "actor_id": ACTOR},
    "content_ref": CONTENT_REF,
    "schema_version": "1.0.0",
}
TASK = {
    "task_id": "task_feishu_integrity",
    "knowledge_points": ["loops"],
}
FEEDBACK = {"message": "bounded feedback"}
SOURCE_EVENT = {
    "event_id": EVENT_ID,
    "event_type": RuntimeEventType.AGENT_TURN_FEEDBACK_READY.value,
    "command_id": COMMAND_ID,
    "content_ref": CONTENT_REF,
    "occurred_at": "2026-08-16T01:00:00Z",
    "payload": FEEDBACK,
}


def test_projection_requires_exact_game_run_and_frozen_provenance_hashes() -> None:
    run = _run()
    projection = _projection(run)

    _validate_projection_run_authority(projection, run)


@pytest.mark.parametrize(
    "drift",
    (
        "tenant",
        "actor",
        "session",
        "command",
        "run",
        "content",
        "identity",
        "provenance",
    ),
)
def test_projection_game_run_drift_fails_closed(drift: str) -> None:
    run = _run()
    projection = _projection(run)

    if drift == "tenant":
        run.tenant_id = "tenant_other"
    elif drift == "actor":
        run.actor_id = "actor_other"
    elif drift == "session":
        run.session_id = "session_other"
    elif drift == "command":
        run.command_id = "cmd_other"
    elif drift == "run":
        run.run_id = "run_other"
    elif drift == "content":
        run.content_hash = "b" * 64
    elif drift == "identity":
        cast(dict[str, Any], projection.projection_json["identity"])["turn_id"] = "turn_other"
    elif drift == "provenance":
        cast(dict[str, Any], projection.projection_json["run"])[
            "run_feedback_sha256"
        ] = "0" * 64

    with pytest.raises(ValueError, match="Game Run authority drifted"):
        _validate_projection_run_authority(projection, run)


def test_projection_source_event_must_equal_row_authority() -> None:
    run = _run()
    projection = _projection(run)
    projection.projection_json["source_feedback_event_id"] = "event_drifted"
    projection.request_sha256 = _workflow_json_sha256(projection.projection_json)

    with pytest.raises(ValueError, match="Game Run authority drifted"):
        _validate_projection_run_authority(projection, run)


def test_projection_source_event_reloads_exact_runtime_event() -> None:
    projection = _projection(_run())
    event = EventRow(
        event_id=EVENT_ID,
        tenant_id=TENANT,
        stream_id="session:integrity",
        sequence=1,
        occurred_at=NOW,
        event_json=dict(SOURCE_EVENT),
    )

    _validate_projection_source_event(projection, event)
    event.event_json["payload"] = {"message": "forged"}

    with pytest.raises(ValueError, match="source Event authority drifted"):
        _validate_projection_source_event(projection, event)


def test_missing_game_run_fails_closed_for_bulk_reader() -> None:
    projection = _projection(_run())
    session = cast(AsyncSession, _EmptyRunSession())

    with pytest.raises(ValueError, match="no exact Game Run authority"):
        asyncio.run(_validate_projection_runs(session, (projection,)))


def test_evidence_link_requires_same_tenant_actor_content_and_command() -> None:
    projection = _projection(_run())
    evidence = _evidence()

    _validate_evidence_projection(projection, evidence)
    evidence.content_hash = "b" * 64

    with pytest.raises(ValueError, match="linked evidence authority drifted"):
        _validate_evidence_projection(projection, evidence)


def test_run_source_evidence_closes_over_exact_document_and_payload_hash() -> None:
    run = _run()
    projection = _projection(run)
    evidence = _evidence()

    _validate_projection_evidence_authority(
        projection,
        run,
        {evidence.evidence_id: evidence},
    )
    cast(dict[str, Any], evidence.evidence_json["payload"])["run_id"] = "run_drifted"

    with pytest.raises(ValueError, match="Run Evidence authority drifted"):
        _validate_projection_evidence_authority(
            projection,
            run,
            {evidence.evidence_id: evidence},
        )


def test_profile_hash_uses_the_writer_canonical_json_contract() -> None:
    profile_json = {
        "learner_id": LEARNER,
        "actor_id": ACTOR,
        "content": {"content_hash": CONTENT_HASH},
    }
    profile = LearnerProfileRow(
        tenant_id=TENANT,
        learner_id=LEARNER,
        actor_id=ACTOR,
        content_hash=CONTENT_HASH,
        profile_sha256=canonical_json_sha256(profile_json),
        profile_json=profile_json,
        created_at=NOW,
        updated_at=NOW,
    )

    _profile("pseudonym-secret-" + "s" * 32, profile)
    profile.profile_sha256 = "0" * 64

    with pytest.raises(ValueError, match="Profile authority hash drifted"):
        _profile("pseudonym-secret-" + "s" * 32, profile)


@pytest.mark.parametrize("drift", ("learner", "actor", "content"))
def test_profile_rejects_rehashed_inner_identity_drift(drift: str) -> None:
    profile_json: dict[str, Any] = {
        "learner_id": LEARNER,
        "actor_id": ACTOR,
        "content": {"content_hash": CONTENT_HASH},
    }
    profile = LearnerProfileRow(
        tenant_id=TENANT,
        learner_id=LEARNER,
        actor_id=ACTOR,
        content_hash=CONTENT_HASH,
        profile_sha256=canonical_json_sha256(profile_json),
        profile_json=profile_json,
        created_at=NOW,
        updated_at=NOW,
    )
    if drift == "learner":
        profile.profile_json["learner_id"] = "learner_drifted"
    elif drift == "actor":
        profile.profile_json["actor_id"] = "actor_drifted"
    else:
        cast(dict[str, Any], profile.profile_json["content"])["content_hash"] = "c" * 64
    profile.profile_sha256 = canonical_json_sha256(profile.profile_json)

    with pytest.raises(ValueError, match="Profile authority hash drifted"):
        _profile("pseudonym-secret-" + "s" * 32, profile)

def test_projected_assistance_closes_over_run_and_build_provenance() -> None:
    build = _build_provenance()
    run = _run_provenance(build)
    projection = _projection(_run())
    projection.projection_json["assistance"] = _assistance(run, build)

    _validate_projection_assistance(projection, run, build)
    cast(dict[str, Any], projection.projection_json["assistance"])[
        "assistance_authority"
    ] = "SKILL_PATCH"

    with pytest.raises(ValueError, match="assistance authority drifted"):
        _validate_projection_assistance(projection, run, build)


def test_projected_task_closes_over_published_content_authority() -> None:
    projection = _projection(_run())
    content = ProductContentUnitRow(
        tenant_id=TENANT,
        unit_id=CONTENT_REF["unit_id"],
        version=CONTENT_REF["version"],
        content_hash=CONTENT_HASH,
        audiences=["LEARNER"],
        published_at=NOW,
        content_json={
            "content_ref": CONTENT_REF,
            "audiences": ["LEARNER"],
            "task": TASK,
        },
    )

    _validate_projection_task(projection, content)
    cast(dict[str, Any], projection.projection_json["task"])["concept"] = "unbound"

    with pytest.raises(ValueError, match="Content task authority drifted"):
        _validate_projection_task(projection, content)

    cast(dict[str, Any], projection.projection_json["task"])["concept"] = "loops"
    cast(dict[str, Any], projection.projection_json["task"])["task_name"] = (
        "raw chat or source code"
    )
    with pytest.raises(ValueError, match="Content task authority drifted"):
        _validate_projection_task(projection, content)


def test_derived_learner_evidence_rejects_raw_payload_even_when_rehashed() -> None:
    run = _run()
    projection, evidence = _projection_with_derived_evidence(run)

    _validate_projection_learner_evidence_authority(
        projection,
        run,
        {evidence.evidence_id: evidence},
    )
    payload = cast(dict[str, Any], evidence.evidence_json["payload"])
    payload["raw_source_code"] = "print(secret_chat)"
    payload_hash = canonical_json_sha256(payload)
    cast(dict[str, Any], evidence.evidence_json["integrity"])[
        "payload_sha256"
    ] = payload_hash
    cast(dict[str, Any], evidence.evidence_json["evidence_ref"])["sha256"] = payload_hash
    result = cast(dict[str, Any], projection.result_json)
    learner = cast(dict[str, Any], result["learner"])
    learner["evidence_sha256"] = canonical_json_sha256(evidence.evidence_json)
    projection.result_sha256 = _workflow_json_sha256(result)

    with pytest.raises(ValueError, match="derived Evidence authority drifted"):
        _validate_projection_learner_evidence_authority(
            projection,
            run,
            {evidence.evidence_id: evidence},
        )


@pytest.mark.parametrize("drift", ("occurred_at", "recorded_at", "versions", "command"))
def test_derived_evidence_rejects_rehashed_time_version_and_command_drift(
    drift: str,
) -> None:
    run = _run()
    projection, evidence = _projection_with_derived_evidence(run)
    if drift == "occurred_at":
        evidence.evidence_json["occurred_at"] = "2026-08-16T02:00:00Z"
    elif drift == "recorded_at":
        evidence.evidence_json["recorded_at"] = "2026-08-16T02:00:00Z"
        evidence.recorded_at = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)
    elif drift == "versions":
        evidence.evidence_json["versions"] = {"model_version": "forged"}
    else:
        evidence.command_id = None
        cast(dict[str, Any], evidence.evidence_json["source"])["command_id"] = None
    result = cast(dict[str, Any], projection.result_json)
    cast(dict[str, Any], result["learner"])["evidence_sha256"] = canonical_json_sha256(
        evidence.evidence_json
    )
    projection.result_sha256 = _workflow_json_sha256(result)

    with pytest.raises(ValueError, match="derived Evidence authority drifted"):
        _validate_projection_learner_evidence_authority(
            projection,
            run,
            {evidence.evidence_id: evidence},
        )


def test_projection_receipt_is_reloaded_and_hash_closed() -> None:
    projection, _ = _projection_with_derived_evidence(_run())
    receipt = _projection_receipt(projection)
    parent = _parent_workflow()

    _validate_projection_receipt(projection, receipt, parent)
    receipt.output_sha256 = "0" * 64

    with pytest.raises(ValueError, match="commit receipt authority drifted"):
        _validate_projection_receipt(projection, receipt, parent)


def test_projection_receipt_closes_over_parent_turn_fence() -> None:
    projection, _ = _projection_with_derived_evidence(_run())
    receipt = _projection_receipt(projection)
    parent = _parent_workflow()

    assert projection.fencing_token != receipt.fencing_token
    _validate_projection_receipt(projection, receipt, parent)
    parent.fencing_token += 1

    with pytest.raises(ValueError, match="commit receipt authority drifted"):
        _validate_projection_receipt(projection, receipt, parent)


def test_projection_receipt_rejects_rehashed_committed_profile_identity() -> None:
    projection, _ = _projection_with_derived_evidence(_run())
    receipt = _projection_receipt(projection)
    parent = _parent_workflow()
    commit_learner = cast(dict[str, Any], receipt.receipt_json["learner"])
    profile = cast(dict[str, Any], commit_learner["profile"])
    committed_projection = cast(dict[str, Any], commit_learner["projection"])
    profile["learner_id"] = "learner_other"
    profile_sha256 = canonical_json_sha256(profile)
    commit_learner["profile_sha256"] = profile_sha256
    committed_projection["profile_sha256"] = profile_sha256
    commit_learner["projection_sha256"] = canonical_json_sha256(committed_projection)
    result = cast(dict[str, Any], projection.result_json)
    cast(dict[str, Any], result["learner"])["profile_sha256"] = profile_sha256
    receipt.output_sha256 = workflow_receipt_sha256(receipt.receipt_json)
    embedded = cast(dict[str, Any], result["projection_receipt"])
    embedded["output_sha256"] = receipt.output_sha256
    embedded["receipt_json"] = receipt.receipt_json
    projection.result_sha256 = _workflow_json_sha256(result)

    with pytest.raises(ValueError, match="commit receipt authority drifted"):
        _validate_projection_receipt(projection, receipt, parent)


def test_bulk_validation_calls_full_writer_run_provenance_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = _projection(_run())
    provenance = _run_provenance(_build_provenance())
    session = cast(AsyncSession, _SequencedScalarsSession((_run(),), (provenance,)))
    calls: list[str] = []

    async def reject_corrupt_graph(_session: Any, row: SkillRunProvenanceRow) -> None:
        calls.append(row.run_id)
        return None

    monkeypatch.setattr(feishu_adapter, "validate_run_provenance", reject_corrupt_graph)

    with pytest.raises(ValueError, match="assistance graph is corrupt"):
        asyncio.run(_validate_projection_runs(session, (projection,)))
    assert calls == [RUN_ID]


class _EmptyScalars:
    def all(self) -> list[RunRow]:
        return []


class _EmptyRunSession:
    async def scalars(self, _statement: Any) -> _EmptyScalars:
        return _EmptyScalars()


class _SequenceScalars:
    def __init__(self, values: tuple[Any, ...]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return list(self._values)


class _SequencedScalarsSession:
    def __init__(self, *values: tuple[Any, ...]) -> None:
        self._values = list(values)

    async def scalars(self, _statement: Any) -> _SequenceScalars:
        return _SequenceScalars(self._values.pop(0))


def _run() -> RunRow:
    run_json = {
        "request_context": REQUEST_CONTEXT,
        "run_id": RUN_ID,
        "session_id": SESSION_ID,
        "turn_id": TURN_ID,
        "command_id": COMMAND_ID,
        "status": "SUCCEEDED",
        "terminal": True,
        "sandbox": {"status": "SUCCEEDED", "failure": None},
        "world_application": {
            "status": "COMMITTED",
            "receipt": {"world_revision": 1},
            "failure": None,
        },
        "evidence_refs": [EVIDENCE_REF],
        "agent_feedback": {"message_key": "learning.complete"},
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }
    return RunRow(
        run_id=RUN_ID,
        tenant_id=TENANT,
        actor_id=ACTOR,
        content_hash=CONTENT_HASH,
        session_id=SESSION_ID,
        turn_id=TURN_ID,
        command_id=COMMAND_ID,
        created_at=NOW,
        run_json=run_json,
    )


def _projection(run: RunRow) -> LearnerProjectionJobRow:
    immutable_run = dict(run.run_json)
    immutable_run.pop("agent_feedback", None)
    immutable_run.pop("updated_at", None)
    projection_json = {
        "identity": {
            "tenant_id": TENANT,
            "job_id": JOB_ID,
            "command_id": COMMAND_ID,
            "session_id": SESSION_ID,
            "turn_id": TURN_ID,
            "run_id": RUN_ID,
            "learner_id": LEARNER,
            "actor_id": ACTOR,
            "content_hash": CONTENT_HASH,
        },
        "command": {
            "command_id": COMMAND_ID,
            "request_context": REQUEST_CONTEXT,
            "versions": {},
        },
        "run": {
            "run_id": RUN_ID,
            "task_success": True,
            "failure_key": None,
            "run_authority_sha256": canonical_json_sha256(immutable_run),
            "run_feedback_sha256": canonical_json_sha256(run.run_json),
        },
        "assistance": {"run_id": RUN_ID},
        "task": {
            "task_id": TASK["task_id"],
            "concept": "loops",
            "task_sha256": canonical_json_sha256(TASK),
        },
        "source_evidence_ids": [EVIDENCE_ID],
        "source_feedback_event_id": EVENT_ID,
        "source_feedback_event": SOURCE_EVENT,
        "source_feedback_event_sha256": canonical_json_sha256(SOURCE_EVENT),
        "feedback": FEEDBACK,
        "projection": {
            "expected_revision": 0,
            "through_sequence": 1,
            "recorded_at": "2026-08-16T01:00:00Z",
        },
        "session": {"session_id": SESSION_ID, "world_id": "world_feishu_integrity"},
    }
    result_json = {"schema_version": "1.0.0"}
    return LearnerProjectionJobRow(
        job_id=JOB_ID,
        tenant_id=TENANT,
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        turn_id=TURN_ID,
        run_id=RUN_ID,
        learner_id=LEARNER,
        actor_id=ACTOR,
        content_hash=CONTENT_HASH,
        source_event_id=EVENT_ID,
        expected_revision=0,
        through_sequence=1,
        projection_json=projection_json,
        status="SUCCEEDED",
        attempt=1,
        fencing_token=1,
        request_sha256=_workflow_json_sha256(projection_json),
        result_sha256=_workflow_json_sha256(result_json),
        result_json=result_json,
        completed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _evidence() -> EvidenceRow:
    return EvidenceRow(
        evidence_id=EVIDENCE_ID,
        tenant_id=TENANT,
        actor_id=ACTOR,
        content_hash=CONTENT_HASH,
        command_id=COMMAND_ID,
        recorded_at=NOW,
        evidence_json={
            "request_context": REQUEST_CONTEXT,
            "evidence_ref": EVIDENCE_REF,
            "subject": {"learner_id": ACTOR},
            "source": {
                "source_type": "SKILL_RUN",
                "source_id": RUN_ID,
                "command_id": COMMAND_ID,
                "world_id": "world_feishu_integrity",
            },
            "occurred_at": "2026-08-16T01:00:00Z",
            "recorded_at": "2026-08-16T01:00:00Z",
            "integrity": {
                "payload_sha256": canonical_json_sha256(EVIDENCE_PAYLOAD),
                "previous_evidence_sha256": None,
            },
            "payload": dict(EVIDENCE_PAYLOAD),
            "related_evidence": [],
            "versions": {},
        },
    )


def _projection_with_derived_evidence(
    run: RunRow,
) -> tuple[LearnerProjectionJobRow, EvidenceRow]:
    projection = _projection(run)
    evidence_id = "evidence_learner_feishu_integrity"
    payload = {
        "evidence_kind": "LEARNER_OBSERVATION",
        "observation_type": "TASK_COMPLETION",
        "task_id": TASK["task_id"],
        "outcome": "SUCCESS",
        "assistance_level": 0,
    }
    payload_sha256 = canonical_json_sha256(payload)
    reference = {
        "evidence_id": evidence_id,
        "evidence_type": "LEARNER_UPDATE",
        "created_at": "2026-08-16T01:00:00Z",
        "sha256": payload_sha256,
        "uri": f"/v1/evidence/{evidence_id}",
    }
    document = {
        "request_context": REQUEST_CONTEXT,
        "evidence_ref": reference,
        "subject": {"learner_id": LEARNER},
        "source": {
            "source_type": "LEARNER_PROJECTOR",
            "source_id": LEARNER,
            "command_id": COMMAND_ID,
            "world_id": "world_feishu_integrity",
        },
        "occurred_at": "2026-08-16T01:00:00Z",
        "recorded_at": "2026-08-16T01:00:00Z",
        "integrity": {
            "payload_sha256": payload_sha256,
            "previous_evidence_sha256": None,
        },
        "payload": payload,
        "related_evidence": [EVIDENCE_REF],
        "versions": {},
    }
    result = {
        "schema_version": "1.0.0",
        "learner": {
            "profile_sha256": "b" * 64,
            "evidence_id": evidence_id,
            "evidence_sha256": canonical_json_sha256(document),
        },
    }
    projection.result_json = result
    projection.result_sha256 = _workflow_json_sha256(result)
    return projection, EvidenceRow(
        evidence_id=evidence_id,
        tenant_id=TENANT,
        actor_id=ACTOR,
        content_hash=CONTENT_HASH,
        command_id=COMMAND_ID,
        recorded_at=NOW,
        evidence_json=document,
    )


def _projection_receipt(projection: LearnerProjectionJobRow) -> JobStepReceiptRow:
    result = cast(dict[str, Any], projection.result_json)
    evidence_id, evidence_sha256 = (
        cast(dict[str, Any], result["learner"])[key]
        for key in ("evidence_id", "evidence_sha256")
    )
    profile = {
        "learner_id": LEARNER,
        "actor_id": ACTOR,
        "content": CONTENT_REF,
        "revision": 1,
        "projected_through_sequence": 1,
    }
    profile_sha256 = canonical_json_sha256(profile)
    learner_update = {
        "learner_id": LEARNER,
        "previous_revision": 0,
        "learner_revision": 1,
        "projected_through_sequence": 1,
        "changed_competency_ids": ["loops"],
        "updated_at": "2026-08-16T01:00:00Z",
        "evidence_refs": [EVIDENCE_REF],
    }
    committed_projection = {
        "source_feedback_event_id": EVENT_ID,
        "source_evidence_ids": [EVIDENCE_ID],
        "learner_update": learner_update,
        "profile_sha256": profile_sha256,
        "evidence_id": evidence_id,
        "evidence_sha256": evidence_sha256,
        "learner_event_id": "event_learner_feishu_integrity",
        "learner_event_sha256": "d" * 64,
    }
    command = {
        "command_id": COMMAND_ID,
        "request_context": REQUEST_CONTEXT,
    }
    commit = {
        "schema_version": "1.0.0",
        "learner": {
            "profile": profile,
            "profile_sha256": profile_sha256,
            "projection": committed_projection,
            "projection_sha256": canonical_json_sha256(committed_projection),
        },
        "interaction": {},
        "workspace": {},
        "command": {
            "record": command,
            "record_sha256": canonical_json_sha256(command),
        },
    }
    receipt = JobStepReceiptRow(
        receipt_id=workflow_step_receipt_id(
            TENANT,
            JOB_ID,
            "LEARNER_PROJECTION_COMMITTED",
        ),
        tenant_id=TENANT,
        job_id=JOB_ID,
        step_name="LEARNER_PROJECTION_COMMITTED",
        fencing_token=4,
        input_sha256=projection.request_sha256,
        output_sha256=workflow_receipt_sha256(commit),
        receipt_json=commit,
        completed_at=NOW,
    )
    terminal_learner = cast(dict[str, Any], result["learner"])
    terminal_learner.update(
        {
            "learner_id": LEARNER,
            "revision": 1,
            "projected_through_sequence": 1,
            "profile_sha256": profile_sha256,
            "event_id": committed_projection["learner_event_id"],
            "event_sha256": committed_projection["learner_event_sha256"],
            "event_payload_sha256": canonical_json_sha256(learner_update),
        }
    )
    result["projection_receipt"] = {
        "receipt_id": receipt.receipt_id,
        "step_name": receipt.step_name,
        "fencing_token": receipt.fencing_token,
        "input_sha256": receipt.input_sha256,
        "output_sha256": receipt.output_sha256,
        "receipt_json": commit,
        "completed_at": "2026-08-16T01:00:00Z",
    }
    projection.result_sha256 = _workflow_json_sha256(result)
    return receipt


def _parent_workflow() -> WorkflowJobRow:
    return WorkflowJobRow(
        job_id=JOB_ID,
        tenant_id=TENANT,
        command_id=COMMAND_ID,
        operation="EXECUTE_AGENT_TURN",
        subject_type="AGENT_TURN",
        subject_id=TURN_ID,
        phase="COMPLETE",
        status="SUCCEEDED",
        attempt=4,
        fencing_token=4,
        request_sha256="e" * 64,
        job_json={},
        created_at=NOW,
        updated_at=NOW,
    )


def _build_provenance() -> SkillBuildProvenanceRow:
    build = SkillBuildProvenanceRow(
        build_id="build_feishu_integrity",
        provenance_kind="LEGACY_V04",
        legacy_marker_id="legacy_feishu_integrity",
        tenant_id=TENANT,
        actor_id=ACTOR,
        build_request_sha256="1" * 64,
        command_receipt_id=1,
        command_receipt_authority_sha256="2" * 64,
        workflow_job_id="workflow_feishu_integrity",
        workflow_request_sha256="3" * 64,
        session_id=SESSION_ID,
        draft_id=None,
        skill_id="skill_feishu_integrity",
        draft_revision_row_id=None,
        draft_revision=None,
        draft_sha256=None,
        source_bundle_sha256="4" * 64,
        origin_accepted_revision_row_id=None,
        patch_id=None,
        patch_decision_id=None,
        assistance_authority="NONE",
        authority_sha256="0" * 64,
        created_at=NOW,
    )
    build.authority_sha256 = build_provenance_sha256(build)
    return build


def _run_provenance(build: SkillBuildProvenanceRow) -> SkillRunProvenanceRow:
    run = SkillRunProvenanceRow(
        run_id=RUN_ID,
        build_id=build.build_id,
        provenance_kind=build.provenance_kind,
        build_authority_sha256=build.authority_sha256,
        tenant_id=TENANT,
        actor_id=ACTOR,
        session_id=SESSION_ID,
        activation_id="activation_feishu_integrity",
        activation_sha256="5" * 64,
        activation_authority_sha256="6" * 64,
        registry_revision=1,
        certification_id="certification_feishu_integrity",
        certification_sha256="7" * 64,
        certification_authority_sha256="8" * 64,
        artifact_sha256="9" * 64,
        artifact_authority_sha256="a" * 64,
        draft_revision_row_id=None,
        draft_sha256=None,
        assistance_authority="NONE",
        authority_sha256="0" * 64,
        created_at=NOW,
    )
    run.authority_sha256 = run_provenance_sha256(run)
    return run


def _assistance(
    run: SkillRunProvenanceRow,
    build: SkillBuildProvenanceRow,
) -> dict[str, Any]:
    return {
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
        "used_skill_patch": False,
    }
