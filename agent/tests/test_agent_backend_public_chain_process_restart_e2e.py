from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import hashlib
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "python"
TEST_ROOT = Path(__file__).resolve().parent
CONTRACTS_ROOT = REPOSITORY_ROOT / "contracts"
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from a8_state_fingerprint import (  # noqa: E402
    A8StateFingerprint,
    a8_state_fingerprint,
    missing_a8_business_tables,
)
from agent_runtime_fixtures import WORLD_ID, make_operation  # noqa: E402
from postgres_test_support import postgres_test_server  # noqa: E402
from test_agent_backend_skill_build_executor import (  # noqa: E402
    AGENT_PROFILE_ID,
    LEARNER_ID,
    PINNED_GCC_IMAGE,
    TEST_SUITE_VERSION,
    _seed_only_build_authority,
)
from yaya_agent_backend.auth import JwtAuthenticator  # noqa: E402
from yaya_agent_backend.composition import production_versions  # noqa: E402
from yaya_agent_backend.config import ProductionSettings  # noqa: E402
from yaya_agent_backend.database import PostgresDatabase  # noqa: E402
from yaya_agent_backend.wire import ContractSchemaValidator  # noqa: E402
from yaya_agent_build import (  # noqa: E402
    CPP20_SAFE_V1_PROFILE,
    canonical_source_bundle_sha256,
)

JWT_SECRET = "process-restart-e2e-secret-000000000000000000000"
JWT_ISSUER = "yaya-process-restart-e2e"
JWT_AUDIENCE = "yaya-process-restart-api"
SKILL_ID = "skill_process_restart_e2e_0001"
DRAFT_ID = "draft_process_restart_e2e_0001"
_STARTUP_SECONDS = 45.0
_COMMAND_SECONDS = 240.0
_SHUTDOWN_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class _HttpResult:
    status: int
    headers: dict[str, str]
    payload: dict[str, object]
    raw_body: bytes


@dataclass(frozen=True, slots=True)
class _Request:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class _CompletedControl:
    request: _Request
    accepted: _HttpResult
    command: dict[str, object]
    resource_url: str
    resource: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ArtifactSnapshot:
    path: Path
    payload: bytes
    mode: int
    size: int
    modified_ns: int


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


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        candidate.bind(("127.0.0.1", 0))
        return cast(int, candidate.getsockname()[1])


