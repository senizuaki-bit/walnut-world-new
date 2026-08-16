"""Real PostgreSQL vertical and corruption gates for INT2 provenance."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select
from yaya_agent_build import (
    DigestPinnedDockerCppBuilder,
    DockerBuildResult,
    canonical_source_bundle_sha256,
)
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContractError,
    ErrorCategory,
    Failure,
    HarvestIntent,
    OperationContext,
    SandboxLimits,
    SandboxRunResult,
    SandboxUsage,
    SkillRef,
    Success,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    LEARNER_PROJECTION_POLICY_VERSION,
    SkillInvocationRequest,
    side_effect_execution_id,
    skill_invocation_request_sha256,
)

from tests.integration.test_terminal_read_closure import (
    _activate_and_read_skill,
    _activation_scope,
    _claim_activation,
    _claim_build,
    _claim_workflow_eventually,
    _database_url,
    _execute_build,
    _portal_call,
    _terminal_identities,
    _TerminalBuild,
)
from tests.integration.test_turn_execution_durability import (
    _build_terminal_projection_authority,
    _ExecutionFixture,
    _finish_and_project,
    _ReplyProvider,
    _successful_provider_resource,
)
from walnut_backend.adapters.postgres import run_outcomes
from walnut_backend.adapters.postgres.agent_runtime import (
    AgentRuntimeAuthorityError,
    PostgresAgentRuntimeReads,
)
from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.models import (
    AgentSessionRow,
    AgentTurnRow,
    CommandRow,
    EvidenceRow,
    JobStepReceiptRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    LearnerProjectionJobRow,
    ProductContentUnitRow,
    ProductDraftRevisionAssistanceRow,
    ProductDraftRevisionRow,
    ProductDraftRow,
    ProductInteractionRow,
    ProductPatchDecisionReceiptRow,
    ProductSkillPatchDecisionRow,
    ProductWorkspaceRow,
    RegistryEntryRow,
    RegistryHeadRow,
    RunRow,
    SkillActivationProvenanceRow,
    SkillActivationRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
    SkillCertificationProvenanceRow,
    SkillRunProvenanceRow,
    WorkflowJobRow,
    WorldPresentationEventRow,
    WorldSnapshotRow,
    WorldStreamRow,
    command_record_from_data,
    world_snapshot_from_data,
)
from walnut_backend.adapters.postgres.skill_invocation import (
    PostgresFencedSkillInvocation,
)
from walnut_backend.adapters.postgres.workflow_jobs import (
    workflow_receipt_sha256,
    workflow_step_receipt_id,
)
from walnut_backend.adapters.postgres.world import PostgresWorldUnitOfWork
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings
from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules
from walnut_backend.workers.build_worker import BuildWorkflowHandler
from walnut_backend.workers.control_worker import ControlWorkflowHandler
from walnut_backend.workers.turn_projection import finish_turn_projection
from walnut_backend.workers.turn_worker import TurnWorkflowHandler


@dataclass(frozen=True, slots=True)
class _FailedRunExecution:
    run_id: str
    fixture: _ExecutionFixture
    result: Any


@dataclass(frozen=True, slots=True)
class _FailureChain:
    terminal: _TerminalBuild
    operation: OperationContext
    session_id: str
    world_id: str
    world_revision: int
    world_sequence: int
    next_turn_sequence: int
    interactions: tuple[dict[str, Any], ...]


class _PatchReplyProvider(_ReplyProvider):
    async def dispatch(self, identity: Any, request: Any, context: Any) -> Any:
        del request, context
        self.calls += 1
        resource = _successful_provider_resource(identity)
        result = cast(Any, resource.result)
        reply = result.value
        output = {
            "kind": "decision",
            "decision": {
                "role": "teaching_agent",
                "response_type": "skill_patch",
                "message": "Review this exact replacement before deciding.",
                "question": None,
                "hint_level": 4,
                "learner_inference": None,
                "skill_patch": {
                    "replacement_content": (
                        "// INT2 student-confirmed candidate\n"
                        "#include <yaya/skill.hpp>\n"
                        "int main() { return 0; }\n"
                    ),
                    "rationale": "Replace the current entrypoint after the exact failed Run.",
                },
                "requires_student_confirmation": True,
            },
            "tool_calls": [],
        }
        patched = replace(resource, result=Success(replace(reply, output=output)))
        self.resources[identity.dispatch_id] = patched
        return patched


def test_activation_entry_corruption_fails_all_public_consumers(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        snapshot, operation = _activate_and_read_skill(client, terminal)
        activation_id, activation_command_id, entry_revision = _portal_call(
            client, _active_activation_identity, terminal
        )
        headers = terminal.headers
        assert (
            client.get(f"/v1/skill-activations/{activation_id}", headers=headers).status_code == 200
        )
        assert (
            client.get(f"/v1/commands/{activation_command_id}", headers=headers).status_code == 200
        )
        assert client.get("/v1/student-bootstrap", headers=headers).status_code == 200

        original = _portal_call(
            client,
            _replace_entry_authority,
            terminal,
            entry_revision,
            "authority_corrupt_12345678",
        )
        try:
            assert (
                client.get(f"/v1/skill-activations/{activation_id}", headers=headers).status_code
                == 500
            )
            assert (
                client.get(f"/v1/commands/{activation_command_id}", headers=headers).status_code
                == 500
            )
            assert client.get("/v1/student-bootstrap", headers=headers).status_code == 500
            with pytest.raises(AgentRuntimeAuthorityError):
                _portal_call(
                    client,
                    PostgresAgentRuntimeReads(terminal.sessions).get_bound_skill,
                    snapshot.ref,
                    operation,
                )
        finally:
            _portal_call(
                client,
                _replace_entry_authority,
                terminal,
                entry_revision,
                original,
            )


@pytest.mark.parametrize(
    "tamper",
    ("entry_and_receipt", "job_and_receipt_input", "job_json"),
)
def test_activation_seal_rejects_coordinated_authority_rewrites(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _activate_and_read_skill(client, terminal)
        activation_id, activation_command_id, entry_revision = _portal_call(
            client, _active_activation_identity, terminal
        )
        assert (
            client.get(
                f"/v1/skill-activations/{activation_id}", headers=terminal.headers
            ).status_code
            == 200
        )

        _portal_call(
            client,
            _coordinated_activation_rewrite,
            terminal,
            entry_revision,
            tamper,
        )

        assert (
            client.get(
                f"/v1/skill-activations/{activation_id}", headers=terminal.headers
            ).status_code
            == 500
        )
        assert (
            client.get(
                f"/v1/commands/{activation_command_id}", headers=terminal.headers
            ).status_code
            == 500
        )


def test_certification_rejects_build_workflow_job_json_drift(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _, operation = _activate_and_read_skill(client, terminal)
        operation = replace(
            operation,
            actor=ActorRef(
                terminal.tenant_id,
                terminal.actor_id,
                ActorType.STUDENT,
                ("game:player",),
            ),
        )
        activation_id, _, _ = _portal_call(client, _active_activation_identity, terminal)
        assert (
            client.get(
                f"/v1/skill-builds/{terminal.build_id}", headers=terminal.headers
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/v1/skill-activations/{activation_id}", headers=terminal.headers
            ).status_code
            == 200
        )

        _portal_call(client, _rewrite_build_job_trace, terminal)

        assert (
            client.get(
                f"/v1/skill-builds/{terminal.build_id}", headers=terminal.headers
            ).status_code
            == 500
        )
        assert (
            client.get(
                f"/v1/skill-activations/{activation_id}", headers=terminal.headers
            ).status_code
            == 500
        )


def test_certification_seal_rejects_coordinated_build_context_rewrite(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _activate_and_read_skill(client, terminal)
        activation_id, _, _ = _portal_call(client, _active_activation_identity, terminal)
        assert (
            client.get(
                f"/v1/skill-builds/{terminal.build_id}", headers=terminal.headers
            ).status_code
            == 200
        )
        assert (
            client.get(f"/v1/commands/{terminal.command_id}", headers=terminal.headers).status_code
            == 200
        )
        assert (
            client.get(
                f"/v1/skill-activations/{activation_id}", headers=terminal.headers
            ).status_code
            == 200
        )

        _portal_call(client, _rewrite_all_build_context_traces, terminal)

        assert (
            client.get(
                f"/v1/skill-builds/{terminal.build_id}", headers=terminal.headers
            ).status_code
            == 500
        )
        assert (
            client.get(f"/v1/commands/{terminal.command_id}", headers=terminal.headers).status_code
            == 500
        )
        assert (
            client.get(
                f"/v1/skill-activations/{activation_id}", headers=terminal.headers
            ).status_code
            == 500
        )


def test_run_read_rejects_noncanonical_skill_invoked_receipt_identity(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _, operation = _activate_and_read_skill(client, terminal)
        operation = replace(
            operation,
            actor=ActorRef(
                terminal.tenant_id,
                terminal.actor_id,
                ActorType.STUDENT,
                ("game:player",),
            ),
        )
        session_id, world_id, revision, sequence, turn_sequence = _portal_call(
            client, _prepare_failed_run, terminal
        )
        suffix = terminal.build_id[-20:]
        turn_id = f"turn_run_receipt_{suffix}"
        headers = {
            **terminal.headers,
            "X-Request-Id": f"req_run_receipt_{suffix}",
            "X-Trace-Id": f"trace_run_receipt_{suffix}",
            "X-Correlation-Id": f"corr_run_receipt_{suffix}",
            "Idempotency-Key": f"idem_run_receipt_{suffix}",
        }
        payload = {
            "turn_id": turn_id,
            "expected_world_revision": revision,
            "input": {
                "type": "MESSAGE",
                "text": "run failing fixture",
                "locale": "zh-CN",
            },
            "skill_bindings": [
                {
                    "skill_id": terminal.skill_id,
                    "skill_version_id": terminal.skill_version_id,
                    "artifact_sha256": terminal.artifact_sha256,
                    "certification_id": terminal.certification_id,
                }
            ],
            "client_state": {
                "last_event_sequence": sequence,
                "client_turn_sequence": turn_sequence,
            },
        }
        app = cast(Any, client.app)
        assert (
            app.state.contract_release.validate(
                "contracts/schemas/game/agent-turn-create-request.schema.json", payload
            )
            == []
        )
        accepted = _portal_call(
            client,
            app.state.agent_turns.accept,
            session_id,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers["Idempotency-Key"],
            operation,
        )
        assert accepted.__class__.__name__ == "Success", accepted
        accepted_command_id = str(accepted.value.command.command_id)
        execution = _portal_call(
            client,
            _execute_failed_run,
            terminal,
            app.state.workflow_jobs,
            app.state.game_queries._command_store,
            accepted_command_id,
            turn_id,
            session_id,
            world_id,
            revision,
        )
        run_id = execution.run_id
        assert client.get(f"/v1/runs/{run_id}", headers=terminal.headers).status_code == 200

        _portal_call(client, _rewrite_invocation_receipt_id, terminal, run_id)

        assert client.get(f"/v1/runs/{run_id}", headers=terminal.headers).status_code == 500


def test_patch_vertical_materializes_selected_failure_interaction(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _, operation = _activate_and_read_skill(client, terminal)
        operation = replace(
            operation,
            actor=ActorRef(
                terminal.tenant_id,
                terminal.actor_id,
                ActorType.STUDENT,
                ("game:player",),
            ),
        )
        session_id, world_id, revision, sequence, turn_sequence = _portal_call(
            client, _prepare_failed_run, terminal
        )
        suffix = terminal.build_id[-20:]
        turn_id = f"turn_patch_failure_{suffix}"
        headers = {
            **terminal.headers,
            "X-Request-Id": f"req_patch_failure_{suffix}",
            "X-Trace-Id": f"trace_patch_failure_{suffix}",
            "X-Correlation-Id": f"corr_patch_failure_{suffix}",
            "Idempotency-Key": f"idem_patch_failure_{suffix}",
        }
        payload = {
            "turn_id": turn_id,
            "expected_world_revision": revision,
            "input": {
                "type": "MESSAGE",
                "text": "produce the fourth exact failure",
                "locale": "zh-CN",
            },
            "skill_bindings": [
                {
                    "skill_id": terminal.skill_id,
                    "skill_version_id": terminal.skill_version_id,
                    "artifact_sha256": terminal.artifact_sha256,
                    "certification_id": terminal.certification_id,
                }
            ],
            "client_state": {
                "last_event_sequence": sequence,
                "client_turn_sequence": turn_sequence,
            },
        }
        app = cast(Any, client.app)
        accepted = _portal_call(
            client,
            app.state.agent_turns.accept,
            session_id,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers["Idempotency-Key"],
            operation,
        )
        assert accepted.__class__.__name__ == "Success", accepted
        execution = _portal_call(
            client,
            _execute_failed_run,
            terminal,
            app.state.workflow_jobs,
            app.state.game_queries._command_store,
            str(accepted.value.command.command_id),
            turn_id,
            session_id,
            world_id,
            revision,
        )
        learner_id, task = _portal_call(client, _patch_projection_inputs, terminal)
        authority, outcome, decision = _portal_call(
            client,
            _failure_projection_authority,
            execution.fixture,
            execution.result,
            learner_id,
            task,
            1,
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

        response = client.get(
            f"/product-experience/v1/sessions/{session_id}/agent-interactions",
            headers=terminal.headers,
        )
        assert response.status_code == 200, response.text
        interactions = response.json()["interactions"]
        assert len(interactions) == 1
        assert interactions[0]["role"] == "teaching_agent"
        assert interactions[0]["response_type"] == "question"


def test_patch_vertical_materializes_real_four_failure_suffix(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _, operation = _activate_and_read_skill(client, terminal)
        operation = replace(
            operation,
            actor=ActorRef(
                terminal.tenant_id,
                terminal.actor_id,
                ActorType.STUDENT,
                ("game:player",),
            ),
        )
        chain = _materialize_failure_chain(client, terminal, operation, count=4)

        assert [item["sequence"] for item in chain.interactions] == [1, 2, 3, 4]
        assert [item["role"] for item in chain.interactions] == [
            "teaching_agent",
            "teaching_agent",
            "bug_agent",
            "bug_agent",
        ]
        assert chain.interactions[-1]["response_type"] == "question"


def test_four_failure_public_run_read_is_bounded_and_fail_closed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _, operation = _activate_and_read_skill(client, terminal)
        operation = replace(
            operation,
            actor=ActorRef(
                terminal.tenant_id,
                terminal.actor_id,
                ActorType.STUDENT,
                ("game:player",),
            ),
        )
        chain = _materialize_failure_chain(client, terminal, operation, count=4)
        run_id, receipt_json_before, receipt_sha_before = _portal_call(
            client,
            _fourth_failure_receipt_snapshot,
            terminal,
            chain.session_id,
        )

        projection_calls: Counter[tuple[str, ...]] = Counter()
        load_calls: Counter[tuple[str, ...]] = Counter()
        original_projection = run_outcomes._validate_terminal_projection_uncached
        original_load = run_outcomes._load_validated_run_uncached

        async def counted_projection(
            session: Any,
            authority: Any,
            *,
            validation_state: Any,
        ) -> None:
            projection_calls[
                (
                    authority.job.tenant_id,
                    authority.context.actor.actor_id,
                    authority.context.content_ref.content_hash,
                    authority.run.session_id,
                    authority.run.turn_id,
                    authority.run.run_id,
                    authority.run.command_id,
                )
            ] += 1
            await original_projection(
                session,
                authority,
                validation_state=validation_state,
            )

        async def counted_load(*args: Any, **kwargs: Any) -> Any:
            authority = await original_load(*args, **kwargs)
            load_calls[
                (
                    authority.job.tenant_id,
                    authority.context.actor.actor_id,
                    authority.context.content_ref.content_hash,
                    authority.run.session_id,
                    authority.run.turn_id,
                    authority.run.run_id,
                    authority.run.command_id,
                )
            ] += 1
            return authority

        monkeypatch.setattr(
            run_outcomes,
            "_validate_terminal_projection_uncached",
            counted_projection,
        )
        monkeypatch.setattr(
            run_outcomes,
            "_load_validated_run_uncached",
            counted_load,
        )

        statements: list[str] = []
        engine = terminal.sessions.kw.get("bind")
        assert engine is not None

        def capture_sql(
            _connection: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            statements.append(" ".join(statement.lower().split()))

        event.listen(engine.sync_engine, "before_cursor_execute", capture_sql)
        started = time.perf_counter()
        try:
            response = client.get(f"/v1/runs/{run_id}", headers=terminal.headers)
        finally:
            elapsed = time.perf_counter() - started
            event.remove(engine.sync_engine, "before_cursor_execute", capture_sql)

        suffix_statements = [
            statement
            for statement in statements
            if "agent_turns" in statement
            and "left outer join" in statement
            and "select distinct game_runs.command_id" in statement
        ]
        prior_scalar_count = sum(
            statement.startswith("select game_runs.run_id from game_runs")
            for statement in statements
        )
        record_property("public_get_elapsed_seconds", f"{elapsed:.6f}")
        record_property("projection_uncached_counts", sorted(projection_calls.values()))
        record_property("validated_run_uncached_counts", sorted(load_calls.values()))
        record_property("failure_suffix_sql_count", len(suffix_statements))
        record_property("prior_run_scalar_sql_count", prior_scalar_count)

        assert response.status_code == 200, response.text
        assert elapsed < 15.0
        assert len(projection_calls) == 4
        assert set(projection_calls.values()) == {1}
        assert len(load_calls) == 4
        assert set(load_calls.values()) == {1}
        assert len(suffix_statements) == 4
        assert prior_scalar_count == 0

        interaction_id = str(chain.interactions[-1]["interaction_id"])
        original_interaction = _portal_call(
            client,
            _tamper_product_interaction_message,
            terminal,
            chain.session_id,
            interaction_id,
        )
        try:
            corrupted = client.get(f"/v1/runs/{run_id}", headers=terminal.headers)
            assert corrupted.status_code == 500, corrupted.text
            assert corrupted.json()["error"]["code"] == "INVARIANT_VIOLATION"
        finally:
            _portal_call(
                client,
                _restore_product_interaction_message,
                terminal,
                chain.session_id,
                interaction_id,
                original_interaction,
            )
        restored = client.get(f"/v1/runs/{run_id}", headers=terminal.headers)
        assert restored.status_code == 200, restored.text

        _, receipt_json_after, receipt_sha_after = _portal_call(
            client,
            _fourth_failure_receipt_snapshot,
            terminal,
            chain.session_id,
        )
        assert receipt_json_after == receipt_json_before
        assert receipt_sha_after == receipt_sha_before
        assert receipt_sha_after == workflow_receipt_sha256(receipt_json_after)


def test_patch_vertical_materializes_proposal_from_selected_failure(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _, operation = _activate_and_read_skill(client, terminal)
        operation = replace(
            operation,
            actor=ActorRef(
                terminal.tenant_id,
                terminal.actor_id,
                ActorType.STUDENT,
                ("game:player",),
            ),
        )
        chain = _materialize_failure_chain(client, terminal, operation, count=4)
        provider = _PatchReplyProvider()
        interactions = _materialize_patch_proposal(client, chain, provider)

        assert provider.calls == 1
        assert _portal_call(
            client,
            _patch_provider_receipt_names,
            terminal,
            str(interactions[-1]["turn_id"]),
        ) == (
            "PATCH_PROVIDER_DISPATCH_01",
            "PATCH_PROVIDER_RESULT_01",
        )
        assert len(interactions) == 5
        proposal = interactions[-1]
        assert proposal["role"] == "teaching_agent"
        assert proposal["response_type"] == "skill_patch"
        assert proposal["hint_level"] == 4
        assert proposal["feedback"]["run_id"] is None
        assert proposal["skill_patch"]["operations"][0]["operation"] == "UPSERT_FILE"
        assert proposal["skill_patch"]["operations"][0]["path"] == "src/main.cpp"

        _portal_call(
            client,
            _append_patch_reconciliation_receipts,
            terminal,
            str(proposal["turn_id"]),
        )
        page = client.get(
            f"/product-experience/v1/sessions/{chain.session_id}/agent-interactions"
            "?after_sequence=4&limit=50",
            headers=terminal.headers,
        )
        selected = client.get(
            f"/product-experience/v1/sessions/{chain.session_id}/agent-interactions/"
            f"{proposal['interaction_id']}",
            headers=terminal.headers,
        )
        assert page.status_code == 200, page.text
        assert selected.status_code == 200, selected.text

        _portal_call(
            client,
            _append_patch_invalid_extra_receipt,
            terminal,
            str(proposal["turn_id"]),
        )
        rejected_page = client.get(
            f"/product-experience/v1/sessions/{chain.session_id}/agent-interactions"
            "?after_sequence=4&limit=50",
            headers=terminal.headers,
        )
        rejected_selected = client.get(
            f"/product-experience/v1/sessions/{chain.session_id}/agent-interactions/"
            f"{proposal['interaction_id']}",
            headers=terminal.headers,
        )
        assert rejected_page.status_code == 500, rejected_page.text
        assert rejected_selected.status_code == 500, rejected_selected.text
        assert rejected_page.json()["error"]["code"] == "INVARIANT_VIOLATION"
        assert rejected_selected.json()["error"]["code"] == "INVARIANT_VIOLATION"


def test_patch_vertical_accepts_current_entrypoint_proposal(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _, operation = _activate_and_read_skill(client, terminal)
        operation = replace(
            operation,
            actor=ActorRef(
                terminal.tenant_id,
                terminal.actor_id,
                ActorType.STUDENT,
                ("game:player",),
            ),
        )
        chain = _materialize_failure_chain(client, terminal, operation, count=4)
        provider = _PatchReplyProvider()
        interactions = _materialize_patch_proposal(client, chain, provider)
        proposal = interactions[-1]
        patch = cast(dict[str, Any], proposal["skill_patch"])
        before = _portal_call(
            client, _patch_decision_state, terminal, chain.session_id, chain.world_id
        )
        body = {
            "decision_id": f"decision_{terminal.build_id[-16:]}",
            "session_id": chain.session_id,
            "turn_id": proposal["turn_id"],
            "interaction_id": proposal["interaction_id"],
            "expected_interaction_revision": proposal["interaction_revision"],
            "patch_id": patch["patch_id"],
            "patch_sha256": patch["patch_sha256"],
            "draft_id": patch["draft_id"],
            "skill_id": patch["skill_id"],
            "base_draft_revision": patch["base_draft_revision"],
            "base_draft_sha256": patch["base_draft_sha256"],
            "result_draft_sha256": patch["result_draft_sha256"],
            "decision": "ACCEPT",
            "reason_code": None,
            "decided_at": proposal["updated_at"],
        }
        raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
        path = (
            f"/product-experience/v1/sessions/{chain.session_id}/"
            f"agent-interactions/{proposal['interaction_id']}/patches/"
            f"{patch['patch_id']}/decision"
        )
        response = client.post(
            path,
            headers={
                **terminal.headers,
                "Content-Type": "application/json",
                "Idempotency-Key": f"idem_patch_accept_{terminal.build_id[-16:]}",
            },
            content=raw_body,
        )

        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["decision"] == "ACCEPT"
        assert receipt["draft_updated"] is True
        assert receipt["draft_revision_after"] == patch["base_draft_revision"] + 1
        assert receipt["draft_sha256_after"] == patch["result_draft_sha256"]
        assert response.headers["idempotency-replayed"] == "false"

        interaction_response = client.get(response.headers["location"], headers=terminal.headers)
        assert interaction_response.status_code == 200, interaction_response.text
        decided_interaction = interaction_response.json()
        assert decided_interaction["interaction_revision"] == 2
        assert decided_interaction["patch_decision"] == receipt
        draft_response = client.get(
            f"/product-experience/v1/sessions/{chain.session_id}/skill-drafts/{patch['draft_id']}",
            headers=terminal.headers,
        )
        assert draft_response.status_code == 200, draft_response.text
        accepted_draft = draft_response.json()
        assert accepted_draft["revision"] == patch["base_draft_revision"] + 1
        assert accepted_draft["draft_sha256"] == patch["result_draft_sha256"]
        assert accepted_draft["last_applied_patch_id"] == patch["patch_id"]
        assert accepted_draft["source_bundle"]["files"] == [
            {
                "path": "src/main.cpp",
                "content": patch["operations"][0]["content"],
                "content_sha256": patch["operations"][0]["content_sha256"],
            }
        ]
        workspace_response = client.get(
            f"/product-experience/v1/sessions/{chain.session_id}/workspace",
            headers=terminal.headers,
        )
        assert workspace_response.status_code == 200, workspace_response.text
        workspace = workspace_response.json()
        assert workspace["skill_draft_refs"] == [
            {
                "draft_id": patch["draft_id"],
                "skill_id": patch["skill_id"],
                "revision": patch["base_draft_revision"] + 1,
                "draft_sha256": patch["result_draft_sha256"],
                "url": (
                    f"/product-experience/v1/sessions/{chain.session_id}/"
                    f"skill-drafts/{patch['draft_id']}"
                ),
            }
        ]

        after = _portal_call(
            client, _patch_decision_state, terminal, chain.session_id, chain.world_id
        )
        assert after["downstream"] == before["downstream"]
        assert len(before["revisions"]) == 1
        assert len(after["revisions"]) == 2
        base, accepted_revision = after["revisions"]
        accepted_row_id = after["decision"]["accepted_row_id"]
        assert base["revision"] == patch["base_draft_revision"]
        assert base["draft_sha256"] == patch["base_draft_sha256"]
        assert accepted_revision == {
            "row_id": accepted_row_id,
            "parent_row_id": base["row_id"],
            "revision": patch["base_draft_revision"] + 1,
            "draft_sha256": patch["result_draft_sha256"],
            "source_kind": "SKILL_PATCH",
            "patch_id": patch["patch_id"],
        }
        assert after["assistance"] == [
            {
                "row_id": accepted_row_id,
                "origin_row_id": accepted_row_id,
                "patch_id": patch["patch_id"],
                "decision_id": body["decision_id"],
                "inherited": False,
            }
        ]
        assert after["decision"] == {
            "decision_id": body["decision_id"],
            "patch_id": patch["patch_id"],
            "base_row_id": base["row_id"],
            "accepted_row_id": accepted_row_id,
            "decision": "ACCEPT",
        }
        assert after["receipt_count"] == 1

        replay = client.post(
            path,
            headers={
                **terminal.headers,
                "Content-Type": "application/json",
                "Idempotency-Key": f"idem_patch_accept_{terminal.build_id[-16:]}",
            },
            content=raw_body,
        )
        assert replay.status_code == 200, replay.text
        assert replay.headers["idempotency-replayed"] == "true"
        assert replay.json() == receipt
        assert provider.calls == 1
        assert (
            _portal_call(
                client,
                _patch_decision_state,
                terminal,
                chain.session_id,
                chain.world_id,
            )
            == after
        )

        conflicting_body = {**body, "decision_id": f"decision_conflict_{terminal.build_id[-16:]}"}
        conflict = client.post(
            path,
            headers={
                **terminal.headers,
                "Content-Type": "application/json",
                "Idempotency-Key": f"idem_patch_accept_{terminal.build_id[-16:]}",
            },
            content=json.dumps(conflicting_body, separators=(",", ":")).encode("utf-8"),
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert (
            _portal_call(
                client,
                _patch_decision_state,
                terminal,
                chain.session_id,
                chain.world_id,
            )
            == after
        )

        _portal_call(
            client,
            _tamper_accepted_decision_projection_to_reject,
            terminal,
            chain.session_id,
            proposal["interaction_id"],
        )
        corrupted = client.get(response.headers["location"], headers=terminal.headers)
        assert corrupted.status_code == 500, corrupted.text
        assert corrupted.json()["error"]["code"] == "INVARIANT_VIOLATION"

        _portal_call(
            client,
            _hide_accepted_decision_projection,
            terminal,
            chain.session_id,
            proposal["interaction_id"],
        )
        hidden = client.get(response.headers["location"], headers=terminal.headers)
        assert hidden.status_code == 500, hidden.text
        assert hidden.json()["error"]["code"] == "INVARIANT_VIOLATION"


def test_patch_vertical_reject_has_no_draft_or_business_side_effect(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _, operation = _activate_and_read_skill(client, terminal)
        operation = replace(
            operation,
            actor=ActorRef(
                terminal.tenant_id,
                terminal.actor_id,
                ActorType.STUDENT,
                ("game:player",),
            ),
        )
        chain = _materialize_failure_chain(client, terminal, operation, count=4)
        provider = _PatchReplyProvider()
        interactions = _materialize_patch_proposal(client, chain, provider)
        proposal = interactions[-1]
        patch = cast(dict[str, Any], proposal["skill_patch"])
        before = _portal_call(
            client, _patch_decision_state, terminal, chain.session_id, chain.world_id
        )
        body = {
            "decision_id": f"decision_reject_{terminal.build_id[-16:]}",
            "session_id": chain.session_id,
            "turn_id": proposal["turn_id"],
            "interaction_id": proposal["interaction_id"],
            "expected_interaction_revision": proposal["interaction_revision"],
            "patch_id": patch["patch_id"],
            "patch_sha256": patch["patch_sha256"],
            "draft_id": patch["draft_id"],
            "skill_id": patch["skill_id"],
            "base_draft_revision": patch["base_draft_revision"],
            "base_draft_sha256": patch["base_draft_sha256"],
            "result_draft_sha256": patch["result_draft_sha256"],
            "decision": "REJECT",
            "reason_code": "STUDENT_REJECTED",
            "decided_at": proposal["updated_at"],
        }
        raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
        path = (
            f"/product-experience/v1/sessions/{chain.session_id}/"
            f"agent-interactions/{proposal['interaction_id']}/patches/"
            f"{patch['patch_id']}/decision"
        )
        headers = {
            **terminal.headers,
            "Content-Type": "application/json",
            "Idempotency-Key": f"idem_patch_reject_{terminal.build_id[-16:]}",
        }
        response = client.post(path, headers=headers, content=raw_body)

        assert response.status_code == 200, response.text
        assert response.headers["idempotency-replayed"] == "false"
        receipt = response.json()
        assert receipt["decision"] == "REJECT"
        assert receipt["reason_code"] == "STUDENT_REJECTED"
        assert receipt["draft_updated"] is False
        assert receipt["draft_revision_after"] == patch["base_draft_revision"]
        assert receipt["draft_sha256_after"] == patch["base_draft_sha256"]
        interaction_response = client.get(response.headers["location"], headers=terminal.headers)
        assert interaction_response.status_code == 200, interaction_response.text
        decided_interaction = interaction_response.json()
        assert decided_interaction["interaction_revision"] == 2
        assert decided_interaction["patch_decision"] == receipt

        after = _portal_call(
            client, _patch_decision_state, terminal, chain.session_id, chain.world_id
        )
        assert after["draft"] == before["draft"]
        assert after["workspace"] == before["workspace"]
        assert after["revisions"] == before["revisions"]
        assert after["assistance"] == before["assistance"] == []
        assert after["downstream"] == before["downstream"]
        assert after["decision"] == {
            "decision_id": body["decision_id"],
            "patch_id": patch["patch_id"],
            "base_row_id": before["revisions"][0]["row_id"],
            "accepted_row_id": None,
            "decision": "REJECT",
        }
        assert after["receipt_count"] == 1

        replay = client.post(path, headers=headers, content=raw_body)
        assert replay.status_code == 200, replay.text
        assert replay.headers["idempotency-replayed"] == "true"
        assert replay.json() == receipt
        assert provider.calls == 1
        assert (
            _portal_call(
                client,
                _patch_decision_state,
                terminal,
                chain.session_id,
                chain.world_id,
            )
            == after
        )


def test_patch_vertical_rejects_stale_cas_and_corrupt_failure_evidence(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _, operation = _activate_and_read_skill(client, terminal)
        operation = replace(
            operation,
            actor=ActorRef(
                terminal.tenant_id,
                terminal.actor_id,
                ActorType.STUDENT,
                ("game:player",),
            ),
        )
        chain = _materialize_failure_chain(client, terminal, operation, count=4)
        provider = _PatchReplyProvider()
        proposal = _materialize_patch_proposal(client, chain, provider)[-1]
        patch = cast(dict[str, Any], proposal["skill_patch"])
        state = _portal_call(
            client, _patch_decision_state, terminal, chain.session_id, chain.world_id
        )
        body = {
            "decision_id": f"decision_guard_{terminal.build_id[-16:]}",
            "session_id": chain.session_id,
            "turn_id": proposal["turn_id"],
            "interaction_id": proposal["interaction_id"],
            "expected_interaction_revision": proposal["interaction_revision"],
            "patch_id": patch["patch_id"],
            "patch_sha256": patch["patch_sha256"],
            "draft_id": patch["draft_id"],
            "skill_id": patch["skill_id"],
            "base_draft_revision": patch["base_draft_revision"],
            "base_draft_sha256": patch["base_draft_sha256"],
            "result_draft_sha256": patch["result_draft_sha256"],
            "decision": "ACCEPT",
            "reason_code": None,
            "decided_at": proposal["updated_at"],
        }
        path = (
            f"/product-experience/v1/sessions/{chain.session_id}/"
            f"agent-interactions/{proposal['interaction_id']}/patches/"
            f"{patch['patch_id']}/decision"
        )
        headers = {
            **terminal.headers,
            "Content-Type": "application/json",
        }
        stale_revision = client.post(
            path,
            headers={
                **headers,
                "Idempotency-Key": f"idem_patch_stale_rev_{terminal.build_id[-16:]}",
            },
            content=json.dumps(
                {**body, "base_draft_revision": patch["base_draft_revision"] + 1},
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        assert stale_revision.status_code == 400, stale_revision.text
        assert stale_revision.json()["error"]["code"] == "INVALID_REQUEST"
        assert (
            _portal_call(client, _patch_decision_state, terminal, chain.session_id, chain.world_id)
            == state
        )

        stale_hash = client.post(
            path,
            headers={
                **headers,
                "Idempotency-Key": f"idem_patch_stale_hash_{terminal.build_id[-16:]}",
            },
            content=json.dumps(
                {**body, "base_draft_sha256": "0" * 64}, separators=(",", ":")
            ).encode("utf-8"),
        )
        assert stale_hash.status_code == 400, stale_hash.text
        assert stale_hash.json()["error"]["code"] == "INVALID_REQUEST"
        assert (
            _portal_call(client, _patch_decision_state, terminal, chain.session_id, chain.world_id)
            == state
        )

        evidence_id = proposal["feedback"]["evidence_refs"][0]["evidence_id"]
        original_evidence = _portal_call(client, _tamper_evidence_payload, terminal, evidence_id)
        proposal_response = client.get(proposal["links"]["self"], headers=terminal.headers)
        assert proposal_response.status_code == 500, proposal_response.text
        assert proposal_response.json()["error"]["code"] == "INVARIANT_VIOLATION"
        decision = client.post(
            path,
            headers={
                **headers,
                "Idempotency-Key": f"idem_patch_evidence_{terminal.build_id[-16:]}",
            },
            content=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        )
        assert decision.status_code == 500, decision.text
        assert decision.json()["error"]["code"] == "INVARIANT_VIOLATION"
        assert (
            _portal_call(client, _patch_decision_state, terminal, chain.session_id, chain.world_id)
            == state
        )

        _portal_call(
            client,
            _restore_evidence_payload,
            terminal,
            evidence_id,
            original_evidence,
        )
        restored_proposal = client.get(proposal["links"]["self"], headers=terminal.headers)
        assert restored_proposal.status_code == 200, restored_proposal.text

        draft_path = (
            f"/product-experience/v1/sessions/{chain.session_id}/skill-drafts/{patch['draft_id']}"
        )
        draft_response = client.get(draft_path, headers=terminal.headers)
        assert draft_response.status_code == 200, draft_response.text
        current_draft = draft_response.json()
        student_bundle = copy.deepcopy(current_draft["source_bundle"])
        entrypoint = student_bundle["entrypoint"]
        entrypoint_files = [item for item in student_bundle["files"] if item["path"] == entrypoint]
        assert len(entrypoint_files) == 1
        student_content = entrypoint_files[0]["content"] + "// student revision\n"
        entrypoint_files[0]["content"] = student_content
        entrypoint_files[0]["content_sha256"] = hashlib.sha256(
            student_content.encode("utf-8")
        ).hexdigest()
        student_update = client.put(
            draft_path,
            headers={
                **terminal.headers,
                "Idempotency-Key": f"idem_patch_student_edit_{terminal.build_id[-16:]}",
            },
            json={
                "session_id": chain.session_id,
                "draft_id": patch["draft_id"],
                "skill_id": patch["skill_id"],
                "content_ref": current_draft["content_ref"],
                "base_revision": current_draft["revision"],
                "base_draft_sha256": current_draft["draft_sha256"],
                "display_name": current_draft["display_name"],
                "source_bundle": student_bundle,
                "client_saved_at": proposal["updated_at"],
            },
        )
        assert student_update.status_code == 200, student_update.text
        student_draft = student_update.json()
        assert student_draft["revision"] == patch["base_draft_revision"] + 1
        assert student_draft["draft_sha256"] != patch["base_draft_sha256"]
        assert student_draft["last_applied_patch_id"] is None

        student_state = _portal_call(
            client, _patch_decision_state, terminal, chain.session_id, chain.world_id
        )
        assert student_state["decision"] is None
        assert student_state["receipt_count"] == 0
        assert student_state["assistance"] == state["assistance"] == []
        assert student_state["downstream"] == state["downstream"]
        assert len(student_state["revisions"]) == len(state["revisions"]) + 1
        assert student_state["revisions"][-1] == {
            "row_id": student_state["revisions"][-1]["row_id"],
            "parent_row_id": state["revisions"][-1]["row_id"],
            "revision": patch["base_draft_revision"] + 1,
            "draft_sha256": student_draft["draft_sha256"],
            "source_kind": "STUDENT",
            "patch_id": None,
        }
        assert student_state["workspace"]["skill_draft_refs"] == [
            {
                "draft_id": patch["draft_id"],
                "skill_id": patch["skill_id"],
                "revision": student_draft["revision"],
                "draft_sha256": student_draft["draft_sha256"],
                "url": draft_path,
            }
        ]

        stale_authority = client.post(
            path,
            headers={
                **headers,
                "Idempotency-Key": f"idem_patch_stale_authority_{terminal.build_id[-16:]}",
            },
            content=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        )
        assert stale_authority.status_code == 409, stale_authority.text
        assert stale_authority.json()["error"]["code"] == "CONTENT_VERSION_MISMATCH"
        assert (
            _portal_call(client, _patch_decision_state, terminal, chain.session_id, chain.world_id)
            == student_state
        )
        assert provider.calls == 1


def test_patch_vertical_manual_build_and_activation_preserve_patch_lineage(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _, operation = _activate_and_read_skill(client, terminal)
        operation = replace(
            operation,
            actor=ActorRef(
                terminal.tenant_id,
                terminal.actor_id,
                ActorType.STUDENT,
                ("game:player",),
            ),
        )
        chain = _materialize_failure_chain(client, terminal, operation, count=4)
        provider = _PatchReplyProvider()
        proposal, patch, accepted_draft, decision_id = _accept_patch_chain(client, chain, provider)
        after_accept = _portal_call(
            client, _patch_decision_state, terminal, chain.session_id, chain.world_id
        )
        assert after_accept["downstream"]["builds"] == 1
        assert after_accept["downstream"]["activations"] == 1
        assert provider.calls == 1

        patched_build = _execute_manual_build_from_draft(
            client,
            terminal,
            accepted_draft,
            tmp_path,
            monkeypatch,
        )
        build_response = client.get(
            f"/v1/skill-builds/{patched_build.build_id}",
            headers=patched_build.headers,
        )
        assert build_response.status_code == 200, build_response.text
        assert build_response.json()["status"] == "CERTIFIED"
        after_build = _portal_call(
            client, _patch_decision_state, terminal, chain.session_id, chain.world_id
        )
        assert after_build["downstream"]["builds"] == 2
        assert after_build["downstream"]["activations"] == 1
        assert after_build["downstream"]["runs"] == after_accept["downstream"]["runs"]
        assert (
            after_build["downstream"]["learner_jobs"] == after_accept["downstream"]["learner_jobs"]
        )
        assert after_build["downstream"]["world"] == after_accept["downstream"]["world"]

        activation_id = _activate_manual_build(client, patched_build)
        activation_response = client.get(
            f"/v1/skill-activations/{activation_id}", headers=patched_build.headers
        )
        assert activation_response.status_code == 200, activation_response.text
        lineage = _portal_call(
            client,
            _patch_build_activation_lineage,
            patched_build,
            chain.session_id,
            patch["draft_id"],
            patch["patch_id"],
            decision_id,
            activation_id,
        )
        assert lineage == {
            "draft_revision": accepted_draft["revision"],
            "draft_sha256": accepted_draft["draft_sha256"],
            "patch_id": patch["patch_id"],
            "decision_id": decision_id,
            "assistance": "SKILL_PATCH",
            "certification_id": patched_build.certification_id,
            "activation_id": activation_id,
        }
        after_activation = _portal_call(
            client, _patch_decision_state, terminal, chain.session_id, chain.world_id
        )
        assert after_activation["downstream"]["builds"] == 2
        assert after_activation["downstream"]["activations"] == 2
        assert after_activation["downstream"]["runs"] == after_build["downstream"]["runs"]
        assert (
            after_activation["downstream"]["learner_jobs"]
            == after_build["downstream"]["learner_jobs"]
        )
        assert after_activation["downstream"]["world"] == after_build["downstream"]["world"]
        assert provider.calls == 1
        assert proposal["patch_decision"]["decision"] == "ACCEPT"


def test_patch_vertical_successful_patched_run_is_assisted_not_independent(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "contract-release.json"
            ),
        ),
        database_url=database_url,
        world_presentation_enabled=True,
        skill_patch_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=True,
        )
        _, operation = _activate_and_read_skill(client, terminal)
        operation = replace(
            operation,
            actor=ActorRef(
                terminal.tenant_id,
                terminal.actor_id,
                ActorType.STUDENT,
                ("game:player",),
            ),
        )
        chain = _materialize_failure_chain(client, terminal, operation, count=4)
        provider = _PatchReplyProvider()
        _, _, accepted_draft, _ = _accept_patch_chain(client, chain, provider)
        patched_build = _execute_manual_build_from_draft(
            client,
            terminal,
            accepted_draft,
            tmp_path,
            monkeypatch,
        )
        activation_id = _activate_manual_build(client, patched_build)
        before = _portal_call(
            client,
            _learner_competency_state,
            patched_build,
            "world_navigation",
        )
        execution = _materialize_successful_patched_turn(
            client,
            chain,
            patched_build,
        )
        run_response = client.get(f"/v1/runs/{execution.run_id}", headers=patched_build.headers)
        assert run_response.status_code == 200, run_response.text
        assert run_response.json()["status"] == "SUCCEEDED"
        assisted = _portal_call(
            client,
            _patched_success_authority,
            patched_build,
            execution.fixture.claim.command_id,
            execution.run_id,
            activation_id,
            "world_navigation",
        )
        assert assisted["run_provenance"] == {
            "build_id": patched_build.build_id,
            "activation_id": activation_id,
            "assistance_authority": "SKILL_PATCH",
            "patch_id": accepted_draft["last_applied_patch_id"],
            "used_skill_patch": True,
        }
        assert assisted["frozen_assistance"]["used_skill_patch"] is True
        assert assisted["frozen_assistance"]["assistance_authority"] == "SKILL_PATCH"
        assert assisted["learner_evidence"]["assistance_level"] == 4
        assert assisted["learner_evidence"]["outcome"] == "SUCCESS"
        assert {
            "ASSISTANCE_BLOCKED_PROMOTION",
            "SKILL_PATCH_BLOCKED_PROMOTION",
        }.issubset(set(assisted["reason_codes"]))
        assert assisted["competency"]["evidence_stage"] == before["evidence_stage"]
        assert assisted["competency"]["assistance_level"] == 4
        assert assisted["world_revision"] == chain.world_revision + 1
        assert assisted["world_presentation_events"] == 8
        assert provider.calls == 1

        closed_state = _portal_call(
            client,
            _patch_decision_state,
            terminal,
            chain.session_id,
            chain.world_id,
        )
        interaction_snapshot = _portal_call(
            client,
            _pop_product_interaction,
            terminal,
            chain.session_id,
            chain.interactions[0]["interaction_id"],
        )
        corrupted = client.get(f"/v1/runs/{execution.run_id}", headers=patched_build.headers)
        assert corrupted.status_code == 500, corrupted.text
        assert corrupted.json()["error"]["code"] == "INVARIANT_VIOLATION"
        assert (
            _portal_call(
                client,
                _patch_decision_state,
                terminal,
                chain.session_id,
                chain.world_id,
            )["downstream"]
            == closed_state["downstream"]
        )
        _portal_call(client, _restore_product_interaction, terminal, interaction_snapshot)
        restored = client.get(f"/v1/runs/{execution.run_id}", headers=patched_build.headers)
        assert restored.status_code == 200, restored.text

        assert (
            _portal_call(
                client,
                _delete_learner_projection_receipt,
                terminal,
                execution.fixture.claim.command_id,
            )
            == 1
        )
        incomplete = client.get(f"/v1/runs/{execution.run_id}", headers=patched_build.headers)
        assert incomplete.status_code == 500, incomplete.text
        assert incomplete.json()["error"]["code"] == "INVARIANT_VIOLATION"
        assert (
            _portal_call(
                client,
                _patch_decision_state,
                terminal,
                chain.session_id,
                chain.world_id,
            )["downstream"]
            == closed_state["downstream"]
        )


def _accept_patch_chain(
    client: TestClient,
    chain: _FailureChain,
    provider: _PatchReplyProvider,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    proposal = _materialize_patch_proposal(client, chain, provider)[-1]
    patch = cast(dict[str, Any], proposal["skill_patch"])
    suffix = chain.terminal.build_id[-16:]
    decision_id = f"decision_manual_build_{suffix}"
    body = {
        "decision_id": decision_id,
        "session_id": chain.session_id,
        "turn_id": proposal["turn_id"],
        "interaction_id": proposal["interaction_id"],
        "expected_interaction_revision": proposal["interaction_revision"],
        "patch_id": patch["patch_id"],
        "patch_sha256": patch["patch_sha256"],
        "draft_id": patch["draft_id"],
        "skill_id": patch["skill_id"],
        "base_draft_revision": patch["base_draft_revision"],
        "base_draft_sha256": patch["base_draft_sha256"],
        "result_draft_sha256": patch["result_draft_sha256"],
        "decision": "ACCEPT",
        "reason_code": None,
        "decided_at": proposal["updated_at"],
    }
    response = client.post(
        (
            f"/product-experience/v1/sessions/{chain.session_id}/"
            f"agent-interactions/{proposal['interaction_id']}/patches/"
            f"{patch['patch_id']}/decision"
        ),
        headers={
            **chain.terminal.headers,
            "Content-Type": "application/json",
            "Idempotency-Key": f"idem_patch_manual_build_{suffix}",
        },
        content=json.dumps(body, separators=(",", ":")).encode("utf-8"),
    )
    assert response.status_code == 200, response.text
    decided = client.get(response.headers["location"], headers=chain.terminal.headers)
    assert decided.status_code == 200, decided.text
    proposal = decided.json()
    draft = client.get(
        (f"/product-experience/v1/sessions/{chain.session_id}/skill-drafts/{patch['draft_id']}"),
        headers=chain.terminal.headers,
    )
    assert draft.status_code == 200, draft.text
    return proposal, patch, draft.json(), decision_id


async def _pop_product_interaction(
    terminal: _TerminalBuild,
    session_id: str,
    interaction_id: str,
) -> dict[str, Any]:
    async with terminal.sessions() as session, session.begin():
        row = await session.scalar(
            select(ProductInteractionRow).where(
                ProductInteractionRow.tenant_id == terminal.tenant_id,
                ProductInteractionRow.actor_id == terminal.actor_id,
                ProductInteractionRow.session_id == session_id,
                ProductInteractionRow.interaction_id == interaction_id,
            )
        )
        assert row is not None
        snapshot = {
            "tenant_id": row.tenant_id,
            "actor_id": row.actor_id,
            "session_id": row.session_id,
            "interaction_id": row.interaction_id,
            "turn_id": row.turn_id,
            "sequence": row.sequence,
            "interaction_revision": row.interaction_revision,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "interaction_json": copy.deepcopy(row.interaction_json),
        }
        await session.delete(row)
        return snapshot


async def _tamper_product_interaction_message(
    terminal: _TerminalBuild,
    session_id: str,
    interaction_id: str,
) -> dict[str, Any]:
    async with terminal.sessions() as session, session.begin():
        row = await session.scalar(
            select(ProductInteractionRow)
            .where(
                ProductInteractionRow.tenant_id == terminal.tenant_id,
                ProductInteractionRow.actor_id == terminal.actor_id,
                ProductInteractionRow.session_id == session_id,
                ProductInteractionRow.interaction_id == interaction_id,
            )
            .with_for_update()
        )
        assert row is not None
        original = copy.deepcopy(row.interaction_json)
        corrupted = copy.deepcopy(original)
        corrupted["message"] = f"{corrupted.get('message', '')} [CORRUPT]"
        row.interaction_json = corrupted
        return original


async def _restore_product_interaction_message(
    terminal: _TerminalBuild,
    session_id: str,
    interaction_id: str,
    original: dict[str, Any],
) -> None:
    async with terminal.sessions() as session, session.begin():
        row = await session.scalar(
            select(ProductInteractionRow)
            .where(
                ProductInteractionRow.tenant_id == terminal.tenant_id,
                ProductInteractionRow.actor_id == terminal.actor_id,
                ProductInteractionRow.session_id == session_id,
                ProductInteractionRow.interaction_id == interaction_id,
            )
            .with_for_update()
        )
        assert row is not None
        row.interaction_json = copy.deepcopy(original)


async def _restore_product_interaction(
    terminal: _TerminalBuild,
    snapshot: dict[str, Any],
) -> None:
    async with terminal.sessions() as session, session.begin():
        session.add(ProductInteractionRow(**snapshot))


async def _delete_learner_projection_receipt(
    terminal: _TerminalBuild,
    command_id: str,
) -> int:
    async with terminal.sessions() as session, session.begin():
        learner_job = await session.scalar(
            select(LearnerProjectionJobRow).where(
                LearnerProjectionJobRow.tenant_id == terminal.tenant_id,
                LearnerProjectionJobRow.command_id == command_id,
            )
        )
        assert learner_job is not None
        result = await session.execute(
            delete(JobStepReceiptRow).where(
                JobStepReceiptRow.tenant_id == terminal.tenant_id,
                JobStepReceiptRow.job_id == learner_job.job_id,
                JobStepReceiptRow.step_name == "LEARNER_PROJECTION_COMMITTED",
            )
        )
        return int(result.rowcount or 0)


def _execute_manual_build_from_draft(
    client: TestClient,
    terminal: _TerminalBuild,
    draft: dict[str, Any],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> _TerminalBuild:
    previous = client.get(f"/v1/skill-builds/{terminal.build_id}", headers=terminal.headers)
    assert previous.status_code == 200, previous.text
    previous_build = previous.json()
    suffix = uuid4().hex[:20]
    payload = {
        "skill_id": draft["skill_id"],
        "display_name": draft["display_name"],
        "client_draft_revision": draft["revision"],
        "source_bundle": draft["source_bundle"],
        "compiler_profile": previous_build["artifact"]["compiler_profile"],
        "test_suite_version": previous_build["artifact"]["test_suite_version"],
        "requested_capabilities": previous_build["certification"]["capabilities"],
    }
    headers = {
        **terminal.headers,
        "X-Request-Id": f"req_patch_build_{suffix}",
        "X-Trace-Id": f"trace_patch_build_{suffix}",
        "X-Correlation-Id": f"corr_patch_build_{suffix}",
        "Idempotency-Key": f"idem_patch_build_{suffix}",
    }
    accepted = client.post("/v1/skill-builds", headers=headers, json=payload)
    assert accepted.status_code == 202, accepted.text
    command_id = cast(str, accepted.json()["command_id"])
    build_id = f"build_{hashlib.sha256(command_id.encode()).hexdigest()[:24]}"
    app = cast(Any, client.app)
    jobs = app.state.workflow_jobs
    claim = _portal_call(client, _claim_build, jobs, terminal.tenant_id)
    assert claim is not None
    assert claim.command_id == command_id
    assert claim.subject_id == build_id

    workspace_root = tmp_path / f"workspace_patch_{suffix}"
    artifact_root = tmp_path / f"artifacts_patch_{suffix}"
    workspace_root.mkdir()
    artifact_root.mkdir()
    staged = workspace_root / "student-patched-skill.bin"
    staged.write_bytes(f"artifact:patch:{suffix}".encode())
    artifact_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
    result = DockerBuildResult(
        build_id=build_id,
        status="SUCCEEDED",
        source_sha256=canonical_source_bundle_sha256(payload["source_bundle"]),
        compiler_profile=payload["compiler_profile"],
        compiler_version="gcc-14.2.0",
        test_suite_version=payload["test_suite_version"],
        build_identity=hashlib.sha256(f"identity:patch:{suffix}".encode()).hexdigest(),
        workspace=workspace_root,
        staged_artifact=staged,
        artifact_sha256=artifact_sha256,
        tests=(),
        diagnostics=(),
        failure=None,
    )

    def fake_build(_builder: DigestPinnedDockerCppBuilder, request: object) -> DockerBuildResult:
        assert getattr(request, "build_id") == build_id
        return result

    monkeypatch.setattr(DigestPinnedDockerCppBuilder, "build", fake_build)
    handler = BuildWorkflowHandler(
        session_factory=terminal.sessions,
        command_store=cast(PostgresCommandStore, app.state.game_queries._command_store),
        workflow_jobs=jobs,
        workspace_root=workspace_root,
        artifact_root=artifact_root,
        lease_seconds=600,
    )
    _portal_call(client, handler.execute, claim)
    identities = _portal_call(client, _terminal_identities, terminal.sessions, command_id)
    return _TerminalBuild(
        tenant_id=terminal.tenant_id,
        actor_id=terminal.actor_id,
        command_id=command_id,
        build_id=build_id,
        job_id=cast(str, identities["job_id"]),
        receipt_id=cast(str, identities["receipt_id"]),
        artifact_sha256=identities["artifact_sha256"],
        certification_id=identities["certification_id"],
        evidence_id=identities["evidence_id"],
        skill_id=draft["skill_id"],
        skill_version_id=identities["skill_version_id"],
        headers=headers,
        sessions=terminal.sessions,
        payload=payload,
    )


def _activate_manual_build(client: TestClient, terminal: _TerminalBuild) -> str:
    assert terminal.skill_version_id is not None
    world_id, agent_profile_id, revision, _ = _portal_call(client, _activation_scope, terminal)
    suffix = uuid4().hex[:20]
    headers = {
        **terminal.headers,
        "X-Request-Id": f"req_patch_activate_{suffix}",
        "X-Trace-Id": f"trace_patch_activate_{suffix}",
        "X-Correlation-Id": f"corr_patch_activate_{suffix}",
        "Idempotency-Key": f"idem_patch_activate_{suffix}",
    }
    accepted = client.post(
        f"/v1/skill-versions/{terminal.skill_version_id}/activations",
        headers=headers,
        json={
            "expected_registry_revision": revision,
            "activation_scope": {
                "world_id": world_id,
                "agent_profile_id": agent_profile_id,
            },
            "reason": "student activated the accepted Patch Build",
        },
    )
    assert accepted.status_code == 202, accepted.text
    app = cast(Any, client.app)
    jobs = app.state.workflow_jobs
    claim = _portal_call(client, _claim_activation, jobs, terminal.tenant_id)
    assert claim is not None
    handler = ControlWorkflowHandler(
        terminal.sessions,
        cast(PostgresCommandStore, app.state.game_queries._command_store),
        jobs,
        lease_seconds=600,
    )
    _portal_call(client, handler.execute, claim)
    command = client.get(f"/v1/commands/{claim.command_id}", headers=headers)
    assert command.status_code == 200, command.text
    return cast(str, claim.subject_id)


async def _patch_build_activation_lineage(
    terminal: _TerminalBuild,
    session_id: str,
    draft_id: str,
    patch_id: str,
    decision_id: str,
    activation_id: str,
) -> dict[str, Any]:
    async with terminal.sessions() as session:
        build = await session.scalar(
            select(SkillBuildProvenanceRow).where(
                SkillBuildProvenanceRow.build_id == terminal.build_id
            )
        )
        certification = await session.scalar(
            select(SkillCertificationProvenanceRow).where(
                SkillCertificationProvenanceRow.certification_id == terminal.certification_id
            )
        )
        activation = await session.scalar(
            select(SkillActivationProvenanceRow).where(
                SkillActivationProvenanceRow.activation_id == activation_id
            )
        )
        revision = await session.scalar(
            select(ProductDraftRevisionRow).where(
                ProductDraftRevisionRow.tenant_id == terminal.tenant_id,
                ProductDraftRevisionRow.actor_id == terminal.actor_id,
                ProductDraftRevisionRow.session_id == session_id,
                ProductDraftRevisionRow.draft_id == draft_id,
                ProductDraftRevisionRow.patch_id == patch_id,
            )
        )
        assert build is not None
        assert certification is not None
        assert activation is not None
        assert revision is not None
        assert build.provenance_kind == "IMMUTABLE_DRAFT"
        assert build.draft_revision_row_id == revision.draft_revision_row_id
        assert build.origin_accepted_revision_row_id == revision.draft_revision_row_id
        assert build.patch_id == patch_id
        assert build.patch_decision_id == decision_id
        assert certification.build_id == build.build_id
        assert certification.build_authority_sha256 == build.authority_sha256
        assert activation.build_id == build.build_id
        assert activation.build_authority_sha256 == build.authority_sha256
        assert activation.certification_id == certification.certification_id
        assert activation.certification_authority_sha256 == certification.authority_sha256
        return {
            "draft_revision": build.draft_revision,
            "draft_sha256": build.draft_sha256,
            "patch_id": build.patch_id,
            "decision_id": build.patch_decision_id,
            "assistance": build.assistance_authority,
            "certification_id": certification.certification_id,
            "activation_id": activation.activation_id,
        }


async def _learner_competency_state(
    terminal: _TerminalBuild,
    concept: str,
) -> dict[str, Any]:
    async with terminal.sessions() as session:
        learner = await session.scalar(
            select(LearnerProfileRow).where(
                LearnerProfileRow.tenant_id == terminal.tenant_id,
                LearnerProfileRow.actor_id == terminal.actor_id,
            )
        )
        assert learner is not None
        competency = cast(
            dict[str, Any],
            cast(dict[str, Any], learner.profile_json["competencies"])[concept],
        )
        return copy.deepcopy(competency)


def _materialize_successful_patched_turn(
    client: TestClient,
    chain: _FailureChain,
    terminal: _TerminalBuild,
) -> _FailedRunExecution:
    app = cast(Any, client.app)
    scope = _portal_call(client, _prepare_harvest_world, terminal)
    session_id, world_id, revision, world_sequence, turn_sequence, avatar_id = scope
    learner_id, task = _portal_call(client, _patch_projection_inputs, terminal)
    suffix = terminal.build_id[-16:]
    turn_id = f"turn_patch_success_{suffix}"
    operation = replace(
        chain.operation,
        request_id=f"req_patch_success_{suffix}",
        trace_id=f"trace_patch_success_{suffix}",
        correlation_id=f"corr_patch_success_{suffix}",
        command_id=f"cmd_patch_success_{suffix}",
        causation_id=None,
    )
    payload = {
        "turn_id": turn_id,
        "expected_world_revision": revision,
        "input": {
            "type": "MESSAGE",
            "text": "harvest all eight ready plots",
            "locale": "zh-CN",
        },
        "skill_bindings": [
            {
                "skill_id": terminal.skill_id,
                "skill_version_id": terminal.skill_version_id,
                "artifact_sha256": terminal.artifact_sha256,
                "certification_id": terminal.certification_id,
            }
        ],
        "client_state": {
            "last_event_sequence": world_sequence,
            "client_turn_sequence": turn_sequence,
        },
    }
    accepted = _portal_call(
        client,
        app.state.agent_turns.accept,
        session_id,
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        f"idem_patch_success_{suffix}",
        operation,
    )
    assert accepted.__class__.__name__ == "Success", accepted
    execution = _portal_call(
        client,
        _execute_successful_run,
        terminal,
        app.state.workflow_jobs,
        app.state.game_queries._command_store,
        cast(str, accepted.value.command.command_id),
        turn_id,
        session_id,
        world_id,
        revision,
        avatar_id,
    )
    assert execution.result.run.task_success is True
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
    return execution


async def _prepare_harvest_world(
    terminal: _TerminalBuild,
) -> tuple[str, str, int, int, int, str]:
    async with terminal.sessions() as session, session.begin():
        owner = await session.scalar(
            select(AgentSessionRow)
            .where(
                AgentSessionRow.tenant_id == terminal.tenant_id,
                AgentSessionRow.actor_id == terminal.actor_id,
                AgentSessionRow.status == "ACTIVE",
            )
            .with_for_update()
        )
        assert owner is not None
        world = await session.scalar(
            select(WorldSnapshotRow)
            .where(
                WorldSnapshotRow.tenant_id == terminal.tenant_id,
                WorldSnapshotRow.actor_id == terminal.actor_id,
                WorldSnapshotRow.world_id == owner.world_id,
            )
            .with_for_update()
        )
        assert world is not None
        avatar_id = f"avatar_{terminal.actor_id}"
        state = {
            "clock": {"day": 1, "minute_of_day": 480, "tick": 10},
            "avatar": {
                "entity_id": avatar_id,
                "position": {"x": 0, "y": 0},
                "energy": 100,
            },
            "inventory": [],
            "plots": [
                {
                    "plot_id": f"plot_{index:04d}",
                    "position": {"x": index, "y": 0},
                    "soil_state": "TILLED",
                    "hydration": 0,
                    "crop": {
                        "crop_type": "tomato",
                        "growth_stage": 2,
                        "planted_at_tick": 10,
                        "ready_to_harvest": True,
                    },
                    "last_updated_event_sequence": world.last_event_sequence,
                }
                for index in range(1, 9)
            ],
            "agents": [],
        }
        snapshot = copy.deepcopy(world.snapshot_json)
        snapshot["state"] = state
        snapshot["state_hash"] = canonical_json_sha256(state)
        world.state_hash = cast(str, snapshot["state_hash"])
        world.snapshot_json = snapshot
        stream = await session.scalar(
            select(WorldStreamRow)
            .where(
                WorldStreamRow.tenant_id == terminal.tenant_id,
                WorldStreamRow.stream_id == f"world:{owner.world_id}",
            )
            .with_for_update()
        )
        if stream is None:
            session.add(
                WorldStreamRow(
                    stream_id=f"world:{owner.world_id}",
                    tenant_id=terminal.tenant_id,
                    world_id=owner.world_id,
                    last_sequence=world.last_event_sequence,
                )
            )
        else:
            assert stream.world_id == owner.world_id
            assert stream.last_sequence == world.last_event_sequence
        return (
            owner.session_id,
            owner.world_id,
            world.revision,
            world.last_event_sequence,
            int(owner.session_json["last_turn_sequence"]) + 1,
            avatar_id,
        )


async def _execute_successful_run(
    terminal: _TerminalBuild,
    jobs: Any,
    command_store: Any,
    command_id: str,
    turn_id: str,
    session_id: str,
    world_id: str,
    expected_revision: int,
    avatar_id: str,
) -> _FailedRunExecution:
    assert terminal.artifact_sha256 is not None
    assert terminal.certification_id is not None
    assert terminal.skill_version_id is not None
    claim = await _claim_workflow_eventually(
        jobs,
        tenant_id=terminal.tenant_id,
        worker_id=f"worker_patch_success_{terminal.build_id[-16:]}",
        lease_seconds=600,
        operation="EXECUTE_AGENT_TURN",
    )
    assert claim is not None and claim.command_id == command_id
    async with terminal.sessions() as session:
        command_row = await session.scalar(
            select(CommandRow).where(CommandRow.command_id == command_id)
        )
        turn = await session.scalar(
            select(AgentTurnRow).where(AgentTurnRow.command_id == command_id)
        )
        assert command_row is not None and turn is not None
        command = command_record_from_data(command_row.record_json)
    context = OperationContext(
        request_id=command.request_context.request_id,
        correlation_id=command.request_context.correlation_id,
        trace_id=command.request_context.trace_id,
        requested_at=command.request_context.requested_at,
        actor=command.request_context.actor,
        content_ref=command.request_context.content_ref,
        schema_version=command.request_context.schema_version,
        command_id=command_id,
        causation_id=None,
    )
    skill_ref = SkillRef(
        terminal.skill_id,
        terminal.skill_version_id,
        terminal.artifact_sha256,
        terminal.certification_id,
    )
    invocation_id = side_effect_execution_id(command_id, turn_id)
    arguments = {"length": 8}
    request_sha256 = skill_invocation_request_sha256(
        tenant_id=terminal.tenant_id,
        invocation_id=invocation_id,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        world_id=world_id,
        expected_world_revision=expected_revision,
        skill_ref=skill_ref,
        arguments=arguments,
    )
    request = SkillInvocationRequest(
        invocation_id=invocation_id,
        tenant_id=terminal.tenant_id,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        world_id=world_id,
        expected_world_revision=expected_revision,
        skill_ref=skill_ref,
        arguments=arguments,
        request_sha256=request_sha256,
    )
    rules_version = cast(str, command.versions.world_rules_version)
    rules = WorldRules(
        command.request_context.content_ref.version,
        8,
        0,
        31,
        0,
        31,
        2,
        8,
    )
    engine = WorldEngine()
    invocation = PostgresFencedSkillInvocation(
        session_factory=terminal.sessions,
        commands=command_store,
        jobs=jobs,
        claim=claim,
        sandbox=_SuccessfulHarvestSandbox(avatar_id, expected_revision),
        limits=SandboxLimits(
            cpu_ms=1_000,
            wall_ms=1_000,
            memory_bytes=64 * 1024 * 1024,
            max_intents=8,
            max_output_bytes=16_384,
            max_processes=4,
        ),
        versions=command.versions,
        world_uow=PostgresWorldUnitOfWork(
            terminal.sessions,
            {rules_version: rules},
            world_engine=engine,
        ),
        world_engine=engine,
        rules_by_version={rules_version: rules},
        lease_seconds=600,
        skill_patch_enabled=True,
    )
    result = await invocation.invoke(request, context)
    async with terminal.sessions() as session:
        world_row = await session.scalar(
            select(WorldSnapshotRow).where(WorldSnapshotRow.world_id == world_id)
        )
    assert world_row is not None
    return _FailedRunExecution(
        run_id=result.run.run_id,
        fixture=_ExecutionFixture(
            sessions=terminal.sessions,
            jobs=jobs,
            claim=claim,
            context=context,
            request=request,
            world=world_snapshot_from_data(world_row.snapshot_json),
            versions=command.versions,
        ),
        result=result,
    )


class _SuccessfulHarvestSandbox:
    def __init__(self, avatar_id: str, expected_revision: int) -> None:
        self._avatar_id = avatar_id
        self._expected_revision = expected_revision

    async def run(self, request: Any, context: OperationContext) -> Success[Any]:
        del context
        now = datetime.now(UTC)
        return Success(
            SandboxRunResult(
                run_id=request.run_id,
                started_at=now,
                finished_at=now,
                action_intents=tuple(
                    HarvestIntent(
                        f"intent_harvest_{index:04d}_{request.run_id[-8:]}",
                        self._avatar_id,
                        self._expected_revision,
                        f"plot_{index:04d}",
                    )
                    for index in range(1, 9)
                ),
                stdout_ref=None,
                stderr_ref=None,
                usage=SandboxUsage(cpu_ms=2, wall_ms=3, peak_memory_bytes=4096),
                evidence_refs=(),
            )
        )

    async def reconcile(self, request: Any, context: OperationContext) -> None:
        del request, context
        raise AssertionError("successful patched Sandbox must not reconcile")


async def _patched_success_authority(
    terminal: _TerminalBuild,
    command_id: str,
    run_id: str,
    activation_id: str,
    concept: str,
) -> dict[str, Any]:
    async with terminal.sessions() as session:
        run = await session.scalar(
            select(SkillRunProvenanceRow).where(SkillRunProvenanceRow.run_id == run_id)
        )
        build = await session.scalar(
            select(SkillBuildProvenanceRow).where(
                SkillBuildProvenanceRow.build_id == terminal.build_id
            )
        )
        learner_job = await session.scalar(
            select(LearnerProjectionJobRow).where(
                LearnerProjectionJobRow.tenant_id == terminal.tenant_id,
                LearnerProjectionJobRow.command_id == command_id,
            )
        )
        learner = await session.scalar(
            select(LearnerProfileRow).where(
                LearnerProfileRow.tenant_id == terminal.tenant_id,
                LearnerProfileRow.actor_id == terminal.actor_id,
            )
        )
        projection_receipt = (
            await session.scalar(
                select(JobStepReceiptRow).where(
                    JobStepReceiptRow.tenant_id == terminal.tenant_id,
                    JobStepReceiptRow.job_id == learner_job.job_id,
                    JobStepReceiptRow.step_name == "LEARNER_PROJECTION_COMMITTED",
                )
            )
            if learner_job is not None
            else None
        )
        world = await session.scalar(
            select(WorldSnapshotRow).where(
                WorldSnapshotRow.tenant_id == terminal.tenant_id,
                WorldSnapshotRow.actor_id == terminal.actor_id,
            )
        )
        presentation_events = list(
            (
                await session.scalars(
                    select(WorldPresentationEventRow).where(
                        WorldPresentationEventRow.tenant_id == terminal.tenant_id,
                        WorldPresentationEventRow.actor_id == terminal.actor_id,
                        WorldPresentationEventRow.command_id == command_id,
                        WorldPresentationEventRow.run_id == run_id,
                    )
                )
            ).all()
        )
        assert run is not None
        assert build is not None
        assert learner_job is not None
        assert learner is not None
        assert projection_receipt is not None
        assert world is not None
        assert run.build_id == build.build_id
        assert run.build_authority_sha256 == build.authority_sha256
        assert run.activation_id == activation_id
        frozen = cast(dict[str, Any], learner_job.projection_json["assistance"])
        committed = cast(
            dict[str, Any],
            projection_receipt.receipt_json["learner"]["projection"],
        )
        evidence = await session.scalar(
            select(EvidenceRow).where(
                EvidenceRow.tenant_id == terminal.tenant_id,
                EvidenceRow.evidence_id == committed["evidence_id"],
            )
        )
        assert evidence is not None
        competency = cast(
            dict[str, Any],
            cast(dict[str, Any], learner.profile_json["competencies"])[concept],
        )
        return {
            "run_provenance": {
                "build_id": run.build_id,
                "activation_id": run.activation_id,
                "assistance_authority": run.assistance_authority,
                "patch_id": build.patch_id,
                "used_skill_patch": run.assistance_authority == "SKILL_PATCH",
            },
            "frozen_assistance": copy.deepcopy(frozen),
            "learner_evidence": copy.deepcopy(evidence.evidence_json["payload"]),
            "reason_codes": copy.deepcopy(committed["reason_codes"]),
            "competency": copy.deepcopy(competency),
            "world_revision": world.revision,
            "world_presentation_events": len(presentation_events),
        }


def _materialize_patch_proposal(
    client: TestClient,
    chain: _FailureChain,
    provider: _PatchReplyProvider,
) -> tuple[dict[str, Any], ...]:
    app = cast(Any, client.app)
    suffix = chain.terminal.build_id[-16:]
    selected = chain.interactions[-1]
    turn_id = f"turn_patch_request_{suffix}"
    operation = replace(
        chain.operation,
        request_id=f"req_patch_request_{suffix}",
        trace_id=f"trace_patch_request_{suffix}",
        correlation_id=f"corr_patch_request_{suffix}",
        command_id=f"cmd_patch_request_{suffix}",
        causation_id=None,
    )
    payload = {
        "turn_id": turn_id,
        "expected_world_revision": chain.world_revision,
        "input": {
            "type": "UI_ACTION",
            "action_id": "request_ai_patch",
            "selection_id": selected["interaction_id"],
        },
        "skill_bindings": [
            {
                "skill_id": chain.terminal.skill_id,
                "skill_version_id": chain.terminal.skill_version_id,
                "artifact_sha256": chain.terminal.artifact_sha256,
                "certification_id": chain.terminal.certification_id,
            }
        ],
        "client_state": {
            "last_event_sequence": chain.world_sequence,
            "client_turn_sequence": chain.next_turn_sequence,
        },
    }
    accepted = _portal_call(
        client,
        app.state.agent_turns.accept,
        chain.session_id,
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        f"idem_patch_request_{suffix}",
        operation,
    )
    assert accepted.__class__.__name__ == "Success", accepted
    claim = _portal_call(
        client,
        _claim_patch_turn,
        app.state.workflow_jobs,
        chain.terminal.tenant_id,
    )
    assert claim is not None
    versions = accepted.value.command.versions
    handler = TurnWorkflowHandler(
        session_factory=chain.terminal.sessions,
        commands=app.state.game_queries._command_store,
        jobs=app.state.workflow_jobs,
        provider=provider,
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
    _portal_call(client, handler.execute, claim)
    response = client.get(
        f"/product-experience/v1/sessions/{chain.session_id}/agent-interactions",
        headers=chain.terminal.headers,
    )
    assert response.status_code == 200, response.text
    return tuple(cast(list[dict[str, Any]], response.json()["interactions"]))


async def _claim_patch_turn(jobs: Any, tenant_id: str) -> Any:
    return await _claim_workflow_eventually(
        jobs,
        tenant_id=tenant_id,
        worker_id="worker_patch_vertical",
        lease_seconds=600,
        operation="EXECUTE_AGENT_TURN",
    )


async def _patch_provider_receipt_names(
    terminal: _TerminalBuild, turn_id: str
) -> tuple[str, ...]:
    async with terminal.sessions() as session:
        job = await session.scalar(
            select(WorkflowJobRow).where(
                WorkflowJobRow.tenant_id == terminal.tenant_id,
                WorkflowJobRow.operation == "EXECUTE_AGENT_TURN",
                WorkflowJobRow.subject_type == "AGENT_TURN",
                WorkflowJobRow.subject_id == turn_id,
            )
        )
        assert job is not None
        names = (
            await session.scalars(
                select(JobStepReceiptRow.step_name).where(
                    JobStepReceiptRow.tenant_id == terminal.tenant_id,
                    JobStepReceiptRow.job_id == job.job_id,
                    JobStepReceiptRow.step_name.like("PATCH_PROVIDER_%"),
                )
            )
        ).all()
        return tuple(sorted(names))


async def _append_patch_reconciliation_receipts(
    terminal: _TerminalBuild, turn_id: str
) -> None:
    async with terminal.sessions() as session, session.begin():
        job = await session.scalar(
            select(WorkflowJobRow)
            .where(
                WorkflowJobRow.tenant_id == terminal.tenant_id,
                WorkflowJobRow.operation == "EXECUTE_AGENT_TURN",
                WorkflowJobRow.subject_type == "AGENT_TURN",
                WorkflowJobRow.subject_id == turn_id,
            )
            .with_for_update()
        )
        assert job is not None
        job.attempt = 2
        job.fencing_token = 2
        retried_steps = list(
            (
                await session.scalars(
                    select(JobStepReceiptRow).where(
                        JobStepReceiptRow.tenant_id == terminal.tenant_id,
                        JobStepReceiptRow.job_id == job.job_id,
                        JobStepReceiptRow.step_name.in_(
                            (
                                "PATCH_PROVIDER_RESULT_01",
                                "PATCH_PROPOSAL_DERIVED",
                                "TURN_COMPLETED",
                            )
                        ),
                    )
                )
            ).all()
        )
        assert len(retried_steps) == 3
        for step in retried_steps:
            step.fencing_token = 2
        step_name = "WORKER_RECONCILE_1"
        receipt = {
            "code": "WORKFLOW_EXECUTION_FAILED",
            "exception_type": "DurableLlmDispatchUnknown",
            "attempt": 1,
            "retry_after_seconds": 1,
        }
        session.add(
            JobStepReceiptRow(
                receipt_id=workflow_step_receipt_id(
                    terminal.tenant_id, job.job_id, step_name
                ),
                tenant_id=terminal.tenant_id,
                job_id=job.job_id,
                step_name=step_name,
                fencing_token=1,
                input_sha256=job.request_sha256,
                output_sha256=workflow_receipt_sha256(receipt),
                receipt_json=receipt,
                completed_at=job.updated_at,
            )
        )


async def _append_patch_invalid_extra_receipt(
    terminal: _TerminalBuild, turn_id: str
) -> None:
    async with terminal.sessions() as session, session.begin():
        job = await session.scalar(
            select(WorkflowJobRow).where(
                WorkflowJobRow.tenant_id == terminal.tenant_id,
                WorkflowJobRow.operation == "EXECUTE_AGENT_TURN",
                WorkflowJobRow.subject_type == "AGENT_TURN",
                WorkflowJobRow.subject_id == turn_id,
            )
        )
        assert job is not None
        step_name = "WORKER_FAILURE_3"
        receipt = {
            "code": "WORKFLOW_EXECUTION_FAILED",
            "exception_type": "WorkflowBoundaryError",
            "attempt": 3,
        }
        session.add(
            JobStepReceiptRow(
                receipt_id=workflow_step_receipt_id(
                    terminal.tenant_id, job.job_id, step_name
                ),
                tenant_id=terminal.tenant_id,
                job_id=job.job_id,
                step_name=step_name,
                fencing_token=3,
                input_sha256=job.request_sha256,
                output_sha256=workflow_receipt_sha256(receipt),
                receipt_json=receipt,
                completed_at=job.updated_at,
            )
        )


async def _patch_decision_state(
    terminal: _TerminalBuild, session_id: str, world_id: str
) -> dict[str, Any]:
    async with terminal.sessions() as session:
        scope = (
            ProductDraftRow.tenant_id == terminal.tenant_id,
            ProductDraftRow.actor_id == terminal.actor_id,
            ProductDraftRow.session_id == session_id,
        )
        draft = await session.scalar(select(ProductDraftRow).where(*scope))
        revisions = list(
            (
                await session.scalars(
                    select(ProductDraftRevisionRow)
                    .where(
                        ProductDraftRevisionRow.tenant_id == terminal.tenant_id,
                        ProductDraftRevisionRow.actor_id == terminal.actor_id,
                        ProductDraftRevisionRow.session_id == session_id,
                    )
                    .order_by(ProductDraftRevisionRow.revision)
                )
            ).all()
        )
        assistance = list((await session.scalars(select(ProductDraftRevisionAssistanceRow))).all())
        decisions = list(
            (
                await session.scalars(
                    select(ProductSkillPatchDecisionRow).where(
                        ProductSkillPatchDecisionRow.tenant_id == terminal.tenant_id,
                        ProductSkillPatchDecisionRow.actor_id == terminal.actor_id,
                        ProductSkillPatchDecisionRow.session_id == session_id,
                    )
                )
            ).all()
        )
        receipts = list(
            (
                await session.scalars(
                    select(ProductPatchDecisionReceiptRow).where(
                        ProductPatchDecisionReceiptRow.tenant_id == terminal.tenant_id,
                        ProductPatchDecisionReceiptRow.actor_id == terminal.actor_id,
                    )
                )
            ).all()
        )
        workspace = await session.scalar(
            select(ProductWorkspaceRow).where(
                ProductWorkspaceRow.tenant_id == terminal.tenant_id,
                ProductWorkspaceRow.actor_id == terminal.actor_id,
                ProductWorkspaceRow.session_id == session_id,
            )
        )
        builds = list(
            (
                await session.scalars(
                    select(SkillBuildRow).where(
                        SkillBuildRow.tenant_id == terminal.tenant_id,
                        SkillBuildRow.actor_id == terminal.actor_id,
                    )
                )
            ).all()
        )
        activations = list(
            (
                await session.scalars(
                    select(SkillActivationRow).where(
                        SkillActivationRow.tenant_id == terminal.tenant_id,
                        SkillActivationRow.actor_id == terminal.actor_id,
                    )
                )
            ).all()
        )
        runs = list(
            (
                await session.scalars(
                    select(RunRow).where(
                        RunRow.tenant_id == terminal.tenant_id,
                        RunRow.actor_id == terminal.actor_id,
                        RunRow.session_id == session_id,
                    )
                )
            ).all()
        )
        evidence = list(
            (
                await session.scalars(
                    select(EvidenceRow).where(
                        EvidenceRow.tenant_id == terminal.tenant_id,
                        EvidenceRow.actor_id == terminal.actor_id,
                    )
                )
            ).all()
        )
        learner_jobs = list(
            (
                await session.scalars(
                    select(LearnerProjectionJobRow).where(
                        LearnerProjectionJobRow.tenant_id == terminal.tenant_id,
                        LearnerProjectionJobRow.actor_id == terminal.actor_id,
                        LearnerProjectionJobRow.session_id == session_id,
                    )
                )
            ).all()
        )
        world = await session.scalar(
            select(WorldSnapshotRow).where(
                WorldSnapshotRow.tenant_id == terminal.tenant_id,
                WorldSnapshotRow.actor_id == terminal.actor_id,
                WorldSnapshotRow.world_id == world_id,
            )
        )
        return {
            "draft": dict(draft.draft_json) if draft is not None else None,
            "workspace": dict(workspace.workspace_json) if workspace is not None else None,
            "revisions": [
                {
                    "row_id": row.draft_revision_row_id,
                    "parent_row_id": row.parent_revision_row_id,
                    "revision": row.revision,
                    "draft_sha256": row.draft_sha256,
                    "source_kind": row.source_kind,
                    "patch_id": row.patch_id,
                }
                for row in revisions
            ],
            "assistance": [
                {
                    "row_id": row.draft_revision_row_id,
                    "origin_row_id": row.origin_accepted_revision_row_id,
                    "patch_id": row.patch_id,
                    "decision_id": row.patch_decision_id,
                    "inherited": row.inherited,
                }
                for row in assistance
                if row.draft_revision_row_id
                in {revision.draft_revision_row_id for revision in revisions}
            ],
            "decision": (
                {
                    "decision_id": decisions[0].decision_id,
                    "patch_id": decisions[0].patch_id,
                    "base_row_id": decisions[0].base_draft_revision_row_id,
                    "accepted_row_id": decisions[0].accepted_draft_revision_row_id,
                    "decision": decisions[0].decision,
                }
                if len(decisions) == 1
                else None
            ),
            "receipt_count": len(receipts),
            "downstream": {
                "builds": len(builds),
                "activations": len(activations),
                "runs": len(runs),
                "evidence": len(evidence),
                "learner_jobs": len(learner_jobs),
                "world": (
                    world.revision,
                    world.last_event_sequence,
                    world.state_hash,
                )
                if world is not None
                else None,
            },
        }


async def _tamper_accepted_decision_projection_to_reject(
    terminal: _TerminalBuild,
    session_id: str,
    interaction_id: str,
) -> None:
    async with terminal.sessions() as session, session.begin():
        interaction = await session.scalar(
            select(ProductInteractionRow).where(
                ProductInteractionRow.tenant_id == terminal.tenant_id,
                ProductInteractionRow.actor_id == terminal.actor_id,
                ProductInteractionRow.session_id == session_id,
                ProductInteractionRow.interaction_id == interaction_id,
            )
        )
        receipt = await session.scalar(
            select(ProductPatchDecisionReceiptRow).where(
                ProductPatchDecisionReceiptRow.tenant_id == terminal.tenant_id,
                ProductPatchDecisionReceiptRow.actor_id == terminal.actor_id,
                ProductPatchDecisionReceiptRow.interaction_id == interaction_id,
            )
        )
        assert interaction is not None
        assert receipt is not None
        corrupted_receipt = copy.deepcopy(receipt.receipt_json)
        corrupted_receipt.update(
            {
                "decision": "REJECT",
                "reason_code": "STUDENT_REJECTED",
                "draft_updated": False,
                "draft_revision_after": corrupted_receipt["draft_revision_before"],
                "draft_sha256_after": corrupted_receipt["draft_sha256_before"],
            }
        )
        corrupted_interaction = copy.deepcopy(interaction.interaction_json)
        corrupted_interaction["patch_decision"] = corrupted_receipt
        interaction.interaction_json = corrupted_interaction
        receipt.receipt_json = corrupted_receipt


async def _hide_accepted_decision_projection(
    terminal: _TerminalBuild,
    session_id: str,
    interaction_id: str,
) -> None:
    async with terminal.sessions() as session, session.begin():
        interaction = await session.scalar(
            select(ProductInteractionRow).where(
                ProductInteractionRow.tenant_id == terminal.tenant_id,
                ProductInteractionRow.actor_id == terminal.actor_id,
                ProductInteractionRow.session_id == session_id,
                ProductInteractionRow.interaction_id == interaction_id,
            )
        )
        receipt = await session.scalar(
            select(ProductPatchDecisionReceiptRow).where(
                ProductPatchDecisionReceiptRow.tenant_id == terminal.tenant_id,
                ProductPatchDecisionReceiptRow.actor_id == terminal.actor_id,
                ProductPatchDecisionReceiptRow.interaction_id == interaction_id,
            )
        )
        assert interaction is not None
        assert receipt is not None
        hidden_interaction = copy.deepcopy(interaction.interaction_json)
        hidden_interaction["patch_decision"] = None
        hidden_interaction["interaction_revision"] = 1
        hidden_interaction["updated_at"] = hidden_interaction["created_at"]
        interaction.interaction_revision = 1
        interaction.updated_at = interaction.created_at
        interaction.interaction_json = hidden_interaction
        await session.delete(receipt)


async def _tamper_evidence_payload(
    terminal: _TerminalBuild,
    evidence_id: str,
) -> dict[str, Any]:
    async with terminal.sessions() as session, session.begin():
        evidence = await session.scalar(
            select(EvidenceRow).where(
                EvidenceRow.tenant_id == terminal.tenant_id,
                EvidenceRow.actor_id == terminal.actor_id,
                EvidenceRow.evidence_id == evidence_id,
            )
        )
        assert evidence is not None
        document = copy.deepcopy(evidence.evidence_json)
        original = copy.deepcopy(document)
        payload = cast(dict[str, Any], document["payload"])
        payload["int2_corruption_marker"] = True
        evidence.evidence_json = document
        return original


async def _restore_evidence_payload(
    terminal: _TerminalBuild,
    evidence_id: str,
    document: dict[str, Any],
) -> None:
    async with terminal.sessions() as session, session.begin():
        evidence = await session.scalar(
            select(EvidenceRow).where(
                EvidenceRow.tenant_id == terminal.tenant_id,
                EvidenceRow.actor_id == terminal.actor_id,
                EvidenceRow.evidence_id == evidence_id,
            )
        )
        assert evidence is not None
        evidence.evidence_json = copy.deepcopy(document)


def _materialize_failure_chain(
    client: TestClient,
    terminal: _TerminalBuild,
    operation: OperationContext,
    *,
    count: int,
) -> _FailureChain:
    app = cast(Any, client.app)
    session_id, world_id, revision, world_sequence, next_turn_sequence = _portal_call(
        client, _prepare_failed_run, terminal
    )
    learner_id, task = _portal_call(client, _patch_projection_inputs, terminal)
    suffix = terminal.build_id[-16:]
    for failure_count in range(1, count + 1):
        turn_id = f"turn_patch_{failure_count}_{suffix}"
        turn_operation = replace(
            operation,
            request_id=f"req_patch_{failure_count}_{suffix}",
            trace_id=f"trace_patch_{failure_count}_{suffix}",
            correlation_id=f"corr_patch_{failure_count}_{suffix}",
            command_id=f"cmd_patch_{failure_count}_{suffix}",
            causation_id=None,
        )
        payload = {
            "turn_id": turn_id,
            "expected_world_revision": revision,
            "input": {
                "type": "MESSAGE",
                "text": "produce the same exact failure",
                "locale": "zh-CN",
            },
            "skill_bindings": [
                {
                    "skill_id": terminal.skill_id,
                    "skill_version_id": terminal.skill_version_id,
                    "artifact_sha256": terminal.artifact_sha256,
                    "certification_id": terminal.certification_id,
                }
            ],
            "client_state": {
                "last_event_sequence": world_sequence,
                "client_turn_sequence": next_turn_sequence,
            },
        }
        accepted = _portal_call(
            client,
            app.state.agent_turns.accept,
            session_id,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            f"idem_patch_{failure_count}_{suffix}",
            turn_operation,
        )
        assert accepted.__class__.__name__ == "Success", accepted
        execution = _portal_call(
            client,
            _execute_failed_run,
            terminal,
            app.state.workflow_jobs,
            app.state.game_queries._command_store,
            str(accepted.value.command.command_id),
            turn_id,
            session_id,
            world_id,
            revision,
        )
        authority, outcome, decision = _portal_call(
            client,
            _failure_projection_authority,
            execution.fixture,
            execution.result,
            learner_id,
            task,
            failure_count,
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
        next_turn_sequence += 1
    response = client.get(
        f"/product-experience/v1/sessions/{session_id}/agent-interactions",
        headers=terminal.headers,
    )
    assert response.status_code == 200, response.text
    interactions = tuple(cast(list[dict[str, Any]], response.json()["interactions"]))
    assert len(interactions) == count
    return _FailureChain(
        terminal=terminal,
        operation=operation,
        session_id=session_id,
        world_id=world_id,
        world_revision=revision,
        world_sequence=world_sequence,
        next_turn_sequence=next_turn_sequence,
        interactions=interactions,
    )


async def _active_activation_identity(
    terminal: _TerminalBuild,
) -> tuple[str, str, int]:
    async with terminal.sessions() as session:
        head = await session.scalar(
            select(RegistryHeadRow).where(
                RegistryHeadRow.tenant_id == terminal.tenant_id,
                RegistryHeadRow.actor_id == terminal.actor_id,
                RegistryHeadRow.revision > 0,
            )
        )
        assert head is not None
        activation = await session.scalar(
            select(SkillActivationRow).where(
                SkillActivationRow.tenant_id == terminal.tenant_id,
                SkillActivationRow.actor_id == terminal.actor_id,
                SkillActivationRow.registry_revision == head.revision,
            )
        )
        assert activation is not None
        job = await session.scalar(
            select(WorkflowJobRow).where(
                WorkflowJobRow.tenant_id == terminal.tenant_id,
                WorkflowJobRow.subject_id == activation.activation_id,
                WorkflowJobRow.operation == "ACTIVATE_SKILL_VERSION",
            )
        )
        assert job is not None
        return activation.activation_id, job.command_id, head.revision


async def _replace_entry_authority(
    terminal: _TerminalBuild,
    revision: int,
    replacement: str,
) -> str:
    async with terminal.sessions() as session, session.begin():
        row = await session.scalar(
            select(RegistryEntryRow)
            .where(
                RegistryEntryRow.tenant_id == terminal.tenant_id,
                RegistryEntryRow.actor_id == terminal.actor_id,
                RegistryEntryRow.revision == revision,
            )
            .with_for_update()
        )
        assert row is not None
        value = copy.deepcopy(row.entry_json)
        original = cast(str, value["authority_id"])
        value["authority_id"] = replacement
        row.entry_json = value
        return original


async def _coordinated_activation_rewrite(
    terminal: _TerminalBuild,
    revision: int,
    tamper: str,
) -> None:
    async with terminal.sessions() as session, session.begin():
        entry = await session.scalar(
            select(RegistryEntryRow)
            .where(
                RegistryEntryRow.tenant_id == terminal.tenant_id,
                RegistryEntryRow.actor_id == terminal.actor_id,
                RegistryEntryRow.revision == revision,
            )
            .with_for_update()
        )
        activation = await session.scalar(
            select(SkillActivationRow).where(
                SkillActivationRow.tenant_id == terminal.tenant_id,
                SkillActivationRow.actor_id == terminal.actor_id,
                SkillActivationRow.registry_revision == revision,
            )
        )
        assert entry is not None and activation is not None
        job = await session.scalar(
            select(WorkflowJobRow)
            .where(
                WorkflowJobRow.tenant_id == terminal.tenant_id,
                WorkflowJobRow.subject_id == activation.activation_id,
                WorkflowJobRow.operation == "ACTIVATE_SKILL_VERSION",
            )
            .with_for_update()
        )
        assert job is not None
        receipt = await session.scalar(
            select(JobStepReceiptRow)
            .where(
                JobStepReceiptRow.tenant_id == terminal.tenant_id,
                JobStepReceiptRow.job_id == job.job_id,
                JobStepReceiptRow.step_name == "REGISTRY_ACTIVATED",
            )
            .with_for_update()
        )
        assert receipt is not None
        if tamper == "entry_and_receipt":
            entry_json = copy.deepcopy(entry.entry_json)
            entry_json["authority_id"] = "authority_coordinated_rewrite"
            entry.entry_json = entry_json
            entry.entry_sha256 = canonical_json_sha256(entry_json)
            receipt_json = copy.deepcopy(receipt.receipt_json)
            receipt_json["entry_sha256"] = entry.entry_sha256
            receipt.receipt_json = receipt_json
            receipt.output_sha256 = workflow_receipt_sha256(receipt_json)
        elif tamper == "job_and_receipt_input":
            job.request_sha256 = "a" * 64
            receipt.input_sha256 = job.request_sha256
        elif tamper == "job_json":
            job_json = copy.deepcopy(job.job_json)
            job_json["reason"] = "coordinated-but-unsealed"
            job.job_json = job_json
        else:  # pragma: no cover - parametrization owns the closed set.
            raise AssertionError(tamper)


async def _rewrite_build_job_trace(terminal: _TerminalBuild) -> None:
    async with terminal.sessions() as session, session.begin():
        job = await session.scalar(
            select(WorkflowJobRow)
            .where(
                WorkflowJobRow.tenant_id == terminal.tenant_id,
                WorkflowJobRow.subject_id == terminal.build_id,
                WorkflowJobRow.operation == "CREATE_SKILL_BUILD",
            )
            .with_for_update()
        )
        assert job is not None
        job_json = copy.deepcopy(job.job_json)
        context = cast(dict[str, Any], job_json["request_context"])
        context["trace_id"] = "trace_coordinated_build_job_drift"
        job.job_json = job_json


async def _rewrite_all_build_context_traces(terminal: _TerminalBuild) -> None:
    replacement = "trace_coordinated_build_authority_rewrite"
    async with terminal.sessions() as session, session.begin():
        command = await session.scalar(
            select(CommandRow).where(CommandRow.command_id == terminal.command_id).with_for_update()
        )
        build = await session.scalar(
            select(SkillBuildRow)
            .where(SkillBuildRow.build_id == terminal.build_id)
            .with_for_update()
        )
        evidence = await session.scalar(
            select(EvidenceRow)
            .where(EvidenceRow.evidence_id == terminal.evidence_id)
            .with_for_update()
        )
        job = await session.scalar(
            select(WorkflowJobRow)
            .where(
                WorkflowJobRow.tenant_id == terminal.tenant_id,
                WorkflowJobRow.subject_id == terminal.build_id,
                WorkflowJobRow.operation == "CREATE_SKILL_BUILD",
            )
            .with_for_update()
        )
        assert command is not None and build is not None
        assert evidence is not None and job is not None
        for row, attribute in (
            (command, "record_json"),
            (build, "build_json"),
            (evidence, "evidence_json"),
            (job, "job_json"),
        ):
            value = copy.deepcopy(getattr(row, attribute))
            context = cast(dict[str, Any], value["request_context"])
            context["trace_id"] = replacement
            setattr(row, attribute, value)


async def _prepare_failed_run(
    terminal: _TerminalBuild,
) -> tuple[str, str, int, int, int]:
    async with terminal.sessions() as session, session.begin():
        owner = await session.scalar(
            select(AgentSessionRow)
            .where(
                AgentSessionRow.tenant_id == terminal.tenant_id,
                AgentSessionRow.actor_id == terminal.actor_id,
                AgentSessionRow.status == "ACTIVE",
            )
            .with_for_update()
        )
        assert owner is not None
        world = await session.scalar(
            select(WorldSnapshotRow).where(WorldSnapshotRow.world_id == owner.world_id)
        )
        assert world is not None
        await session.execute(
            delete(AgentTurnRow).where(
                AgentTurnRow.tenant_id == terminal.tenant_id,
                AgentTurnRow.session_id == owner.session_id,
                AgentTurnRow.command_id.like("cmd_turn_runtime_%"),
            )
        )
        return (
            owner.session_id,
            owner.world_id,
            world.revision,
            world.last_event_sequence,
            int(owner.session_json["last_turn_sequence"]) + 1,
        )


async def _patch_projection_inputs(
    terminal: _TerminalBuild,
) -> tuple[str, dict[str, Any]]:
    async with terminal.sessions() as session, session.begin():
        authority = await session.scalar(
            select(LaunchAuthorityRow).where(
                LaunchAuthorityRow.tenant_id == terminal.tenant_id,
                LaunchAuthorityRow.actor_id == terminal.actor_id,
                LaunchAuthorityRow.active.is_(True),
            )
        )
        assert authority is not None
        content = await session.scalar(
            select(ProductContentUnitRow).where(
                ProductContentUnitRow.tenant_id == terminal.tenant_id,
                ProductContentUnitRow.unit_id == authority.content_unit_id,
                ProductContentUnitRow.version == authority.content_version,
            )
        )
        assert content is not None
        content_json = copy.deepcopy(content.content_json)
        task = content_json.get("task")
        assert isinstance(task, dict)
        task.setdefault("name", "INT2 failed run")
        task.setdefault("goal", "Close one exact failed run.")
        task.setdefault("story", {"opening": "A deterministic failed Run needs assistance."})
        task.setdefault("knowledge_points", ["world_navigation"])
        task.setdefault("hint_policy", {"max_level": 4})
        content_json["task"] = task
        content.content_json = content_json
        from walnut_backend.adapters.postgres.models import LearnerProfileRow

        learner = await session.scalar(
            select(LearnerProfileRow).where(
                LearnerProfileRow.tenant_id == terminal.tenant_id,
                LearnerProfileRow.learner_id == authority.learner_id,
            )
        )
        assert learner is not None
        profile = copy.deepcopy(learner.profile_json)
        profile["model_version"] = LEARNER_PROJECTION_POLICY_VERSION
        learner.profile_json = profile
        learner.profile_sha256 = canonical_json_sha256(profile)
        return authority.learner_id, copy.deepcopy(task)


async def _failure_projection_authority(
    fixture: _ExecutionFixture,
    result: Any,
    learner_id: str,
    task: dict[str, Any],
    failure_count: int,
) -> tuple[Any, Any, Any]:
    return await _build_terminal_projection_authority(
        fixture,
        result,
        learner_id=learner_id,
        task=task,
        failure_count=failure_count,
        record_final_authority=True,
    )


async def _finish_failed_interaction_only(
    fixture: _ExecutionFixture,
    authority: Any,
    outcome: Any,
    decision: Any,
    result: Any,
) -> None:
    from walnut_backend.adapters.postgres.command_store import PostgresCommandStore

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


async def _execute_failed_run(
    terminal: _TerminalBuild,
    jobs: Any,
    command_store: Any,
    command_id: str,
    turn_id: str,
    session_id: str,
    world_id: str,
    expected_revision: int,
) -> _FailedRunExecution:
    assert terminal.artifact_sha256 is not None
    assert terminal.certification_id is not None
    assert terminal.skill_version_id is not None
    suffix = terminal.build_id[-20:]
    claim = await _claim_workflow_eventually(
        jobs,
        tenant_id=terminal.tenant_id,
        worker_id=f"worker_run_receipt_{suffix}",
        lease_seconds=60,
        operation="EXECUTE_AGENT_TURN",
    )
    assert claim is not None and claim.command_id == command_id
    async with terminal.sessions() as session:
        command_row = await session.scalar(
            select(CommandRow).where(CommandRow.command_id == command_id)
        )
        turn = await session.scalar(
            select(AgentTurnRow).where(AgentTurnRow.command_id == command_id)
        )
        assert command_row is not None and turn is not None
        command = command_record_from_data(command_row.record_json)
    context = OperationContext(
        request_id=command.request_context.request_id,
        correlation_id=command.request_context.correlation_id,
        trace_id=command.request_context.trace_id,
        requested_at=command.request_context.requested_at,
        actor=command.request_context.actor,
        content_ref=command.request_context.content_ref,
        schema_version=command.request_context.schema_version,
        command_id=command_id,
        causation_id=None,
    )
    skill_ref = SkillRef(
        terminal.skill_id,
        terminal.skill_version_id,
        terminal.artifact_sha256,
        terminal.certification_id,
    )
    invocation_id = side_effect_execution_id(command_id, turn_id)
    arguments = {"length": 8}
    request_sha256 = skill_invocation_request_sha256(
        tenant_id=terminal.tenant_id,
        invocation_id=invocation_id,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        world_id=world_id,
        expected_world_revision=expected_revision,
        skill_ref=skill_ref,
        arguments=arguments,
    )
    request = SkillInvocationRequest(
        invocation_id=invocation_id,
        tenant_id=terminal.tenant_id,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        world_id=world_id,
        expected_world_revision=expected_revision,
        skill_ref=skill_ref,
        arguments=arguments,
        request_sha256=request_sha256,
    )
    engine = WorldEngine()
    invocation = PostgresFencedSkillInvocation(
        session_factory=terminal.sessions,
        commands=command_store,
        jobs=jobs,
        claim=claim,
        sandbox=_FailedRunSandbox(),
        limits=SandboxLimits(
            cpu_ms=1_000,
            wall_ms=1_000,
            memory_bytes=64 * 1024 * 1024,
            max_intents=4,
            max_output_bytes=4_096,
            max_processes=4,
        ),
        versions=command.versions,
        world_uow=PostgresWorldUnitOfWork(
            terminal.sessions,
            {"rules-1": WorldRules("1.0.0", 4, 0, 10, 0, 10, 2, 0)},
            world_engine=engine,
        ),
        world_engine=engine,
        rules_by_version={"rules-1": WorldRules("1.0.0", 4, 0, 10, 0, 10, 2, 0)},
        lease_seconds=60,
    )
    result = await invocation.invoke(request, context)
    async with terminal.sessions() as session:
        world_row = await session.scalar(
            select(WorldSnapshotRow).where(WorldSnapshotRow.world_id == world_id)
        )
    assert world_row is not None
    return _FailedRunExecution(
        run_id=result.run.run_id,
        fixture=_ExecutionFixture(
            sessions=terminal.sessions,
            jobs=jobs,
            claim=claim,
            context=context,
            request=request,
            world=world_snapshot_from_data(world_row.snapshot_json),
            versions=command.versions,
        ),
        result=result,
    )


async def _rewrite_invocation_receipt_id(terminal: _TerminalBuild, run_id: str) -> None:
    async with terminal.sessions() as session, session.begin():
        run = await session.scalar(select(RunRow).where(RunRow.run_id == run_id))
        assert run is not None
        job = await session.scalar(
            select(WorkflowJobRow).where(WorkflowJobRow.command_id == run.command_id)
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
        receipt.receipt_id = f"receipt_tampered_{run_id[-20:]}"


async def _fourth_failure_receipt_snapshot(
    terminal: _TerminalBuild,
    session_id: str,
) -> tuple[str, dict[str, Any], str]:
    async with terminal.sessions() as session:
        turn = await session.scalar(
            select(AgentTurnRow)
            .where(
                AgentTurnRow.tenant_id == terminal.tenant_id,
                AgentTurnRow.actor_id == terminal.actor_id,
                AgentTurnRow.session_id == session_id,
            )
            .order_by(AgentTurnRow.turn_sequence.desc())
            .limit(1)
        )
        assert turn is not None
        run = await session.scalar(
            select(RunRow).where(
                RunRow.tenant_id == terminal.tenant_id,
                RunRow.actor_id == terminal.actor_id,
                RunRow.command_id == turn.command_id,
            )
        )
        assert run is not None
        job = await session.scalar(
            select(WorkflowJobRow).where(
                WorkflowJobRow.tenant_id == terminal.tenant_id,
                WorkflowJobRow.command_id == turn.command_id,
            )
        )
        assert job is not None
        receipt = await session.scalar(
            select(JobStepReceiptRow).where(
                JobStepReceiptRow.tenant_id == terminal.tenant_id,
                JobStepReceiptRow.job_id == job.job_id,
                JobStepReceiptRow.step_name == "SKILL_INVOKED",
            )
        )
        assert receipt is not None
        return (
            run.run_id,
            copy.deepcopy(receipt.receipt_json),
            receipt.output_sha256,
        )


class _FailedRunSandbox:
    async def run(self, request: Any, context: OperationContext) -> Failure:
        del request, context
        return Failure(
            ContractError(
                code="SANDBOX_RUNTIME_ERROR",
                category=ErrorCategory.SANDBOX,
                retryable=False,
                user_message_key="sandbox.runtime_error",
                stage="SANDBOX",
                message="fixture failed",
                details={"reason": "EXIT_NONZERO"},
            )
        )

    async def reconcile(self, request: Any, context: OperationContext) -> None:
        del request, context
        return None
