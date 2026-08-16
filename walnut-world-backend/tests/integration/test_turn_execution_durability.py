"""PostgreSQL recovery gates for the fenced Agent Turn execution closure."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_build import (
    CPP20_SAFE_V1_FLAGS,
    DigestPinnedDockerCppBuilder,
    DockerBuildResult,
    canonical_source_bundle_sha256,
)
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    CommandRecord,
    CommandStatus,
    ContentRef,
    ContractError,
    DeliveryPayload,
    ErrorCategory,
    Failure,
    FeishuReportDraftBody,
    FrozenJsonObject,
    LlmMessage,
    LlmReply,
    LlmRequest,
    MoveIntent,
    OperationContext,
    OutboxMessage,
    RequestContext,
    SandboxLimits,
    SandboxRunResult,
    SandboxUsage,
    SkillRef,
    Success,
    UncommittedEvent,
    VersionSet,
    WorldAtomicCommit,
    WorldCommand,
    WorldPosition,
    WorldSnapshot,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    LEARNER_PROJECTION_POLICY_VERSION,
    REVIEW_POLICY_VERSION,
    AgentDecision,
    AgentToolExecutionError,
    DecisionDraft,
    GameEvent,
    LlmDispatchIdentity,
    LlmDispatchResource,
    LlmRelayCapabilities,
    RecoverableLlmUnavailable,
    RunOutcomeInvariantError,
    SkillInvocationRequest,
    TaskSnapshot,
    TeachingDirective,
    TeachingPhase,
    derive_run_outcome_event,
    operation_context_sha256,
    provider_dispatch_id,
    side_effect_execution_id,
    skill_invocation_request_sha256,
)

from walnut_backend.adapters.postgres import run_outcomes as run_outcome_module
from walnut_backend.adapters.postgres import skill_invocation as invocation_module
from walnut_backend.adapters.postgres.agent_runtime import (
    AgentRuntimeAuthorityError,
    PostgresAgentRuntimeReads,
    _agent_trace_audit_id,
)
from walnut_backend.adapters.postgres.agent_sessions import PostgresAgentSessionStore
from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.durable_llm import (
    DurableLlmDispatchUnknown,
    PostgresDurableLlm,
)
from walnut_backend.adapters.postgres.learner_projection_jobs import (
    ClaimedLearnerProjectionJob,
    LearnerProjectionFenceLost,
    LearnerProjectionInvariantError,
    PostgresLearnerProjectionJobStore,
)
from walnut_backend.adapters.postgres.models import (
    AgentProfileRow,
    AgentSessionRow,
    AgentTurnRow,
    AuditRow,
    BuildPolicyRow,
    CommandRow,
    CurrentSessionBindingRow,
    EventRow,
    EvidenceRow,
    IdempotencyReceiptRow,
    JobStepReceiptRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    LearnerProjectionJobRow,
    ProductContentUnitRow,
    ProductDraftRow,
    ProductInteractionRow,
    ProductWorkspaceRow,
    RegistryEntryRow,
    RegistryHeadRow,
    RunRow,
    SkillActivationProvenanceRow,
    SkillActivationRow,
    SkillArtifactRow,
    SkillBuildRow,
    SkillCertificationRow,
    SkillRunProvenanceRow,
    WorkflowJobRow,
    WorldSnapshotRow,
    command_record_data,
    command_record_from_data,
    json_value,
    request_context_data,
    world_snapshot_data,
    world_snapshot_from_data,
)
from walnut_backend.adapters.postgres.outbox import PostgresOutbox
from walnut_backend.adapters.postgres.product_interactions import (
    PostgresProductInteractionStore,
)
from walnut_backend.adapters.postgres.product_workspaces import (
    initial_workspace_resource,
    refresh_workspace_in_session,
)
from walnut_backend.adapters.postgres.run_outcomes import (
    PostgresRunOutcomeAuthority,
    run_authority_sha256,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.session_binding_authority import (
    current_session_binding_id,
)
from walnut_backend.adapters.postgres.skill_activations import PostgresSkillActivationStore
from walnut_backend.adapters.postgres.skill_builds import PostgresSkillBuildStore
from walnut_backend.adapters.postgres.skill_invocation import PostgresFencedSkillInvocation
from walnut_backend.adapters.postgres.workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
    WorkflowBoundaryError,
    WorkflowFenceLost,
    WorkflowInvariantError,
    workflow_json_sha256,
    workflow_receipt_sha256,
)
from walnut_backend.adapters.postgres.world import (
    PostgresWorldUnitOfWork,
    world_commit_identifier,
)
from walnut_backend.api.app import create_app
from walnut_backend.application.game.agent_sessions import AgentSessions
from walnut_backend.application.game.skill_activations import SkillActivations
from walnut_backend.application.game.skill_builds import SkillBuildCommands
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings
from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules
from walnut_backend.workers.build_worker import BuildWorkflowHandler
from walnut_backend.workers.control_worker import ControlWorkflowHandler
from walnut_backend.workers.learner_projector import PostgresLearnerProjector
from walnut_backend.workers.learner_worker import LearnerProjectionWorker
from walnut_backend.workers.turn_projection import (
    _canonical_teaching_directive,
    finish_turn_projection,
)
from walnut_backend.workers.turn_worker import _TurnAuthority
from walnut_backend.workers.workflow_worker import WorkflowWorker

BACKEND_ROOT = Path(__file__).resolve().parents[2]
_TEST_WORKFLOW_LEASE_SECONDS = 60 * 60


def test_provider_receipt_replay_and_dispatch_ambiguity() -> None:
    asyncio.run(_exercise_provider_receipts(_database_url()))


def test_root_and_final_provider_ordinals_survive_process_restart() -> None:
    asyncio.run(_exercise_root_final_provider_ordinals(_database_url()))


def test_provider_wrong_context_and_cross_context_replay_fail_closed() -> None:
    asyncio.run(_exercise_provider_context_binding(_database_url()))


def test_stale_fence_cannot_dispatch_provider_or_write_receipt() -> None:
    asyncio.run(_exercise_stale_fence(_database_url()))


def test_sandbox_response_loss_is_not_dispatched_twice() -> None:
    asyncio.run(_exercise_sandbox_response_loss(_database_url()))


def test_host_pause_after_sandbox_result_reclaims_fence_and_reconciles_once() -> None:
    asyncio.run(_exercise_midflight_host_pause_recovery(_database_url()))


def test_sandbox_pending_reconciliation_is_retryable_without_redispatch() -> None:
    asyncio.run(_exercise_sandbox_pending_reconciliation(_database_url()))


def test_sandbox_unavailable_before_create_recovers_via_reconcile_once() -> None:
    asyncio.run(_exercise_sandbox_unavailable_recovery(_database_url()))


def test_clock_skewed_failed_sandbox_result_preserves_causal_run_floor() -> None:
    asyncio.run(_exercise_clock_skewed_failed_sandbox_result(_database_url()))


def test_run_outcome_runtime_invariant_is_translated_for_worker() -> None:
    asyncio.run(_exercise_run_outcome_runtime_invariant(_database_url()))


def test_workflow_invariant_dead_letters_after_one_failure_receipt() -> None:
    asyncio.run(_exercise_workflow_invariant_dead_letter(_database_url()))


def test_in_progress_turn_command_get_closes_exact_run_link() -> None:
    database_url = _database_url()
    running = asyncio.run(_seed_in_progress_command(database_url, applying=False))
    applying = asyncio.run(_seed_in_progress_command(database_url, applying=True))
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        for fixture, status, stage in (
            (running, "RUNNING_SANDBOX", "SANDBOX"),
            (applying, "APPLYING_WORLD", "WORLD_COMMIT"),
        ):
            response = client.get(
                f"/v1/commands/{fixture.command_id}", headers=_command_headers(fixture)
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == status
            assert response.json()["stage"] == stage
            assert response.json()["links"]["run"] == f"/v1/runs/{fixture.run_id}"

        run_response = client.get(f"/v1/runs/{applying.run_id}", headers=_command_headers(applying))
        assert run_response.status_code == 200, run_response.text

        asyncio.run(
            _replace_command_run_link(
                database_url,
                running.command_id,
                "/v1/runs/run_tampered_valid_0001",
            )
        )
        replaced = client.get(
            f"/v1/commands/{running.command_id}", headers=_command_headers(running)
        )
        assert replaced.status_code == 500, replaced.text
        assert replaced.json()["error"]["code"] == "INVARIANT_VIOLATION"
        asyncio.run(
            _replace_command_run_link(
                database_url, running.command_id, f"/v1/runs/{running.run_id}"
            )
        )

        asyncio.run(_replace_command_run_link(database_url, applying.command_id, None))
        removed = client.get(
            f"/v1/commands/{applying.command_id}", headers=_command_headers(applying)
        )
        assert removed.status_code == 500, removed.text
        assert removed.json()["error"]["code"] == "INVARIANT_VIOLATION"
        asyncio.run(
            _replace_command_run_link(
                database_url, applying.command_id, f"/v1/runs/{applying.run_id}"
            )
        )


def test_in_progress_turn_recovery_fences_and_cotamper_fail_closed() -> None:
    database_url = _database_url()
    retrying = asyncio.run(
        _seed_in_progress_command(database_url, applying=False, recovery_status="RETRY_WAIT")
    )
    claimed = asyncio.run(
        _seed_in_progress_command(database_url, applying=False, recovery_status="CLAIMED")
    )
    applying_claimed = asyncio.run(
        _seed_in_progress_command(database_url, applying=True, recovery_status="CLAIMED")
    )
    assert retrying.job_status == "RETRY_WAIT"
    assert retrying.dispatch_fencing_token == retrying.job_fencing_token
    for fixture in (claimed, applying_claimed):
        assert fixture.job_status == "CLAIMED"
        assert fixture.dispatch_fencing_token < fixture.job_fencing_token

    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        for fixture in (retrying, claimed, applying_claimed):
            response = client.get(
                f"/v1/commands/{fixture.command_id}", headers=_command_headers(fixture)
            )
            assert response.status_code == 200, response.text

        asyncio.run(_cotamper_dispatch_world(database_url, retrying))
        world_tampered = client.get(
            f"/v1/commands/{retrying.command_id}", headers=_command_headers(retrying)
        )
        assert world_tampered.status_code == 500, world_tampered.text

        asyncio.run(
            _replace_completion_arguments(
                database_url, applying_claimed.command_id, {"move": "west"}
            )
        )
        arguments_tampered = client.get(
            f"/v1/commands/{applying_claimed.command_id}",
            headers=_command_headers(applying_claimed),
        )
        assert arguments_tampered.status_code == 500, arguments_tampered.text
        asyncio.run(
            _replace_completion_arguments(
                database_url, applying_claimed.command_id, {"move": "east"}
            )
        )

        asyncio.run(
            _replace_completion_fence(
                database_url,
                applying_claimed.command_id,
                applying_claimed.job_fencing_token + 1,
            )
        )
        fence_tampered = client.get(
            f"/v1/commands/{applying_claimed.command_id}",
            headers=_command_headers(applying_claimed),
        )
        assert fence_tampered.status_code == 500, fence_tampered.text


def test_run_row_timestamp_duplicates_wire_for_success_and_failure() -> None:
    database_url = _database_url()
    successful = asyncio.run(_seed_in_progress_command(database_url, applying=True))
    failed = asyncio.run(_seed_failed_run(database_url))
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        for fixture in (successful, failed):
            response = client.get(f"/v1/runs/{fixture.run_id}", headers=_command_headers(fixture))
            assert response.status_code == 200, response.text
            asyncio.run(_assert_run_timestamp_duplicate(database_url, fixture.run_id))


def test_world_run_evidence_and_completion_receipt_commit_atomically() -> None:
    asyncio.run(_exercise_successful_atomic_publish(_database_url()))


def test_world_publish_rolls_back_when_evidence_insert_fails() -> None:
    asyncio.run(_exercise_atomic_publish_rollback(_database_url()))


def test_world_commit_failure_never_commits_partial_world_state() -> None:
    asyncio.run(_exercise_world_failure_rollback(_database_url()))


def test_terminal_projection_rolls_back_as_one_fenced_transaction() -> None:
    asyncio.run(_exercise_terminal_projection_transaction(_database_url()))


def test_learner_handoff_takeover_fences_old_process_and_projects_once() -> None:
    asyncio.run(_exercise_learner_takeover(_database_url()))


def test_learner_commit_ack_loss_reconciles_full_terminal_closure() -> None:
    asyncio.run(_exercise_learner_commit_ack_loss(_database_url()))


def test_corrupt_learner_objective_dead_letters_without_projection() -> None:
    asyncio.run(_exercise_corrupt_learner_objective(_database_url()))


def test_learner_terminal_validator_rejects_coordinated_evidence_tamper() -> None:
    asyncio.run(_exercise_learner_terminal_tamper(_database_url()))


def test_learner_projection_rejects_conflicting_source_evidence_metadata() -> None:
    asyncio.run(_exercise_learner_source_metadata_conflict(_database_url()))


def test_learner_projection_catalog_is_global_stable_deduped_and_bounded() -> None:
    asyncio.run(_exercise_learner_catalog_compaction(_database_url()))


def test_control_activation_clock_regression_preserves_causal_floor() -> None:
    asyncio.run(_exercise_control_activation_clock_regression(_database_url()))


def test_same_session_projects_teaching_teaching_bug_book_gap_free() -> None:
    asyncio.run(_exercise_four_role_projection_chain(_database_url()))


def test_provider_receipt_cotamper_has_zero_learner_side_effects() -> None:
    asyncio.run(_exercise_provider_receipt_cotamper(_database_url()))


def test_final_provider_failure_then_success_is_gap_free() -> None:
    asyncio.run(_exercise_final_provider_failure_then_success(_database_url()))


def test_terminal_command_and_run_share_exact_evidence_refs_and_tamper_fails_closed() -> None:
    database_url = _database_url()
    fixture = asyncio.run(_seed_terminal_command_with_legacy_null_uri(database_url))
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get(
            f"/v1/commands/{fixture.command_id}", headers=_command_headers(fixture)
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "APPLIED"
        assert payload["terminal"] is True
        assert payload["evidence_refs"]
        assert all("uri" not in reference for reference in payload["evidence_refs"])
        assert all(
            isinstance(reference.get("sha256"), str) for reference in payload["evidence_refs"]
        )
        run_response = client.get(f"/v1/runs/{fixture.run_id}", headers=_command_headers(fixture))
        assert run_response.status_code == 200, run_response.text
        assert payload["evidence_refs"] == run_response.json()["evidence_refs"]
        assert all(reference["created_at"].endswith("Z") for reference in payload["evidence_refs"])

        asyncio.run(_replace_terminal_command_evidence_uri(database_url, fixture.command_id, 17))
        tampered = client.get(
            f"/v1/commands/{fixture.command_id}", headers=_command_headers(fixture)
        )
        assert tampered.status_code == 500, tampered.text
        assert tampered.json()["error"]["code"] == "INVARIANT_VIOLATION"


@pytest.mark.parametrize(
    ("successful", "handoff_status", "terminal_status"),
    (
        (False, CommandStatus.RUNNING_SANDBOX, CommandStatus.REJECTED),
        (True, CommandStatus.APPLYING_WORLD, CommandStatus.APPLIED),
    ),
)
def test_waiting_projection_command_read_closes_exact_learner_handoff(
    successful: bool,
    handoff_status: CommandStatus,
    terminal_status: CommandStatus,
) -> None:
    database_url = _database_url()
    reference = asyncio.run(_seed_waiting_projection_command(database_url, successful=successful))
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    handoff = asyncio.run(_read_waiting_projection_command(database_url, reference))
    assert isinstance(handoff, Success), handoff
    assert handoff.value.status is handoff_status
    with TestClient(create_app(settings)) as client:
        response = client.get(
            f"/v1/commands/{reference.command_id}",
            headers=_command_headers(reference),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == handoff_status.value

    asyncio.run(_complete_waiting_projection(database_url, reference))

    terminal = asyncio.run(_read_waiting_projection_command(database_url, reference))
    assert isinstance(terminal, Success), terminal
    assert terminal.value.status is terminal_status
    with TestClient(create_app(settings)) as client:
        response = client.get(
            f"/v1/commands/{reference.command_id}",
            headers=_command_headers(reference),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == terminal_status.value
        interaction_response = client.get(
            (
                f"/product-experience/v1/sessions/{reference.session_id}/"
                "agent-interactions?after_sequence=0&limit=50"
            ),
            headers=_command_headers(reference),
        )
        assert interaction_response.status_code == 200, interaction_response.text
        page = interaction_response.json()
        assert page["high_watermark_sequence"] == 1
        assert page["next_after_sequence"] == 1
        assert len(page["interactions"]) == 1
        interaction = page["interactions"][0]
        assert interaction["turn_id"] == reference.turn_id
        assert interaction["feedback"]["command_id"] == reference.command_id
        assert interaction["feedback"]["run_id"] == reference.run_id
        assert (
            interaction["feedback_event"]["occurred_at"] == interaction["feedback"]["completed_at"]
        )
        assert interaction["feedback_event"]["occurred_at"].endswith("Z")
        run_response = client.get(
            f"/v1/runs/{reference.run_id}",
            headers=_command_headers(reference),
        )
        assert run_response.status_code == 200, run_response.text
        assert run_response.json()["run_id"] == reference.run_id

    timestamps = asyncio.run(_projection_timestamp_spellings(database_url, reference))
    assert timestamps["public"].endswith("Z")
    assert timestamps["durable"].endswith("+00:00")
    assert datetime.fromisoformat(timestamps["public"].replace("Z", "+00:00")) == (
        datetime.fromisoformat(timestamps["durable"])
    )
    assert timestamps["receipt_interaction_id"] == timestamps["interaction_id"]
    assert timestamps["receipt_sequence"] == 1


@pytest.mark.parametrize("successful", (False, True))
def test_waiting_projection_handoff_preserves_host_clock_causal_floor(
    successful: bool,
) -> None:
    database_url = _database_url()
    reference = asyncio.run(
        _seed_waiting_projection_command(
            database_url,
            successful=successful,
            command_clock_ahead=timedelta(seconds=2),
        )
    )
    assert reference.database_clock_before_skew is not None
    assert reference.command_updated_at > reference.database_clock_before_skew

    handoff = asyncio.run(_read_waiting_projection_command(database_url, reference))
    assert isinstance(handoff, Success), handoff
    timestamps = asyncio.run(_waiting_projection_causal_timestamps(database_url, reference))
    assert timestamps.pop("command_updated_at") == reference.command_updated_at
    handoff_times = set(timestamps.values())
    assert len(handoff_times) == 1
    handoff_floor = handoff_times.pop()
    assert handoff_floor >= reference.command_updated_at
    assert handoff_floor >= reference.database_clock_before_skew


@pytest.mark.parametrize(
    "tamper",
    ("missing", "objective_hash", "identity_rehashed", "stale_fence", "terminal_status"),
)
def test_waiting_projection_command_read_rejects_learner_handoff_tamper(
    tamper: str,
) -> None:
    database_url = _database_url()
    reference = asyncio.run(_seed_waiting_projection_command(database_url))
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    asyncio.run(_tamper_waiting_projection(database_url, reference, tamper))
    result = asyncio.run(_read_waiting_projection_command(database_url, reference))
    assert isinstance(result, Failure)
    assert result.error.code == "INVARIANT_VIOLATION"
    with TestClient(create_app(settings)) as client:
        response = client.get(
            f"/v1/commands/{reference.command_id}",
            headers=_command_headers(reference),
        )
        assert response.status_code == 500, response.text
        assert response.json()["error"]["code"] == "INVARIANT_VIOLATION"


def _database_url() -> str:
    value = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if value is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required PostgreSQL Turn durability coverage"
        )
    return value


async def _seed_in_progress_command(
    database_url: str,
    *,
    applying: bool,
    recovery_status: str | None = None,
) -> _InProgressCommand:
    fixture = await _seed_execution(database_url)
    try:
        await _seed_in_progress_turn_authority(fixture)
        with _patched_authority_loader(fixture):
            if applying:
                result = await _invocation(fixture, _SuccessfulSandbox()).invoke(
                    fixture.request, fixture.context
                )
                assert result.run.task_success is True
            else:
                with pytest.raises(ConnectionError, match="sandbox response lost"):
                    await _invocation(fixture, _LostSandbox()).invoke(
                        fixture.request, fixture.context
                    )
        if recovery_status is not None:
            if recovery_status not in {"RETRY_WAIT", "CLAIMED"}:
                raise AssertionError(f"unsupported recovery status: {recovery_status}")
            async with fixture.sessions() as session, session.begin():
                await fixture.jobs.retry_in_session(
                    session,
                    fixture.claim,
                    delay_seconds=0,
                    phase="SANDBOX",
                    error={"code": "UNKNOWN_COMMIT_STATE", "retryable": True},
                )
            if recovery_status == "CLAIMED":
                recovered = await fixture.jobs.claim_next(
                    tenant_id=fixture.claim.tenant_id,
                    worker_id=f"recovery_{uuid4().hex[:20]}",
                    lease_seconds=60,
                    operation="EXECUTE_AGENT_TURN",
                )
                assert recovered is not None
                assert recovered.fencing_token > fixture.claim.fencing_token
        async with fixture.sessions() as session:
            row = await session.scalar(
                select(CommandRow).where(CommandRow.command_id == fixture.claim.command_id)
            )
            receipt = await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.job_id == fixture.claim.job_id,
                    JobStepReceiptRow.step_name == "SANDBOX_DISPATCHED",
                )
            )
            job = await session.scalar(
                select(WorkflowJobRow).where(WorkflowJobRow.job_id == fixture.claim.job_id)
            )
            assert row is not None
            assert receipt is not None
            assert job is not None
            assert job.request_sha256 != receipt.input_sha256
            assert receipt.input_sha256 == receipt.receipt_json["request_sha256"]
            record = command_record_from_data(row.record_json)
            run_id = receipt.receipt_json["run_id"]
            assert isinstance(run_id, str)
            assert record.links.get("run") == f"/v1/runs/{run_id}"
            return _InProgressCommand(
                tenant_id=fixture.claim.tenant_id,
                actor_id=fixture.context.actor.actor_id,
                command_id=fixture.claim.command_id,
                run_id=run_id,
                job_status=job.status,
                job_fencing_token=job.fencing_token,
                dispatch_fencing_token=receipt.fencing_token,
            )
    finally:
        await _dispose(fixture.sessions)


async def _seed_failed_run(database_url: str) -> _InProgressCommand:
    fixture = await _seed_execution(database_url)
    try:
        with _patched_authority_loader(fixture):
            result = await _invocation(fixture, _FailedSandbox()).invoke(
                fixture.request, fixture.context
            )
        assert result.run.task_success is False
        async with fixture.sessions() as session:
            dispatch = await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.job_id == fixture.claim.job_id,
                    JobStepReceiptRow.step_name == "SANDBOX_DISPATCHED",
                )
            )
            job = await session.scalar(
                select(WorkflowJobRow).where(WorkflowJobRow.job_id == fixture.claim.job_id)
            )
            assert dispatch is not None and job is not None
            return _InProgressCommand(
                tenant_id=fixture.claim.tenant_id,
                actor_id=fixture.context.actor.actor_id,
                command_id=fixture.claim.command_id,
                run_id=result.run.run_id,
                job_status=job.status,
                job_fencing_token=job.fencing_token,
                dispatch_fencing_token=dispatch.fencing_token,
            )
    finally:
        await _dispose(fixture.sessions)


async def _seed_in_progress_turn_authority(fixture: _ExecutionFixture) -> None:
    async with fixture.sessions() as session:
        owner = await session.scalar(
            select(AgentSessionRow).where(
                AgentSessionRow.tenant_id == fixture.claim.tenant_id,
                AgentSessionRow.session_id == fixture.request.session_id,
            )
        )
        turn = await session.scalar(
            select(AgentTurnRow).where(
                AgentTurnRow.tenant_id == fixture.claim.tenant_id,
                AgentTurnRow.turn_id == fixture.request.turn_id,
                AgentTurnRow.command_id == fixture.request.command_id,
            )
        )
    assert owner is not None and turn is not None


async def _seed_terminal_command_with_legacy_null_uri(
    database_url: str,
) -> _TerminalCommand:
    fixture = await _seed_execution(database_url)
    try:
        with _patched_authority_loader(fixture):
            result = await _invocation(fixture, _SuccessfulSandbox()).invoke(
                fixture.request, fixture.context
            )
        authority, outcome, decision = await _seed_terminal_projection_authority(fixture, result)
        await _finish_and_project(fixture, authority, outcome, decision, result)
        async with fixture.sessions() as session, session.begin():
            row = await session.scalar(
                select(CommandRow)
                .where(CommandRow.command_id == fixture.claim.command_id)
                .with_for_update()
            )
            assert row is not None
            record = copy.deepcopy(row.record_json)
            references = record.get("evidence_refs")
            assert isinstance(references, list) and references
            for reference in references:
                assert isinstance(reference, dict)
                reference["uri"] = None
            row.record_json = record
        return _TerminalCommand(
            tenant_id=fixture.claim.tenant_id,
            actor_id=fixture.context.actor.actor_id,
            command_id=fixture.claim.command_id,
            run_id=result.run.run_id,
        )
    finally:
        await _dispose(fixture.sessions)


def _command_headers(
    fixture: _InProgressCommand | _TerminalCommand | _WaitingProjectionCommand,
) -> dict[str, str]:
    suffix = uuid4().hex[:20]
    return {
        "Authorization": f"Bearer {fixture.tenant_id}:{fixture.actor_id}",
        "X-Request-Id": f"req_command_read_{suffix}",
        "X-Trace-Id": f"trace_command_read_{suffix}",
        "X-Correlation-Id": f"corr_command_read_{suffix}",
        "X-Schema-Version": "1.0.0",
    }


async def _replace_command_run_link(
    database_url: str, command_id: str, run_link: str | None
) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            row = await session.scalar(
                select(CommandRow).where(CommandRow.command_id == command_id).with_for_update()
            )
            assert row is not None
            record = command_record_from_data(row.record_json)
            links = dict(record.links)
            if run_link is None:
                links.pop("run", None)
            else:
                links["run"] = run_link
            row.record_json = command_record_data(
                replace(record, links=cast(FrozenJsonObject, links))
            )
    finally:
        await sessions.kw["bind"].dispose()


async def _replace_terminal_command_evidence_uri(
    database_url: str, command_id: str, uri: object
) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            row = await session.scalar(
                select(CommandRow).where(CommandRow.command_id == command_id).with_for_update()
            )
            assert row is not None
            record = copy.deepcopy(row.record_json)
            references = record.get("evidence_refs")
            assert isinstance(references, list) and references
            reference = references[0]
            assert isinstance(reference, dict)
            reference["uri"] = uri
            row.record_json = record
    finally:
        await sessions.kw["bind"].dispose()


async def _cotamper_dispatch_world(database_url: str, fixture: _InProgressCommand) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            job = await session.scalar(
                select(WorkflowJobRow).where(WorkflowJobRow.command_id == fixture.command_id)
            )
            turn = await session.scalar(
                select(AgentTurnRow).where(AgentTurnRow.command_id == fixture.command_id)
            )
            assert job is not None and turn is not None
            receipt = await session.scalar(
                select(JobStepReceiptRow)
                .where(
                    JobStepReceiptRow.job_id == job.job_id,
                    JobStepReceiptRow.step_name == "SANDBOX_DISPATCHED",
                )
                .with_for_update()
            )
            assert receipt is not None
            output = copy.deepcopy(receipt.receipt_json)
            binding = output["skill"]
            arguments = output["arguments"]
            assert isinstance(binding, dict) and isinstance(arguments, dict)
            tampered_world_id = f"world_tampered_{uuid4().hex[:20]}"
            output["world_id"] = tampered_world_id
            request_sha256 = skill_invocation_request_sha256(
                tenant_id=job.tenant_id,
                invocation_id=output["invocation_id"],
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                command_id=fixture.command_id,
                world_id=tampered_world_id,
                expected_world_revision=output["expected_world_revision"],
                skill_ref=SkillRef(**binding),
                arguments=arguments,
            )
            output["request_sha256"] = request_sha256
            receipt.input_sha256 = request_sha256
            receipt.output_sha256 = workflow_receipt_sha256(output)
            receipt.receipt_json = output
    finally:
        await sessions.kw["bind"].dispose()


async def _replace_completion_arguments(
    database_url: str,
    command_id: str,
    arguments: dict[str, object],
) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            job = await session.scalar(
                select(WorkflowJobRow).where(WorkflowJobRow.command_id == command_id)
            )
            assert job is not None
            receipt = await session.scalar(
                select(JobStepReceiptRow)
                .where(
                    JobStepReceiptRow.job_id == job.job_id,
                    JobStepReceiptRow.step_name == "SKILL_INVOKED",
                )
                .with_for_update()
            )
            assert receipt is not None
            output = copy.deepcopy(receipt.receipt_json)
            run = output["run"]
            assert isinstance(run, dict)
            binding = run["skill_ref"]
            assert isinstance(binding, dict)
            request_sha256 = skill_invocation_request_sha256(
                tenant_id=job.tenant_id,
                invocation_id=output["invocation_id"],
                session_id=run["session_id"],
                turn_id=run["turn_id"],
                command_id=run["command_id"],
                world_id=run["world_id"],
                expected_world_revision=run["world_revision_before"],
                skill_ref=SkillRef(**binding),
                arguments=arguments,
            )
            output["arguments"] = arguments
            output["request_sha256"] = request_sha256
            receipt.input_sha256 = request_sha256
            receipt.output_sha256 = workflow_receipt_sha256(output)
            receipt.receipt_json = output
    finally:
        await sessions.kw["bind"].dispose()


async def _replace_completion_fence(database_url: str, command_id: str, fencing_token: int) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            job = await session.scalar(
                select(WorkflowJobRow).where(WorkflowJobRow.command_id == command_id)
            )
            assert job is not None
            receipt = await session.scalar(
                select(JobStepReceiptRow)
                .where(
                    JobStepReceiptRow.job_id == job.job_id,
                    JobStepReceiptRow.step_name == "SKILL_INVOKED",
                )
                .with_for_update()
            )
            assert receipt is not None
            receipt.fencing_token = fencing_token
    finally:
        await sessions.kw["bind"].dispose()


async def _assert_run_timestamp_duplicate(database_url: str, run_id: str) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session:
            row = await session.scalar(select(RunRow).where(RunRow.run_id == run_id))
            assert row is not None
            created_at = row.run_json.get("created_at")
            assert isinstance(created_at, str)
            assert row.created_at == datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    finally:
        await sessions.kw["bind"].dispose()


@dataclass(slots=True)
class _ExecutionFixture:
    sessions: async_sessionmaker[AsyncSession]
    jobs: PostgresWorkflowJobStore
    claim: ClaimedWorkflowJob
    context: OperationContext
    request: SkillInvocationRequest
    world: WorldSnapshot
    versions: VersionSet


class _LeaseResilientInvocation:
    """Model a worker restart when the host slept past the database lease."""

    def __init__(
        self,
        fixture: _ExecutionFixture,
        sandbox: Any,
        rules: WorldRules | None,
    ) -> None:
        self._fixture = fixture
        self._sandbox = sandbox
        self._rules = rules

    async def invoke(
        self,
        request: SkillInvocationRequest,
        context: OperationContext,
    ) -> Any:
        await _refresh_execution_claim_after_host_pause(self._fixture)
        try:
            return await _raw_invocation(
                self._fixture,
                self._sandbox,
                rules=self._rules,
            ).invoke(request, context)
        except WorkflowFenceLost:
            await _refresh_execution_claim_after_host_pause(self._fixture, require_expired=True)
            return await _raw_invocation(
                self._fixture,
                self._sandbox,
                rules=self._rules,
            ).invoke(request, context)


@dataclass(frozen=True, slots=True)
class _InProgressCommand:
    tenant_id: str
    actor_id: str
    command_id: str
    run_id: str
    job_status: str
    job_fencing_token: int
    dispatch_fencing_token: int


@dataclass(frozen=True, slots=True)
class _TerminalCommand:
    tenant_id: str
    actor_id: str
    command_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class _WaitingProjectionCommand:
    tenant_id: str
    actor_id: str
    command_id: str
    run_id: str
    session_id: str
    turn_id: str
    context: OperationContext
    command_updated_at: datetime
    database_clock_before_skew: datetime | None


class _InvariantFailureHandler:
    operations = frozenset({"EXECUTE_AGENT_TURN"})

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, claim: ClaimedWorkflowJob) -> None:
        del claim
        self.calls += 1
        raise WorkflowInvariantError("sensitive durable corruption detail")


class _ReplyProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.reconciliations = 0
        self.resources: dict[str, LlmDispatchResource] = {}

    async def validate_capabilities(self) -> LlmRelayCapabilities:
        return LlmRelayCapabilities(
            protocol="YAYA_RECOVERABLE_LLM_V1",
            result_retention_seconds=604_800,
            max_request_bytes=4_194_304,
            max_response_bytes=4_194_304,
            atomic_put_by_dispatch_id=True,
            linearizable_get=True,
            immutable_request_hash=True,
            max_generation_count=1,
        )

    async def dispatch(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource:
        del request, context
        self.calls += 1
        resource = _successful_provider_resource(identity)
        self.resources[identity.dispatch_id] = resource
        return resource

    async def reconcile(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource:
        del request, context
        self.reconciliations += 1
        return self.resources.get(
            identity.dispatch_id,
            LlmDispatchResource(
                identity=identity,
                completion_sha256="c" * 64,
                state="ABSENT",
                generation_count=0,
                replayed=False,
            ),
        )


class _LostReplyProvider(_ReplyProvider):
    async def dispatch(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource:
        await super().dispatch(identity, request, context)
        raise RecoverableLlmUnavailable("provider response and immediate GET were lost")


def _successful_provider_resource(identity: LlmDispatchIdentity) -> LlmDispatchResource:
    return LlmDispatchResource(
        identity=identity,
        completion_sha256="c" * 64,
        state="SUCCEEDED",
        generation_count=1,
        replayed=False,
        result=Success(
            LlmReply(
                output=cast(FrozenJsonObject, {"decision": "invoke_skill"}),
                provider="fake-provider",
                model="fake-model-v1",
                source="provider",
                degraded=False,
                fallback_reason=None,
                input_tokens=3,
                output_tokens=2,
                evidence_refs=(),
            )
        ),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        raw_response_sha256="d" * 64,
    )


class _SuccessfulSandbox:
    def __init__(self) -> None:
        self.calls = 0
        self.reconciliations = 0
        self.outcome: Any | None = None

    async def run(self, request: Any, context: OperationContext) -> Any:
        del context
        self.calls += 1
        now = datetime.now(UTC)
        self.outcome = Success(
            SandboxRunResult(
                run_id=request.run_id,
                started_at=now,
                finished_at=now,
                action_intents=(
                    MoveIntent(
                        intent_id=f"intent_{request.run_id[-16:]}",
                        actor_entity_id="avatar_turn_durable",
                        expected_world_revision=0,
                        destination=WorldPosition(2, 1),
                    ),
                ),
                stdout_ref=None,
                stderr_ref=None,
                usage=SandboxUsage(cpu_ms=2, wall_ms=3, peak_memory_bytes=4096),
                evidence_refs=(),
            )
        )
        return self.outcome

    async def reconcile(self, request: Any, context: OperationContext) -> Any:
        del request, context
        self.reconciliations += 1
        assert self.outcome is not None
        return self.outcome


class _ClockSkewedIncompleteSandbox:
    def __init__(self, *, started_at: datetime, finished_at: datetime) -> None:
        self.started_at = started_at
        self.finished_at = finished_at

    async def run(self, request: Any, context: OperationContext) -> Any:
        del context
        return Success(
            SandboxRunResult(
                run_id=request.run_id,
                started_at=self.started_at,
                finished_at=self.finished_at,
                action_intents=(
                    MoveIntent(
                        intent_id=f"intent_{request.run_id[-16:]}",
                        actor_entity_id="avatar_turn_durable",
                        expected_world_revision=0,
                        destination=WorldPosition(2, 1),
                    ),
                ),
                stdout_ref=None,
                stderr_ref=None,
                usage=SandboxUsage(cpu_ms=2, wall_ms=3, peak_memory_bytes=4096),
                evidence_refs=(),
            )
        )

    async def reconcile(self, request: Any, context: OperationContext) -> Any:
        del request, context
        raise AssertionError("completed fixture Sandbox must replay the database receipt")


class _FailedSandbox:
    def __init__(self) -> None:
        self.outcome: Any | None = None

    async def run(self, request: Any, context: OperationContext) -> Any:
        del request, context
        self.outcome = Failure(
            ContractError(
                code="SANDBOX_RUNTIME_ERROR",
                category=ErrorCategory.SANDBOX,
                retryable=False,
                user_message_key="sandbox.runtime_error",
                stage="SANDBOX",
                message="The fixture Sandbox rejected execution.",
                details={"reason": "EXIT_NONZERO"},
            )
        )
        return self.outcome

    async def reconcile(self, request: Any, context: OperationContext) -> Any:
        del request, context
        assert self.outcome is not None
        return self.outcome


class _ExpireFenceAfterSuccessfulSandbox:
    def __init__(self, fixture: _ExecutionFixture) -> None:
        self.fixture = fixture
        self.delegate = _SuccessfulSandbox()

    async def run(self, request: Any, context: OperationContext) -> Any:
        outcome = await self.delegate.run(request, context)
        async with self.fixture.sessions() as session, session.begin():
            await session.execute(
                update(WorkflowJobRow)
                .where(
                    WorkflowJobRow.tenant_id == self.fixture.claim.tenant_id,
                    WorkflowJobRow.job_id == self.fixture.claim.job_id,
                    WorkflowJobRow.fencing_token == self.fixture.claim.fencing_token,
                )
                .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        return outcome

    async def reconcile(self, request: Any, context: OperationContext) -> Any:
        return await self.delegate.reconcile(request, context)


class _LostSandbox:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = {} if state is None else state
        self.calls = 0
        self.reconciliations = 0

    async def run(self, request: Any, context: OperationContext) -> Any:
        self.calls += 1
        successful = _SuccessfulSandbox()
        self.state["outcome"] = await successful.run(request, context)
        raise ConnectionError("sandbox response lost after dispatch")

    async def reconcile(self, request: Any, context: OperationContext) -> Any:
        del request, context
        self.reconciliations += 1
        return self.state.get("outcome")


class _PendingSandbox:
    def __init__(self) -> None:
        self.calls = 0
        self.reconciliations = 0

    async def run(self, request: Any, context: OperationContext) -> Any:
        del request, context
        self.calls += 1
        raise ConnectionError("sandbox response lost before terminal observation")

    async def reconcile(self, request: Any, context: OperationContext) -> Any:
        del request, context
        self.reconciliations += 1
        return None


class _UnavailableSandbox:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.calls = 0
        self.reconciliations = 0

    async def run(self, request: Any, context: OperationContext) -> Any:
        del request, context
        self.calls += 1
        raise ConnectionError("sandbox unavailable before container create")

    async def reconcile(self, request: Any, context: OperationContext) -> Any:
        self.reconciliations += 1
        outcome = self.state.get("outcome")
        if outcome is None:
            self.state["container_creates"] = int(self.state.get("container_creates", 0)) + 1
            self.state["container_starts"] = int(self.state.get("container_starts", 0)) + 1
            outcome = await _SuccessfulSandbox().run(request, context)
            self.state["outcome"] = outcome
        return outcome


async def _exercise_provider_receipts(database_url: str) -> None:
    successful = await _seed_execution(database_url)
    provider = _ReplyProvider()
    request = _llm_request(successful.versions)
    try:
        first = PostgresDurableLlm(
            session_factory=successful.sessions,
            jobs=successful.jobs,
            claim=successful.claim,
            provider=provider,
            provider_name="fake-provider",
            model_version="fake-model-v1",
            lease_seconds=60,
        )
        first_result = await first.generate(request, successful.context)
        replay = PostgresDurableLlm(
            session_factory=successful.sessions,
            jobs=successful.jobs,
            claim=successful.claim,
            provider=provider,
            provider_name="fake-provider",
            model_version="fake-model-v1",
            lease_seconds=60,
        )
        replay_result = await replay.generate(request, successful.context)
        assert replay_result == first_result
        assert provider.calls == 1

        lost = await _seed_execution(database_url)
        lost_provider = _LostReplyProvider()
        try:
            first_lost = PostgresDurableLlm(
                session_factory=lost.sessions,
                jobs=lost.jobs,
                claim=lost.claim,
                provider=lost_provider,
                provider_name="fake-provider",
                model_version="fake-model-v1",
                lease_seconds=60,
            )
            with pytest.raises(DurableLlmDispatchUnknown, match="acknowledgement is unknown"):
                await first_lost.generate(_llm_request(lost.versions), lost.context)

            recovered = PostgresDurableLlm(
                session_factory=lost.sessions,
                jobs=lost.jobs,
                claim=lost.claim,
                provider=lost_provider,
                provider_name="fake-provider",
                model_version="fake-model-v1",
                lease_seconds=60,
            )
            recovered_result = await recovered.generate(_llm_request(lost.versions), lost.context)
            assert isinstance(recovered_result, Success)
            assert lost_provider.calls == 1
            assert lost_provider.reconciliations == 1
            assert await _step_names(lost) == [
                "PROVIDER_DISPATCH_01",
                "PROVIDER_RESULT_01",
            ]
        finally:
            await _dispose(lost.sessions)
    finally:
        await _dispose(successful.sessions)


async def _exercise_root_final_provider_ordinals(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    provider = _ReplyProvider()
    request = _llm_request(fixture.versions)

    def durable(namespace: str, base: int) -> PostgresDurableLlm:
        return PostgresDurableLlm(
            session_factory=fixture.sessions,
            jobs=fixture.jobs,
            claim=fixture.claim,
            provider=provider,
            provider_name="fake-provider",
            model_version="fake-model-v1",
            lease_seconds=60,
            receipt_namespace=namespace,
            ordinal_base=base,
        )

    try:
        root = await durable("ROOT", 0).generate(request, fixture.context)
        final = await durable("FINAL", 100).generate(request, fixture.context)
        assert isinstance(root, Success) and isinstance(final, Success)
        assert provider.calls == 2
        first_dispatch_ids = set(provider.resources)
        assert len(first_dispatch_ids) == 2

        restarted_root = await durable("ROOT", 0).generate(request, fixture.context)
        restarted_final = await durable("FINAL", 100).generate(request, fixture.context)
        assert restarted_root == root and restarted_final == final
        assert provider.calls == 2
        assert provider.reconciliations == 2
        assert set(provider.resources) == first_dispatch_ids
        assert await _step_names(fixture) == [
            "ROOT_PROVIDER_DISPATCH_01",
            "ROOT_PROVIDER_RESULT_01",
            "FINAL_PROVIDER_DISPATCH_01",
            "FINAL_PROVIDER_RESULT_01",
        ]
        async with fixture.sessions() as session:
            receipts = list(
                (
                    await session.scalars(
                        select(JobStepReceiptRow).where(
                            JobStepReceiptRow.job_id == fixture.claim.job_id,
                            JobStepReceiptRow.step_name.in_(
                                (
                                    "ROOT_PROVIDER_DISPATCH_01",
                                    "FINAL_PROVIDER_DISPATCH_01",
                                )
                            ),
                        )
                    )
                ).all()
            )
        by_name = {receipt.step_name: receipt.receipt_json for receipt in receipts}
        assert by_name["ROOT_PROVIDER_DISPATCH_01"]["ordinal"] == 1
        assert by_name["FINAL_PROVIDER_DISPATCH_01"]["ordinal"] == 101
    finally:
        await _dispose(fixture.sessions)


async def _exercise_provider_context_binding(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    provider = _ReplyProvider()
    request = _llm_request(fixture.versions)
    try:
        wrong_command = replace(
            fixture.context,
            command_id=f"cmd_wrong_{fixture.request.invocation_id[-20:]}",
        )
        with pytest.raises(WorkflowInvariantError):
            await PostgresDurableLlm(
                session_factory=fixture.sessions,
                jobs=fixture.jobs,
                claim=fixture.claim,
                provider=provider,
                provider_name="fake-provider",
                model_version="fake-model-v1",
                lease_seconds=60,
            ).generate(request, wrong_command)
        assert provider.calls == 0
        assert await _step_names(fixture) == []

        await PostgresDurableLlm(
            session_factory=fixture.sessions,
            jobs=fixture.jobs,
            claim=fixture.claim,
            provider=provider,
            provider_name="fake-provider",
            model_version="fake-model-v1",
            lease_seconds=60,
        ).generate(request, fixture.context)
        assert provider.calls == 1

        cross_context = replace(
            fixture.context,
            actor=ActorRef(
                tenant_id=fixture.context.actor.tenant_id,
                actor_id=f"other_{fixture.request.invocation_id[-20:]}",
                actor_type=ActorType.STUDENT,
                roles=("game:player",),
            ),
            content_ref=ContentRef(
                unit_id="UNIT_CROSS_CONTEXT",
                version="1.0.0",
                content_hash=hashlib.sha256(b"cross-context").hexdigest(),
            ),
        )
        with pytest.raises(WorkflowInvariantError):
            await PostgresDurableLlm(
                session_factory=fixture.sessions,
                jobs=fixture.jobs,
                claim=fixture.claim,
                provider=provider,
                provider_name="fake-provider",
                model_version="fake-model-v1",
                lease_seconds=60,
            ).generate(request, cross_context)
        assert provider.calls == 1
        assert await _step_names(fixture) == [
            "PROVIDER_DISPATCH_01",
            "PROVIDER_RESULT_01",
        ]
    finally:
        await _dispose(fixture.sessions)


async def _exercise_stale_fence(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    provider = _ReplyProvider()
    try:
        async with fixture.sessions() as session, session.begin():
            await session.execute(
                update(WorkflowJobRow)
                .where(WorkflowJobRow.job_id == fixture.claim.job_id)
                .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        takeover = await fixture.jobs.claim_next(
            tenant_id=fixture.claim.tenant_id,
            worker_id=f"worker_takeover_{uuid4().hex[:12]}",
            lease_seconds=60,
            operation="EXECUTE_AGENT_TURN",
        )
        assert takeover is not None
        assert takeover.fencing_token == fixture.claim.fencing_token + 1

        stale = PostgresDurableLlm(
            session_factory=fixture.sessions,
            jobs=fixture.jobs,
            claim=fixture.claim,
            provider=provider,
            provider_name="fake-provider",
            model_version="fake-model-v1",
            lease_seconds=60,
        )
        with pytest.raises(WorkflowFenceLost, match="fence lost"):
            await stale.generate(_llm_request(fixture.versions), fixture.context)
        assert provider.calls == 0
        assert await _step_names(fixture) == []
    finally:
        await _dispose(fixture.sessions)


async def _exercise_sandbox_response_loss(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    durable_state: dict[str, Any] = {}
    first_process = _LostSandbox(durable_state)
    invocation = _invocation(fixture, first_process)
    try:
        with _patched_authority_loader(fixture):
            with pytest.raises(ConnectionError, match="sandbox response lost"):
                await invocation.invoke(fixture.request, fixture.context)

            restarted_process = _LostSandbox(durable_state)
            recovered = await _invocation(fixture, restarted_process).invoke(
                fixture.request, fixture.context
            )
            replay = await _invocation(fixture, restarted_process).invoke(
                fixture.request, fixture.context
            )
        assert recovered == replay
        assert recovered.run.task_success is True
        assert first_process.calls == 1
        assert first_process.reconciliations == 0
        assert restarted_process.calls == 0
        assert restarted_process.reconciliations == 1
        assert await _step_names(fixture) == ["SANDBOX_DISPATCHED", "SKILL_INVOKED"]
        assert await _projection_state(fixture) == (1, 1, 1, 2)
        authority, outcome, decision = await _seed_terminal_projection_authority(fixture, recovered)
        await _finish_and_project(fixture, authority, outcome, decision, recovered)
        assert await _runtime_side_effect_counts(fixture) == (1, 1, 1)
    finally:
        await _dispose(fixture.sessions)


async def _exercise_midflight_host_pause_recovery(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    stale_claim = fixture.claim
    sandbox = _ExpireFenceAfterSuccessfulSandbox(fixture)
    try:
        with _patched_authority_loader(fixture):
            result = await _invocation(fixture, sandbox).invoke(
                fixture.request,
                fixture.context,
            )
        assert result.run.task_success is True
        assert fixture.claim.job_id == stale_claim.job_id
        assert fixture.claim.fencing_token == stale_claim.fencing_token + 1
        assert sandbox.delegate.calls == 1
        assert sandbox.delegate.reconciliations == 1
        assert await _step_names(fixture) == ["SANDBOX_DISPATCHED", "SKILL_INVOKED"]
        commands = PostgresCommandStore(fixture.sessions)
        in_progress = await commands.get(fixture.claim.command_id, fixture.context)
        assert isinstance(in_progress, Success), in_progress
        with pytest.raises(WorkflowFenceLost):
            await fixture.jobs.renew(
                stale_claim,
                lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
            )
        authority, outcome, decision = await _seed_terminal_projection_authority(
            fixture,
            result,
            failure_count=0,
        )
        await _finish_and_project(fixture, authority, outcome, decision, result)
        terminal = await commands.get(fixture.claim.command_id, fixture.context)
        assert isinstance(terminal, Success), terminal
        assert terminal.value.status is CommandStatus.APPLIED
        assert terminal.value.terminal is True
        assert await _runtime_side_effect_counts(fixture) == (1, 1, 1)
    finally:
        await _dispose(fixture.sessions)


async def _exercise_clock_skewed_failed_sandbox_result(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    accepted_at = fixture.context.requested_at
    sandbox_started_at = accepted_at - timedelta(seconds=2)
    sandbox_finished_at = accepted_at - timedelta(seconds=1)
    sandbox = _ClockSkewedIncompleteSandbox(
        started_at=sandbox_started_at,
        finished_at=sandbox_finished_at,
    )
    incomplete_rules = replace(_ruleset(), success_score=1)
    try:
        with _patched_authority_loader(fixture):
            result = await _invocation(fixture, sandbox, rules=incomplete_rules).invoke(
                fixture.request,
                fixture.context,
            )
        assert result.run.task_success is False

        # This canonical outcome construction is the exact formal-worker branch
        # that used to reject occurred_at before its root Turn timestamp.
        _authority, outcome, _decision = await _seed_terminal_projection_authority(
            fixture,
            result,
            failure_count=1,
        )
        async with fixture.sessions() as session:
            run_row = await session.scalar(
                select(RunRow).where(RunRow.command_id == fixture.claim.command_id)
            )
        assert run_row is not None
        assert run_row.created_at == accepted_at
        assert run_row.run_json["created_at"] == accepted_at.isoformat().replace("+00:00", "Z")
        assert run_row.run_json["sandbox"]["started_at"] == sandbox_started_at.isoformat().replace(
            "+00:00", "Z"
        )
        assert run_row.run_json["sandbox"][
            "finished_at"
        ] == sandbox_finished_at.isoformat().replace("+00:00", "Z")
        assert result.run.evidence_refs[0].created_at == sandbox_finished_at
        assert outcome.occurred_at == accepted_at
    finally:
        await _dispose(fixture.sessions)


async def _exercise_run_outcome_runtime_invariant(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    try:
        with _patched_authority_loader(fixture):
            result = await _invocation(fixture, _FailedSandbox()).invoke(
                fixture.request,
                fixture.context,
            )
        authority, _outcome, _decision = await _seed_terminal_projection_authority(
            fixture,
            result,
            failure_count=1,
            record_final_authority=False,
        )
        outcomes = PostgresRunOutcomeAuthority(
            fixture.sessions,
            fixture.jobs,
            lease_seconds=60,
        )
        with (
            patch.object(
                run_outcome_module,
                "derive_run_outcome_event",
                side_effect=RunOutcomeInvariantError("sensitive authority detail"),
            ),
            pytest.raises(
                WorkflowInvariantError,
                match="Run outcome derivation rejected durable authority",
            ) as raised,
        ):
            await outcomes.derive(
                fixture.claim,
                root_event=authority.event,
                context=fixture.context,
            )
        assert type(raised.value) is WorkflowInvariantError
        assert isinstance(raised.value.__cause__, RunOutcomeInvariantError)
    finally:
        await _dispose(fixture.sessions)


async def _exercise_workflow_invariant_dead_letter(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    handler = _InvariantFailureHandler()
    try:
        # The seeding helper claims but never executes the Job. Expire only that
        # lease so the worker under test can take a fresh fence.
        async with fixture.sessions() as session, session.begin():
            await session.execute(
                update(WorkflowJobRow)
                .where(
                    WorkflowJobRow.tenant_id == fixture.claim.tenant_id,
                    WorkflowJobRow.job_id == fixture.claim.job_id,
                    WorkflowJobRow.fencing_token == fixture.claim.fencing_token,
                )
                .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        worker = WorkflowWorker(
            session_factory=fixture.sessions,
            jobs=fixture.jobs,
            commands=PostgresCommandStore(fixture.sessions),
            handlers=(handler,),
            worker_id=f"worker_invariant_{uuid4().hex[:16]}",
            lease_seconds=60,
            maximum_attempts=5,
        )
        assert await worker.run_once(fixture.claim.tenant_id) is True
        assert handler.calls == 1
        assert await worker.run_once(fixture.claim.tenant_id) is False
        assert handler.calls == 1

        async with fixture.sessions() as session:
            job = await session.scalar(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == fixture.claim.tenant_id,
                    WorkflowJobRow.job_id == fixture.claim.job_id,
                )
            )
            command_row = await session.scalar(
                select(CommandRow).where(
                    CommandRow.tenant_id == fixture.claim.tenant_id,
                    CommandRow.command_id == fixture.claim.command_id,
                )
            )
            receipts = list(
                await session.scalars(
                    select(JobStepReceiptRow)
                    .where(
                        JobStepReceiptRow.tenant_id == fixture.claim.tenant_id,
                        JobStepReceiptRow.job_id == fixture.claim.job_id,
                        JobStepReceiptRow.step_name.like("WORKER_FAILURE_%"),
                    )
                    .order_by(JobStepReceiptRow.step_name)
                )
            )
        assert job is not None and command_row is not None
        assert job.status == "DEAD_LETTER"
        assert len(receipts) == 1
        assert receipts[0].step_name == "WORKER_FAILURE_2"
        assert receipts[0].receipt_json == {
            "code": "WORKFLOW_EXECUTION_FAILED",
            "exception_type": "WorkflowInvariantError",
            "attempt": 2,
        }
        command = command_record_from_data(command_row.record_json)
        assert command.status is CommandStatus.FAILED
        assert command.terminal is True
    finally:
        await _dispose(fixture.sessions)


async def _exercise_sandbox_pending_reconciliation(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    first_process = _PendingSandbox()
    try:
        with _patched_authority_loader(fixture):
            with pytest.raises(ConnectionError, match="before terminal observation"):
                await _invocation(fixture, first_process).invoke(fixture.request, fixture.context)
            restarted_process = _PendingSandbox()
            with pytest.raises(AgentToolExecutionError) as raised:
                await _invocation(fixture, restarted_process).invoke(
                    fixture.request, fixture.context
                )
        assert raised.value.code == "UNKNOWN_COMMIT_STATE"
        assert raised.value.details["retryable"] is True
        assert first_process.calls == 1
        assert restarted_process.calls == 0
        assert restarted_process.reconciliations == 1
        assert await _step_names(fixture) == ["SANDBOX_DISPATCHED"]
        assert await _projection_state(fixture) == (0, 0, 0, 0)
    finally:
        await _dispose(fixture.sessions)


async def _exercise_sandbox_unavailable_recovery(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    durable_state: dict[str, Any] = {}
    first_process = _UnavailableSandbox(durable_state)
    try:
        with _patched_authority_loader(fixture):
            with pytest.raises(ConnectionError, match="before container create"):
                await _invocation(fixture, first_process).invoke(fixture.request, fixture.context)
            restarted_process = _UnavailableSandbox(durable_state)
            recovered = await _invocation(fixture, restarted_process).invoke(
                fixture.request, fixture.context
            )
            replay = await _invocation(fixture, restarted_process).invoke(
                fixture.request, fixture.context
            )
        assert recovered == replay
        assert recovered.run.task_success is True
        assert first_process.calls == 1
        assert first_process.reconciliations == 0
        assert restarted_process.calls == 0
        assert restarted_process.reconciliations == 1
        assert durable_state["container_creates"] == 1
        assert durable_state["container_starts"] == 1
        assert await _step_names(fixture) == ["SANDBOX_DISPATCHED", "SKILL_INVOKED"]
        assert await _projection_state(fixture) == (1, 1, 1, 2)
    finally:
        await _dispose(fixture.sessions)


async def _exercise_successful_atomic_publish(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    sandbox = _SuccessfulSandbox()
    try:
        with _patched_authority_loader(fixture):
            result = await _invocation(fixture, sandbox).invoke(fixture.request, fixture.context)
            replay = await _invocation(fixture, sandbox).invoke(fixture.request, fixture.context)
        assert result == replay
        assert result.run.task_success is True
        assert result.run.world_commit is not None
        assert sandbox.calls == 1
        assert await _step_names(fixture) == ["SANDBOX_DISPATCHED", "SKILL_INVOKED"]
        assert await _projection_state(fixture) == (1, 1, 1, 2)
    finally:
        await _dispose(fixture.sessions)


async def _exercise_atomic_publish_rollback(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    sandbox = _SuccessfulSandbox()
    duplicate_id = invocation_module._identifier("evidence_run", fixture.request.invocation_id)
    try:
        async with fixture.sessions() as session, session.begin():
            session.add(
                EvidenceRow(
                    evidence_id=duplicate_id,
                    tenant_id=fixture.claim.tenant_id,
                    actor_id=fixture.context.actor.actor_id,
                    content_hash=fixture.context.content_ref.content_hash,
                    command_id=None,
                    recorded_at=datetime.now(UTC),
                    evidence_json={"fixture": "forced duplicate evidence identity"},
                )
            )
        with _patched_authority_loader(fixture):
            with pytest.raises(IntegrityError):
                await _invocation(fixture, sandbox).invoke(fixture.request, fixture.context)
        assert sandbox.calls == 1
        assert await _step_names(fixture) == ["SANDBOX_DISPATCHED"]
        assert await _projection_state(fixture) == (0, 0, 0, 0)
    finally:
        await _dispose(fixture.sessions)


async def _exercise_world_failure_rollback(database_url: str) -> None:
    """A returned Failure must not commit events/snapshot written before the failure."""

    fixture = await _seed_execution(database_url)
    outbox = PostgresOutbox(fixture.sessions)
    destination = "FEISHU_REPORT_DRAFT"
    idempotency_key = f"world-conflict-{uuid4().hex}"
    first_id = f"outbox_first_{uuid4().hex}"
    conflicting_id = f"outbox_conflict_{uuid4().hex}"

    def message(message_id: str, report_id: str) -> OutboxMessage:
        return OutboxMessage(
            message_id=message_id,
            destination=destination,
            idempotency_key=idempotency_key,
            payload=DeliveryPayload(
                delivery_id=message_id,
                operation=destination,
                deduplication_key=idempotency_key,
                attempt=1,
                body=FeishuReportDraftBody(report_id=report_id),
            ),
            created_at=datetime.now(UTC),
            operation_context=fixture.context,
        )

    try:
        seeded = await outbox.enqueue(message(first_id, f"report_{uuid4().hex}"), fixture.context)
        assert isinstance(seeded, Success)
        intent = MoveIntent(
            intent_id=f"intent_{uuid4().hex}",
            actor_entity_id="avatar_turn_durable",
            expected_world_revision=0,
            destination=WorldPosition(2, 1),
        )
        transition = WorldEngine().apply(fixture.world.state, (intent,), _ruleset())
        run_id = f"run_world_conflict_{uuid4().hex}"
        committed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        request = WorldAtomicCommit(
            stream_id=f"world:{fixture.world.world_id}",
            expected_stream_sequence="NO_STREAM",
            command=WorldCommand(
                run_id=run_id,
                world_id=fixture.world.world_id,
                expected_world_revision=0,
                world_rules_version="rules-1",
                skill_ref=fixture.request.skill_ref,
                intents=(intent,),
            ),
            events=(
                UncommittedEvent(
                    event_type="world.committed",
                    event_version=1,
                    producer="world-atomicity-test",
                    trace_id=fixture.context.trace_id,
                    command_id=fixture.context.command_id,
                    correlation_id=fixture.context.correlation_id,
                    causation_id=fixture.context.command_id,
                    content_ref=fixture.context.content_ref,
                    payload=cast(
                        FrozenJsonObject,
                        {
                            "commit_id": world_commit_identifier(
                                fixture.claim.tenant_id,
                                f"world:{fixture.world.world_id}",
                                run_id,
                                0,
                            ),
                            "run_id": run_id,
                            "world_id": fixture.world.world_id,
                            "previous_world_revision": 0,
                            "world_revision": 1,
                            "state_hash": transition.state_hash,
                            "applied_intent_ids": transition.applied_intent_ids,
                            "committed_at": committed_at,
                            "evidence_refs": (),
                        },
                    ),
                ),
            ),
            outbox_messages=(message(conflicting_id, f"changed_report_{uuid4().hex}"),),
        )
        result = await PostgresWorldUnitOfWork(fixture.sessions, {"rules-1": _ruleset()}).commit(
            request, fixture.context
        )
        assert isinstance(result, Failure)
        assert result.error.message is not None
        assert "outbox message conflicts" in result.error.message
        assert await _projection_state(fixture) == (0, 0, 0, 0)
    finally:
        await _dispose(fixture.sessions)


async def _exercise_terminal_projection_transaction(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    sandbox = _SuccessfulSandbox()
    try:
        with _patched_authority_loader(fixture):
            result = await _invocation(fixture, sandbox).invoke(fixture.request, fixture.context)
        assert result.run.task_success is True
        authority, outcome, decision = await _seed_terminal_projection_authority(fixture, result)
        initial = await _terminal_projection_state(fixture)
        world_commit = result.run.world_commit
        assert world_commit is not None
        assert initial == {
            "command_status": "APPLYING_WORLD",
            "command_terminal": False,
            "job_status": "RUNNING",
            "learner_revision": 0,
            "run_has_feedback": False,
            "non_world_events": 0,
            "interactions": 0,
            "projection_jobs": 0,
            "evidence": 2,
            "receipts": 6,
            "workspace_revision": 2,
            "workspace_world_revision": world_commit.world_revision,
            "workspace_event_sequence": world_commit.last_event_sequence,
            "workspace_state_hash": world_commit.state_hash,
            "workspace_interaction_sequence": 0,
            "workspace_draft_revision": 1,
        }

        await finish_turn_projection(
            session_factory=fixture.sessions,
            commands=PostgresCommandStore(fixture.sessions),
            jobs=fixture.jobs,
            authority=authority,
            outcome=outcome,
            decision=decision,
            result=result,
            lease_seconds=60,
        )
        handed_off = await _terminal_projection_state(fixture)
        assert handed_off["job_status"] == "WAITING_PROJECTION"
        assert handed_off["learner_revision"] == 0
        assert handed_off["interactions"] == 0
        # The Turn process can lose the hand-off commit acknowledgement and
        # retry the exact closure call. It must observe the immutable learner
        # objective and return without appending feedback a second time.
        await finish_turn_projection(
            session_factory=fixture.sessions,
            commands=PostgresCommandStore(fixture.sessions),
            jobs=fixture.jobs,
            authority=authority,
            outcome=outcome,
            decision=decision,
            result=result,
            lease_seconds=60,
        )
        assert await _terminal_projection_state(fixture) == handed_off
        learner_jobs = PostgresLearnerProjectionJobStore(fixture.sessions)
        learner_claim = await _claim_learner_eventually(
            learner_jobs,
            tenant_id=fixture.claim.tenant_id,
            worker_id="learner-test-first",
            lease_seconds=60,
        )
        assert learner_claim is not None
        projector = PostgresLearnerProjector(
            session_factory=fixture.sessions,
            jobs=learner_jobs,
            commands=PostgresCommandStore(fixture.sessions),
            lease_seconds=60,
        )

        async def fail_terminal_commit(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise LearnerProjectionFenceLost(
                "injected failure after all learner terminal projections"
            )

        with patch.object(
            learner_jobs,
            "complete_in_session",
            new=fail_terminal_commit,
        ):
            with pytest.raises(LearnerProjectionFenceLost, match="injected failure"):
                await projector.project(learner_claim)

        rolled_back = await _terminal_projection_state(fixture)
        assert rolled_back == handed_off

        await projector.project(learner_claim)
        await projector.validate_terminal(learner_claim)
        assert await _terminal_projection_state(fixture) == {
            "command_status": "APPLIED",
            "command_terminal": True,
            "job_status": "SUCCEEDED",
            "learner_revision": 1,
            "run_has_feedback": True,
            "non_world_events": 2,
            "interactions": 1,
            "projection_jobs": 1,
            "evidence": 3,
            "receipts": 8,
            "workspace_revision": 3,
            "workspace_world_revision": world_commit.world_revision,
            "workspace_event_sequence": world_commit.last_event_sequence,
            "workspace_state_hash": world_commit.state_hash,
            "workspace_interaction_sequence": 1,
            "workspace_draft_revision": 1,
        }
        interaction_store = PostgresProductInteractionStore(fixture.sessions)
        page = await interaction_store.list(
            fixture.request.session_id,
            0,
            100,
            fixture.context,
        )
        assert isinstance(page, Success), page
        interactions = page.value["interactions"]
        assert isinstance(interactions, list) and len(interactions) == 1
        interaction = interactions[0]
        assert isinstance(interaction, dict)
        item = await interaction_store.get(
            fixture.request.session_id,
            cast(str, interaction["interaction_id"]),
            fixture.context,
        )
        assert isinstance(item, Success), item
        assert item.value == interaction
    finally:
        await _dispose(fixture.sessions)


async def _handoff_successful_turn(
    fixture: _ExecutionFixture,
) -> None:
    with _patched_authority_loader(fixture):
        result = await _invocation(fixture, _SuccessfulSandbox()).invoke(
            fixture.request,
            fixture.context,
        )
    authority, outcome, decision = await _seed_terminal_projection_authority(
        fixture,
        result,
    )
    await finish_turn_projection(
        session_factory=fixture.sessions,
        commands=PostgresCommandStore(fixture.sessions),
        jobs=fixture.jobs,
        authority=authority,
        outcome=outcome,
        decision=decision,
        result=result,
        lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
    )


async def _claim_learner_eventually(
    jobs: PostgresLearnerProjectionJobStore,
    *,
    tenant_id: str,
    worker_id: str,
    lease_seconds: int,
) -> ClaimedLearnerProjectionJob | None:
    # Projection timestamps preserve causal host/Docker times. PostgreSQL can
    # briefly trail that clock, so READY need not mean claimable in the same tick.
    deadline = monotonic() + 2.0
    while True:
        claim = await jobs.claim_next(
            tenant_id=tenant_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if claim is not None or monotonic() >= deadline:
            return claim
        await asyncio.sleep(0.01)


async def _run_learner_worker_eventually(
    worker: LearnerProjectionWorker,
    tenant_id: str,
) -> bool:
    deadline = monotonic() + 2.0
    while True:
        if await worker.run_once(tenant_id):
            return True
        if monotonic() >= deadline:
            return False
        await asyncio.sleep(0.01)


async def _exercise_learner_takeover(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    try:
        await _handoff_successful_turn(fixture)
        assert (
            await fixture.jobs.claim_next(
                tenant_id=fixture.claim.tenant_id,
                worker_id="old-turn-worker",
                lease_seconds=60,
            )
            is None
        )
        learner_jobs = PostgresLearnerProjectionJobStore(fixture.sessions)
        stale = await _claim_learner_eventually(
            learner_jobs,
            tenant_id=fixture.claim.tenant_id,
            worker_id="learner-old-process",
            lease_seconds=60,
        )
        assert stale is not None
        async with fixture.sessions() as session, session.begin():
            await session.execute(
                update(LearnerProjectionJobRow)
                .where(
                    LearnerProjectionJobRow.tenant_id == stale.tenant_id,
                    LearnerProjectionJobRow.job_id == stale.job_id,
                )
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        takeover = await learner_jobs.claim_next(
            tenant_id=fixture.claim.tenant_id,
            worker_id="learner-new-process",
            lease_seconds=60,
        )
        assert takeover is not None
        assert takeover.fencing_token == stale.fencing_token + 1
        projector = PostgresLearnerProjector(
            session_factory=fixture.sessions,
            jobs=learner_jobs,
            commands=PostgresCommandStore(fixture.sessions),
            lease_seconds=60,
        )
        with pytest.raises(LearnerProjectionFenceLost):
            await projector.project(stale)
        await projector.project(takeover)
        await projector.validate_terminal(takeover)
        state = await _terminal_projection_state(fixture)
        assert state["learner_revision"] == 1
        assert state["interactions"] == 1
        assert state["job_status"] == "SUCCEEDED"
    finally:
        await _dispose(fixture.sessions)


async def _exercise_learner_commit_ack_loss(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    try:
        await _handoff_successful_turn(fixture)
        learner_jobs = PostgresLearnerProjectionJobStore(fixture.sessions)
        delegate = PostgresLearnerProjector(
            session_factory=fixture.sessions,
            jobs=learner_jobs,
            commands=PostgresCommandStore(fixture.sessions),
            lease_seconds=60,
        )

        class _LostCommitAck:
            project_calls = 0
            validation_calls = 0

            async def project(self, claim: Any) -> None:
                self.project_calls += 1
                await delegate.project(claim)
                raise ConnectionError("learner commit acknowledgement lost")

            async def validate_terminal(self, claim: Any) -> None:
                self.validation_calls += 1
                await delegate.validate_terminal(claim)

        lost_ack = _LostCommitAck()
        worker = LearnerProjectionWorker(
            session_factory=fixture.sessions,
            jobs=learner_jobs,
            commands=PostgresCommandStore(fixture.sessions),
            projector=lost_ack,
            worker_id="learner-ack-loss",
            lease_seconds=60,
        )
        assert await _run_learner_worker_eventually(worker, fixture.claim.tenant_id) is True
        assert lost_ack.project_calls == 1
        assert lost_ack.validation_calls == 1
        state = await _terminal_projection_state(fixture)
        assert state["learner_revision"] == 1
        assert state["interactions"] == 1
        assert state["projection_jobs"] == 1
    finally:
        await _dispose(fixture.sessions)


async def _exercise_corrupt_learner_objective(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    try:
        await _handoff_successful_turn(fixture)
        async with fixture.sessions() as session, session.begin():
            row = await session.scalar(
                select(LearnerProjectionJobRow)
                .where(
                    LearnerProjectionJobRow.tenant_id == fixture.claim.tenant_id,
                    LearnerProjectionJobRow.job_id == fixture.claim.job_id,
                )
                .with_for_update()
            )
            assert row is not None
            corrupt = copy.deepcopy(row.projection_json)
            corrupt["feedback_sha256"] = "f" * 64
            row.projection_json = corrupt
        learner_jobs = PostgresLearnerProjectionJobStore(fixture.sessions)
        worker = LearnerProjectionWorker(
            session_factory=fixture.sessions,
            jobs=learner_jobs,
            commands=PostgresCommandStore(fixture.sessions),
            projector=PostgresLearnerProjector(
                session_factory=fixture.sessions,
                jobs=learner_jobs,
                commands=PostgresCommandStore(fixture.sessions),
                lease_seconds=60,
            ),
            worker_id="learner-corrupt-objective",
            lease_seconds=60,
        )
        assert await _run_learner_worker_eventually(worker, fixture.claim.tenant_id) is True
        async with fixture.sessions() as session:
            learner_job = await session.scalar(
                select(LearnerProjectionJobRow).where(
                    LearnerProjectionJobRow.tenant_id == fixture.claim.tenant_id,
                    LearnerProjectionJobRow.job_id == fixture.claim.job_id,
                )
            )
            parent = await session.scalar(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == fixture.claim.tenant_id,
                    WorkflowJobRow.job_id == fixture.claim.job_id,
                )
            )
            learner = await session.scalar(
                select(LearnerProfileRow).where(
                    LearnerProfileRow.tenant_id == fixture.claim.tenant_id
                )
            )
            interactions = await session.scalar(
                select(func.count())
                .select_from(ProductInteractionRow)
                .where(ProductInteractionRow.tenant_id == fixture.claim.tenant_id)
            )
        assert learner_job is not None and learner_job.status == "DEAD_LETTER"
        assert parent is not None and parent.status == "DEAD_LETTER"
        assert learner is not None and learner.profile_json["revision"] == 0
        assert interactions == 0
    finally:
        await _dispose(fixture.sessions)


async def _exercise_learner_terminal_tamper(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    try:
        await _handoff_successful_turn(fixture)
        learner_jobs = PostgresLearnerProjectionJobStore(fixture.sessions)
        claim = await _claim_learner_eventually(
            learner_jobs,
            tenant_id=fixture.claim.tenant_id,
            worker_id="learner-tamper-gate",
            lease_seconds=60,
        )
        assert claim is not None
        projector = PostgresLearnerProjector(
            session_factory=fixture.sessions,
            jobs=learner_jobs,
            commands=PostgresCommandStore(fixture.sessions),
            lease_seconds=60,
        )
        await projector.project(claim)
        async with fixture.sessions() as session, session.begin():
            evidence = await session.scalar(
                select(EvidenceRow)
                .where(
                    EvidenceRow.tenant_id == claim.tenant_id,
                    EvidenceRow.command_id == claim.command_id,
                    EvidenceRow.evidence_json["source"]["source_type"].astext
                    == "LEARNER_PROJECTOR",
                )
                .with_for_update()
            )
            learner_job = await session.scalar(
                select(LearnerProjectionJobRow)
                .where(
                    LearnerProjectionJobRow.tenant_id == claim.tenant_id,
                    LearnerProjectionJobRow.job_id == claim.job_id,
                )
                .with_for_update()
            )
            assert evidence is not None and learner_job is not None
            evidence_wire = copy.deepcopy(evidence.evidence_json)
            evidence_wire["payload"]["task_id"] = "task_coordinated_tamper_01"
            evidence.evidence_json = evidence_wire
            result_wire = copy.deepcopy(learner_job.result_json)
            assert isinstance(result_wire, dict)
            result_wire["learner"]["evidence_sha256"] = canonical_json_sha256(evidence_wire)
            learner_job.result_json = result_wire
            learner_job.result_sha256 = workflow_json_sha256(result_wire)
        with pytest.raises(
            WorkflowInvariantError,
            match="terminal learner projection authority drifted",
        ) as raised:
            await projector.validate_terminal(claim)
        assert type(raised.value) is WorkflowInvariantError
        assert type(raised.value.__cause__) is LearnerProjectionInvariantError
        assert str(raised.value.__cause__) == "terminal Learner projection drifted"
    finally:
        await _dispose(fixture.sessions)


async def _exercise_learner_source_metadata_conflict(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    try:
        await _handoff_successful_turn(fixture)
        async with fixture.sessions() as session, session.begin():
            learner = await session.scalar(
                select(LearnerProfileRow)
                .where(
                    LearnerProfileRow.tenant_id == fixture.claim.tenant_id,
                    LearnerProfileRow.actor_id == fixture.context.actor.actor_id,
                )
                .with_for_update()
            )
            job = await session.scalar(
                select(LearnerProjectionJobRow).where(
                    LearnerProjectionJobRow.tenant_id == fixture.claim.tenant_id,
                    LearnerProjectionJobRow.job_id == fixture.claim.job_id,
                )
            )
            assert learner is not None and job is not None
            terminal = cast(dict[str, Any], job.projection_json["terminal_command"])
            source_refs = cast(list[dict[str, Any]], terminal["evidence_refs"])
            assert source_refs
            conflicting = copy.deepcopy(source_refs[0])
            conflicting["sha256"] = "e" * 64 if conflicting.get("sha256") == "f" * 64 else "f" * 64
            profile = dict(learner.profile_json)
            profile["evidence_refs"] = [conflicting]
            learner.profile_json = profile
            learner.profile_sha256 = canonical_json_sha256(profile)

        learner_jobs = PostgresLearnerProjectionJobStore(fixture.sessions)
        claim = await _claim_learner_eventually(
            learner_jobs,
            tenant_id=fixture.claim.tenant_id,
            worker_id="learner-source-metadata-conflict",
            lease_seconds=60,
        )
        assert claim is not None
        projector = PostgresLearnerProjector(
            session_factory=fixture.sessions,
            jobs=learner_jobs,
            commands=PostgresCommandStore(fixture.sessions),
            lease_seconds=60,
        )
        with pytest.raises(
            LearnerProjectionInvariantError,
            match="conflicting immutable metadata",
        ):
            await projector.project(claim)

        async with fixture.sessions() as session:
            learner = await session.scalar(
                select(LearnerProfileRow).where(
                    LearnerProfileRow.tenant_id == fixture.claim.tenant_id,
                    LearnerProfileRow.actor_id == fixture.context.actor.actor_id,
                )
            )
            interactions = await session.scalar(
                select(func.count())
                .select_from(ProductInteractionRow)
                .where(ProductInteractionRow.tenant_id == fixture.claim.tenant_id)
            )
            learner_events = await session.scalar(
                select(func.count())
                .select_from(EventRow)
                .where(
                    EventRow.tenant_id == fixture.claim.tenant_id,
                    EventRow.stream_id == f"learner:{claim.learner_id}",
                )
            )
            learner_evidence = await session.scalar(
                select(func.count())
                .select_from(EvidenceRow)
                .where(
                    EvidenceRow.tenant_id == fixture.claim.tenant_id,
                    EvidenceRow.evidence_json["source"]["source_type"].astext
                    == "LEARNER_PROJECTOR",
                )
            )
        assert learner is not None
        assert learner.profile_json["revision"] == 0
        assert learner.profile_json["evidence_refs"] == [conflicting]
        assert interactions == 0
        assert learner_events == 0
        assert learner_evidence == 0
    finally:
        await _dispose(fixture.sessions)


async def _exercise_learner_catalog_compaction(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    try:
        await _handoff_successful_turn(fixture)
        async with fixture.sessions() as session, session.begin():
            learner = await session.scalar(
                select(LearnerProfileRow)
                .where(
                    LearnerProfileRow.tenant_id == fixture.claim.tenant_id,
                    LearnerProfileRow.actor_id == fixture.context.actor.actor_id,
                )
                .with_for_update()
            )
            job = await session.scalar(
                select(LearnerProjectionJobRow).where(
                    LearnerProjectionJobRow.tenant_id == fixture.claim.tenant_id,
                    LearnerProjectionJobRow.job_id == fixture.claim.job_id,
                )
            )
            assert learner is not None and job is not None
            terminal = cast(dict[str, Any], job.projection_json["terminal_command"])
            source_refs = copy.deepcopy(cast(list[dict[str, Any]], terminal["evidence_refs"]))
            assert len(source_refs) == 2

            observed_at = fixture.context.requested_at.astimezone(UTC)
            observed_wire = observed_at.isoformat().replace("+00:00", "Z")
            review_wire = (observed_at + timedelta(days=1)).isoformat().replace("+00:00", "Z")
            synthetic_refs = [
                {
                    "evidence_id": f"evidence_catalog_{index:04d}",
                    "evidence_type": "SANDBOX_LOG",
                    "created_at": observed_wire,
                    "sha256": hashlib.sha256(f"catalog:{index}".encode()).hexdigest(),
                }
                for index in range(63)
            ]
            prior_catalog = [
                *synthetic_refs[:31],
                copy.deepcopy(source_refs[0]),
                *synthetic_refs[31:],
            ]
            assert len(prior_catalog) == 64

            def competency(concept: str, evidence_ids: list[str]) -> dict[str, Any]:
                return {
                    "concept": concept,
                    "evidence_stage": "OBSERVED",
                    "assistance_level": 0,
                    "last_observed_at": observed_wire,
                    "next_review_at": review_wire,
                    "evidence_ids": evidence_ids,
                }

            dropped_id = cast(str, synthetic_refs[0]["evidence_id"])
            retained_id = cast(str, synthetic_refs[1]["evidence_id"])
            untouched_id = cast(str, synthetic_refs[2]["evidence_id"])
            profile = dict(learner.profile_json)
            profile["evidence_refs"] = prior_catalog
            profile["competencies"] = {
                "catalog_trimmed": competency("catalog_trimmed", [dropped_id, retained_id]),
                "catalog_removed": competency("catalog_removed", [dropped_id]),
                "catalog_untouched": competency("catalog_untouched", [untouched_id]),
            }
            learner.profile_json = profile
            learner.profile_sha256 = canonical_json_sha256(profile)

        learner_jobs = PostgresLearnerProjectionJobStore(fixture.sessions)
        claim = await _claim_learner_eventually(
            learner_jobs,
            tenant_id=fixture.claim.tenant_id,
            worker_id="learner-catalog-compaction",
            lease_seconds=60,
        )
        assert claim is not None
        projector = PostgresLearnerProjector(
            session_factory=fixture.sessions,
            jobs=learner_jobs,
            commands=PostgresCommandStore(fixture.sessions),
            lease_seconds=60,
        )
        # This intentionally exercises the PostgreSQL projection boundary.  The
        # preloaded revision-zero catalog is not an invented immutable receipt
        # history, so the historical terminal-chain validator is out of scope.
        await projector.project(claim)

        async with fixture.sessions() as session:
            learner = await session.scalar(
                select(LearnerProfileRow).where(
                    LearnerProfileRow.tenant_id == fixture.claim.tenant_id,
                    LearnerProfileRow.actor_id == fixture.context.actor.actor_id,
                )
            )
            receipt = await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == fixture.claim.tenant_id,
                    JobStepReceiptRow.job_id == fixture.claim.job_id,
                    JobStepReceiptRow.step_name == "LEARNER_PROJECTION_COMMITTED",
                )
            )
        assert learner is not None and receipt is not None
        learner_commit = cast(dict[str, Any], receipt.receipt_json["learner"])
        projection = cast(dict[str, Any], learner_commit["projection"])
        update = cast(dict[str, Any], projection["learner_update"])
        committed_profile = cast(dict[str, Any], learner_commit["profile"])
        expected_catalog = [*prior_catalog[1:], source_refs[1]]

        assert learner.profile_json == committed_profile
        assert committed_profile["evidence_refs"] == expected_catalog
        assert len(expected_catalog) == 64
        assert len({item["evidence_id"] for item in expected_catalog}) == 64
        assert expected_catalog[30] == source_refs[0]
        assert expected_catalog[-1] == source_refs[1]
        assert update["evidence_refs"] == source_refs
        assert projection["source_evidence_ids"] == [item["evidence_id"] for item in source_refs]

        competencies = cast(dict[str, dict[str, Any]], committed_profile["competencies"])
        assert competencies["catalog_trimmed"]["evidence_ids"] == [retained_id]
        assert competencies["catalog_untouched"]["evidence_ids"] == [untouched_id]
        assert "catalog_removed" not in competencies
        assert set(competencies["world_navigation"]["evidence_ids"]) == {
            item["evidence_id"] for item in source_refs
        }
        assert update["changed_competency_ids"] == [
            "catalog_removed",
            "catalog_trimmed",
            "world_navigation",
        ]
        catalog_ids = {item["evidence_id"] for item in expected_catalog}
        assert all(
            set(value["evidence_ids"]).issubset(catalog_ids) for value in competencies.values()
        )
    finally:
        await _dispose(fixture.sessions)


async def _exercise_control_activation_clock_regression(database_url: str) -> None:
    fixture = await _seed_execution(
        database_url,
        activation_clock_regression=True,
        session_clock_regression=True,
    )
    try:
        with _patched_authority_loader(fixture):
            result = await _invocation(fixture, _FailedSandbox()).invoke(
                fixture.request,
                fixture.context,
            )
        assert result.run.task_success is False
        async with fixture.sessions() as session:
            activation = await session.scalar(
                select(SkillActivationRow).where(
                    SkillActivationRow.tenant_id == fixture.claim.tenant_id,
                    SkillActivationRow.skill_version_id
                    == fixture.request.skill_ref.skill_version_id,
                )
            )
            activation_command = await session.scalar(
                select(CommandRow).where(
                    CommandRow.tenant_id == fixture.claim.tenant_id,
                    CommandRow.command_type == "ACTIVATE_SKILL_VERSION",
                )
            )
            assert activation is not None and activation_command is not None
            activation_job = await session.scalar(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == fixture.claim.tenant_id,
                    WorkflowJobRow.command_id == activation_command.command_id,
                )
            )
            assert activation_job is not None
            activation_receipt = await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == fixture.claim.tenant_id,
                    JobStepReceiptRow.job_id == activation_job.job_id,
                    JobStepReceiptRow.step_name == "REGISTRY_ACTIVATED",
                )
            )
            activation_provenance = await session.scalar(
                select(SkillActivationProvenanceRow).where(
                    SkillActivationProvenanceRow.tenant_id == fixture.claim.tenant_id,
                    SkillActivationProvenanceRow.activation_id == activation.activation_id,
                )
            )
            session_command = await session.scalar(
                select(CommandRow).where(
                    CommandRow.tenant_id == fixture.claim.tenant_id,
                    CommandRow.command_type == "CREATE_AGENT_SESSION",
                )
            )
            assert session_command is not None
            session_job = await session.scalar(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == fixture.claim.tenant_id,
                    WorkflowJobRow.command_id == session_command.command_id,
                )
            )
            assert session_job is not None
            session_receipt = await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == fixture.claim.tenant_id,
                    JobStepReceiptRow.job_id == session_job.job_id,
                    JobStepReceiptRow.step_name == "SESSION_BOUND",
                )
            )
            binding = await session.scalar(
                select(CurrentSessionBindingRow).where(
                    CurrentSessionBindingRow.tenant_id == fixture.claim.tenant_id,
                    CurrentSessionBindingRow.session_id == fixture.request.session_id,
                )
            )
        assert activation_receipt is not None and activation_provenance is not None
        assert (
            activation_command.accepted_at
            <= activation_job.created_at
            <= activation.activated_at
            <= activation_receipt.completed_at
        )
        assert (
            activation_command.updated_at
            == activation_receipt.completed_at
            == activation_job.updated_at
            == activation_provenance.created_at
        )
        assert (
            command_record_from_data(activation_command.record_json).updated_at
            == activation_command.updated_at
        )
        assert activation_receipt.fencing_token == activation_job.fencing_token
        _assert_terminal_control_job(activation_job)

        assert session_receipt is not None and binding is not None
        assert (
            session_command.accepted_at
            <= session_job.created_at
            <= binding.bound_at
            <= session_receipt.completed_at
        )
        assert session_command.updated_at == session_receipt.completed_at == session_job.updated_at
        assert (
            command_record_from_data(session_command.record_json).updated_at
            == session_command.updated_at
        )
        assert session_receipt.fencing_token == session_job.fencing_token
        _assert_terminal_control_job(session_job)
    finally:
        await _dispose(fixture.sessions)


def _assert_terminal_control_job(job: WorkflowJobRow) -> None:
    assert job.status == "SUCCEEDED"
    assert job.phase == "COMPLETE"
    assert job.lease_owner is None
    assert job.lease_expires_at is None
    assert job.next_attempt_at is None


async def _exercise_four_role_projection_chain(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    turns = [fixture]
    runtime_reads = PostgresAgentRuntimeReads(fixture.sessions)
    try:
        roles: list[str] = []
        evidence_catalog: list[dict[str, Any]] = []
        for sequence, failure_count in ((1, 1), (2, 2), (3, 3)):
            current = (
                fixture
                if sequence == 1
                else await _seed_followup_execution(
                    fixture,
                    sequence=sequence,
                    expected_world_revision=0,
                )
            )
            if current is not fixture:
                turns.append(current)
            with _patched_authority_loader(current):
                result = await _invocation(current, _FailedSandbox()).invoke(
                    current.request,
                    current.context,
                )
            assert result.run.task_success is False
            authority, outcome, decision = (
                await _seed_terminal_projection_authority(
                    current,
                    result,
                    failure_count=failure_count,
                )
                if sequence == 1
                else await _seed_followup_projection_authority(
                    current,
                    result,
                    failure_count=failure_count,
                )
            )
            roles.append(decision.role)
            await _finish_and_project(current, authority, outcome, decision, result)
            if failure_count == 3:
                assert result.run.build_id is None
                async with fixture.sessions() as session:
                    invocation_receipt = await session.scalar(
                        select(JobStepReceiptRow).where(
                            JobStepReceiptRow.tenant_id == current.claim.tenant_id,
                            JobStepReceiptRow.job_id == current.claim.job_id,
                            JobStepReceiptRow.step_name == "SKILL_INVOKED",
                        )
                    )
                assert invocation_receipt is not None
                raw_receipt = copy.deepcopy(invocation_receipt.receipt_json)
                raw_output_sha256 = invocation_receipt.output_sha256
                direct_run = await runtime_reads.get_run(
                    result.run.run_id,
                    current.context,
                )
                same_failure_runs = await runtime_reads.list_same_failure_runs(
                    current.request.session_id,
                    cast(str, result.run.failure_key),
                    result.run.run_id,
                    failure_count,
                    current.context,
                )
                assert len(same_failure_runs) == failure_count
                assert same_failure_runs[-1] == direct_run
                assert direct_run.build_id is not None
                assert {item.build_id for item in same_failure_runs} == {direct_run.build_id}
                session_runs = await runtime_reads.list_session_runs(
                    current.request.session_id,
                    result.run.run_id,
                    current.context,
                )
                assert session_runs[-1] == direct_run
                assert {item.build_id for item in session_runs} == {direct_run.build_id}

                async with fixture.sessions() as session, session.begin():
                    provenance = await session.scalar(
                        select(SkillRunProvenanceRow)
                        .where(SkillRunProvenanceRow.run_id == result.run.run_id)
                        .with_for_update()
                    )
                    assert provenance is not None
                    provenance_sha256 = provenance.authority_sha256
                    damaged_sha256 = "e" * 64 if provenance_sha256 == "f" * 64 else "f" * 64
                    await session.execute(text("SET LOCAL session_replication_role = 'replica'"))
                    await session.execute(
                        update(SkillRunProvenanceRow)
                        .where(SkillRunProvenanceRow.run_id == result.run.run_id)
                        .values(authority_sha256=damaged_sha256)
                    )
                try:
                    with pytest.raises(
                        AgentRuntimeAuthorityError,
                        match="Run provenance is missing or corrupt",
                    ):
                        await runtime_reads.get_run(result.run.run_id, current.context)
                    with pytest.raises(
                        AgentRuntimeAuthorityError,
                        match="Run provenance is missing or corrupt",
                    ):
                        await runtime_reads.list_same_failure_runs(
                            current.request.session_id,
                            cast(str, result.run.failure_key),
                            result.run.run_id,
                            failure_count,
                            current.context,
                        )
                    with pytest.raises(
                        AgentRuntimeAuthorityError,
                        match="Run provenance is missing or corrupt",
                    ):
                        await runtime_reads.list_session_runs(
                            current.request.session_id,
                            result.run.run_id,
                            current.context,
                        )
                finally:
                    async with fixture.sessions() as session, session.begin():
                        provenance = await session.scalar(
                            select(SkillRunProvenanceRow)
                            .where(SkillRunProvenanceRow.run_id == result.run.run_id)
                            .with_for_update()
                        )
                        assert provenance is not None
                        await session.execute(
                            text("SET LOCAL session_replication_role = 'replica'")
                        )
                        await session.execute(
                            update(SkillRunProvenanceRow)
                            .where(SkillRunProvenanceRow.run_id == result.run.run_id)
                            .values(authority_sha256=provenance_sha256)
                        )
                async with fixture.sessions() as session:
                    invocation_receipt = await session.scalar(
                        select(JobStepReceiptRow).where(
                            JobStepReceiptRow.tenant_id == current.claim.tenant_id,
                            JobStepReceiptRow.job_id == current.claim.job_id,
                            JobStepReceiptRow.step_name == "SKILL_INVOKED",
                        )
                    )
                assert invocation_receipt is not None
                assert invocation_receipt.receipt_json == raw_receipt
                assert invocation_receipt.output_sha256 == raw_output_sha256
            evidence_catalog = await _assert_learner_source_catalog(
                current,
                evidence_catalog,
            )

        # Keep the certified Draft bytes unchanged for this durability-only
        # sequence.  A correction would require a new Build and Activation;
        # mutating the Draft head alone is intentionally rejected by INT2.

        successful = await _seed_followup_execution(
            fixture,
            sequence=4,
            expected_world_revision=0,
        )
        turns.append(successful)
        with _patched_authority_loader(successful):
            result = await _invocation(successful, _SuccessfulSandbox()).invoke(
                successful.request,
                successful.context,
            )
        # The success World commit must advance the Product workspace in its
        # own transaction before the Book final context closes prior history.
        async with fixture.sessions() as session, session.begin():
            workspace = await session.scalar(
                select(ProductWorkspaceRow)
                .where(
                    ProductWorkspaceRow.tenant_id == fixture.claim.tenant_id,
                    ProductWorkspaceRow.actor_id == fixture.context.actor.actor_id,
                    ProductWorkspaceRow.session_id == fixture.request.session_id,
                )
                .with_for_update()
            )
            assert workspace is not None
            current_workspace = copy.deepcopy(workspace.workspace_json)
            stale_workspace = copy.deepcopy(current_workspace)
            stale_checkpoint = cast(dict[str, Any], stale_workspace["world_checkpoint"])
            stale_checkpoint["world_revision"] = result.run.world_revision_before
            workspace.workspace_json = stale_workspace
        with pytest.raises(
            AgentRuntimeAuthorityError,
            match="terminal learner projection authority drifted",
        ) as raised:
            await runtime_reads.list_session_runs(
                successful.request.session_id,
                result.run.run_id,
                successful.context,
            )
        assert type(raised.value) is AgentRuntimeAuthorityError
        workflow_error = raised.value.__cause__
        assert type(workflow_error) is WorkflowInvariantError
        learner_error = workflow_error.__cause__
        assert type(learner_error) is LearnerProjectionInvariantError
        assert str(learner_error) == "Workspace mutable head has no exact durable authority"
        async with fixture.sessions() as session, session.begin():
            workspace = await session.scalar(
                select(ProductWorkspaceRow)
                .where(
                    ProductWorkspaceRow.tenant_id == fixture.claim.tenant_id,
                    ProductWorkspaceRow.actor_id == fixture.context.actor.actor_id,
                    ProductWorkspaceRow.session_id == fixture.request.session_id,
                )
                .with_for_update()
            )
            assert workspace is not None
            workspace.workspace_json = current_workspace
        session_runs = await runtime_reads.list_session_runs(
            successful.request.session_id,
            result.run.run_id,
            successful.context,
        )
        assert len(session_runs) == 4
        assert [item.task_success for item in session_runs] == [False, False, False, True]
        assert session_runs[-1].run_id == result.run.run_id
        direct_run = await runtime_reads.get_run(result.run.run_id, successful.context)
        assert session_runs[-1] == direct_run
        assert direct_run.build_id is not None
        assert {item.build_id for item in session_runs} == {direct_run.build_id}
        authority, outcome, decision = await _seed_followup_projection_authority(
            successful,
            result,
            failure_count=0,
        )
        roles.append(decision.role)
        await _finish_and_project(successful, authority, outcome, decision, result)
        evidence_catalog = await _assert_learner_source_catalog(
            successful,
            evidence_catalog,
        )

        assert roles == ["teaching_agent", "teaching_agent", "bug_agent", "book_agent"]
        assert evidence_catalog
        store = PostgresProductInteractionStore(fixture.sessions)
        page = await store.list(fixture.request.session_id, 0, 100, successful.context)
        assert isinstance(page, Success), page
        interactions = cast(list[dict[str, Any]], page.value["interactions"])
        assert [item["sequence"] for item in interactions] == [1, 2, 3, 4]
        assert [item["role"] for item in interactions] == roles
        async with fixture.sessions() as session:
            learner = await session.scalar(
                select(LearnerProfileRow).where(
                    LearnerProfileRow.tenant_id == fixture.claim.tenant_id
                )
            )
            commands = list(
                (
                    await session.scalars(
                        select(CommandRow)
                        .where(CommandRow.command_id.in_([item.claim.command_id for item in turns]))
                        .order_by(CommandRow.accepted_at)
                    )
                ).all()
            )
        assert learner is not None and learner.profile_json["revision"] == 4
        assert [row.status for row in commands] == [
            "REJECTED",
            "REJECTED",
            "REJECTED",
            "APPLIED",
        ]
        assert [row.record_json["stage"] for row in commands] == [
            "SANDBOX",
            "SANDBOX",
            "SANDBOX",
            "COMPLETE",
        ]
    finally:
        await _dispose(fixture.sessions)


async def _assert_learner_source_catalog(
    fixture: _ExecutionFixture,
    prior_catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    async with fixture.sessions() as session:
        learner = await session.scalar(
            select(LearnerProfileRow).where(
                LearnerProfileRow.tenant_id == fixture.claim.tenant_id,
                LearnerProfileRow.actor_id == fixture.context.actor.actor_id,
            )
        )
        job = await session.scalar(
            select(LearnerProjectionJobRow).where(
                LearnerProjectionJobRow.tenant_id == fixture.claim.tenant_id,
                LearnerProjectionJobRow.job_id == fixture.claim.job_id,
            )
        )
        run = await session.scalar(
            select(RunRow).where(
                RunRow.tenant_id == fixture.claim.tenant_id,
                RunRow.command_id == fixture.claim.command_id,
            )
        )
        receipt = await session.scalar(
            select(JobStepReceiptRow).where(
                JobStepReceiptRow.tenant_id == fixture.claim.tenant_id,
                JobStepReceiptRow.job_id == fixture.claim.job_id,
                JobStepReceiptRow.step_name == "LEARNER_PROJECTION_COMMITTED",
            )
        )
        learner_event = await session.scalar(
            select(EventRow).where(
                EventRow.tenant_id == fixture.claim.tenant_id,
                EventRow.event_json["command_id"].astext == fixture.claim.command_id,
                EventRow.stream_id.like("learner:%"),
            )
        )
    assert all(item is not None for item in (learner, job, run, receipt, learner_event))
    assert learner is not None
    assert job is not None
    assert run is not None
    assert receipt is not None
    assert learner_event is not None

    source_refs = copy.deepcopy(cast(list[dict[str, Any]], run.run_json["evidence_refs"]))
    source_ids = [cast(str, item["evidence_id"]) for item in source_refs]
    terminal = cast(dict[str, Any], job.projection_json["terminal_command"])
    learner_commit = cast(dict[str, Any], receipt.receipt_json["learner"])
    committed_profile = cast(dict[str, Any], learner_commit["profile"])
    projection = cast(dict[str, Any], learner_commit["projection"])
    update = cast(dict[str, Any], projection["learner_update"])
    event_payload = cast(dict[str, Any], learner_event.event_json["payload"])

    expected_catalog: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for reference in (*prior_catalog, *source_refs):
        evidence_id = cast(str, reference["evidence_id"])
        existing = by_id.get(evidence_id)
        if existing is not None:
            assert existing == reference
            continue
        by_id[evidence_id] = reference
        expected_catalog.append(reference)
    expected_catalog = expected_catalog[-64:]

    assert terminal["evidence_refs"] == source_refs
    assert job.projection_json["source_evidence_ids"] == source_ids
    assert update["evidence_refs"] == source_refs
    assert projection["source_evidence_ids"] == source_ids
    assert event_payload == update
    assert committed_profile == learner.profile_json
    assert committed_profile["evidence_refs"] == expected_catalog
    catalog = {
        cast(str, item["evidence_id"]): item
        for item in cast(list[dict[str, Any]], committed_profile["evidence_refs"])
    }
    assert all(
        catalog[evidence_id] == reference
        for evidence_id, reference in zip(source_ids, source_refs, strict=True)
    )
    competencies = cast(dict[str, dict[str, Any]], committed_profile["competencies"])
    assert all(set(value["evidence_ids"]).issubset(catalog) for value in competencies.values())
    return expected_catalog


async def _exercise_provider_receipt_cotamper(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    try:
        await _handoff_successful_turn(fixture)
        async with fixture.sessions() as session, session.begin():
            provider = await session.scalar(
                select(JobStepReceiptRow)
                .where(
                    JobStepReceiptRow.tenant_id == fixture.claim.tenant_id,
                    JobStepReceiptRow.job_id == fixture.claim.job_id,
                    JobStepReceiptRow.step_name == "FINAL_PROVIDER_RESULT_01",
                )
                .with_for_update()
            )
            learner = await session.scalar(
                select(LearnerProjectionJobRow)
                .where(
                    LearnerProjectionJobRow.tenant_id == fixture.claim.tenant_id,
                    LearnerProjectionJobRow.job_id == fixture.claim.job_id,
                )
                .with_for_update()
            )
            assert provider is not None and learner is not None
            provider_wire = copy.deepcopy(provider.receipt_json)
            provider_wire["result"]["reply"]["output"]["decision"][
                "requires_student_confirmation"
            ] = True
            provider.receipt_json = provider_wire
            provider.output_sha256 = workflow_receipt_sha256(provider_wire)
            objective = copy.deepcopy(learner.projection_json)
            learner.projection_json = objective
            learner.request_sha256 = workflow_json_sha256(objective)
        learner_jobs = PostgresLearnerProjectionJobStore(fixture.sessions)
        worker = LearnerProjectionWorker(
            session_factory=fixture.sessions,
            jobs=learner_jobs,
            commands=PostgresCommandStore(fixture.sessions),
            projector=PostgresLearnerProjector(
                session_factory=fixture.sessions,
                jobs=learner_jobs,
                commands=PostgresCommandStore(fixture.sessions),
                lease_seconds=60,
            ),
            worker_id="learner-provider-cotamper",
            lease_seconds=60,
        )
        assert await _run_learner_worker_eventually(worker, fixture.claim.tenant_id) is True
        state = await _terminal_projection_state(fixture)
        assert state["learner_revision"] == 0
        assert state["interactions"] == 0
        assert state["workspace_revision"] == 2
        assert state["command_terminal"] is False
        async with fixture.sessions() as session:
            job = await session.scalar(
                select(LearnerProjectionJobRow).where(
                    LearnerProjectionJobRow.tenant_id == fixture.claim.tenant_id,
                    LearnerProjectionJobRow.job_id == fixture.claim.job_id,
                )
            )
            terminal_receipt = await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == fixture.claim.tenant_id,
                    JobStepReceiptRow.job_id == fixture.claim.job_id,
                    JobStepReceiptRow.step_name == "TURN_COMPLETED",
                )
            )
        assert job is not None and job.status == "DEAD_LETTER"
        assert terminal_receipt is None
    finally:
        await _dispose(fixture.sessions)


async def _exercise_final_provider_failure_then_success(database_url: str) -> None:
    fixture = await _seed_execution(database_url)
    try:
        with _patched_authority_loader(fixture):
            failed_result = await _invocation(fixture, _FailedSandbox()).invoke(
                fixture.request,
                fixture.context,
            )
        _, failed_outcome, failed_decision = await _seed_terminal_projection_authority(
            fixture,
            failed_result,
            failure_count=1,
            record_final_authority=False,
        )
        outcomes = PostgresRunOutcomeAuthority(
            fixture.sessions,
            fixture.jobs,
            lease_seconds=60,
        )
        with pytest.raises(WorkflowBoundaryError) as raised:
            await outcomes.record_final_decision(
                fixture.claim,
                outcome=failed_outcome,
                decision=failed_decision,
                result=failed_result,
                context=fixture.context,
            )
        assert raised.value.stage == "PROVIDER_RECEIPT_HISTORY"
        failed_state = await _terminal_projection_state(fixture)
        assert failed_state["learner_revision"] == 0
        assert failed_state["interactions"] == 0
        assert failed_state["run_has_feedback"] is False
        assert failed_state["command_terminal"] is False

        successful = await _seed_followup_execution(
            fixture,
            sequence=2,
            expected_world_revision=0,
        )
        with _patched_authority_loader(successful):
            successful_result = await _invocation(
                successful,
                _SuccessfulSandbox(),
            ).invoke(successful.request, successful.context)
        authority, outcome, decision = await _seed_followup_projection_authority(
            successful,
            successful_result,
            failure_count=0,
        )
        await _finish_and_project(
            successful,
            authority,
            outcome,
            decision,
            successful_result,
        )
        store = PostgresProductInteractionStore(fixture.sessions)
        page = await store.list(fixture.request.session_id, 0, 100, successful.context)
        assert isinstance(page, Success), page
        interactions = cast(list[dict[str, Any]], page.value["interactions"])
        assert [item["sequence"] for item in interactions] == [1]
        assert [item["role"] for item in interactions] == ["book_agent"]
    finally:
        await _dispose(fixture.sessions)


async def _finish_and_project(
    fixture: _ExecutionFixture,
    authority: _TurnAuthority,
    outcome: GameEvent,
    decision: AgentDecision,
    result: Any,
) -> None:
    await finish_turn_projection(
        session_factory=fixture.sessions,
        commands=PostgresCommandStore(fixture.sessions),
        jobs=fixture.jobs,
        authority=authority,
        outcome=outcome,
        decision=decision,
        result=result,
        lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
    )
    async with fixture.sessions() as session:
        learner_row = await session.scalar(
            select(LearnerProjectionJobRow).where(
                LearnerProjectionJobRow.tenant_id == fixture.claim.tenant_id,
                LearnerProjectionJobRow.job_id == fixture.claim.job_id,
            )
        )
        command_row = await session.scalar(
            select(CommandRow).where(
                CommandRow.tenant_id == fixture.claim.tenant_id,
                CommandRow.command_id == fixture.claim.command_id,
            )
        )
    assert learner_row is not None and command_row is not None
    preterminal = command_record_from_data(command_row.record_json)
    projection = cast(dict[str, Any], learner_row.projection_json["projection"])
    recorded_at = datetime.fromisoformat(
        cast(str, projection["recorded_at"]).replace("Z", "+00:00")
    )
    assert recorded_at >= max(
        preterminal.updated_at,
        decision.completed_at,
        *(item.created_at for item in result.run.evidence_refs),
    )
    learner_jobs = PostgresLearnerProjectionJobStore(fixture.sessions)
    learner_claim = await _claim_learner_eventually(
        learner_jobs,
        tenant_id=fixture.claim.tenant_id,
        worker_id="learner-test",
        lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
    )
    assert learner_claim is not None
    projector = PostgresLearnerProjector(
        session_factory=fixture.sessions,
        jobs=learner_jobs,
        commands=PostgresCommandStore(fixture.sessions),
        lease_seconds=60,
    )
    await projector.project(learner_claim)
    await projector.validate_terminal(learner_claim)


async def _seed_waiting_projection_command(
    database_url: str,
    *,
    successful: bool = False,
    command_clock_ahead: timedelta | None = None,
) -> _WaitingProjectionCommand:
    fixture = await _seed_execution(database_url)
    try:
        with _patched_authority_loader(fixture):
            sandbox = _SuccessfulSandbox() if successful else _FailedSandbox()
            result = await _invocation(fixture, sandbox).invoke(
                fixture.request,
                fixture.context,
            )
        authority, outcome, decision = await _seed_terminal_projection_authority(
            fixture,
            result,
            failure_count=0 if successful else 1,
        )
        database_clock_before_skew: datetime | None = None
        if command_clock_ahead is not None:
            advanced_command, database_clock_before_skew = await _advance_command_clock(
                fixture,
                command_clock_ahead,
            )
            authority = replace(authority, command=advanced_command)
        turn_clock = nullcontext()
        learner_clock = nullcontext()
        if database_clock_before_skew is not None:
            turn_clock = patch(
                "walnut_backend.workers.turn_projection._database_now",
                return_value=database_clock_before_skew,
            )
            learner_clock = patch(
                "walnut_backend.adapters.postgres.learner_projection_jobs._database_now",
                return_value=database_clock_before_skew,
            )
        with turn_clock, learner_clock:
            await finish_turn_projection(
                session_factory=fixture.sessions,
                commands=PostgresCommandStore(fixture.sessions),
                jobs=fixture.jobs,
                authority=authority,
                outcome=outcome,
                decision=decision,
                result=result,
                lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
            )
        return _WaitingProjectionCommand(
            tenant_id=fixture.claim.tenant_id,
            actor_id=fixture.context.actor.actor_id,
            command_id=fixture.claim.command_id,
            run_id=result.run.run_id,
            session_id=fixture.request.session_id,
            turn_id=fixture.request.turn_id,
            context=fixture.context,
            command_updated_at=authority.command.updated_at,
            database_clock_before_skew=database_clock_before_skew,
        )
    finally:
        await _dispose(fixture.sessions)


async def _advance_command_clock(
    fixture: _ExecutionFixture,
    ahead: timedelta,
) -> tuple[CommandRecord, datetime]:
    if ahead <= timedelta(0):
        raise ValueError("Command clock advance must be positive")
    async with fixture.sessions() as session, session.begin():
        database_now = await session.scalar(select(func.clock_timestamp()))
        row = await session.scalar(
            select(CommandRow)
            .where(
                CommandRow.tenant_id == fixture.claim.tenant_id,
                CommandRow.command_id == fixture.claim.command_id,
            )
            .with_for_update()
        )
        assert isinstance(database_now, datetime) and database_now.tzinfo is not None
        assert row is not None
        current = command_record_from_data(row.record_json)
        advanced = replace(
            current,
            updated_at=max(database_now, current.updated_at) + ahead,
        )
        row.updated_at = advanced.updated_at
        row.record_json = command_record_data(advanced)
        return advanced, database_now


async def _read_waiting_projection_command(
    database_url: str,
    reference: _WaitingProjectionCommand,
) -> Success[CommandRecord] | Failure:
    sessions = create_session_factory(database_url)
    try:
        return await PostgresCommandStore(sessions).get(
            reference.command_id,
            reference.context,
        )
    finally:
        await _dispose(sessions)


async def _waiting_projection_causal_timestamps(
    database_url: str,
    reference: _WaitingProjectionCommand,
) -> dict[str, datetime]:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session:
            command = await session.scalar(
                select(CommandRow).where(
                    CommandRow.tenant_id == reference.tenant_id,
                    CommandRow.command_id == reference.command_id,
                )
            )
            learner = await session.scalar(
                select(LearnerProjectionJobRow).where(
                    LearnerProjectionJobRow.tenant_id == reference.tenant_id,
                    LearnerProjectionJobRow.command_id == reference.command_id,
                )
            )
            parent = await session.scalar(
                select(WorkflowJobRow).where(
                    WorkflowJobRow.tenant_id == reference.tenant_id,
                    WorkflowJobRow.command_id == reference.command_id,
                )
            )
            assert command is not None and learner is not None and parent is not None
            assert learner.next_attempt_at is not None
            return {
                "command_updated_at": command.updated_at,
                "learner_created_at": learner.created_at,
                "learner_updated_at": learner.updated_at,
                "learner_next_attempt_at": learner.next_attempt_at,
                "parent_updated_at": parent.updated_at,
            }
    finally:
        await _dispose(sessions)


async def _complete_waiting_projection(
    database_url: str,
    reference: _WaitingProjectionCommand,
) -> None:
    sessions = create_session_factory(database_url)
    try:
        learner_jobs = PostgresLearnerProjectionJobStore(sessions)
        projector = PostgresLearnerProjector(
            session_factory=sessions,
            jobs=learner_jobs,
            commands=PostgresCommandStore(sessions),
            lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
        )
        worker = LearnerProjectionWorker(
            session_factory=sessions,
            jobs=learner_jobs,
            commands=PostgresCommandStore(sessions),
            projector=projector,
            worker_id="learner-command-read-closure",
            lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
        )
        assert await _run_learner_worker_eventually(worker, reference.tenant_id) is True
    finally:
        await _dispose(sessions)


async def _projection_timestamp_spellings(
    database_url: str,
    reference: _WaitingProjectionCommand,
) -> dict[str, Any]:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session:
            interaction = await session.scalar(
                select(ProductInteractionRow).where(
                    ProductInteractionRow.tenant_id == reference.tenant_id,
                    ProductInteractionRow.actor_id == reference.actor_id,
                    ProductInteractionRow.session_id == reference.session_id,
                    ProductInteractionRow.turn_id == reference.turn_id,
                )
            )
            assert interaction is not None
            feedback_event = interaction.interaction_json.get("feedback_event")
            assert isinstance(feedback_event, dict)
            event_id = feedback_event.get("event_id")
            assert isinstance(event_id, str)
            durable_event = await session.scalar(
                select(EventRow).where(
                    EventRow.tenant_id == reference.tenant_id,
                    EventRow.event_id == event_id,
                )
            )
            learner_job = await session.scalar(
                select(LearnerProjectionJobRow).where(
                    LearnerProjectionJobRow.tenant_id == reference.tenant_id,
                    LearnerProjectionJobRow.command_id == reference.command_id,
                )
            )
            assert durable_event is not None and learner_job is not None
            receipt = await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == reference.tenant_id,
                    JobStepReceiptRow.job_id == learner_job.job_id,
                    JobStepReceiptRow.step_name == "TURN_COMPLETED",
                )
            )
            assert receipt is not None
            return {
                "public": feedback_event["occurred_at"],
                "durable": durable_event.event_json["occurred_at"],
                "interaction_id": interaction.interaction_id,
                "receipt_interaction_id": receipt.receipt_json["interaction_id"],
                "receipt_sequence": receipt.receipt_json["sequence"],
            }
    finally:
        await _dispose(sessions)


async def _tamper_waiting_projection(
    database_url: str,
    reference: _WaitingProjectionCommand,
    tamper: str,
) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            learner = await session.scalar(
                select(LearnerProjectionJobRow)
                .where(
                    LearnerProjectionJobRow.tenant_id == reference.tenant_id,
                    LearnerProjectionJobRow.command_id == reference.command_id,
                )
                .with_for_update()
            )
            assert learner is not None
            if tamper == "missing":
                await session.delete(learner)
                return
            if tamper in {"objective_hash", "identity_rehashed"}:
                objective = copy.deepcopy(learner.projection_json)
                identity = objective.get("identity")
                assert isinstance(identity, dict)
                identity["run_id"] = "run_tampered_valid_0001"
                learner.projection_json = objective
                if tamper == "identity_rehashed":
                    learner.request_sha256 = workflow_json_sha256(objective)
                return
            if tamper == "stale_fence":
                learner.fencing_token = learner.attempt + 1
                return
            if tamper == "terminal_status":
                now = await session.scalar(select(func.clock_timestamp()))
                assert isinstance(now, datetime) and now.tzinfo is not None
                learner.status = "DEAD_LETTER"
                learner.next_attempt_at = None
                learner.last_error_json = {"code": "INVARIANT_VIOLATION"}
                learner.completed_at = now
                learner.updated_at = now
                return
            raise AssertionError(f"unknown learner hand-off tamper: {tamper}")
    finally:
        await _dispose(sessions)


async def _seed_terminal_projection_authority(
    fixture: _ExecutionFixture,
    result: Any,
    *,
    failure_count: int = 0,
    record_final_authority: bool = True,
) -> tuple[_TurnAuthority, GameEvent, AgentDecision]:
    async with fixture.sessions() as existing_session:
        existing_owner = await existing_session.scalar(
            select(AgentSessionRow).where(
                AgentSessionRow.tenant_id == fixture.claim.tenant_id,
                AgentSessionRow.session_id == fixture.request.session_id,
            )
        )
        existing_content = await existing_session.scalar(
            select(ProductContentUnitRow).where(
                ProductContentUnitRow.tenant_id == fixture.claim.tenant_id,
                ProductContentUnitRow.unit_id == fixture.context.content_ref.unit_id,
                ProductContentUnitRow.version == fixture.context.content_ref.version,
            )
        )
        existing_turn = await existing_session.scalar(
            select(AgentTurnRow).where(
                AgentTurnRow.tenant_id == fixture.claim.tenant_id,
                AgentTurnRow.turn_id == fixture.request.turn_id,
            )
        )
    if existing_owner is not None and existing_content is not None and existing_turn is not None:
        existing_task = cast(dict[str, Any], existing_content.content_json["task"])
        existing_learner_id = cast(str, existing_owner.session_json["learner_id"])
        return await _build_terminal_projection_authority(
            fixture,
            result,
            learner_id=existing_learner_id,
            task=existing_task,
            failure_count=failure_count,
            record_final_authority=record_final_authority,
        )

    now = datetime.now(UTC) - timedelta(seconds=5)
    suffix = fixture.request.invocation_id[-20:]
    learner_id = f"learner_{suffix}"
    agent_profile_id = f"agent_profile_{suffix}"
    authority_id = f"authority_{suffix}"
    build_id = f"build_{suffix}"
    task = {
        "task_id": f"task_{suffix}",
        "name": "Durable projection task",
        "goal": "Validate one durable projection.",
        "story": {"opening": "A deterministic integration fixture."},
        "knowledge_points": ["world_navigation"],
        "hint_policy": {"max_level": 4},
    }
    session_resource = {
        "request_context": request_context_data(fixture.context),
        "session_id": fixture.request.session_id,
        "world_id": fixture.request.world_id,
        "learner_id": learner_id,
        "agent_profile_id": agent_profile_id,
        "channel": "GAME",
        "status": "ACTIVE",
        "content": {
            "unit_id": fixture.context.content_ref.unit_id,
            "version": fixture.context.content_ref.version,
            "content_hash": fixture.context.content_ref.content_hash,
        },
    }
    draft_id = f"draft_{suffix}"
    draft_sha256 = hashlib.sha256(f"draft:{suffix}".encode()).hexdigest()
    draft_resource = {
        "draft_id": draft_id,
        "skill_id": fixture.request.skill_ref.skill_id,
        "revision": 1,
        "draft_sha256": draft_sha256,
    }
    workspace_resource = initial_workspace_resource(
        tenant_id=fixture.claim.tenant_id,
        session_resource=session_resource,
        world_revision=0,
        last_event_sequence=0,
        state_hash=fixture.world.state_hash,
        draft_resource=draft_resource,
        task_id=cast(str, task["task_id"]),
        created_at=now,
    )
    workspace_id = cast(str, workspace_resource["workspace_id"])
    profile: dict[str, Any] = {
        "learner_id": learner_id,
        "revision": 0,
        "model_version": LEARNER_PROJECTION_POLICY_VERSION,
        "review_policy_version": REVIEW_POLICY_VERSION,
        "projected_through_sequence": 0,
        "competencies": {},
        "evidence_refs": [],
        "updated_at": now.isoformat().replace("+00:00", "Z"),
    }
    agent_profile = {
        "agent_profile_id": agent_profile_id,
        "provider": "fake-provider",
        "model_version": fixture.versions.model_version,
        "prompt_version": fixture.versions.prompt_version,
    }
    policy = {
        "schema_version": "1.0.0",
        "compiler_profile": "YAYA_CPP20_SAFE_V1",
        "compiler_version": fixture.versions.compiler_version,
        "compiler_image": (
            "ghcr.io/yaya/student-cpp@" + cast(str, fixture.versions.sandbox_image_digest)
        ),
        "test_suite_version": fixture.versions.test_suite_version,
        "compile_flags": [],
        "public_tests": [],
        "hidden_tests": [],
        "limits": {},
    }
    activation_id = f"activation_{suffix}"
    activation_wire = {
        "request_context": request_context_data(fixture.context),
        "activation_id": activation_id,
        "skill_id": fixture.request.skill_ref.skill_id,
        "skill_version_id": fixture.request.skill_ref.skill_version_id,
        "certification_id": fixture.request.skill_ref.certification_id,
        "artifact_sha256": fixture.request.skill_ref.artifact_sha256,
        "activation_scope": {
            "world_id": fixture.request.world_id,
            "agent_profile_id": agent_profile_id,
        },
        "previous_registry_revision": 0,
        "registry_revision": 1,
        "activated_at": now.isoformat().replace("+00:00", "Z"),
    }
    entry_wire = {
        "authority_id": authority_id,
        "activation_id": activation_id,
        "actor_id": fixture.context.actor.actor_id,
        "content_hash": fixture.context.content_ref.content_hash,
        "world_id": fixture.request.world_id,
        "agent_profile_id": agent_profile_id,
        "skill_id": fixture.request.skill_ref.skill_id,
        "skill_version_id": fixture.request.skill_ref.skill_version_id,
        "certification_id": fixture.request.skill_ref.certification_id,
        "artifact_sha256": fixture.request.skill_ref.artifact_sha256,
        "previous_revision": 0,
        "revision": 1,
        "activated_at": now.isoformat().replace("+00:00", "Z"),
    }
    async with fixture.sessions() as session, session.begin():
        session.add_all(
            [
                AgentSessionRow(
                    session_id=fixture.request.session_id,
                    tenant_id=fixture.claim.tenant_id,
                    actor_id=fixture.context.actor.actor_id,
                    command_id=f"cmd_session_{fixture.request.invocation_id[-20:]}",
                    world_id=fixture.request.world_id,
                    status="ACTIVE",
                    created_at=now,
                    updated_at=now,
                    session_json=session_resource,
                ),
                ProductDraftRow(
                    tenant_id=fixture.claim.tenant_id,
                    actor_id=fixture.context.actor.actor_id,
                    session_id=fixture.request.session_id,
                    draft_id=draft_id,
                    skill_id=fixture.request.skill_ref.skill_id,
                    revision=1,
                    draft_sha256=draft_sha256,
                    created_at=now,
                    updated_at=now,
                    draft_json=draft_resource,
                ),
                ProductWorkspaceRow(
                    workspace_id=workspace_id,
                    tenant_id=fixture.claim.tenant_id,
                    actor_id=fixture.context.actor.actor_id,
                    session_id=fixture.request.session_id,
                    workspace_revision=1,
                    updated_at=now,
                    workspace_json=workspace_resource,
                ),
                AgentTurnRow(
                    tenant_id=fixture.claim.tenant_id,
                    actor_id=fixture.context.actor.actor_id,
                    session_id=fixture.request.session_id,
                    turn_id=fixture.request.turn_id,
                    command_id=fixture.request.command_id,
                    turn_sequence=1,
                    created_at=fixture.context.requested_at,
                    request_json={
                        "turn_id": fixture.request.turn_id,
                        "expected_world_revision": 0,
                        "input": {"type": "MESSAGE", "text": "move"},
                        "skill_bindings": [
                            {
                                "skill_id": fixture.request.skill_ref.skill_id,
                                "skill_version_id": (fixture.request.skill_ref.skill_version_id),
                                "artifact_sha256": (fixture.request.skill_ref.artifact_sha256),
                                "certification_id": (fixture.request.skill_ref.certification_id),
                            }
                        ],
                        "client_state": {
                            "last_event_sequence": 0,
                            "client_turn_sequence": 1,
                        },
                    },
                ),
                LearnerProfileRow(
                    tenant_id=fixture.claim.tenant_id,
                    learner_id=learner_id,
                    actor_id=fixture.context.actor.actor_id,
                    content_hash=fixture.context.content_ref.content_hash,
                    profile_sha256=canonical_json_sha256(profile),
                    profile_json=profile,
                    created_at=now,
                    updated_at=now,
                ),
                AgentProfileRow(
                    tenant_id=fixture.claim.tenant_id,
                    agent_profile_id=agent_profile_id,
                    actor_id=fixture.context.actor.actor_id,
                    content_hash=fixture.context.content_ref.content_hash,
                    profile_sha256=canonical_json_sha256(agent_profile),
                    profile_json=agent_profile,
                    created_at=now,
                ),
                ProductContentUnitRow(
                    tenant_id=fixture.claim.tenant_id,
                    unit_id=fixture.context.content_ref.unit_id,
                    version=fixture.context.content_ref.version,
                    content_hash=fixture.context.content_ref.content_hash,
                    audiences=["LEARNER"],
                    published_at=now,
                    content_json={"task": task},
                ),
                BuildPolicyRow(
                    tenant_id=fixture.claim.tenant_id,
                    build_policy_id=fixture.versions.policy_version,
                    actor_id=fixture.context.actor.actor_id,
                    content_hash=fixture.context.content_ref.content_hash,
                    compiler_profile="YAYA_CPP20_SAFE_V1",
                    compiler_version=cast(str, fixture.versions.compiler_version),
                    sandbox_image_digest=cast(str, fixture.versions.sandbox_image_digest),
                    test_suite_version=cast(str, fixture.versions.test_suite_version),
                    allowed_capabilities=["WORLD_READ"],
                    max_source_files=32,
                    max_source_bytes=1_048_576,
                    policy_json=policy,
                    policy_sha256=canonical_json_sha256(policy),
                    active=True,
                    created_at=now,
                ),
                SkillBuildRow(
                    build_id=build_id,
                    tenant_id=fixture.claim.tenant_id,
                    actor_id=fixture.context.actor.actor_id,
                    command_id=f"cmd_build_{suffix}",
                    skill_id=fixture.request.skill_ref.skill_id,
                    status="SUCCEEDED",
                    terminal=True,
                    created_at=now,
                    updated_at=now,
                    build_json={"build_id": build_id},
                    request_json={"source_bundle": {"files": []}},
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                LaunchAuthorityRow(
                    tenant_id=fixture.claim.tenant_id,
                    authority_id=authority_id,
                    actor_id=fixture.context.actor.actor_id,
                    content_unit_id=fixture.context.content_ref.unit_id,
                    content_version=fixture.context.content_ref.version,
                    content_hash=fixture.context.content_ref.content_hash,
                    world_id=fixture.request.world_id,
                    learner_id=learner_id,
                    agent_profile_id=agent_profile_id,
                    build_policy_id=fixture.versions.policy_version,
                    channel="GAME",
                    teaching_spec_version=fixture.versions.teaching_spec_version,
                    authority_sha256="7" * 64,
                    active=True,
                    created_at=now,
                ),
                SkillArtifactRow(
                    tenant_id=fixture.claim.tenant_id,
                    artifact_sha256=fixture.request.skill_ref.artifact_sha256,
                    build_id=build_id,
                    actor_id=fixture.context.actor.actor_id,
                    content_hash=fixture.context.content_ref.content_hash,
                    skill_id=fixture.request.skill_ref.skill_id,
                    source_sha256="8" * 64,
                    artifact_uri=("artifact://sha256/" + fixture.request.skill_ref.artifact_sha256),
                    metadata_json={"build_id": build_id},
                    created_at=now,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                CurrentSessionBindingRow(
                    binding_id=current_session_binding_id(
                        fixture.claim.tenant_id,
                        authority_id,
                        fixture.request.session_id,
                    ),
                    tenant_id=fixture.claim.tenant_id,
                    authority_id=authority_id,
                    session_id=fixture.request.session_id,
                    actor_id=fixture.context.actor.actor_id,
                    content_hash=fixture.context.content_ref.content_hash,
                    world_id=fixture.request.world_id,
                    learner_id=learner_id,
                    agent_profile_id=agent_profile_id,
                    bound_at=now,
                ),
                SkillCertificationRow(
                    certification_id=fixture.request.skill_ref.certification_id,
                    tenant_id=fixture.claim.tenant_id,
                    build_id=build_id,
                    skill_id=fixture.request.skill_ref.skill_id,
                    skill_version_id=fixture.request.skill_ref.skill_version_id,
                    artifact_sha256=fixture.request.skill_ref.artifact_sha256,
                    actor_id=fixture.context.actor.actor_id,
                    content_hash=fixture.context.content_ref.content_hash,
                    certification_sha256="9" * 64,
                    certification_json={
                        "certification_id": fixture.request.skill_ref.certification_id
                    },
                    certified_at=now,
                ),
                RegistryHeadRow(
                    tenant_id=fixture.claim.tenant_id,
                    authority_id=authority_id,
                    actor_id=fixture.context.actor.actor_id,
                    content_hash=fixture.context.content_ref.content_hash,
                    world_id=fixture.request.world_id,
                    agent_profile_id=agent_profile_id,
                    revision=1,
                    updated_at=now,
                ),
            ]
        )
        await session.flush()
        session.add(
            RegistryEntryRow(
                tenant_id=fixture.claim.tenant_id,
                actor_id=fixture.context.actor.actor_id,
                content_hash=fixture.context.content_ref.content_hash,
                world_id=fixture.request.world_id,
                agent_profile_id=agent_profile_id,
                revision=1,
                skill_id=fixture.request.skill_ref.skill_id,
                skill_version_id=fixture.request.skill_ref.skill_version_id,
                certification_id=fixture.request.skill_ref.certification_id,
                artifact_sha256=fixture.request.skill_ref.artifact_sha256,
                previous_revision=0,
                entry_sha256=canonical_json_sha256(entry_wire),
                entry_json=entry_wire,
                activated_at=now,
            )
        )
        await session.flush()
        session.add(
            SkillActivationRow(
                activation_id=activation_id,
                tenant_id=fixture.claim.tenant_id,
                actor_id=fixture.context.actor.actor_id,
                content_hash=fixture.context.content_ref.content_hash,
                world_id=fixture.request.world_id,
                agent_profile_id=agent_profile_id,
                skill_id=fixture.request.skill_ref.skill_id,
                skill_version_id=fixture.request.skill_ref.skill_version_id,
                certification_id=fixture.request.skill_ref.certification_id,
                artifact_sha256=fixture.request.skill_ref.artifact_sha256,
                previous_registry_revision=0,
                registry_revision=1,
                activation_sha256=canonical_json_sha256(activation_wire),
                activation_json=activation_wire,
                activated_at=now,
            )
        )
    return await _build_terminal_projection_authority(
        fixture,
        result,
        learner_id=learner_id,
        task=task,
        failure_count=failure_count,
        record_final_authority=record_final_authority,
    )


async def _build_terminal_projection_authority(
    fixture: _ExecutionFixture,
    result: Any,
    *,
    learner_id: str,
    task: dict[str, Any],
    failure_count: int,
    record_final_authority: bool,
) -> tuple[_TurnAuthority, GameEvent, AgentDecision]:
    async with fixture.sessions() as session:
        command_row = await session.scalar(
            select(CommandRow).where(CommandRow.command_id == fixture.claim.command_id)
        )
        run_row = await session.scalar(
            select(RunRow).where(RunRow.command_id == fixture.claim.command_id)
        )
        turn_row = await session.scalar(
            select(AgentTurnRow).where(
                AgentTurnRow.tenant_id == fixture.claim.tenant_id,
                AgentTurnRow.turn_id == fixture.request.turn_id,
            )
        )
        learner_row = await session.scalar(
            select(LearnerProfileRow).where(
                LearnerProfileRow.tenant_id == fixture.claim.tenant_id,
                LearnerProfileRow.learner_id == learner_id,
            )
        )
    assert command_row is not None
    assert run_row is not None
    assert turn_row is not None
    assert learner_row is not None
    command = command_record_from_data(command_row.record_json)
    input_value = turn_row.request_json["input"]
    assert isinstance(input_value, dict)
    event = GameEvent(
        event_id=(
            "gameevent_"
            + hashlib.sha256(fixture.request.command_id.encode("utf-8")).hexdigest()[:24]
        ),
        event_type="run_skill_requested",
        student_id=fixture.context.actor.actor_id,
        task_id=cast(str, task["task_id"]),
        session_id=fixture.request.session_id,
        turn_id=fixture.request.turn_id,
        command_id=fixture.request.command_id,
        occurred_at=turn_row.created_at,
        expected_world_revision=fixture.request.expected_world_revision,
        skill_ref=fixture.request.skill_ref,
        payload=cast(FrozenJsonObject, {"input": dict(input_value)}),
    )
    authority = _TurnAuthority(
        claim=fixture.claim,
        command=command,
        context=fixture.context,
        event=event,
        task=task,
        learner_id=learner_id,
    )
    story = cast(dict[str, Any], task["story"])
    hint_policy = cast(dict[str, Any], task["hint_policy"])
    task_snapshot = TaskSnapshot(
        task_id=cast(str, task["task_id"]),
        title=cast(str, task["name"]),
        goal=cast(str, task["goal"]),
        story=cast(str, story["opening"]),
        knowledge_points=tuple(cast(list[str], task["knowledge_points"])),
        request_context=RequestContext(
            request_id=fixture.context.request_id,
            correlation_id=fixture.context.correlation_id,
            trace_id=fixture.context.trace_id,
            requested_at=fixture.context.requested_at,
            actor=fixture.context.actor,
            content_ref=fixture.context.content_ref,
            schema_version=fixture.context.schema_version,
        ),
        max_hint_level=cast(int, hint_policy["max_level"]),
    )
    outcome = derive_run_outcome_event(
        root_event=event,
        run=result.run,
        task=task_snapshot,
        failure_count=failure_count,
        occurred_at=max(
            (run_row.created_at, *(item.created_at for item in result.run.evidence_refs))
        ),
    )
    role = (
        "book_agent"
        if outcome.event_type == "task_completed"
        else "bug_agent"
        if failure_count >= 3
        else "teaching_agent"
    )
    response_type = "growth_summary" if role == "book_agent" else "question"
    message = (
        "The exact Skill Run completed."
        if role == "book_agent"
        else "The exact Skill Run failed with validated evidence."
    )
    question = None if role == "book_agent" else "Which invariant failed in this run?"
    completed_at = max(
        (run_row.created_at, *(item.created_at for item in result.run.evidence_refs))
    )
    profile_revision = learner_row.profile_json.get("revision")
    assert isinstance(profile_revision, int) and not isinstance(profile_revision, bool)
    directive_wire = _canonical_teaching_directive(
        outcome=outcome,
        role=role,
        task=task,
        profile={
            "revision": profile_revision,
            "competencies": {},
            "evidence_refs": [],
        },
        teaching_spec_version=fixture.versions.teaching_spec_version,
    )
    directive = TeachingDirective(
        phase=TeachingPhase(cast(str, directive_wire["phase"])),
        target_concept=cast(str | None, directive_wire["target_concept"]),
        hint_level=cast(int, directive_wire["hint_level"]),
        allowed_response_types=tuple(cast(list[Any], directive_wire["allowed_response_types"])),
        patch_eligible=cast(bool, directive_wire["patch_eligible"]),
        full_solution_eligible=cast(bool, directive_wire["full_solution_eligible"]),
        required_evidence_ids=tuple(cast(list[str], directive_wire["required_evidence_ids"])),
        reason_codes=tuple(cast(list[str], directive_wire["reason_codes"])),
        pedagogy_policy_version=cast(str, directive_wire["pedagogy_policy_version"]),
        learner_revision=cast(int, directive_wire["learner_revision"]),
        teaching_spec_version=cast(str, directive_wire["teaching_spec_version"]),
    )
    decision = AgentDecision(
        draft=DecisionDraft(
            role=cast(Any, role),
            response_type=cast(Any, response_type),
            message=message,
            question=question,
            hint_level=None,
            learner_inference=None,
            skill_patch=None,
            requires_student_confirmation=False,
        ),
        message_key=f"agent.{role}.{response_type}",
        source="provider",
        degraded=False,
        fallback_reason=None,
        provider="fake-provider",
        model="fake-model-v1",
        input_tokens=3,
        output_tokens=2,
        tool_calls=(),
        evidence_refs=result.run.evidence_refs,
        completed_at=completed_at,
        teaching_directive=directive,
    )
    outcome_wire = cast(dict[str, Any], json_value(outcome))
    outcome_sha256 = canonical_json_sha256(outcome_wire)
    provider_request_sha256 = hashlib.sha256(
        f"final-provider:{fixture.request.command_id}".encode()
    ).hexdigest()
    provider_context_sha256 = operation_context_sha256(fixture.context)
    dispatch_id = provider_dispatch_id(
        fixture.claim.tenant_id,
        fixture.claim.job_id,
        101,
        provider_request_sha256,
    )
    dispatch_output = {
        "schema_version": "2.0.0",
        "ordinal": 101,
        "dispatch_id": dispatch_id,
        "request_sha256": provider_request_sha256,
        "context_sha256": provider_context_sha256,
        "provider": "fake-provider",
        "model": "fake-model-v1",
        "command_id": fixture.request.command_id,
        "turn_id": fixture.request.turn_id,
        "timeout_ms": 30000,
    }
    result_output = {
        "schema_version": "2.0.0",
        "dispatch": {
            "dispatch_id": dispatch_id,
            "request_sha256": provider_request_sha256,
            "context_sha256": provider_context_sha256,
            "provider": "fake-provider",
            "model": "fake-model-v1",
            "completion_sha256": "e" * 64,
            "state": "SUCCEEDED",
            "generation_count": 1,
            "raw_response_sha256": "f" * 64,
        },
        "result": {
            "schema_version": "1.0.0",
            "outcome": "SUCCESS",
            "reply": {
                "output": {
                    "kind": "decision",
                    "decision": cast(dict[str, Any], json_value(decision.draft)),
                    "tool_calls": [],
                },
                "provider": "fake-provider",
                "model": "fake-model-v1",
                "source": "provider",
                "degraded": False,
                "fallback_reason": None,
                "input_tokens": 3,
                "output_tokens": 2,
                "evidence_refs": [],
            },
        },
    }
    async with fixture.sessions() as session, session.begin():
        for name, fields in (
            (
                "agent.turn.started",
                {
                    "event_id": outcome.event_id,
                    "event_type": outcome.event_type,
                    "session_id": outcome.session_id,
                    "tool_count": 0,
                    "hint_level": directive.hint_level,
                },
            ),
            (
                "agent.model.requested",
                {
                    "request_number": 1,
                    "message_count": 1,
                    "tool_round_complete": False,
                    "session_run_count": 0,
                    "skill_history_versions": [],
                },
            ),
            (
                "agent.turn.finished",
                {
                    "validated": True,
                    "fallback": False,
                    "fallback_reason": None,
                    "model_provider": "fake-provider",
                    "model": "fake-model-v1",
                    "model_requests": 1,
                    "tool_calls": 0,
                    "input_tokens": 3,
                    "output_tokens": 2,
                },
            ),
        ):
            if not record_final_authority:
                continue
            trace_record = {
                "name": name,
                "turn_id": fixture.request.turn_id,
                "role": role,
                "fields": fields,
                "command_id": fixture.request.command_id,
                "trace_id": fixture.context.trace_id,
            }
            session.add(
                AuditRow(
                    audit_id=_agent_trace_audit_id(fixture.claim.tenant_id, trace_record),
                    tenant_id=fixture.claim.tenant_id,
                    occurred_at=completed_at,
                    operation="AGENT_RUNTIME_TRACE",
                    outcome="SUCCESS",
                    record_json=trace_record,
                )
            )
        await fixture.jobs.record_step_in_session(
            session,
            fixture.claim,
            step_name="OUTCOME_DERIVED",
            input_sha256=result.request_sha256,
            output={
                "schema_version": "1.0.0",
                "event": outcome_wire,
                "run_sha256": run_authority_sha256(run_row.run_json),
                "invocation_request_sha256": result.request_sha256,
            },
        )
        if record_final_authority:
            await fixture.jobs.record_step_in_session(
                session,
                fixture.claim,
                step_name="FINAL_PROVIDER_DISPATCH_01",
                input_sha256=provider_request_sha256,
                output=dispatch_output,
            )
            provider_receipt = await fixture.jobs.record_step_in_session(
                session,
                fixture.claim,
                step_name="FINAL_PROVIDER_RESULT_01",
                input_sha256=provider_request_sha256,
                output=result_output,
            )
            await fixture.jobs.record_step_in_session(
                session,
                fixture.claim,
                step_name="FINAL_DECISION_DERIVED",
                input_sha256=outcome_sha256,
                output={
                    "schema_version": "1.0.0",
                    "outcome_event_id": outcome.event_id,
                    "outcome_sha256": outcome_sha256,
                    "run_id": result.run.run_id,
                    "invocation_request_sha256": result.request_sha256,
                    "provider_result_receipts": [
                        {
                            "receipt_id": provider_receipt.receipt_id,
                            "step_name": provider_receipt.step_name,
                            "output_sha256": provider_receipt.output_sha256,
                        }
                    ],
                    "decision": cast(dict[str, Any], json_value(decision)),
                },
            )
    return authority, outcome, decision
    """Removed superseded single-success authority fixture.
    async with fixture.sessions() as session:
        row = await session.scalar(
            select(CommandRow).where(CommandRow.command_id == fixture.claim.command_id)
        )
        run_row = await session.scalar(
            select(RunRow).where(RunRow.command_id == fixture.claim.command_id)
        )
    assert row is not None and run_row is not None
    command = command_record_from_data(row.record_json)
    event = GameEvent(
        event_id=f"gameevent_{fixture.request.invocation_id[-20:]}",
        event_type="run_skill_requested",
        student_id=fixture.context.actor.actor_id,
        task_id=task["task_id"],
        session_id=fixture.request.session_id,
        turn_id=fixture.request.turn_id,
        command_id=fixture.request.command_id,
        occurred_at=now,
        expected_world_revision=0,
        skill_ref=fixture.request.skill_ref,
        payload=cast(FrozenJsonObject, {"input": {"type": "MESSAGE", "text": "move"}}),
    )
    authority = _TurnAuthority(
        claim=fixture.claim,
        command=command,
        context=fixture.context,
        event=event,
        task=task,
        learner_id=learner_id,
    )
    task_snapshot = TaskSnapshot(
        task_id=task["task_id"],
        title="Durable projection task",
        goal="Validate one durable projection.",
        story="A deterministic integration fixture.",
        knowledge_points=("world_navigation",),
        request_context=RequestContext(
            request_id=fixture.context.request_id,
            correlation_id=fixture.context.correlation_id,
            trace_id=fixture.context.trace_id,
            requested_at=fixture.context.requested_at,
            actor=fixture.context.actor,
            content_ref=fixture.context.content_ref,
            schema_version=fixture.context.schema_version,
        ),
        max_hint_level=4,
    )
    outcome = derive_run_outcome_event(
        root_event=event,
        run=result.run,
        task=task_snapshot,
        failure_count=0,
        occurred_at=run_row.created_at,
    )
    completed_at = max(
        (run_row.created_at, *(item.created_at for item in result.run.evidence_refs))
    )
    message = "The exact Skill Run completed."
    decision = AgentDecision(
        draft=DecisionDraft(
            role="book_agent",
            response_type="growth_summary",
            message=message,
            question=None,
            hint_level=None,
            learner_inference=None,
            skill_patch=None,
            requires_student_confirmation=False,
        ),
        message_key="agent.book_agent.growth_summary",
        source="provider",
        degraded=False,
        fallback_reason=None,
        provider="fake-provider",
        model="fake-model-v1",
        input_tokens=3,
        output_tokens=2,
        tool_calls=(),
        evidence_refs=result.run.evidence_refs,
        completed_at=completed_at,
        teaching_directive=TeachingDirective(
            phase=TeachingPhase.SUMMARIZATION,
            target_concept="world_navigation",
            hint_level=0,
            allowed_response_types=("growth_summary",),
            patch_eligible=False,
            full_solution_eligible=False,
            required_evidence_ids=tuple(item.evidence_id for item in result.run.evidence_refs),
            reason_codes=(
                "TASK_COMPLETED",
                "PATCH_DISABLED_RUNTIME_STAGE",
                "FULL_SOLUTION_DISABLED",
            ),
            pedagogy_policy_version=PEDAGOGY_POLICY_VERSION,
            learner_revision=0,
            teaching_spec_version=fixture.versions.teaching_spec_version,
        ),
    )
    outcome_wire = cast(dict[str, Any], json_value(outcome))
    provider_request_sha256 = "c" * 64
    dispatch_id = f"llm_dispatch_{suffix}"
    raw_decision = {
        "role": "book_agent",
        "response_type": "growth_summary",
        "message": message,
        "question": None,
        "hint_level": None,
        "learner_inference": None,
        "skill_patch": None,
        "requires_student_confirmation": False,
    }
    dispatch_output = {
        "schema_version": "2.0.0",
        "ordinal": 101,
        "dispatch_id": dispatch_id,
        "request_sha256": provider_request_sha256,
        "context_sha256": "d" * 64,
        "provider": "fake-provider",
        "model": "fake-model-v1",
        "command_id": fixture.request.command_id,
        "turn_id": fixture.request.turn_id,
        "timeout_ms": 30000,
    }
    result_output = {
        "schema_version": "2.0.0",
        "dispatch": {
            "dispatch_id": dispatch_id,
            "request_sha256": provider_request_sha256,
            "context_sha256": "d" * 64,
            "provider": "fake-provider",
            "model": "fake-model-v1",
            "completion_sha256": "e" * 64,
            "state": "SUCCEEDED",
            "generation_count": 1,
            "raw_response_sha256": "f" * 64,
        },
        "result": {
            "schema_version": "1.0.0",
            "outcome": "SUCCESS",
            "reply": {
                "output": {
                    "kind": "decision",
                    "decision": raw_decision,
                    "tool_calls": [],
                },
                "provider": "fake-provider",
                "model": "fake-model-v1",
                "source": "provider",
                "degraded": False,
                "fallback_reason": None,
                "input_tokens": 3,
                "output_tokens": 2,
                "evidence_refs": [],
            },
        },
    }
    outcome_sha256 = canonical_json_sha256(outcome_wire)
    async with fixture.sessions() as session, session.begin():
        await fixture.jobs.record_step_in_session(
            session,
            fixture.claim,
            step_name="OUTCOME_DERIVED",
            input_sha256=result.request_sha256,
            output={
                "schema_version": "1.0.0",
                "event": outcome_wire,
                "run_sha256": run_authority_sha256(run_row.run_json),
                "invocation_request_sha256": result.request_sha256,
            },
        )
        await fixture.jobs.record_step_in_session(
            session,
            fixture.claim,
            step_name="FINAL_PROVIDER_DISPATCH_01",
            input_sha256=provider_request_sha256,
            output=dispatch_output,
        )
        provider_receipt = await fixture.jobs.record_step_in_session(
            session,
            fixture.claim,
            step_name="FINAL_PROVIDER_RESULT_01",
            input_sha256=provider_request_sha256,
            output=result_output,
        )
        await fixture.jobs.record_step_in_session(
            session,
            fixture.claim,
            step_name="FINAL_DECISION_DERIVED",
            input_sha256=outcome_sha256,
            output={
                "schema_version": "1.0.0",
                "outcome_event_id": outcome.event_id,
                "outcome_sha256": outcome_sha256,
                "run_id": result.run.run_id,
                "invocation_request_sha256": result.request_sha256,
                "provider_result_receipts": [
                    {
                        "receipt_id": provider_receipt.receipt_id,
                        "step_name": provider_receipt.step_name,
                        "output_sha256": provider_receipt.output_sha256,
                    }
                ],
                "decision": cast(dict[str, Any], json_value(decision)),
            },
        )
    return authority, outcome, decision
    """


async def _terminal_projection_state(fixture: _ExecutionFixture) -> dict[str, Any]:
    async with fixture.sessions() as session:
        command = await session.scalar(
            select(CommandRow).where(CommandRow.command_id == fixture.claim.command_id)
        )
        job = await session.scalar(
            select(WorkflowJobRow).where(WorkflowJobRow.job_id == fixture.claim.job_id)
        )
        learner = await session.scalar(
            select(LearnerProfileRow).where(LearnerProfileRow.tenant_id == fixture.claim.tenant_id)
        )
        run = await session.scalar(
            select(RunRow).where(RunRow.command_id == fixture.claim.command_id)
        )
        assert command is not None and job is not None and learner is not None and run is not None
        non_world_events = await session.scalar(
            select(func.count())
            .select_from(EventRow)
            .where(
                EventRow.tenant_id == fixture.claim.tenant_id,
                EventRow.stream_id != f"world:{fixture.world.world_id}",
            )
        )
        interactions = await session.scalar(
            select(func.count())
            .select_from(ProductInteractionRow)
            .where(ProductInteractionRow.tenant_id == fixture.claim.tenant_id)
        )
        projection_jobs = await session.scalar(
            select(func.count())
            .select_from(LearnerProjectionJobRow)
            .where(LearnerProjectionJobRow.tenant_id == fixture.claim.tenant_id)
        )
        evidence = await session.scalar(
            select(func.count())
            .select_from(EvidenceRow)
            .where(
                EvidenceRow.tenant_id == fixture.claim.tenant_id,
                EvidenceRow.command_id == fixture.claim.command_id,
            )
        )
        receipts = await session.scalar(
            select(func.count())
            .select_from(JobStepReceiptRow)
            .where(JobStepReceiptRow.job_id == fixture.claim.job_id)
        )
        workspace = await session.scalar(
            select(ProductWorkspaceRow).where(
                ProductWorkspaceRow.tenant_id == fixture.claim.tenant_id,
                ProductWorkspaceRow.session_id == fixture.request.session_id,
            )
        )
    assert workspace is not None
    profile = learner.profile_json
    run_wire = run.run_json
    workspace_wire = workspace.workspace_json
    checkpoint = workspace_wire["world_checkpoint"]
    draft_refs = workspace_wire["skill_draft_refs"]
    assert isinstance(checkpoint, dict)
    assert isinstance(draft_refs, list) and len(draft_refs) == 1
    draft_ref = draft_refs[0]
    assert isinstance(draft_ref, dict)
    return {
        "command_status": command.status,
        "command_terminal": command.terminal,
        "job_status": job.status,
        "learner_revision": profile.get("revision"),
        "run_has_feedback": run_wire.get("agent_feedback") is not None,
        "non_world_events": int(non_world_events or 0),
        "interactions": int(interactions or 0),
        "projection_jobs": int(projection_jobs or 0),
        "evidence": int(evidence or 0),
        "receipts": int(receipts or 0),
        "workspace_revision": workspace.workspace_revision,
        "workspace_world_revision": checkpoint["world_revision"],
        "workspace_event_sequence": checkpoint["last_event_sequence"],
        "workspace_state_hash": checkpoint["state_hash"],
        "workspace_interaction_sequence": workspace_wire["last_interaction_sequence"],
        "workspace_draft_revision": draft_ref["revision"],
    }


async def _seed_execution_skeletal(database_url: str) -> _ExecutionFixture:
    suffix = uuid4().hex[:20]
    tenant_id = f"tenant_durable_{suffix}"
    actor_id = f"student_durable_{suffix}"
    command_id = f"cmd_turn_durable_{suffix}"
    turn_id = f"turn_durable_{suffix}"
    session_id = f"session_durable_{suffix}"
    world_id = f"world_durable_{suffix}"
    invocation_id = side_effect_execution_id(command_id, turn_id)
    content_hash = hashlib.sha256(f"content:{suffix}".encode()).hexdigest()
    artifact_sha256 = hashlib.sha256(f"artifact:{suffix}".encode()).hexdigest()
    # Keep contract accepted_at strictly before PostgreSQL clock_timestamp()
    # even on Windows hosts with coarse or slightly skewed clock sources.
    now = datetime.now(UTC) - timedelta(seconds=1)
    actor = ActorRef(tenant_id, actor_id, ActorType.STUDENT, ("game:player",))
    content = ContentRef(f"UNIT_{suffix.upper()}", "1.0.0", content_hash)
    origin = RequestContext(
        request_id=f"req_{suffix}",
        correlation_id=f"corr_{suffix}",
        trace_id=f"trace_{suffix}",
        requested_at=now,
        actor=actor,
        content_ref=content,
    )
    context = OperationContext(
        request_id=origin.request_id,
        correlation_id=origin.correlation_id,
        trace_id=origin.trace_id,
        requested_at=origin.requested_at,
        actor=origin.actor,
        content_ref=origin.content_ref,
        command_id=command_id,
        causation_id=None,
    )
    skill_ref = SkillRef(
        skill_id=f"skill_{suffix}",
        skill_version_id=f"skillver_{suffix}",
        artifact_sha256=artifact_sha256,
        certification_id=f"cert_{suffix}",
    )
    turn_request = {
        "turn_id": turn_id,
        "expected_world_revision": 0,
        "input": {"type": "MESSAGE", "text": "move"},
        "skill_bindings": [
            {
                "skill_id": skill_ref.skill_id,
                "skill_version_id": skill_ref.skill_version_id,
                "artifact_sha256": skill_ref.artifact_sha256,
                "certification_id": skill_ref.certification_id,
            }
        ],
        "client_state": {"last_event_sequence": 0, "client_turn_sequence": 1},
    }
    accepted_request_sha256 = canonical_json_sha256(turn_request)
    versions = VersionSet(
        api_version="1.1.0",
        event_version="1.0.0",
        policy_version=f"policy_{suffix}",
        world_rules_version="rules-1",
        teaching_spec_version="agent-teaching-v1",
        skill_version=skill_ref.skill_version_id,
        artifact_sha256=skill_ref.artifact_sha256,
        compiler_version="gcc-14.2.0",
        sandbox_image_digest="sha256:" + "b" * 64,
        test_suite_version="test-suite-1",
        prompt_version="prompt-durable-v1",
        model_version="fake-model-v1",
    )
    command = CommandRecord(
        request_context=origin,
        command_id=command_id,
        command_type="EXECUTE_AGENT_TURN",
        status=CommandStatus.ACCEPTED,
        stage="ACCEPT",
        terminal=False,
        accepted_at=now,
        updated_at=now,
        result=None,
        error=None,
        evidence_refs=(),
        versions=versions,
        links=cast(FrozenJsonObject, {"self": f"/v1/commands/{command_id}"}),
    )
    state = cast(
        FrozenJsonObject,
        {
            "clock": {"day": 1, "minute_of_day": 0, "tick": 1},
            "avatar": {
                "entity_id": "avatar_turn_durable",
                "position": {"x": 1, "y": 1},
                "energy": 100,
            },
            "inventory": [],
            "plots": [],
            "agents": [],
        },
    )
    world = WorldSnapshot(
        request_context=origin,
        world_id=world_id,
        revision=0,
        last_event_sequence=0,
        state_hash=canonical_json_sha256(state),
        generated_at=now,
        world_rules_version="rules-1",
        state=state,
    )
    arguments = cast(FrozenJsonObject, {"move": "east"})
    request_sha256 = skill_invocation_request_sha256(
        tenant_id=tenant_id,
        invocation_id=invocation_id,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        world_id=world_id,
        expected_world_revision=0,
        skill_ref=skill_ref,
        arguments=arguments,
    )
    request = SkillInvocationRequest(
        invocation_id=invocation_id,
        tenant_id=tenant_id,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        world_id=world_id,
        expected_world_revision=0,
        skill_ref=skill_ref,
        arguments=arguments,
        request_sha256=request_sha256,
    )
    assert accepted_request_sha256 != request.request_sha256
    sessions = create_session_factory(database_url)
    jobs = PostgresWorkflowJobStore(sessions)
    async with sessions() as session, session.begin():
        session.add_all(
            [
                CommandRow(
                    command_id=command.command_id,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    command_type=command.command_type,
                    status=command.status.value,
                    revision=command.revision,
                    terminal=command.terminal,
                    accepted_at=now,
                    updated_at=now,
                    record_json=command_record_data(command),
                ),
                IdempotencyReceiptRow(
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    operation=command.command_type,
                    idempotency_key=f"turn-durable-{suffix}",
                    request_sha256=accepted_request_sha256,
                    command_id=command_id,
                    accepted_at=now,
                ),
                WorldSnapshotRow(
                    world_id=world.world_id,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    content_hash=content_hash,
                    revision=0,
                    last_event_sequence=0,
                    state_hash=world.state_hash,
                    generated_at=now,
                    snapshot_json=world_snapshot_data(world),
                ),
            ]
        )
        await session.flush()
        await jobs.enqueue_in_session(
            session,
            tenant_id=tenant_id,
            command_id=command_id,
            operation="EXECUTE_AGENT_TURN",
            subject_type="AGENT_TURN",
            subject_id=turn_id,
            request_sha256=accepted_request_sha256,
            job={
                "schema_version": "1.0.0",
                "turn_id": turn_id,
                "session_id": session_id,
                "turn_sequence": 1,
                "request": turn_request,
                "request_context": request_context_data(context),
            },
        )
    claim = await jobs.claim_next(
        tenant_id=tenant_id,
        worker_id=f"worker_{suffix}",
        lease_seconds=60,
        operation="EXECUTE_AGENT_TURN",
    )
    assert claim is not None
    return _ExecutionFixture(sessions, jobs, claim, context, request, world, versions)


async def _seed_execution(
    database_url: str,
    *,
    activation_clock_regression: bool = False,
    session_clock_regression: bool = False,
) -> _ExecutionFixture:
    """Create one Turn on a real certified Build and Activation authority chain."""

    suffix = uuid4().hex[:20]
    tenant_id = f"tenant_durable_{suffix}"
    actor_id = f"student_durable_{suffix}"
    world_id = f"world_durable_{suffix}"
    learner_id = actor_id
    agent_profile_id = f"agent_profile_{suffix}"
    authority_id = f"authority_{suffix}"
    policy_id = f"policy_{suffix}"
    content_hash = hashlib.sha256(f"content:{suffix}".encode()).hexdigest()
    actor = ActorRef(tenant_id, actor_id, ActorType.STUDENT, ("game:player",))
    content = ContentRef(f"UNIT_{suffix.upper()}", "1.0.0", content_hash)
    seeded_at = datetime.now(UTC) - timedelta(seconds=2)
    seed_context = RequestContext(
        request_id=f"req_seed_{suffix}",
        correlation_id=f"corr_seed_{suffix}",
        trace_id=f"trace_seed_{suffix}",
        requested_at=seeded_at,
        actor=actor,
        content_ref=content,
    )
    state = cast(
        FrozenJsonObject,
        {
            "clock": {"day": 1, "minute_of_day": 0, "tick": 1},
            "avatar": {
                "entity_id": "avatar_turn_durable",
                "position": {"x": 1, "y": 1},
                "energy": 100,
            },
            "inventory": [],
            "plots": [],
            "agents": [],
        },
    )
    world = WorldSnapshot(
        request_context=seed_context,
        world_id=world_id,
        revision=0,
        last_event_sequence=0,
        state_hash=canonical_json_sha256(state),
        generated_at=seeded_at,
        world_rules_version="rules-1",
        state=state,
    )
    source = "int main() { return 0; }\n"
    source_bundle = {
        "language": "CPP20",
        "entrypoint": "main.cpp",
        "files": [
            {
                "path": "main.cpp",
                "content": source,
                "content_sha256": hashlib.sha256(source.encode()).hexdigest(),
            }
        ],
    }
    starter = {
        "skill_id": f"skill_{suffix}",
        "display_name": "Durable execution skill",
        "source_bundle": source_bundle,
        "compiler_profile": "YAYA_CPP20_SAFE_V1",
        "test_suite_version": "test-suite-1",
    }
    task = {
        "task_id": f"task_{suffix}",
        "name": "Durable projection task",
        "goal": "Validate one durable projection.",
        "story": {"opening": "A deterministic integration fixture."},
        "knowledge_points": ["world_navigation"],
        "hint_policy": {"max_level": 4},
        "allowed_capabilities": ["WORLD_READ"],
        "starter_skill": starter,
    }
    image_digest = "sha256:" + "b" * 64
    parameter_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["move"],
        "properties": {"move": {"type": "string", "const": "east"}},
    }
    policy = {
        "schema_version": "1.0.0",
        "compiler_image": f"ghcr.io/yaya/student-cpp@{image_digest}",
        "compiler_profile": starter["compiler_profile"],
        "compiler_version": "gcc-14.2.0",
        "test_suite_version": starter["test_suite_version"],
        "compile_flags": list(CPP20_SAFE_V1_FLAGS),
        "public_tests": [
            {
                "test_case_id": "durability-public-1",
                "visibility": "PUBLIC",
                "arguments": [],
                "stdin_base64": "",
                "expected_stdout_sha256": None,
            }
        ],
        "hidden_tests": [
            {
                "test_case_id": "durability-hidden-1",
                "visibility": "HIDDEN",
                "arguments": [],
                "stdin_base64": "",
                "expected_stdout_sha256": None,
            }
        ],
        "limits": {
            "compile_wall_ms": 30_000,
            "test_wall_ms": 30_000,
            "memory_bytes": 268_435_456,
            "max_processes": 32,
            "cpu_millis": 1_000,
            "tmpfs_bytes": 67_108_864,
            "max_output_bytes": 1_048_576,
            "max_artifact_bytes": 16_777_216,
        },
        "parameter_schema": parameter_schema,
    }
    learner = {
        "schema_version": "1.0.0",
        "learner_id": learner_id,
        "actor_id": actor_id,
        "content": {
            "unit_id": content.unit_id,
            "version": content.version,
            "content_hash": content.content_hash,
        },
        "locale": "zh-CN",
        "revision": 0,
        "projected_through_sequence": 0,
        "model_version": LEARNER_PROJECTION_POLICY_VERSION,
        "review_policy_version": REVIEW_POLICY_VERSION,
        "competencies": {},
        "evidence_refs": [],
        "updated_at": seeded_at.isoformat().replace("+00:00", "Z"),
    }
    agent_profile = {
        "schema_version": "1.0.0",
        "agent_profile_id": agent_profile_id,
        "actor_id": actor_id,
        "content": {
            "unit_id": content.unit_id,
            "version": content.version,
            "content_hash": content.content_hash,
        },
        "role": "durability-test-tutor",
        "revision": 1,
        "provider": "fake-provider",
        "model_version": "fake-model-v1",
        "prompt_version": "prompt-durable-v1",
    }
    launch_wire = {
        "schema_version": "1.0.0",
        "authority_id": authority_id,
        "actor_id": actor_id,
        "content": {
            "unit_id": content.unit_id,
            "version": content.version,
            "content_hash": content.content_hash,
        },
        "world_id": world_id,
        "learner_id": learner_id,
        "agent_profile_id": agent_profile_id,
        "build_policy_id": policy_id,
        "channel": "GAME",
        "teaching_spec_version": "agent-teaching-v1",
        "active": True,
    }
    sessions = create_session_factory(database_url)
    commands = PostgresCommandStore(sessions)
    jobs = PostgresWorkflowJobStore(sessions)
    async with sessions() as session, session.begin():
        session.add_all(
            [
                ProductContentUnitRow(
                    tenant_id=tenant_id,
                    unit_id=content.unit_id,
                    version=content.version,
                    content_hash=content.content_hash,
                    audiences=["LEARNER"],
                    published_at=seeded_at,
                    content_json={
                        "content_ref": {
                            "unit_id": content.unit_id,
                            "version": content.version,
                            "content_hash": content.content_hash,
                        },
                        "status": "PUBLISHED",
                        "unit_type": "TASK",
                        "audiences": ["LEARNER"],
                        "published_at": seeded_at.isoformat().replace("+00:00", "Z"),
                        "task": task,
                    },
                ),
                WorldSnapshotRow(
                    world_id=world.world_id,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    content_hash=content_hash,
                    revision=0,
                    last_event_sequence=0,
                    state_hash=world.state_hash,
                    generated_at=seeded_at,
                    snapshot_json=world_snapshot_data(world),
                ),
                LearnerProfileRow(
                    tenant_id=tenant_id,
                    learner_id=learner_id,
                    actor_id=actor_id,
                    content_hash=content_hash,
                    profile_sha256=canonical_json_sha256(learner),
                    profile_json=learner,
                    created_at=seeded_at,
                    updated_at=seeded_at,
                ),
                AgentProfileRow(
                    tenant_id=tenant_id,
                    agent_profile_id=agent_profile_id,
                    actor_id=actor_id,
                    content_hash=content_hash,
                    profile_sha256=canonical_json_sha256(agent_profile),
                    profile_json=agent_profile,
                    created_at=seeded_at,
                ),
                BuildPolicyRow(
                    tenant_id=tenant_id,
                    build_policy_id=policy_id,
                    actor_id=actor_id,
                    content_hash=content_hash,
                    compiler_profile=cast(str, starter["compiler_profile"]),
                    compiler_version="gcc-14.2.0",
                    sandbox_image_digest=image_digest,
                    test_suite_version=cast(str, starter["test_suite_version"]),
                    allowed_capabilities=["WORLD_READ"],
                    max_source_files=32,
                    max_source_bytes=1_048_576,
                    policy_json=policy,
                    policy_sha256=canonical_json_sha256(policy),
                    active=True,
                    created_at=seeded_at,
                ),
            ]
        )
        await session.flush()
        session.add(
            LaunchAuthorityRow(
                tenant_id=tenant_id,
                authority_id=authority_id,
                actor_id=actor_id,
                content_unit_id=content.unit_id,
                content_version=content.version,
                content_hash=content_hash,
                world_id=world_id,
                learner_id=learner_id,
                agent_profile_id=agent_profile_id,
                build_policy_id=policy_id,
                channel="GAME",
                teaching_spec_version="agent-teaching-v1",
                authority_sha256=canonical_json_sha256(launch_wire),
                active=True,
                created_at=seeded_at,
            )
        )
        await session.flush()
        session.add(
            RegistryHeadRow(
                tenant_id=tenant_id,
                authority_id=authority_id,
                actor_id=actor_id,
                content_hash=content_hash,
                world_id=world_id,
                agent_profile_id=agent_profile_id,
                revision=0,
                updated_at=seeded_at,
            )
        )

    bootstrap_context = OperationContext(
        request_id=f"req_session_{suffix}",
        correlation_id=f"corr_session_{suffix}",
        trace_id=f"trace_session_{suffix}",
        requested_at=seeded_at,
        actor=actor,
        content_ref=content,
        command_id=f"cmd_transport_session_{suffix}",
        causation_id=None,
    )
    session_request = {
        "world_id": world_id,
        "learner_id": learner_id,
        "agent_profile_id": agent_profile_id,
        "channel": "GAME",
        "locale": "zh-CN",
        "content": {
            "unit_id": content.unit_id,
            "version": content.version,
            "content_hash": content.content_hash,
        },
        "expected_world_revision": 0,
    }
    accepted_session = await AgentSessions(
        PostgresAgentSessionStore(sessions, commands, jobs)
    ).accept(
        _canonical_json_bytes(session_request),
        f"idem_session_{suffix}",
        bootstrap_context,
    )
    assert isinstance(accepted_session, Success), accepted_session
    session_resource, session_receipt = accepted_session.value
    session_id = cast(str, session_resource["session_id"])
    session_claim = await jobs.claim_next(
        tenant_id=tenant_id,
        worker_id=f"worker_session_{suffix}",
        lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
        operation="CREATE_AGENT_SESSION",
    )
    assert session_claim is not None
    assert session_claim.command_id == session_receipt.command.command_id
    session_handler = ControlWorkflowHandler(
        sessions,
        commands,
        jobs,
        lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
    )
    if session_clock_regression:
        accepted_at = session_receipt.command.accepted_at
        with (
            patch(
                "walnut_backend.workers.control_worker._database_now",
                side_effect=(
                    accepted_at - timedelta(seconds=2),
                    accepted_at - timedelta(seconds=1),
                ),
            ),
            patch(
                "walnut_backend.adapters.postgres.workflow_jobs._database_now",
                side_effect=(
                    accepted_at - timedelta(seconds=3),
                    accepted_at + timedelta(seconds=30),
                    accepted_at - timedelta(seconds=4),
                ),
            ),
        ):
            await session_handler.execute(session_claim)
    else:
        await session_handler.execute(session_claim)

    build_context = replace(
        bootstrap_context,
        request_id=f"req_build_{suffix}",
        correlation_id=f"corr_build_{suffix}",
        trace_id=f"trace_build_{suffix}",
        command_id=f"cmd_transport_build_{suffix}",
    )
    build_request = {
        "skill_id": starter["skill_id"],
        "display_name": starter["display_name"],
        "client_draft_revision": 1,
        "source_bundle": source_bundle,
        "compiler_profile": starter["compiler_profile"],
        "test_suite_version": starter["test_suite_version"],
        "requested_capabilities": ["WORLD_READ"],
    }
    accepted_build = await SkillBuildCommands(
        PostgresSkillBuildStore(sessions, commands, jobs)
    ).accept(
        _canonical_json_bytes(build_request),
        f"idem_build_{suffix}",
        build_context,
    )
    assert isinstance(accepted_build, Success), accepted_build
    build_resource, build_receipt = accepted_build.value
    build_id = cast(str, build_resource["build_id"])
    build_claim = await jobs.claim_next(
        tenant_id=tenant_id,
        worker_id=f"worker_build_{suffix}",
        lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
        operation="CREATE_SKILL_BUILD",
    )
    assert build_claim is not None
    assert build_claim.command_id == build_receipt.command.command_id
    artifact_bytes = f"artifact:{suffix}".encode()
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    source_sha256 = canonical_source_bundle_sha256(source_bundle)
    with TemporaryDirectory(prefix=f"walnut-durability-{suffix}-") as temporary:
        temporary_root = Path(temporary)
        workspace_root = temporary_root / "workspaces"
        artifact_root = temporary_root / "artifacts"
        build_workspace = workspace_root / build_id
        workspace_root.mkdir()
        artifact_root.mkdir()
        build_workspace.mkdir()
        staged_artifact = build_workspace / "skill.bin"
        staged_artifact.write_bytes(artifact_bytes)
        docker_result = DockerBuildResult(
            build_id=build_id,
            status="SUCCEEDED",
            source_sha256=source_sha256,
            compiler_profile="YAYA_CPP20_SAFE_V1",
            compiler_version="gcc-14.2.0",
            test_suite_version="test-suite-1",
            build_identity=hashlib.sha256(f"identity:{suffix}".encode()).hexdigest(),
            workspace=build_workspace,
            staged_artifact=staged_artifact,
            artifact_sha256=artifact_sha256,
            tests=(),
            diagnostics=(),
            failure=None,
        )

        def fake_build(
            _builder: DigestPinnedDockerCppBuilder, request_value: object
        ) -> DockerBuildResult:
            assert getattr(request_value, "build_id") == build_id
            return docker_result

        with patch.object(DigestPinnedDockerCppBuilder, "build", fake_build):
            await BuildWorkflowHandler(
                session_factory=sessions,
                command_store=commands,
                workflow_jobs=jobs,
                workspace_root=workspace_root,
                artifact_root=artifact_root,
                lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
            ).execute(build_claim)

    async with sessions() as session:
        certification = await session.scalar(
            select(SkillCertificationRow).where(
                SkillCertificationRow.tenant_id == tenant_id,
                SkillCertificationRow.build_id == build_id,
            )
        )
    assert certification is not None
    skill_ref = SkillRef(
        skill_id=certification.skill_id,
        skill_version_id=certification.skill_version_id,
        artifact_sha256=certification.artifact_sha256,
        certification_id=certification.certification_id,
    )
    activation_context = replace(
        bootstrap_context,
        request_id=f"req_activation_{suffix}",
        correlation_id=f"corr_activation_{suffix}",
        trace_id=f"trace_activation_{suffix}",
        command_id=f"cmd_transport_activation_{suffix}",
    )
    activation_request = {
        "expected_registry_revision": 0,
        "activation_scope": {
            "world_id": world_id,
            "agent_profile_id": agent_profile_id,
        },
        "reason": "durability fixture activation",
    }
    accepted_activation = await SkillActivations(
        PostgresSkillActivationStore(sessions, commands, jobs)
    ).accept(
        skill_ref.skill_version_id,
        _canonical_json_bytes(activation_request),
        f"idem_activation_{suffix}",
        activation_context,
    )
    assert isinstance(accepted_activation, Success), accepted_activation
    activation_id, activation_receipt = accepted_activation.value
    activation_claim = await jobs.claim_next(
        tenant_id=tenant_id,
        worker_id=f"worker_activation_{suffix}",
        lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
        operation="ACTIVATE_SKILL_VERSION",
    )
    assert activation_claim is not None
    assert activation_claim.command_id == activation_receipt.command.command_id
    assert activation_claim.subject_id == activation_id
    activation_handler = ControlWorkflowHandler(
        sessions,
        commands,
        jobs,
        lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
    )
    if activation_clock_regression:
        accepted_at = activation_receipt.command.accepted_at
        with (
            patch(
                "walnut_backend.workers.control_worker._database_now",
                side_effect=(
                    accepted_at - timedelta(seconds=2),
                    accepted_at - timedelta(seconds=1),
                ),
            ),
            patch(
                "walnut_backend.adapters.postgres.workflow_jobs._database_now",
                side_effect=(
                    accepted_at - timedelta(seconds=3),
                    accepted_at + timedelta(seconds=30),
                    accepted_at - timedelta(seconds=4),
                ),
            ),
        ):
            await activation_handler.execute(activation_claim)
    else:
        await activation_handler.execute(activation_claim)

    async with sessions() as session:
        database_now = await session.scalar(select(func.clock_timestamp()))
    assert isinstance(database_now, datetime) and database_now.tzinfo is not None
    command_id = f"cmd_turn_durable_{suffix}"
    turn_id = f"turn_durable_{suffix}"
    origin = RequestContext(
        request_id=f"req_{suffix}",
        correlation_id=f"corr_{suffix}",
        trace_id=f"trace_{suffix}",
        requested_at=database_now,
        actor=actor,
        content_ref=content,
    )
    context = OperationContext(
        request_id=origin.request_id,
        correlation_id=origin.correlation_id,
        trace_id=origin.trace_id,
        requested_at=origin.requested_at,
        actor=origin.actor,
        content_ref=origin.content_ref,
        command_id=command_id,
        causation_id=None,
    )
    versions = VersionSet(
        api_version="1.1.0",
        event_version="1.0.0",
        policy_version=policy_id,
        world_rules_version="rules-1",
        teaching_spec_version="agent-teaching-v1",
        skill_version=skill_ref.skill_version_id,
        artifact_sha256=skill_ref.artifact_sha256,
        compiler_version="gcc-14.2.0",
        sandbox_image_digest=image_digest,
        test_suite_version="test-suite-1",
        prompt_version="prompt-durable-v1",
        model_version="fake-model-v1",
    )
    turn_request = {
        "turn_id": turn_id,
        "expected_world_revision": 0,
        "input": {"type": "MESSAGE", "text": "move"},
        "skill_bindings": [
            {
                "skill_id": skill_ref.skill_id,
                "skill_version_id": skill_ref.skill_version_id,
                "artifact_sha256": skill_ref.artifact_sha256,
                "certification_id": skill_ref.certification_id,
            }
        ],
        "client_state": {"last_event_sequence": 0, "client_turn_sequence": 1},
    }
    accepted_request_sha256 = canonical_json_sha256(turn_request)
    command = CommandRecord(
        request_context=origin,
        command_id=command_id,
        command_type="EXECUTE_AGENT_TURN",
        status=CommandStatus.ACCEPTED,
        stage="ACCEPT",
        terminal=False,
        accepted_at=database_now,
        updated_at=database_now,
        result=None,
        error=None,
        evidence_refs=(),
        versions=versions,
        links=cast(FrozenJsonObject, {"self": f"/v1/commands/{command_id}"}),
    )
    invocation_id = side_effect_execution_id(command_id, turn_id)
    arguments = cast(FrozenJsonObject, {"move": "east"})
    request_sha256 = skill_invocation_request_sha256(
        tenant_id=tenant_id,
        invocation_id=invocation_id,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        world_id=world_id,
        expected_world_revision=0,
        skill_ref=skill_ref,
        arguments=arguments,
    )
    request = SkillInvocationRequest(
        invocation_id=invocation_id,
        tenant_id=tenant_id,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        world_id=world_id,
        expected_world_revision=0,
        skill_ref=skill_ref,
        arguments=arguments,
        request_sha256=request_sha256,
    )
    async with sessions() as session, session.begin():
        session.add_all(
            [
                CommandRow(
                    command_id=command.command_id,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    command_type=command.command_type,
                    status=command.status.value,
                    revision=command.revision,
                    terminal=command.terminal,
                    accepted_at=database_now,
                    updated_at=database_now,
                    record_json=command_record_data(command),
                ),
                IdempotencyReceiptRow(
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    operation=command.command_type,
                    idempotency_key=f"turn-durable-{suffix}",
                    request_sha256=accepted_request_sha256,
                    command_id=command_id,
                    accepted_at=database_now,
                ),
                AgentTurnRow(
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    command_id=command_id,
                    turn_sequence=1,
                    created_at=database_now,
                    request_json=turn_request,
                ),
            ]
        )
        await session.flush()
        await jobs.enqueue_in_session(
            session,
            tenant_id=tenant_id,
            command_id=command_id,
            operation="EXECUTE_AGENT_TURN",
            subject_type="AGENT_TURN",
            subject_id=turn_id,
            request_sha256=accepted_request_sha256,
            job={
                "schema_version": "1.0.0",
                "turn_id": turn_id,
                "session_id": session_id,
                "turn_sequence": 1,
                "request": turn_request,
                "request_context": request_context_data(context),
            },
        )
    claim = await jobs.claim_next(
        tenant_id=tenant_id,
        worker_id=f"worker_{suffix}",
        lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
        operation="EXECUTE_AGENT_TURN",
    )
    assert claim is not None
    return _ExecutionFixture(sessions, jobs, claim, context, request, world, versions)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


async def _seed_followup_execution(
    base: _ExecutionFixture,
    *,
    sequence: int,
    expected_world_revision: int,
) -> _ExecutionFixture:
    suffix = uuid4().hex[:20]
    async with base.sessions() as chronology_session:
        current_workspace_updated_at = await chronology_session.scalar(
            select(ProductWorkspaceRow.updated_at).where(
                ProductWorkspaceRow.tenant_id == base.claim.tenant_id,
                ProductWorkspaceRow.actor_id == base.context.actor.actor_id,
                ProductWorkspaceRow.session_id == base.request.session_id,
            )
        )
    assert current_workspace_updated_at is not None
    now = max(
        datetime.now(UTC) - timedelta(seconds=1),
        current_workspace_updated_at + timedelta(microseconds=1),
    )
    command_id = f"cmd_turn_durable_{suffix}"
    turn_id = f"turn_durable_{suffix}"
    origin = RequestContext(
        request_id=f"req_{suffix}",
        correlation_id=f"corr_{suffix}",
        trace_id=f"trace_{suffix}",
        requested_at=now,
        actor=base.context.actor,
        content_ref=base.context.content_ref,
    )
    context = OperationContext(
        request_id=origin.request_id,
        correlation_id=origin.correlation_id,
        trace_id=origin.trace_id,
        requested_at=origin.requested_at,
        actor=origin.actor,
        content_ref=origin.content_ref,
        command_id=command_id,
        causation_id=None,
    )
    turn_request = {
        "turn_id": turn_id,
        "expected_world_revision": expected_world_revision,
        "input": {"type": "MESSAGE", "text": f"move-{sequence}"},
        "skill_bindings": [
            {
                "skill_id": base.request.skill_ref.skill_id,
                "skill_version_id": base.request.skill_ref.skill_version_id,
                "artifact_sha256": base.request.skill_ref.artifact_sha256,
                "certification_id": base.request.skill_ref.certification_id,
            }
        ],
        "client_state": {
            "last_event_sequence": base.world.last_event_sequence,
            "client_turn_sequence": sequence,
        },
    }
    accepted_sha256 = canonical_json_sha256(turn_request)
    command = CommandRecord(
        request_context=origin,
        command_id=command_id,
        command_type="EXECUTE_AGENT_TURN",
        status=CommandStatus.ACCEPTED,
        stage="ACCEPT",
        terminal=False,
        accepted_at=now,
        updated_at=now,
        result=None,
        error=None,
        evidence_refs=(),
        versions=base.versions,
        links=cast(FrozenJsonObject, {"self": f"/v1/commands/{command_id}"}),
    )
    invocation_id = side_effect_execution_id(command_id, turn_id)
    arguments = cast(FrozenJsonObject, {"move": "east"})
    request_sha256 = skill_invocation_request_sha256(
        tenant_id=base.claim.tenant_id,
        invocation_id=invocation_id,
        session_id=base.request.session_id,
        turn_id=turn_id,
        command_id=command_id,
        world_id=base.request.world_id,
        expected_world_revision=expected_world_revision,
        skill_ref=base.request.skill_ref,
        arguments=arguments,
    )
    request = SkillInvocationRequest(
        invocation_id=invocation_id,
        tenant_id=base.claim.tenant_id,
        session_id=base.request.session_id,
        turn_id=turn_id,
        command_id=command_id,
        world_id=base.request.world_id,
        expected_world_revision=expected_world_revision,
        skill_ref=base.request.skill_ref,
        arguments=arguments,
        request_sha256=request_sha256,
    )
    async with base.sessions() as session, session.begin():
        snapshot = await session.scalar(
            select(WorldSnapshotRow).where(
                WorldSnapshotRow.tenant_id == base.claim.tenant_id,
                WorldSnapshotRow.world_id == base.request.world_id,
            )
        )
        assert snapshot is not None
        world = world_snapshot_from_data(snapshot.snapshot_json)
        session.add_all(
            [
                CommandRow(
                    command_id=command_id,
                    tenant_id=base.claim.tenant_id,
                    actor_id=base.context.actor.actor_id,
                    command_type=command.command_type,
                    status=command.status.value,
                    revision=command.revision,
                    terminal=False,
                    accepted_at=now,
                    updated_at=now,
                    record_json=command_record_data(command),
                ),
                IdempotencyReceiptRow(
                    tenant_id=base.claim.tenant_id,
                    actor_id=base.context.actor.actor_id,
                    operation=command.command_type,
                    idempotency_key=f"turn-durable-{suffix}",
                    request_sha256=accepted_sha256,
                    command_id=command_id,
                    accepted_at=now,
                ),
                AgentTurnRow(
                    tenant_id=base.claim.tenant_id,
                    actor_id=base.context.actor.actor_id,
                    session_id=base.request.session_id,
                    turn_id=turn_id,
                    command_id=command_id,
                    turn_sequence=sequence,
                    created_at=now,
                    request_json=turn_request,
                ),
            ]
        )
        await session.flush()
        # Production accepts each follow-up Turn through PostgresAgentTurnStore,
        # which advances the mutable Product workspace before the Turn worker
        # validates the preceding terminal learner projection.  Keep this
        # fixture on that exact chronology so historical receipt replay cannot
        # accidentally require the old workspace snapshot to remain current.
        owner = await session.scalar(
            select(AgentSessionRow)
            .where(
                AgentSessionRow.tenant_id == base.claim.tenant_id,
                AgentSessionRow.actor_id == base.context.actor.actor_id,
                AgentSessionRow.session_id == base.request.session_id,
            )
            .with_for_update()
        )
        assert owner is not None
        owner_session = copy.deepcopy(owner.session_json)
        owner_session["last_turn_sequence"] = sequence
        owner_session["updated_at"] = now.isoformat().replace("+00:00", "Z")
        owner.session_json = owner_session
        owner.updated_at = now
        await refresh_workspace_in_session(
            session,
            tenant_id=base.claim.tenant_id,
            actor_id=base.context.actor.actor_id,
            session_id=base.request.session_id,
            updated_at=now,
        )
        await base.jobs.enqueue_in_session(
            session,
            tenant_id=base.claim.tenant_id,
            command_id=command_id,
            operation="EXECUTE_AGENT_TURN",
            subject_type="AGENT_TURN",
            subject_id=turn_id,
            request_sha256=accepted_sha256,
            job={
                "schema_version": "1.0.0",
                "turn_id": turn_id,
                "session_id": base.request.session_id,
                "turn_sequence": sequence,
                "request": turn_request,
                "request_context": request_context_data(context),
            },
        )
    claim = await base.jobs.claim_next(
        tenant_id=base.claim.tenant_id,
        worker_id=f"worker_{suffix}",
        lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
        operation="EXECUTE_AGENT_TURN",
    )
    assert claim is not None and claim.command_id == command_id
    return _ExecutionFixture(
        base.sessions,
        base.jobs,
        claim,
        context,
        request,
        world,
        base.versions,
    )


async def _seed_followup_projection_authority(
    fixture: _ExecutionFixture,
    result: Any,
    *,
    failure_count: int,
) -> tuple[_TurnAuthority, GameEvent, AgentDecision]:
    async with fixture.sessions() as session:
        owner = await session.scalar(
            select(AgentSessionRow).where(
                AgentSessionRow.tenant_id == fixture.claim.tenant_id,
                AgentSessionRow.session_id == fixture.request.session_id,
            )
        )
        content = await session.scalar(
            select(ProductContentUnitRow).where(
                ProductContentUnitRow.tenant_id == fixture.claim.tenant_id,
                ProductContentUnitRow.unit_id == fixture.context.content_ref.unit_id,
                ProductContentUnitRow.version == fixture.context.content_ref.version,
            )
        )
    assert owner is not None and content is not None
    task = cast(dict[str, Any], content.content_json["task"])
    learner_id = cast(str, owner.session_json["learner_id"])
    return await _build_terminal_projection_authority(
        fixture,
        result,
        learner_id=learner_id,
        task=task,
        failure_count=failure_count,
        record_final_authority=True,
    )


async def _refresh_execution_claim_after_host_pause(
    fixture: _ExecutionFixture,
    *,
    require_expired: bool = False,
) -> None:
    stale = fixture.claim
    async with fixture.sessions() as session:
        database_now = await session.scalar(select(func.clock_timestamp()))
        current = await session.scalar(
            select(WorkflowJobRow).where(
                WorkflowJobRow.tenant_id == stale.tenant_id,
                WorkflowJobRow.job_id == stale.job_id,
            )
        )
    assert isinstance(database_now, datetime) and database_now.tzinfo is not None
    assert current is not None
    still_owned = (
        current.fencing_token == stale.fencing_token
        and current.lease_owner == stale.lease_owner
        and current.lease_expires_at is not None
        and current.lease_expires_at > database_now
    )
    if still_owned:
        if require_expired:
            raise WorkflowFenceLost("workflow fence changed without an expired recoverable claim")
        return
    reclaimed = await fixture.jobs.claim_next(
        tenant_id=stale.tenant_id,
        worker_id=f"worker_resume_{uuid4().hex[:20]}",
        lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
        operation=stale.operation,
    )
    if reclaimed is None or reclaimed.job_id != stale.job_id:
        raise WorkflowFenceLost("expired test workflow could not be reclaimed")
    assert reclaimed.fencing_token > stale.fencing_token
    with pytest.raises(WorkflowFenceLost):
        await fixture.jobs.renew(stale, lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS)
    fixture.claim = reclaimed


def _invocation(
    fixture: _ExecutionFixture,
    sandbox: Any,
    *,
    rules: WorldRules | None = None,
) -> _LeaseResilientInvocation:
    return _LeaseResilientInvocation(fixture, sandbox, rules)


def _raw_invocation(
    fixture: _ExecutionFixture,
    sandbox: Any,
    *,
    rules: WorldRules | None = None,
) -> PostgresFencedSkillInvocation:
    effective_rules = _ruleset() if rules is None else rules
    engine = WorldEngine()
    return PostgresFencedSkillInvocation(
        session_factory=fixture.sessions,
        commands=PostgresCommandStore(fixture.sessions),
        jobs=fixture.jobs,
        claim=fixture.claim,
        sandbox=sandbox,
        limits=SandboxLimits(
            cpu_ms=1000,
            wall_ms=1000,
            memory_bytes=64 * 1024 * 1024,
            max_intents=4,
            max_output_bytes=4096,
            max_processes=4,
        ),
        versions=fixture.versions,
        world_uow=PostgresWorldUnitOfWork(
            fixture.sessions, {"rules-1": effective_rules}, world_engine=engine
        ),
        world_engine=engine,
        rules_by_version={"rules-1": effective_rules},
        lease_seconds=_TEST_WORKFLOW_LEASE_SECONDS,
    )


def _patched_authority_loader(fixture: _ExecutionFixture) -> Any:
    del fixture
    return nullcontext()


def _llm_request(versions: VersionSet) -> LlmRequest:
    return LlmRequest(
        messages=(LlmMessage("user", "invoke the exact bound skill"),),
        output_schema=cast(FrozenJsonObject, {"type": "object"}),
        temperature=0.0,
        max_output_tokens=128,
        timeout_ms=1000,
        versions=versions,
    )


def _ruleset() -> WorldRules:
    return WorldRules(
        content_version="1.0.0",
        max_actions=4,
        min_x=0,
        max_x=4,
        min_y=0,
        max_y=4,
        harvest_growth_stage=2,
        success_score=0,
    )


async def _step_names(fixture: _ExecutionFixture) -> list[str]:
    async with fixture.sessions() as session:
        values = await session.scalars(
            select(JobStepReceiptRow.step_name)
            .where(JobStepReceiptRow.job_id == fixture.claim.job_id)
            .order_by(JobStepReceiptRow.completed_at, JobStepReceiptRow.step_name)
        )
        return list(values)


async def _projection_state(fixture: _ExecutionFixture) -> tuple[int, int, int, int]:
    async with fixture.sessions() as session:
        revision = await session.scalar(
            select(WorldSnapshotRow.revision).where(
                WorldSnapshotRow.tenant_id == fixture.claim.tenant_id,
                WorldSnapshotRow.world_id == fixture.world.world_id,
            )
        )
        events = await session.scalar(
            select(func.count())
            .select_from(EventRow)
            .where(
                EventRow.tenant_id == fixture.claim.tenant_id,
                EventRow.stream_id == f"world:{fixture.world.world_id}",
            )
        )
        runs = await session.scalar(
            select(func.count())
            .select_from(RunRow)
            .where(
                RunRow.tenant_id == fixture.claim.tenant_id,
                RunRow.command_id == fixture.claim.command_id,
            )
        )
        evidence = await session.scalar(
            select(func.count())
            .select_from(EvidenceRow)
            .where(
                EvidenceRow.tenant_id == fixture.claim.tenant_id,
                EvidenceRow.command_id == fixture.claim.command_id,
            )
        )
    assert revision is not None
    return int(revision), int(events or 0), int(runs or 0), int(evidence or 0)


async def _runtime_side_effect_counts(fixture: _ExecutionFixture) -> tuple[int, int, int]:
    """Count the one-shot runtime effects after terminal turn projection."""

    async with fixture.sessions() as session:
        world_events = await session.scalar(
            select(func.count())
            .select_from(EventRow)
            .where(
                EventRow.tenant_id == fixture.claim.tenant_id,
                EventRow.stream_id == f"world:{fixture.world.world_id}",
            )
        )
        runs = await session.scalar(
            select(func.count())
            .select_from(RunRow)
            .where(
                RunRow.tenant_id == fixture.claim.tenant_id,
                RunRow.command_id == fixture.claim.command_id,
            )
        )
        interactions = await session.scalar(
            select(func.count())
            .select_from(ProductInteractionRow)
            .where(ProductInteractionRow.tenant_id == fixture.claim.tenant_id)
        )
    return int(world_events or 0), int(runs or 0), int(interactions or 0)


async def _dispose(sessions: async_sessionmaker[AsyncSession]) -> None:
    await sessions.kw["bind"].dispose()
