"""Terminal Command and Build reads close every durable source row."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_build import (
    CPP20_SAFE_V1_FLAGS,
    BuildDiagnostic,
    DigestPinnedDockerCppBuilder,
    DockerBuildFailure,
    DockerBuildResult,
    canonical_source_bundle_sha256,
)
from yaya_agent_contracts import OperationContext, SkillRef, canonical_json_sha256
from yaya_agent_runtime import (
    AgentToolInputError,
    GameEvent,
    PackagedRoleConfigProvider,
    SessionSnapshot,
    SkillSnapshot,
    TaskSnapshot,
    TurnContext,
    WorldSummary,
    build_default_tool_registry,
)

from tests.integration.test_product_workspace_lifecycle import (
    _accept_session,
    _dispose,
)
from walnut_backend.adapters.postgres.agent_runtime import (
    AgentRuntimeAuthorityError,
    PostgresAgentRuntimeReads,
)
from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.models import (
    AgentProfileRow,
    AgentSessionRow,
    AgentTurnRow,
    BuildPolicyRow,
    CommandRow,
    EvidenceRow,
    JobStepReceiptRow,
    LaunchAuthorityRow,
    LearnerProfileRow,
    ProductContentUnitRow,
    RegistryHeadRow,
    SkillArtifactRow,
    SkillBuildProvenanceRow,
    SkillBuildRow,
    SkillBuildTerminalAuthorityRow,
    SkillCertificationRow,
    WorkflowJobRow,
    WorldSnapshotRow,
    request_context_from_data,
)
from walnut_backend.adapters.postgres.session import create_session_factory
from walnut_backend.adapters.postgres.skill_provenance import (
    build_terminal_authority_sha256,
    build_terminal_command_authority_sha256,
    build_terminal_receipt_authority_sha256,
    build_terminal_workflow_authority_sha256,
)
from walnut_backend.adapters.postgres.workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
    workflow_step_receipt_id,
)
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings
from walnut_backend.workers.build_worker import BuildWorkflowHandler
from walnut_backend.workers.control_worker import ControlWorkflowHandler

BACKEND_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class _TerminalBuild:
    tenant_id: str
    actor_id: str
    command_id: str
    build_id: str
    job_id: str
    receipt_id: str
    artifact_sha256: str | None
    certification_id: str | None
    evidence_id: str | None
    skill_id: str
    skill_version_id: str | None
    payload: dict[str, Any]
    headers: dict[str, str]
    sessions: async_sessionmaker[AsyncSession]


def test_certified_parameter_schema_drives_real_runtime_and_tamper_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
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
        assert terminal.skill_version_id is not None
        snapshot, operation = _activate_and_read_skill(client, terminal)
        assert snapshot.request_context.request_id != operation.request_id
        assert snapshot.request_context.trace_id != operation.trace_id
        assert snapshot.request_context.actor == operation.actor
        assert snapshot.request_context.content_ref == operation.content_ref
        decorated = cast(Mapping[str, object], snapshot.parameter_schema)
        certification_extension = cast(Mapping[str, object], decorated["x-yaya-certification"])
        assert certification_extension["build_id"] == terminal.build_id
        assert certification_extension["skill_id"] == terminal.skill_id
        assert certification_extension["skill_version_id"] == terminal.skill_version_id
        assert certification_extension["artifact_sha256"] == terminal.artifact_sha256
        assert certification_extension["certification_id"] == terminal.certification_id
        assert tuple(cast(tuple[str, ...], certification_extension["capabilities"])) == (
            "WORLD_READ",
        )
        context = _turn_context(snapshot, operation)
        tools = build_default_tool_registry(cast(Any, object()), cast(Any, object()))
        allowed = PackagedRoleConfigProvider.load().get("xiaohutao").allowed_tools
        definitions = tools.model_definitions("xiaohutao", allowed, context)
        invoke = next(item for item in definitions if item["name"] == "invoke_skill")
        input_schema = cast(Mapping[str, object], invoke["input_schema"])
        properties = cast(Mapping[str, object], input_schema["properties"])
        arguments = cast(Mapping[str, object], properties["arguments"])
        assert "x-yaya-certification" not in arguments
        assert tuple(cast(tuple[str, ...], arguments["required"])) == ("length",)
        parameter_properties = cast(Mapping[str, object], arguments["properties"])
        assert dict(cast(Mapping[str, object], parameter_properties["length"])) == {
            "type": "integer",
            "const": 8,
        }
        assert tools.validate_call(
            role="xiaohutao",
            allowed_names=allowed,
            name="invoke_skill",
            arguments={"skill_id": "bound_skill", "arguments": {"length": 8}},
            turn_context=context,
        )
        with pytest.raises(AgentToolInputError):
            tools.validate_call(
                role="xiaohutao",
                allowed_names=allowed,
                name="invoke_skill",
                arguments={"skill_id": "bound_skill", "arguments": {"length": 7}},
                turn_context=context,
            )

        _portal_call(client, _tamper_certified_parameter_schema, terminal)
        with pytest.raises(
            AgentRuntimeAuthorityError,
            match="parameter schema drifted|not active in the exact durable scope",
        ):
            _portal_call(
                client,
                PostgresAgentRuntimeReads(terminal.sessions).get_bound_skill,
                snapshot.ref,
                operation,
            )
        _assert_build_read(client, terminal, 500)


def test_certified_build_and_command_reads_fail_closed_on_single_field_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
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
        assert terminal.artifact_sha256 is not None
        assert terminal.certification_id is not None
        assert terminal.evidence_id is not None
        _assert_command_read(client, terminal, 200)
        _assert_build_read(client, terminal, 200)

        _tamper_json_and_assert(
            client,
            terminal,
            CommandRow,
            "command_id",
            terminal.command_id,
            "record_json",
            ("result", "resource_url"),
            f"/v1/skill-builds/build_corrupt_{uuid4().hex}",
            command=True,
        )
        _tamper_column_and_assert(
            client,
            terminal,
            WorkflowJobRow,
            "job_id",
            terminal.job_id,
            "subject_id",
            f"build_corrupt_{uuid4().hex}",
            command=True,
        )
        _tamper_column_and_assert(
            client,
            terminal,
            WorkflowJobRow,
            "job_id",
            terminal.job_id,
            "phase",
            "CORRUPT_PHASE",
            command=True,
        )
        _tamper_json_and_assert(
            client,
            terminal,
            WorkflowJobRow,
            "job_id",
            terminal.job_id,
            "job_json",
            ("build_id",),
            f"build_corrupt_{uuid4().hex}",
            command=True,
        )
        _tamper_column_and_assert(
            client,
            terminal,
            WorkflowJobRow,
            "job_id",
            terminal.job_id,
            "last_error_json",
            {"code": "CORRUPT_TERMINAL_ERROR"},
            command=True,
        )
        _tamper_json_and_assert(
            client,
            terminal,
            SkillBuildRow,
            "build_id",
            terminal.build_id,
            "build_json",
            ("versions", "api_version"),
            "9.9.9",
            command=True,
        )
        _tamper_json_and_assert(
            client,
            terminal,
            JobStepReceiptRow,
            "receipt_id",
            terminal.receipt_id,
            "receipt_json",
            ("evidence_id",),
            f"evidence_corrupt_{uuid4().hex}",
        )
        _tamper_json_and_assert(
            client,
            terminal,
            SkillArtifactRow,
            "artifact_sha256",
            terminal.artifact_sha256,
            "metadata_json",
            ("schema_version",),
            "9.9.9",
        )
        _tamper_json_and_assert(
            client,
            terminal,
            SkillCertificationRow,
            "certification_id",
            terminal.certification_id,
            "certification_json",
            ("skill_version_id",),
            f"skillver_corrupt_{uuid4().hex}",
        )
        _tamper_json_and_assert(
            client,
            terminal,
            EvidenceRow,
            "evidence_id",
            terminal.evidence_id,
            "evidence_json",
            ("source", "world_id"),
            f"world_corrupt_{uuid4().hex}",
        )
        _tamper_json_and_assert(
            client,
            terminal,
            SkillBuildRow,
            "build_id",
            terminal.build_id,
            "build_json",
            ("phases", 2, "diagnostic_codes"),
            ["CORRUPT_DIAGNOSTIC"],
        )


def test_rejected_build_closes_command_error_to_job_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=False,
        )
        _assert_command_read(client, terminal, 200)
        _assert_build_read(client, terminal, 200)
        _tamper_json_and_assert(
            client,
            terminal,
            CommandRow,
            "command_id",
            terminal.command_id,
            "record_json",
            ("error", "details", "pipeline_code"),
            "CORRUPT_PIPELINE_CODE",
        )
        _tamper_json_and_assert(
            client,
            terminal,
            SkillBuildRow,
            "build_id",
            terminal.build_id,
            "build_json",
            ("failure", "details", "pipeline_code"),
            "CORRUPT_PIPELINE_CODE",
        )


@pytest.mark.parametrize("succeed", [False, True], ids=["rejected", "certified"])
def test_terminal_build_writer_and_idempotent_replay_close_execution_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    succeed: bool,
) -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        terminal = _execute_build(
            client,
            database_url=database_url,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            succeed=succeed,
        )
        expected_status = "CERTIFIED" if succeed else "REJECTED"
        before = _portal_call(client, _terminal_build_seal_state, terminal)
        assert before["terminal_status"] == expected_status
        assert before["terminal_count"] == 1
        assert before["job_count"] == 1

        replay = client.post(
            "/v1/skill-builds",
            headers=terminal.headers,
            json=terminal.payload,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["command_id"] == terminal.command_id
        assert _portal_call(client, _terminal_build_seal_state, terminal) == before

        _portal_call(
            client,
            _replace_column,
            terminal.sessions,
            JobStepReceiptRow,
            "receipt_id",
            terminal.receipt_id,
            "input_sha256",
            "f" * 64,
        )
        refused = client.post(
            "/v1/skill-builds",
            headers=terminal.headers,
            json=terminal.payload,
        )
        assert refused.status_code == 500, refused.text
        assert refused.json()["error"]["code"] == "INVARIANT_VIOLATION"
        after = _portal_call(client, _terminal_build_seal_state, terminal)
        assert after["terminal_count"] == 1
        assert after["job_count"] == 1


def test_accepted_build_requires_terminal_authority_to_be_absent() -> None:
    database_url = _database_url()
    settings = replace(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=BACKEND_ROOT / "contract-release.json",
        ),
        database_url=database_url,
    )
    with TestClient(create_app(settings)) as client:
        suffix = uuid4().hex[:20]
        session_fixture = _portal_call(client, _accept_student_session, database_url)
        _portal_call(client, session_fixture.handler.execute, session_fixture.claim)
        _portal_call(client, _dispose, session_fixture.sessions)
        _portal_call(
            client,
            _enable_worker_policy,
            database_url,
            session_fixture.tenant_id,
            session_fixture.actor_id,
        )
        starter = session_fixture.starter
        payload = {
            "skill_id": starter["skill_id"],
            "display_name": "Accepted build authority",
            "client_draft_revision": 1,
            "source_bundle": starter["source_bundle"],
            "compiler_profile": starter["compiler_profile"],
            "test_suite_version": starter["test_suite_version"],
            "requested_capabilities": ["WORLD_READ"],
        }
        headers = {
            "Authorization": (f"Bearer {session_fixture.tenant_id}:{session_fixture.actor_id}"),
            "X-Request-Id": f"req_accepted_{suffix}",
            "X-Trace-Id": f"trace_accepted_{suffix}",
            "X-Correlation-Id": f"corr_accepted_{suffix}",
            "X-Schema-Version": "1.0.0",
            "Idempotency-Key": f"idem_accepted_{suffix}",
        }
        accepted = client.post("/v1/skill-builds", headers=headers, json=payload)
        assert accepted.status_code == 202, accepted.text
        command_id = str(accepted.json()["command_id"])
        build_id = f"build_{hashlib.sha256(command_id.encode()).hexdigest()[:24]}"
        app = cast(FastAPI, client.app)
        jobs = cast(PostgresWorkflowJobStore, app.state.workflow_jobs)
        assert client.get(f"/v1/skill-builds/{build_id}", headers=headers).status_code == 200
        before = _portal_call(
            client,
            _accepted_build_seal_counts,
            jobs._sessions,
            build_id,
            command_id,
        )
        assert before == {"build_count": 1, "job_count": 1, "terminal_count": 0}

        replay = client.post("/v1/skill-builds", headers=headers, json=payload)
        assert replay.status_code == 202, replay.text
        assert replay.json()["command_id"] == command_id
        assert (
            _portal_call(
                client,
                _accepted_build_seal_counts,
                jobs._sessions,
                build_id,
                command_id,
            )
            == before
        )

        _portal_call(
            client,
            _inject_premature_build_terminal_authority,
            jobs._sessions,
            build_id,
        )
        refused_read = client.get(f"/v1/skill-builds/{build_id}", headers=headers)
        assert refused_read.status_code == 500, refused_read.text
        refused_replay = client.post("/v1/skill-builds", headers=headers, json=payload)
        assert refused_replay.status_code == 500, refused_replay.text
        assert _portal_call(
            client,
            _accepted_build_seal_counts,
            jobs._sessions,
            build_id,
            command_id,
        ) == {"build_count": 1, "job_count": 1, "terminal_count": 1}


def _execute_build(
    client: TestClient,
    *,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    succeed: bool,
) -> _TerminalBuild:
    suffix = uuid4().hex[:20]
    session_fixture = _portal_call(client, _accept_student_session, database_url)
    _portal_call(client, session_fixture.handler.execute, session_fixture.claim)
    _portal_call(client, _dispose, session_fixture.sessions)
    tenant_id = session_fixture.tenant_id
    actor_id = session_fixture.actor_id
    _portal_call(client, _enable_worker_policy, database_url, tenant_id, actor_id)
    starter = session_fixture.starter
    source_bundle = cast(dict[str, Any], starter["source_bundle"])
    payload = {
        "skill_id": starter["skill_id"],
        "display_name": "Terminal closure skill",
        "client_draft_revision": 1,
        "source_bundle": source_bundle,
        "compiler_profile": starter["compiler_profile"],
        "test_suite_version": starter["test_suite_version"],
        "requested_capabilities": ["WORLD_READ"],
    }
    headers = {
        "Authorization": f"Bearer {tenant_id}:{actor_id}",
        "X-Request-Id": f"req_terminal_{suffix}",
        "X-Trace-Id": f"trace_terminal_{suffix}",
        "X-Correlation-Id": f"corr_terminal_{suffix}",
        "X-Schema-Version": "1.0.0",
        "Idempotency-Key": f"idem_terminal_{suffix}",
    }
    accepted = client.post("/v1/skill-builds", headers=headers, json=payload)
    assert accepted.status_code == 202, accepted.text
    command_id = str(accepted.json()["command_id"])
    build_id = f"build_{hashlib.sha256(command_id.encode()).hexdigest()[:24]}"
    app = cast(FastAPI, client.app)
    jobs = cast(PostgresWorkflowJobStore, app.state.workflow_jobs)
    sessions = jobs._sessions
    claim = _portal_call(client, _claim_build, jobs, tenant_id)
    assert claim is not None
    assert claim.command_id == command_id
    assert claim.subject_id == build_id

    workspace_root = tmp_path / f"workspace_{suffix}"
    artifact_root = tmp_path / f"artifacts_{suffix}"
    workspace_root.mkdir()
    artifact_root.mkdir()
    staged = workspace_root / "student-skill.bin"
    staged.write_bytes(f"artifact:{suffix}".encode())
    artifact_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
    source_sha256 = canonical_source_bundle_sha256(payload["source_bundle"])
    build_identity = hashlib.sha256(f"identity:{suffix}".encode()).hexdigest()
    diagnostic = BuildDiagnostic("CPP_COMPILE_ERROR", "synthetic compiler error")
    result = DockerBuildResult(
        build_id=build_id,
        status="SUCCEEDED" if succeed else "FAILED",
        source_sha256=source_sha256,
        compiler_profile="YAYA_CPP20_SAFE_V1",
        compiler_version="gcc-14.2.0",
        test_suite_version=cast(str, starter["test_suite_version"]),
        build_identity=build_identity,
        workspace=workspace_root,
        staged_artifact=staged if succeed else None,
        artifact_sha256=artifact_sha256 if succeed else None,
        tests=(),
        diagnostics=() if succeed else (diagnostic,),
        failure=(
            None
            if succeed
            else DockerBuildFailure(
                code="CPP_COMPILE_FAILED",
                stage="COMPILE",
                diagnostics=(diagnostic,),
            )
        ),
    )

    def fake_build(_builder: DigestPinnedDockerCppBuilder, request: object) -> DockerBuildResult:
        assert getattr(request, "build_id") == build_id
        return result

    monkeypatch.setattr(DigestPinnedDockerCppBuilder, "build", fake_build)
    command_store = cast(PostgresCommandStore, app.state.game_queries._command_store)
    handler = BuildWorkflowHandler(
        session_factory=sessions,
        command_store=command_store,
        workflow_jobs=jobs,
        workspace_root=workspace_root,
        artifact_root=artifact_root,
        lease_seconds=60,
    )
    _portal_call(client, handler.execute, claim)
    identities = _portal_call(client, _terminal_identities, sessions, command_id)
    return _TerminalBuild(
        tenant_id=tenant_id,
        actor_id=actor_id,
        command_id=command_id,
        build_id=build_id,
        job_id=identities["job_id"],
        receipt_id=identities["receipt_id"],
        artifact_sha256=identities["artifact_sha256"],
        certification_id=identities["certification_id"],
        evidence_id=identities["evidence_id"],
        skill_id=payload["skill_id"],
        skill_version_id=identities["skill_version_id"],
        payload=payload,
        headers=headers,
        sessions=sessions,
    )


async def _terminal_build_seal_state(terminal: _TerminalBuild) -> dict[str, object]:
    async with terminal.sessions() as session:
        seal = await session.scalar(
            select(SkillBuildTerminalAuthorityRow).where(
                SkillBuildTerminalAuthorityRow.build_id == terminal.build_id
            )
        )
        seals = list(
            (
                await session.scalars(
                    select(SkillBuildTerminalAuthorityRow).where(
                        SkillBuildTerminalAuthorityRow.build_id == terminal.build_id
                    )
                )
            ).all()
        )
        jobs = list(
            (
                await session.scalars(
                    select(WorkflowJobRow).where(WorkflowJobRow.command_id == terminal.command_id)
                )
            ).all()
        )
        assert seal is not None
        return {
            "terminal_count": len(seals),
            "terminal_status": seal.terminal_status,
            "terminal_authority_sha256": seal.authority_sha256,
            "job_count": len(jobs),
        }


async def _accepted_build_seal_counts(
    sessions: async_sessionmaker[AsyncSession],
    build_id: str,
    command_id: str,
) -> dict[str, int]:
    async with sessions() as session:
        builds = list(
            (
                await session.scalars(
                    select(SkillBuildRow).where(SkillBuildRow.build_id == build_id)
                )
            ).all()
        )
        jobs = list(
            (
                await session.scalars(
                    select(WorkflowJobRow).where(WorkflowJobRow.command_id == command_id)
                )
            ).all()
        )
        terminals = list(
            (
                await session.scalars(
                    select(SkillBuildTerminalAuthorityRow).where(
                        SkillBuildTerminalAuthorityRow.build_id == build_id
                    )
                )
            ).all()
        )
        return {
            "build_count": len(builds),
            "job_count": len(jobs),
            "terminal_count": len(terminals),
        }


async def _inject_premature_build_terminal_authority(
    sessions: async_sessionmaker[AsyncSession],
    build_id: str,
) -> None:
    async with sessions() as session, session.begin():
        build = await session.scalar(
            select(SkillBuildRow).where(SkillBuildRow.build_id == build_id)
        )
        provenance = await session.scalar(
            select(SkillBuildProvenanceRow).where(SkillBuildProvenanceRow.build_id == build_id)
        )
        command = await session.scalar(
            select(CommandRow).where(
                CommandRow.command_id == (build.command_id if build is not None else "")
            )
        )
        workflow = await session.scalar(
            select(WorkflowJobRow)
            .where(
                WorkflowJobRow.job_id
                == (provenance.workflow_job_id if provenance is not None else "")
            )
            .with_for_update()
        )
        assert build is not None
        assert provenance is not None
        assert command is not None
        assert workflow is not None
        workflow.fencing_token = 1
        receipt_output = {"build_id": build.build_id, "premature": True}
        receipt = JobStepReceiptRow(
            receipt_id=workflow_step_receipt_id(
                build.tenant_id,
                workflow.job_id,
                "BUILD_REJECTED",
            ),
            tenant_id=build.tenant_id,
            job_id=workflow.job_id,
            step_name="BUILD_REJECTED",
            fencing_token=workflow.fencing_token,
            input_sha256=workflow.request_sha256,
            output_sha256=canonical_json_sha256(receipt_output),
            receipt_json=receipt_output,
            completed_at=workflow.updated_at,
        )
        session.add(receipt)
        await session.flush()
        terminal = SkillBuildTerminalAuthorityRow(
            build_id=build.build_id,
            tenant_id=build.tenant_id,
            actor_id=build.actor_id,
            build_authority_sha256=provenance.authority_sha256,
            terminal_status="REJECTED",
            command_id=command.command_id,
            command_authority_sha256=build_terminal_command_authority_sha256(command),
            workflow_job_id=workflow.job_id,
            workflow_job_sha256=build_terminal_workflow_authority_sha256(workflow),
            terminal_receipt_id=receipt.receipt_id,
            terminal_receipt_authority_sha256=(build_terminal_receipt_authority_sha256(receipt)),
            certification_id=None,
            certification_authority_sha256=None,
            authority_sha256="0" * 64,
            created_at=workflow.updated_at,
        )
        terminal.authority_sha256 = build_terminal_authority_sha256(terminal)
        session.add(terminal)


def _activate_and_read_skill(
    client: TestClient,
    terminal: _TerminalBuild,
) -> tuple[SkillSnapshot, OperationContext]:
    assert terminal.artifact_sha256 is not None
    assert terminal.certification_id is not None
    assert terminal.skill_version_id is not None
    reference = SkillRef(
        terminal.skill_id,
        terminal.skill_version_id,
        terminal.artifact_sha256,
        terminal.certification_id,
    )
    world_id, agent_profile_id, revision, request = _portal_call(
        client, _activation_scope, terminal
    )
    suffix = uuid4().hex[:20]
    headers = {
        **terminal.headers,
        "X-Request-Id": f"req_activate_{suffix}",
        "X-Trace-Id": f"trace_activate_{suffix}",
        "X-Correlation-Id": f"corr_activate_{suffix}",
        "Idempotency-Key": f"idem_activate_{suffix}",
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
            "reason": "student confirmed activation",
        },
    )
    assert accepted.status_code == 202, accepted.text
    app = cast(FastAPI, client.app)
    jobs = cast(PostgresWorkflowJobStore, app.state.workflow_jobs)
    claim = _portal_call(client, _claim_activation, jobs, terminal.tenant_id)
    assert claim is not None
    handler = ControlWorkflowHandler(
        terminal.sessions,
        cast(PostgresCommandStore, app.state.game_queries._command_store),
        jobs,
        lease_seconds=60,
    )
    _portal_call(client, handler.execute, claim)
    assert claim.command_id == accepted.json()["command_id"]
    activation = client.get(f"/v1/skill-activations/{claim.subject_id}", headers=headers)
    assert activation.status_code == 200, activation.text
    command = client.get(f"/v1/commands/{claim.command_id}", headers=headers)
    assert command.status_code == 200, command.text
    operation_suffix = uuid4().hex[:20]
    runtime_command_id = f"cmd_turn_runtime_{operation_suffix}"
    _portal_call(
        client,
        _seed_runtime_turn,
        terminal,
        runtime_command_id,
        operation_suffix,
    )
    operation = OperationContext(
        request_id=f"req_turn_runtime_{operation_suffix}",
        correlation_id=f"corr_turn_runtime_{operation_suffix}",
        trace_id=f"trace_turn_runtime_{operation_suffix}",
        requested_at=datetime.now(UTC),
        actor=request.actor,
        content_ref=request.content_ref,
        schema_version=request.schema_version,
        command_id=runtime_command_id,
        causation_id=terminal.command_id,
        deadline_at=None,
    )
    snapshot = _portal_call(
        client,
        PostgresAgentRuntimeReads(terminal.sessions).get_bound_skill,
        reference,
        operation,
    )
    return snapshot, operation


async def _seed_runtime_turn(
    terminal: _TerminalBuild,
    command_id: str,
    suffix: str,
) -> None:
    async with terminal.sessions() as session, session.begin():
        owner = await session.scalar(
            select(AgentSessionRow).where(
                AgentSessionRow.tenant_id == terminal.tenant_id,
                AgentSessionRow.actor_id == terminal.actor_id,
                AgentSessionRow.status == "ACTIVE",
            )
        )
        assert owner is not None
        session.add(
            AgentTurnRow(
                tenant_id=terminal.tenant_id,
                actor_id=terminal.actor_id,
                session_id=owner.session_id,
                turn_id=f"turn_runtime_{suffix}",
                command_id=command_id,
                turn_sequence=1,
                created_at=datetime.now(UTC),
                request_json={"fixture": "runtime binding"},
            )
        )


async def _activation_scope(
    terminal: _TerminalBuild,
) -> tuple[str, str, int, Any]:
    async with terminal.sessions() as session, session.begin():
        build = await session.scalar(
            select(SkillBuildRow).where(SkillBuildRow.build_id == terminal.build_id)
        )
        authority = await session.scalar(
            select(LaunchAuthorityRow).where(
                LaunchAuthorityRow.tenant_id == terminal.tenant_id,
                LaunchAuthorityRow.actor_id == terminal.actor_id,
                LaunchAuthorityRow.active.is_(True),
            )
        )
        assert build is not None and authority is not None
        head = await session.scalar(
            select(RegistryHeadRow).where(
                RegistryHeadRow.tenant_id == terminal.tenant_id,
                RegistryHeadRow.actor_id == terminal.actor_id,
                RegistryHeadRow.authority_id == authority.authority_id,
            )
        )
        if head is None:
            head = RegistryHeadRow(
                tenant_id=terminal.tenant_id,
                actor_id=terminal.actor_id,
                content_hash=authority.content_hash,
                world_id=authority.world_id,
                agent_profile_id=authority.agent_profile_id,
                authority_id=authority.authority_id,
                revision=0,
                updated_at=datetime.now(UTC),
            )
            session.add(head)
            await session.flush()
        return (
            authority.world_id,
            authority.agent_profile_id,
            head.revision,
            request_context_from_data(build.build_json["request_context"]),
        )


def _turn_context(snapshot: SkillSnapshot, operation: OperationContext) -> TurnContext:
    suffix = hashlib.sha256(operation.command_id.encode()).hexdigest()[:20]
    task_id = f"task_runtime_{suffix}"
    session_id = f"session_runtime_{suffix}"
    world_id = f"world_runtime_{suffix}"
    return TurnContext(
        role="xiaohutao",
        event=GameEvent(
            event_id=f"event_runtime_{suffix}",
            event_type="run_skill_requested",
            student_id=operation.actor.actor_id,
            task_id=task_id,
            session_id=session_id,
            turn_id=f"turn_runtime_{suffix}",
            command_id=operation.command_id,
            occurred_at=operation.requested_at,
            expected_world_revision=0,
            skill_ref=snapshot.ref,
        ),
        task=TaskSnapshot(
            task_id=task_id,
            title="Runtime schema regression",
            goal="Invoke the exact certified Skill.",
            story="",
            knowledge_points=(),
            request_context=snapshot.request_context,
        ),
        session=SessionSnapshot(
            session_id=session_id,
            student_id=operation.actor.actor_id,
            task_id=task_id,
            world_id=world_id,
            request_context=snapshot.request_context,
        ),
        hint_level=0,
        world=WorldSummary(
            world_id=world_id,
            revision=0,
            last_event_sequence=0,
            state_hash=canonical_json_sha256({}),
            visible_state={},
        ),
        skill=snapshot,
        available_skills=(snapshot,),
    )


async def _tamper_certified_parameter_schema(terminal: _TerminalBuild) -> None:
    assert terminal.certification_id is not None
    async with terminal.sessions() as session, session.begin():
        row = await session.scalar(
            select(SkillCertificationRow)
            .where(SkillCertificationRow.certification_id == terminal.certification_id)
            .with_for_update()
        )
        assert row is not None
        value = copy.deepcopy(row.certification_json)
        schema = cast(dict[str, Any], value["parameter_schema"])
        properties = cast(dict[str, Any], schema["properties"])
        length = cast(dict[str, Any], properties["length"])
        length["const"] = 7
        value["parameter_schema_sha256"] = canonical_json_sha256(schema)
        row.certification_json = value
        row.certification_sha256 = canonical_json_sha256(value)


async def _enable_worker_policy(database_url: str, tenant_id: str, actor_id: str) -> None:
    sessions = create_session_factory(database_url)
    try:
        async with sessions() as session, session.begin():
            row = await session.scalar(
                select(BuildPolicyRow)
                .where(
                    BuildPolicyRow.tenant_id == tenant_id,
                    BuildPolicyRow.actor_id == actor_id,
                    BuildPolicyRow.active.is_(True),
                )
                .with_for_update()
            )
            assert row is not None
            policy = copy.deepcopy(row.policy_json)
            policy["compile_flags"] = list(CPP20_SAFE_V1_FLAGS)
            policy["limits"] = {
                "compile_wall_ms": 30_000,
                "test_wall_ms": 30_000,
                "memory_bytes": 268_435_456,
                "max_processes": 32,
                "cpu_millis": 1_000,
                "tmpfs_bytes": 67_108_864,
                "max_output_bytes": 1_048_576,
                "max_artifact_bytes": 16_777_216,
            }
            policy["public_tests"] = [
                {
                    "test_case_id": "terminal-public-1",
                    "visibility": "PUBLIC",
                    "arguments": [],
                    "stdin_base64": "",
                    "expected_stdout_sha256": None,
                }
            ]
            policy["hidden_tests"] = [
                {
                    "test_case_id": "terminal-hidden-1",
                    "visibility": "HIDDEN",
                    "arguments": [],
                    "stdin_base64": "",
                    "expected_stdout_sha256": None,
                }
            ]
            policy["parameter_schema"] = {
                "type": "object",
                "additionalProperties": False,
                "required": ["length"],
                "properties": {"length": {"type": "integer", "const": 8}},
            }
            row.policy_json = policy
            row.policy_sha256 = canonical_json_sha256(policy)
            authority = await session.scalar(
                select(LaunchAuthorityRow).where(
                    LaunchAuthorityRow.tenant_id == tenant_id,
                    LaunchAuthorityRow.actor_id == actor_id,
                    LaunchAuthorityRow.active.is_(True),
                )
            )
            assert authority is not None
            content_ref = {
                "unit_id": authority.content_unit_id,
                "version": authority.content_version,
                "content_hash": authority.content_hash,
            }
            content = await session.scalar(
                select(ProductContentUnitRow).where(
                    ProductContentUnitRow.tenant_id == tenant_id,
                    ProductContentUnitRow.unit_id == authority.content_unit_id,
                    ProductContentUnitRow.version == authority.content_version,
                )
            )
            learner = await session.scalar(
                select(LearnerProfileRow).where(
                    LearnerProfileRow.tenant_id == tenant_id,
                    LearnerProfileRow.learner_id == authority.learner_id,
                )
            )
            profile = await session.scalar(
                select(AgentProfileRow).where(
                    AgentProfileRow.tenant_id == tenant_id,
                    AgentProfileRow.agent_profile_id == authority.agent_profile_id,
                )
            )
            assert content is not None and learner is not None and profile is not None
            content_json = copy.deepcopy(content.content_json)
            content_json.update(
                {
                    "content_ref": content_ref,
                    "status": "PUBLISHED",
                    "unit_type": "TASK",
                    "audiences": list(content.audiences),
                    "published_at": content.published_at.isoformat(),
                }
            )
            content.content_json = content_json
            learner.profile_json = {
                "schema_version": "1.0.0",
                "learner_id": learner.learner_id,
                "actor_id": actor_id,
                "content": content_ref,
                "locale": "zh-CN",
                "revision": 0,
                "projected_through_sequence": 0,
                "model_version": "learner-projection-v1",
                "review_policy_version": "review-v1",
                "competencies": {},
                "evidence_refs": [],
                "updated_at": learner.updated_at.isoformat(),
            }
            learner.profile_sha256 = canonical_json_sha256(learner.profile_json)
            profile.profile_json = {
                "schema_version": "1.0.0",
                "agent_profile_id": profile.agent_profile_id,
                "actor_id": actor_id,
                "content": content_ref,
                "role": "terminal-test-tutor",
                "revision": 1,
                "provider": "fake-provider",
                "model_version": "fake-model-v1",
                "prompt_version": "prompt-workspace-v1",
            }
            profile.profile_sha256 = canonical_json_sha256(profile.profile_json)
            world = await session.scalar(
                select(WorldSnapshotRow).where(
                    WorldSnapshotRow.tenant_id == tenant_id,
                    WorldSnapshotRow.actor_id == actor_id,
                    WorldSnapshotRow.world_id == authority.world_id,
                )
            )
            assert world is not None
            world_json = copy.deepcopy(world.snapshot_json)
            world_json["state"] = {
                "clock": {"day": 1, "minute_of_day": 480, "tick": 0},
                "avatar": {
                    "entity_id": f"avatar_{actor_id}",
                    "position": {"x": 0, "y": 0},
                    "energy": 100,
                },
                "inventory": [],
                "plots": [],
                "agents": [],
            }
            world_json["state_hash"] = canonical_json_sha256(world_json["state"])
            world_json["state_schema_version"] = "1.0.0"
            world.state_hash = cast(str, world_json["state_hash"])
            world.snapshot_json = world_json
            authority.authority_sha256 = canonical_json_sha256(
                {
                    "schema_version": "1.0.0",
                    "authority_id": authority.authority_id,
                    "actor_id": authority.actor_id,
                    "content": content_ref,
                    "world_id": authority.world_id,
                    "learner_id": authority.learner_id,
                    "agent_profile_id": authority.agent_profile_id,
                    "build_policy_id": authority.build_policy_id,
                    "channel": authority.channel,
                    "teaching_spec_version": authority.teaching_spec_version,
                    "active": authority.active,
                }
            )
    finally:
        await sessions.kw["bind"].dispose()


async def _accept_student_session(database_url: str) -> Any:
    return await _accept_session(database_url, student_is_learner=True)


async def _claim_build(jobs: PostgresWorkflowJobStore, tenant_id: str) -> ClaimedWorkflowJob | None:
    return await _claim_workflow_eventually(
        jobs,
        tenant_id=tenant_id,
        worker_id=f"worker_terminal_{uuid4().hex[:20]}",
        operation="CREATE_SKILL_BUILD",
    )


async def _claim_activation(
    jobs: PostgresWorkflowJobStore, tenant_id: str
) -> ClaimedWorkflowJob | None:
    return await _claim_workflow_eventually(
        jobs,
        tenant_id=tenant_id,
        worker_id=f"worker_activation_{uuid4().hex[:20]}",
        operation="ACTIVATE_SKILL_VERSION",
    )


async def _claim_workflow_eventually(
    jobs: PostgresWorkflowJobStore,
    *,
    tenant_id: str,
    worker_id: str,
    operation: str,
    lease_seconds: int = 60,
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


async def _terminal_identities(
    sessions: async_sessionmaker[AsyncSession], command_id: str
) -> dict[str, str | None]:
    async with sessions() as session:
        job = await session.scalar(
            select(WorkflowJobRow).where(WorkflowJobRow.command_id == command_id)
        )
        assert job is not None
        receipt = await session.scalar(
            select(JobStepReceiptRow).where(JobStepReceiptRow.job_id == job.job_id)
        )
        assert receipt is not None
        artifact = await session.scalar(
            select(SkillArtifactRow).where(SkillArtifactRow.build_id == job.subject_id)
        )
        certification = await session.scalar(
            select(SkillCertificationRow).where(SkillCertificationRow.build_id == job.subject_id)
        )
        evidence = await session.scalar(
            select(EvidenceRow).where(EvidenceRow.command_id == command_id)
        )
        return {
            "job_id": job.job_id,
            "receipt_id": receipt.receipt_id,
            "artifact_sha256": artifact.artifact_sha256 if artifact is not None else None,
            "certification_id": (
                certification.certification_id if certification is not None else None
            ),
            "skill_version_id": (
                certification.skill_version_id if certification is not None else None
            ),
            "evidence_id": evidence.evidence_id if evidence is not None else None,
        }


def _tamper_json_and_assert(
    client: TestClient,
    terminal: _TerminalBuild,
    model: type[Any],
    key_name: str,
    key: object,
    column_name: str,
    path: tuple[str | int, ...],
    corrupt_value: object,
    *,
    command: bool = False,
) -> None:
    original = _portal_call(
        client,
        _replace_json_path,
        terminal.sessions,
        model,
        key_name,
        key,
        column_name,
        path,
        corrupt_value,
    )
    try:
        if command:
            _assert_command_read(client, terminal, 500)
        _assert_build_read(client, terminal, 500)
    finally:
        _portal_call(
            client,
            _replace_json_path,
            terminal.sessions,
            model,
            key_name,
            key,
            column_name,
            path,
            original,
        )
    if command:
        _assert_command_read(client, terminal, 200)
    _assert_build_read(client, terminal, 200)


def _tamper_column_and_assert(
    client: TestClient,
    terminal: _TerminalBuild,
    model: type[Any],
    key_name: str,
    key: object,
    column_name: str,
    corrupt_value: object,
    *,
    command: bool = False,
) -> None:
    original = _portal_call(
        client,
        _replace_column,
        terminal.sessions,
        model,
        key_name,
        key,
        column_name,
        corrupt_value,
    )
    try:
        if command:
            _assert_command_read(client, terminal, 500)
        _assert_build_read(client, terminal, 500)
    finally:
        _portal_call(
            client,
            _replace_column,
            terminal.sessions,
            model,
            key_name,
            key,
            column_name,
            original,
        )
    if command:
        _assert_command_read(client, terminal, 200)
    _assert_build_read(client, terminal, 200)


async def _replace_json_path(
    sessions: async_sessionmaker[AsyncSession],
    model: type[Any],
    key_name: str,
    key: object,
    column_name: str,
    path: tuple[str | int, ...],
    replacement: object,
) -> object:
    async with sessions() as session, session.begin():
        key_column = getattr(model, key_name)
        row = await session.scalar(select(model).where(key_column == key).with_for_update())
        assert row is not None
        value = copy.deepcopy(getattr(row, column_name))
        current: Any = value
        for segment in path[:-1]:
            current = current[segment]
        original = copy.deepcopy(current[path[-1]])
        current[path[-1]] = replacement
        setattr(row, column_name, value)
        return original


async def _replace_column(
    sessions: async_sessionmaker[AsyncSession],
    model: type[Any],
    key_name: str,
    key: object,
    column_name: str,
    replacement: object,
) -> object:
    async with sessions() as session, session.begin():
        key_column = getattr(model, key_name)
        row = await session.scalar(select(model).where(key_column == key).with_for_update())
        assert row is not None
        original = getattr(row, column_name)
        setattr(row, column_name, replacement)
        return original


def _assert_command_read(client: TestClient, terminal: _TerminalBuild, status: int) -> None:
    response = client.get(f"/v1/commands/{terminal.command_id}", headers=terminal.headers)
    assert response.status_code == status, response.text
    if status == 500:
        assert response.json()["error"]["code"] == "INVARIANT_VIOLATION"


def _assert_build_read(client: TestClient, terminal: _TerminalBuild, status: int) -> None:
    response = client.get(f"/v1/skill-builds/{terminal.build_id}", headers=terminal.headers)
    assert response.status_code == status, response.text
    if status == 500:
        assert response.json()["error"]["code"] == "INVARIANT_VIOLATION"


def _portal_call(client: TestClient, function: Any, *args: Any) -> Any:
    assert client.portal is not None
    return client.portal.call(function, *args)


def _database_url() -> str:
    value = os.environ.get("WALNUT_TEST_DATABASE_URL")
    if value is None:
        raise RuntimeError("set WALNUT_TEST_DATABASE_URL for terminal Build PostgreSQL coverage")
    return value
