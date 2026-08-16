from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import hashlib
import http.client
import json
import stat
import tempfile
import threading
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import cast

from yaya_agent_backend.composition import (
    ProductionComposition,
    create_production_composition,
    production_versions,
)
from yaya_agent_backend.config import ProductionSettings
from yaya_agent_backend.http_api import AgentHttpApi, serve_http
from yaya_agent_backend.http_router import ProductionHttpApi
from yaya_agent_backend.product_http_api import ProductHttpApi
from yaya_agent_build import (
    CPP20_SAFE_V1_PROFILE,
    canonical_source_bundle_sha256,
)
from yaya_agent_contracts import canonical_json_sha256

from tests.agent_runtime_fixtures import WORLD_ID, make_operation
from tests.postgres_test_support import postgres_test_server
from tests.test_agent_backend_skill_build_executor import (
    AGENT_PROFILE_ID,
    LEARNER_ID,
    PINNED_GCC_IMAGE,
    TEST_SUITE_VERSION,
    _seed_only_build_authority,
)

CONTRACTS_ROOT = Path(__file__).resolve().parents[1] / "contracts"
JWT_SECRET = "public-chain-http-e2e-secret-000000000000000000"
JWT_ISSUER = "yaya-public-chain-http-e2e"
JWT_AUDIENCE = "yaya-game-api"
SKILL_ID = "skill_public_chain_http_e2e_0001"
DRAFT_ID = "draft_public_chain_http_e2e_0001"


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
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


