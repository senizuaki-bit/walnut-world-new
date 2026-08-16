"""PostgreSQL atomicity gates for the Product workspace lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    OperationContext,
    Success,
    canonical_json_sha256,
)

from walnut_backend.adapters.postgres import product_drafts as product_drafts_module
from walnut_backend.adapters.postgres import workflow_jobs as workflow_jobs_module
from walnut_backend.adapters.postgres.agent_sessions import PostgresAgentSessionStore
from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.models import (
    AgentProfileRow,
    AgentSessionRow,
    BuildPolicyRow,
    CommandRow,
    CurrentSessionBindingRow,
    JobStepReceiptRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    ProductContentUnitRow,
    ProductDraftRevisionRow,
    ProductDraftRow,
    ProductIdempotencyReceiptRow,
    ProductWorkspaceRow,
    WorkflowJobRow,
    WorldSnapshotRow,
)
from walnut_backend.adapters.postgres.product_drafts import PostgresProductDraftStore
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
)
from walnut_backend.application.game.agent_sessions import AgentSessions
from walnut_backend.workers import control_worker as control_worker_module
from walnut_backend.workers.control_worker import ControlWorkflowHandler


def test_session_control_creates_starter_draft_and_workspace_without_preseed() -> None:
    asyncio.run(_exercise_session_materialization(_database_url()))


def test_session_control_holds_request_floor_when_database_clock_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_exercise_session_materialization_clock_lag(_database_url(), monkeypatch))


def test_session_control_projection_is_one_recoverable_transaction() -> None:
    asyncio.run(_exercise_session_projection_rollback(_database_url()))


def test_failed_session_control_marks_orphan_failed_without_product_state() -> None:
    asyncio.run(_exercise_failed_session(_database_url()))


def test_draft_put_advances_workspace_and_rolls_back_together() -> None:
    asyncio.run(_exercise_draft_workspace_transaction(_database_url()))


def test_student_draft_writes_append_exact_immutable_revisions() -> None:
    asyncio.run(_exercise_student_draft_revision_history(_database_url()))


@dataclass(frozen=True, slots=True)
class _SessionFixture:
    sessions: async_sessionmaker[AsyncSession]
    commands: PostgresCommandStore
    jobs: PostgresWorkflowJobStore
    handler: ControlWorkflowHandler
    claim: ClaimedWorkflowJob
    context: OperationContext
    tenant_id: str
    actor_id: str
    authority_id: str
    session_id: str
    command_id: str
    world_id: str
    content: dict[str, str]
    starter: dict[str, Any]
    world_revision: int
    last_event_sequence: int
    state_hash: str
    request: dict[str, Any]
    idempotency_key: str


async def _exercise_session_materialization(database_url: str) -> None:
    fixture = await _accept_session(database_url)
    try:
        assert await _lifecycle_state(fixture) == {
            "session_status": "ACTIVE",
            "command_status": "ACCEPTED",
            "command_terminal": False,
            "job_status": "CLAIMED",
            "bindings": 0,
            "drafts": 0,
            "workspaces": 0,
            "receipts": 0,
        }

        await fixture.handler.execute(fixture.claim)

        command = await fixture.commands.get(fixture.command_id, fixture.context)
        assert isinstance(command, Success)
        assert command.value.status.value == "APPLIED"

        state = await _lifecycle_state(fixture)
        assert state == {
            "session_status": "ACTIVE",
            "command_status": "APPLIED",
            "command_terminal": True,
            "job_status": "SUCCEEDED",
            "bindings": 1,
            "drafts": 1,
            "workspaces": 1,
            "receipts": 1,
        }
        draft, workspace, session_resource = await _product_resources(fixture)
        assert draft["revision"] == 1
        assert draft["skill_id"] == fixture.starter["skill_id"]
        assert draft["display_name"] == fixture.starter["display_name"]
        assert draft["source_bundle"] == fixture.starter["source_bundle"]
        assert draft["content_ref"] == fixture.content
        assert workspace["workspace_revision"] == 1
        assert workspace["session"] == session_resource
        assert workspace["content_ref"] == fixture.content
        assert workspace["world_checkpoint"] == {
            "world_id": fixture.world_id,
            "world_revision": fixture.world_revision,
            "last_event_sequence": fixture.last_event_sequence,
            "state_hash": fixture.state_hash,
        }
        assert workspace["skill_draft_refs"] == [
            {
                "draft_id": draft["draft_id"],
                "skill_id": draft["skill_id"],
                "revision": 1,
                "draft_sha256": draft["draft_sha256"],
                "url": (
                    f"/product-experience/v1/sessions/{fixture.session_id}/"
                    f"skill-drafts/{draft['draft_id']}"
                ),
            }
        ]
        assert workspace["last_interaction_sequence"] == 0
        await _assert_session_product_causal_timeline(fixture)
    finally:
        await _dispose(fixture.sessions)


async def _exercise_session_materialization_clock_lag(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_at = datetime.now(UTC) + timedelta(seconds=30)
    projection_now = requested_at + timedelta(seconds=1)

    async def logical_projection_now(_session: AsyncSession) -> datetime:
        return projection_now

    monkeypatch.setattr(workflow_jobs_module, "_database_now", logical_projection_now)
    monkeypatch.setattr(control_worker_module, "_database_now", logical_projection_now)

    fixture = await _accept_session(database_url, requested_at=requested_at)
    try:
        async with fixture.sessions() as session:
            accepted = await session.scalar(
                select(CommandRow).where(
                    CommandRow.tenant_id == fixture.tenant_id,
                    CommandRow.command_id == fixture.command_id,
                )
            )
            initial_session = await session.scalar(
                select(AgentSessionRow).where(
                    AgentSessionRow.tenant_id == fixture.tenant_id,
                    AgentSessionRow.session_id == fixture.session_id,
                )
            )
        assert accepted is not None and initial_session is not None
        assert accepted.accepted_at == requested_at
        assert initial_session.created_at == requested_at

        later_context = replace(
            fixture.context,
            request_id=f"req_replay_{uuid4().hex[:16]}",
            correlation_id=f"corr_replay_{uuid4().hex[:16]}",
            trace_id=f"trace_replay_{uuid4().hex[:16]}",
            requested_at=requested_at + timedelta(seconds=10),
            command_id=f"cmd_replay_{uuid4().hex[:16]}",
        )
        replay = await AgentSessions(
            PostgresAgentSessionStore(fixture.sessions, fixture.commands, fixture.jobs)
        ).accept(
            _json_bytes(fixture.request),
            fixture.idempotency_key,
            later_context,
        )
        assert isinstance(replay, Success)
        replayed_session, replayed_receipt = replay.value
        assert replayed_receipt.created is False
        assert replayed_receipt.command.accepted_at == requested_at
        assert replayed_receipt.command.request_context.requested_at == requested_at
        assert _resource_time(
            replayed_session["request_context"]["requested_at"]
        ) == requested_at

        await fixture.handler.execute(fixture.claim)
        await _assert_session_product_causal_timeline(fixture)
    finally:
        await _dispose(fixture.sessions)


async def _exercise_session_projection_rollback(database_url: str) -> None:
    fixture = await _accept_session(database_url)

    async def fail_terminal_commit(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("injected failure after starter workspace staging")

    try:
        with patch.object(
            fixture.jobs,
            "finish_in_session",
            new=fail_terminal_commit,
        ):
            with pytest.raises(RuntimeError, match="injected failure"):
                await fixture.handler.execute(fixture.claim)

        assert await _lifecycle_state(fixture) == {
            "session_status": "ACTIVE",
            "command_status": "ACCEPTED",
            "command_terminal": False,
            "job_status": "CLAIMED",
            "bindings": 0,
            "drafts": 0,
            "workspaces": 0,
            "receipts": 0,
        }

        # The unchanged claim remains recoverable because the failed transaction
        # did not publish its RUNNING state, receipt, Command CAS, or projections.
        await fixture.handler.execute(fixture.claim)
        recovered = await _lifecycle_state(fixture)
        assert recovered["command_status"] == "APPLIED"
        assert recovered["job_status"] == "SUCCEEDED"
        assert recovered["bindings"] == 1
        assert recovered["drafts"] == 1
        assert recovered["workspaces"] == 1
    finally:
        await _dispose(fixture.sessions)


async def _exercise_failed_session(database_url: str) -> None:
    fixture = await _accept_session(database_url, learner_audience=False)
    try:
        await fixture.handler.execute(fixture.claim)
        assert await _lifecycle_state(fixture) == {
            "session_status": "FAILED",
            "command_status": "FAILED",
            "command_terminal": True,
            "job_status": "FAILED",
            "bindings": 0,
            "drafts": 0,
            "workspaces": 0,
            "receipts": 1,
        }
        async with fixture.sessions() as session:
            resource = await session.scalar(
                select(AgentSessionRow).where(
                    AgentSessionRow.tenant_id == fixture.tenant_id,
                    AgentSessionRow.session_id == fixture.session_id,
                )
            )
            receipt = await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == fixture.tenant_id,
                    JobStepReceiptRow.job_id == fixture.claim.job_id,
                )
            )
        assert resource is not None and resource.session_json["status"] == "FAILED"
        assert receipt is not None and receipt.step_name == "CONTROL_REJECTED"
    finally:
        await _dispose(fixture.sessions)


async def _exercise_draft_workspace_transaction(database_url: str) -> None:
    fixture = await _accept_session(database_url)
    try:
        await fixture.handler.execute(fixture.claim)
        initial_draft, initial_workspace, _ = await _product_resources(fixture)
        draft_id = initial_draft["draft_id"]
        store = PostgresProductDraftStore(fixture.sessions)

        updated_source = "int main() { return 1; }\n"
        update = _draft_update(
            fixture,
            initial_draft,
            display_name="updated starter",
            source=updated_source,
        )
        raw_update = _json_bytes(update)
        outcome = await store.upsert(
            fixture.session_id,
            draft_id,
            update,
            raw_update,
            f"idem_draft_update_{uuid4().hex}",
            _next_context(fixture.context, "draft_update"),
        )
        assert isinstance(outcome, Success)
        assert outcome.value.resource["revision"] == 2

        current_draft, current_workspace, _ = await _product_resources(fixture)
        assert current_draft["revision"] == 2
        assert current_workspace["workspace_revision"] == 2
        assert current_workspace["skill_draft_refs"] == [
            {
                "draft_id": draft_id,
                "skill_id": current_draft["skill_id"],
                "revision": 2,
                "draft_sha256": current_draft["draft_sha256"],
                "url": (
                    f"/product-experience/v1/sessions/{fixture.session_id}/skill-drafts/{draft_id}"
                ),
            }
        ]

        failed_source = "int main() { return 2; }\n"
        failed_update = _draft_update(
            fixture,
            current_draft,
            display_name="must roll back",
            source=failed_source,
        )
        raw_failed = _json_bytes(failed_update)
        failed_key = f"idem_draft_rollback_{uuid4().hex}"

        async def fail_workspace_refresh(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise RuntimeError("injected workspace refresh failure")

        with patch.object(
            product_drafts_module,
            "refresh_workspace_in_session",
            new=fail_workspace_refresh,
        ):
            with pytest.raises(RuntimeError, match="injected workspace refresh failure"):
                await store.upsert(
                    fixture.session_id,
                    draft_id,
                    failed_update,
                    raw_failed,
                    failed_key,
                    _next_context(fixture.context, "draft_rollback"),
                )

        rolled_back_draft, rolled_back_workspace, _ = await _product_resources(fixture)
        assert rolled_back_draft == current_draft
        assert rolled_back_workspace == current_workspace
        async with fixture.sessions() as session:
            receipts = await session.scalar(
                select(func.count())
                .select_from(ProductIdempotencyReceiptRow)
                .where(
                    ProductIdempotencyReceiptRow.tenant_id == fixture.tenant_id,
                    ProductIdempotencyReceiptRow.idempotency_key == failed_key,
                )
            )
        assert int(receipts or 0) == 0
        assert initial_workspace["workspace_revision"] == 1
    finally:
        await _dispose(fixture.sessions)


async def _exercise_student_draft_revision_history(database_url: str) -> None:
    fixture = await _accept_session(database_url)
    try:
        await fixture.handler.execute(fixture.claim)
        initial_draft, _, _ = await _product_resources(fixture)
        rows = await _draft_revision_rows(fixture)
        assert len(rows) == 1
        assert rows[0].revision == 1
        assert rows[0].draft_sha256 == initial_draft["draft_sha256"]
        assert rows[0].entrypoint == initial_draft["source_bundle"]["entrypoint"]
        assert rows[0].source_kind == "STUDENT"
        assert rows[0].patch_id is None
        assert rows[0].draft_json == initial_draft

        source = "int main() { return 7; }\n"
        update = _draft_update(
            fixture,
            initial_draft,
            display_name="immutable revision two",
            source=source,
        )
        store = PostgresProductDraftStore(fixture.sessions)
        result = await store.upsert(
            fixture.session_id,
            str(initial_draft["draft_id"]),
            update,
            _json_bytes(update),
            f"idem_draft_revision_{uuid4().hex}",
            _next_context(fixture.context, "draft_revision"),
        )
        assert isinstance(result, Success)

        rows = await _draft_revision_rows(fixture)
        assert [row.revision for row in rows] == [1, 2]
        assert rows[0].draft_json == initial_draft
        assert rows[1].draft_sha256 == result.value.resource["draft_sha256"]
        assert rows[1].entrypoint == "src/main.cpp"
        assert rows[1].source_kind == "STUDENT"
        assert rows[1].patch_id is None
        assert rows[1].draft_json == result.value.resource
    finally:
        await _dispose(fixture.sessions)


async def _accept_session(
    database_url: str,
    *,
    learner_audience: bool = True,
    student_is_learner: bool = False,
    requested_at: datetime | None = None,
) -> _SessionFixture:
    suffix = uuid4().hex[:20]
    tenant_id = f"tenant_workspace_{suffix}"
    actor_id = f"student_workspace_{suffix}"
    world_id = f"world_workspace_{suffix}"
    learner_id = actor_id if student_is_learner else f"learner_workspace_{suffix}"
    agent_profile_id = f"agent_workspace_{suffix}"
    authority_id = f"authority_workspace_{suffix}"
    build_policy_id = f"policy_workspace_{suffix}"
    unit_id = f"UNIT_WORKSPACE_{suffix.upper()}"
    content_hash = hashlib.sha256(f"content:{suffix}".encode()).hexdigest()
    content = {"unit_id": unit_id, "version": "1.0.0", "content_hash": content_hash}
    now = datetime.now(UTC) - timedelta(seconds=1)
    source = "#include <yaya/skill.hpp>\nint main() { return 0; }\n"
    starter: dict[str, Any] = {
        "skill_id": f"skill_workspace_{suffix}",
        "display_name": "starter workspace skill",
        "source_bundle": {
            "language": "CPP20",
            "entrypoint": "src/main.cpp",
            "files": [
                {
                    "path": "src/main.cpp",
                    "content": source,
                    "content_sha256": hashlib.sha256(source.encode()).hexdigest(),
                }
            ],
        },
        "compiler_profile": "YAYA_CPP20_SAFE_V1",
        "test_suite_version": "workspace-suite-v1",
    }
    state_hash = hashlib.sha256(f"world:{suffix}".encode()).hexdigest()
    world_revision = 3
    last_event_sequence = 8
    sessions = create_session_factory(database_url)
    commands = PostgresCommandStore(sessions)
    jobs = PostgresWorkflowJobStore(sessions)
    await _seed_authority(
        sessions,
        tenant_id=tenant_id,
        actor_id=actor_id,
        authority_id=authority_id,
        world_id=world_id,
        learner_id=learner_id,
        agent_profile_id=agent_profile_id,
        build_policy_id=build_policy_id,
        content=content,
        starter=starter,
        state_hash=state_hash,
        world_revision=world_revision,
        last_event_sequence=last_event_sequence,
        learner_audience=learner_audience,
        now=now,
    )
    context = OperationContext(
        request_id=f"req_workspace_{suffix}",
        correlation_id=f"corr_workspace_{suffix}",
        trace_id=f"trace_workspace_{suffix}",
        requested_at=requested_at or now,
        actor=ActorRef(
            tenant_id,
            actor_id,
            ActorType.STUDENT,
            ("game:player",),
        ),
        content_ref=ContentRef(**content),
        schema_version="1.0.0",
        command_id=f"cmd_transport_workspace_{suffix}",
        causation_id=None,
    )
    request = {
        "world_id": world_id,
        "learner_id": learner_id,
        "agent_profile_id": agent_profile_id,
        "channel": "GAME",
        "locale": "zh-CN",
        "content": content,
        "expected_world_revision": world_revision,
    }
    idempotency_key = f"idem_workspace_{suffix}"
    outcome = await AgentSessions(PostgresAgentSessionStore(sessions, commands, jobs)).accept(
        _json_bytes(request),
        idempotency_key,
        context,
    )
    assert isinstance(outcome, Success)
    resource, receipt = outcome.value
    assert receipt.created is True
    claim = await jobs.claim_next(
        tenant_id=tenant_id,
        worker_id=f"worker_workspace_{suffix}",
        lease_seconds=60,
        operation="CREATE_AGENT_SESSION",
    )
    assert claim is not None
    assert claim.command_id == receipt.command.command_id
    assert claim.subject_id == resource["session_id"]
    return _SessionFixture(
        sessions=sessions,
        commands=commands,
        jobs=jobs,
        handler=ControlWorkflowHandler(sessions, commands, jobs, lease_seconds=60),
        claim=claim,
        context=context,
        tenant_id=tenant_id,
        actor_id=actor_id,
        authority_id=authority_id,
        session_id=resource["session_id"],
        command_id=receipt.command.command_id,
        world_id=world_id,
        content=content,
        starter=starter,
        world_revision=world_revision,
        last_event_sequence=last_event_sequence,
        state_hash=state_hash,
        request=request,
        idempotency_key=idempotency_key,
    )


async def _seed_authority(
    sessions: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    actor_id: str,
    authority_id: str,
    world_id: str,
    learner_id: str,
    agent_profile_id: str,
    build_policy_id: str,
    content: dict[str, str],
    starter: dict[str, Any],
    state_hash: str,
    world_revision: int,
    last_event_sequence: int,
    learner_audience: bool,
    now: datetime,
) -> None:
    learner = {"learner_id": learner_id, "locale": "zh-CN", "revision": 0}
    profile = {
        "agent_profile_id": agent_profile_id,
        "provider": "fake-provider",
        "model_version": "fake-model-v1",
        "prompt_version": "prompt-workspace-v1",
    }
    policy = {
        "schema_version": "1.0.0",
        "compiler_profile": starter["compiler_profile"],
        "compiler_version": "gcc-14.2.0",
        "compiler_image": "ghcr.io/yaya/student-cpp@sha256:" + "b" * 64,
        "test_suite_version": starter["test_suite_version"],
        "compile_flags": [],
        "public_tests": [],
        "hidden_tests": [],
        "limits": {},
    }
    task = {
        "task_id": f"task_{world_id}",
        "allowed_capabilities": ["WORLD_READ", "WATER"],
        "starter_skill": starter,
    }
    async with sessions() as session, session.begin():
        session.add_all(
            [
                ProductContentUnitRow(
                    tenant_id=tenant_id,
                    unit_id=content["unit_id"],
                    version=content["version"],
                    content_hash=content["content_hash"],
                    audiences=["LEARNER"] if learner_audience else ["TEACHER_PREVIEW"],
                    published_at=now,
                    content_json={"content_ref": content, "task": task},
                ),
                WorldSnapshotRow(
                    tenant_id=tenant_id,
                    world_id=world_id,
                    actor_id=actor_id,
                    content_hash=content["content_hash"],
                    revision=world_revision,
                    last_event_sequence=last_event_sequence,
                    state_hash=state_hash,
                    generated_at=now,
                    snapshot_json={
                        "request_context": {
                            "request_id": f"req_world_{world_id}",
                            "correlation_id": f"corr_world_{world_id}",
                            "trace_id": f"trace_world_{world_id}",
                            "requested_at": now.isoformat(),
                            "actor": {
                                "tenant_id": tenant_id,
                                "actor_id": actor_id,
                                "actor_type": "student",
                                "roles": ["game:player"],
                            },
                            "content_ref": content,
                            "schema_version": "1.0.0",
                        },
                        "world_id": world_id,
                        "revision": world_revision,
                        "last_event_sequence": last_event_sequence,
                        "state_hash": state_hash,
                        "generated_at": now.isoformat(),
                        "world_rules_version": "rules-workspace-v1",
                        "state": {},
                    },
                ),
                LearnerProfileRow(
                    tenant_id=tenant_id,
                    learner_id=learner_id,
                    actor_id=actor_id,
                    content_hash=content["content_hash"],
                    profile_sha256=canonical_json_sha256(learner),
                    profile_json=learner,
                    created_at=now,
                    updated_at=now,
                ),
                AgentProfileRow(
                    tenant_id=tenant_id,
                    agent_profile_id=agent_profile_id,
                    actor_id=actor_id,
                    content_hash=content["content_hash"],
                    profile_sha256=canonical_json_sha256(profile),
                    profile_json=profile,
                    created_at=now,
                ),
                BuildPolicyRow(
                    tenant_id=tenant_id,
                    build_policy_id=build_policy_id,
                    actor_id=actor_id,
                    content_hash=content["content_hash"],
                    compiler_profile=starter["compiler_profile"],
                    compiler_version="gcc-14.2.0",
                    sandbox_image_digest="sha256:" + "b" * 64,
                    test_suite_version=starter["test_suite_version"],
                    allowed_capabilities=["WORLD_READ", "WATER"],
                    max_source_files=32,
                    max_source_bytes=1_048_576,
                    policy_json=policy,
                    policy_sha256=canonical_json_sha256(policy),
                    active=True,
                    created_at=now,
                ),
            ]
        )
        await session.flush()
        session.add(
            LaunchAuthorityRow(
                tenant_id=tenant_id,
                authority_id=authority_id,
                actor_id=actor_id,
                content_unit_id=content["unit_id"],
                content_version=content["version"],
                content_hash=content["content_hash"],
                world_id=world_id,
                learner_id=learner_id,
                agent_profile_id=agent_profile_id,
                build_policy_id=build_policy_id,
                channel="GAME",
                teaching_spec_version="teaching-workspace-v1",
                authority_sha256=hashlib.sha256(authority_id.encode()).hexdigest(),
                active=True,
                created_at=now,
            )
        )


async def _lifecycle_state(fixture: _SessionFixture) -> dict[str, Any]:
    async with fixture.sessions() as session:
        resource = await session.scalar(
            select(AgentSessionRow).where(
                AgentSessionRow.tenant_id == fixture.tenant_id,
                AgentSessionRow.session_id == fixture.session_id,
            )
        )
        command = await session.scalar(
            select(CommandRow).where(CommandRow.command_id == fixture.command_id)
        )
        job = await session.scalar(
            select(WorkflowJobRow).where(WorkflowJobRow.job_id == fixture.claim.job_id)
        )

        async def count(model: Any) -> int:
            clauses = [model.tenant_id == fixture.tenant_id]
            if hasattr(model, "session_id"):
                clauses.append(model.session_id == fixture.session_id)
            value = await session.scalar(select(func.count()).select_from(model).where(*clauses))
            return int(value or 0)

        receipts = await session.scalar(
            select(func.count())
            .select_from(JobStepReceiptRow)
            .where(JobStepReceiptRow.job_id == fixture.claim.job_id)
        )
        bindings = await count(CurrentSessionBindingRow)
        drafts = await count(ProductDraftRow)
        workspaces = await count(ProductWorkspaceRow)
    assert resource is not None and command is not None and job is not None
    return {
        "session_status": resource.status,
        "command_status": command.status,
        "command_terminal": command.terminal,
        "job_status": job.status,
        "bindings": bindings,
        "drafts": drafts,
        "workspaces": workspaces,
        "receipts": int(receipts or 0),
    }


async def _product_resources(
    fixture: _SessionFixture,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    async with fixture.sessions() as session:
        draft = await session.scalar(
            select(ProductDraftRow).where(
                ProductDraftRow.tenant_id == fixture.tenant_id,
                ProductDraftRow.session_id == fixture.session_id,
            )
        )
        workspace = await session.scalar(
            select(ProductWorkspaceRow).where(
                ProductWorkspaceRow.tenant_id == fixture.tenant_id,
                ProductWorkspaceRow.session_id == fixture.session_id,
            )
        )
        resource = await session.scalar(
            select(AgentSessionRow).where(
                AgentSessionRow.tenant_id == fixture.tenant_id,
                AgentSessionRow.session_id == fixture.session_id,
            )
        )
    assert draft is not None and workspace is not None and resource is not None
    return dict(draft.draft_json), dict(workspace.workspace_json), dict(resource.session_json)


async def _assert_session_product_causal_timeline(fixture: _SessionFixture) -> None:
    """The accepted request is the floor for every starter projection write."""

    async with fixture.sessions() as session:
        resource = await session.scalar(
            select(AgentSessionRow).where(
                AgentSessionRow.tenant_id == fixture.tenant_id,
                AgentSessionRow.session_id == fixture.session_id,
            )
        )
        command = await session.scalar(
            select(CommandRow).where(
                CommandRow.tenant_id == fixture.tenant_id,
                CommandRow.command_id == fixture.command_id,
            )
        )
        job = await session.scalar(
            select(WorkflowJobRow).where(
                WorkflowJobRow.tenant_id == fixture.tenant_id,
                WorkflowJobRow.job_id == fixture.claim.job_id,
            )
        )
        binding = await session.scalar(
            select(CurrentSessionBindingRow).where(
                CurrentSessionBindingRow.tenant_id == fixture.tenant_id,
                CurrentSessionBindingRow.session_id == fixture.session_id,
            )
        )
        draft = await session.scalar(
            select(ProductDraftRow).where(
                ProductDraftRow.tenant_id == fixture.tenant_id,
                ProductDraftRow.session_id == fixture.session_id,
            )
        )
        revision = await session.scalar(
            select(ProductDraftRevisionRow).where(
                ProductDraftRevisionRow.tenant_id == fixture.tenant_id,
                ProductDraftRevisionRow.session_id == fixture.session_id,
                ProductDraftRevisionRow.revision == 1,
            )
        )
        workspace = await session.scalar(
            select(ProductWorkspaceRow).where(
                ProductWorkspaceRow.tenant_id == fixture.tenant_id,
                ProductWorkspaceRow.session_id == fixture.session_id,
            )
        )
        receipt = await session.scalar(
            select(JobStepReceiptRow).where(
                JobStepReceiptRow.tenant_id == fixture.tenant_id,
                JobStepReceiptRow.job_id == fixture.claim.job_id,
                JobStepReceiptRow.step_name == "SESSION_BOUND",
            )
        )

    assert resource is not None
    assert command is not None
    assert job is not None
    assert binding is not None
    assert draft is not None
    assert revision is not None
    assert workspace is not None
    assert receipt is not None

    requested_at = fixture.context.requested_at.astimezone(UTC)
    session_created_at = _resource_time(resource.session_json["created_at"])
    draft_created_at = _resource_time(draft.draft_json["created_at"])
    workspace_created_at = _resource_time(workspace.workspace_json["created_at"])
    workspace_updated_at = _resource_time(workspace.workspace_json["updated_at"])
    draft_requested_at = _resource_time(
        draft.draft_json["request_context"]["requested_at"]
    )

    assert requested_at == draft_requested_at
    assert requested_at <= command.accepted_at
    assert command.accepted_at == resource.created_at == resource.updated_at
    assert resource.created_at == session_created_at
    assert command.accepted_at <= job.created_at
    assert job.created_at <= binding.bound_at
    assert binding.bound_at == draft.created_at == draft.updated_at
    assert draft.created_at == revision.created_at == workspace.updated_at
    assert draft.created_at == draft_created_at
    assert workspace.updated_at == workspace_created_at == workspace_updated_at
    assert draft_requested_at <= draft_created_at
    assert draft.updated_at <= receipt.completed_at
    assert receipt.completed_at <= command.updated_at <= job.updated_at


def _resource_time(value: object) -> datetime:
    assert isinstance(value, str)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed.astimezone(UTC)


async def _draft_revision_rows(fixture: _SessionFixture) -> list[ProductDraftRevisionRow]:
    async with fixture.sessions() as session:
        return list(
            (
                await session.scalars(
                    select(ProductDraftRevisionRow)
                    .where(
                        ProductDraftRevisionRow.tenant_id == fixture.tenant_id,
                        ProductDraftRevisionRow.session_id == fixture.session_id,
                    )
                    .order_by(ProductDraftRevisionRow.revision)
                )
            ).all()
        )


def _draft_update(
    fixture: _SessionFixture,
    previous: dict[str, Any],
    *,
    display_name: str,
    source: str,
) -> dict[str, Any]:
    return {
        "session_id": fixture.session_id,
        "draft_id": previous["draft_id"],
        "skill_id": previous["skill_id"],
        "content_ref": fixture.content,
        "base_revision": previous["revision"],
        "base_draft_sha256": previous["draft_sha256"],
        "display_name": display_name,
        "source_bundle": {
            "language": "CPP20",
            "entrypoint": "src/main.cpp",
            "files": [
                {
                    "path": "src/main.cpp",
                    "content": source,
                    "content_sha256": hashlib.sha256(source.encode()).hexdigest(),
                }
            ],
        },
        "client_saved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def _next_context(context: OperationContext, label: str) -> OperationContext:
    suffix = uuid4().hex[:16]
    return replace(
        context,
        request_id=f"req_{label}_{suffix}",
        correlation_id=f"corr_{label}_{suffix}",
        trace_id=f"trace_{label}_{suffix}",
        requested_at=datetime.now(UTC),
        command_id=f"cmd_{label}_{suffix}",
    )


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _database_url() -> str:
    value = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if value is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required Product workspace PostgreSQL coverage"
        )
    return value


async def _dispose(sessions: async_sessionmaker[AsyncSession]) -> None:
    await sessions.kw["bind"].dispose()
