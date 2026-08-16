from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import hashlib
import http.client
import json
import stat
import sys
import tempfile
import threading
import unittest
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    SESSION_ID,
    TASK_ID,
    WORLD_ID,
    make_operation,
    make_task,
    make_world_state,
)
from postgres_test_support import postgres_test_server  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from test_agent_backend_docker_cpp_sandbox import (  # noqa: E402
    PINNED_GCC_IMAGE,
    compile_linux,
    install_artifact,
)
from test_agent_backend_live_e2e import (  # noqa: E402
    _generation_budget_guard,
    _provider_thinking_mode,
    _required_generation_budget,
    _required_provider_api_key,
    _required_provider_setting,
)
from yaya_agent_backend.codec import decode_as, encode  # noqa: E402
from yaya_agent_backend.composition import (  # noqa: E402
    ProductionComposition,
    create_production_composition,
)
from yaya_agent_backend.config import ProductionSettings  # noqa: E402
from yaya_agent_backend.http_api import AgentHttpApi, serve_http  # noqa: E402
from yaya_agent_backend.http_router import ProductionHttpApi  # noqa: E402
from yaya_agent_backend.product_http_api import ProductHttpApi  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    ActiveSkill,
    BuildArtifact,
    CertifiedSkill,
    RequestContext,
    SkillRef,
    Success,
    canonical_json_sha256,
)
from yaya_agent_runtime import (  # noqa: E402
    AgentTraceEvent,
    CommittedAgentTurn,
    SessionSnapshot,
    SkillSnapshot,
    TeachingPhase,
)

_JWT_SECRET = "role-live-e2e-jwt-secret-0000000000000000000000"
_JWT_ISSUER = "yaya-role-live-e2e"
_JWT_AUDIENCE = "yaya-game-api"
_LIVE_SKILL_ID = "skill_live_e2e_watering_0001"
_FAILURE_TURNS = (
    "turn_live_e2e_failure_0001",
    "turn_live_e2e_failure_0002",
    "turn_live_e2e_failure_0003",
)
_SUCCESS_TURN = "turn_live_e2e_success_0004"

