from __future__ import annotations

import asyncio
import hashlib
import stat
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

import psycopg  # noqa: E402
from agent_runtime_fixtures import (  # noqa: E402
    make_operation,
    make_task,
    make_versions,
    make_world_state,
)
from postgres_test_support import (  # noqa: E402
    postgres_test_server,
    reset_sandbox_recovery_results,
)
from psycopg.types.json import Jsonb  # noqa: E402
from test_agent_backend_docker_cpp_sandbox import (  # noqa: E402
    PINNED_GCC_IMAGE,
    _compile_linux,
    _install,
)
from yaya_agent_backend.codec import decode_as, encode  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.invocation import PostgresSkillInvocationService  # noqa: E402
from yaya_agent_backend.stores import PostgresEventStore  # noqa: E402
from yaya_agent_backend.world import WateringWorldEngine  # noqa: E402
from yaya_agent_backend.world_uow import PostgresWorldUnitOfWork  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActiveSkill,
    BuildArtifact,
    CertifiedSkill,
    CommandStatus,
    Failure,
    NewCommand,
    OperationContext,
    RequestContext,
    SandboxLimits,
    Success,
    UncommittedEvent,
    canonical_json_sha256,
)
from yaya_agent_runtime import (  # noqa: E402
    AgentPersistenceError,
    AgentToolExecutionError,
    SessionSnapshot,
    SkillInvocationRequest,
    SkillSnapshot,
    skill_invocation_request_sha256,
)
from yaya_agent_sandbox import DockerCppSandbox  # noqa: E402

_WATERING_CPP = r"""
#include <iostream>
#include <stdexcept>
#include <string>

int main(int argc, char** argv) {
    if (argc != 2) {
        return 3;
    }
    int length = 0;
    try {
        std::size_t parsed = 0;
        const std::string raw(argv[1]);
        length = std::stoi(raw, &parsed);
        if (parsed != raw.size() || length < 0 || length > 8) {
            return 3;
        }
    } catch (const std::exception&) {
        return 3;
    }
    std::cout << "{\"actions\":[";
    for (int index = 1; index <= length; ++index) {
        if (index != 1) {
            std::cout << ',';
        }
        std::cout
            << "{\"intent_id\":\"intent_integration_000" << index
            << "\",\"action_type\":\"WATER\""
            << ",\"actor_entity_id\":\"avatar_0001\""
            << ",\"expected_world_revision\":5"
            << ",\"plot_id\":\"plot_000" << index
            << "\",\"amount_ml\":100}";
    }
    std::cout << "]}";
    return 0;
}
""".strip()


def _request_context(context: OperationContext) -> RequestContext:
    return RequestContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        requested_at=context.requested_at,
        actor=context.actor,
        content_ref=context.content_ref,
    )


class _CountingSandbox:
    def __init__(self, delegate: DockerCppSandbox) -> None:
        self.delegate = delegate
        self.run_count = 0

    async def run(self, request: Any, context: OperationContext) -> Any:
        self.run_count += 1
        return await self.delegate.run(request, context)

    async def cancel(self, run_id: str, reason_code: str, context: OperationContext) -> Any:
        return await self.delegate.cancel(run_id, reason_code, context)

    async def compile_and_test(self, request: Any, context: OperationContext) -> Any:
        return await self.delegate.compile_and_test(request, context)


class _GateSandbox(_CountingSandbox):
    def __init__(self, delegate: DockerCppSandbox) -> None:
        super().__init__(delegate)
        self.completed = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, request: Any, context: OperationContext) -> Any:
        result = await super().run(request, context)
        self.completed.set()
        await self.release.wait()
        return result


class _ForgedRunIdSandbox(_CountingSandbox):
    async def run(self, request: Any, context: OperationContext) -> Any:
        result = await super().run(request, context)
        if isinstance(result, Success):
            return Success(replace(result.value, run_id="run_forged_sandbox_0001"))
        return result


class _TransactionResponseLoss:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    async def __aenter__(self) -> Any:
        return await self._delegate.__aenter__()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        result = await self._delegate.__aexit__(exc_type, exc, traceback)
        if exc_type is None:
            raise psycopg.OperationalError("injected loss of COMMIT response")
        return result


class _ConnectionResponseLoss:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def transaction(self) -> _TransactionResponseLoss:
        return _TransactionResponseLoss(self._delegate.transaction())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _CommitResponseLossDatabase:
    def __init__(self, delegate: PostgresDatabase) -> None:
        self._delegate = delegate
        self._lost = False

    async def connect(self, *, autocommit: bool = False) -> Any:
        connection = await self._delegate.connect(autocommit=autocommit)
        if not self._lost:
            self._lost = True
            return _ConnectionResponseLoss(connection)
        return connection


class _CommitResponseLossAndReconciliationDownDatabase:
    def __init__(self, delegate: PostgresDatabase) -> None:
        self._delegate = delegate
        self._connections = 0

    async def connect(self, *, autocommit: bool = False) -> Any:
        self._connections += 1
        if self._connections == 1:
            return _ConnectionResponseLoss(await self._delegate.connect(autocommit=autocommit))
        raise psycopg.OperationalError("injected reconciliation database interruption")


class AgentBackendSkillInvocationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._artifact_context = tempfile.TemporaryDirectory(prefix="yaya-invocation-cpp-")
        cls._server_context = postgres_test_server()
        cls._artifact_target: Path | None = None
        try:
            cls.root = Path(cls._artifact_context.__enter__()).resolve()
            build_root = cls.root / "build"
            cls.artifact_root = cls.root / "artifacts"
            cls.sandbox_temp_root = cls.root / "sandbox-work"
            cls.sandbox_result_root = cls.root / "sandbox-results"
            build_root.mkdir()
            cls.artifact_root.mkdir()
            cls.sandbox_temp_root.mkdir()
            cls.sandbox_result_root.mkdir()
            executable = _compile_linux(_WATERING_CPP, build_root, "watering_integration")
            cls.skill_ref, cls._artifact_target = _install(executable, cls.artifact_root)
            cls.sandbox = DockerCppSandbox(
                cls.artifact_root,
                image=PINNED_GCC_IMAGE,
                result_root=cls.sandbox_result_root,
                temp_root=cls.sandbox_temp_root,
            )
            cls.server = cls._server_context.__enter__()
            cls.database = PostgresDatabase(cls.server.dsn)
            asyncio.run(cls.database.migrate())
        except BaseException:
            cls._server_context.__exit__(*sys.exc_info())
            if cls._artifact_target is not None:
                cls._artifact_target.chmod(stat.S_IWRITE | stat.S_IREAD)
            cls._artifact_context.__exit__(*sys.exc_info())
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server_context.__exit__(None, None, None)
        if cls._artifact_target is not None:
            cls._artifact_target.chmod(stat.S_IWRITE | stat.S_IREAD)
        cls._artifact_context.__exit__(None, None, None)

    async def asyncSetUp(self) -> None:
        await self._reset_database()
        self.counting_sandbox = _CountingSandbox(self.sandbox)

    async def _reset_database(self) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            for table in (
                "yaya_worlds",
                "yaya_events",
                "yaya_projection_outbox",
                "yaya_evidence",
                "yaya_runs",
                "yaya_skill_invocations",
            ):
                await connection.execute(
                    f"DROP TRIGGER IF EXISTS yaya_test_fail_invocation_publish ON {table}"
                )
            await connection.execute("DROP FUNCTION IF EXISTS yaya_test_fail_invocation_publish()")
            await connection.execute(
                """
                TRUNCATE yaya_skill_invocations,yaya_runs,yaya_evidence,
                  yaya_projection_outbox,yaya_events,yaya_command_jobs,yaya_commands,
                  yaya_registry_active,yaya_registry_certifications,yaya_skills,
                  yaya_agent_sessions,yaya_worlds,yaya_tasks CASCADE
                """
            )
        finally:
            await connection.close()
        reset_sandbox_recovery_results(
            self.sandbox_result_root,
            owner_root=self.root,
        )

    def _service(
        self,
        *,
        database: Any | None = None,
        sandbox: Any | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> PostgresSkillInvocationService:
        selected_database = database or self.database
        world_engine = WateringWorldEngine()
        return PostgresSkillInvocationService(
            database=selected_database,
            sandbox=sandbox or self.counting_sandbox,
            world_engine=world_engine,
            world_uow=PostgresWorldUnitOfWork(selected_database, world_engine),
            limits=SandboxLimits(
                cpu_ms=1_000,
                wall_ms=3_000,
                memory_bytes=67_108_864,
                max_intents=8,
                max_output_bytes=65_536,
                max_processes=1,
                network_access=False,
            ),
            versions=make_versions(),
            contracts_root=CONTRACTS_ROOT,
            clock=clock,
        )

    async def _seed_authority(
        self,
        context: OperationContext,
        *,
        session_id: str = "session_watering_0001",
        turn_id: str = "turn_watering_0001",
        world_id: str = "world_watering_0001",
        client_turn_sequence: int = 1,
        seed_skill: bool = True,
    ) -> tuple[dict[str, object], object]:
        task = make_task(context)
        state = make_world_state()
        state_hash = canonical_json_sha256(state)
        request_context = _request_context(context)
        session = SessionSnapshot(
            session_id=session_id,
            student_id=context.actor.actor_id,
            task_id=task.task_id,
            world_id=world_id,
            request_context=request_context,
        )
        command = NewCommand(
            command_type="EXECUTE_AGENT_TURN",
            idempotency_key=f"agent-turn:{context.command_id}",
            request_sha256=hashlib.sha256(context.command_id.encode("utf-8")).hexdigest(),
            versions=make_versions(),
        )
        record = command.initial_record(context, context.requested_at)
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_tasks(tenant_id,task_id,actor_id,content_hash,snapshot_json)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                """,
                (
                    context.actor.tenant_id,
                    task.task_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    Jsonb(encode(task)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_worlds(
                  tenant_id,world_id,actor_id,content_hash,stream_id,revision,
                  last_event_sequence,state_hash,world_rules_version,state_json,
                  request_context_json
                ) VALUES (%s,%s,%s,%s,%s,5,0,%s,'farm-rules-1',%s,%s)
                ON CONFLICT DO NOTHING
                """,
                (
                    context.actor.tenant_id,
                    world_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    f"world:{world_id}",
                    state_hash,
                    Jsonb(state),
                    Jsonb(encode(request_context)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_agent_sessions(
                  tenant_id,session_id,actor_id,task_id,world_id,content_hash,snapshot_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    session_id,
                    context.actor.actor_id,
                    task.task_id,
                    world_id,
                    context.content_ref.content_hash,
                    Jsonb(encode(session)),
                ),
            )
            await connection.execute(
                """
                INSERT INTO yaya_commands(
                  tenant_id,actor_id,operation,idempotency_key,command_id,
                  session_id,turn_id,client_turn_sequence,request_sha256,content_hash,
                  revision,status,updated_at,record_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    command.operation,
                    command.idempotency_key,
                    context.command_id,
                    session_id,
                    turn_id,
                    client_turn_sequence,
                    command.request_sha256,
                    context.content_ref.content_hash,
                    record.revision,
                    record.status.value,
                    record.updated_at,
                    Jsonb(encode(record)),
                ),
            )
            if seed_skill:
                source_sha256 = hashlib.sha256(_WATERING_CPP.encode("utf-8")).hexdigest()
                skill = SkillSnapshot(
                    ref=self.skill_ref,
                    source_code=_WATERING_CPP,
                    source_sha256=source_sha256,
                    entrypoint="watering_integration.cpp",
                    parameter_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["length"],
                        "properties": {"length": {"type": "integer", "minimum": 1, "maximum": 8}},
                    },
                    request_context=request_context,
                )
                artifact = BuildArtifact(
                    artifact_sha256=self.skill_ref.artifact_sha256,
                    source_sha256=source_sha256,
                    compiler_profile="YAYA_CPP20_SAFE_V1",
                    compiler_version="gcc-pinned",
                    sandbox_image_digest=PINNED_GCC_IMAGE,
                    test_suite_version="watering-integration-v1",
                    artifact_uri=f"artifact://skill/{self.skill_ref.artifact_sha256}",
                )
                certified = CertifiedSkill(
                    certification_id=self.skill_ref.certification_id,
                    skill_id=self.skill_ref.skill_id,
                    skill_version_id=self.skill_ref.skill_version_id,
                    semantic_version="1.0.0",
                    artifact=artifact,
                    capabilities=("watering",),
                    certified_at=context.requested_at,
                    revoked_at=None,
                )
                active = ActiveSkill(
                    skill=certified,
                    registry_revision=1,
                    activated_at=context.requested_at,
                )
                await connection.execute(
                    """
                    INSERT INTO yaya_skills(
                      tenant_id,skill_id,skill_version_id,certification_id,actor_id,
                      session_id,content_hash,artifact_sha256,snapshot_json,active
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
                    """,
                    (
                        context.actor.tenant_id,
                        self.skill_ref.skill_id,
                        self.skill_ref.skill_version_id,
                        self.skill_ref.certification_id,
                        context.actor.actor_id,
                        session_id,
                        context.content_ref.content_hash,
                        self.skill_ref.artifact_sha256,
                        Jsonb(encode(skill)),
                    ),
                )
                await connection.execute(
                    """
                    INSERT INTO yaya_registry_certifications(
                      tenant_id,certification_id,skill_id,skill_version_id,
                      artifact_sha256,record_json,rejected
                    ) VALUES (%s,%s,%s,%s,%s,%s,FALSE)
                    """,
                    (
                        context.actor.tenant_id,
                        self.skill_ref.certification_id,
                        self.skill_ref.skill_id,
                        self.skill_ref.skill_version_id,
                        self.skill_ref.artifact_sha256,
                        Jsonb(encode(certified)),
                    ),
                )
                await connection.execute(
                    """
                    INSERT INTO yaya_registry_active(
                      tenant_id,actor_id,skill_id,record_json,revision
                    ) VALUES (%s,%s,%s,%s,1)
                    """,
                    (
                        context.actor.tenant_id,
                        context.actor.actor_id,
                        self.skill_ref.skill_id,
                        Jsonb(encode(active)),
                    ),
                )
        finally:
            await connection.close()
        return state, record

    def _request(
        self,
        context: OperationContext,
        *,
        invocation_id: str,
        length: int,
        session_id: str = "session_watering_0001",
        turn_id: str = "turn_watering_0001",
        world_id: str = "world_watering_0001",
        expected_world_revision: int = 5,
    ) -> SkillInvocationRequest:
        arguments = {"length": length}
        request_sha256 = skill_invocation_request_sha256(
            tenant_id=context.actor.tenant_id,
            invocation_id=invocation_id,
            session_id=session_id,
            turn_id=turn_id,
            command_id=context.command_id,
            world_id=world_id,
            expected_world_revision=expected_world_revision,
            skill_ref=self.skill_ref,
            arguments=arguments,
        )
        return SkillInvocationRequest(
            invocation_id=invocation_id,
            tenant_id=context.actor.tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            command_id=context.command_id,
            world_id=world_id,
            expected_world_revision=expected_world_revision,
            skill_ref=self.skill_ref,
            arguments=arguments,
            request_sha256=request_sha256,
        )

    async def _database_snapshot(self, world_id: str = "world_watering_0001") -> dict[str, Any]:
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT revision,last_event_sequence,state_hash,state_json
                FROM yaya_worlds WHERE world_id=%s
                """,
                (world_id,),
            )
            world = await cursor.fetchone()
            counts_cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_runs) AS runs,
                  (SELECT count(*) FROM yaya_evidence) AS evidence,
                  (SELECT count(*) FROM yaya_skill_invocations) AS invocations,
                  (SELECT count(*) FROM yaya_events) AS events,
                  (SELECT count(*) FROM yaya_projection_outbox) AS outbox
                """
            )
            counts = await counts_cursor.fetchone()
        finally:
            await connection.close()
        if world is None or counts is None:
            raise AssertionError("PostgreSQL snapshot was incomplete")
        return {**world, **counts}

    async def _insert_world(self, context: OperationContext, world_id: str) -> None:
        state = make_world_state()
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_worlds(
                  tenant_id,world_id,actor_id,content_hash,stream_id,revision,
                  last_event_sequence,state_hash,world_rules_version,state_json,
                  request_context_json
                ) VALUES (%s,%s,%s,%s,%s,5,0,%s,'farm-rules-1',%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    world_id,
                    context.actor.actor_id,
                    context.content_ref.content_hash,
                    f"world:{world_id}",
                    canonical_json_sha256(state),
                    Jsonb(state),
                    Jsonb(encode(_request_context(context))),
                ),
            )
        finally:
            await connection.close()

    async def _insert_command(
        self,
        context: OperationContext,
        *,
        session_id: str,
        turn_id: str,
        client_turn_sequence: int,
    ) -> object:
        command = NewCommand(
            command_type="EXECUTE_AGENT_TURN",
            idempotency_key=f"agent-turn:{context.command_id}",
            request_sha256=hashlib.sha256(context.command_id.encode("utf-8")).hexdigest(),
            versions=make_versions(),
        )
        record = command.initial_record(context, context.requested_at)
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                INSERT INTO yaya_commands(
                  tenant_id,actor_id,operation,idempotency_key,command_id,
                  session_id,turn_id,client_turn_sequence,request_sha256,content_hash,
                  revision,status,updated_at,record_json
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    command.operation,
                    command.idempotency_key,
                    context.command_id,
                    session_id,
                    turn_id,
                    client_turn_sequence,
                    command.request_sha256,
                    context.content_ref.content_hash,
                    record.revision,
                    record.status.value,
                    record.updated_at,
                    Jsonb(encode(record)),
                ),
            )
        finally:
            await connection.close()
        return record

    async def _drift_skill_authority_during_sandbox(
        self,
        context: OperationContext,
        drift: str,
    ) -> None:
        connection = await self.database.connect(autocommit=True)
        try:
            if drift == "certification_rejected":
                updated = await connection.execute(
                    """
                    UPDATE yaya_registry_certifications SET rejected=TRUE
                    WHERE tenant_id=%s AND certification_id=%s AND rejected=FALSE
                    """,
                    (context.actor.tenant_id, self.skill_ref.certification_id),
                )
                if updated.rowcount != 1:
                    raise AssertionError("active certification was not rejected exactly once")
                return
            if drift != "registry_switched":
                raise AssertionError(f"unsupported Skill authority drift {drift}")

            cursor = await connection.execute(
                """
                SELECT c.record_json AS certification_json,
                       s.snapshot_json AS skill_json,s.session_id
                FROM yaya_registry_certifications c
                JOIN yaya_skills s
                  ON s.tenant_id=c.tenant_id AND s.certification_id=c.certification_id
                 AND s.skill_id=c.skill_id AND s.skill_version_id=c.skill_version_id
                 AND s.artifact_sha256=c.artifact_sha256
                WHERE c.tenant_id=%s AND c.certification_id=%s AND c.rejected=FALSE
                """,
                (context.actor.tenant_id, self.skill_ref.certification_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise AssertionError("original active certification disappeared")
            certified = decode_as(row["certification_json"], CertifiedSkill)
            skill = decode_as(row["skill_json"], SkillSnapshot)
            switched_certified = replace(
                certified,
                certification_id="certification_registry_switched_0001",
                skill_version_id="skill_version_registry_switched_0001",
                semantic_version="2.0.0",
            )
            switched_skill = replace(
                skill,
                ref=replace(
                    skill.ref,
                    certification_id=switched_certified.certification_id,
                    skill_version_id=switched_certified.skill_version_id,
                ),
            )
            switched_active = ActiveSkill(
                skill=switched_certified,
                registry_revision=2,
                activated_at=datetime.now(UTC),
            )
            await connection.execute(
                """
                INSERT INTO yaya_registry_certifications(
                  tenant_id,certification_id,skill_id,skill_version_id,
                  artifact_sha256,record_json,rejected
                ) VALUES (%s,%s,%s,%s,%s,%s,FALSE)
                """,
                (
                    context.actor.tenant_id,
                    switched_certified.certification_id,
                    switched_certified.skill_id,
                    switched_certified.skill_version_id,
                    switched_certified.artifact.artifact_sha256,
                    Jsonb(encode(switched_certified)),
                ),
            )
            await connection.execute(
                """
                UPDATE yaya_skills SET active=FALSE
                WHERE tenant_id=%s AND skill_version_id=%s AND active=TRUE
                """,
                (context.actor.tenant_id, self.skill_ref.skill_version_id),
            )
            await connection.execute(
                """
                INSERT INTO yaya_skills(
                  tenant_id,skill_id,skill_version_id,certification_id,actor_id,
                  session_id,content_hash,artifact_sha256,snapshot_json,active
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
                """,
                (
                    context.actor.tenant_id,
                    switched_skill.ref.skill_id,
                    switched_skill.ref.skill_version_id,
                    switched_skill.ref.certification_id,
                    context.actor.actor_id,
                    row["session_id"],
                    context.content_ref.content_hash,
                    switched_skill.ref.artifact_sha256,
                    Jsonb(encode(switched_skill)),
                ),
            )
            updated = await connection.execute(
                """
                UPDATE yaya_registry_active SET record_json=%s,revision=2
                WHERE tenant_id=%s AND actor_id=%s AND skill_id=%s AND revision=1
                """,
                (
                    Jsonb(encode(switched_active)),
                    context.actor.tenant_id,
                    context.actor.actor_id,
                    self.skill_ref.skill_id,
                ),
            )
            if updated.rowcount != 1:
                raise AssertionError("Registry active binding was not switched exactly once")
        finally:
            await connection.close()

    async def _install_publish_fault(
        self,
        table: str,
        operation: str,
        predicate: str | None,
    ) -> None:
        allowed = {
            ("yaya_worlds", "UPDATE"),
            ("yaya_events", "INSERT"),
            ("yaya_projection_outbox", "INSERT"),
            ("yaya_evidence", "INSERT"),
            ("yaya_runs", "INSERT"),
            ("yaya_skill_invocations", "INSERT"),
        }
        if (table, operation) not in allowed:
            raise AssertionError("fault trigger target is not allowlisted")
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                CREATE FUNCTION yaya_test_fail_invocation_publish() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'injected invocation publish failure'
                        USING ERRCODE = '40001';
                END
                $$
                """
            )
            when_clause = f" WHEN ({predicate})" if predicate is not None else ""
            await connection.execute(
                f"""
                CREATE TRIGGER yaya_test_fail_invocation_publish
                BEFORE {operation} ON {table}
                FOR EACH ROW{when_clause}
                EXECUTE FUNCTION yaya_test_fail_invocation_publish()
                """
            )
        finally:
            await connection.close()

    async def test_real_cpp_8_of_8_is_atomic_and_replay_never_reruns_sandbox(self) -> None:
        context = make_operation()
        await self._seed_authority(context)
        request = self._request(
            context,
            invocation_id="invocation_watering_success_0001",
            length=8,
        )
        service = self._service()
        result = await service.invoke(request, context)
        self.assertTrue(result.run.task_success)
        self.assertEqual(result.run.world_revision_before, 5)
        self.assertEqual(result.run.world_revision_after, 6)
        self.assertEqual(len(result.run.evidence_refs), 2)
        snapshot = await self._database_snapshot()
        self.assertEqual(snapshot["revision"], 6)
        self.assertEqual(snapshot["last_event_sequence"], 1)
        self.assertEqual(
            (snapshot["runs"], snapshot["evidence"], snapshot["invocations"]),
            (1, 2, 1),
        )
        self.assertEqual((snapshot["events"], snapshot["outbox"]), (1, 1))
        self.assertTrue(all(plot["hydration"] == 100 for plot in snapshot["state_json"]["plots"]))
        self.assertEqual(self.counting_sandbox.run_count, 1)

        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT evidence_json FROM yaya_evidence
                ORDER BY evidence_type,evidence_id
                """
            )
            evidence_rows = list(await cursor.fetchall())
        finally:
            await connection.close()
        self.assertEqual(len(evidence_rows), 2)
        for row in evidence_rows:
            evidence_value = row["evidence_json"]
            if not isinstance(evidence_value, Mapping):
                self.fail("persisted Evidence is not an object")
            evidence = cast(Mapping[str, object], evidence_value)
            reference_value = evidence.get("evidence_ref")
            if not isinstance(reference_value, Mapping):
                self.fail("persisted EvidenceRef is not an object")
            reference = cast(Mapping[str, object], reference_value)
            self.assertEqual(reference["created_at"], evidence["occurred_at"])

        replay = await self._service().invoke(request, context)
        self.assertEqual(replay, result)
        self.assertEqual(self.counting_sandbox.run_count, 1)
        self.assertEqual(await self._database_snapshot(), snapshot)

    async def test_run_publication_uses_durable_postgres_time_across_host_clock_skew(
        self,
    ) -> None:
        accepted_at = datetime.now(UTC) + timedelta(minutes=5)
        context = replace(
            make_operation(),
            requested_at=accepted_at,
            deadline_at=accepted_at + timedelta(seconds=30),
        )
        await self._seed_authority(context)
        stale_host_time = accepted_at - timedelta(days=1)

        result = await self._service(clock=lambda: stale_host_time).invoke(
            self._request(
                context,
                invocation_id="invocation_watering_clock_skew_0001",
                length=8,
            ),
            context,
        )
        self.assertTrue(result.run.task_success)
        self.assertIsNotNone(result.run.world_commit)

        connection = await self.database.connect(autocommit=True)
        try:
            run_cursor = await connection.execute(
                """
                SELECT r.created_at,r.wire_json,c.updated_at
                FROM yaya_runs r JOIN yaya_commands c
                  ON c.tenant_id=r.tenant_id AND c.command_id=r.command_id
                WHERE r.run_id=%s
                """,
                (result.run.run_id,),
            )
            run_row = await run_cursor.fetchone()
            evidence_cursor = await connection.execute(
                """
                SELECT recorded_at,evidence_json FROM yaya_evidence
                WHERE tenant_id=%s ORDER BY evidence_id
                """,
                (context.actor.tenant_id,),
            )
            evidence_rows = list(await evidence_cursor.fetchall())
            world_cursor = await connection.execute(
                """
                SELECT updated_at FROM yaya_worlds
                WHERE tenant_id=%s AND world_id=%s
                """,
                (context.actor.tenant_id, result.run.world_id),
            )
            world_row = await world_cursor.fetchone()
        finally:
            await connection.close()

        self.assertIsNotNone(run_row)
        self.assertIsNotNone(world_row)
        assert run_row is not None and world_row is not None
        publication_time = cast(datetime, run_row["created_at"])
        wire = cast(Mapping[str, object], run_row["wire_json"])
        self.assertGreaterEqual(publication_time, accepted_at)
        self.assertGreaterEqual(publication_time, cast(datetime, run_row["updated_at"]))
        self.assertNotEqual(publication_time, stale_host_time)
        self.assertEqual(
            wire["created_at"],
            publication_time.isoformat().replace("+00:00", "Z"),
        )
        self.assertEqual(
            {cast(datetime, row["recorded_at"]) for row in evidence_rows},
            {publication_time},
        )
        publication_wire_time = publication_time.isoformat().replace("+00:00", "Z")
        for evidence_row in evidence_rows:
            evidence = cast(Mapping[str, object], evidence_row["evidence_json"])
            reference = cast(Mapping[str, object], evidence["evidence_ref"])
            self.assertEqual(evidence["occurred_at"], publication_wire_time)
            self.assertEqual(reference["created_at"], publication_wire_time)
        self.assertEqual(world_row["updated_at"], publication_time)
        assert result.run.world_commit is not None
        self.assertEqual(result.run.world_commit.committed_at, publication_time)

    async def test_real_cpp_7_of_8_persists_failure_without_world_change(self) -> None:
        context = make_operation()
        initial_state, _ = await self._seed_authority(context)
        initial_hash = canonical_json_sha256(initial_state)
        result = await self._service().invoke(
            self._request(
                context,
                invocation_id="invocation_watering_short_0001",
                length=7,
            ),
            context,
        )
        self.assertFalse(result.run.task_success)
        self.assertEqual(result.run.failure_key, "watering_loop_short")
        self.assertIsNone(result.run.world_commit)
        self.assertEqual(len(result.run.evidence_refs), 1)
        snapshot = await self._database_snapshot()
        self.assertEqual(
            (snapshot["revision"], snapshot["last_event_sequence"], snapshot["state_hash"]),
            (5, 0, initial_hash),
        )
        self.assertEqual(snapshot["state_json"], initial_state)
        self.assertEqual(
            (
                snapshot["runs"],
                snapshot["evidence"],
                snapshot["invocations"],
                snapshot["events"],
                snapshot["outbox"],
            ),
            (1, 1, 1, 0, 0),
        )
        conflicting_request = self._request(
            context,
            invocation_id="invocation_watering_short_0001",
            length=8,
        )
        with self.assertRaises(AgentToolExecutionError) as conflict:
            await self._service().invoke(conflicting_request, context)
        self.assertEqual(conflict.exception.code, "TOOL_IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(self.counting_sandbox.run_count, 1)
        self.assertEqual(await self._database_snapshot(), snapshot)

    async def test_commit_response_loss_reconciles_exact_receipt(self) -> None:
        context = make_operation()
        await self._seed_authority(context)
        request = self._request(
            context,
            invocation_id="invocation_commit_response_loss_0001",
            length=8,
        )
        service = self._service(database=_CommitResponseLossDatabase(self.database))
        result = await service.invoke(request, context)
        self.assertTrue(result.run.task_success)
        self.assertEqual(self.counting_sandbox.run_count, 1)
        snapshot = await self._database_snapshot()
        self.assertEqual(
            (
                snapshot["revision"],
                snapshot["runs"],
                snapshot["evidence"],
                snapshot["invocations"],
                snapshot["events"],
                snapshot["outbox"],
            ),
            (6, 1, 2, 1, 1, 1),
        )
        replay = await self._service().invoke(request, context)
        self.assertEqual(replay, result)
        self.assertEqual(self.counting_sandbox.run_count, 1)

    async def test_commit_unknown_and_reconciliation_interruption_is_explicit(self) -> None:
        context = make_operation()
        await self._seed_authority(context)
        request = self._request(
            context,
            invocation_id="invocation_reconciliation_down_0001",
            length=8,
        )
        unavailable = _CommitResponseLossAndReconciliationDownDatabase(self.database)
        with self.assertRaises(AgentToolExecutionError) as unknown:
            await self._service(database=unavailable).invoke(request, context)
        self.assertEqual(unknown.exception.code, "UNKNOWN_COMMIT_STATE")
        self.assertEqual(
            unknown.exception.details.get("runtime_warning"),
            "SIDE_EFFECT_COMMIT_UNKNOWN",
        )
        self.assertEqual(self.counting_sandbox.run_count, 1)
        committed = await self._service().invoke(request, context)
        self.assertTrue(committed.run.task_success)
        self.assertEqual(self.counting_sandbox.run_count, 1)
        snapshot = await self._database_snapshot()
        self.assertEqual(
            (
                snapshot["revision"],
                snapshot["runs"],
                snapshot["evidence"],
                snapshot["invocations"],
                snapshot["events"],
                snapshot["outbox"],
            ),
            (6, 1, 2, 1, 1, 1),
        )

    async def test_cross_tenant_actor_and_content_authority_never_reaches_sandbox(self) -> None:
        context = make_operation()
        await self._seed_authority(context)
        request = self._request(
            context,
            invocation_id="invocation_cross_authority_0001",
            length=8,
        )
        contexts = {
            "tenant": replace(
                context,
                actor=replace(context.actor, tenant_id="tenant_wrong_invocation"),
            ),
            "actor": replace(
                context,
                actor=replace(context.actor, actor_id="student_wrong_invocation_0001"),
            ),
            "content": replace(
                context,
                content_ref=replace(context.content_ref, content_hash="f" * 64),
            ),
        }
        initial = await self._database_snapshot()
        for identity, alien_context in contexts.items():
            with self.subTest(identity=identity):
                with self.assertRaises(AgentToolExecutionError) as denied:
                    await self._service().invoke(request, alien_context)
                self.assertEqual(
                    denied.exception.code,
                    "TOOL_INVOCATION_IDENTITY_MISMATCH",
                )
        self.assertEqual(self.counting_sandbox.run_count, 0)
        self.assertEqual(await self._database_snapshot(), initial)

    async def test_session_cannot_invoke_skill_bound_only_to_another_session(self) -> None:
        first_context = make_operation(command_id="cmd_skill_session_a_0001")
        await self._seed_authority(
            first_context,
            session_id="session_skill_binding_a_0001",
            turn_id="turn_skill_binding_a_0001",
            world_id="world_skill_binding_a_0001",
        )
        second_context = make_operation(command_id="cmd_skill_session_b_0001")
        await self._seed_authority(
            second_context,
            session_id="session_skill_binding_b_0001",
            turn_id="turn_skill_binding_b_0001",
            world_id="world_skill_binding_b_0001",
            seed_skill=False,
        )
        before_first = await self._database_snapshot("world_skill_binding_a_0001")
        before_second = await self._database_snapshot("world_skill_binding_b_0001")
        request = self._request(
            second_context,
            invocation_id="invocation_cross_session_skill_0001",
            length=8,
            session_id="session_skill_binding_b_0001",
            turn_id="turn_skill_binding_b_0001",
            world_id="world_skill_binding_b_0001",
        )

        with self.assertRaises(AgentToolExecutionError) as denied:
            await self._service().invoke(request, second_context)
        self.assertEqual(denied.exception.code, "TOOL_SKILL_BINDING_MISMATCH")
        self.assertEqual(self.counting_sandbox.run_count, 0)
        self.assertEqual(
            await self._database_snapshot("world_skill_binding_a_0001"),
            before_first,
        )
        self.assertEqual(
            await self._database_snapshot("world_skill_binding_b_0001"),
            before_second,
        )

    async def test_generic_event_store_cannot_bypass_world_unit_of_work_stream(self) -> None:
        context = make_operation()
        await self._seed_authority(context)
        event = UncommittedEvent(
            event_type="world.action_applied",
            event_version=1,
            producer="fault_injection_test",
            trace_id=context.trace_id,
            command_id=context.command_id,
            correlation_id=context.correlation_id,
            causation_id=context.command_id,
            content_ref=context.content_ref,
            payload={"world_id": "world_watering_0001"},
        )
        result = await PostgresEventStore(self.database).append(
            "world:world_watering_0001",
            0,
            (event,),
            context,
        )
        self.assertIsInstance(result, Failure)
        self.assertEqual(result.error.code, "INVARIANT_VIOLATION")
        snapshot = await self._database_snapshot()
        self.assertEqual((snapshot["revision"], snapshot["last_event_sequence"]), (5, 0))
        self.assertEqual(snapshot["events"], 0)

    async def test_tampered_receipt_storage_hash_fails_loud(self) -> None:
        context = make_operation()
        await self._seed_authority(context)
        request = self._request(
            context,
            invocation_id="invocation_tampered_receipt_0001",
            length=7,
        )
        service = self._service()
        await service.invoke(request, context)
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_skill_invocations SET request_sha256=%s
                WHERE tenant_id=%s AND invocation_id=%s
                """,
                ("f" * 64, context.actor.tenant_id, request.invocation_id),
            )
        finally:
            await connection.close()
        with self.assertRaises(AgentPersistenceError):
            await service.get_result(request.invocation_id, context)

    async def test_old_revision_and_session_world_mislink_fail_before_sandbox(self) -> None:
        context = make_operation()
        await self._seed_authority(context)
        initial = await self._database_snapshot()
        stale = self._request(
            context,
            invocation_id="invocation_stale_revision_0001",
            length=8,
            expected_world_revision=4,
        )
        with self.assertRaises(AgentToolExecutionError) as stale_error:
            await self._service().invoke(stale, context)
        self.assertEqual(stale_error.exception.code, "TOOL_WORLD_REVISION_CONFLICT")
        self.assertEqual(self.counting_sandbox.run_count, 0)

        foreign_world_id = "world_watering_mislinked_0002"
        await self._insert_world(context, foreign_world_id)
        mislinked = self._request(
            context,
            invocation_id="invocation_world_mislink_0001",
            length=8,
            world_id=foreign_world_id,
        )
        with self.assertRaises(AgentToolExecutionError) as identity_error:
            await self._service().invoke(mislinked, context)
        self.assertEqual(
            identity_error.exception.code,
            "TOOL_INVOCATION_IDENTITY_MISMATCH",
        )
        self.assertEqual(self.counting_sandbox.run_count, 0)
        self.assertEqual(await self._database_snapshot(), initial)
        foreign = await self._database_snapshot(foreign_world_id)
        self.assertEqual((foreign["revision"], foreign["last_event_sequence"]), (5, 0))

    async def test_command_cancelled_while_real_sandbox_runs_fences_world_commit(self) -> None:
        context = make_operation()
        initial_state, record = await self._seed_authority(context)
        initial_hash = canonical_json_sha256(initial_state)
        gate = _GateSandbox(self.sandbox)
        invocation = asyncio.create_task(
            self._service(sandbox=gate).invoke(
                self._request(
                    context,
                    invocation_id="invocation_cancel_during_sandbox_0001",
                    length=8,
                ),
                context,
            )
        )
        await asyncio.wait_for(gate.completed.wait(), timeout=15)
        cancelled = replace(
            record,
            revision=record.revision + 1,
            status=CommandStatus.CANCELLED,
            stage="SANDBOX",
            terminal=True,
            updated_at=datetime.now(UTC),
        )
        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute(
                """
                UPDATE yaya_commands SET revision=%s,status=%s,updated_at=%s,record_json=%s
                WHERE tenant_id=%s AND command_id=%s
                """,
                (
                    cancelled.revision,
                    cancelled.status.value,
                    cancelled.updated_at,
                    Jsonb(encode(cancelled)),
                    context.actor.tenant_id,
                    context.command_id,
                ),
            )
        finally:
            await connection.close()
        gate.release.set()
        with self.assertRaises(AgentToolExecutionError) as cancelled_error:
            await invocation
        self.assertEqual(
            cancelled_error.exception.code,
            "TOOL_INVOCATION_IDENTITY_MISMATCH",
        )
        snapshot = await self._database_snapshot()
        self.assertEqual(
            (
                snapshot["revision"],
                snapshot["last_event_sequence"],
                snapshot["state_hash"],
                snapshot["runs"],
                snapshot["evidence"],
                snapshot["invocations"],
                snapshot["events"],
                snapshot["outbox"],
            ),
            (5, 0, initial_hash, 0, 0, 0, 0, 0),
        )

    async def test_skill_authority_drift_during_sandbox_fences_all_side_effects(self) -> None:
        for index, drift in enumerate(
            ("registry_switched", "certification_rejected"),
            start=1,
        ):
            with self.subTest(drift=drift):
                await self._reset_database()
                context = make_operation(command_id=f"cmd_skill_drift_000{index}")
                initial_state, _ = await self._seed_authority(context)
                initial_hash = canonical_json_sha256(initial_state)
                gate = _GateSandbox(self.sandbox)
                invocation = asyncio.create_task(
                    self._service(sandbox=gate).invoke(
                        self._request(
                            context,
                            invocation_id=f"invocation_skill_drift_000{index}",
                            length=8,
                        ),
                        context,
                    )
                )
                try:
                    await asyncio.wait_for(gate.completed.wait(), timeout=15)
                    self.assertEqual(gate.run_count, 1)
                    await self._drift_skill_authority_during_sandbox(context, drift)
                    after_authority_change = await self._database_snapshot()
                finally:
                    gate.release.set()

                with self.assertRaises(AgentToolExecutionError) as denied:
                    await invocation
                self.assertEqual(denied.exception.code, "TOOL_SKILL_BINDING_MISMATCH")
                self.assertEqual(gate.run_count, 1)
                final = await self._database_snapshot()
                self.assertEqual(final, after_authority_change)
                self.assertEqual(
                    (
                        final["revision"],
                        final["last_event_sequence"],
                        final["state_hash"],
                        final["runs"],
                        final["evidence"],
                        final["invocations"],
                        final["events"],
                        final["outbox"],
                    ),
                    (5, 0, initial_hash, 0, 0, 0, 0, 0),
                )

    async def test_forged_sandbox_success_run_id_is_persisted_only_as_failure(self) -> None:
        context = make_operation()
        initial_state, _ = await self._seed_authority(context)
        forged = _ForgedRunIdSandbox(self.sandbox)
        result = await self._service(sandbox=forged).invoke(
            self._request(
                context,
                invocation_id="invocation_forged_run_id_0001",
                length=8,
            ),
            context,
        )
        self.assertFalse(result.run.task_success)
        self.assertEqual(result.run.failure_key, "sandbox_execution_failed")
        snapshot = await self._database_snapshot()
        self.assertEqual(snapshot["state_json"], initial_state)
        self.assertEqual(
            (
                snapshot["revision"],
                snapshot["last_event_sequence"],
                snapshot["runs"],
                snapshot["evidence"],
                snapshot["invocations"],
                snapshot["events"],
                snapshot["outbox"],
            ),
            (5, 0, 1, 1, 1, 0, 0),
        )
        connection = await self.database.connect(autocommit=True)
        try:
            cursor = await connection.execute("SELECT wire_json FROM yaya_runs")
            row = await cursor.fetchone()
        finally:
            await connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(
            row["wire_json"]["sandbox"]["failure"]["code"],
            "SANDBOX_RUNTIME_ERROR",
        )
        self.assertEqual(
            row["wire_json"]["sandbox"]["failure"]["details"]["reason"],
            "RUN_ID_MISMATCH",
        )

    async def test_two_real_invocations_racing_one_world_have_one_cas_winner(self) -> None:
        first_context = make_operation(command_id="cmd_watering_race_0001")
        await self._seed_authority(
            first_context,
            turn_id="turn_watering_race_0001",
        )
        second_context = make_operation(command_id="cmd_watering_race_0002")
        await self._insert_command(
            second_context,
            session_id="session_watering_0001",
            turn_id="turn_watering_race_0002",
            client_turn_sequence=2,
        )
        first_request = self._request(
            first_context,
            invocation_id="invocation_world_race_0001",
            length=8,
            turn_id="turn_watering_race_0001",
        )
        second_request = self._request(
            second_context,
            invocation_id="invocation_world_race_0002",
            length=8,
            turn_id="turn_watering_race_0002",
        )
        outcomes = await asyncio.gather(
            self._service().invoke(first_request, first_context),
            self._service().invoke(second_request, second_context),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if not isinstance(item, BaseException)]
        failures = [item for item in outcomes if isinstance(item, BaseException)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], AgentToolExecutionError)
        self.assertEqual(failures[0].code, "TOOL_WORLD_REVISION_CONFLICT")
        snapshot = await self._database_snapshot()
        self.assertEqual(
            (
                snapshot["revision"],
                snapshot["last_event_sequence"],
                snapshot["runs"],
                snapshot["evidence"],
                snapshot["invocations"],
                snapshot["events"],
                snapshot["outbox"],
            ),
            (6, 1, 1, 2, 1, 1, 1),
        )

    async def test_each_atomic_publish_write_fault_rolls_back_every_record(self) -> None:
        fault_points = (
            ("yaya_worlds", "UPDATE", "NEW.revision = 6"),
            ("yaya_events", "INSERT", None),
            ("yaya_projection_outbox", "INSERT", None),
            ("yaya_evidence", "INSERT", "NEW.evidence_type = 'SANDBOX_LOG'"),
            ("yaya_evidence", "INSERT", "NEW.evidence_type = 'WORLD_COMMIT'"),
            ("yaya_runs", "INSERT", None),
            ("yaya_skill_invocations", "INSERT", None),
        )
        for index, (table, operation, predicate) in enumerate(fault_points, start=1):
            with self.subTest(write_point=f"{table}:{predicate}"):
                await self._reset_database()
                context = make_operation()
                initial_state, _ = await self._seed_authority(context)
                initial_hash = canonical_json_sha256(initial_state)
                await self._install_publish_fault(table, operation, predicate)
                request = self._request(
                    context,
                    invocation_id=f"invocation_fault_point_000{index}",
                    length=8,
                )
                with self.assertRaises(AgentToolExecutionError) as failure:
                    await self._service().invoke(request, context)
                self.assertEqual(
                    failure.exception.code,
                    "TOOL_PERSISTENCE_ROLLED_BACK",
                )
                self.assertEqual(failure.exception.details["commit_state"], "ROLLED_BACK")
                self.assertEqual(
                    failure.exception.details["runtime_warning"],
                    "SIDE_EFFECT_ROLLED_BACK",
                )
                self.assertEqual(failure.exception.details["sqlstate"], "40001")
                snapshot = await self._database_snapshot()
                self.assertEqual(
                    (
                        snapshot["revision"],
                        snapshot["last_event_sequence"],
                        snapshot["state_hash"],
                        snapshot["runs"],
                        snapshot["evidence"],
                        snapshot["invocations"],
                        snapshot["events"],
                        snapshot["outbox"],
                    ),
                    (5, 0, initial_hash, 0, 0, 0, 0, 0),
                )

    async def test_known_rollback_retries_same_invocation_and_commits_world_once(self) -> None:
        context = make_operation()
        initial_state, _ = await self._seed_authority(context)
        initial_hash = canonical_json_sha256(initial_state)
        await self._install_publish_fault("yaya_runs", "INSERT", None)
        request = self._request(
            context,
            invocation_id="invocation_known_rollback_retry_0001",
            length=8,
        )
        service = self._service()

        with self.assertRaises(AgentToolExecutionError) as rolled_back:
            await service.invoke(request, context)
        self.assertEqual(rolled_back.exception.code, "TOOL_PERSISTENCE_ROLLED_BACK")
        self.assertEqual(self.counting_sandbox.run_count, 1)
        after_rollback = await self._database_snapshot()
        self.assertEqual(
            (
                after_rollback["revision"],
                after_rollback["last_event_sequence"],
                after_rollback["state_hash"],
                after_rollback["runs"],
                after_rollback["evidence"],
                after_rollback["invocations"],
                after_rollback["events"],
                after_rollback["outbox"],
            ),
            (5, 0, initial_hash, 0, 0, 0, 0, 0),
        )

        connection = await self.database.connect(autocommit=True)
        try:
            await connection.execute("DROP TRIGGER yaya_test_fail_invocation_publish ON yaya_runs")
            await connection.execute("DROP FUNCTION yaya_test_fail_invocation_publish()")
        finally:
            await connection.close()

        committed = await service.invoke(request, context)
        self.assertTrue(committed.run.task_success)
        self.assertEqual(self.counting_sandbox.run_count, 2)
        replay = await service.invoke(request, context)
        self.assertEqual(replay, committed)
        self.assertEqual(self.counting_sandbox.run_count, 2)
        final = await self._database_snapshot()
        self.assertEqual(
            (
                final["revision"],
                final["last_event_sequence"],
                final["runs"],
                final["evidence"],
                final["invocations"],
                final["events"],
                final["outbox"],
            ),
            (6, 1, 1, 2, 1, 1, 1),
        )


if __name__ == "__main__":
    unittest.main()