class PublicChainHttpE2ETests(unittest.IsolatedAsyncioTestCase):
    """Real HTTP -> PostgreSQL worker -> pinned Docker public certification chain."""

    async def test_bootstrap_session_draft_build_certification_activation(self) -> None:
        source = """#include <iostream>
#include <string>

int main(int argc, char* argv[]) {
    if (argc != 2) {
        return 2;
    }
    std::string input;
    if (!std::getline(std::cin, input)) {
        return 3;
    }
    std::cout << argv[1] << ':' << input << '\\n';
    return 0;
}
        """
        bundle = _source_bundle(source)
        expected_source_sha256 = canonical_source_bundle_sha256(bundle)

        with tempfile.TemporaryDirectory(prefix="yaya-public-chain-http-e2e-") as raw_root:
            artifact_root = Path(raw_root).resolve() / "artifacts"
            artifact_root.mkdir()
            try:
                with postgres_test_server() as postgres:
                    settings = self._settings(postgres.dsn, artifact_root)
                    composition = await create_production_composition(settings)
                    authority_context = make_operation(
                        command_id="cmd_public_chain_http_authority_0001"
                    )
                    await _seed_only_build_authority(
                        composition.database,
                        context_override=authority_context,
                        versions_override=production_versions(settings),
                    )

                    http_server, http_thread, port = self._start_http(composition)
                    token = composition.authenticator.issue_for_test(
                        authority_context.actor,
                        now=datetime.now(UTC),
                    )
                    worker_stop = asyncio.Event()
                    worker_task = asyncio.create_task(
                        composition.student_chain_worker.run_forever(worker_stop),
                        name="public-chain-continuous-control-worker",
                    )
                    # Ensure the continuously polling worker predates every HTTP
                    # acceptance. This preserves the regression shape for a
                    # claim timestamp captured immediately before a new job commits.
                    await asyncio.sleep(0.1)
                    try:
                        bootstrap = await self._get_json(
                            port,
                            token,
                            "/v1/bootstrap",
                            "bootstrap",
                        )
                        composition.validator.validate(
                            "schemas/game/bootstrap-response.schema.json",
                            bootstrap,
                        )
                        self.assertNotIn("wss_url", bootstrap)

                        content = {
                            "unit_id": authority_context.content_ref.unit_id,
                            "version": authority_context.content_ref.version,
                            "content_hash": authority_context.content_ref.content_hash,
                        }
                        session_body: dict[str, object] = {
                            "world_id": WORLD_ID,
                            "learner_id": LEARNER_ID,
                            "agent_profile_id": AGENT_PROFILE_ID,
                            "channel": "GAME",
                            "locale": "en-US",
                            "content": content,
                            "expected_world_revision": 5,
                        }
                        _, session = await self._submit_and_read_resource(
                            composition,
                            port,
                            token,
                            worker_task,
                            target="/v1/agent-sessions",
                            body=_json_bytes(session_body),
                            idempotency_key="public-chain-session-create-0001",
                            suffix="session-create",
                            lose_first_response=True,
                        )
                        composition.validator.validate(
                            "schemas/game/agent-session.schema.json",
                            session,
                        )
                        session_id = cast(str, session["session_id"])
                        self.assertEqual(session["status"], "ACTIVE")
                        self.assertEqual(session["last_turn_sequence"], 0)
                        self.assertEqual(session["world_id"], WORLD_ID)

                        draft_target = (
                            f"/product-experience/v1/sessions/{session_id}/skill-drafts/{DRAFT_ID}"
                        )
                        draft_body: dict[str, object] = {
                            "session_id": session_id,
                            "draft_id": DRAFT_ID,
                            "skill_id": SKILL_ID,
                            "content_ref": content,
                            "base_revision": 0,
                            "base_draft_sha256": None,
                            "display_name": "Public HTTP Docker Skill",
                            "source_bundle": bundle,
                            "client_saved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        }
                        draft_status, draft_headers, draft = await self._request_json(
                            port,
                            "PUT",
                            draft_target,
                            self._headers(
                                token,
                                "draft-v1",
                                idempotency_key="public-chain-draft-v1-0001",
                            ),
                            _json_bytes(draft_body),
                        )
                        self.assertEqual(draft_status, 201, draft)
                        composition.validator.validate(
                            "schemas/product-experience/skill-draft.schema.json",
                            draft,
                        )
                        self.assertEqual(draft["revision"], 1)
                        self.assertEqual(draft["source_bundle"], bundle)
                        self.assertEqual(
                            draft_headers.get("etag"),
                            f'"draft:1:{draft["draft_sha256"]}"',
                        )

                        build_body: dict[str, object] = {
                            "skill_id": SKILL_ID,
                            "display_name": "Public HTTP Docker Skill",
                            "client_draft_revision": 1,
                            "source_bundle": bundle,
                            "compiler_profile": CPP20_SAFE_V1_PROFILE,
                            "test_suite_version": TEST_SUITE_VERSION,
                            "requested_capabilities": ["WATER", "WORLD_READ"],
                        }
                        _, build = await self._submit_and_read_resource(
                            composition,
                            port,
                            token,
                            worker_task,
                            target="/v1/skill-builds",
                            body=_json_bytes(build_body),
                            idempotency_key="public-chain-build-v1-0001",
                            suffix="build-v1",
                        )
                        composition.validator.validate(
                            "schemas/game/skill-build.schema.json",
                            build,
                        )
                        self.assertEqual(build["status"], "CERTIFIED", build)
                        self.assertIs(build["terminal"], True)
                        self.assertIsNone(build["failure"])
                        self.assertEqual(
                            [
                                phase["status"]
                                for phase in cast(list[dict[str, object]], build["phases"])
                            ],
                            ["PASSED"] * 5,
                        )
                        artifact = cast(dict[str, object], build["artifact"])
                        certification = cast(dict[str, object], build["certification"])
                        artifact_sha256 = cast(str, artifact["artifact_sha256"])
                        skill_version_id = cast(str, build["skill_version_id"])
                        certification_id = cast(str, certification["certification_id"])
                        self.assertEqual(artifact["source_sha256"], expected_source_sha256)
                        self.assertEqual(artifact["compiler_profile"], CPP20_SAFE_V1_PROFILE)
                        self.assertEqual(artifact["test_suite_version"], TEST_SUITE_VERSION)
                        artifact_path = artifact_root / artifact_sha256[:2] / artifact_sha256
                        self.assertTrue(artifact_path.is_file())
                        self.assertFalse(artifact_path.is_symlink())
                        self.assertEqual(
                            hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                            artifact_sha256,
                        )
                        self.assertEqual(artifact_path.stat().st_mode & 0o222, 0)

                        activation_body: dict[str, object] = {
                            "expected_registry_revision": 0,
                            "activation_scope": {
                                "world_id": WORLD_ID,
                                "agent_profile_id": AGENT_PROFILE_ID,
                            },
                            "reason": "Activate the exact HTTP-certified pinned Docker artifact.",
                        }
                        _, activation = await self._submit_and_read_resource(
                            composition,
                            port,
                            token,
                            worker_task,
                            target=f"/v1/skill-versions/{skill_version_id}/activations",
                            body=_json_bytes(activation_body),
                            idempotency_key="public-chain-activation-v1-0001",
                            suffix="activation-v1",
                        )
                        composition.validator.validate(
                            "schemas/game/skill-activation.schema.json",
                            activation,
                        )
                        self.assertEqual(activation["skill_id"], SKILL_ID)
                        self.assertEqual(activation["skill_version_id"], skill_version_id)
                        self.assertEqual(activation["certification_id"], certification_id)
                        self.assertEqual(activation["artifact_sha256"], artifact_sha256)
                        self.assertEqual(activation["previous_registry_revision"], 0)
                        self.assertEqual(activation["registry_revision"], 1)

                        await self._assert_durable_closure(
                            composition,
                            session_id=session_id,
                            build_id=cast(str, build["build_id"]),
                            skill_version_id=skill_version_id,
                            certification_id=certification_id,
                            artifact_sha256=artifact_sha256,
                            activation_id=cast(str, activation["activation_id"]),
                            expected_source_sha256=expected_source_sha256,
                        )

                        # The published World checkpoint is an imported revision-5
                        # snapshot with an empty local event stream.  Prove the
                        # first exact-version Turn crosses the real HTTP acceptance
                        # boundary with client cursor zero.  The Agent worker stays
                        # stopped so this provider-independent test never invokes a
                        # model or substitutes fallback output for the live gate.
                        turn_body: dict[str, object] = {
                            "turn_id": "turn_public_chain_acceptance_0001",
                            "expected_world_revision": 5,
                            "input": {
                                "type": "MESSAGE",
                                "text": "Execute the exact activated Skill.",
                                "locale": "en-US",
                            },
                            "skill_bindings": [
                                {
                                    "skill_id": SKILL_ID,
                                    "skill_version_id": skill_version_id,
                                    "artifact_sha256": artifact_sha256,
                                    "certification_id": certification_id,
                                }
                            ],
                            "client_state": {
                                "last_event_sequence": 0,
                                "client_turn_sequence": 1,
                            },
                        }
                        turn_status, turn_headers, turn_accepted = await self._request_json(
                            port,
                            "POST",
                            f"/v1/agent-sessions/{session_id}/turns",
                            self._headers(
                                token,
                                "turn-acceptance",
                                idempotency_key="public-chain-turn-acceptance-0001",
                            ),
                            _json_bytes(turn_body),
                        )
                        self.assertEqual(turn_status, 202, turn_accepted)
                        self.assertEqual(turn_headers.get("idempotency-replayed"), "false")
                        composition.validator.validate(
                            "schemas/game/accepted-game-job.schema.json",
                            turn_accepted,
                        )
                        accepted_command = await self._get_json(
                            port,
                            token,
                            f"/v1/commands/{turn_accepted['command_id']}",
                            "turn-acceptance-command",
                        )
                        self.assertEqual(accepted_command["status"], "ACCEPTED")
                        self.assertIs(accepted_command["terminal"], False)

                        workspace_root = artifact_root / ".build-workspaces"
                        self.assertEqual(
                            [
                                candidate
                                for candidate in workspace_root.rglob("*")
                                if candidate.is_file()
                            ],
                            [],
                        )
                    finally:
                        await self._stop_worker(worker_stop, worker_task)
                        await self._stop_http(http_server, http_thread)
            finally:
                for candidate in artifact_root.rglob("*"):
                    if candidate.is_file() and not candidate.is_symlink():
                        candidate.chmod(stat.S_IWRITE | stat.S_IREAD)

    @staticmethod
    def _settings(dsn: str, artifact_root: Path) -> ProductionSettings:
        return ProductionSettings(
            database_dsn=dsn,
            artifact_root=artifact_root,
            contracts_root=CONTRACTS_ROOT,
            auth_hmac_secret=JWT_SECRET,
            auth_issuer=JWT_ISSUER,
            auth_audience=JWT_AUDIENCE,
            llm_mode="fallback",
            llm_endpoint=None,
            llm_api_key=None,
            llm_model="explicit-fallback",
            llm_provider="explicit-fallback",
            llm_response_format="json_object",
            llm_thinking_mode=None,
            llm_max_response_bytes=2_097_152,
            allow_insecure_llm_localhost=False,
            http_host="127.0.0.1",
            http_port=8080,
            worker_id="worker_public_chain_http_e2e_0001",
            worker_lease_seconds=180,
            worker_poll_ms=25,
            learner_worker_id="learner_public_chain_http_e2e_0001",
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

    @staticmethod
    def _start_http(
        composition: ProductionComposition,
    ) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
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
        servers: list[ThreadingHTTPServer] = []

        def capture(server: ThreadingHTTPServer) -> None:
            servers.append(server)
            captured.set()

        thread = threading.Thread(
            target=serve_http,
            args=(api, "127.0.0.1", 0),
            kwargs={"ready": ready, "server_created": capture},
            daemon=True,
            name="yaya-public-chain-http-e2e",
        )
        thread.start()
        if not captured.wait(10) or not ready.wait(10) or not servers:
            raise RuntimeError("public-chain localhost HTTP server did not become ready")
        server = servers[0]
        return server, thread, int(server.server_address[1])

    @staticmethod
    async def _stop_http(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
        await asyncio.to_thread(server.shutdown)
        thread.join(timeout=10)
        if thread.is_alive():
            raise AssertionError("public-chain localhost HTTP server did not stop")

    @staticmethod
    async def _stop_worker(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
        stop.set()
        if task.done():
            await asyncio.gather(task, return_exceptions=True)
            return
        try:
            await asyncio.wait_for(task, timeout=30)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise AssertionError("public-chain durable worker did not stop") from None

    @staticmethod
    def _headers(
        token: str,
        suffix: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        normalized = hashlib.sha256(suffix.encode("utf-8")).hexdigest()[:16]
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Schema-Version": "1.0.0",
            "X-Request-Id": f"req_public_chain_{normalized}",
            "X-Trace-Id": f"trace_public_chain_{normalized}",
            "X-Correlation-Id": f"corr_public_chain_{normalized}",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _http(
        port: int,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> tuple[int, dict[str, str], dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
        try:
            connection.request(method, target, body=body, headers=dict(headers))
            response = connection.getresponse()
            raw_payload = response.read()
            decoded = cast(object, json.loads(raw_payload.decode("utf-8")))
            if not isinstance(decoded, dict):
                raise AssertionError(
                    f"{method} {target} returned a non-object JSON payload: {decoded!r}"
                )
            return (
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                cast(dict[str, object], decoded),
            )
        finally:
            connection.close()

    @staticmethod
    def _discard_response_body(
        port: int,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> int:
        """Observe server acceptance but deliberately lose its committed body."""

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
        try:
            lost_headers = dict(headers)
            lost_headers["Connection"] = "close"
            connection.request(method, target, body=body, headers=lost_headers)
            response = connection.getresponse()
            return response.status
        finally:
            connection.close()

    async def _request_json(
        self,
        port: int,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], dict[str, object]]:
        return await asyncio.to_thread(
            self._http,
            port,
            method,
            target,
            headers,
            body,
        )

    async def _get_json(
        self,
        port: int,
        token: str,
        target: str,
        suffix: str,
    ) -> dict[str, object]:
        status, _, payload = await self._request_json(
            port,
            "GET",
            target,
            self._headers(token, suffix),
        )
        self.assertEqual(status, 200, (target, payload))
        return payload

    async def _submit_and_read_resource(
        self,
        composition: ProductionComposition,
        port: int,
        token: str,
        worker_task: asyncio.Task[None],
        *,
        target: str,
        body: bytes,
        idempotency_key: str,
        suffix: str,
        lose_first_response: bool = False,
    ) -> tuple[dict[str, object], dict[str, object]]:
        first_headers = self._headers(
            token,
            f"{suffix}-lost" if lose_first_response else suffix,
            idempotency_key=idempotency_key,
        )
        expected_replay = "false"
        if lose_first_response:
            lost_status = await asyncio.to_thread(
                self._discard_response_body,
                port,
                "POST",
                target,
                first_headers,
                body,
            )
            self.assertEqual(lost_status, 202)
            first_headers = self._headers(
                token,
                f"{suffix}-reconcile",
                idempotency_key=idempotency_key,
            )
            expected_replay = "true"
        status, response_headers, accepted = await self._request_json(
            port,
            "POST",
            target,
            first_headers,
            body,
        )
        self.assertEqual(status, 202, accepted)
        self.assertEqual(response_headers.get("idempotency-replayed"), expected_replay)
        composition.validator.validate(
            "schemas/game/accepted-game-job.schema.json",
            accepted,
        )
        command_id = cast(str, accepted["command_id"])
        command = await self._await_terminal_command(
            composition,
            port,
            token,
            worker_task,
            command_id,
        )
        self.assertEqual(command["status"], "APPLIED", command)
        result = command.get("result")
        if not isinstance(result, dict):
            self.fail(f"terminal command has no object result: {command!r}")
        result_object = cast(dict[str, object], result)
        resource_url = result_object.get("resource_url")
        if not isinstance(resource_url, str):
            self.fail(f"terminal command has no resource URL: {command!r}")
        resource = await self._get_json(
            port,
            token,
            resource_url,
            f"{suffix}-resource",
        )
        return accepted, resource

    async def _await_terminal_command(
        self,
        composition: ProductionComposition,
        port: int,
        token: str,
        worker_task: asyncio.Task[None],
        command_id: str,
    ) -> dict[str, object]:
        deadline = asyncio.get_running_loop().time() + 180
        attempt = 0
        while asyncio.get_running_loop().time() < deadline:
            if worker_task.done():
                error = worker_task.exception()
                self.fail(f"durable control worker stopped before command completion: {error!r}")
            attempt += 1
            command = await self._get_json(
                port,
                token,
                f"/v1/commands/{command_id}",
                f"command-{command_id[-8:]}-{attempt}",
            )
            composition.validator.validate("schemas/game/command.schema.json", command)
            if command.get("terminal") is True:
                return command
            await asyncio.sleep(0.2)
        diagnostics = await self._control_diagnostics(composition)
        self.fail(f"command {command_id} did not terminate: {diagnostics!r}")

    @staticmethod
    async def _control_diagnostics(
        composition: ProductionComposition,
    ) -> list[dict[str, object]]:
        connection = await composition.database.connect(autocommit=True)
        try:
            cursor = await connection.execute(
                """
                SELECT operation,phase,state,attempt,last_error_code
                FROM yaya_control_jobs
                ORDER BY created_at,job_id
                """
            )
            return [dict(row) for row in await cursor.fetchall()]
        finally:
            await connection.close()

    async def _assert_durable_closure(
        self,
        composition: ProductionComposition,
        *,
        session_id: str,
        build_id: str,
        skill_version_id: str,
        certification_id: str,
        artifact_sha256: str,
        activation_id: str,
        expected_source_sha256: str,
    ) -> None:
        connection = await composition.database.connect(autocommit=True)
        try:
            count_cursor = await connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM yaya_public_agent_sessions)::int AS sessions,
                  (SELECT count(*) FROM yaya_skill_draft_revisions)::int AS drafts,
                  (SELECT count(*) FROM yaya_product_write_receipts)::int AS draft_receipts,
                  (SELECT count(*) FROM yaya_skill_builds)::int AS builds,
                  (SELECT count(*) FROM yaya_build_step_receipts)::int AS build_receipts,
                  (SELECT count(*) FROM yaya_artifacts)::int AS artifacts,
                  (SELECT count(*) FROM yaya_skill_certifications)::int AS certifications,
                  (SELECT count(*) FROM yaya_skills)::int AS skill_versions,
                  (SELECT count(*) FROM yaya_registry_entries)::int AS registry_entries,
                  (SELECT count(*) FROM yaya_skill_activations)::int AS activations,
                  (SELECT count(*) FROM yaya_session_skill_versions)::int AS bindings,
                  (SELECT count(*) FROM yaya_control_jobs)::int AS control_jobs,
                  (SELECT count(*) FROM yaya_commands)::int AS commands
                """
            )
            counts = await count_cursor.fetchone()
            closure_cursor = await connection.execute(
                """
                SELECT b.status,b.terminal,b.source_bundle_sha256,b.resource_sha256,
                       b.resource_json,a.build_id AS artifact_build_id,a.source_sha256,
                       c.certification_id,c.skill_version_id,c.artifact_sha256,
                       c.certification_sha256,c.record_json AS certification_json,
                       ac.activation_id,ac.registry_revision,ac.activation_sha256,
                       ac.record_json AS activation_json,h.revision AS registry_revision_head,
                       sess.session_id,
                       array_agg(DISTINCT j.state ORDER BY j.state) AS job_states,
                       array_agg(DISTINCT cmd.status ORDER BY cmd.status) AS command_statuses
                FROM yaya_skill_builds b
                JOIN yaya_artifacts a
                  ON a.tenant_id=b.tenant_id AND a.build_id=b.build_id
                JOIN yaya_skill_certifications c
                  ON c.tenant_id=b.tenant_id AND c.build_id=b.build_id
                 AND c.artifact_sha256=a.artifact_sha256
                JOIN yaya_skill_activations ac
                  ON ac.tenant_id=c.tenant_id
                 AND ac.certification_id=c.certification_id
                 AND ac.skill_version_id=c.skill_version_id
                 AND ac.artifact_sha256=c.artifact_sha256
                JOIN yaya_registry_heads h
                  ON h.tenant_id=ac.tenant_id AND h.actor_id=ac.actor_id
                 AND h.content_hash=ac.content_hash AND h.world_id=ac.world_id
                 AND h.agent_profile_id=ac.agent_profile_id AND h.skill_id=ac.skill_id
                JOIN yaya_public_agent_sessions sess
                  ON sess.tenant_id=ac.tenant_id AND sess.actor_id=ac.actor_id
                 AND sess.content_hash=ac.content_hash AND sess.world_id=ac.world_id
                 AND sess.agent_profile_id=ac.agent_profile_id
                CROSS JOIN yaya_control_jobs j
                CROSS JOIN yaya_commands cmd
                WHERE b.build_id=%s AND c.skill_version_id=%s
                  AND c.certification_id=%s AND c.artifact_sha256=%s
                  AND ac.activation_id=%s AND sess.session_id=%s
                GROUP BY b.status,b.terminal,b.source_bundle_sha256,b.resource_sha256,
                         b.resource_json,a.build_id,a.source_sha256,c.certification_id,
                         c.skill_version_id,c.artifact_sha256,c.certification_sha256,
                         c.record_json,ac.activation_id,ac.registry_revision,
                         ac.activation_sha256,ac.record_json,h.revision,sess.session_id
                """,
                (
                    build_id,
                    skill_version_id,
                    certification_id,
                    artifact_sha256,
                    activation_id,
                    session_id,
                ),
            )
            closure = await closure_cursor.fetchone()
        finally:
            await connection.close()

        self.assertIsNotNone(counts)
        assert counts is not None
        self.assertEqual(
            dict(counts),
            {
                "sessions": 1,
                "drafts": 1,
                "draft_receipts": 1,
                "builds": 1,
                "build_receipts": 5,
                "artifacts": 1,
                "certifications": 1,
                "skill_versions": 1,
                "registry_entries": 1,
                "activations": 1,
                "bindings": 0,
                "control_jobs": 3,
                "commands": 3,
            },
        )
        self.assertIsNotNone(closure)
        assert closure is not None
        self.assertEqual(closure["status"], "CERTIFIED")
        self.assertIs(closure["terminal"], True)
        self.assertEqual(closure["source_bundle_sha256"], expected_source_sha256)
        self.assertEqual(closure["source_sha256"], expected_source_sha256)
        self.assertEqual(closure["artifact_build_id"], build_id)
        self.assertEqual(closure["certification_id"], certification_id)
        self.assertEqual(closure["skill_version_id"], skill_version_id)
        self.assertEqual(closure["artifact_sha256"], artifact_sha256)
        self.assertEqual(closure["activation_id"], activation_id)
        self.assertEqual(closure["registry_revision"], 1)
        self.assertEqual(closure["registry_revision_head"], 1)
        self.assertEqual(closure["session_id"], session_id)
        self.assertEqual(closure["job_states"], ["SUCCEEDED"])
        self.assertEqual(closure["command_statuses"], ["APPLIED"])
        self.assertEqual(
            canonical_json_sha256(cast(dict[str, object], closure["resource_json"])),
            closure["resource_sha256"],
        )
        certification_record = cast(dict[str, object], closure["certification_json"])
        self.assertEqual(
            canonical_json_sha256(certification_record), closure["certification_sha256"]
        )
        self.assertEqual(certification_record["compiler_image"], PINNED_GCC_IMAGE)
        certification_tests = cast(list[dict[str, object]], certification_record["tests"])
        self.assertEqual(
            [(item["visibility"], item["status"]) for item in certification_tests],
            [("PUBLIC", "PASSED"), ("HIDDEN", "PASSED")],
        )
        self.assertEqual(
            canonical_json_sha256(cast(dict[str, object], closure["activation_json"])),
            closure["activation_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
