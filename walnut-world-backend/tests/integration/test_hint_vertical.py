"""Real PostgreSQL vertical for the no-Run teaching hint Turn.

A hint asks the teaching roles to explain the student's current situation.  It
must reach a terminal ``APPLIED`` Command and one readable AgentInteraction
without producing a Run, Evidence, a learner projection or any World event.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    SandboxLimits,
    Success,
)

from tests.integration.test_int2_patch_vertical import (
    _claim_patch_turn,
    _FailedRunSandbox,
    _patch_projection_inputs,
    _prepare_failed_run,
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
from walnut_backend.workers.turn_worker import TurnWorkflowHandler

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class _HintReplyProvider(_ReplyProvider):
    """Return one non-degraded teaching question with no Patch and no tools."""

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
    versions = accepted.value.command.versions
    handler = TurnWorkflowHandler(
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
    del operation
    _portal_call(client, handler.execute, claim)


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


def test_hint_turn_applies_without_run_evidence_or_world_change(
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

        _run_hint_turn(client, terminal, operation, accepted)

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
        assert interaction["response_type"] == "question"
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
