from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import base64
import hashlib
import http.client
import json
import stat
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from yaya_agent_backend.codec import decode_as  # noqa: E402
from yaya_agent_backend.composition import (  # noqa: E402
    create_production_composition,
    production_versions,
)
from yaya_agent_build import CPP20_SAFE_V1_PROFILE  # noqa: E402
from yaya_agent_contracts import SkillRef  # noqa: E402
from yaya_agent_runtime import AgentTraceEvent  # noqa: E402

from tests import test_agent_backend_role_live_e2e as role_live  # noqa: E402
from tests.a8_state_fingerprint import (  # noqa: E402
    A8StateFingerprint,
    a8_state_fingerprint,
    missing_a8_business_tables,
)
from tests.agent_runtime_fixtures import make_operation  # noqa: E402
from tests.postgres_test_support import postgres_test_server  # noqa: E402
from tests.test_agent_backend_role_live_e2e import (  # noqa: E402
    _FAILURE_CPP,
    _FAILURE_TURNS,
    _LIVE_SKILL_ID,
    _SUCCESS_CPP,
    _SUCCESS_TURN,
    _generation_budget_guard,
    _ObservedTurn,
    _provider_thinking_mode,
    _required_generation_budget,
    _required_provider_api_key,
    _required_provider_setting,
)
from tests.test_agent_backend_skill_build_executor import (  # noqa: E402
    AGENT_PROFILE_ID,
    LEARNER_ID,
    TEST_SUITE_VERSION,
    _seed_only_build_authority,
)


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _source_bundle(source: str) -> dict[str, object]:
    return {
        "language": "CPP20",
        "entrypoint": "main.cpp",
        "files": [
            {
                "path": "main.cpp",
                "content": source,
                "content_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            }
        ],
    }


class PublicStudentChainRoleLiveE2E(role_live.AgentBackendRoleLiveE2E):
    """Published authority -> public source chain -> real Provider roles."""

    # Do not inherit the legacy A6 seed-based test.  That test remains on its
    # original class as an independent regression; this class proves A8's
    # student-facing production chain.
    test_real_three_failures_bug_then_success_book_replay_restart = None  # type: ignore[assignment]

    async def test_public_v1_v2_chain_real_provider_replay_and_restart(self) -> None:
        generation_budget = _required_generation_budget()
        endpoint = _required_provider_setting("YAYA_LLM_ENDPOINT")
        api_key = _required_provider_api_key()
        model = _required_provider_setting("YAYA_LLM_MODEL")
        provider = _required_provider_setting("YAYA_LLM_PROVIDER")
        thinking_mode = _provider_thinking_mode()

        http_server: Any | None = None
        agent_stop: asyncio.Event | None = None
        learner_stop: asyncio.Event | None = None
        control_stop: asyncio.Event | None = None
        agent_task: asyncio.Task[None] | None = None
        learner_task: asyncio.Task[None] | None = None
        control_task: asyncio.Task[None] | None = None
        http_thread: Any | None = None
        with ExitStack() as stack:
            generation_guard, generation_counter = _generation_budget_guard(generation_budget)
            stack.enter_context(generation_guard)
            raw_root = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="yaya-public-role-live-e2e-")
            )
            artifact_root = Path(raw_root).resolve() / "artifacts"
            artifact_root.mkdir()
            self.assertEqual(list(artifact_root.iterdir()), [])

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
                versions = production_versions(settings)
                context = make_operation(command_id="cmd_public_role_authority_0001")
                io_test = {
                    "visibility": "PUBLIC",
                    "arguments": ["8"],
                    "stdin_base64": base64.b64encode(b"").decode("ascii"),
                    "expected_stdout_sha256": None,
                }
                await _seed_only_build_authority(
                    composition.database,
                    context_override=context,
                    versions_override=versions,
                    public_tests_override=[{"test_case_id": "public_role_runtime_0001", **io_test}],
                    hidden_tests_override=[
                        {
                            "test_case_id": "hidden_role_runtime_0001",
                            **{**io_test, "visibility": "HIDDEN"},
                        }
                    ],
                    parameter_schema_override={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["length"],
                        "properties": {"length": {"type": "integer", "const": 8}},
                    },
                )

                http_server, http_thread, port = self._start_http(composition)
                token = composition.authenticator.issue_for_test(
                    context.actor,
                    now=datetime.now(UTC),
                )
                agent_stop = asyncio.Event()
                learner_stop = asyncio.Event()
                control_stop = asyncio.Event()
                agent_task = asyncio.create_task(composition.worker.run_forever(agent_stop))
                learner_task = asyncio.create_task(
                    composition.learner_worker.run_forever(learner_stop)
                )
                control_task = asyncio.create_task(
                    composition.student_chain_worker.run_forever(control_stop)
                )
                try:
                    bootstrap = await self._get_json(port, token, "/v1/bootstrap", "bootstrap")
                    composition.validator.validate(
                        "schemas/game/bootstrap-response.schema.json", bootstrap
                    )
                    self.assertNotIn("wss_url", bootstrap)

                    session_body: dict[str, object] = {
                        "world_id": "world_watering_0001",
                        "learner_id": LEARNER_ID,
                        "agent_profile_id": AGENT_PROFILE_ID,
                        "channel": "GAME",
                        "locale": "en-US",
                        "content": {
                            "unit_id": context.content_ref.unit_id,
                            "version": context.content_ref.version,
                            "content_hash": context.content_ref.content_hash,
                        },
                        "expected_world_revision": 5,
                    }
                    session_raw = _json_bytes(session_body)
                    session_accepted, session_command, session = await self._submit_resource(
                        composition,
                        port,
                        token,
                        target="/v1/agent-sessions",
                        body=session_raw,
                        key="public-role-session-create-0001",
                        suffix="session-create",
                        lose_first_response=True,
                    )
                    self.session_id = cast(str, session["session_id"])
                    self.assertNotEqual(self.session_id, "session_agent_001")
                    self.assertEqual(
                        cast(dict[str, object], session_command["result"])["resource_id"],
                        self.session_id,
                    )
                    composition.validator.validate(
                        "schemas/game/agent-session.schema.json", session
                    )
                    self.assertEqual(session["status"], "ACTIVE")
                    self.assertEqual(session["last_turn_sequence"], 0)

                    draft_id = "draft_role_public_0001"
                    failed_bundle = _source_bundle(_FAILURE_CPP)
                    draft_v1_body = self._draft_body(
                        context,
                        session_id=self.session_id,
                        draft_id=draft_id,
                        source_bundle=failed_bundle,
                        revision=0,
                        draft_sha256=None,
                    )
                    draft_target = (
                        f"/product-experience/v1/sessions/{self.session_id}/skill-drafts/{draft_id}"
                    )
                    draft_v1_raw = _json_bytes(draft_v1_body)
                    draft_v1_status, draft_v1_headers, draft_v1 = await self._put_draft(
                        port,
                        token,
                        target=draft_target,
                        body=draft_v1_raw,
                        key="public-role-draft-v1-0001",
                        suffix="draft-v1",
                    )
                    self.assertEqual(draft_v1_status, 201)
                    self.assertEqual(draft_v1["revision"], 1)
                    self.assertEqual(
                        draft_v1_headers["etag"],
                        f'"draft:1:{draft_v1["draft_sha256"]}"',
                    )

                    build_v1_raw = _json_bytes(
                        self._build_body(failed_bundle, client_draft_revision=1)
                    )
                    build_v1_accepted, _, build_v1 = await self._submit_resource(
                        composition,
                        port,
                        token,
                        target="/v1/skill-builds",
                        body=build_v1_raw,
                        key="public-role-build-v1-0001",
                        suffix="build-v1",
                    )
                    failed_ref = self._skill_ref(build_v1)
                    self.assertEqual(build_v1["status"], "CERTIFIED")
                    activation_v1_raw = _json_bytes(self._activation_body(0))
                    activation_v1_accepted, _, activation_v1 = await self._submit_resource(
                        composition,
                        port,
                        token,
                        target=(f"/v1/skill-versions/{failed_ref.skill_version_id}/activations"),
                        body=activation_v1_raw,
                        key="public-role-activation-v1-0001",
                        suffix="activation-v1",
                    )
                    self.assertEqual(activation_v1["registry_revision"], 1)

                    failures: list[_ObservedTurn] = []
                    for sequence, (turn_id, role) in enumerate(
                        zip(
                            _FAILURE_TURNS,
                            ("teaching_agent", "teaching_agent", "bug_agent"),
                            strict=True,
                        ),
                        start=1,
                    ):
                        observed = await self._submit_and_observe(
                            composition,
                            port,
                            token,
                            failed_ref,
                            turn_id=turn_id,
                            client_turn_sequence=sequence,
                            idempotency_key=f"public-role-turn-failure-{sequence:04d}",
                            expected_role=role,
                            expected_command_status="REJECTED",
                            expected_run_status="REJECTED",
                        )
                        self._assert_failure_turn(observed, sequence, role)
                        failures.append(observed)
                        await self._await_learner_projection(
                            composition,
                            expected=sequence,
                        )

                    success_bundle = _source_bundle(_SUCCESS_CPP)
                    draft_v2_body = self._draft_body(
                        context,
                        session_id=self.session_id,
                        draft_id=draft_id,
                        source_bundle=success_bundle,
                        revision=1,
                        draft_sha256=cast(str, draft_v1["draft_sha256"]),
                    )
                    draft_v2_raw = _json_bytes(draft_v2_body)
                    draft_v2_status, draft_v2_headers, draft_v2 = await self._put_draft(
                        port,
                        token,
                        target=draft_target,
                        body=draft_v2_raw,
                        key="public-role-draft-v2-0001",
                        suffix="draft-v2",
                    )
                    self.assertEqual(draft_v2_status, 200)
                    self.assertEqual(draft_v2["revision"], 2)

                    build_v2_raw = _json_bytes(
                        self._build_body(success_bundle, client_draft_revision=2)
                    )
                    build_v2_accepted, _, build_v2 = await self._submit_resource(
                        composition,
                        port,
                        token,
                        target="/v1/skill-builds",
                        body=build_v2_raw,
                        key="public-role-build-v2-0001",
                        suffix="build-v2",
                    )
                    success_ref = self._skill_ref(build_v2)
                    self.assertNotEqual(
                        failed_ref.skill_version_id,
                        success_ref.skill_version_id,
                    )
                    activation_v2_raw = _json_bytes(self._activation_body(1))
                    activation_v2_accepted, _, activation_v2 = await self._submit_resource(
                        composition,
                        port,
                        token,
                        target=(f"/v1/skill-versions/{success_ref.skill_version_id}/activations"),
                        body=activation_v2_raw,
                        key="public-role-activation-v2-0001",
                        suffix="activation-v2",
                    )
                    self.assertEqual(activation_v2["registry_revision"], 2)

                    success = await self._submit_and_observe(
                        composition,
                        port,
                        token,
                        success_ref,
                        turn_id=_SUCCESS_TURN,
                        client_turn_sequence=4,
                        idempotency_key="public-role-turn-success-0004",
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
                        expected=(*failures, success),
                        suffix="public-chain",
                    )

                    counts = await self._public_counts(composition)
                    self.assertEqual(
                        counts,
                        {
                            "control_jobs": 5,
                            "public_sessions": 1,
                            "draft_revisions": 2,
                            "draft_receipts": 2,
                            "builds": 2,
                            "build_history": 6,
                            "build_receipts": 10,
                            "artifacts": 2,
                            "certifications": 2,
                            "skill_versions": 2,
                            "activations": 2,
                            "registry_revision": 2,
                            "session_bindings": 2,
                            "commands": 9,
                            "turn_jobs": 4,
                            "runs": 4,
                            "evidence": 7,
                            "interactions": 4,
                            "learner_revision": 4,
                            "world_revision": 6,
                        },
                    )
                    immutable_artifacts = self._published_artifact_files(artifact_root)
                    self.assertEqual(len(immutable_artifacts), 2)
                    for candidate in immutable_artifacts:
                        self.assertEqual(candidate.stat().st_mode & 0o222, 0)
                        self.assertEqual(
                            hashlib.sha256(candidate.read_bytes()).hexdigest(),
                            candidate.name,
                        )

                    model_requests, finished = await self._model_trace_graph(composition)
                    expected_roles = {
                        _FAILURE_TURNS[0]: "teaching_agent",
                        _FAILURE_TURNS[1]: "teaching_agent",
                        _FAILURE_TURNS[2]: "bug_agent",
                        _SUCCESS_TURN: "book_agent",
                    }
                    self.assertEqual(
                        set(model_requests),
                        {
                            (turn_id, role)
                            for turn_id, final_role in expected_roles.items()
                            for role in ("xiaohutao", final_role)
                        },
                    )
                    self.assertEqual(set(finished), set(model_requests))
                    self.assertLessEqual(sum(model_requests.values()), generation_budget)
                    self.assertEqual(generation_counter.used, sum(model_requests.values()))
                    for observed in (*failures, success):
                        self.assertEqual(model_requests[(observed.turn_id, "xiaohutao")], 2)
                        final_requests = model_requests[
                            (observed.turn_id, expected_roles[observed.turn_id])
                        ]
                        self.assertGreaterEqual(final_requests, 1)
                        self.assertLessEqual(final_requests, 3)
                    for trace in finished.values():
                        self.assertIs(trace.fields["fallback"], False)
                        self.assertEqual(trace.fields["model_provider"], provider)
                    requested_traces = await self._requested_model_traces(composition)
                    book_requests = [
                        trace
                        for trace in requested_traces
                        if trace.turn_id == _SUCCESS_TURN and trace.role == "book_agent"
                    ]
                    self.assertEqual(
                        len(book_requests), model_requests[(_SUCCESS_TURN, "book_agent")]
                    )
                    for trace in book_requests:
                        self.assertEqual(trace.fields["session_run_count"], 4)
                        self.assertEqual(
                            trace.fields["skill_history_versions"],
                            (failed_ref.skill_version_id, success_ref.skill_version_id),
                        )

                    fingerprint = await self._public_fingerprint(composition)
                    artifact_fingerprint = self._artifact_fingerprint(artifact_root)
                    model_fingerprint = (model_requests, finished)
                    await self._assert_draft_replay(
                        port,
                        token,
                        target=draft_target,
                        body=draft_v1_raw,
                        key="public-role-draft-v1-0001",
                        suffix="replay-draft-v1",
                        expected_status=draft_v1_status,
                        expected_headers=draft_v1_headers,
                        expected=draft_v1,
                    )
                    await self._assert_resource_replay(
                        port,
                        token,
                        target="/v1/agent-sessions",
                        body=session_raw,
                        key="public-role-session-create-0001",
                        suffix="replay-session",
                        expected=session_accepted,
                    )
                    await self._assert_resource_replay(
                        port,
                        token,
                        target="/v1/skill-builds",
                        body=build_v1_raw,
                        key="public-role-build-v1-0001",
                        suffix="replay-build-v1",
                        expected=build_v1_accepted,
                    )
                    await self._assert_resource_replay(
                        port,
                        token,
                        target=(f"/v1/skill-versions/{failed_ref.skill_version_id}/activations"),
                        body=activation_v1_raw,
                        key="public-role-activation-v1-0001",
                        suffix="replay-activation-v1",
                        expected=activation_v1_accepted,
                    )
                    await self._assert_resource_replay(
                        port,
                        token,
                        target="/v1/skill-builds",
                        body=build_v2_raw,
                        key="public-role-build-v2-0001",
                        suffix="replay-build-v2",
                        expected=build_v2_accepted,
                    )
                    await self._assert_resource_replay(
                        port,
                        token,
                        target=(f"/v1/skill-versions/{success_ref.skill_version_id}/activations"),
                        body=activation_v2_raw,
                        key="public-role-activation-v2-0001",
                        suffix="replay-activation-v2",
                        expected=activation_v2_accepted,
                    )
                    await self._assert_draft_replay(
                        port,
                        token,
                        target=draft_target,
                        body=draft_v2_raw,
                        key="public-role-draft-v2-0001",
                        suffix="replay-draft-v2",
                        expected_status=draft_v2_status,
                        expected_headers=draft_v2_headers,
                        expected=draft_v2,
                    )
                    for index, observed in enumerate((*failures, success), start=1):
                        await self._assert_http_replay(
                            port,
                            token,
                            observed,
                            suffix=f"public-turn-replay-{index:04d}",
                        )
                    self.assertEqual(await self._public_fingerprint(composition), fingerprint)
                    self.assertEqual(await self._model_trace_graph(composition), model_fingerprint)
                    self.assertEqual(
                        self._artifact_fingerprint(artifact_root), artifact_fingerprint
                    )

                    await self._stop_worker(agent_stop, agent_task)
                    await self._stop_worker(learner_stop, learner_task)
                    await self._stop_worker(control_stop, control_task)
                    agent_stop = learner_stop = control_stop = None
                    agent_task = learner_task = control_task = None
                    if http_server is None or http_thread is None:
                        self.fail("public-chain HTTP server disappeared")
                    await self._stop_http(http_server, http_thread)
                    http_server = http_thread = None

                    restarted = await create_production_composition(settings)
                    http_server, http_thread, restarted_port = self._start_http(restarted)
                    agent_stop = asyncio.Event()
                    learner_stop = asyncio.Event()
                    control_stop = asyncio.Event()
                    agent_task = asyncio.create_task(restarted.worker.run_forever(agent_stop))
                    learner_task = asyncio.create_task(
                        restarted.learner_worker.run_forever(learner_stop)
                    )
                    control_task = asyncio.create_task(
                        restarted.student_chain_worker.run_forever(control_stop)
                    )
                    restored_session = await self._get_json(
                        restarted_port,
                        token,
                        f"/v1/agent-sessions/{self.session_id}",
                        "restart-session",
                    )
                    restored_draft = await self._get_json(
                        restarted_port,
                        token,
                        draft_target,
                        "restart-draft",
                    )
                    restored_v1 = await self._get_json(
                        restarted_port,
                        token,
                        f"/v1/skill-builds/{build_v1['build_id']}",
                        "restart-build-v1",
                    )
                    restored_v2 = await self._get_json(
                        restarted_port,
                        token,
                        f"/v1/skill-builds/{build_v2['build_id']}",
                        "restart-build-v2",
                    )
                    restarted.validator.validate(
                        "schemas/game/agent-session.schema.json", restored_session
                    )
                    expected_restored_session = dict(session)
                    expected_restored_session["last_turn_sequence"] = 4
                    self.assertEqual(restored_session, expected_restored_session)
                    self.assertEqual(restored_draft, draft_v2)
                    self.assertEqual(restored_v1, build_v1)
                    self.assertEqual(restored_v2, build_v2)
                    await self._assert_resource_replay(
                        restarted_port,
                        token,
                        target="/v1/agent-sessions",
                        body=session_raw,
                        key="public-role-session-create-0001",
                        suffix="restart-replay-session",
                        expected=session_accepted,
                    )
                    await self._assert_draft_replay(
                        restarted_port,
                        token,
                        target=draft_target,
                        body=draft_v1_raw,
                        key="public-role-draft-v1-0001",
                        suffix="restart-replay-draft-v1",
                        expected_status=draft_v1_status,
                        expected_headers=draft_v1_headers,
                        expected=draft_v1,
                    )
                    await self._assert_resource_replay(
                        restarted_port,
                        token,
                        target="/v1/skill-builds",
                        body=build_v1_raw,
                        key="public-role-build-v1-0001",
                        suffix="restart-replay-build-v1",
                        expected=build_v1_accepted,
                    )
                    await self._assert_resource_replay(
                        restarted_port,
                        token,
                        target=(f"/v1/skill-versions/{failed_ref.skill_version_id}/activations"),
                        body=activation_v1_raw,
                        key="public-role-activation-v1-0001",
                        suffix="restart-replay-activation-v1",
                        expected=activation_v1_accepted,
                    )
                    await self._assert_draft_replay(
                        restarted_port,
                        token,
                        target=draft_target,
                        body=draft_v2_raw,
                        key="public-role-draft-v2-0001",
                        suffix="restart-replay-draft-v2",
                        expected_status=draft_v2_status,
                        expected_headers=draft_v2_headers,
                        expected=draft_v2,
                    )
                    await self._assert_resource_replay(
                        restarted_port,
                        token,
                        target="/v1/skill-builds",
                        body=build_v2_raw,
                        key="public-role-build-v2-0001",
                        suffix="restart-replay-build-v2",
                        expected=build_v2_accepted,
                    )
                    await self._assert_resource_replay(
                        restarted_port,
                        token,
                        target=(f"/v1/skill-versions/{success_ref.skill_version_id}/activations"),
                        body=activation_v2_raw,
                        key="public-role-activation-v2-0001",
                        suffix="restart-replay-activation-v2",
                        expected=activation_v2_accepted,
                    )
                    for index, observed in enumerate((*failures, success), start=1):
                        await self._assert_http_replay(
                            restarted_port,
                            token,
                            observed,
                            suffix=f"restart-turn-replay-{index:04d}",
                        )
                    restored_product = await self._read_product_pages(
                        restarted,
                        restarted_port,
                        token,
                        expected=(*failures, success),
                        suffix="public-restart",
                    )
                    self.assertEqual(restored_product, product)
                    self.assertEqual(await self._public_fingerprint(restarted), fingerprint)
                    self.assertEqual(await self._model_trace_graph(restarted), model_fingerprint)
                    self.assertEqual(
                        self._artifact_fingerprint(artifact_root), artifact_fingerprint
                    )

                    print(
                        "YAYA_PUBLIC_CHAIN_LIVE_EVIDENCE="
                        + json.dumps(
                            {
                                "counts": counts,
                                "model_requests_by_role": dict(
                                    sorted(
                                        Counter(
                                            role
                                            for (_, role), request_count in model_requests.items()
                                            for _ in range(request_count)
                                        ).items()
                                    )
                                ),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                finally:
                    for stop, task in (
                        (agent_stop, agent_task),
                        (learner_stop, learner_task),
                        (control_stop, control_task),
                    ):
                        if stop is not None and task is not None:
                            await self._stop_worker(stop, task)
                    if http_server is not None and http_thread is not None:
                        await self._stop_http(http_server, http_thread)
                    for candidate in artifact_root.rglob("*"):
                        if candidate.is_file() and not candidate.is_symlink():
                            candidate.chmod(stat.S_IWRITE | stat.S_IREAD)

    @staticmethod
    def _draft_body(
        context: Any,
        *,
        session_id: str,
        draft_id: str,
        source_bundle: dict[str, object],
        revision: int,
        draft_sha256: str | None,
    ) -> dict[str, object]:
        return {
            "session_id": session_id,
            "draft_id": draft_id,
            "skill_id": _LIVE_SKILL_ID,
            "content_ref": {
                "unit_id": context.content_ref.unit_id,
                "version": context.content_ref.version,
                "content_hash": context.content_ref.content_hash,
            },
            "base_revision": revision,
            "base_draft_sha256": draft_sha256,
            "display_name": "Public role watering Skill",
            "source_bundle": source_bundle,
            "client_saved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }

    @staticmethod
    def _build_body(
        source_bundle: dict[str, object],
        *,
        client_draft_revision: int,
    ) -> dict[str, object]:
        return {
            "skill_id": _LIVE_SKILL_ID,
            "display_name": "Public role watering Skill",
            "client_draft_revision": client_draft_revision,
            "source_bundle": source_bundle,
            "compiler_profile": CPP20_SAFE_V1_PROFILE,
            "test_suite_version": TEST_SUITE_VERSION,
            "requested_capabilities": ["WATER", "WORLD_READ"],
        }

    @staticmethod
    def _activation_body(expected_revision: int) -> dict[str, object]:
        return {
            "expected_registry_revision": expected_revision,
            "activation_scope": {
                "world_id": "world_watering_0001",
                "agent_profile_id": AGENT_PROFILE_ID,
            },
            "reason": "Activate the exact successful public Build certification.",
        }

    @staticmethod
    def _skill_ref(build: dict[str, object]) -> SkillRef:
        artifact = cast(dict[str, object], build["artifact"])
        certification = cast(dict[str, object], build["certification"])
        return SkillRef(
            skill_id=cast(str, build["skill_id"]),
            skill_version_id=cast(str, build["skill_version_id"]),
            artifact_sha256=cast(str, artifact["artifact_sha256"]),
            certification_id=cast(str, certification["certification_id"]),
        )

    async def _submit_resource(
        self,
        composition: Any,
        port: int,
        token: str,
        *,
        target: str,
        body: bytes,
        key: str,
        suffix: str,
        lose_first_response: bool = False,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        request_headers = self._headers(
            token,
            f"{suffix}-lost" if lose_first_response else suffix,
            idempotency_key=key,
        )
        expected_replay = "false"
        if lose_first_response:
            lost_status = await asyncio.to_thread(
                self._discard_response_body,
                port,
                target,
                request_headers,
                body,
            )
            self.assertEqual(lost_status, 202)
            request_headers = self._headers(
                token,
                f"{suffix}-reconcile",
                idempotency_key=key,
            )
            expected_replay = "true"
        status, headers, accepted = await asyncio.to_thread(
            self._http,
            port,
            "POST",
            target,
            request_headers,
            body,
        )
        self.assertEqual(status, 202, accepted)
        self.assertEqual(headers["idempotency-replayed"], expected_replay)
        composition.validator.validate("schemas/game/accepted-game-job.schema.json", accepted)
        command = await self._await_terminal(
            composition,
            port,
            token,
            cast(str, accepted["command_id"]),
        )
        self.assertEqual(command["status"], "APPLIED", command)
        result = cast(dict[str, object], command["result"])
        resource = await self._get_json(
            port,
            token,
            cast(str, result["resource_url"]),
            f"{suffix}-resource",
        )
        return accepted, command, resource

    @staticmethod
    def _discard_response_body(
        port: int,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> int:
        """Drop a committed 202 body, then let idempotency recover it."""

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
        try:
            lost_headers = dict(headers)
            lost_headers["Connection"] = "close"
            connection.request("POST", target, body=body, headers=lost_headers)
            response = connection.getresponse()
            return response.status
        finally:
            connection.close()

    @staticmethod
    async def _requested_model_traces(composition: Any) -> tuple[AgentTraceEvent, ...]:
        connection = await composition.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                "SELECT trace_json FROM yaya_agent_traces ORDER BY trace_record_id"
            )
            traces = tuple(
                decode_as(row["trace_json"], AgentTraceEvent) for row in await cursor.fetchall()
            )
        finally:
            await connection.close()
        return tuple(trace for trace in traces if trace.name == "agent.model.requested")

    async def _put_draft(
        self,
        port: int,
        token: str,
        *,
        target: str,
        body: bytes,
        key: str,
        suffix: str,
    ) -> tuple[int, dict[str, str], dict[str, object]]:
        status, headers, payload = await asyncio.to_thread(
            self._http,
            port,
            "PUT",
            target,
            self._headers(token, suffix, idempotency_key=key),
            body,
        )
        self.assertIn(status, (200, 201), payload)
        return status, headers, payload

    async def _assert_draft_replay(
        self,
        port: int,
        token: str,
        *,
        target: str,
        body: bytes,
        key: str,
        suffix: str,
        expected_status: int,
        expected_headers: Mapping[str, str],
        expected: dict[str, object],
    ) -> None:
        status, headers, payload = await self._put_draft(
            port,
            token,
            target=target,
            body=body,
            key=key,
            suffix=suffix,
        )
        self.assertEqual(status, expected_status, payload)
        self.assertEqual(headers["idempotency-replayed"], "true")
        for name in ("location", "etag", "x-draft-revision"):
            self.assertEqual(headers[name], expected_headers[name])
        self.assertEqual(payload, expected)

    async def _assert_resource_replay(
        self,
        port: int,
        token: str,
        *,
        target: str,
        body: bytes,
        key: str,
        suffix: str,
        expected: dict[str, object],
    ) -> None:
        status, headers, payload = await asyncio.to_thread(
            self._http,
            port,
            "POST",
            target,
            self._headers(token, suffix, idempotency_key=key),
            body,
        )
        self.assertEqual(status, 202, payload)
        self.assertEqual(headers["idempotency-replayed"], "true")
        self.assertEqual(payload, expected)

    @staticmethod
    async def _public_counts(composition: Any) -> dict[str, int]:
        connection = await composition.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_control_jobs)::int AS control_jobs,
                  (SELECT count(*) FROM yaya_public_agent_sessions)::int AS public_sessions,
                  (SELECT count(*) FROM yaya_skill_draft_revisions)::int AS draft_revisions,
                  (SELECT count(*) FROM yaya_product_write_receipts)::int AS draft_receipts,
                  (SELECT count(*) FROM yaya_skill_builds)::int AS builds,
                  (SELECT count(*) FROM yaya_skill_build_history)::int AS build_history,
                  (SELECT count(*) FROM yaya_build_step_receipts)::int AS build_receipts,
                  (SELECT count(*) FROM yaya_artifacts)::int AS artifacts,
                  (SELECT count(*) FROM yaya_skill_certifications)::int AS certifications,
                  (SELECT count(*) FROM yaya_skills)::int AS skill_versions,
                  (SELECT count(*) FROM yaya_skill_activations)::int AS activations,
                  (SELECT revision FROM yaya_registry_heads)::int AS registry_revision,
                  (SELECT count(*) FROM yaya_session_skill_versions)::int AS session_bindings,
                  (SELECT count(*) FROM yaya_commands)::int AS commands,
                  (SELECT count(*) FROM yaya_command_jobs)::int AS turn_jobs,
                  (SELECT count(*) FROM yaya_runs)::int AS runs,
                  (SELECT count(*) FROM yaya_evidence)::int AS evidence,
                  (SELECT count(*) FROM yaya_agent_interactions)::int AS interactions,
                  (SELECT revision FROM yaya_learner_models)::int AS learner_revision,
                  (SELECT revision FROM yaya_worlds)::int AS world_revision
                """
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise AssertionError("public count query returned no row")
        return {key: cast(int, value) for key, value in row.items()}

    @staticmethod
    async def _public_fingerprint(composition: Any) -> A8StateFingerprint:
        fingerprint = await a8_state_fingerprint(composition.database)
        missing = missing_a8_business_tables(fingerprint)
        if missing:
            raise AssertionError(f"A8 business tables missing from live database: {missing!r}")
        return fingerprint

    @staticmethod
    def _published_artifact_files(root: Path) -> list[Path]:
        infrastructure_roots = {".build-workspaces", ".sandbox-results"}
        return [
            candidate
            for candidate in root.rglob("*")
            if candidate.is_file()
            and candidate.relative_to(root).parts[0] not in infrastructure_roots
        ]

    @staticmethod
    def _artifact_fingerprint(root: Path) -> dict[str, tuple[int, int, str]]:
        workspace_root = root / ".build-workspaces"
        if workspace_root.exists() and any(workspace_root.rglob("*")):
            raise AssertionError("terminal live Builds left deterministic workspaces behind")
        result: dict[str, tuple[int, int, str]] = {}
        for candidate in sorted(root.rglob("*")):
            if candidate.is_symlink():
                raise AssertionError(f"artifact root contains a symlink: {candidate}")
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(root).as_posix()
            payload = candidate.read_bytes()
            result[relative] = (
                len(payload),
                candidate.stat().st_mode & 0o777,
                hashlib.sha256(payload).hexdigest(),
            )
        return result


if __name__ == "__main__":
    import unittest

    unittest.main()