class PublicChainProcessRestartE2ETests(unittest.IsolatedAsyncioTestCase):
    """Real OS-process replay over public HTTP, PostgreSQL and pinned Docker."""

    async def test_public_chain_survives_fresh_serve_and_worker_processes(self) -> None:
        started = time.perf_counter()
        self._assert_real_docker_dependency()
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
        active_processes: list[subprocess.Popen[str]] = []
        process_pids: list[int] = []

        with tempfile.TemporaryDirectory(prefix="yaya-process-restart-e2e-") as raw_root:
            artifact_root = Path(raw_root).resolve() / "artifacts"
            artifact_root.mkdir()
            try:
                with postgres_test_server() as postgres:
                    database = PostgresDatabase(postgres.dsn)
                    await database.migrate()
                    port = _reserve_loopback_port()
                    settings = self._settings(postgres.dsn, artifact_root, port)
                    authority_context = make_operation(
                        command_id="cmd_process_restart_authority_0001"
                    )
                    await _seed_only_build_authority(
                        database,
                        context_override=authority_context,
                        versions_override=production_versions(settings),
                    )
                    token = JwtAuthenticator(
                        hmac_secret=JWT_SECRET,
                        issuer=JWT_ISSUER,
                        audience=JWT_AUDIENCE,
                    ).issue_for_test(authority_context.actor, now=datetime.now(UTC))
                    validator = ContractSchemaValidator(CONTRACTS_ROOT)

                    try:
                        first_serve = self._spawn(
                            "serve",
                            generation="first",
                            settings=settings,
                        )
                        active_processes.append(first_serve)
                        process_pids.append(first_serve.pid)
                        await self._wait_http_ready(first_serve, port, token)
                        first_worker = self._spawn(
                            "worker",
                            generation="first",
                            settings=settings,
                        )
                        active_processes.append(first_worker)
                        process_pids.append(first_worker.pid)
                        await self._wait_worker_alive(first_worker)
                        self.assertNotEqual(first_serve.pid, first_worker.pid)

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
                        session_request = self._request(
                            token,
                            "POST",
                            "/v1/agent-sessions",
                            session_body,
                            suffix="session",
                            idempotency_key="process-restart-session-0001",
                        )
                        session_control = await self._complete_control(
                            port,
                            first_worker,
                            session_request,
                            validator,
                        )
                        session = session_control.resource
                        validator.validate("schemas/game/agent-session.schema.json", session)
                        self.assertEqual(
                            (session["status"], session["last_turn_sequence"]), ("ACTIVE", 0)
                        )
                        session_id = cast(str, session["session_id"])

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
                            "display_name": "Process Restart Docker Skill",
                            "source_bundle": bundle,
                            "client_saved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        }
                        draft_request = self._request(
                            token,
                            "PUT",
                            draft_target,
                            draft_body,
                            suffix="draft",
                            idempotency_key="process-restart-draft-0001",
                        )
                        draft_result = await self._exchange_async(port, draft_request)
                        self.assertEqual(draft_result.status, 201, draft_result.payload)
                        self.assertEqual(draft_result.headers.get("idempotency-replayed"), "false")
                        self.assertEqual(draft_result.headers.get("location"), draft_target)
                        validator.validate(
                            "schemas/product-experience/skill-draft.schema.json",
                            draft_result.payload,
                        )
                        draft_etag = f'"draft:1:{draft_result.payload["draft_sha256"]}"'
                        self.assertEqual(draft_result.headers.get("etag"), draft_etag)

                        build_body: dict[str, object] = {
                            "skill_id": SKILL_ID,
                            "display_name": "Process Restart Docker Skill",
                            "client_draft_revision": 1,
                            "source_bundle": bundle,
                            "compiler_profile": CPP20_SAFE_V1_PROFILE,
                            "test_suite_version": TEST_SUITE_VERSION,
                            "requested_capabilities": ["WATER", "WORLD_READ"],
                        }
                        build_request = self._request(
                            token,
                            "POST",
                            "/v1/skill-builds",
                            build_body,
                            suffix="build",
                            idempotency_key="process-restart-build-0001",
                        )
                        build_control = await self._complete_control(
                            port,
                            first_worker,
                            build_request,
                            validator,
                        )
                        build = build_control.resource
                        validator.validate("schemas/game/skill-build.schema.json", build)
                        self.assertEqual((build["status"], build["terminal"]), ("CERTIFIED", True))
                        artifact = cast(dict[str, object], build["artifact"])
                        certification = cast(dict[str, object], build["certification"])
                        self.assertEqual(artifact["source_sha256"], expected_source_sha256)
                        artifact_sha256 = cast(str, artifact["artifact_sha256"])
                        build_id = cast(str, build["build_id"])
                        skill_version_id = cast(str, build["skill_version_id"])
                        certification_id = cast(str, certification["certification_id"])

                        activation_body: dict[str, object] = {
                            "expected_registry_revision": 0,
                            "activation_scope": {
                                "world_id": WORLD_ID,
                                "agent_profile_id": AGENT_PROFILE_ID,
                            },
                            "reason": "Prove public process-restart replay.",
                        }
                        activation_request = self._request(
                            token,
                            "POST",
                            f"/v1/skill-versions/{skill_version_id}/activations",
                            activation_body,
                            suffix="activation",
                            idempotency_key="process-restart-activation-0001",
                        )
                        activation_control = await self._complete_control(
                            port,
                            first_worker,
                            activation_request,
                            validator,
                        )
                        activation = activation_control.resource
                        validator.validate("schemas/game/skill-activation.schema.json", activation)
                        self.assertEqual(
                            (
                                activation["skill_version_id"],
                                activation["certification_id"],
                                activation["artifact_sha256"],
                                activation["registry_revision"],
                            ),
                            (skill_version_id, certification_id, artifact_sha256, 1),
                        )

                        first_worker_exit = await self._crash_and_reap(
                            first_worker,
                            "first worker",
                        )
                        active_processes.remove(first_worker)
                        first_serve_exit = await self._crash_and_reap(
                            first_serve,
                            "first serve",
                        )
                        active_processes.remove(first_serve)
                        self.assertNotEqual(first_worker_exit, 0)
                        self.assertNotEqual(first_serve_exit, 0)

                        baseline = await self._state(database)
                        self._assert_exact_counts(baseline)
                        artifact_snapshot = self._artifact_snapshot(
                            artifact_root,
                            artifact_sha256,
                        )
                        self._assert_workspace_empty(artifact_root)
                        self.assertEqual(self._build_containers(build_id), [])
                        replay_events_since = self._docker_timestamp()

                        second_serve = self._spawn(
                            "serve",
                            generation="second",
                            settings=settings,
                        )
                        active_processes.append(second_serve)
                        process_pids.append(second_serve.pid)
                        await self._wait_http_ready(second_serve, port, token)
                        second_worker = self._spawn(
                            "worker",
                            generation="second",
                            settings=settings,
                        )
                        active_processes.append(second_worker)
                        process_pids.append(second_worker.pid)
                        await self._wait_worker_alive(second_worker)
                        self.assertEqual(len(set(process_pids)), 4, process_pids)

                        replayed_session = await self._exchange_async(port, session_request)
                        self._assert_replay(session_control.accepted, replayed_session)
                        replayed_draft = await self._exchange_async(port, draft_request)
                        self._assert_replay(draft_result, replayed_draft, expect_etag=True)
                        replayed_build = await self._exchange_async(port, build_request)
                        self._assert_replay(build_control.accepted, replayed_build)
                        replayed_activation = await self._exchange_async(
                            port,
                            activation_request,
                        )
                        self._assert_replay(
                            activation_control.accepted,
                            replayed_activation,
                        )

                        await self._assert_control_snapshot(
                            port,
                            token,
                            session_control,
                            suffix="session-replay",
                        )
                        await self._assert_control_snapshot(
                            port,
                            token,
                            build_control,
                            suffix="build-replay",
                        )
                        await self._assert_control_snapshot(
                            port,
                            token,
                            activation_control,
                            suffix="activation-replay",
                        )
                        draft_get = await self._exchange_async(
                            port,
                            _Request(
                                "GET",
                                draft_target,
                                self._headers(token, "draft-get"),
                                b"",
                            ),
                        )
                        self.assertEqual(draft_get.status, 200, draft_get.payload)
                        self.assertEqual(draft_get.payload, draft_result.payload)
                        self.assertEqual(draft_get.headers.get("etag"), draft_etag)

                        await self._crash_and_reap(second_worker, "second worker")
                        active_processes.remove(second_worker)
                        await self._crash_and_reap(second_serve, "second serve")
                        active_processes.remove(second_serve)
                        replay_events_until = self._docker_timestamp()

                        after = await self._state(database)
                        self.assertEqual(after, baseline)
                        self._assert_exact_counts(after)
                        self.assertEqual(
                            self._artifact_snapshot(artifact_root, artifact_sha256),
                            artifact_snapshot,
                        )
                        self._assert_workspace_empty(artifact_root)
                        self.assertEqual(self._build_containers(build_id), [])
                        self.assertEqual(
                            self._build_create_events(
                                build_id,
                                replay_events_since,
                                replay_events_until,
                            ),
                            [],
                        )
                    finally:
                        await self._force_cleanup(active_processes)

                elapsed = time.perf_counter() - started
                print(
                    "A8_PUBLIC_CHAIN_PROCESS_RESTART_OK "
                    f"serve_pids={process_pids[0]},{process_pids[2]} "
                    f"worker_pids={process_pids[1]},{process_pids[3]} "
                    "sessions=1 builds=1 artifacts=1 skill_versions=1 "
                    f"certifications=1 activations=1 duration_seconds={elapsed:.3f}",
                    flush=True,
                )
            finally:
                for candidate in artifact_root.rglob("*"):
                    if candidate.is_file() and not candidate.is_symlink():
                        candidate.chmod(stat.S_IWRITE | stat.S_IREAD)

    @staticmethod
    def _settings(dsn: str, artifact_root: Path, port: int) -> ProductionSettings:
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
            http_port=port,
            worker_id="worker_process_restart_seed_0001",
            worker_lease_seconds=180,
            worker_poll_ms=25,
            learner_worker_id="learner_process_restart_unused_0001",
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
    def _environment(settings: ProductionSettings, generation: str) -> dict[str, str]:
        environment = os.environ.copy()
        existing_pythonpath = environment.get("PYTHONPATH", "").strip()
        environment["PYTHONPATH"] = str(PACKAGE_ROOT) + (
            os.pathsep + existing_pythonpath if existing_pythonpath else ""
        )
        environment.update(
            {
                "PYTHONUNBUFFERED": "1",
                "YAYA_DATABASE_DSN": settings.database_dsn,
                "YAYA_ARTIFACT_ROOT": str(settings.artifact_root),
                "YAYA_CONTRACTS_ROOT": str(settings.contracts_root),
                "YAYA_AUTH_HMAC_SECRET": JWT_SECRET,
                "YAYA_AUTH_ISSUER": JWT_ISSUER,
                "YAYA_AUTH_AUDIENCE": JWT_AUDIENCE,
                "YAYA_LLM_MODE": "fallback",
                "YAYA_LLM_MODEL": "explicit-fallback",
                "YAYA_LLM_PROVIDER": "explicit-fallback",
                "YAYA_HTTP_HOST": "127.0.0.1",
                "YAYA_HTTP_PORT": str(settings.http_port),
                "YAYA_WORKER_ID": f"worker_process_restart_{generation}_0001",
                "YAYA_WORKER_LEASE_SECONDS": "180",
                "YAYA_WORKER_POLL_MS": "25",
                "YAYA_SANDBOX_IMAGE": PINNED_GCC_IMAGE,
                "YAYA_DOCKER_EXE": "docker",
            }
        )
        for name in (
            "YAYA_LLM_ENDPOINT",
            "YAYA_LLM_API_KEY",
            "YAYA_LLM_API_KEY_FILE",
            "YAYA_LLM_THINKING_MODE",
        ):
            environment.pop(name, None)
        return environment

    def _spawn(
        self,
        role: str,
        *,
        generation: str,
        settings: ProductionSettings,
    ) -> subprocess.Popen[str]:
        command = [sys.executable, "-m", "yaya_agent_backend", role]
        environment = self._environment(settings, generation)
        if os.name == "nt":
            return subprocess.Popen(
                command,
                cwd=REPOSITORY_ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        return subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )

    async def _wait_http_ready(
        self,
        process: subprocess.Popen[str],
        port: int,
        token: str,
    ) -> None:
        deadline = time.monotonic() + _STARTUP_SECONDS
        request = _Request(
            "GET",
            "/v1/bootstrap",
            self._headers(token, f"ready-{process.pid}"),
            b"",
        )
        while time.monotonic() < deadline:
            self._assert_process_alive(process, "serve")
            try:
                response = await self._exchange_async(port, request)
                if response.status == 200:
                    return
            except (ConnectionError, OSError, TimeoutError):
                pass
            await asyncio.sleep(0.05)
        self.fail(f"serve PID {process.pid} did not become HTTP-ready")

    async def _wait_worker_alive(self, process: subprocess.Popen[str]) -> None:
        await asyncio.sleep(1.0)
        self._assert_process_alive(process, "worker")

    @staticmethod
    def _assert_process_alive(process: subprocess.Popen[str], role: str) -> None:
        return_code = process.poll()
        if return_code is None:
            return
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(
            f"{role} PID {process.pid} exited with {return_code}; "
            f"stdout={stdout[-2000:]!r}; stderr={stderr[-2000:]!r}"
        )

    async def _crash_and_reap(
        self,
        process: subprocess.Popen[str],
        label: str,
    ) -> int:
        """Intentionally crash the process group to prove durable restart recovery."""
        self._assert_process_alive(process, label)
        started = time.monotonic()
        if os.name == "nt":
            killed = await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_SHUTDOWN_SECONDS,
            )
            if killed.returncode != 0 and process.poll() is None:
                raise AssertionError(
                    f"failed to crash {label} PID {process.pid}; "
                    f"stdout={killed.stdout[-2000:]!r}; "
                    f"stderr={killed.stderr[-2000:]!r}"
                )
        else:
            os.killpg(process.pid, signal.SIGKILL)
        try:
            return_code = await asyncio.to_thread(process.wait, _SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise AssertionError(f"{label} PID {process.pid} was not reaped") from error
        await asyncio.to_thread(process.communicate)
        self.assertNotEqual(return_code, 0, label)
        self.assertLess(time.monotonic() - started, _SHUTDOWN_SECONDS)
        self.assertEqual(process.poll(), return_code)
        return return_code

    @staticmethod
    async def _force_cleanup(processes: list[subprocess.Popen[str]]) -> None:
        for process in reversed(processes):
            if process.poll() is None:
                process.kill()
            try:
                await asyncio.to_thread(process.communicate, timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.communicate)

    @staticmethod
    def _headers(
        token: str,
        suffix: str,
        *,
        body: bytes | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        normalized = hashlib.sha256(suffix.encode("utf-8")).hexdigest()[:16]
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Schema-Version": "1.0.0",
            "X-Request-Id": f"req_process_restart_{normalized}",
            "X-Trace-Id": f"trace_process_restart_{normalized}",
            "X-Correlation-Id": f"corr_process_restart_{normalized}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _request(
        self,
        token: str,
        method: str,
        target: str,
        value: Mapping[str, object],
        *,
        suffix: str,
        idempotency_key: str,
    ) -> _Request:
        body = _json_bytes(value)
        return _Request(
            method,
            target,
            self._headers(
                token,
                suffix,
                body=body,
                idempotency_key=idempotency_key,
            ),
            body,
        )

    @staticmethod
    def _exchange(port: int, request: _Request) -> _HttpResult:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
        try:
            connection.request(
                request.method,
                request.target,
                body=request.body or None,
                headers=request.headers,
            )
            response = connection.getresponse()
            raw_body = response.read()
            decoded = cast(object, json.loads(raw_body.decode("utf-8")))
            if not isinstance(decoded, dict):
                raise AssertionError(f"{request.method} {request.target} returned non-object JSON")
            return _HttpResult(
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                cast(dict[str, object], decoded),
                raw_body,
            )
        finally:
            connection.close()

    async def _exchange_async(self, port: int, request: _Request) -> _HttpResult:
        return await asyncio.to_thread(self._exchange, port, request)

    async def _complete_control(
        self,
        port: int,
        worker: subprocess.Popen[str],
        request: _Request,
        validator: ContractSchemaValidator,
    ) -> _CompletedControl:
        accepted = await self._exchange_async(port, request)
        self.assertEqual(accepted.status, 202, accepted.payload)
        self.assertEqual(accepted.headers.get("idempotency-replayed"), "false")
        validator.validate("schemas/game/accepted-game-job.schema.json", accepted.payload)
        location = accepted.headers.get("location")
        self.assertEqual(location, f"/v1/commands/{accepted.payload['command_id']}")
        command = await self._await_terminal_command(
            port,
            worker,
            cast(str, location),
            request.headers["Authorization"],
            validator,
        )
        self.assertEqual(command["status"], "APPLIED", command)
        result = cast(dict[str, object], command["result"])
        resource_url = cast(str, result["resource_url"])
        token = request.headers["Authorization"].removeprefix("Bearer ")
        resource_result = await self._exchange_async(
            port,
            _Request(
                "GET",
                resource_url,
                self._headers(token, f"resource-{result['resource_id']}"),
                b"",
            ),
        )
        self.assertEqual(resource_result.status, 200, resource_result.payload)
        return _CompletedControl(
            request,
            accepted,
            command,
            resource_url,
            resource_result.payload,
        )

    async def _await_terminal_command(
        self,
        port: int,
        worker: subprocess.Popen[str],
        location: str,
        authorization: str,
        validator: ContractSchemaValidator,
    ) -> dict[str, object]:
        token = authorization.removeprefix("Bearer ")
        deadline = time.monotonic() + _COMMAND_SECONDS
        attempt = 0
        while time.monotonic() < deadline:
            self._assert_process_alive(worker, "worker")
            attempt += 1
            response = await self._exchange_async(
                port,
                _Request(
                    "GET",
                    location,
                    self._headers(token, f"command-{location[-8:]}-{attempt}"),
                    b"",
                ),
            )
            self.assertEqual(response.status, 200, response.payload)
            validator.validate("schemas/game/command.schema.json", response.payload)
            if response.payload.get("terminal") is True:
                return response.payload
            await asyncio.sleep(0.2)
        self.fail(f"command {location} did not terminate under worker PID {worker.pid}")

    def _assert_replay(
        self,
        original: _HttpResult,
        replayed: _HttpResult,
        *,
        expect_etag: bool = False,
    ) -> None:
        self.assertEqual(replayed.status, original.status, replayed.payload)
        self.assertEqual(replayed.payload, original.payload)
        self.assertEqual(original.headers.get("idempotency-replayed"), "false")
        self.assertEqual(replayed.headers.get("idempotency-replayed"), "true")
        for name in (
            "location",
            "content-type",
            "content-length",
            "cache-control",
            "x-request-id",
            "x-trace-id",
            "x-correlation-id",
        ):
            self.assertEqual(replayed.headers.get(name), original.headers.get(name), name)
        self.assertIsNotNone(original.headers.get("location"))
        if expect_etag:
            self.assertIsNotNone(original.headers.get("etag"))
            self.assertEqual(replayed.headers.get("etag"), original.headers.get("etag"))

    async def _assert_control_snapshot(
        self,
        port: int,
        token: str,
        control: _CompletedControl,
        *,
        suffix: str,
    ) -> None:
        location = control.accepted.headers["location"]
        command = await self._exchange_async(
            port,
            _Request("GET", location, self._headers(token, f"{suffix}-command"), b""),
        )
        self.assertEqual(command.status, 200, command.payload)
        self.assertEqual(command.payload, control.command)
        resource = await self._exchange_async(
            port,
            _Request(
                "GET",
                control.resource_url,
                self._headers(token, f"{suffix}-resource"),
                b"",
            ),
        )
        self.assertEqual(resource.status, 200, resource.payload)
        self.assertEqual(resource.payload, control.resource)

    async def _state(self, database: PostgresDatabase) -> A8StateFingerprint:
        state = await a8_state_fingerprint(database)
        self.assertEqual(missing_a8_business_tables(state), ())
        return state

    def _assert_exact_counts(self, state: A8StateFingerprint) -> None:
        expected = {
            "yaya_agent_sessions": 1,
            "yaya_public_agent_sessions": 1,
            "yaya_skill_draft_revisions": 1,
            "yaya_skill_draft_heads": 1,
            "yaya_product_write_receipts": 1,
            "yaya_skill_builds": 1,
            "yaya_skill_build_history": 3,
            "yaya_build_step_receipts": 5,
            "yaya_artifacts": 1,
            "yaya_compile_results": 1,
            "yaya_evidence": 1,
            "yaya_skills": 1,
            "yaya_skill_certifications": 1,
            "yaya_registry_certifications": 1,
            "yaya_registry_heads": 1,
            "yaya_registry_entries": 1,
            "yaya_skill_activations": 1,
            "yaya_commands": 3,
            "yaya_control_jobs": 3,
            "yaya_command_jobs": 0,
        }
        self.assertEqual(
            {table: state[table].row_count for table in expected},
            expected,
        )

    def _artifact_snapshot(self, root: Path, digest: str) -> _ArtifactSnapshot:
        path = root / digest[:2] / digest
        metadata = path.lstat()
        self.assertTrue(path.is_file())
        self.assertFalse(path.is_symlink())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual(metadata.st_mode & 0o222, 0)
        return _ArtifactSnapshot(
            path.resolve(strict=True),
            path.read_bytes(),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_size,
            metadata.st_mtime_ns,
        )

    def _assert_workspace_empty(self, artifact_root: Path) -> None:
        workspace_root = artifact_root / ".build-workspaces"
        self.assertTrue(workspace_root.is_dir())
        self.assertEqual(list(workspace_root.rglob("*")), [])

    @staticmethod
    def _build_containers(build_id: str) -> list[str]:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--all",
                "--filter",
                f"label=local.yaya.build_id={build_id}",
                "--format",
                "{{.ID}} {{.Names}}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return [line for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def _docker_timestamp() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @staticmethod
    def _build_create_events(build_id: str, since: str, until: str) -> list[str]:
        result = subprocess.run(
            [
                "docker",
                "events",
                "--since",
                since,
                "--until",
                until,
                "--filter",
                "type=container",
                "--filter",
                "event=create",
                "--filter",
                f"label=local.yaya.build_id={build_id}",
                "--format",
                "{{json .}}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return [line for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def _assert_real_docker_dependency() -> None:
        for command in (
            ["docker", "version"],
            ["docker", "image", "inspect", PINNED_GCC_IMAGE],
        ):
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )


if __name__ == "__main__":
    unittest.main()