_SUCCESS_CPP = r"""
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
            << "{\"intent_id\":\"intent_role_live_000" << index
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

# This is still a real pinned-Docker execution.  It emits seven objectively
# valid actions, leaving World rules to derive watering_loop_short.
_FAILURE_CPP = _SUCCESS_CPP.replace("index <= length", "index < length")

_BUSINESS_TABLES = (
    "yaya_commands",
    "yaya_command_jobs",
    "yaya_runs",
    "yaya_evidence",
    "yaya_skill_invocations",
    "yaya_agent_turns",
    "yaya_agent_messages",
    "yaya_agent_interactions",
    "yaya_events",
    "yaya_projection_outbox",
    "yaya_outbox",
    "yaya_learner_models",
    "yaya_learner_projection_jobs",
    "yaya_learner_projection_receipts",
    "yaya_learner_projection_failures",
    "yaya_agent_traces",
    "yaya_worlds",
)


def _request_context() -> RequestContext:
    operation = make_operation(command_id="cmd_live_role_seed_0001")
    return RequestContext(
        request_id=operation.request_id,
        correlation_id=operation.correlation_id,
        trace_id=operation.trace_id,
        requested_at=operation.requested_at,
        actor=operation.actor,
        content_ref=operation.content_ref,
    )


def _versioned_ref(raw: SkillRef, version: int) -> SkillRef:
    return SkillRef(
        skill_id=_LIVE_SKILL_ID,
        skill_version_id=f"skill_version_live_e2e_{version:04d}",
        artifact_sha256=raw.artifact_sha256,
        certification_id=f"certification_live_e2e_{version:04d}",
    )


def _turn_body(
    skill: SkillSnapshot | SkillRef,
    *,
    turn_id: str,
    client_turn_sequence: int,
) -> bytes:
    skill_ref = skill.ref if isinstance(skill, SkillSnapshot) else skill
    body = {
        "turn_id": turn_id,
        "expected_world_revision": 5,
        "input": {
            "type": "MESSAGE",
            "text": "Execute the bound certified watering Skill and report only verified facts.",
            "locale": "en-US",
        },
        "skill_bindings": [
            {
                "skill_id": skill_ref.skill_id,
                "skill_version_id": skill_ref.skill_version_id,
                "artifact_sha256": skill_ref.artifact_sha256,
                "certification_id": skill_ref.certification_id,
            }
        ],
        "client_state": {
            "last_event_sequence": 0,
            "client_turn_sequence": client_turn_sequence,
        },
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _ObservedTurn:
    turn_id: str
    idempotency_key: str
    body: bytes
    accepted: dict[str, object]
    command: dict[str, object]
    run: dict[str, object]
    evidence: tuple[dict[str, object], ...]
    committed: CommittedAgentTurn


class AgentBackendRoleLiveE2E(unittest.IsolatedAsyncioTestCase):
    """Real Provider → formal HTTP → Worker → Docker → Bug/Book acceptance."""

    async def test_real_three_failures_bug_then_success_book_replay_restart(self) -> None:
        generation_budget = _required_generation_budget()
        endpoint = _required_provider_setting("YAYA_LLM_ENDPOINT")
        api_key = _required_provider_api_key()
        model = _required_provider_setting("YAYA_LLM_MODEL")
        provider = _required_provider_setting("YAYA_LLM_PROVIDER")
        thinking_mode = _provider_thinking_mode()

        http_server: Any | None = None
        http_thread: threading.Thread | None = None
        agent_stop: asyncio.Event | None = None
        learner_stop: asyncio.Event | None = None
        agent_task: asyncio.Task[None] | None = None
        learner_task: asyncio.Task[None] | None = None
        artifact_targets: list[Path] = []
        with ExitStack() as stack:
            generation_guard, generation_counter = _generation_budget_guard(generation_budget)
            stack.enter_context(generation_guard)
            raw_root = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="yaya-role-live-e2e-")
            )
            root = Path(raw_root).resolve()
            build_root = root / "build"
            artifact_root = root / "artifacts"
            build_root.mkdir()
            artifact_root.mkdir()

            failed_executable = compile_linux(
                _FAILURE_CPP,
                build_root,
                "watering_role_failure",
            )
            success_executable = compile_linux(
                _SUCCESS_CPP,
                build_root,
                "watering_role_success",
            )
            raw_failed_ref, failed_target = install_artifact(
                failed_executable,
                artifact_root,
            )
            raw_success_ref, success_target = install_artifact(
                success_executable,
                artifact_root,
            )
            artifact_targets.extend((failed_target, success_target))
            for target in artifact_targets:
                stack.callback(target.chmod, stat.S_IWRITE | stat.S_IREAD)
            failed_ref = _versioned_ref(raw_failed_ref, 1)
            success_ref = _versioned_ref(raw_success_ref, 2)

            with postgres_test_server() as postgres:
                settings = self._settings(
                    postgres.dsn,
                    artifact_root,
                    endpoint=endpoint,
                    api_key=api_key,
                    model=model,
                    provider=provider,
                    thinking_mode=thinking_mode,
                )
                composition = await create_production_composition(settings)
                failed_skill, success_skill, success_certified = await self._seed_initial_authority(
                    composition,
                    failed_ref=failed_ref,
                    success_ref=success_ref,
                )
                self.assertEqual(
                    await self._durable_counts(composition),
                    {
                        "commands": 0,
                        "jobs": 0,
                        "runs": 0,
                        "evidence": 0,
                        "invocations": 0,
                        "turns": 0,
                        "messages": 0,
                        "interactions": 0,
                        "events": 0,
                        "projection_outbox": 0,
                        "worker_outbox": 0,
                        "learner_models": 0,
                        "learner_jobs": 0,
                        "learner_receipts": 0,
                        "learner_failures": 0,
                        "model_requests": 0,
                        "world_revision": 5,
                        "world_sequence": 0,
                    },
                    "seed helper may create authority, never outcomes or projections",
                )

                http_server, http_thread, port = self._start_http(composition)
                token = composition.authenticator.issue_for_test(
                    make_operation().actor,
                    now=datetime.now(UTC),
                )
                agent_stop = asyncio.Event()
                learner_stop = asyncio.Event()
                agent_task = asyncio.create_task(
                    composition.worker.run_forever(agent_stop),
                    name="role-live-agent-worker",
                )
                learner_task = asyncio.create_task(
                    composition.learner_worker.run_forever(learner_stop),
                    name="role-live-learner-worker",
                )
                try:
                    failures: list[_ObservedTurn] = []
                    expected_roles = ("teaching_agent", "teaching_agent", "bug_agent")
                    for sequence, (turn_id, expected_role) in enumerate(
                        zip(_FAILURE_TURNS, expected_roles, strict=True),
                        start=1,
                    ):
                        observed = await self._submit_and_observe(
                            composition,
                            port,
                            token,
                            failed_skill,
                            turn_id=turn_id,
                            client_turn_sequence=sequence,
                            idempotency_key=f"agent-turn:role-live:failure:{sequence:04d}",
                            expected_role=expected_role,
                            expected_command_status="REJECTED",
                            expected_run_status="REJECTED",
                        )
                        self._assert_failure_turn(observed, sequence, expected_role)
                        failures.append(observed)
                        await self._await_learner_projection(
                            composition,
                            expected=sequence,
                        )
                        await self._assert_learner_snapshot(
                            composition,
                            expected_revision=sequence,
                        )

                    self.assertEqual(
                        len({cast(str, item.run["run_id"]) for item in failures}),
                        3,
                    )
                    self.assertEqual(
                        len({cast(str, item.command["command_id"]) for item in failures}),
                        3,
                    )
                    self.assertEqual(len({item.turn_id for item in failures}), 3)
                    self.assertEqual(
                        {item.committed.event.failure_key for item in failures},
                        {"watering_loop_short"},
                    )
                    self.assertEqual(
                        {
                            cast(
                                dict[str, object],
                                cast(
                                    dict[str, object],
                                    cast(dict[str, object], item.run["world_application"])[
                                        "failure"
                                    ],
                                )["details"],
                            )["reason"]
                            for item in failures
                        },
                        {"watering_loop_short"},
                    )

                    await self._activate_second_version(
                        composition,
                        success_skill,
                        success_certified,
                    )
                    success = await self._submit_and_observe(
                        composition,
                        port,
                        token,
                        success_skill,
                        turn_id=_SUCCESS_TURN,
                        client_turn_sequence=4,
                        idempotency_key="agent-turn:role-live:success:0004",
                        expected_role="book_agent",
                        expected_command_status="APPLIED",
                        expected_run_status="SUCCEEDED",
                    )
                    self._assert_success_turn(success)
                    await self._assert_world_http(composition, port, token)
                    await self._await_learner_projection(composition, expected=4)
                    await self._assert_learner_snapshot(composition, expected_revision=4)

                    product = await self._read_product_pages(
                        composition,
                        port,
                        token,
                        expected=(failures[0], failures[1], failures[2], success),
                        suffix="before-replay",
                    )

                    before_replay = await self._durable_counts(composition)
                    model_requests, finished = await self._model_trace_graph(composition)
                    observed_turns = (*failures, success)
                    final_roles = {
                        item.turn_id: cast(str, item.committed.route.role)
                        for item in observed_turns
                    }
                    expected_model_keys = {
                        (item.turn_id, role)
                        for item in observed_turns
                        for role in ("xiaohutao", final_roles[item.turn_id])
                    }
                    self.assertEqual(set(model_requests), expected_model_keys)
                    self.assertEqual(set(finished), expected_model_keys)
                    for item in observed_turns:
                        self.assertEqual(model_requests[(item.turn_id, "xiaohutao")], 2)
                        final_requests = model_requests[(item.turn_id, final_roles[item.turn_id])]
                        self.assertGreaterEqual(final_requests, 1)
                        self.assertLessEqual(final_requests, 3)
                    for trace in finished.values():
                        self.assertIs(trace.fields["fallback"], False)
                        self.assertEqual(trace.fields["model_provider"], provider)
                    self.assertEqual(
                        before_replay["model_requests"],
                        sum(model_requests.values()),
                    )
                    self.assertLessEqual(sum(model_requests.values()), generation_budget)
                    self.assertEqual(generation_counter.used, sum(model_requests.values()))
                    self.assertEqual(
                        {
                            key: value
                            for key, value in before_replay.items()
                            if key != "model_requests"
                        },
                        {
                            "commands": 4,
                            "jobs": 4,
                            "runs": 4,
                            "evidence": 5,
                            "invocations": 4,
                            "turns": 8,
                            "messages": 4,
                            "interactions": 4,
                            "events": 13,
                            "projection_outbox": 13,
                            "worker_outbox": 4,
                            "learner_models": 1,
                            "learner_jobs": 4,
                            "learner_receipts": 4,
                            "learner_failures": 0,
                            "world_revision": 6,
                            "world_sequence": 1,
                        },
                    )
                    # Four public finals each append feedback and Learner-inference
                    # events; the Learner Worker appends four model-update events,
                    # and the successful World appends the thirteenth event.  The
                    # four model updates are also the exact Worker-outbox messages.
                    requests_by_role: Counter[str] = Counter()
                    for (_, role), request_count in model_requests.items():
                        requests_by_role[role] += request_count
                    print(
                        "YAYA_REAL_PROVIDER_LIVE_EVIDENCE="
                        + json.dumps(
                            {
                                "durable_counts": before_replay,
                                "model_requests_by_role": dict(sorted(requests_by_role.items())),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    replay_fingerprint = await self._business_fingerprint(composition)
                    for index, observed in enumerate((*failures, success), start=1):
                        await self._assert_http_replay(
                            port,
                            token,
                            observed,
                            suffix=f"replay{index:04d}",
                        )
                    await asyncio.sleep(0.5)
                    self.assertEqual(await self._durable_counts(composition), before_replay)
                    self.assertEqual(
                        await self._business_fingerprint(composition),
                        replay_fingerprint,
                    )

                    await self._stop_worker(agent_stop, agent_task)
                    await self._stop_worker(learner_stop, learner_task)
                    agent_stop = None
                    learner_stop = None
                    agent_task = None
                    learner_task = None
                    await self._stop_http(http_server, http_thread)
                    http_server = None
                    http_thread = None

                    before_restart = await self._durable_counts(composition)
                    restart_fingerprint = await self._business_fingerprint(composition)
                    restarted = await create_production_composition(settings)
                    http_server, http_thread, restarted_port = self._start_http(restarted)
                    agent_stop = asyncio.Event()
                    learner_stop = asyncio.Event()
                    agent_task = asyncio.create_task(restarted.worker.run_forever(agent_stop))
                    learner_task = asyncio.create_task(
                        restarted.learner_worker.run_forever(learner_stop)
                    )
                    await asyncio.sleep(0.5)
                    restarted_product = await self._read_product_pages(
                        restarted,
                        restarted_port,
                        token,
                        expected=(failures[0], failures[1], failures[2], success),
                        suffix="after-restart",
                    )
                    self.assertEqual(restarted_product, product)
                    self.assertEqual(await self._durable_counts(restarted), before_restart)
                    self.assertEqual(
                        await self._business_fingerprint(restarted),
                        restart_fingerprint,
                    )
                finally:
                    if agent_stop is not None and agent_task is not None:
                        await self._stop_worker(agent_stop, agent_task)
                    if learner_stop is not None and learner_task is not None:
                        await self._stop_worker(learner_stop, learner_task)
                    if http_server is not None and http_thread is not None:
                        await self._stop_http(http_server, http_thread)

    @staticmethod
    def _settings(
        dsn: str,
        artifact_root: Path,
        *,
        endpoint: str,
        api_key: str,
        model: str,
        provider: str,
        thinking_mode: Literal["enabled", "disabled"] | None,
    ) -> ProductionSettings:
        return ProductionSettings(
            database_dsn=dsn,
            artifact_root=artifact_root,
            contracts_root=CONTRACTS_ROOT,
            auth_hmac_secret=_JWT_SECRET,
            auth_issuer=_JWT_ISSUER,
            auth_audience=_JWT_AUDIENCE,
            llm_mode="provider",
            llm_endpoint=endpoint,
            llm_api_key=api_key,
            llm_model=model,
            llm_provider=provider,
            llm_response_format="json_object",
            llm_thinking_mode=thinking_mode,
            llm_max_response_bytes=2_097_152,
            allow_insecure_llm_localhost=False,
            http_host="127.0.0.1",
            http_port=8080,
            worker_id="worker_role_live_e2e_0001",
            worker_lease_seconds=180,
            worker_poll_ms=25,
            learner_worker_id="learner_role_live_e2e_0001",
            learner_worker_lease_seconds=60,
            learner_worker_poll_ms=25,
            sandbox_wall_ms=3_000,
            sandbox_cpu_ms=1_000,
            sandbox_memory_bytes=67_108_864,
            sandbox_max_intents=8,
            sandbox_max_output_bytes=65_536,
            sandbox_max_processes=1,
            sandbox_image=PINNED_GCC_IMAGE,
            docker_executable="docker",
        )

    async def _seed_initial_authority(
        self,
        composition: ProductionComposition,
        *,
        failed_ref: SkillRef,
        success_ref: SkillRef,
    ) -> tuple[SkillSnapshot, SkillSnapshot, CertifiedSkill]:
        operation = make_operation(command_id="cmd_live_role_seed_0001")
        task = replace(make_task(operation), knowledge_points=("for_loop",))
        session = SessionSnapshot(
            session_id=SESSION_ID,
            student_id=operation.actor.actor_id,
            task_id=TASK_ID,
            world_id=WORLD_ID,
            request_context=operation,
        )
        failed_skill = self._skill_snapshot(failed_ref, _FAILURE_CPP, operation)
        success_skill = self._skill_snapshot(success_ref, _SUCCESS_CPP, operation)
        failed_certified = self._certified(failed_skill, semantic_version="1.0.0")
        success_certified = self._certified(success_skill, semantic_version="2.0.0")
        active = ActiveSkill(failed_certified, 1, operation.requested_at)
        state = make_world_state()
        connection = await composition.database.connect()
        try:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO yaya_tasks(
                      tenant_id,task_id,actor_id,content_hash,snapshot_json
                    ) VALUES (%s,%s,%s,%s,%s)
                    """,
                    (
                        operation.actor.tenant_id,
                        TASK_ID,
                        operation.actor.actor_id,
                        operation.content_ref.content_hash,
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
                    """,
                    (
                        operation.actor.tenant_id,
                        WORLD_ID,
                        operation.actor.actor_id,
                        operation.content_ref.content_hash,
                        f"world:{WORLD_ID}",
                        canonical_json_sha256(state),
                        Jsonb(state),
                        Jsonb(encode(_request_context())),
                    ),
                )
                await connection.execute(
                    """
                    INSERT INTO yaya_agent_sessions(
                      tenant_id,session_id,actor_id,task_id,world_id,content_hash,snapshot_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        operation.actor.tenant_id,
                        SESSION_ID,
                        operation.actor.actor_id,
                        TASK_ID,
                        WORLD_ID,
                        operation.content_ref.content_hash,
                        Jsonb(encode(session)),
                    ),
                )
                for skill, certified, active_flag in (
                    (failed_skill, failed_certified, True),
                    (success_skill, success_certified, False),
                ):
                    await connection.execute(
                        """
                        INSERT INTO yaya_skills(
                          tenant_id,skill_id,skill_version_id,certification_id,actor_id,
                          session_id,content_hash,artifact_sha256,snapshot_json,active
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            operation.actor.tenant_id,
                            skill.ref.skill_id,
                            skill.ref.skill_version_id,
                            skill.ref.certification_id,
                            operation.actor.actor_id,
                            SESSION_ID,
                            operation.content_ref.content_hash,
                            skill.ref.artifact_sha256,
                            Jsonb(encode(skill)),
                            active_flag,
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
                            operation.actor.tenant_id,
                            certified.certification_id,
                            certified.skill_id,
                            certified.skill_version_id,
                            certified.artifact.artifact_sha256,
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
                        operation.actor.tenant_id,
                        operation.actor.actor_id,
                        failed_certified.skill_id,
                        Jsonb(encode(active)),
                    ),
                )
        finally:
            await connection.close()
        return failed_skill, success_skill, success_certified

    @staticmethod
    def _skill_snapshot(
        skill_ref: SkillRef,
        source: str,
        operation: Any,
    ) -> SkillSnapshot:
        return SkillSnapshot(
            ref=skill_ref,
            source_code=source,
            source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            entrypoint=(
                "watering_role_failure" if source == _FAILURE_CPP else "watering_role_success"
            ),
            parameter_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["length"],
                "properties": {"length": {"type": "integer", "const": 8}},
            },
            request_context=operation,
        )

    @staticmethod
    def _certified(skill: SkillSnapshot, *, semantic_version: str) -> CertifiedSkill:
        artifact = BuildArtifact(
            artifact_sha256=skill.ref.artifact_sha256,
            source_sha256=skill.source_sha256,
            compiler_profile="gcc-cpp20-container",
            compiler_version="gcc-cpp20-container",
            sandbox_image_digest=PINNED_GCC_IMAGE,
            test_suite_version="agent-role-live-e2e-v1",
            artifact_uri=f"file:///certified-artifacts/{skill.ref.artifact_sha256}",
        )
        return CertifiedSkill(
            certification_id=skill.ref.certification_id,
            skill_id=skill.ref.skill_id,
            skill_version_id=skill.ref.skill_version_id,
            semantic_version=semantic_version,
            artifact=artifact,
            capabilities=("WORLD_READ", "WATER"),
            certified_at=skill.request_context.requested_at,
            revoked_at=None,
        )

    async def _activate_second_version(
        self,
        composition: ProductionComposition,
        skill: SkillSnapshot,
        certified: CertifiedSkill,
    ) -> None:
        active = ActiveSkill(certified, 2, datetime.now(UTC))
        connection = await composition.database.connect()
        try:
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE yaya_skills SET active=(skill_version_id=%s)
                    WHERE tenant_id=%s AND actor_id=%s AND content_hash=%s AND skill_id=%s
                    """,
                    (
                        skill.ref.skill_version_id,
                        skill.request_context.actor.tenant_id,
                        skill.request_context.actor.actor_id,
                        skill.request_context.content_ref.content_hash,
                        skill.ref.skill_id,
                    ),
                )
                updated = await connection.execute(
                    """
                    UPDATE yaya_registry_active SET record_json=%s,revision=2
                    WHERE tenant_id=%s AND actor_id=%s AND skill_id=%s AND revision=1
                    """,
                    (
                        Jsonb(encode(active)),
                        skill.request_context.actor.tenant_id,
                        skill.request_context.actor.actor_id,
                        skill.ref.skill_id,
                    ),
                )
                self.assertEqual(updated.rowcount, 1)
        finally:
            await connection.close()

    async def _submit_and_observe(
        self,
        composition: ProductionComposition,
        port: int,
        token: str,
        skill: SkillSnapshot | SkillRef,
        *,
        turn_id: str,
        client_turn_sequence: int,
        idempotency_key: str,
        expected_role: str,
        expected_command_status: str,
        expected_run_status: str,
    ) -> _ObservedTurn:
        body = _turn_body(
            skill,
            turn_id=turn_id,
            client_turn_sequence=client_turn_sequence,
        )
        status, headers, accepted = await asyncio.to_thread(
            self._http,
            port,
            "POST",
            f"/v1/agent-sessions/{getattr(self, 'session_id', SESSION_ID)}/turns",
            self._headers(
                token,
                f"accept{client_turn_sequence:04d}",
                idempotency_key=idempotency_key,
            ),
            body,
        )
        self.assertEqual(status, 202, accepted)
        self.assertEqual(headers["idempotency-replayed"], "false")
        composition.validator.validate(
            "schemas/game/accepted-game-job.schema.json",
            accepted,
        )
        command_id = cast(str, accepted["command_id"])
        command = await self._await_terminal(composition, port, token, command_id)
        if command["status"] != expected_command_status:
            self.fail(
                {
                    "command": command,
                    "diagnostics": await self._diagnostics(composition),
                }
            )
        self.assertIs(command["terminal"], True)
        run_target = cast(dict[str, str], command["links"])["run"]
        run = await self._get_json(port, token, run_target, f"run{client_turn_sequence:04d}")
        composition.validator.validate("schemas/game/run.schema.json", run)
        self.assertEqual(run["status"], expected_run_status)
        feedback = cast(dict[str, object], run["agent_feedback"])
        self.assertEqual(feedback["source"], "provider")
        self.assertIs(feedback["degraded"], False)
        self.assertIsNone(feedback["fallback_reason"])
        evidence_documents: list[dict[str, object]] = []
        for index, reference in enumerate(
            cast(list[dict[str, object]], run["evidence_refs"]),
            start=1,
        ):
            evidence = await self._get_json(
                port,
                token,
                f"/v1/evidence/{reference['evidence_id']}",
                f"evidence{client_turn_sequence:02d}{index:02d}",
            )
            composition.validator.validate("schemas/game/evidence.schema.json", evidence)
            self.assertEqual(evidence["evidence_ref"], reference)
            evidence_documents.append(evidence)
        committed, job_state = await self._committed_turn(composition, command_id)
        self.assertEqual(job_state, "DONE")
        self.assertEqual(committed.route.role, expected_role)
        self.assertEqual(committed.decision.draft.role, expected_role)
        self.assertEqual(committed.decision.source, "provider")
        self.assertFalse(committed.decision.degraded)
        self.assertIsNone(committed.decision.fallback_reason)
        self.assertEqual(
            committed.decision.evidence_refs,
            committed.event.evidence_refs,
        )
        return _ObservedTurn(
            turn_id,
            idempotency_key,
            body,
            accepted,
            command,
            run,
            tuple(evidence_documents),
            committed,
        )

    def _assert_failure_turn(
        self,
        observed: _ObservedTurn,
        failure_count: int,
        role: str,
    ) -> None:
        self.assertEqual(observed.committed.event.event_type, "run_failed")
        self.assertEqual(observed.committed.event.failure_count, failure_count)
        self.assertEqual(observed.committed.event.failure_key, "watering_loop_short")
        sandbox = cast(dict[str, object], observed.run["sandbox"])
        self.assertEqual(sandbox["status"], "SUCCEEDED")
        self.assertEqual(len(cast(list[object], sandbox["action_intents"])), 7)
        world = cast(dict[str, object], observed.run["world_application"])
        self.assertEqual(world["status"], "REJECTED")
        self.assertIsNone(world["receipt"])
        world_failure = cast(dict[str, object], world["failure"])
        self.assertEqual(world_failure["code"], "WORLD_RULE_REJECTED")
        self.assertEqual(
            cast(dict[str, object], world_failure["details"])["reason"],
            "watering_loop_short",
        )
        self.assertEqual(len(observed.evidence), 1)
        payload = cast(dict[str, object], observed.evidence[0]["payload"])
        self.assertEqual(payload["evidence_kind"], "SKILL_RUN")
        self.assertEqual(payload["intent_count"], 7)
        self.assertEqual(payload["world_status"], "REJECTED")
        directive = observed.committed.decision.teaching_directive
        self.assertIsNotNone(directive)
        assert directive is not None
        self.assertEqual(directive.phase, TeachingPhase.RECTIFICATION)
        self.assertFalse(directive.patch_eligible)
        self.assertFalse(directive.full_solution_eligible)
        self.assertEqual(
            set(directive.required_evidence_ids),
            {item.evidence_id for item in observed.committed.event.evidence_refs},
        )
        inference = observed.committed.decision.draft.learner_inference
        self.assertIsNotNone(inference)
        assert inference is not None
        self.assertEqual(
            set(inference.evidence_ids),
            set(directive.required_evidence_ids),
        )
        if role == "bug_agent":
            self.assertEqual(directive.allowed_response_types, ("question",))
            self.assertEqual(observed.committed.decision.draft.response_type, "question")
            self.assertIsNotNone(observed.committed.decision.draft.question)

    def _assert_success_turn(self, observed: _ObservedTurn) -> None:
        self.assertEqual(observed.committed.event.event_type, "task_completed")
        self.assertEqual(observed.committed.event.failure_count, 0)
        self.assertIsNone(observed.committed.event.failure_key)
        sandbox = cast(dict[str, object], observed.run["sandbox"])
        self.assertEqual(sandbox["status"], "SUCCEEDED")
        self.assertEqual(len(cast(list[object], sandbox["action_intents"])), 8)
        world = cast(dict[str, object], observed.run["world_application"])
        self.assertEqual(world["status"], "COMMITTED")
        self.assertIsNone(world["failure"])
        receipt = cast(dict[str, object], world["receipt"])
        self.assertEqual(receipt["previous_revision"], 5)
        self.assertEqual(receipt["world_revision"], 6)
        self.assertEqual(receipt["first_event_sequence"], 1)
        self.assertEqual(receipt["last_event_sequence"], 1)
        self.assertEqual(
            {
                cast(dict[str, object], item["evidence_ref"])["evidence_type"]
                for item in observed.evidence
            },
            {"SANDBOX_LOG", "WORLD_COMMIT"},
        )
        directive = observed.committed.decision.teaching_directive
        self.assertIsNotNone(directive)
        assert directive is not None
        self.assertEqual(directive.phase, TeachingPhase.SUMMARIZATION)
        self.assertEqual(directive.allowed_response_types, ("growth_summary",))
        self.assertEqual(directive.hint_level, 0)
        self.assertFalse(directive.patch_eligible)
        self.assertFalse(directive.full_solution_eligible)
        decision = observed.committed.decision.draft
        self.assertEqual(decision.response_type, "growth_summary")
        self.assertIsNone(decision.hint_level)
        self.assertIsNone(decision.skill_patch)
        inference = decision.learner_inference
        self.assertIsNotNone(inference)
        assert inference is not None
        public_copy = (decision.message + "\n" + inference.reason).casefold()
        for forbidden in (
            "permanent mastery",
            "mastered forever",
            "never fail again",
            "永久掌握",
            "永不再犯",
        ):
            self.assertNotIn(forbidden, public_copy)

    async def _assert_world_http(
        self,
        composition: ProductionComposition,
        port: int,
        token: str,
    ) -> None:
        snapshot = await self._get_json(
            port,
            token,
            f"/v1/worlds/{WORLD_ID}/snapshot",
            "roleworldsnapshot",
        )
        self.assertEqual(snapshot["revision"], 6)
        self.assertEqual(snapshot["last_event_sequence"], 1)
        plots = cast(list[dict[str, object]], cast(dict[str, object], snapshot["state"])["plots"])
        self.assertEqual([plot["hydration"] for plot in plots], [100] * 8)
        events = await self._get_json(
            port,
            token,
            f"/v1/worlds/{WORLD_ID}/events?after_sequence=0&limit=1",
            "roleworldevents",
        )
        self.assertEqual(len(cast(list[object], events["events"])), 1)
        event = cast(list[dict[str, object]], events["events"])[0]
        self.assertEqual(event["event_type"], "world.committed")
        composition.validator.validate(
            "schemas/game/world-event-page.schema.json",
            events,
        )

    async def _read_product_pages(
        self,
        composition: ProductionComposition,
        port: int,
        token: str,
        *,
        expected: tuple[_ObservedTurn, ...],
        suffix: str,
    ) -> dict[str, object]:
        before = await self._business_fingerprint(composition)
        after_sequence = 0
        pages: list[bytes] = []
        interactions: list[dict[str, object]] = []
        high_watermark: int | None = None
        while True:
            headers = self._headers(token, f"product{suffix}{after_sequence:04d}")
            headers.pop("Idempotency-Key")
            headers.pop("Content-Type")
            status, response_headers, body = await asyncio.to_thread(
                self._http_raw,
                port,
                "GET",
                f"/product-experience/v1/sessions/{getattr(self, 'session_id', SESSION_ID)}/"
                f"agent-interactions?after_sequence={after_sequence}&limit=1",
                headers,
            )
            self.assertEqual(status, 200, body)
            page = cast(dict[str, object], json.loads(body.decode("utf-8")))
            composition.validator.validate(
                "schemas/product-experience/agent-interaction-page.schema.json",
                page,
            )
            self.assertEqual(page["requested_after_sequence"], after_sequence)
            self.assertEqual(page["requested_limit"], 1)
            if high_watermark is None:
                high_watermark = cast(int, page["high_watermark_sequence"])
            self.assertEqual(page["high_watermark_sequence"], high_watermark)
            self.assertEqual(
                response_headers["x-interaction-high-watermark"],
                str(high_watermark),
            )
            page_items = cast(list[dict[str, object]], page["interactions"])
            self.assertEqual(len(page_items), 1)
            interactions.extend(page_items)
            pages.append(body)
            after_sequence = cast(int, page["next_after_sequence"])
            if page["has_more"] is False:
                break
        self.assertEqual(high_watermark, len(expected))
        self.assertEqual(
            [cast(int, item["sequence"]) for item in interactions],
            list(range(1, len(expected) + 1)),
        )
        self.assertEqual(
            [cast(str, item["role"]) for item in interactions],
            ["teaching_agent", "teaching_agent", "bug_agent", "book_agent"],
        )
        self.assertEqual(
            [
                cast(str, cast(dict[str, object], item["feedback"])["run_id"])
                for item in interactions
            ],
            [cast(str, item.run["run_id"]) for item in expected],
        )
        gets: dict[str, dict[str, object]] = {}
        for index, interaction in enumerate(interactions, start=1):
            interaction_id = cast(str, interaction["interaction_id"])
            headers = self._headers(token, f"productget{suffix}{index:04d}")
            headers.pop("Idempotency-Key")
            headers.pop("Content-Type")
            status, response_headers, body = await asyncio.to_thread(
                self._http_raw,
                port,
                "GET",
                f"/product-experience/v1/sessions/"
                f"{getattr(self, 'session_id', SESSION_ID)}/agent-interactions/{interaction_id}",
                headers,
            )
            self.assertEqual(status, 200, body)
            restored = cast(dict[str, object], json.loads(body.decode("utf-8")))
            composition.validator.validate(
                "schemas/product-experience/agent-interaction.schema.json",
                restored,
            )
            self.assertEqual(restored, interaction)
            self.assertEqual(
                response_headers["x-interaction-revision"],
                str(interaction["interaction_revision"]),
            )
            etag = response_headers["etag"]
            self.assertTrue(etag.startswith('"interaction:') and etag.endswith('"'))
            gets[interaction_id] = {"body": body, "etag": etag}
        self.assertEqual(
            await self._business_fingerprint(composition),
            before,
            "Product list/get performed a durable write or dependency side effect",
        )
        return {"pages": pages, "gets": gets}

    async def _assert_http_replay(
        self,
        port: int,
        token: str,
        observed: _ObservedTurn,
        *,
        suffix: str,
    ) -> None:
        status, headers, accepted = await asyncio.to_thread(
            self._http,
            port,
            "POST",
            f"/v1/agent-sessions/{getattr(self, 'session_id', SESSION_ID)}/turns",
            self._headers(
                token,
                suffix,
                idempotency_key=observed.idempotency_key,
            ),
            observed.body,
        )
        self.assertEqual(status, 202, accepted)
        self.assertEqual(headers["idempotency-replayed"], "true")
        self.assertEqual(accepted, observed.accepted)

    async def _await_terminal(
        self,
        composition: ProductionComposition,
        port: int,
        token: str,
        command_id: str,
    ) -> dict[str, object]:
        deadline = asyncio.get_running_loop().time() + 180
        attempt = 0
        while asyncio.get_running_loop().time() < deadline:
            attempt += 1
            command = await self._get_json(
                port,
                token,
                f"/v1/commands/{command_id}",
                f"command{command_id[-6:]}{attempt:04d}",
            )
            composition.validator.validate("schemas/game/command.schema.json", command)
            if command.get("terminal") is True:
                return command
            await asyncio.sleep(0.2)
        diagnostics = await self._diagnostics(composition)
        self.fail(f"Command {command_id} did not terminate: {diagnostics!r}")

    async def _committed_turn(
        self,
        composition: ProductionComposition,
        command_id: str,
    ) -> tuple[CommittedAgentTurn, str]:
        connection = await composition.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT t.record_json,j.state,
                       (SELECT count(*)::int
                        FROM yaya_agent_interactions i
                        WHERE i.tenant_id=t.tenant_id
                          AND i.command_id=j.command_id) AS interaction_count
                FROM yaya_agent_turns t
                JOIN yaya_command_jobs j
                  ON j.tenant_id=t.tenant_id
                 AND j.command_id=t.record_json #>> '{$fields,event,$fields,command_id}'
                WHERE t.record_json #>> '{$fields,event,$fields,command_id}'=%s
                """,
                (command_id,),
            )
            rows = await cursor.fetchall()
        finally:
            await connection.close()
        self.assertEqual(len(rows), 2)
        committed = [decode_as(row["record_json"], CommittedAgentTurn) for row in rows]
        roots = [turn for turn in committed if turn.event.event_type == "run_skill_requested"]
        derived = [
            turn for turn in committed if turn.event.event_type in {"run_failed", "task_completed"}
        ]
        self.assertEqual(len(roots), 1)
        self.assertEqual(len(derived), 1)
        self.assertEqual(roots[0].route.role, "xiaohutao")
        self.assertEqual(roots[0].decision.draft.role, "xiaohutao")
        self.assertEqual(roots[0].decision.source, "provider")
        self.assertFalse(roots[0].decision.degraded)
        self.assertIsNone(roots[0].decision.fallback_reason)
        self.assertIsNone(roots[0].decision.teaching_directive)
        self.assertEqual({cast(str, row["state"]) for row in rows}, {"DONE"})
        self.assertEqual({cast(int, row["interaction_count"]) for row in rows}, {1})
        return derived[0], "DONE"

    async def _await_learner_projection(
        self,
        composition: ProductionComposition,
        *,
        expected: int,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + 120
        while asyncio.get_running_loop().time() < deadline:
            connection = await composition.database.connect(autocommit=True)
            try:
                cursor = await connection.execute(
                    """
                    SELECT count(*)::int AS total,
                           count(*) FILTER (WHERE state='SUCCEEDED')::int AS succeeded,
                           count(*) FILTER (WHERE state='FAILED')::int AS failed
                    FROM yaya_learner_projection_jobs
                    """
                )
                row = await cursor.fetchone()
            finally:
                await connection.close()
            if row is not None and row["failed"]:
                connection = await composition.database.connect(autocommit=True)
                try:
                    jobs = await connection.execute(
                        """
                        SELECT job_id,state,attempt,last_error_code,last_error_json
                        FROM yaya_learner_projection_jobs
                        ORDER BY source_stream_sequence,job_id
                        """
                    )
                    failures = await connection.execute(
                        """
                        SELECT job_id,classification,error_code,error_json
                        FROM yaya_learner_projection_failures
                        ORDER BY recorded_at,failure_id
                        """
                    )
                    diagnostics = {
                        "summary": dict(row),
                        "jobs": [dict(item) for item in await jobs.fetchall()],
                        "failures": [dict(item) for item in await failures.fetchall()],
                    }
                finally:
                    await connection.close()
                self.fail(f"Learner projection failed: {diagnostics!r}")
            if row is not None and row["total"] == expected and row["succeeded"] == expected:
                return
            await asyncio.sleep(0.1)
        self.fail("Learner projection did not reach the expected durable receipts")

    async def _assert_learner_snapshot(
        self,
        composition: ProductionComposition,
        *,
        expected_revision: int,
    ) -> None:
        result = await composition.learner_store.get_snapshot(
            make_operation().actor.actor_id,
            make_operation(),
        )
        self.assertIsInstance(result, Success)
        assert isinstance(result, Success)
        self.assertEqual(result.value.revision, expected_revision)
        self.assertEqual(result.value.projected_through_sequence, expected_revision)
        self.assertIn("for_loop", result.value.competencies)

    @staticmethod
    async def _model_trace_graph(
        composition: ProductionComposition,
    ) -> tuple[
        dict[tuple[str, str], int],
        dict[tuple[str, str], AgentTraceEvent],
    ]:
        connection = await composition.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                "SELECT trace_json FROM yaya_agent_traces ORDER BY trace_record_id"
            )
            traces = [
                decode_as(row["trace_json"], AgentTraceEvent) for row in await cursor.fetchall()
            ]
        finally:
            await connection.close()
        requests = Counter(
            (trace.turn_id, trace.role) for trace in traces if trace.name == "agent.model.requested"
        )
        finished_items = [trace for trace in traces if trace.name == "agent.turn.finished"]
        finished = {(trace.turn_id, trace.role): trace for trace in finished_items}
        if len(finished) != len(finished_items):
            raise AssertionError("one live role emitted duplicate finished traces")
        return dict(requests), finished

    @staticmethod
    async def _durable_counts(
        composition: ProductionComposition,
    ) -> dict[str, int]:
        connection = await composition.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_commands)::int AS commands,
                  (SELECT count(*) FROM yaya_command_jobs)::int AS jobs,
                  (SELECT count(*) FROM yaya_runs)::int AS runs,
                  (SELECT count(*) FROM yaya_evidence)::int AS evidence,
                  (SELECT count(*) FROM yaya_skill_invocations)::int AS invocations,
                  (SELECT count(*) FROM yaya_agent_turns WHERE record_json IS NOT NULL)::int AS turns,
                  (SELECT count(*) FROM yaya_agent_messages)::int AS messages,
                  (SELECT count(*) FROM yaya_agent_interactions)::int AS interactions,
                  (SELECT count(*) FROM yaya_events)::int AS events,
                  (SELECT count(*) FROM yaya_projection_outbox)::int AS projection_outbox,
                  (SELECT count(*) FROM yaya_outbox)::int AS worker_outbox,
                  (SELECT count(*) FROM yaya_learner_models)::int AS learner_models,
                  (SELECT count(*) FROM yaya_learner_projection_jobs)::int AS learner_jobs,
                  (SELECT count(*) FROM yaya_learner_projection_receipts)::int AS learner_receipts,
                  (SELECT count(*) FROM yaya_learner_projection_failures)::int AS learner_failures,
                  (SELECT count(*) FROM yaya_agent_traces
                    WHERE trace_json #>> '{$fields,name}'='agent.model.requested')::int
                    AS model_requests,
                  (SELECT revision FROM yaya_worlds WHERE world_id=%s)::int AS world_revision,
                  (SELECT last_event_sequence FROM yaya_worlds WHERE world_id=%s)::int
                    AS world_sequence
                """,
                (WORLD_ID, WORLD_ID),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("durable count query returned no row")
        return {name: cast(int, value) for name, value in row.items()}

    @staticmethod
    async def _business_fingerprint(
        composition: ProductionComposition,
    ) -> dict[str, tuple[int, str]]:
        connection = await composition.database.connect(autocommit=True)
        try:
            fingerprints: dict[str, tuple[int, str]] = {}
            for table in _BUSINESS_TABLES:
                cursor = await connection.execute(
                    f"""
                    SELECT count(*)::int AS count,
                           md5(COALESCE(string_agg(value::text,'' ORDER BY value::text),'')) AS hash
                    FROM (SELECT to_jsonb(t) AS value FROM {table} t) rows
                    """
                )
                row = await cursor.fetchone()
                if row is None:
                    raise AssertionError(f"fingerprint query returned no row for {table}")
                fingerprints[table] = (cast(int, row["count"]), cast(str, row["hash"]))
            return fingerprints
        finally:
            await connection.close()

    @staticmethod
    async def _diagnostics(composition: ProductionComposition) -> dict[str, object]:
        connection = await composition.database.connect(autocommit=True)
        try:
            traces = await connection.execute(
                "SELECT trace_json FROM yaya_agent_traces ORDER BY trace_record_id"
            )
            jobs = await connection.execute(
                "SELECT command_id,state,last_error_code FROM yaya_command_jobs ORDER BY command_id"
            )
            return {
                "traces": [row["trace_json"] for row in await traces.fetchall()],
                "jobs": [dict(row) for row in await jobs.fetchall()],
            }
        finally:
            await connection.close()

    @staticmethod
    def _headers(
        token: str,
        suffix: str,
        *,
        idempotency_key: str = "agent-turn:role-live:default:0001",
    ) -> dict[str, str]:
        normalized = hashlib.sha256(suffix.encode("utf-8")).hexdigest()[:16]
        return {
            "Authorization": f"Bearer {token}",
            "X-Schema-Version": "1.0.0",
            "X-Request-Id": f"req_role_{normalized}",
            "X-Trace-Id": f"trace_role_{normalized}",
            "X-Correlation-Id": f"corr_role_{normalized}",
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _start_http(
        composition: ProductionComposition,
    ) -> tuple[Any, threading.Thread, int]:
        api = ProductionHttpApi(
            game=AgentHttpApi(
                application=composition.application,
                authenticator=composition.authenticator,
                validator=composition.validator,
                student_chain=composition.student_chain_application,
            ),
            product=ProductHttpApi(
                application=composition.product_application,
                authenticator=composition.authenticator,
                validator=composition.validator,
                draft_application=composition.draft_application,
            ),
        )
        ready = threading.Event()
        captured = threading.Event()
        servers: list[Any] = []

        def capture(server: object) -> None:
            servers.append(server)
            captured.set()

        thread = threading.Thread(
            target=serve_http,
            args=(api, "127.0.0.1", 0),
            kwargs={"ready": ready, "server_created": capture},
            daemon=True,
            name="yaya-role-live-http",
        )
        thread.start()
        if not captured.wait(10) or not ready.wait(10) or not servers:
            raise RuntimeError("production role HTTP server did not become ready")
        server = servers[0]
        return server, thread, int(server.server_address[1])

    @staticmethod
    async def _stop_http(server: Any, thread: threading.Thread) -> None:
        await asyncio.to_thread(server.shutdown)
        thread.join(timeout=10)
        if thread.is_alive():
            raise AssertionError("production role HTTP server did not stop")

    @staticmethod
    async def _stop_worker(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
        stop.set()
        await asyncio.wait_for(task, timeout=10)

    @staticmethod
    def _http(
        port: int,
        method: str,
        target: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            return (
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                cast(dict[str, object], payload),
            )
        finally:
            connection.close()

    @staticmethod
    def _http_raw(
        port: int,
        method: str,
        target: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            return (
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                response.read(),
            )
        finally:
            connection.close()

    async def _get_json(
        self,
        port: int,
        token: str,
        target: str,
        suffix: str,
    ) -> dict[str, object]:
        headers = self._headers(token, suffix)
        headers.pop("Idempotency-Key")
        headers.pop("Content-Type")
        status, _, payload = await asyncio.to_thread(
            self._http,
            port,
            "GET",
            target,
            headers,
        )
        self.assertEqual(status, 200, (target, payload))
        return payload


if __name__ == "__main__":
    unittest.main()
