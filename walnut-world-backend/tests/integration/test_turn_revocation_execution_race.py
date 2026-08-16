"""A certification revoked after Turn acceptance cannot reach a side effect."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    LlmRequest,
    OperationContext,
    SandboxLimits,
    VersionSet,
    canonical_json_sha256,
)
from yaya_agent_runtime import LlmDispatchIdentity, LlmDispatchResource

from tests.integration.test_skill_activation_acceptance import (
    _Fixture,
    _seed_activation_authority,
)
from walnut_backend.adapters.postgres.agent_runtime import (
    AgentRuntimeAuthorityError,
    PostgresAgentRuntimeReads,
)
from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.models import (
    CommandRow,
    CurrentSessionBindingRow,
    EventRow,
    JobStepReceiptRow,
    LearnerProfileRow,
    LearnerProjectionJobRow,
    ProductContentUnitRow,
    ProductInteractionRow,
    ProductWorkspaceRow,
    RunRow,
    SkillCertificationRevocationRow,
    WorkflowJobRow,
    WorldSnapshotRow,
    command_record_from_data,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
)
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings
from walnut_backend.domain.world.rules import WorldRules
from walnut_backend.workers.control_worker import ControlWorkflowHandler
from walnut_backend.workers.turn_worker import TurnWorkflowHandler
from walnut_backend.workers.workflow_worker import WorkflowWorker

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_revocation_after_turn_acceptance_blocks_durable_execution_side_effects() -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required PostgreSQL revocation-race coverage"
        )
    asyncio.run(_exercise_revocation_race(database_url))


@pytest.mark.parametrize("tamper", ("binding_id", "bound_at"))
def test_binding_corruption_after_turn_acceptance_blocks_worker_side_effects(
    tamper: str,
) -> None:
    database_url = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if database_url is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required PostgreSQL binding-race coverage"
        )
    asyncio.run(_exercise_revocation_race(database_url, binding_tamper=tamper))


async def _exercise_revocation_race(
    database_url: str,
    *,
    binding_tamper: str | None = None,
) -> None:
    suffix = uuid4().hex[:16]
    fixture = await _seed_activation_authority(
        database_url,
        suffix,
        tenant_id=f"tenant_revocation_{suffix}",
    )
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    sessions = create_session_factory(database_url)
    jobs = PostgresWorkflowJobStore(sessions)
    commands = PostgresCommandStore(sessions)
    control = ControlWorkflowHandler(sessions, commands, jobs, lease_seconds=60)
    try:
        with TestClient(create_app(settings)) as client:
            activation = client.post(
                f"/v1/skill-versions/{fixture.skill_version_id}/activations",
                headers=_headers(fixture, suffix, "activation"),
                json={
                    "expected_registry_revision": 0,
                    "activation_scope": {
                        "world_id": fixture.world_id,
                        "agent_profile_id": fixture.agent_profile_id,
                    },
                    "reason": "revocation race regression",
                },
            )
            assert activation.status_code == 202, activation.text
            await _execute_control_job(
                jobs,
                control,
                fixture,
                operation="ACTIVATE_SKILL_VERSION",
                command_id=activation.json()["command_id"],
                suffix=suffix,
            )

            session_id, world_revision, last_event_sequence = await _current_session_state(
                sessions, fixture
            )

            turn_id = f"turn_revocation_{suffix}"
            accepted_turn = client.post(
                f"/v1/agent-sessions/{session_id}/turns",
                headers=_headers(fixture, suffix, "turn"),
                json={
                    "turn_id": turn_id,
                    "expected_world_revision": world_revision,
                    "input": {"type": "MESSAGE", "text": "move", "locale": "zh-CN"},
                    "skill_bindings": [
                        {
                            "skill_id": fixture.skill_id,
                            "skill_version_id": fixture.skill_version_id,
                            "artifact_sha256": fixture.artifact_sha256,
                            "certification_id": fixture.certification_id,
                        }
                    ],
                    "client_state": {
                        "last_event_sequence": last_event_sequence,
                        "client_turn_sequence": 1,
                    },
                },
            )
            assert accepted_turn.status_code == 202, accepted_turn.text
            turn_command_id = accepted_turn.json()["command_id"]

        if binding_tamper is None:
            await _append_revocation(database_url, fixture, suffix)
        else:
            await _tamper_current_binding(
                sessions,
                fixture,
                session_id,
                suffix,
                binding_tamper,
            )
            runtime_context = await _turn_context(sessions, turn_command_id)
            with pytest.raises(
                AgentRuntimeAuthorityError,
                match="current Session binding authority is corrupt",
            ):
                await PostgresAgentRuntimeReads(sessions).get_session(
                    session_id,
                    runtime_context,
                )
        baseline = await _side_effect_state(sessions, fixture, session_id, turn_command_id)
        assert baseline["runs"] == 0
        assert baseline["world_events"] == 0
        assert baseline["interactions"] == 0
        assert baseline["learner_projection_jobs"] == 0

        provider = _NoCallProvider()
        sandbox = _NoCallSandbox()
        versions = VersionSet(**fixture.expected_versions)
        handler = TurnWorkflowHandler(
            session_factory=sessions,
            commands=commands,
            jobs=jobs,
            provider=provider,
            sandbox=sandbox,
            limits=SandboxLimits(
                cpu_ms=1_000,
                wall_ms=1_000,
                memory_bytes=64 * 1024 * 1024,
                max_intents=4,
                max_output_bytes=4_096,
                max_processes=4,
            ),
            versions=versions,
            rules_by_version={
                versions.world_rules_version: WorldRules(
                    content_version="1.0.0",
                    max_actions=4,
                    min_x=0,
                    max_x=4,
                    min_y=0,
                    max_y=4,
                    harvest_growth_stage=2,
                    success_score=0,
                )
            },
            provider_name="fixture-provider",
            model_version="fixture-model-v7",
            prompt_version="fixture-prompt-v5",
            sandbox_image_digest="sha256:" + "b" * 64,
            lease_seconds=60,
        )

        worker = WorkflowWorker(
            session_factory=sessions,
            jobs=jobs,
            commands=commands,
            handlers=(handler,),
            worker_id=f"worker_execution_authority_{suffix}",
            lease_seconds=60,
            maximum_attempts=1,
        )
        assert await _run_workflow_worker_eventually(worker, fixture.tenant_id) is True

        after = await _side_effect_state(sessions, fixture, session_id, turn_command_id)
        assert provider.calls == 0
        assert sandbox.calls == 0
        assert after == baseline
        async with sessions() as session:
            sandbox_receipts = await session.scalar(
                select(func.count(JobStepReceiptRow.receipt_id))
                .join(WorkflowJobRow, WorkflowJobRow.job_id == JobStepReceiptRow.job_id)
                .where(
                    WorkflowJobRow.tenant_id == fixture.tenant_id,
                    WorkflowJobRow.command_id == turn_command_id,
                    JobStepReceiptRow.step_name == "SANDBOX_DISPATCHED",
                )
            )
            command_row = await session.scalar(
                select(CommandRow).where(CommandRow.command_id == turn_command_id)
            )
            job_row = await session.scalar(
                select(WorkflowJobRow).where(WorkflowJobRow.command_id == turn_command_id)
            )
        assert int(sandbox_receipts or 0) == 0
        assert command_row is not None
        assert job_row is not None
        command = command_record_from_data(command_row.record_json)
        assert command.status.value == "FAILED"
        assert command.terminal is True
        assert command.error is not None
        assert command.error.code == "INTERNAL_ERROR"
        assert job_row.status == "DEAD_LETTER"
        assert job_row.last_error_json is not None
        assert job_row.last_error_json["exception_type"] in {
            "AgentRuntimeAuthorityError",
            "WorkflowInvariantError",
        }
    finally:
        await sessions.kw["bind"].dispose()


async def _current_session_state(
    sessions: async_sessionmaker[AsyncSession], fixture: _Fixture
) -> tuple[str, int, int]:
    async with sessions() as session:
        binding = await session.scalar(
            select(CurrentSessionBindingRow).where(
                CurrentSessionBindingRow.tenant_id == fixture.tenant_id,
                CurrentSessionBindingRow.authority_id == fixture.authority_id,
                CurrentSessionBindingRow.actor_id == fixture.actor_id,
                CurrentSessionBindingRow.content_hash == fixture.content["content_hash"],
                CurrentSessionBindingRow.world_id == fixture.world_id,
                CurrentSessionBindingRow.agent_profile_id == fixture.agent_profile_id,
            )
        )
        world = await session.scalar(
            select(WorldSnapshotRow).where(
                WorldSnapshotRow.tenant_id == fixture.tenant_id,
                WorldSnapshotRow.actor_id == fixture.actor_id,
                WorldSnapshotRow.world_id == fixture.world_id,
                WorldSnapshotRow.content_hash == fixture.content["content_hash"],
            )
        )
    assert binding is not None
    assert world is not None
    return binding.session_id, world.revision, world.last_event_sequence


async def _execute_control_job(
    jobs: PostgresWorkflowJobStore,
    handler: ControlWorkflowHandler,
    fixture: _Fixture,
    *,
    operation: str,
    command_id: str,
    suffix: str,
) -> None:
    claim = await _claim_workflow_eventually(
        jobs,
        tenant_id=fixture.tenant_id,
        worker_id=f"worker_control_{suffix}_{operation.lower()}",
        lease_seconds=60,
        operation=operation,
    )
    assert claim is not None
    assert claim.command_id == command_id
    await handler.execute(claim)


async def _claim_workflow_eventually(
    jobs: PostgresWorkflowJobStore,
    *,
    tenant_id: str,
    worker_id: str,
    lease_seconds: int,
    operation: str,
) -> ClaimedWorkflowJob | None:
    # HTTP request time is a trusted causal floor. PostgreSQL can briefly trail
    # the Gateway host clock, so READY need not mean claimable in the same tick.
    deadline = monotonic() + 2.0
    while True:
        claim = await jobs.claim_next(
            tenant_id=tenant_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            operation=operation,
        )
        if claim is not None or monotonic() >= deadline:
            return claim
        await asyncio.sleep(0.01)


async def _run_workflow_worker_eventually(worker: WorkflowWorker, tenant_id: str) -> bool:
    deadline = monotonic() + 2.0
    while True:
        if await worker.run_once(tenant_id):
            return True
        if monotonic() >= deadline:
            return False
        await asyncio.sleep(0.01)


async def _install_runtime_task(database_url: str, fixture: _Fixture, suffix: str) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            content = await session.scalar(
                select(ProductContentUnitRow)
                .where(
                    ProductContentUnitRow.tenant_id == fixture.tenant_id,
                    ProductContentUnitRow.unit_id == fixture.content["unit_id"],
                    ProductContentUnitRow.version == fixture.content["version"],
                    ProductContentUnitRow.content_hash == fixture.content["content_hash"],
                )
                .with_for_update()
            )
            assert content is not None
            value = copy.deepcopy(content.content_json)
            source = "#include <yaya/skill.hpp>\nint main() { return 0; }\n"
            value["task"] = {
                "task_id": f"task_revocation_{suffix}",
                "name": "Revocation race",
                "goal": "Reject a revoked exact Skill before execution.",
                "story": {"opening": "A certification changed after acceptance."},
                "knowledge_points": ["world_navigation"],
                "hint_policy": {"max_level": 4},
                "allowed_capabilities": ["WORLD_READ"],
                "starter_skill": {
                    "skill_id": f"starter_revocation_{suffix}",
                    "display_name": "revocation race starter",
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
                    "test_suite_version": "test-suite-activation-v2",
                },
            }
            content.content_json = value
    finally:
        await sessions.kw["bind"].dispose()


async def _append_revocation(database_url: str, fixture: _Fixture, suffix: str) -> None:
    sessions = create_session_factory(database_url)
    now = datetime.now(UTC)
    revocation = {
        "schema_version": "1.0.0",
        "revocation_id": f"revocation_execution_{suffix}",
        "certification_id": fixture.certification_id,
        "reason_code": "SECURITY_POLICY_CHANGED",
        "revoked_at": now.isoformat(),
    }
    try:
        async with sessions() as session, session.begin():
            session.add(
                SkillCertificationRevocationRow(
                    revocation_id=revocation["revocation_id"],
                    tenant_id=fixture.tenant_id,
                    certification_id=fixture.certification_id,
                    revocation_sha256=canonical_json_sha256(revocation),
                    reason_code=revocation["reason_code"],
                    revocation_json=revocation,
                    revoked_at=now,
                )
            )
    finally:
        await sessions.kw["bind"].dispose()


async def _tamper_current_binding(
    sessions: async_sessionmaker[AsyncSession],
    fixture: _Fixture,
    session_id: str,
    suffix: str,
    tamper: str,
) -> None:
    async with sessions() as session, session.begin():
        binding = await session.scalar(
            select(CurrentSessionBindingRow)
            .where(
                CurrentSessionBindingRow.tenant_id == fixture.tenant_id,
                CurrentSessionBindingRow.session_id == session_id,
            )
            .with_for_update()
        )
        assert binding is not None
        if tamper == "binding_id":
            binding.binding_id = f"binding_corrupt_{suffix}"
        elif tamper == "bound_at":
            database_now = await session.scalar(select(func.clock_timestamp()))
            assert isinstance(database_now, datetime)
            assert database_now.tzinfo is not None
            binding.bound_at = database_now + timedelta(days=1)
        else:  # pragma: no cover - parametrization is a closed set
            raise AssertionError(tamper)


async def _turn_context(
    sessions: async_sessionmaker[AsyncSession],
    command_id: str,
) -> OperationContext:
    async with sessions() as session:
        row = await session.scalar(select(CommandRow).where(CommandRow.command_id == command_id))
    assert row is not None
    command = command_record_from_data(row.record_json)
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


async def _side_effect_state(
    sessions: async_sessionmaker[AsyncSession],
    fixture: _Fixture,
    session_id: str,
    command_id: str,
) -> dict[str, Any]:
    async with sessions() as session:
        world = await session.scalar(
            select(WorldSnapshotRow).where(
                WorldSnapshotRow.tenant_id == fixture.tenant_id,
                WorldSnapshotRow.world_id == fixture.world_id,
            )
        )
        learner = await session.scalar(
            select(LearnerProfileRow).where(
                LearnerProfileRow.tenant_id == fixture.tenant_id,
                LearnerProfileRow.learner_id == fixture.actor_id,
            )
        )
        workspace = await session.scalar(
            select(ProductWorkspaceRow).where(
                ProductWorkspaceRow.tenant_id == fixture.tenant_id,
                ProductWorkspaceRow.session_id == session_id,
            )
        )
        runs = await session.scalar(
            select(func.count(RunRow.run_id)).where(
                RunRow.tenant_id == fixture.tenant_id,
                RunRow.command_id == command_id,
            )
        )
        world_events = await session.scalar(
            select(func.count(EventRow.event_id)).where(
                EventRow.tenant_id == fixture.tenant_id,
                EventRow.stream_id == f"world:{fixture.world_id}",
            )
        )
        interactions = await session.scalar(
            select(func.count(ProductInteractionRow.interaction_id)).where(
                ProductInteractionRow.tenant_id == fixture.tenant_id,
                ProductInteractionRow.session_id == session_id,
            )
        )
        learner_jobs = await session.scalar(
            select(func.count(LearnerProjectionJobRow.job_id)).where(
                LearnerProjectionJobRow.tenant_id == fixture.tenant_id,
                LearnerProjectionJobRow.command_id == command_id,
            )
        )
    assert world is not None
    assert learner is not None
    assert workspace is not None
    return {
        "world_revision": world.revision,
        "world_event_sequence": world.last_event_sequence,
        "world_state_hash": world.state_hash,
        "world_snapshot": copy.deepcopy(world.snapshot_json),
        "runs": int(runs or 0),
        "world_events": int(world_events or 0),
        "interactions": int(interactions or 0),
        "learner_projection_jobs": int(learner_jobs or 0),
        "learner_revision": learner.profile_json.get("revision"),
        "learner_sha256": learner.profile_sha256,
        "learner_profile": copy.deepcopy(learner.profile_json),
        "workspace_revision": workspace.workspace_revision,
        "workspace": copy.deepcopy(workspace.workspace_json),
    }


def _headers(fixture: _Fixture, suffix: str, operation: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {fixture.tenant_id}:{fixture.actor_id}",
        "X-Request-Id": f"req_revocation_{operation}_{suffix}",
        "X-Trace-Id": f"trace_revocation_{operation}_{suffix}",
        "X-Correlation-Id": f"corr_revocation_{operation}_{suffix}",
        "X-Schema-Version": "1.0.0",
        "Idempotency-Key": f"idem_revocation_{operation}_{suffix}",
    }


class _NoCallProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def validate_capabilities(self) -> Any:
        self.calls += 1
        raise AssertionError("revoked Turn must not inspect Provider capabilities")

    async def dispatch(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource:
        del identity, request, context
        self.calls += 1
        raise AssertionError("revoked Turn must not dispatch Provider")

    async def reconcile(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource:
        del identity, request, context
        self.calls += 1
        raise AssertionError("revoked Turn must not reconcile Provider")


class _NoCallSandbox:
    def __init__(self) -> None:
        self.calls = 0

    async def compile_and_test(self, request: Any, context: OperationContext) -> Any:
        del request, context
        self.calls += 1
        raise AssertionError("revoked Turn must not compile in Sandbox")

    async def run(self, request: Any, context: OperationContext) -> Any:
        del request, context
        self.calls += 1
        raise AssertionError("revoked Turn must not dispatch Sandbox")

    async def cancel(
        self,
        run_id: str,
        reason_code: str,
        context: OperationContext,
    ) -> Any:
        del run_id, reason_code, context
        self.calls += 1
        raise AssertionError("revoked Turn must not cancel Sandbox")

    async def reconcile(self, request: Any, context: OperationContext) -> Any:
        del request, context
        self.calls += 1
        raise AssertionError("revoked Turn must not reconcile Sandbox")
