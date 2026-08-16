from __future__ import annotations

import hashlib
import io
import json
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import make_operation, make_world_snapshot  # noqa: E402
from yaya_agent_contracts import (  # noqa: E402
    Failure,
    OperationContext,
    SandboxLimits,
    SandboxRunRequest,
    SandboxRunResult,
    SandboxUsage,
    SkillRef,
    Success,
    WaterIntent,
)
from yaya_agent_sandbox import (  # noqa: E402
    DockerCppSandbox,
    SandboxOutcomeUnknownError,
    SandboxResultIntegrityError,
)

PINNED_GCC_IMAGE = "gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c"

_ISOLATION_PROBE_SOURCE = r"""
#include <arpa/inet.h>
#include <cerrno>
#include <fstream>
#include <iostream>
#include <netinet/in.h>
#include <stdexcept>
#include <string>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char** argv) {
    if (argc != 2) {
        return 3;
    }
    int port = 0;
    try {
        std::size_t parsed = 0;
        const std::string raw(argv[1]);
        port = std::stoi(raw, &parsed);
        if (parsed != raw.size() || port < 1 || port > 65535) {
            return 3;
        }
    } catch (const std::exception&) {
        return 3;
    }

    bool escaped = false;
    {
        std::ofstream outside("../isolation-sentinel.txt", std::ios::trunc);
        outside << "OVERWRITTEN";
        escaped = escaped || outside.good();
    }
    {
        std::ifstream secret("../isolation-secret.txt");
        std::string leaked;
        std::getline(secret, leaked);
        escaped = escaped || leaked == "HOST_SECRET";
    }

    const int client = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (client >= 0) {
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_port = htons(static_cast<unsigned short>(port));
        if (inet_pton(AF_INET, "127.0.0.1", &address.sin_addr) == 1) {
            const int connected = connect(
                client,
                reinterpret_cast<const sockaddr*>(&address),
                static_cast<socklen_t>(sizeof(address))
            );
            if (connected == 0) {
                const std::string payload = "SANDBOX_NETWORK_ESCAPE:HOST_SECRET";
                send(client, payload.data(), payload.size(), 0);
                escaped = true;
            }
        }
        close(client);
    }

    errno = 0;
    const pid_t child = fork();
    if (child == 0) {
        _exit(0);
    }
    if (child > 0) {
        escaped = true;
        waitpid(child, nullptr, 0);
    } else if (errno != EAGAIN && errno != ENOMEM) {
        return 4;
    }

    const int action_count = escaped ? 7 : 8;
    std::cout << "{\"actions\":[";
    for (int index = 1; index <= action_count; ++index) {
        if (index != 1) {
            std::cout << ',';
        }
        std::cout
            << "{\"intent_id\":\"intent_docker_000" << index
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

_INFINITE_SOURCE = r"""
#include <iostream>
int main() {
    for (;;) {
    }
}
""".strip()


def _compile_linux(source: str, build_root: Path, stem: str) -> Path:
    source_path = build_root / f"{stem}.cpp"
    executable = build_root / stem
    source_path.write_text(source, encoding="utf-8", newline="\n")
    mount = f"type=bind,source={build_root},target=/src"
    compiled = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--mount",
            mount,
            "--workdir",
            "/src",
            PINNED_GCC_IMAGE,
            "g++",
            "-std=c++20",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-o",
            f"/src/{stem}",
            f"/src/{stem}.cpp",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if compiled.returncode != 0 or not executable.is_file():
        raise AssertionError(
            f"pinned Docker C++ compilation failed ({compiled.returncode}):\n"
            f"{compiled.stdout[-2000:]}\n{compiled.stderr[-2000:]}"
        )
    return executable


def _install(executable: Path, artifact_root: Path) -> tuple[SkillRef, Path]:
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    target = artifact_root / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(executable, target)
    target.chmod(stat.S_IREAD)
    return (
        SkillRef(
            skill_id=f"skill_docker_{digest[:16]}",
            skill_version_id=f"skill_version_docker_{digest[:16]}",
            artifact_sha256=digest,
            certification_id=f"certification_docker_{digest[:16]}",
        ),
        target,
    )


# Shared integration-test helpers.  The leading-underscore implementations are
# retained for compatibility with older tests; new vertical E2E tests use the
# explicit public aliases so strict type checking does not rely on private API.
compile_linux = _compile_linux
install_artifact = _install


def _request(
    skill_ref: SkillRef,
    *,
    run_id: str,
    length: int,
    wall_ms: int,
) -> SandboxRunRequest:
    return SandboxRunRequest(
        run_id=run_id,
        skill_ref=skill_ref,
        world_id="world_watering_0001",
        world_snapshot=make_world_snapshot(),
        input={"length": length},
        deterministic_seed="docker-isolation-seed-0001",
        limits=SandboxLimits(
            cpu_ms=500,
            wall_ms=wall_ms,
            memory_bytes=67_108_864,
            max_intents=8,
            max_output_bytes=65_536,
            max_processes=1,
            network_access=False,
        ),
    )


class DockerCppSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_container_blocks_escape_and_cleans_timeout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-docker-sandbox-") as raw_root:
            root = Path(raw_root).resolve()
            build_root = root / "build"
            artifact_root = root / "artifacts"
            temp_root = root / "work"
            result_root = root / "results"
            build_root.mkdir()
            artifact_root.mkdir()
            temp_root.mkdir()
            result_root.mkdir()
            probe_executable = _compile_linux(
                _ISOLATION_PROBE_SOURCE,
                build_root,
                "isolation_probe",
            )
            infinite_executable = _compile_linux(
                _INFINITE_SOURCE,
                build_root,
                "infinite_loop",
            )
            probe_ref, probe_artifact = _install(probe_executable, artifact_root)
            infinite_ref, infinite_artifact = _install(infinite_executable, artifact_root)
            sentinel = temp_root / "isolation-sentinel.txt"
            secret = temp_root / "isolation-secret.txt"
            sentinel.write_text("SAFE", encoding="utf-8")
            secret.write_text("HOST_SECRET", encoding="utf-8")

            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            network_escape = threading.Event()

            def receive_escape() -> None:
                try:
                    listener.settimeout(2)
                    connection, _ = listener.accept()
                    with connection:
                        if b"SANDBOX_NETWORK_ESCAPE" in connection.recv(128):
                            network_escape.set()
                except (OSError, TimeoutError):
                    return

            receiver = threading.Thread(target=receive_escape, daemon=True)
            receiver.start()
            sandbox = DockerCppSandbox(
                artifact_root,
                image=PINNED_GCC_IMAGE,
                result_root=result_root,
                temp_root=temp_root,
            )
            operation = make_operation()
            try:
                probe = await sandbox.run(
                    _request(
                        probe_ref,
                        run_id="run_docker_isolation_0001",
                        length=port,
                        wall_ms=3_000,
                    ),
                    operation,
                )
                self.assertIsInstance(probe, Success)
                assert isinstance(probe, Success)
                self.assertEqual(len(probe.value.action_intents), 8)
                self.assertTrue(
                    all(isinstance(intent, WaterIntent) for intent in probe.value.action_intents)
                )
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "SAFE")
                self.assertEqual(secret.read_text(encoding="utf-8"), "HOST_SECRET")
                self.assertFalse(network_escape.is_set())
                self.assertEqual(
                    sandbox._active,  # pyright: ignore[reportPrivateUsage]
                    {},
                )

                timed_out = await sandbox.run(
                    _request(
                        infinite_ref,
                        run_id="run_docker_timeout_0001",
                        length=1,
                        wall_ms=250,
                    ),
                    operation,
                )
                self.assertIsInstance(timed_out, Failure)
                assert isinstance(timed_out, Failure)
                self.assertEqual(timed_out.error.code, "SANDBOX_RESOURCE_LIMIT")
                self.assertEqual(timed_out.error.details["reason"], "WALL_TIMEOUT")
                self.assertEqual(
                    sandbox._active,  # pyright: ignore[reportPrivateUsage]
                    {},
                )
                leftovers = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "--all",
                        "--quiet",
                        "--filter",
                        "name=yaya-sbx-",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(leftovers.stdout.strip(), "")
            finally:
                listener.close()
                receiver.join(timeout=3)
                probe_artifact.chmod(stat.S_IWRITE | stat.S_IREAD)
                infinite_artifact.chmod(stat.S_IWRITE | stat.S_IREAD)

    def test_unpinned_image_is_rejected_before_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-docker-pin-") as raw_root:
            with self.assertRaisesRegex(ValueError, "pinned"):
                DockerCppSandbox(Path(raw_root), image="gcc:latest", result_root=Path(raw_root))


class DockerCppSandboxRecoveryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _sandbox(root: Path) -> DockerCppSandbox:
        artifact_root = root / "artifacts"
        result_root = root / "results"
        temp_root = root / "work"
        for directory in (artifact_root, result_root, temp_root):
            directory.mkdir(exist_ok=True)
        inspected = subprocess.CompletedProcess(
            ("docker", "image", "inspect"), 0, stdout="linux\n", stderr=""
        )
        with patch("yaya_agent_sandbox.docker.subprocess.run", return_value=inspected):
            return DockerCppSandbox(
                artifact_root,
                image=PINNED_GCC_IMAGE,
                result_root=result_root,
                temp_root=temp_root,
            )

    @staticmethod
    def _fixture() -> tuple[SandboxRunRequest, OperationContext, bytes, Success[SandboxRunResult]]:
        request = _request(
            SkillRef(
                skill_id="skill_sandbox_recovery_0001",
                skill_version_id="skill_version_sandbox_recovery_0001",
                artifact_sha256="a" * 64,
                certification_id="certification_sandbox_recovery_0001",
            ),
            run_id="run_sandbox_recovery_0001",
            length=1,
            wall_ms=3_000,
        )
        operation = make_operation()
        stdout = json.dumps(
            {
                "actions": [
                    {
                        "intent_id": "intent_sandbox_recovery_0001",
                        "action_type": "WATER",
                        "actor_entity_id": "avatar_0001",
                        "expected_world_revision": request.world_snapshot.revision,
                        "plot_id": "plot_0001",
                        "amount_ml": 100,
                    }
                ]
            },
            separators=(",", ":"),
        ).encode()
        started = datetime(2026, 8, 12, 1, 2, 3, tzinfo=UTC)
        outcome = Success(
            SandboxRunResult(
                run_id=request.run_id,
                started_at=started,
                finished_at=started,
                action_intents=(
                    WaterIntent(
                        intent_id="intent_sandbox_recovery_0001",
                        actor_entity_id="avatar_0001",
                        expected_world_revision=request.world_snapshot.revision,
                        plot_id="plot_0001",
                        amount_ml=100,
                    ),
                ),
                stdout_ref=None,
                stderr_ref=None,
                usage=SandboxUsage(cpu_ms=0, wall_ms=0, peak_memory_bytes=0),
                evidence_refs=(),
            )
        )
        return request, operation, stdout, outcome

    async def test_atomic_receipt_survives_restart_and_rejects_tamper_and_identity_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-sandbox-result-") as raw_root:
            root = Path(raw_root).resolve()
            sandbox = self._sandbox(root)
            request, operation, stdout, outcome = self._fixture()
            key = sandbox._run_key(  # pyright: ignore[reportPrivateUsage]
                request.run_id, operation
            )
            identity = sandbox._recovery_identity(  # pyright: ignore[reportPrivateUsage]
                request, operation, key
            )
            sandbox._write_result_receipt(  # pyright: ignore[reportPrivateUsage]
                identity, request, outcome, stdout, b""
            )
            self.assertTrue(identity.receipt_path.is_file())
            self.assertEqual(identity.receipt_path.stat().st_mode & 0o222, 0)

            restarted = self._sandbox(root)
            recovered = await restarted.reconcile(request, operation)
            self.assertEqual(recovered, outcome)

            drifted_request = replace(request, input={"length": 2})
            with self.assertRaisesRegex(SandboxResultIntegrityError, "different request"):
                await restarted.reconcile(drifted_request, operation)
            drifted_context = replace(operation, request_id="req_sandbox_recovery_drift_0001")
            with self.assertRaisesRegex(SandboxResultIntegrityError, "different request"):
                await restarted.reconcile(request, drifted_context)

            envelope = json.loads(identity.receipt_path.read_bytes())
            envelope["payload"]["request_sha256"] = "b" * 64
            identity.receipt_path.chmod(stat.S_IWRITE | stat.S_IREAD)
            identity.receipt_path.write_bytes(
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            )
            identity.receipt_path.chmod(stat.S_IREAD)
            with self.assertRaisesRegex(SandboxResultIntegrityError, "corrupt"):
                await restarted.reconcile(request, operation)

    async def test_success_response_loss_reconciles_after_restart_without_second_execution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-sandbox-response-loss-") as raw_root:
            root = Path(raw_root).resolve()
            sandbox = self._sandbox(root)
            request, operation, stdout, outcome = self._fixture()
            execution_calls = 0

            def execute_once(*_: object) -> object:
                nonlocal execution_calls
                execution_calls += 1
                return type(
                    "Execution",
                    (),
                    {"result": outcome.value, "stdout": stdout, "stderr": b""},
                )()

            with (
                patch.object(
                    sandbox,
                    "_resolve_verified_artifact",
                    return_value=root / "isolated-skill",
                ),
                patch.object(sandbox, "_execute", side_effect=execute_once),
                patch.object(sandbox, "_remove_completed_container"),
            ):
                # The caller deliberately drops this successful response.
                await sandbox.run(request, operation)
            self.assertEqual(execution_calls, 1)

            restarted = self._sandbox(root)
            with (
                patch.object(
                    restarted,
                    "_execute",
                    side_effect=AssertionError("receipt replay must not execute"),
                ),
                patch.object(
                    restarted,
                    "_inspect_container",
                    side_effect=AssertionError("receipt replay must not inspect Docker"),
                ),
            ):
                recovered = await restarted.reconcile(request, operation)
            self.assertEqual(recovered, outcome)
            self.assertEqual(execution_calls, 1)

    async def test_start_attach_ack_loss_recovers_success_logs_without_second_start(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-sandbox-start-ack-loss-") as raw_root:
            root = Path(raw_root).resolve()
            sandbox = self._sandbox(root)
            request, operation, stdout, outcome = self._fixture()
            key = sandbox._run_key(  # pyright: ignore[reportPrivateUsage]
                request.run_id, operation
            )
            identity = sandbox._recovery_identity(  # pyright: ignore[reportPrivateUsage]
                request, operation, key
            )
            common = {
                "Config": {
                    "Labels": dict(identity.labels),
                    "Image": PINNED_GCC_IMAGE,
                    "User": "65534:65534",
                    "WorkingDir": "/tmp",
                },
                "HostConfig": {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "LogConfig": {"Type": "local", "Config": {"max-file": "1"}},
                },
            }
            created = {
                **common,
                "State": {
                    "Status": "created",
                    "ExitCode": 0,
                    "OOMKilled": False,
                },
            }
            exited = {
                **common,
                "State": {
                    "Status": "exited",
                    "ExitCode": 0,
                    "OOMKilled": False,
                    "StartedAt": "2026-08-12T01:02:03.000000000Z",
                    "FinishedAt": "2026-08-12T01:02:03.000000000Z",
                },
            }
            commands: list[tuple[str, ...]] = []

            def recovered_command(
                command: list[str], **_: object
            ) -> subprocess.CompletedProcess[bytes]:
                argv = tuple(command)
                commands.append(argv)
                if argv[1] == "create":
                    return subprocess.CompletedProcess(argv, 0, stdout=b"container\n", stderr=b"")
                if argv[1] == "logs":
                    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")
                if argv[1:3] == ("rm", "--force"):
                    return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
                raise AssertionError(f"unexpected Docker command: {argv}")

            class LostStartAttach:
                def __init__(self) -> None:
                    self.stdout = io.BytesIO()
                    self.stderr = io.BytesIO()

                @staticmethod
                def wait(timeout: float | None = None) -> int:
                    del timeout
                    return 1

                @staticmethod
                def poll() -> int:
                    return 1

                @staticmethod
                def kill() -> None:
                    return None

            persistent_artifact = root / "artifacts" / "aa" / ("a" * 64)
            with (
                patch.object(
                    sandbox,
                    "_resolve_verified_artifact",
                    return_value=persistent_artifact,
                ),
                patch.object(
                    sandbox,
                    "_inspect_container",
                    side_effect=[None, created, exited, exited, exited],
                ),
                patch(
                    "yaya_agent_sandbox.docker.subprocess.run",
                    side_effect=recovered_command,
                ),
                patch(
                    "yaya_agent_sandbox.docker.subprocess.Popen",
                    return_value=LostStartAttach(),
                ) as start_attach,
            ):
                recovered = await sandbox.run(request, operation)
            self.assertEqual(recovered, outcome)
            self.assertEqual(start_attach.call_count, 1)
            self.assertEqual(sum(command[1] == "create" for command in commands), 1)
            self.assertEqual([command[1] for command in commands], ["create", "logs", "rm"])
            started_argv = tuple(start_attach.call_args.args[0])
            self.assertEqual(started_argv[1:3], ("start", "--attach"))

    async def test_unavailable_before_create_recovers_once_via_durable_launch_intent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-sandbox-unavailable-") as raw_root:
            root = Path(raw_root).resolve()
            sandbox = self._sandbox(root)
            request, operation, stdout, outcome = self._fixture()
            persistent_artifact = root / "artifacts" / "aa" / ("a" * 64)
            with (
                patch.object(
                    sandbox,
                    "_resolve_verified_artifact",
                    return_value=persistent_artifact,
                ),
                patch.object(
                    sandbox,
                    "_inspect_container",
                    side_effect=SandboxOutcomeUnknownError("daemon unavailable"),
                ),
                patch(
                    "yaya_agent_sandbox.docker.subprocess.run",
                    side_effect=AssertionError("unavailable preflight must not mutate Docker"),
                ),
                self.assertRaises(SandboxOutcomeUnknownError),
            ):
                await sandbox.run(request, operation)
            self.assertEqual(len(list((root / "results").rglob("*.launch.json"))), 1)
            self.assertEqual(
                [
                    path
                    for path in (root / "results").rglob("*.json")
                    if not path.name.endswith(".launch.json")
                ],
                [],
            )

            restarted = self._sandbox(root)
            key = restarted._run_key(  # pyright: ignore[reportPrivateUsage]
                request.run_id, operation
            )
            identity = restarted._recovery_identity(  # pyright: ignore[reportPrivateUsage]
                request, operation, key
            )
            common = {
                "Config": {
                    "Labels": dict(identity.labels),
                    "Image": PINNED_GCC_IMAGE,
                    "User": "65534:65534",
                    "WorkingDir": "/tmp",
                },
                "HostConfig": {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "LogConfig": {"Type": "local", "Config": {"max-file": "1"}},
                },
            }
            created = {
                **common,
                "State": {"Status": "created", "ExitCode": 0, "OOMKilled": False},
            }
            exited = {
                **common,
                "State": {
                    "Status": "exited",
                    "ExitCode": 0,
                    "OOMKilled": False,
                    "StartedAt": "2026-08-12T01:02:03.000000000Z",
                    "FinishedAt": "2026-08-12T01:02:03.000000000Z",
                },
            }
            commands: list[tuple[str, ...]] = []

            def recovered_command(
                command: list[str], **_: object
            ) -> subprocess.CompletedProcess[bytes]:
                argv = tuple(command)
                commands.append(argv)
                if argv[1] in {"create", "start"}:
                    return subprocess.CompletedProcess(argv, 0, stdout=b"container\n", stderr=b"")
                if argv[1] == "logs":
                    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")
                if argv[1:3] == ("rm", "--force"):
                    return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
                raise AssertionError(f"unexpected Docker command: {argv}")

            with (
                patch.object(
                    restarted,
                    "_resolve_verified_artifact",
                    return_value=persistent_artifact,
                ),
                patch.object(
                    restarted,
                    "_inspect_container",
                    side_effect=[None, None, created, exited, exited],
                ),
                patch(
                    "yaya_agent_sandbox.docker.subprocess.run",
                    side_effect=recovered_command,
                ),
            ):
                recovered = await restarted.reconcile(request, operation)
            self.assertEqual(recovered, outcome)
            self.assertEqual(
                [command[1] for command in commands], ["create", "start", "logs", "rm"]
            )
            create_command = next(command for command in commands if command[1] == "create")
            mount_index = create_command.index("--mount") + 1
            self.assertIn(str(persistent_artifact), create_command[mount_index])
            self.assertEqual(sum(command[1] == "create" for command in commands), 1)
            self.assertEqual(sum(command[1] == "start" for command in commands), 1)

    async def test_created_container_is_started_once_and_reconciled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-sandbox-created-") as raw_root:
            root = Path(raw_root).resolve()
            sandbox = self._sandbox(root)
            request, operation, stdout, outcome = self._fixture()
            key = sandbox._run_key(  # pyright: ignore[reportPrivateUsage]
                request.run_id, operation
            )
            identity = sandbox._recovery_identity(  # pyright: ignore[reportPrivateUsage]
                request, operation, key
            )
            common = {
                "Config": {
                    "Labels": dict(identity.labels),
                    "Image": PINNED_GCC_IMAGE,
                    "User": "65534:65534",
                    "WorkingDir": "/tmp",
                },
                "HostConfig": {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "LogConfig": {"Type": "local", "Config": {"max-file": "1"}},
                },
            }
            created = {
                **common,
                "State": {"Status": "created", "ExitCode": 0, "OOMKilled": False},
            }
            exited = {
                **common,
                "State": {
                    "Status": "exited",
                    "ExitCode": 0,
                    "OOMKilled": False,
                    "StartedAt": "2026-08-12T01:02:03.000000000Z",
                    "FinishedAt": "2026-08-12T01:02:03.000000000Z",
                },
            }
            commands: list[tuple[str, ...]] = []

            def recovered_command(
                command: list[str], **_: object
            ) -> subprocess.CompletedProcess[bytes]:
                argv = tuple(command)
                commands.append(argv)
                if argv[1] == "start":
                    return subprocess.CompletedProcess(argv, 0, stdout=b"container\n", stderr=b"")
                if argv[1] == "logs":
                    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")
                if argv[1:3] == ("rm", "--force"):
                    return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
                raise AssertionError(f"unexpected Docker command: {argv}")

            with (
                patch.object(
                    sandbox,
                    "_inspect_container",
                    side_effect=[created, exited, exited],
                ),
                patch(
                    "yaya_agent_sandbox.docker.subprocess.run",
                    side_effect=recovered_command,
                ),
            ):
                recovered = await sandbox.reconcile(request, operation)
            self.assertEqual(recovered, outcome)
            self.assertEqual([command[1] for command in commands], ["start", "logs", "rm"])
            self.assertEqual(sum(command[1] == "start" for command in commands), 1)
            self.assertFalse(any(command[1] == "create" for command in commands))

    async def test_exited_success_is_reconciled_from_bounded_logs_without_second_start(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-sandbox-exited-") as raw_root:
            root = Path(raw_root).resolve()
            sandbox = self._sandbox(root)
            request, operation, stdout, outcome = self._fixture()
            key = sandbox._run_key(  # pyright: ignore[reportPrivateUsage]
                request.run_id, operation
            )
            identity = sandbox._recovery_identity(  # pyright: ignore[reportPrivateUsage]
                request, operation, key
            )
            inspected = {
                "Config": {
                    "Labels": dict(identity.labels),
                    "Image": PINNED_GCC_IMAGE,
                    "User": "65534:65534",
                    "WorkingDir": "/tmp",
                },
                "HostConfig": {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "LogConfig": {"Type": "local", "Config": {"max-file": "1"}},
                },
                "State": {
                    "Status": "exited",
                    "ExitCode": 0,
                    "OOMKilled": False,
                    "StartedAt": "2026-08-12T01:02:03.000000000Z",
                    "FinishedAt": "2026-08-12T01:02:03.000000000Z",
                },
            }
            commands: list[tuple[str, ...]] = []

            def recovered_command(
                command: list[str], **_: object
            ) -> subprocess.CompletedProcess[bytes]:
                argv = tuple(command)
                commands.append(argv)
                if argv[1] == "logs":
                    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")
                if argv[1:3] == ("rm", "--force"):
                    return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
                raise AssertionError(f"unexpected recovery command: {argv}")

            with (
                patch.object(sandbox, "_inspect_container", side_effect=[inspected, inspected]),
                patch("yaya_agent_sandbox.docker.subprocess.run", side_effect=recovered_command),
            ):
                recovered = await sandbox.reconcile(request, operation)
            self.assertEqual(recovered, outcome)
            self.assertEqual([command[1] for command in commands], ["logs", "rm"])
            self.assertFalse(any(command[1] in {"create", "start"} for command in commands))

            restarted = self._sandbox(root)
            with patch.object(
                restarted,
                "_inspect_container",
                side_effect=AssertionError("durable receipt must avoid Docker"),
            ):
                self.assertEqual(await restarted.reconcile(request, operation), outcome)

    async def test_running_container_is_waited_and_reconciled_without_second_start(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-sandbox-running-") as raw_root:
            root = Path(raw_root).resolve()
            sandbox = self._sandbox(root)
            request, operation, stdout, outcome = self._fixture()
            key = sandbox._run_key(  # pyright: ignore[reportPrivateUsage]
                request.run_id, operation
            )
            identity = sandbox._recovery_identity(  # pyright: ignore[reportPrivateUsage]
                request, operation, key
            )
            common = {
                "Config": {
                    "Labels": dict(identity.labels),
                    "Image": PINNED_GCC_IMAGE,
                    "User": "65534:65534",
                    "WorkingDir": "/tmp",
                },
                "HostConfig": {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "LogConfig": {"Type": "local", "Config": {"max-file": "1"}},
                },
            }
            running = {
                **common,
                "State": {
                    "Status": "running",
                    "ExitCode": 0,
                    "OOMKilled": False,
                    "StartedAt": "2026-08-12T01:02:03.000000000Z",
                    "FinishedAt": "0001-01-01T00:00:00Z",
                },
            }
            exited = {
                **common,
                "State": {
                    "Status": "exited",
                    "ExitCode": 0,
                    "OOMKilled": False,
                    "StartedAt": "2026-08-12T01:02:03.000000000Z",
                    "FinishedAt": "2026-08-12T01:02:03.000000000Z",
                },
            }
            commands: list[tuple[str, ...]] = []

            def recovered_command(
                command: list[str], **_: object
            ) -> subprocess.CompletedProcess[bytes]:
                argv = tuple(command)
                commands.append(argv)
                if argv[1] == "wait":
                    return subprocess.CompletedProcess(argv, 0, stdout=b"0\n", stderr=b"")
                if argv[1] == "logs":
                    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")
                if argv[1:3] == ("rm", "--force"):
                    return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
                raise AssertionError(f"unexpected recovery command: {argv}")

            with (
                patch.object(
                    sandbox,
                    "_inspect_container",
                    side_effect=[running, exited, exited],
                ),
                patch("yaya_agent_sandbox.docker.subprocess.run", side_effect=recovered_command),
            ):
                recovered = await sandbox.reconcile(request, operation)
            self.assertEqual(recovered, outcome)
            self.assertEqual([command[1] for command in commands], ["wait", "logs", "rm"])
            self.assertFalse(any(command[1] in {"create", "start"} for command in commands))
            self.assertEqual(
                {command[-1] for command in commands},
                {f"yaya-sbx-{identity.run_key_sha256[:24]}"},
            )

    async def test_container_request_hash_drift_fails_closed_before_takeover(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-sandbox-container-drift-") as raw_root:
            root = Path(raw_root).resolve()
            sandbox = self._sandbox(root)
            request, operation, _, _ = self._fixture()
            key = sandbox._run_key(  # pyright: ignore[reportPrivateUsage]
                request.run_id, operation
            )
            identity = sandbox._recovery_identity(  # pyright: ignore[reportPrivateUsage]
                request, operation, key
            )
            labels = dict(identity.labels)
            labels["local.yaya.request_sha256"] = "b" * 64
            inspected = {
                "Config": {
                    "Labels": labels,
                    "Image": PINNED_GCC_IMAGE,
                    "User": "65534:65534",
                    "WorkingDir": "/tmp",
                },
                "HostConfig": {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "LogConfig": {"Type": "local", "Config": {"max-file": "1"}},
                },
                "State": {"Status": "exited"},
            }
            with (
                patch.object(sandbox, "_inspect_container", return_value=inspected),
                patch(
                    "yaya_agent_sandbox.docker.subprocess.run",
                    side_effect=AssertionError("identity drift must fail before Docker mutation"),
                ),
                self.assertRaisesRegex(SandboxResultIntegrityError, "identity.*drifted"),
            ):
                await sandbox.reconcile(request, operation)


if __name__ == "__main__":
    unittest.main()
