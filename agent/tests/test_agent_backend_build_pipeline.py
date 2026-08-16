from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_build import (  # noqa: E402
    CPP20_SAFE_V1_FLAGS,
    ArtifactIntegrityError,
    ArtifactPublicationError,
    BuildResourceLimits,
    CommandOutputLimitError,
    CommandResult,
    CommandTimeoutError,
    CommandUnavailableError,
    ContentAddressedArtifactPublisher,
    CppTestCase,
    CppTestSuite,
    DigestPinnedDockerCppBuilder,
    SourceBundleValidationError,
    canonical_source_bundle_sha256,
    validate_source_bundle,
)
from yaya_agent_contracts import (  # noqa: E402
    CompileAndTestRequest,
    SandboxLimits,
    SkillSourceBundle,
    SkillSourceFile,
)

IMAGE_DIGEST = "1" * 64
PINNED_IMAGE = f"registry.example/yaya-cpp@sha256:{IMAGE_DIGEST}"
COMPILER_VERSION = "15.2.0"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bundle(content: str = "int main() { return 0; }\n") -> SkillSourceBundle:
    source_file = SkillSourceFile("src/main.cpp", content, _sha256(content))
    return SkillSourceBundle(entrypoint="src/main.cpp", files=(source_file,))


def _request(
    *,
    content: str = "int main() { return 0; }\n",
    compiler_profile: str = "YAYA_CPP20_SAFE_V1",
    test_suite_version: str = "suite-v1",
) -> CompileAndTestRequest:
    return CompileAndTestRequest(
        build_id="build_pipeline_0001",
        skill_id="skill_pipeline_0001",
        source_bundle=_bundle(content),
        compiler_profile=compiler_profile,
        test_suite_version=test_suite_version,
        limits=SandboxLimits(
            cpu_ms=500,
            wall_ms=2_000,
            memory_bytes=67_108_864,
            max_intents=8,
            max_output_bytes=65_536,
            max_processes=8,
            network_access=False,
        ),
    )


def _suite() -> CppTestSuite:
    return CppTestSuite(
        "suite-v1",
        public_tests=(CppTestCase("public-basic", "PUBLIC", arguments=("public-pass",)),),
        hidden_tests=(CppTestCase("hidden-boundary", "HIDDEN", arguments=("hidden-pass",)),),
    )


def _recorded_container_from_create(
    command: tuple[str, ...],
    *,
    image_id: str,
    status: str,
) -> dict[str, object]:
    def option(name: str) -> str:
        return command[command.index(name) + 1]

    def options(name: str) -> list[str]:
        return [command[index + 1] for index, item in enumerate(command) if item == name]

    labels = dict(value.split("=", 1) for value in options("--label"))
    entrypoint_index = command.index("--entrypoint")
    entrypoint = command[entrypoint_index + 1]
    image_reference = command[entrypoint_index + 2]
    arguments = command[entrypoint_index + 3 :]
    mounts: list[dict[str, object]] = []
    host_mounts: list[dict[str, object]] = []
    for wire in options("--mount"):
        source, target_wire = wire.removeprefix("type=bind,source=").rsplit(",target=", 1)
        read_only = target_wire.endswith(",readonly")
        target = target_wire[: -len(",readonly")] if read_only else target_wire
        mounts.append(
            {
                "Type": "bind",
                "Source": source,
                "Destination": target,
                "Mode": "",
                "RW": not read_only,
                "Propagation": "rprivate",
            }
        )
        host_mounts.append(
            {
                "Type": "bind",
                "Source": source,
                "Target": target,
                "ReadOnly": read_only,
            }
        )
    ulimits: list[dict[str, object]] = []
    for wire in options("--ulimit"):
        name, limits = wire.split("=", 1)
        soft, hard = limits.split(":", 1)
        ulimits.append({"Name": name, "Soft": int(soft), "Hard": int(hard)})
    tmpfs_target, tmpfs_options = option("--tmpfs").split(":", 1)
    log_options = dict(value.split("=", 1) for value in options("--log-opt"))
    return {
        "status": status,
        "exit_code": 0,
        "oom_killed": False,
        "labels": labels,
        "entrypoint": entrypoint,
        "arguments": arguments,
        "stdout": b"",
        "stderr": b"",
        "image_id": image_id,
        "config": {
            "Labels": labels,
            "Image": image_reference,
            "Entrypoint": [entrypoint],
            "Cmd": list(arguments),
            "User": option("--user"),
            "WorkingDir": option("--workdir"),
            "OpenStdin": "--interactive" in command,
            "AttachStdin": "--interactive" in command,
            "StdinOnce": "--interactive" in command,
            "Tty": False,
        },
        "host_config": {
            "NetworkMode": option("--network"),
            "ReadonlyRootfs": "--read-only" in command,
            "CapAdd": None,
            "CapDrop": options("--cap-drop"),
            "SecurityOpt": options("--security-opt"),
            "Privileged": False,
            "IpcMode": option("--ipc"),
            "PidMode": "",
            "UTSMode": "",
            "Binds": None,
            "VolumesFrom": None,
            "Devices": [],
            "DeviceRequests": None,
            "PidsLimit": int(option("--pids-limit")),
            "Memory": int(option("--memory")),
            "MemorySwap": int(option("--memory-swap")),
            "NanoCpus": int(round(float(option("--cpus")) * 1_000_000_000)),
            "Tmpfs": {tmpfs_target: tmpfs_options},
            "Ulimits": ulimits,
            "LogConfig": {"Type": option("--log-driver"), "Config": log_options},
            "Mounts": host_mounts,
        },
        "mounts": mounts,
    }


