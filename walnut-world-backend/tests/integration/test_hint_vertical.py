"""Real PostgreSQL vertical for the no-Run teaching hint Turn.

A hint asks the teaching roles to explain the student's current situation.  It
must reach a terminal ``APPLIED`` Command and one readable AgentInteraction
without producing a Run, Evidence, a learner projection or any World event.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    SandboxLimits,
    Success,
)
from yaya_agent_runtime import ContextBuilder

from tests.integration.test_int2_patch_vertical import (
    _claim_patch_turn,
    _execute_successful_run,
    _FailedRunSandbox,
    _failure_projection_authority,
    _finish_and_project,
    _patch_projection_inputs,
    _prepare_failed_run,
    _prepare_harvest_world,
)
from tests.integration.test_terminal_read_closure import (
    _activate_and_read_skill,
    _database_url,
    _execute_build,
    _portal_call,
    _TerminalBuild,
)
from tests.integration.test_turn_execution_durability import (
    _ReplyProvider,
    _successful_provider_resource,
)
from walnut_backend.adapters.postgres.models import (
    CommandRow,
    EvidenceRow,
    LearnerProjectionJobRow,
    RunRow,
    WorkflowJobRow,
    WorldPresentationEventRow,
    command_record_from_data,
)
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings
from walnut_backend.domain.world.rules import WorldRules
from walnut_backend.workers import turn_projection
from walnut_backend.workers.turn_worker import TurnWorkflowHandler
from walnut_backend.workers.workflow_worker import WorkflowWorker

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class _HintReplyProvider(_ReplyProvider):
    """Return one non-degraded teaching question with no Patch and no tools."""

    def __init__(self, response_type: str = "question") -> None:
        super().__init__()
        self.response_type = response_type

    async def dispatch(self, identity: Any, request: Any, context: Any) -> Any:
        del request, context
        self.calls += 1
        resource = _successful_provider_resource(identity)
        reply = cast(Any, resource.result).value
        output = {
            "kind": "decision",
            "decision": {
                "role": "teaching_agent",
                "response_type": "question",
                "message": "先看看你现在给作物浇水的判断条件。",
                "question": "你觉得什么时候才应该浇水呢？",
                "hint_level": None,
                "learner_inference": None,
                "skill_patch": None,
                "requires_student_confirmation": False,
            },
            "tool_calls": [],
        }
        if self.response_type == "message":
            output["decision"].update(
                response_type="message",
                message="我是叮当师傅，可以和你聊天，也可以陪你学编程。",
                question=None,
            )
        patched = replace(resource, result=Success(replace(reply, output=output)))
        self.resources[identity.dispatch_id] = patched
        return patched


class _HintToolThenDecisionProvider(_ReplyProvider):
    """Take one read-only tool round before deciding, as a live model may."""

    def __init__(self) -> None:
        super().__init__()
        self.rounds = 0

    async def dispatch(self, identity: Any, request: Any, context: Any) -> Any:
        del request, context
        self.calls += 1
        self.rounds += 1
        resource = _successful_provider_resource(identity)
        reply = cast(Any, resource.result).value
        if self.rounds == 1:
            output: dict[str, Any] = {
                "kind": "tool_calls",
                "decision": None,
                "tool_calls": [
                    {
                        "call_id": "call_hint_vertical_0001",
                        "name": "get_current_task",
                        "arguments": {},
                    }
                ],
            }
        else:
            output = {
                "kind": "decision",
                "decision": {
                    "role": "teaching_agent",
                    "response_type": "question",
                    "message": "我看过这一关的目标了，再看看你的判断条件。",
                    "question": "什么情况下这块地才需要水？",
                    "hint_level": None,
                    "learner_inference": None,
                    "skill_patch": None,
                    "requires_student_confirmation": False,
                },
                "tool_calls": [],
            }
        patched = replace(resource, result=Success(replace(reply, output=output)))
        self.resources[identity.dispatch_id] = patched
        return patched


class _SuccessfulRunHintProvider(_HintReplyProvider):
    """Answer from the committed Run while its separate Book work is paused."""

    def __init__(self) -> None:
        super().__init__("message")
        self.contexts: list[dict[str, Any]] = []

    async def dispatch(self, identity: Any, request: Any, context: Any) -> Any:
        self.contexts.append(json.loads(request.messages[1].content)["turn_context"])
        resource = await super().dispatch(identity, request, context)
        reply = cast(Any, resource.result).value
        output = dict(reply.output)
        output["decision"] = {
            **output["decision"],
            "message": "任务已完成，刚才的运行已经成功。你想了解这段代码的哪一步？",
        }
        patched = replace(resource, result=Success(replace(reply, output=output)))
        self.resources[identity.dispatch_id] = patched
        return patched


class _PendingSuccessfulRunHintProvider(_SuccessfulRunHintProvider):
    async def dispatch(self, identity: Any, request: Any, context: Any) -> Any:
        resource = await super().dispatch(identity, request, context)
        return replace(
            resource,
            state="PENDING",
            result=None,
            raw_response_sha256=None,
            retry_after_seconds=1,
        )


@contextmanager
def _query_metrics() -> Iterator[dict[str, int | float]]:
    metrics: dict[str, int | float] = {"sql": 0}

    def count_query(*_args: Any) -> None:
        metrics["sql"] += 1

    event.listen(Engine, "before_cursor_execute", count_query)
    started = time.monotonic()
    try:
        yield metrics
    finally:
        metrics["seconds"] = round(time.monotonic() - started, 3)
        event.remove(Engine, "before_cursor_execute", count_query)


def _settings(database_url: str) -> Settings:
    return replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )


def _student_operation(client: TestClient, terminal: _TerminalBuild) -> Any:
    _, operation = _activate_and_read_skill(client, terminal)
    return replace(
        operation,
        actor=ActorRef(
            terminal.tenant_id,
            terminal.actor_id,
            ActorType.STUDENT,
            ("game:player",),
        ),
    )


def _hint_payload(
    *,
    revision: int,
    sequence: int,
    turn_sequence: int,
    turn_id: str,
    skill_bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "turn_id": turn_id,
        "expected_world_revision": revision,
        "input": {
            "type": "MESSAGE",
            "text": "请根据我当前的代码给出下一层教学提示。",
            "locale": "zh-CN",
        },
        "skill_bindings": skill_bindings,
        "client_state": {
            "last_event_sequence": sequence,
            "client_turn_sequence": turn_sequence,
        },
    }


def _run_hint_turn(
    client: TestClient,
    terminal: _TerminalBuild,
    operation: Any,
    accepted: Any,
    provider: Any | None = None,
) -> None:
    app = cast(Any, client.app)
    claim = _portal_call(client, _claim_patch_turn, app.state.workflow_jobs, terminal.tenant_id)
    assert claim is not None
    handler = _hint_handler(client, terminal, accepted, provider)
    del operation
    _portal_call(client, handler.execute, claim)


def _hint_handler(
    client: TestClient,
    terminal: _TerminalBuild,
    accepted: Any,
    provider: Any | None,
) -> TurnWorkflowHandler:
    app = cast(Any, client.app)
    versions = accepted.value.command.versions
    return TurnWorkflowHandler(
        session_factory=terminal.sessions,
        commands=app.state.game_queries._command_store,
        jobs=app.state.workflow_jobs,
        provider=provider or _HintReplyProvider(),
        sandbox=_FailedRunSandbox(),
        limits=SandboxLimits(
            cpu_ms=1_000,
            wall_ms=1_000,
            memory_bytes=64 * 1024 * 1024,
            max_intents=4,
            max_output_bytes=4_096,
            max_processes=4,
        ),
        versions=versions,
        rules_by_version={versions.world_rules_version: WorldRules("1.0.0", 4, 0, 10, 0, 10, 2, 0)},
        provider_name="fake-provider",
        model_version=cast(str, versions.model_version),
        prompt_version=cast(str, versions.prompt_version),
        sandbox_image_digest=cast(str, versions.sandbox_image_digest),
        skill_patch_enabled=True,
        lease_seconds=600,
    )


async def _hint_side_effects(
    terminal: _TerminalBuild, command_id: str, turn_id: str
) -> dict[str, Any]:
    async with terminal.sessions() as session:
        command_row = await session.scalar(
            select(CommandRow).where(
                CommandRow.tenant_id == terminal.tenant_id,
                CommandRow.command_id == command_id,
            )
        )
        job = await session.scalar(
            select(WorkflowJobRow).where(
                WorkflowJobRow.tenant_id == terminal.tenant_id,
                WorkflowJobRow.command_id == command_id,
                WorkflowJobRow.subject_type == "AGENT_TURN",
                WorkflowJobRow.subject_id == turn_id,
            )
        )
        runs = list(
            (
                await session.scalars(
                    select(RunRow).where(
                        RunRow.tenant_id == terminal.tenant_id,
                        RunRow.command_id == command_id,
                    )
                )
            ).all()
        )
        evidence = list(
            (
                await session.scalars(
                    select(EvidenceRow).where(
                        EvidenceRow.tenant_id == terminal.tenant_id,
                        EvidenceRow.command_id == command_id,
                    )
                )
            ).all()
        )
        learner_jobs = list(
            (
                await session.scalars(
                    select(LearnerProjectionJobRow).where(
                        LearnerProjectionJobRow.tenant_id == terminal.tenant_id,
                        LearnerProjectionJobRow.command_id == command_id,
                    )
                )
            ).all()
        )
        world_events = list(
            (
                await session.scalars(
                    select(WorldPresentationEventRow).where(
                        WorldPresentationEventRow.tenant_id == terminal.tenant_id,
                        WorldPresentationEventRow.command_id == command_id,
                    )
                )
            ).all()
        )
        assert command_row is not None
        assert job is not None
        return {
            "command": command_record_from_data(command_row.record_json),
            "job_status": job.status,
            "job_phase": job.phase,
            "runs": len(runs),
            "evidence": len(evidence),
            "learner_jobs": len(learner_jobs),
            "world_events": len(world_events),
        }


@pytest.mark.parametrize("response_type", ["question", "message"])
def test_hint_turn_applies_without_run_evidence_or_world_change(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    response_type: str,
) -> None:
    database_url = _database_url()
    with TestClient(create_app(_settings(database_url))) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        operation = _student_operation(client, terminal)
        session_id, _world_id, revision, sequence, turn_sequence = _portal_call(
            client, _prepare_failed_run, terminal
        )
        # The seeded ContentUnit carries only the Build-facing task fields; the
        # teaching roles additionally require its story, concepts and hint policy.
        _portal_call(client, _patch_projection_inputs, terminal)
        suffix = terminal.build_id[-20:]
        turn_id = f"turn_hint_{suffix}"
        operation = replace(
            operation,
            request_id=f"req_hint_{suffix}",
            trace_id=f"trace_hint_{suffix}",
            correlation_id=f"corr_hint_{suffix}",
            command_id=f"cmd_hint_{suffix}",
            causation_id=None,
        )
        payload = _hint_payload(
            revision=revision,
            sequence=sequence,
            turn_sequence=turn_sequence,
            turn_id=turn_id,
            skill_bindings=[],
        )
        if response_type == "message":
            payload["input"]["text"] = "你是谁？"
        app = cast(Any, client.app)
        accepted = _portal_call(
            client,
            app.state.agent_turns.accept,
            session_id,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            f"idem_hint_{suffix}",
            operation,
        )
        assert accepted.__class__.__name__ == "Success", accepted
        command_id = str(accepted.value.command.command_id)

        _run_hint_turn(client, terminal, operation, accepted, _HintReplyProvider(response_type))

        state = _portal_call(client, _hint_side_effects, terminal, command_id, turn_id)
        command = state["command"]
        assert command.terminal is True
        assert command.status.value == "APPLIED"
        assert command.stage == "COMPLETE"
        assert command.result == {
            "result_type": "NO_EFFECT",
            "reason_code": "HINT_DELIVERED",
        }
        assert command.links == {"self": f"/v1/commands/{command_id}"}
        assert command.evidence_refs == ()
        assert state["job_status"] == "SUCCEEDED"
        assert state["job_phase"] == "COMPLETE"
        assert state["runs"] == 0
        assert state["evidence"] == 0
        assert state["learner_jobs"] == 0
        assert state["world_events"] == 0

        listed = client.get(
            f"/product-experience/v1/sessions/{session_id}/agent-interactions",
            headers=terminal.headers,
        )
        assert listed.status_code == 200, listed.text
        interactions = cast(list[dict[str, Any]], listed.json()["interactions"])
        assert len(interactions) == 1
        interaction = interactions[0]
        assert interaction["role"] == "teaching_agent"
        assert interaction["response_type"] == response_type
        assert interaction["hint_level"] is None
        assert interaction["skill_patch"] is None
        assert interaction["turn_id"] == turn_id
        assert interaction["feedback"]["run_id"] is None
        assert interaction["feedback"]["command_id"] == command_id
        assert interaction["feedback"]["source"] == "provider"
        assert interaction["feedback"]["degraded"] is False
        assert interaction["feedback"]["evidence_refs"] == []

        # The single-resource read runs the same durable authority closure.
        fetched = client.get(
            f"/product-experience/v1/sessions/{session_id}/"
            f"agent-interactions/{interaction['interaction_id']}",
            headers=terminal.headers,
        )
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["interaction_id"] == interaction["interaction_id"]


@pytest.mark.parametrize("concurrent_resource", ("run", "evidence", "provider_recovery", "hint_before_learner"))
def test_hint_answers_from_committed_success_before_book_and_learner_finish(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    concurrent_resource: str,
) -> None:
    database_url = _database_url()
    with TestClient(create_app(_settings(database_url))) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        operation = _student_operation(client, terminal)
        _portal_call(client, _prepare_failed_run, terminal)
        learner_id, task = _portal_call(client, _patch_projection_inputs, terminal)
        scope = _portal_call(client, _prepare_harvest_world, terminal)
        session_id, world_id, revision, sequence, turn_sequence, avatar_id = scope
        suffix = terminal.build_id[-16:]
        run_turn_id = f"turn_success_before_hint_{suffix}"
        run_operation = replace(
            operation,
            command_id=f"cmd_success_before_hint_{suffix}",
            requested_at=datetime.now(UTC),
            causation_id=None,
        )
        run_payload = _hint_payload(
            revision=revision,
            sequence=sequence,
            turn_sequence=turn_sequence,
            turn_id=run_turn_id,
            skill_bindings=[
                {
                    "skill_id": terminal.skill_id,
                    "skill_version_id": terminal.skill_version_id,
                    "artifact_sha256": terminal.artifact_sha256,
                    "certification_id": terminal.certification_id,
                }
            ],
        )
        app = cast(Any, client.app)
        run_accepted = _portal_call(
            client,
            app.state.agent_turns.accept,
            session_id,
            json.dumps(run_payload).encode(),
            f"idem_success_before_hint_{suffix}",
            run_operation,
        )
        assert isinstance(run_accepted, Success), run_accepted
        execution = _portal_call(
            client,
            _execute_successful_run,
            terminal,
            app.state.workflow_jobs,
            app.state.game_queries._command_store,
            run_accepted.value.command.command_id,
            run_turn_id,
            session_id,
            world_id,
            revision,
            avatar_id,
        )
        assert execution.result.run.task_success is True
        assert execution.result.run.world_commit is not None
        run_state = _portal_call(
            client,
            _hint_side_effects,
            terminal,
            run_accepted.value.command.command_id,
            run_turn_id,
        )
        assert run_state["command"].terminal is False
        assert run_state["learner_jobs"] == 0

        hint_turn_id = f"turn_hint_after_success_{suffix}"
        hint_operation = replace(
            operation,
            command_id=f"cmd_hint_after_success_{suffix}",
            requested_at=datetime.now(UTC),
            causation_id=None,
        )
        hint_payload = _hint_payload(
            revision=execution.fixture.world.revision,
            sequence=execution.fixture.world.last_event_sequence,
            turn_sequence=turn_sequence + 1,
            turn_id=hint_turn_id,
            skill_bindings=[],
        )
        hint_payload["input"]["text"] = "刚才运行成功了吗？"
        accepted = _portal_call(
            client,
            app.state.agent_turns.accept,
            session_id,
            json.dumps(hint_payload).encode(),
            f"idem_hint_after_success_{suffix}",
            hint_operation,
        )
        assert isinstance(accepted, Success), accepted
        if concurrent_resource == "hint_before_learner":
            from tests.integration import test_turn_execution_durability as execution_helpers

            authority, outcome, decision = _portal_call(
                client, _failure_projection_authority, execution.fixture,
                execution.result, learner_id, task, 0,
            )

            async def handoff_then_hint_then_project() -> None:
                fixture = execution.fixture
                commands = execution_helpers.PostgresCommandStore(fixture.sessions)
                await execution_helpers.finish_turn_projection(
                    session_factory=fixture.sessions, commands=commands,
                    jobs=fixture.jobs, authority=authority, outcome=outcome,
                    decision=decision, result=execution.result, lease_seconds=600,
                )

            _portal_call(client, handoff_then_hint_then_project)
            _run_hint_turn(client, terminal, hint_operation, accepted, _SuccessfulRunHintProvider())

            async def project_after_hint() -> None:
                fixture = execution.fixture
                jobs = execution_helpers.PostgresLearnerProjectionJobStore(fixture.sessions)
                claim = await execution_helpers._claim_learner_eventually(
                    jobs, tenant_id=terminal.tenant_id, worker_id="learner-after-hint",
                    lease_seconds=600,
                )
                assert claim is not None
                projector = execution_helpers.PostgresLearnerProjector(
                    session_factory=fixture.sessions, jobs=jobs,
                    commands=execution_helpers.PostgresCommandStore(fixture.sessions),
                    lease_seconds=600,
                )
                await projector.project(claim)
                await projector.validate_terminal(claim)

            _portal_call(client, project_after_hint)
            listed = client.get(
                f"/product-experience/v1/sessions/{session_id}/agent-interactions",
                headers=terminal.headers,
            )
            assert listed.status_code == 200, listed.text
            assert [item["turn_id"] for item in listed.json()["interactions"]] == [
                hint_turn_id, run_turn_id,
            ]
            for url in (
                f"/v1/runs/{execution.run_id}",
                f"/v1/commands/{run_accepted.value.command.command_id}",
                f"/product-experience/v1/sessions/{session_id}/workspace",
            ):
                response = client.get(url, headers=terminal.headers)
                assert response.status_code == 200, response.text
            return
        if concurrent_resource == "provider_recovery":
            pending_provider = _PendingSuccessfulRunHintProvider()
            context_builds = 0
            original_build = ContextBuilder.build

            async def count_context_builds(self: Any, *args: Any, **kwargs: Any) -> Any:
                nonlocal context_builds
                context_builds += 1
                return await original_build(self, *args, **kwargs)

            monkeypatch.setattr(ContextBuilder, "build", count_context_builds)

            def worker() -> WorkflowWorker:
                # A new handler rebuilds all in-memory state on each attempt.
                return WorkflowWorker(
                    session_factory=terminal.sessions,
                    jobs=app.state.workflow_jobs,
                    commands=app.state.game_queries._command_store,
                    handlers=(_hint_handler(client, terminal, accepted, pending_provider),),
                    worker_id=f"worker_hint_recovery_{suffix}",
                    lease_seconds=600,
                )

            async def run_once_when_due() -> bool:
                # Host/Docker causal timestamps can briefly lead PostgreSQL.
                # Match the real polling loop instead of assuming READY means
                # claimable in the very same tick as acceptance.
                current_worker = worker()
                deadline = time.monotonic() + 2.0
                while True:
                    if await current_worker.run_once(terminal.tenant_id):
                        return True
                    if time.monotonic() >= deadline:
                        return False
                    await asyncio.sleep(0.01)

            with _query_metrics() as pending_metrics:
                assert _portal_call(client, run_once_when_due)
            waiting = _portal_call(
                client,
                _hint_side_effects,
                terminal,
                accepted.value.command.command_id,
                hint_turn_id,
            )
            assert waiting["job_status"] == "RETRY_WAIT"
            authority, outcome, decision = _portal_call(
                client,
                _failure_projection_authority,
                execution.fixture,
                execution.result,
                learner_id,
                task,
                0,
            )
            _portal_call(
                client,
                _finish_and_project,
                execution.fixture,
                authority,
                outcome,
                decision,
                execution.result,
            )
            with _query_metrics() as recovery_metrics:
                assert _portal_call(client, run_once_when_due)
            recovered = _portal_call(
                client,
                _hint_side_effects,
                terminal,
                accepted.value.command.command_id,
                hint_turn_id,
            )
            assert recovered["command"].status.value == "APPLIED", recovered
            assert pending_provider.calls == 1
            assert pending_provider.reconciliations >= 1
            assert context_builds == 1
            assert (
                recovered["runs"],
                recovered["evidence"],
                recovered["learner_jobs"],
                recovered["world_events"],
            ) == (0, 0, 0, 0)
            after_recovery = client.get(
                f"/product-experience/v1/sessions/{session_id}/agent-interactions",
                headers=terminal.headers,
            )
            assert after_recovery.status_code == 200, after_recovery.text
            latest = after_recovery.json()["interactions"][-1]
            assert latest["turn_id"] == hint_turn_id
            assert latest["feedback"]["run_id"] == execution.run_id
            assert "任务已完成" in latest["feedback"]["message"]
            print(
                json.dumps(
                    {
                        "scenario": "hint_provider_recovery_after_book",
                        "pending_attempt": pending_metrics,
                        "recovery_attempt": recovery_metrics,
                        "context_builds": context_builds,
                        "provider_generations": pending_provider.calls,
                    }
                )
            )
            return
        provider = _SuccessfulRunHintProvider()
        with _query_metrics() as execution_metrics:
            _run_hint_turn(client, terminal, hint_operation, accepted, provider)

        assert provider.calls == 1
        assert provider.contexts[0]["run_result"]["task_success"] is True
        hint_state = _portal_call(
            client,
            _hint_side_effects,
            terminal,
            accepted.value.command.command_id,
            hint_turn_id,
        )
        assert hint_state["command"].terminal is True
        assert hint_state["command"].status.value == "APPLIED"
        assert (
            hint_state["runs"],
            hint_state["evidence"],
            hint_state["learner_jobs"],
            hint_state["world_events"],
        ) == (0, 0, 0, 0)
        still_pending = _portal_call(
            client,
            _hint_side_effects,
            terminal,
            run_accepted.value.command.command_id,
            run_turn_id,
        )
        assert still_pending["command"].terminal is False
        assert still_pending["learner_jobs"] == 0
        with _query_metrics() as read_metrics:
            listed = client.get(
                f"/product-experience/v1/sessions/{session_id}/agent-interactions",
                headers=terminal.headers,
            )
        assert listed.status_code == 200, listed.text
        interaction = listed.json()["interactions"][-1]
        assert interaction["turn_id"] == hint_turn_id
        assert interaction["feedback"]["run_id"] == execution.run_id
        assert "任务已完成" in interaction["feedback"]["message"]
        fetched = client.get(
            f"/product-experience/v1/sessions/{session_id}/agent-interactions/"
            f"{interaction['interaction_id']}",
            headers=terminal.headers,
        )
        assert fetched.status_code == 200, fetched.text
        print(
            json.dumps(
                {
                    "scenario": "hint_after_committed_success_before_book",
                    "hint_execution": execution_metrics,
                    "interaction_list": read_metrics,
                    "provider_calls": provider.calls,
                }
            )
        )
        # The prompt reply must not block the older Run's later summary/learner
        # closure, and completing it must leave the earlier Hint readable.
        authority, outcome, decision = _portal_call(
            client,
            _failure_projection_authority,
            execution.fixture,
            execution.result,
            learner_id,
            task,
            0,
        )
        _portal_call(
            client,
            _finish_and_project,
            execution.fixture,
            authority,
            outcome,
            decision,
            execution.result,
        )
        after_summary = client.get(
            f"/product-experience/v1/sessions/{session_id}/agent-interactions",
            headers=terminal.headers,
        )
        assert after_summary.status_code == 200, after_summary.text
        assert [item["turn_id"] for item in after_summary.json()["interactions"]] == [
            hint_turn_id,
            run_turn_id,
        ]

        # Pause a real public read after it loaded the workspace, then commit a
        # second Hint on another transaction. All closure reads must still use
        # the first read's snapshot rather than mixing two valid workspace heads.
        workspace_loaded = threading.Event()
        hint_finished = threading.Event()
        validate_workspace = turn_projection._validate_current_workspace_head

        async def pause_after_workspace_load(*args: Any, **kwargs: Any) -> None:
            if not workspace_loaded.is_set():
                workspace_loaded.set()
                assert await asyncio.to_thread(hint_finished.wait, 30)
            await validate_workspace(*args, **kwargs)

        monkeypatch.setattr(
            turn_projection,
            "_validate_current_workspace_head",
            pause_after_workspace_load,
        )
        resource_url = (
            f"/v1/runs/{execution.run_id}"
            if concurrent_resource == "run"
            else f"/v1/evidence/{execution.result.run.evidence_refs[0].evidence_id}"
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(client.get, resource_url, headers=terminal.headers)
            try:
                assert workspace_loaded.wait(15)
                next_hint_turn_id = f"turn_hint_during_read_{suffix}"
                next_hint_operation = replace(
                    hint_operation,
                    command_id=f"cmd_hint_during_read_{suffix}",
                    requested_at=datetime.now(UTC),
                )
                next_hint_payload = _hint_payload(
                    revision=execution.fixture.world.revision,
                    sequence=execution.fixture.world.last_event_sequence,
                    turn_sequence=turn_sequence + 2,
                    turn_id=next_hint_turn_id,
                    skill_bindings=[],
                )
                next_hint = _portal_call(
                    client,
                    app.state.agent_turns.accept,
                    session_id,
                    json.dumps(next_hint_payload).encode(),
                    f"idem_hint_during_read_{suffix}",
                    next_hint_operation,
                )
                assert isinstance(next_hint, Success), next_hint
                _run_hint_turn(
                    client,
                    terminal,
                    next_hint_operation,
                    next_hint,
                    _SuccessfulRunHintProvider(),
                )
            finally:
                hint_finished.set()
            concurrent_read = reading.result(timeout=15)
        assert concurrent_read.status_code == 200, concurrent_read.text


def test_hint_turn_read_fails_closed_when_projection_source_drifts(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    with TestClient(create_app(_settings(database_url))) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        operation = _student_operation(client, terminal)
        session_id, _world_id, revision, sequence, turn_sequence = _portal_call(
            client, _prepare_failed_run, terminal
        )
        # The seeded ContentUnit carries only the Build-facing task fields; the
        # teaching roles additionally require its story, concepts and hint policy.
        _portal_call(client, _patch_projection_inputs, terminal)
        suffix = terminal.build_id[-20:]
        turn_id = f"turn_hint_drift_{suffix}"
        operation = replace(
            operation,
            request_id=f"req_hint_drift_{suffix}",
            trace_id=f"trace_hint_drift_{suffix}",
            correlation_id=f"corr_hint_drift_{suffix}",
            command_id=f"cmd_hint_drift_{suffix}",
            causation_id=None,
        )
        app = cast(Any, client.app)
        accepted = _portal_call(
            client,
            app.state.agent_turns.accept,
            session_id,
            json.dumps(
                _hint_payload(
                    revision=revision,
                    sequence=sequence,
                    turn_sequence=turn_sequence,
                    turn_id=turn_id,
                    skill_bindings=[],
                ),
                separators=(",", ":"),
            ).encode("utf-8"),
            f"idem_hint_drift_{suffix}",
            operation,
        )
        assert accepted.__class__.__name__ == "Success", accepted
        _run_hint_turn(client, terminal, operation, accepted)

        _portal_call(client, _tamper_hint_interaction_role, terminal, session_id)
        listed = client.get(
            f"/product-experience/v1/sessions/{session_id}/agent-interactions",
            headers=terminal.headers,
        )
        assert listed.status_code == 500, listed.text
        assert listed.json()["error"]["code"] == "INVARIANT_VIOLATION"


async def _tamper_hint_interaction_role(terminal: _TerminalBuild, session_id: str) -> None:
    from walnut_backend.adapters.postgres.models import ProductInteractionRow

    async with terminal.sessions() as session, session.begin():
        row = await session.scalar(
            select(ProductInteractionRow).where(
                ProductInteractionRow.tenant_id == terminal.tenant_id,
                ProductInteractionRow.session_id == session_id,
            )
        )
        assert row is not None
        value = dict(row.interaction_json)
        value["role"] = "bug_agent"
        row.interaction_json = value


def test_hint_gateway_rejects_bindingless_non_message_turn(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    with TestClient(create_app(_settings(database_url))) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        operation = _student_operation(client, terminal)
        session_id, _world_id, revision, sequence, turn_sequence = _portal_call(
            client, _prepare_failed_run, terminal
        )
        # The seeded ContentUnit carries only the Build-facing task fields; the
        # teaching roles additionally require its story, concepts and hint policy.
        _portal_call(client, _patch_projection_inputs, terminal)
        suffix = terminal.build_id[-20:]
        payload = {
            "turn_id": f"turn_hint_reject_{suffix}",
            "expected_world_revision": revision,
            "input": {"type": "ASSIGNED_TASK", "task_id": "task_hint_reject_0001"},
            "skill_bindings": [],
            "client_state": {
                "last_event_sequence": sequence,
                "client_turn_sequence": turn_sequence,
            },
        }
        app = cast(Any, client.app)
        rejected = _portal_call(
            client,
            app.state.agent_turns.accept,
            session_id,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            f"idem_hint_reject_{suffix}",
            replace(
                operation,
                request_id=f"req_hint_reject_{suffix}",
                trace_id=f"trace_hint_reject_{suffix}",
                correlation_id=f"corr_hint_reject_{suffix}",
                command_id=f"cmd_hint_reject_{suffix}",
                causation_id=None,
            ),
        )
        assert rejected.__class__.__name__ == "Failure", rejected
        assert rejected.error.code == "SKILL_NOT_CERTIFIED"


def test_hint_turn_survives_one_read_only_tool_round(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool round plus the decision must stay inside the HINT receipt bound."""

    database_url = _database_url()
    with TestClient(create_app(_settings(database_url))) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        operation = _student_operation(client, terminal)
        session_id, _world_id, revision, sequence, turn_sequence = _portal_call(
            client, _prepare_failed_run, terminal
        )
        _portal_call(client, _patch_projection_inputs, terminal)
        suffix = terminal.build_id[-20:]
        turn_id = f"turn_hint_tool_{suffix}"
        operation = replace(
            operation,
            request_id=f"req_hint_tool_{suffix}",
            trace_id=f"trace_hint_tool_{suffix}",
            correlation_id=f"corr_hint_tool_{suffix}",
            command_id=f"cmd_hint_tool_{suffix}",
            causation_id=None,
        )
        app = cast(Any, client.app)
        accepted = _portal_call(
            client,
            app.state.agent_turns.accept,
            session_id,
            json.dumps(
                _hint_payload(
                    revision=revision,
                    sequence=sequence,
                    turn_sequence=turn_sequence,
                    turn_id=turn_id,
                    skill_bindings=[],
                ),
                separators=(",", ":"),
            ).encode("utf-8"),
            f"idem_hint_tool_{suffix}",
            operation,
        )
        assert accepted.__class__.__name__ == "Success", accepted
        provider = _HintToolThenDecisionProvider()
        _run_hint_turn(client, terminal, operation, accepted, provider)
        assert provider.rounds == 2

        listed = client.get(
            f"/product-experience/v1/sessions/{session_id}/agent-interactions",
            headers=terminal.headers,
        )
        assert listed.status_code == 200, listed.text
        interactions = cast(list[dict[str, Any]], listed.json()["interactions"])
        assert len(interactions) == 1
        assert interactions[0]["response_type"] == "question"
        assert interactions[0]["feedback"]["run_id"] is None