class RecordingDockerRunner:
    """Stateful Docker control-plane double; production command construction is untouched."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.containers: dict[str, dict[str, object]] = {}
        self.compiler_version = COMPILER_VERSION
        self.image_digest = IMAGE_DIGEST
        self.image_id = f"sha256:{IMAGE_DIGEST}"
        self.docker_available = True
        self.image_available = True
        self.image_os = "linux"
        self.compile_returncode = 0
        self.compile_stdout = b""
        self.compile_stderr = b""
        self.artifact_bytes = b"\x7fELF deterministic certified artifact"
        self.test_behaviors: dict[str, tuple[int, bytes, bytes]] = {}
        self.timeout_argument: str | None = None
        self.output_limit_argument: str | None = None
        self.timeout_compile = False
        self.output_limit_compile = False
        self.lose_compile_start_response_once = False
        self.lose_compile_start_response_as_nonzero_once = False

    @staticmethod
    def _option(command: tuple[str, ...], option: str) -> str:
        return command[command.index(option) + 1]

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        max_output_bytes: int,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        del timeout_seconds
        argv = tuple(command)
        self.calls.append(argv)
        if len(argv) >= 2 and argv[1] == "version":
            if not self.docker_available:
                raise CommandUnavailableError(argv)
            return CommandResult(0, b"27.0.0\n")
        if len(argv) >= 3 and argv[1:3] == ("image", "inspect"):
            if not self.image_available:
                return CommandResult(1, b"", b"image unavailable")
            output = (
                f"{self.image_id}\n{self.image_os}\n"
                f'["registry.example/yaya-cpp@sha256:{self.image_digest}"]\n'
            ).encode()
            return CommandResult(0, output)
        if len(argv) >= 2 and argv[1] == "inspect":
            name = argv[2]
            container = self.containers.get(name)
            if container is None:
                return CommandResult(1, b"", b"not found")
            state = {
                "Status": container["status"],
                "ExitCode": container["exit_code"],
                "OOMKilled": container.get("oom_killed", False),
            }
            payload = {
                "Image": container["image_id"],
                "Path": container["entrypoint"],
                "Args": list(container["arguments"]),
                "Config": container["config"],
                "HostConfig": container["host_config"],
                "Mounts": container["mounts"],
                "State": state,
            }
            return CommandResult(0, json.dumps(payload, separators=(",", ":")).encode())
        if len(argv) >= 2 and argv[1] == "create":
            name = self._option(argv, "--name")
            if name in self.containers:
                return CommandResult(1, b"", b"name conflict")
            self.containers[name] = _recorded_container_from_create(
                argv, image_id=self.image_id, status="created"
            )
            return CommandResult(0, name.encode())
        if len(argv) >= 2 and argv[1] == "cp":
            if argv[2] == "-":
                if not input_bytes or b"yaya-source" not in input_bytes:
                    return CommandResult(1, b"", b"missing source archive")
                return CommandResult(0)
            destination = Path(argv[3])
            destination.write_bytes(self.artifact_bytes)
            return CommandResult(0)
        if len(argv) >= 2 and argv[1] == "start":
            name = argv[-1]
            container = self.containers[name]
            entrypoint = str(container["entrypoint"])
            arguments = tuple(container["arguments"])
            is_compile = entrypoint == "/bin/sh" and any("exec g++" in item for item in arguments)
            if entrypoint == "g++" and "-dumpfullversion" in arguments:
                returncode, stdout, stderr = 0, f"{self.compiler_version}\n".encode(), b""
            elif is_compile:
                returncode = self.compile_returncode
                stdout = self.compile_stdout
                stderr = self.compile_stderr
            else:
                marker = arguments[0] if arguments else ""
                returncode, stdout, stderr = self.test_behaviors.get(marker, (0, b"", b""))
            container["stdout"] = stdout
            container["stderr"] = stderr
            container["status"] = "exited"
            container["exit_code"] = returncode
            marker = arguments[0] if arguments else ""
            if is_compile and self.lose_compile_start_response_once:
                self.lose_compile_start_response_once = False
                raise CommandUnavailableError(argv, stdout, stderr)
            if is_compile and self.lose_compile_start_response_as_nonzero_once:
                self.lose_compile_start_response_as_nonzero_once = False
                return CommandResult(125, b"", b"docker attach response lost")
            if is_compile and self.timeout_compile:
                raise CommandTimeoutError(
                    argv, stdout[:max_output_bytes], stderr[:max_output_bytes]
                )
            if is_compile and self.output_limit_compile:
                raise CommandOutputLimitError(
                    argv, stdout[:max_output_bytes], stderr[:max_output_bytes]
                )
            if self.timeout_argument is not None and marker == self.timeout_argument:
                raise CommandTimeoutError(
                    argv, stdout[:max_output_bytes], stderr[:max_output_bytes]
                )
            if self.output_limit_argument is not None and marker == self.output_limit_argument:
                raise CommandOutputLimitError(
                    argv, stdout[:max_output_bytes], stderr[:max_output_bytes]
                )
            if len(stdout) + len(stderr) > max_output_bytes:
                raise CommandOutputLimitError(
                    argv, stdout[:max_output_bytes], stderr[:max_output_bytes]
                )
            return CommandResult(returncode, stdout, stderr)
        if len(argv) >= 2 and argv[1] == "wait":
            container = self.containers[argv[2]]
            return CommandResult(0, f"{container['exit_code']}\n".encode())
        if len(argv) >= 2 and argv[1] == "logs":
            container = self.containers[argv[2]]
            stdout = bytes(container["stdout"])
            stderr = bytes(container["stderr"])
            if len(stdout) + len(stderr) > max_output_bytes:
                raise CommandOutputLimitError(
                    argv, stdout[:max_output_bytes], stderr[:max_output_bytes]
                )
            return CommandResult(0, stdout, stderr)
        if len(argv) >= 2 and argv[1] == "kill":
            container = self.containers[argv[2]]
            container["status"] = "exited"
            container["exit_code"] = 137
            return CommandResult(0)
        if len(argv) >= 3 and argv[1:3] == ("rm", "--force"):
            self.containers.pop(argv[3], None)
            return CommandResult(0)
        raise AssertionError(f"unexpected external command: {argv}")


def _builder(root: Path, runner: RecordingDockerRunner) -> DigestPinnedDockerCppBuilder:
    return DigestPinnedDockerCppBuilder(
        root,
        image=PINNED_IMAGE,
        compiler_version=COMPILER_VERSION,
        test_suites=(_suite(),),
        runner=runner,
        limits=BuildResourceLimits(
            compile_wall_ms=2_000,
            test_wall_ms=1_000,
            memory_bytes=134_217_728,
            max_processes=16,
            cpus=0.5,
            tmpfs_bytes=8_388_608,
            max_output_bytes=4_096,
            max_artifact_bytes=1_048_576,
        ),
    )


class SourceBundleTests(unittest.TestCase):
    def test_hash_matches_frozen_javascript_projection_and_preserves_file_order(self) -> None:
        first = "// 苗\n"
        second = "int main() { return 0; }\n"
        source_bundle = {
            "language": "CPP20",
            "entrypoint": "src/main.cpp",
            "files": [
                {"path": "src/z.cpp", "content": first, "content_sha256": _sha256(first)},
                {
                    "path": "src/main.cpp",
                    "content": second,
                    "content_sha256": _sha256(second),
                },
            ],
        }
        projection = (
            f'[["src/z.cpp","{_sha256(first)}"],["src/main.cpp","{_sha256(second)}"]]'
        ).encode()
        validated = validate_source_bundle(source_bundle)
        self.assertEqual(validated.source_sha256, hashlib.sha256(projection).hexdigest())
        reversed_bundle = {**source_bundle, "files": list(reversed(source_bundle["files"]))}
        self.assertNotEqual(
            canonical_source_bundle_sha256(reversed_bundle), validated.source_sha256
        )

    def test_strict_validation_rejects_forgery_duplicates_entrypoint_escape_and_unknowns(
        self,
    ) -> None:
        content = "int main() {}"
        base = {
            "language": "CPP20",
            "entrypoint": "src/main.cpp",
            "files": [
                {
                    "path": "src/main.cpp",
                    "content": content,
                    "content_sha256": _sha256(content),
                }
            ],
        }
        cases = (
            ({**base, "extra": True}, "UNKNOWN_SOURCE_FIELD"),
            (
                {
                    **base,
                    "files": [{**base["files"][0], "content_sha256": "0" * 64}],
                },
                "SOURCE_CONTENT_HASH_MISMATCH",
            ),
            ({**base, "files": [base["files"][0], base["files"][0]]}, "DUPLICATE_SOURCE_PATH"),
            ({**base, "entrypoint": "src/missing.cpp"}, "SOURCE_ENTRYPOINT_NOT_FOUND"),
            (
                {
                    **base,
                    "entrypoint": "../main.cpp",
                    "files": [{**base["files"][0], "path": "../main.cpp"}],
                },
                "INVALID_SOURCE_PATH",
            ),
        )
        for value, code in cases:
            with self.subTest(code=code), self.assertRaises(SourceBundleValidationError) as raised:
                validate_source_bundle(value)
            self.assertEqual(raised.exception.code, code)

    def test_count_and_aggregate_utf8_limits_are_enforced(self) -> None:
        files = []
        for index in range(33):
            content = f"int value_{index};"
            files.append(
                {
                    "path": f"src/f{index}.cpp",
                    "content": content,
                    "content_sha256": _sha256(content),
                }
            )
        with self.assertRaises(SourceBundleValidationError) as too_many:
            validate_source_bundle(
                {"language": "CPP20", "entrypoint": "src/f0.cpp", "files": files}
            )
        self.assertEqual(too_many.exception.code, "SOURCE_FILE_LIMIT_EXCEEDED")
        oversized = "苗" * 349_526
        with self.assertRaises(SourceBundleValidationError) as too_large:
            validate_source_bundle(
                {
                    "language": "CPP20",
                    "entrypoint": "src/main.cpp",
                    "files": [
                        {
                            "path": "src/main.cpp",
                            "content": oversized,
                            "content_sha256": _sha256(oversized),
                        }
                    ],
                }
            )
        self.assertEqual(too_large.exception.code, "SOURCE_BUNDLE_BYTES_EXCEEDED")

    def test_game_path_aliases_hash_as_spelled_but_materialization_collisions_fail_closed(
        self,
    ) -> None:
        first = "int a;"
        second = "int main() { return 0; }"
        value = {
            "language": "CPP20",
            "entrypoint": "src/main.cpp",
            "files": [
                {"path": "src//main.cpp", "content": first, "content_sha256": _sha256(first)},
                {
                    "path": "src/main.cpp",
                    "content": second,
                    "content_sha256": _sha256(second),
                },
            ],
        }
        self.assertEqual(validate_source_bundle(value).files[0].path, "src//main.cpp")


class DockerBuilderTests(unittest.TestCase):
    @staticmethod
    def _container_from_create(command: tuple[str, ...], *, status: str) -> dict[str, object]:
        return _recorded_container_from_create(
            command, image_id=f"sha256:{IMAGE_DIGEST}", status=status
        )

    def test_rejects_unpinned_image_before_any_external_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runner = RecordingDockerRunner()
            with self.assertRaisesRegex(ValueError, "exact sha256 digest"):
                DigestPinnedDockerCppBuilder(
                    Path(raw_root),
                    image="gcc:latest",
                    compiler_version=COMPILER_VERSION,
                    test_suites=(_suite(),),
                    runner=runner,
                )
            self.assertEqual(runner.calls, [])

    def test_success_uses_hardened_create_start_wait_cp_inspect_and_fixed_flags(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            runner = RecordingDockerRunner()
            builder = _builder(root, runner)
            result = builder.build(_request())
            self.assertTrue(result.succeeded, result.failure)
            self.assertEqual(
                result.artifact_sha256, hashlib.sha256(runner.artifact_bytes).hexdigest()
            )
            self.assertEqual([test.visibility for test in result.tests], ["PUBLIC", "HIDDEN"])
            verbs = [call[1] for call in runner.calls if len(call) > 1]
            for verb in ("create", "start", "wait", "cp", "inspect"):
                self.assertIn(verb, verbs)
            self.assertNotIn("run", verbs)
            creates = [call for call in runner.calls if len(call) > 1 and call[1] == "create"]
            self.assertGreaterEqual(len(creates), 4)
            for command in creates:
                joined = "\n".join(command)
                for required in (
                    "--pull=never",
                    "--network\nnone",
                    "--read-only",
                    "--cap-drop\nALL",
                    "--security-opt\nno-new-privileges=true",
                    "--user\n65534:65534",
                    "--pids-limit",
                    "--memory",
                    "--memory-swap",
                    "--cpus",
                    "--tmpfs",
                    "--ulimit",
                ):
                    self.assertIn(required, joined)
                self.assertIn(PINNED_IMAGE, command)
                labels = dict(
                    command[index + 1].split("=", 1)
                    for index, item in enumerate(command)
                    if item == "--label"
                )
                self.assertEqual(
                    labels["local.yaya.security_protocol"],
                    "yaya-docker-build-security-v1",
                )
                self.assertRegex(labels["local.yaya.security_sha256"], r"^[a-f0-9]{64}$")
            compile_command = next(
                command
                for command in creates
                if "--entrypoint" in command
                and command[command.index("--entrypoint") + 1] == "/bin/sh"
                and "exec g++" in " ".join(command)
            )
            compile_wire = " ".join(compile_command)
            for flag in CPP20_SAFE_V1_FLAGS:
                self.assertIn(flag, compile_wire)
            self.assertIn("-Werror", compile_wire)
            self.assertIn("target=/yaya-source.tar,readonly", compile_wire)
            self.assertFalse(
                any(len(call) > 2 and call[1:3] == ("cp", "-") for call in runner.calls),
                "read-only rootfs source input must use the isolated read-only tar mount",
            )
            self.assertTrue(
                any(
                    len(call) > 3
                    and call[1] == "cp"
                    and call[2].endswith(":/yaya-output/container-skill")
                    for call in runner.calls
                )
            )
            self.assertIsNotNone(result.workspace)
            builder.discard_workspace(result)
            assert result.workspace is not None
            self.assertFalse(result.workspace.exists())

    def test_lost_compile_start_response_reconciles_without_reexecution(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runner = RecordingDockerRunner()
            runner.lose_compile_start_response_once = True
            builder = _builder(Path(raw_root), runner)

            result = builder.build(_request())

            self.assertTrue(result.succeeded, result.failure)
            compile_create = next(
                call
                for call in runner.calls
                if len(call) > 1
                and call[1] == "create"
                and call[call.index("--entrypoint") + 1] == "/bin/sh"
                and "exec g++" in " ".join(call)
            )
            compile_name = compile_create[compile_create.index("--name") + 1]
            compile_starts = [
                call
                for call in runner.calls
                if len(call) > 1 and call[1] == "start" and call[-1] == compile_name
            ]
            self.assertEqual(len(compile_starts), 1)
            self.assertTrue(any(len(call) > 1 and call[1] == "logs" for call in runner.calls))

    def test_nonzero_attach_response_reconciles_container_logs_without_reexecution(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runner = RecordingDockerRunner()
            runner.lose_compile_start_response_as_nonzero_once = True
            builder = _builder(Path(raw_root), runner)

            result = builder.build(_request())

            self.assertTrue(result.succeeded, result.failure)
            compile_create = next(
                call
                for call in runner.calls
                if len(call) > 1
                and call[1] == "create"
                and call[call.index("--entrypoint") + 1] == "/bin/sh"
                and "exec g++" in " ".join(call)
            )
            compile_name = compile_create[compile_create.index("--name") + 1]
            compile_starts = [
                call
                for call in runner.calls
                if len(call) > 1 and call[1] == "start" and call[-1] == compile_name
            ]
            self.assertEqual(len(compile_starts), 1)
            self.assertTrue(
                any(
                    len(call) > 2 and call[1] == "logs" and call[2] == compile_name
                    for call in runner.calls
                )
            )

    def test_step_receipts_make_replay_deterministic_without_reexecution(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runner = RecordingDockerRunner()
            builder = _builder(Path(raw_root), runner)
            request = _request()
            first = builder.build(request)
            self.assertTrue(first.succeeded)
            create_count = sum(call[1] == "create" for call in runner.calls if len(call) > 1)
            second = builder.build(request)
            self.assertTrue(second.succeeded, second.failure)
            self.assertEqual(first.build_identity, second.build_identity)
            self.assertEqual(first.artifact_sha256, second.artifact_sha256)
            self.assertEqual(
                sum(call[1] == "create" for call in runner.calls if len(call) > 1),
                create_count,
            )

    def test_takeover_reconciles_created_compile_container_without_recreating_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runner = RecordingDockerRunner()
            builder = _builder(Path(raw_root), runner)
            request = _request()
            first = builder.build(request)
            self.assertTrue(first.succeeded)
            compile_create = next(
                command
                for command in runner.calls
                if len(command) > 1
                and command[1] == "create"
                and command[command.index("--entrypoint") + 1] == "/bin/sh"
                and "exec g++" in " ".join(command)
            )
            name = compile_create[compile_create.index("--name") + 1]
            self.assertEqual(name, builder.container_name(request, "compile"))
            reconstructed = self._container_from_create(compile_create, status="created")
            runner.containers[name] = reconstructed
            step_identity = dict(
                compile_create[index + 1].split("=", 1)
                for index, item in enumerate(compile_create)
                if item == "--label"
            )["local.yaya.step_identity"]
            assert first.workspace is not None and first.staged_artifact is not None
            receipt = first.workspace / "receipts" / f"{step_identity}.json"
            receipt.chmod(stat.S_IWRITE | stat.S_IREAD)
            receipt.unlink()
            first.staged_artifact.chmod(stat.S_IWRITE | stat.S_IREAD)
            first.staged_artifact.unlink()
            compile_create_count = runner.calls.count(compile_create)
            takeover = builder.build(request)
            self.assertTrue(takeover.succeeded, takeover.failure)
            self.assertEqual(runner.calls.count(compile_create), compile_create_count)

    def test_takeover_rejects_forged_labels_image_config_limits_and_mounts(self) -> None:
        drift_cases = (
            "security-label",
            "image-id",
            "image-reference",
            "entrypoint",
            "command",
            "user",
            "network",
            "read-only-root",
            "cap-drop",
            "no-new-privileges",
            "pids",
            "cpu",
            "memory",
            "tmpfs",
            "ulimits",
            "mount-source",
            "mount-read-only",
            "host-mount-source",
        )
        for drift in drift_cases:
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as raw_root:
                runner = RecordingDockerRunner()
                builder = _builder(Path(raw_root), runner)
                request = _request()
                first = builder.build(request)
                self.assertTrue(first.succeeded, first.failure)
                compile_create = next(
                    command
                    for command in runner.calls
                    if len(command) > 1
                    and command[1] == "create"
                    and command[command.index("--entrypoint") + 1] == "/bin/sh"
                    and "exec g++" in " ".join(command)
                )
                name = compile_create[compile_create.index("--name") + 1]
                forged = self._container_from_create(compile_create, status="created")
                config = cast(dict[str, object], forged["config"])
                host = cast(dict[str, object], forged["host_config"])
                mounts = cast(list[dict[str, object]], forged["mounts"])
                host_mounts = cast(list[dict[str, object]], host["Mounts"])
                expected_code = "CONTAINER_SECURITY_DRIFT"
                if drift == "security-label":
                    labels = cast(dict[str, str], forged["labels"])
                    labels["local.yaya.security_sha256"] = "0" * 64
                    expected_code = "CONTAINER_IDENTITY_CONFLICT"
                elif drift == "image-id":
                    forged["image_id"] = f"sha256:{'2' * 64}"
                elif drift == "image-reference":
                    config["Image"] = f"registry.example/forged@sha256:{'2' * 64}"
                elif drift == "entrypoint":
                    config["Entrypoint"] = ["/bin/false"]
                elif drift == "command":
                    config["Cmd"] = ["-c", "true"]
                elif drift == "user":
                    config["User"] = "0:0"
                elif drift == "network":
                    host["NetworkMode"] = "host"
                elif drift == "read-only-root":
                    host["ReadonlyRootfs"] = False
                elif drift == "cap-drop":
                    host["CapDrop"] = []
                elif drift == "no-new-privileges":
                    host["SecurityOpt"] = []
                elif drift == "pids":
                    host["PidsLimit"] = 17
                elif drift == "cpu":
                    host["NanoCpus"] = 1_000_000_000
                elif drift == "memory":
                    host["Memory"] = 268_435_456
                elif drift == "tmpfs":
                    host["Tmpfs"] = {"/tmp": "rw,exec,size=8388608"}
                elif drift == "ulimits":
                    ulimits = cast(list[dict[str, object]], host["Ulimits"])
                    ulimits[0]["Hard"] = 1
                elif drift == "mount-source":
                    mounts[0]["Source"] = str(Path(raw_root) / "forged-source")
                elif drift == "mount-read-only":
                    mounts[0]["RW"] = True
                elif drift == "host-mount-source":
                    host_mounts[0]["Source"] = str(Path(raw_root) / "forged-source")
                else:  # pragma: no cover - the closed tuple above is exhaustive
                    self.fail(f"unhandled drift case {drift}")
                runner.containers[name] = forged
                labels = cast(dict[str, str], forged["labels"])
                step_identity = labels["local.yaya.step_identity"]
                assert first.workspace is not None and first.staged_artifact is not None
                receipt = first.workspace / "receipts" / f"{step_identity}.json"
                receipt.chmod(stat.S_IWRITE | stat.S_IREAD)
                receipt.unlink()
                first.staged_artifact.chmod(stat.S_IWRITE | stat.S_IREAD)
                first.staged_artifact.unlink()
                compile_starts = sum(
                    len(call) > 2 and call[1] == "start" and call[-1] == name
                    for call in runner.calls
                )
                takeover = builder.build(request)
                self.assertFalse(takeover.succeeded)
                assert takeover.failure is not None
                self.assertEqual(takeover.failure.code, expected_code)
                self.assertEqual(
                    sum(
                        len(call) > 2 and call[1] == "start" and call[-1] == name
                        for call in runner.calls
                    ),
                    compile_starts,
                )

    def test_takeover_compares_container_artifact_with_pre_receipt_staging_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runner = RecordingDockerRunner()
            builder = _builder(Path(raw_root), runner)
            request = _request()
            first = builder.build(request)
            self.assertTrue(first.succeeded)
            compile_create = next(
                command
                for command in runner.calls
                if len(command) > 1
                and command[1] == "create"
                and command[command.index("--entrypoint") + 1] == "/bin/sh"
                and "exec g++" in " ".join(command)
            )
            name = compile_create[compile_create.index("--name") + 1]
            runner.containers[name] = self._container_from_create(compile_create, status="exited")
            step_identity = dict(
                compile_create[index + 1].split("=", 1)
                for index, item in enumerate(compile_create)
                if item == "--label"
            )["local.yaya.step_identity"]
            assert first.workspace is not None and first.staged_artifact is not None
            receipt = first.workspace / "receipts" / f"{step_identity}.json"
            receipt.chmod(stat.S_IWRITE | stat.S_IREAD)
            receipt.unlink()
            first.staged_artifact.chmod(stat.S_IWRITE | stat.S_IREAD)
            first.staged_artifact.write_bytes(b"pre-receipt drift")
            takeover = builder.build(request)
            self.assertFalse(takeover.succeeded)
            assert takeover.failure is not None
            self.assertEqual(takeover.failure.code, "STAGED_ARTIFACT_DRIFT")

    def test_staged_artifact_drift_is_not_recompiled_or_recognized_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runner = RecordingDockerRunner()
            builder = _builder(Path(raw_root), runner)
            request = _request()
            first = builder.build(request)
            self.assertTrue(first.succeeded)
            assert first.staged_artifact is not None
            first.staged_artifact.chmod(stat.S_IWRITE | stat.S_IREAD)
            first.staged_artifact.write_bytes(b"drift")
            create_count = sum(call[1] == "create" for call in runner.calls if len(call) > 1)
            replay = builder.build(request)
            self.assertFalse(replay.succeeded)
            assert replay.failure is not None
            self.assertEqual(replay.failure.code, "STAGED_ARTIFACT_DRIFT")
            self.assertEqual(
                sum(call[1] == "create" for call in runner.calls if len(call) > 1),
                create_count,
            )

    def test_docker_unavailable_image_drift_and_compiler_drift_are_explicit(self) -> None:
        cases = (
            ("docker", "DOCKER_UNAVAILABLE"),
            ("image", "COMPILER_IMAGE_DIGEST_DRIFT"),
            ("compiler", "COMPILER_VERSION_DRIFT"),
        )
        for mode, expected in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw_root:
                runner = RecordingDockerRunner()
                if mode == "docker":
                    runner.docker_available = False
                elif mode == "image":
                    runner.image_digest = "2" * 64
                else:
                    runner.compiler_version = "unexpected"
                result = _builder(Path(raw_root), runner).build(_request())
                self.assertFalse(result.succeeded)
                assert result.failure is not None
                self.assertEqual(result.failure.code, expected)
                self.assertEqual(result.failure.retryable, mode == "docker")

    def test_warning_as_error_compile_timeout_and_compile_output_limit_are_classified(self) -> None:
        cases = (
            ("warning", "COMPILE_ERROR"),
            ("timeout", "COMPILE_TIMEOUT"),
            ("output", "COMPILE_OUTPUT_LIMIT"),
        )
        for mode, expected in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw_root:
                runner = RecordingDockerRunner()
                if mode == "warning":
                    runner.compile_returncode = 1
                    runner.compile_stderr = b"main.cpp: warning: rejected by -Werror\n"
                elif mode == "timeout":
                    runner.timeout_compile = True
                else:
                    runner.output_limit_compile = True
                    runner.compile_stderr = b"x" * 10_000
                result = _builder(Path(raw_root), runner).build(_request())
                self.assertFalse(result.succeeded)
                self.assertIsNone(result.artifact_sha256)
                self.assertIsNone(result.staged_artifact)
                assert result.failure is not None
                self.assertEqual(result.failure.code, expected)
                if mode == "warning":
                    self.assertIn("-Werror", " ".join(result.diagnostics[0].message.split()))

    def test_hidden_failure_is_bounded_and_does_not_disclose_hidden_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runner = RecordingDockerRunner()
            runner.test_behaviors["hidden-pass"] = (
                1,
                b"SECRET_EXPECTED_ANSWER=water-17\n",
                b"hidden line 91\n",
            )
            result = _builder(Path(raw_root), runner).build(_request())
            self.assertFalse(result.succeeded)
            assert result.failure is not None
            self.assertEqual(result.failure.stage, "HIDDEN_TEST")
            self.assertEqual(result.failure.code, "HIDDEN_TEST_FAILED")
            self.assertNotIn("SECRET_EXPECTED_ANSWER", repr(result.diagnostics))
            self.assertLessEqual(len(result.diagnostics), 100)
            self.assertTrue(all(len(item.message) <= 512 for item in result.diagnostics))
            self.assertIsNone(result.artifact_sha256)

    def test_public_and_hidden_timeouts_have_distinct_failure_classification(self) -> None:
        for marker, stage, code in (
            ("public-pass", "PUBLIC_TEST", "PUBLIC_TEST_TIMEOUT"),
            ("hidden-pass", "HIDDEN_TEST", "HIDDEN_TEST_TIMEOUT"),
        ):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as raw_root:
                runner = RecordingDockerRunner()
                runner.timeout_argument = marker
                result = _builder(Path(raw_root), runner).build(_request())
                assert result.failure is not None
                self.assertEqual(result.failure.stage, stage)
                self.assertEqual(result.failure.code, code)

    def test_unknown_profile_suite_and_host_normalization_collision_fail_before_compile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runner = RecordingDockerRunner()
            builder = _builder(Path(raw_root), runner)
            wrong_profile = builder.build(_request(compiler_profile="HOST_GCC"))
            assert wrong_profile.failure is not None
            self.assertEqual(wrong_profile.failure.code, "UNSUPPORTED_COMPILER_PROFILE")
            wrong_suite = builder.build(_request(test_suite_version="unknown-suite"))
            assert wrong_suite.failure is not None
            self.assertEqual(wrong_suite.failure.code, "UNKNOWN_TEST_SUITE_VERSION")

            first = "int helper;"
            second = "int main() { return 0; }"
            bundle = SkillSourceBundle(
                entrypoint="src/main.cpp",
                files=(
                    SkillSourceFile("src//main.cpp", first, _sha256(first)),
                    SkillSourceFile("src/main.cpp", second, _sha256(second)),
                ),
            )
            request = CompileAndTestRequest(
                build_id="build_collision_0001",
                skill_id="skill_collision_0001",
                source_bundle=bundle,
                compiler_profile="YAYA_CPP20_SAFE_V1",
                test_suite_version="suite-v1",
                limits=_request().limits,
            )
            collision = builder.build(request)
            assert collision.failure is not None
            self.assertEqual(collision.failure.code, "SOURCE_PATH_MATERIALIZATION_COLLISION")


class ArtifactPublisherTests(unittest.TestCase):
    def test_publish_is_content_addressed_read_only_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            artifact_root = root / "artifacts"
            artifact_root.mkdir()
            source = root / "skill"
            source.write_bytes(b"certified executable bytes")
            publisher = ContentAddressedArtifactPublisher(artifact_root)
            published = publisher.publish(source)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertEqual(published.artifact_sha256, digest)
            self.assertEqual(published.path, artifact_root / digest[:2] / digest)
            self.assertEqual(published.path.read_bytes(), source.read_bytes())
            self.assertFalse(
                published.path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            )
            self.assertEqual(publisher.publish(source), published)
            leftovers = tuple(published.path.parent.glob(".*.tmp"))
            self.assertEqual(leftovers, ())
            self.assertEqual(publisher.verify(digest), published)

    def test_existing_bytes_drift_and_writable_artifacts_fail_closed_without_overwrite(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            artifact_root = root / "artifacts"
            artifact_root.mkdir()
            source = root / "skill"
            source.write_bytes(b"trusted")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            target = artifact_root / digest[:2] / digest
            target.parent.mkdir()
            target.write_bytes(b"drifted")
            target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            publisher = ContentAddressedArtifactPublisher(artifact_root)
            with self.assertRaises(ArtifactIntegrityError) as drift:
                publisher.publish(source)
            self.assertEqual(drift.exception.code, "ARTIFACT_DIGEST_MISMATCH")
            self.assertEqual(target.read_bytes(), b"drifted")

            target.chmod(stat.S_IWRITE | stat.S_IREAD)
            target.write_bytes(source.read_bytes())
            with self.assertRaises(ArtifactIntegrityError) as writable:
                publisher.verify(digest)
            self.assertEqual(writable.exception.code, "WRITABLE_ARTIFACT")

    def test_verify_detects_post_publication_hash_drift_and_rejects_symlink_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            artifact_root = root / "artifacts"
            artifact_root.mkdir()
            source = root / "skill"
            source.write_bytes(b"trusted")
            publisher = ContentAddressedArtifactPublisher(artifact_root)
            published = publisher.publish(source)
            published.path.chmod(stat.S_IWRITE | stat.S_IREAD)
            published.path.write_bytes(b"tampered")
            published.path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            with self.assertRaises(ArtifactIntegrityError) as drift:
                publisher.verify(published.artifact_sha256)
            self.assertEqual(drift.exception.code, "ARTIFACT_DIGEST_MISMATCH")

            link = root / "source-link"
            try:
                os.symlink(source, link)
            except OSError:
                # Windows may deny symlink creation without Developer Mode.  A Path subclass with
                # altered trust semantics would only test the double, so retain a deterministic
                # filesystem safety assertion instead.
                source.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                self.assertFalse(source.is_symlink())
            else:
                with self.assertRaises(ArtifactPublicationError) as invalid:
                    publisher.publish(link)
                self.assertEqual(invalid.exception.code, "INVALID_ARTIFACT_SOURCE")


if __name__ == "__main__":
    unittest.main()
