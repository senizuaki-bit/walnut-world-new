"""Provider-neutral pinned Docker C++ build and artifact publication primitives.

This module deliberately has no HTTP, database, worker, or runtime-Sandbox wiring.  It is the
production adapter boundary those layers can call: source bytes are validated independently of a
Product draft, every external command goes through an injectable runner, and no host compiler
fallback exists.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, cast

from yaya_agent_contracts import CompileAndTestRequest, SkillSourceBundle

MAX_SOURCE_FILES = 32
MAX_SOURCE_BYTES = 1_048_576
MAX_DIAGNOSTICS = 100
MAX_DIAGNOSTIC_CHARS = 512
CPP20_SAFE_V1_PROFILE = "YAYA_CPP20_SAFE_V1"
CPP20_SAFE_V1_FLAGS = (
    "-std=c++20",
    "-O2",
    "-pipe",
    "-Wall",
    "-Wextra",
    "-Wpedantic",
    "-Werror",
    "-Wconversion",
    "-Wshadow",
    "-fstack-protector-strong",
    "-D_FORTIFY_SOURCE=2",
    "-Wl,-z,relro,-z,now",
)

_DOCKER_SECURITY_PROTOCOL = "yaya-docker-build-security-v1"
_DOCKER_USER = "65534:65534"
_DOCKER_WORKDIR = "/tmp"
_DOCKER_CAP_DROP = ("ALL",)
_DOCKER_SECURITY_OPTIONS = ("no-new-privileges=true",)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_SOURCE_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9_.\/-]{1,240}$")
_ENTRYPOINT = re.compile(r"^[A-Za-z0-9_.\/-]{1,240}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PINNED_IMAGE = re.compile(r"^[a-z0-9./:_-]+@sha256:[a-f0-9]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
_TEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SourceBundleValidationError(ValueError):
    """A frozen Game ``SkillSourceBundle`` invariant was not satisfied."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ArtifactPublicationError(RuntimeError):
    """Artifact publication could not complete without an overwrite or trust downgrade."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ArtifactIntegrityError(ArtifactPublicationError):
    """An existing content-addressed artifact failed verification."""


@dataclass(frozen=True, slots=True)
class ValidatedSourceFile:
    path: str
    content: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedSourceBundle:
    language: Literal["CPP20"]
    entrypoint: str
    files: tuple[ValidatedSourceFile, ...]
    source_sha256: str
    total_utf8_bytes: int


def _unicode_scalar_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise SourceBundleValidationError("INVALID_SOURCE_TYPE", f"{field} must be a string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise SourceBundleValidationError(
            "INVALID_SOURCE_UNICODE", f"{field} must contain only Unicode scalar values"
        )
    return value


def _exact_keys(value: Mapping[object, object], expected: set[str], field: str) -> None:
    keys = set(value)
    if keys != expected:
        raise SourceBundleValidationError(
            "UNKNOWN_SOURCE_FIELD",
            f"{field} must contain exactly {', '.join(sorted(expected))}",
        )


def _bundle_projection(bundle: SkillSourceBundle | Mapping[str, object]) -> Mapping[str, object]:
    if isinstance(bundle, SkillSourceBundle):
        return {
            "language": bundle.language,
            "entrypoint": bundle.entrypoint,
            "files": tuple(
                {
                    "path": source_file.path,
                    "content": source_file.content,
                    "content_sha256": source_file.content_sha256,
                }
                for source_file in bundle.files
            ),
        }
    if not isinstance(bundle, Mapping):
        raise SourceBundleValidationError("INVALID_SOURCE_TYPE", "source_bundle must be an object")
    _exact_keys(
        cast(Mapping[object, object], bundle), {"language", "entrypoint", "files"}, "source_bundle"
    )
    return bundle


def validate_source_bundle(
    bundle: SkillSourceBundle | Mapping[str, object],
) -> ValidatedSourceBundle:
    """Validate the frozen Game bundle and calculate its server-authoritative source hash.

    The source hash intentionally matches the existing frozen JavaScript build semantics:
    SHA-256 of compact UTF-8 JSON for ``files.map(file => [path, content_sha256])``.  File order is
    therefore significant; host path normalization and object-key ordering never participate.
    """

    value = _bundle_projection(bundle)
    language = _unicode_scalar_text(value.get("language"), "source_bundle.language")
    if language != "CPP20":
        raise SourceBundleValidationError(
            "UNSUPPORTED_SOURCE_LANGUAGE", "source_bundle.language must be CPP20"
        )
    entrypoint = _unicode_scalar_text(value.get("entrypoint"), "source_bundle.entrypoint")
    if _ENTRYPOINT.fullmatch(entrypoint) is None:
        raise SourceBundleValidationError(
            "INVALID_SOURCE_ENTRYPOINT", "source_bundle.entrypoint is not a frozen Game path"
        )
    raw_files = value.get("files")
    if isinstance(raw_files, (str, bytes, bytearray)) or not isinstance(raw_files, Sequence):
        raise SourceBundleValidationError(
            "INVALID_SOURCE_TYPE", "source_bundle.files must be an array"
        )
    source_items = cast(Sequence[object], raw_files)
    if not 1 <= len(source_items) <= MAX_SOURCE_FILES:
        raise SourceBundleValidationError(
            "SOURCE_FILE_LIMIT_EXCEEDED",
            f"source_bundle.files must contain between 1 and {MAX_SOURCE_FILES} files",
        )

    files: list[ValidatedSourceFile] = []
    seen_paths: set[str] = set()
    total_utf8_bytes = 0
    entrypoint_matches = 0
    for index, raw_file in enumerate(source_items):
        label = f"source_bundle.files[{index}]"
        if not isinstance(raw_file, Mapping):
            raise SourceBundleValidationError("INVALID_SOURCE_TYPE", f"{label} must be an object")
        source_mapping = cast(Mapping[str, object], raw_file)
        _exact_keys(
            cast(Mapping[object, object], source_mapping),
            {"path", "content", "content_sha256"},
            label,
        )
        path = _unicode_scalar_text(source_mapping.get("path"), f"{label}.path")
        if _SOURCE_PATH.fullmatch(path) is None:
            raise SourceBundleValidationError(
                "INVALID_SOURCE_PATH", f"{label}.path is not a frozen Game source path"
            )
        if path in seen_paths:
            raise SourceBundleValidationError(
                "DUPLICATE_SOURCE_PATH", f"source path {path!r} occurs more than once"
            )
        seen_paths.add(path)
        if path == entrypoint:
            entrypoint_matches += 1
        content = _unicode_scalar_text(source_mapping.get("content"), f"{label}.content")
        if len(content) > MAX_SOURCE_BYTES:
            raise SourceBundleValidationError(
                "SOURCE_FILE_LENGTH_EXCEEDED", f"{label}.content exceeds the schema limit"
            )
        encoded_content = content.encode("utf-8")
        total_utf8_bytes += len(encoded_content)
        if total_utf8_bytes > MAX_SOURCE_BYTES:
            raise SourceBundleValidationError(
                "SOURCE_BUNDLE_BYTES_EXCEEDED",
                f"source bundle exceeds {MAX_SOURCE_BYTES} UTF-8 bytes",
            )
        content_sha256 = _unicode_scalar_text(
            source_mapping.get("content_sha256"), f"{label}.content_sha256"
        )
        if _SHA256.fullmatch(content_sha256) is None:
            raise SourceBundleValidationError(
                "INVALID_SOURCE_HASH", f"{label}.content_sha256 must be lowercase SHA-256"
            )
        if hashlib.sha256(encoded_content).hexdigest() != content_sha256:
            raise SourceBundleValidationError(
                "SOURCE_CONTENT_HASH_MISMATCH",
                f"{label}.content_sha256 does not match its UTF-8 content",
            )
        files.append(ValidatedSourceFile(path, content, content_sha256))

    if entrypoint_matches != 1:
        raise SourceBundleValidationError(
            "SOURCE_ENTRYPOINT_NOT_FOUND",
            "source_bundle.entrypoint must identify exactly one source file",
        )
    hash_projection = tuple((source_file.path, source_file.content_sha256) for source_file in files)
    canonical_bytes = json.dumps(hash_projection, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return ValidatedSourceBundle(
        language="CPP20",
        entrypoint=entrypoint,
        files=tuple(files),
        source_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        total_utf8_bytes=total_utf8_bytes,
    )


def canonical_source_bundle_sha256(
    bundle: SkillSourceBundle | Mapping[str, object],
) -> str:
    """Return the server-authoritative source hash after full validation."""

    return validate_source_bundle(bundle).source_sha256


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class CommandExecutionError(RuntimeError):
    def __init__(self, command: Sequence[str], stdout: bytes = b"", stderr: bytes = b"") -> None:
        super().__init__(type(self).__name__)
        self.command = tuple(command)
        self.stdout = stdout
        self.stderr = stderr


class CommandUnavailableError(CommandExecutionError):
    pass


class CommandTimeoutError(CommandExecutionError):
    pass


class CommandOutputLimitError(CommandExecutionError):
    pass


class CommandRunner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        max_output_bytes: int,
        input_bytes: bytes | None = None,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    """Run a command without a shell while bounding combined stdout and stderr."""

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        max_output_bytes: int,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        argv = tuple(command)
        if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
            raise ValueError("command must contain NUL-free string arguments")
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as error:
            raise CommandUnavailableError(argv) from error
        if process.stdout is None or process.stderr is None:
            process.kill()
            raise CommandUnavailableError(argv)

        stdout = bytearray()
        stderr = bytearray()
        output_lock = threading.Lock()
        overflow = threading.Event()
        total_output = 0

        def drain(stream: BinaryIO, target: bytearray) -> None:
            nonlocal total_output
            while chunk := stream.read(4096):
                with output_lock:
                    remaining = max_output_bytes - total_output
                    accepted = chunk[: max(0, remaining)]
                    target.extend(accepted)
                    total_output += len(accepted)
                    exceeded = len(accepted) != len(chunk)
                if exceeded:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return

        readers = (
            threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
        )
        for reader in readers:
            reader.start()
        try:
            if input_bytes is not None and process.stdin is not None:
                try:
                    process.stdin.write(input_bytes)
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait()
                for reader in readers:
                    reader.join(timeout=5)
                raise CommandTimeoutError(argv, bytes(stdout), bytes(stderr)) from error
            for reader in readers:
                reader.join(timeout=5)
            if any(reader.is_alive() for reader in readers):
                process.kill()
                process.wait()
                for reader in readers:
                    reader.join(timeout=5)
                raise CommandUnavailableError(argv, bytes(stdout), bytes(stderr))
            if overflow.is_set():
                raise CommandOutputLimitError(argv, bytes(stdout), bytes(stderr))
            return CommandResult(returncode, bytes(stdout), bytes(stderr))
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            process.stdout.close()
            process.stderr.close()


@dataclass(frozen=True, slots=True)
class BuildResourceLimits:
    compile_wall_ms: int = 120_000
    test_wall_ms: int = 15_000
    memory_bytes: int = 536_870_912
    max_processes: int = 64
    cpus: float = 1.0
    tmpfs_bytes: int = 67_108_864
    max_output_bytes: int = 65_536
    max_artifact_bytes: int = 16_777_216

    def __post_init__(self) -> None:
        for name in (
            "compile_wall_ms",
            "test_wall_ms",
            "memory_bytes",
            "max_processes",
            "tmpfs_bytes",
            "max_output_bytes",
            "max_artifact_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.cpus, bool) or not isinstance(self.cpus, (int, float)):
            raise TypeError("cpus must be numeric")
        if not math.isfinite(float(self.cpus)) or not 0.01 <= float(self.cpus) <= 64.0:
            raise ValueError("cpus must be between 0.01 and 64")


@dataclass(frozen=True, slots=True)
class CppTestCase:
    test_case_id: str
    visibility: Literal["PUBLIC", "HIDDEN"]
    arguments: tuple[str, ...] = ()
    stdin: bytes = b""
    expected_stdout_sha256: str | None = None

    def __post_init__(self) -> None:
        if _TEST_ID.fullmatch(self.test_case_id) is None:
            raise ValueError("test_case_id is not a canonical test identifier")
        if self.visibility not in ("PUBLIC", "HIDDEN"):
            raise ValueError("visibility must be PUBLIC or HIDDEN")
        arguments = tuple(self.arguments)
        if len(arguments) > 32:
            raise ValueError("test arguments must contain at most 32 items")
        for argument in arguments:
            if not isinstance(argument, str) or "\x00" in argument or len(argument) > 1024:
                raise ValueError("test arguments must be bounded NUL-free strings")
        if not isinstance(self.stdin, bytes) or len(self.stdin) > 65_536:
            raise ValueError("test stdin must be at most 65536 bytes")
        if self.expected_stdout_sha256 is not None and (
            _SHA256.fullmatch(self.expected_stdout_sha256) is None
        ):
            raise ValueError("expected_stdout_sha256 must be lowercase SHA-256")
        object.__setattr__(self, "arguments", arguments)


@dataclass(frozen=True, slots=True)
class CppTestSuite:
    version: str
    public_tests: tuple[CppTestCase, ...]
    hidden_tests: tuple[CppTestCase, ...]

    def __post_init__(self) -> None:
        if _VERSION.fullmatch(self.version) is None:
            raise ValueError("test suite version is invalid")
        public_tests = tuple(self.public_tests)
        hidden_tests = tuple(self.hidden_tests)
        if not public_tests or not hidden_tests:
            raise ValueError("a certification suite must contain public and hidden tests")
        if any(test.visibility != "PUBLIC" for test in public_tests):
            raise ValueError("public_tests may contain only PUBLIC tests")
        if any(test.visibility != "HIDDEN" for test in hidden_tests):
            raise ValueError("hidden_tests may contain only HIDDEN tests")
        identifiers = tuple(test.test_case_id for test in (*public_tests, *hidden_tests))
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("test_case_id values must be unique within a suite")
        object.__setattr__(self, "public_tests", public_tests)
        object.__setattr__(self, "hidden_tests", hidden_tests)


@dataclass(frozen=True, slots=True)
class BuildDiagnostic:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class DockerTestResult:
    test_case_id: str
    visibility: Literal["PUBLIC", "HIDDEN"]
    status: Literal["PASSED", "FAILED", "TIMEOUT", "ERROR"]
    diagnostic_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DockerBuildFailure:
    code: str
    stage: Literal["VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST"]
    diagnostics: tuple[BuildDiagnostic, ...]
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class DockerBuildResult:
    build_id: str
    status: Literal["SUCCEEDED", "FAILED"]
    source_sha256: str | None
    compiler_profile: str
    compiler_version: str
    test_suite_version: str
    build_identity: str | None
    workspace: Path | None
    staged_artifact: Path | None
    artifact_sha256: str | None
    tests: tuple[DockerTestResult, ...]
    diagnostics: tuple[BuildDiagnostic, ...]
    failure: DockerBuildFailure | None

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"


class _PipelineAbort(RuntimeError):
    def __init__(
        self,
        code: str,
        stage: Literal["VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST"],
        diagnostics: tuple[BuildDiagnostic, ...] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.stage: Literal["VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST"] = stage
        self.diagnostics = diagnostics


_RETRYABLE_PIPELINE_CODES = frozenset(
    {
        "DOCKER_UNAVAILABLE",
        "DOCKER_OUTCOME_UNKNOWN",
        "DOCKER_CREATE_RECONCILIATION_FAILED",
        "DOCKER_WAIT_FAILED",
        "CONTAINER_STATE_MISSING",
        "DOCKER_LOG_RECONCILIATION_FAILED",
        "COMPILER_IMAGE_UNAVAILABLE",
        "COMPILER_IMAGE_INSPECT_FAILED",
    }
)


@dataclass(frozen=True, slots=True)
class _StepExecution:
    identity: str
    exit_code: int
    stdout: bytes
    stderr: bytes
    oom_killed: bool
    limit_reason: Literal["TIMEOUT", "OUTPUT_LIMIT"] | None = None
    copied_artifact_sha256: str | None = None
    copied_artifact_size: int | None = None


@dataclass(frozen=True, slots=True)
class _ExpectedBindMount:
    source: str
    target: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class _ExpectedContainer:
    image_id: str
    image_reference: str
    entrypoint: str
    arguments: tuple[str, ...]
    interactive: bool
    timeout_cpu_seconds: int
    mounts: tuple[_ExpectedBindMount, ...]


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _bounded_diagnostics(
    code: str,
    stdout: bytes,
    stderr: bytes,
    *,
    hidden: bool = False,
) -> tuple[BuildDiagnostic, ...]:
    if hidden:
        return (BuildDiagnostic(code, "A hidden certification test did not pass."),)
    messages: set[str] = set()
    for raw in (*stderr.splitlines(), *stdout.splitlines()):
        text = raw.decode("utf-8", errors="replace").strip()
        if text:
            messages.add(text[:MAX_DIAGNOSTIC_CHARS])
    if not messages:
        messages.add(code.replace("_", " ").title())
    return tuple(BuildDiagnostic(code, message) for message in sorted(messages)[:MAX_DIAGNOSTICS])


class DigestPinnedDockerCppBuilder:
    """Compile and test C++20 only inside a pinned, isolated Docker image."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        image: str,
        compiler_version: str,
        test_suites: Sequence[CppTestSuite] | Mapping[str, CppTestSuite],
        runner: CommandRunner | None = None,
        docker_executable: str = "docker",
        limits: BuildResourceLimits = BuildResourceLimits(),
    ) -> None:
        root = workspace_root.expanduser().resolve()
        if not root.is_dir() or workspace_root.is_symlink():
            raise ValueError("workspace_root must be an existing non-symlink directory")
        if _PINNED_IMAGE.fullmatch(image) is None:
            raise ValueError("compiler image must be pinned by an exact sha256 digest")
        if not compiler_version or len(compiler_version) > 96 or "\x00" in compiler_version:
            raise ValueError("compiler_version must be a bounded non-empty string")
        if not docker_executable or "\x00" in docker_executable:
            raise ValueError("docker_executable must be a NUL-free string")
        values = (
            tuple(test_suites.values()) if isinstance(test_suites, Mapping) else tuple(test_suites)
        )
        suites = {suite.version: suite for suite in values}
        if not suites or len(suites) != len(values):
            raise ValueError("test suite versions must be non-empty and unique")
        if isinstance(test_suites, Mapping) and any(
            key != suite.version for key, suite in test_suites.items()
        ):
            raise ValueError("test suite mapping keys must equal suite.version")
        self._workspace_root = root
        self._image = image
        self._image_digest = image.rsplit("@sha256:", 1)[1]
        self._compiler_version = compiler_version
        self._test_suites = suites
        self._runner = runner if runner is not None else SubprocessCommandRunner()
        self._docker = docker_executable
        self._limits = limits
        self._verified_image_id: str | None = None
        self._security_profile_sha256 = hashlib.sha256(
            _canonical_json_bytes(
                {
                    "protocol": _DOCKER_SECURITY_PROTOCOL,
                    "image_reference": image,
                    "config": {
                        "user": _DOCKER_USER,
                        "working_dir": _DOCKER_WORKDIR,
                    },
                    "host_config": {
                        "network_mode": "none",
                        "read_only_rootfs": True,
                        "cap_add": [],
                        "cap_drop": list(_DOCKER_CAP_DROP),
                        "security_options": list(_DOCKER_SECURITY_OPTIONS),
                        "privileged": False,
                        "pids_limit": limits.max_processes,
                        "memory": limits.memory_bytes,
                        "memory_swap": limits.memory_bytes,
                        "nano_cpus": int(round(float(f"{float(limits.cpus):.3f}") * 1_000_000_000)),
                        "ipc_mode": "none",
                        "tmpfs": {"/tmp": (f"rw,noexec,nosuid,nodev,size={limits.tmpfs_bytes}")},
                        "base_ulimits": {
                            "core": [0, 0],
                            "fsize": [limits.max_artifact_bytes, limits.max_artifact_bytes],
                            "nofile": [64, 64],
                        },
                        "log_config": {
                            "type": "local",
                            "config": {
                                "compress": "false",
                                "max-file": "1",
                                "max-size": str(limits.max_output_bytes + 1),
                            },
                        },
                    },
                }
            )
        ).hexdigest()
        self._policy_sha256 = hashlib.sha256(
            _canonical_json_bytes(
                {
                    "compiler_flags": CPP20_SAFE_V1_FLAGS,
                    "compiler_profile": CPP20_SAFE_V1_PROFILE,
                    "compiler_version": compiler_version,
                    "docker_security_profile_sha256": self._security_profile_sha256,
                    "image": image,
                    "limits": {
                        "compile_wall_ms": limits.compile_wall_ms,
                        "test_wall_ms": limits.test_wall_ms,
                        "memory_bytes": limits.memory_bytes,
                        "max_processes": limits.max_processes,
                        "cpus": float(limits.cpus),
                        "tmpfs_bytes": limits.tmpfs_bytes,
                        "max_output_bytes": limits.max_output_bytes,
                        "max_artifact_bytes": limits.max_artifact_bytes,
                    },
                    "test_suites": [
                        {
                            "version": suite.version,
                            "public": [self._test_projection(test) for test in suite.public_tests],
                            "hidden": [self._test_projection(test) for test in suite.hidden_tests],
                        }
                        for suite in sorted(suites.values(), key=lambda item: item.version)
                    ],
                }
            )
        ).hexdigest()

    @staticmethod
    def _test_projection(test: CppTestCase) -> Mapping[str, object]:
        return {
            "test_case_id": test.test_case_id,
            "visibility": test.visibility,
            "arguments": test.arguments,
            "stdin_sha256": hashlib.sha256(test.stdin).hexdigest(),
            "expected_stdout_sha256": test.expected_stdout_sha256,
        }

    def build_identity(self, request: CompileAndTestRequest) -> str:
        validated = validate_source_bundle(request.source_bundle)
        return self._identity(request, validated)

    def container_name(self, request: CompileAndTestRequest, phase: str) -> str:
        validated = validate_source_bundle(request.source_bundle)
        identity = self._identity(request, validated)
        phase_hash = hashlib.sha256(
            f"{_DOCKER_SECURITY_PROTOCOL}\x00{identity}\x00{phase}".encode()
        ).hexdigest()[:24]
        return f"yaya-bld-{phase_hash}"

    def build(self, request: CompileAndTestRequest) -> DockerBuildResult:
        validated: ValidatedSourceBundle | None = None
        identity: str | None = None
        workspace: Path | None = None
        tests: list[DockerTestResult] = []
        try:
            validated = validate_source_bundle(request.source_bundle)
            if request.compiler_profile != CPP20_SAFE_V1_PROFILE:
                raise _PipelineAbort("UNSUPPORTED_COMPILER_PROFILE", "VALIDATE_SOURCE")
            suite = self._test_suites.get(request.test_suite_version)
            if suite is None:
                raise _PipelineAbort("UNKNOWN_TEST_SUITE_VERSION", "VALIDATE_SOURCE")
            identity = self._identity(request, validated)
            workspace = self._prepare_workspace(identity, validated)
            self._verify_docker_image()
            self._verify_compiler(request, validated, workspace)
            artifact = self._compile(request, validated, workspace)
            for test in suite.public_tests:
                result = self._run_test(request, validated, workspace, artifact, test)
                tests.append(result)
                if result.status != "PASSED":
                    code = result.diagnostic_codes[0]
                    diagnostics = _bounded_diagnostics(code, b"", b"")
                    raise _PipelineAbort(code, "PUBLIC_TEST", diagnostics)
            for test in suite.hidden_tests:
                result = self._run_test(request, validated, workspace, artifact, test)
                tests.append(result)
                if result.status != "PASSED":
                    code = result.diagnostic_codes[0]
                    diagnostics = _bounded_diagnostics(code, b"", b"", hidden=True)
                    raise _PipelineAbort(code, "HIDDEN_TEST", diagnostics)
            artifact_sha256 = self._hash_regular_staged_artifact(artifact)
            return DockerBuildResult(
                build_id=request.build_id,
                status="SUCCEEDED",
                source_sha256=validated.source_sha256,
                compiler_profile=request.compiler_profile,
                compiler_version=self._compiler_version,
                test_suite_version=request.test_suite_version,
                build_identity=identity,
                workspace=workspace,
                staged_artifact=artifact,
                artifact_sha256=artifact_sha256,
                tests=tuple(tests),
                diagnostics=(),
                failure=None,
            )
        except SourceBundleValidationError as error:
            abort = _PipelineAbort(
                error.code,
                "VALIDATE_SOURCE",
                (BuildDiagnostic(error.code, str(error)[:MAX_DIAGNOSTIC_CHARS]),),
            )
        except _PipelineAbort as error:
            abort = error
        except OSError:
            abort = _PipelineAbort("BUILD_WORKSPACE_ERROR", "COMPILE")
        except Exception:
            abort = _PipelineAbort("INTERNAL_BUILD_ERROR", "COMPILE")
        failure = DockerBuildFailure(
            abort.code,
            abort.stage,
            abort.diagnostics,
            retryable=abort.code in _RETRYABLE_PIPELINE_CODES,
        )
        return DockerBuildResult(
            build_id=request.build_id,
            status="FAILED",
            source_sha256=None if validated is None else validated.source_sha256,
            compiler_profile=request.compiler_profile,
            compiler_version=self._compiler_version,
            test_suite_version=request.test_suite_version,
            build_identity=identity,
            workspace=workspace,
            staged_artifact=None,
            artifact_sha256=None,
            tests=tuple(tests),
            diagnostics=abort.diagnostics,
            failure=failure,
        )

    def discard_workspace(self, result: DockerBuildResult) -> None:
        """Remove retained staging only after the caller durably commits the terminal result."""

        workspace = result.workspace
        if workspace is None:
            return
        resolved = workspace.resolve(strict=True)
        if resolved.parent != self._workspace_root or not resolved.name.startswith("build-"):
            raise RuntimeError("refusing to remove a workspace outside the configured build root")
        if sys.platform == "win32":
            for candidate in resolved.rglob("*"):
                if candidate.is_file() and not candidate.is_symlink():
                    candidate.chmod(stat.S_IWRITE | stat.S_IREAD)
        shutil.rmtree(resolved)

    def _identity(self, request: CompileAndTestRequest, validated: ValidatedSourceBundle) -> str:
        if (
            _IDENTIFIER.fullmatch(request.build_id) is None
            or _IDENTIFIER.fullmatch(request.skill_id) is None
        ):
            raise _PipelineAbort("INVALID_BUILD_IDENTITY", "VALIDATE_SOURCE")
        seed = {
            "build_id": request.build_id,
            "skill_id": request.skill_id,
            "source_sha256": validated.source_sha256,
            "compiler_profile": request.compiler_profile,
            "test_suite_version": request.test_suite_version,
            "policy_sha256": self._policy_sha256,
        }
        return hashlib.sha256(_canonical_json_bytes(seed)).hexdigest()

    def _prepare_workspace(self, identity: str, validated: ValidatedSourceBundle) -> Path:
        workspace = self._workspace_root / f"build-{identity}"
        workspace.mkdir(mode=0o700, exist_ok=True)
        if workspace.is_symlink() or workspace.resolve() != workspace.absolute():
            raise _PipelineAbort("BUILD_WORKSPACE_DRIFT", "VALIDATE_SOURCE")
        source = workspace / "source"
        staging = workspace / "staging"
        receipts = workspace / "receipts"
        for directory in (source, staging, receipts):
            directory.mkdir(mode=0o700, exist_ok=True)
            if directory.is_symlink() or directory.resolve().parent != workspace:
                raise _PipelineAbort("BUILD_WORKSPACE_DRIFT", "VALIDATE_SOURCE")
        try:
            staging.chmod(0o733)
        except OSError:
            pass
        manifest = _canonical_json_bytes(
            {
                "language": validated.language,
                "entrypoint": validated.entrypoint,
                "source_sha256": validated.source_sha256,
                "files": [
                    {"path": item.path, "content_sha256": item.content_sha256}
                    for item in validated.files
                ],
            }
        )
        self._write_reconcilable_file(source / "manifest.json", manifest, mode=0o400)
        for index, source_file in enumerate(validated.files):
            self._write_reconcilable_file(
                source / f"file-{index:02d}", source_file.content.encode("utf-8"), mode=0o400
            )
        return workspace

    @staticmethod
    def _write_reconcilable_file(path: Path, content: bytes, *, mode: int) -> None:
        if path.exists():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise _PipelineAbort("BUILD_WORKSPACE_DRIFT", "VALIDATE_SOURCE")
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".step-", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as writer:
                writer.write(content)
                writer.flush()
                os.fsync(writer.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != content:
                    raise _PipelineAbort("BUILD_WORKSPACE_DRIFT", "VALIDATE_SOURCE")
            else:
                temporary.unlink()
                path.chmod(mode)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            if temporary.exists():
                try:
                    temporary.unlink()
                except PermissionError:
                    temporary.chmod(stat.S_IWRITE | stat.S_IREAD)
                    temporary.unlink()

    def _verify_docker_image(self) -> None:
        try:
            version = self._runner.run(
                [self._docker, "version", "--format", "{{.Server.Version}}"],
                timeout_seconds=15,
                max_output_bytes=4096,
            )
        except (CommandUnavailableError, CommandTimeoutError, CommandOutputLimitError) as error:
            raise _PipelineAbort("DOCKER_UNAVAILABLE", "COMPILE") from error
        if version.returncode != 0 or not version.stdout.strip():
            raise _PipelineAbort("DOCKER_UNAVAILABLE", "COMPILE")
        try:
            inspected = self._runner.run(
                [
                    self._docker,
                    "image",
                    "inspect",
                    self._image,
                    "--format",
                    "{{.Id}}\n{{.Os}}\n{{json .RepoDigests}}",
                ],
                timeout_seconds=30,
                max_output_bytes=16_384,
            )
        except CommandUnavailableError as error:
            raise _PipelineAbort("DOCKER_UNAVAILABLE", "COMPILE") from error
        except (CommandTimeoutError, CommandOutputLimitError) as error:
            raise _PipelineAbort("COMPILER_IMAGE_INSPECT_FAILED", "COMPILE") from error
        if inspected.returncode != 0:
            raise _PipelineAbort("COMPILER_IMAGE_UNAVAILABLE", "COMPILE")
        try:
            image_id, os_name, raw_digests = inspected.stdout.decode("utf-8").strip().split("\n", 2)
            parsed_digests: object = json.loads(raw_digests)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise _PipelineAbort("COMPILER_IMAGE_INSPECT_FAILED", "COMPILE") from error
        if _IMAGE_ID.fullmatch(image_id) is None:
            raise _PipelineAbort("COMPILER_IMAGE_INSPECT_FAILED", "COMPILE")
        if os_name != "linux":
            raise _PipelineAbort("COMPILER_IMAGE_PLATFORM_MISMATCH", "COMPILE")
        if not isinstance(parsed_digests, list):
            raise _PipelineAbort("COMPILER_IMAGE_INSPECT_FAILED", "COMPILE")
        repo_digests = cast(list[object], parsed_digests)
        if not any(
            isinstance(item, str) and item.endswith(f"@sha256:{self._image_digest}")
            for item in repo_digests
        ):
            raise _PipelineAbort("COMPILER_IMAGE_DIGEST_DRIFT", "COMPILE")
        self._verified_image_id = image_id

    def _verify_compiler(
        self,
        request: CompileAndTestRequest,
        validated: ValidatedSourceBundle,
        workspace: Path,
    ) -> None:
        phase = "compiler-version"
        execution = self._run_step(
            request,
            validated,
            workspace,
            phase=phase,
            entrypoint="g++",
            arguments=("-dumpfullversion", "-dumpversion"),
            mounts=(),
            stdin=b"",
            timeout_ms=self._limits.test_wall_ms,
        )
        self._raise_for_step_limit(execution, "COMPILE")
        if execution.oom_killed:
            raise _PipelineAbort("COMPILER_PROBE_MEMORY_LIMIT", "COMPILE")
        if execution.exit_code != 0:
            raise _PipelineAbort(
                "COMPILER_PROBE_FAILED",
                "COMPILE",
                _bounded_diagnostics("COMPILER_PROBE_FAILED", execution.stdout, execution.stderr),
            )
        actual = execution.stdout.decode("utf-8", errors="replace").strip()
        if actual != self._compiler_version:
            raise _PipelineAbort("COMPILER_VERSION_DRIFT", "COMPILE")

    def _compile(
        self,
        request: CompileAndTestRequest,
        validated: ValidatedSourceBundle,
        workspace: Path,
    ) -> Path:
        materialized, archive = self._source_archive(validated)
        source_paths = [materialized[validated.entrypoint]]
        source_paths.extend(
            materialized[item.path]
            for item in validated.files
            if item.path != validated.entrypoint
            and materialized[item.path].lower().endswith((".cpp", ".cc", ".cxx"))
        )
        source_root = workspace / "source"
        archive_path = source_root / "bundle.tar"
        # The compiler process runs as uid/gid 65534.  The single opaque archive is host-read-only
        # and bind-mounted read-only, but must be readable by that non-root identity on Linux.
        self._write_reconcilable_file(archive_path, archive, mode=0o444)
        staging = workspace / "staging"
        candidate = staging / "skill"
        mounts = (
            f"type=bind,source={archive_path},target=/yaya-source.tar,readonly",
            f"type=bind,source={staging},target=/yaya-output",
        )
        compiler_arguments = (
            *CPP20_SAFE_V1_FLAGS,
            "-o",
            "/yaya-output/container-skill",
            *(f"/tmp/yaya-source/{path}" for path in source_paths),
        )
        compile_script = (
            "tar -xf /yaya-source.tar -C /tmp --no-same-owner --no-same-permissions"
            f" && exec g++ {' '.join(shlex.quote(item) for item in compiler_arguments)}"
        )
        execution = self._run_step(
            request,
            validated,
            workspace,
            phase="compile",
            entrypoint="/bin/sh",
            arguments=("-ceu", compile_script),
            mounts=mounts,
            stdin=b"",
            timeout_ms=self._limits.compile_wall_ms,
            copy_out=("/yaya-output/container-skill", candidate),
        )
        self._raise_for_step_limit(execution, "COMPILE")
        if execution.oom_killed:
            raise _PipelineAbort("COMPILE_MEMORY_LIMIT", "COMPILE")
        if execution.exit_code != 0:
            raise _PipelineAbort(
                "COMPILE_ERROR",
                "COMPILE",
                _bounded_diagnostics("COMPILE_ERROR", execution.stdout, execution.stderr),
            )
        self._hash_regular_staged_artifact(candidate)
        try:
            candidate.chmod(
                stat.S_IRUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )
        except OSError as error:
            raise _PipelineAbort("STAGED_ARTIFACT_PERMISSION_ERROR", "COMPILE") from error
        return candidate

    def _source_archive(self, validated: ValidatedSourceBundle) -> tuple[Mapping[str, str], bytes]:
        normalized: dict[str, str] = {}
        occupied: set[str] = set()
        for source_file in validated.files:
            segments = [
                segment for segment in source_file.path.split("/") if segment not in ("", ".")
            ]
            if not segments:
                raise _PipelineAbort("SOURCE_PATH_NOT_MATERIALIZABLE", "VALIDATE_SOURCE")
            path = "/".join(segments)
            if path in occupied:
                raise _PipelineAbort("SOURCE_PATH_MATERIALIZATION_COLLISION", "VALIDATE_SOURCE")
            occupied.add(path)
            normalized[source_file.path] = path
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.GNU_FORMAT) as archive:
            for source_file in validated.files:
                content = source_file.content.encode("utf-8")
                info = tarfile.TarInfo(f"yaya-source/{normalized[source_file.path]}")
                info.size = len(content)
                info.mode = 0o444
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                archive.addfile(info, io.BytesIO(content))
        return normalized, stream.getvalue()

    def _run_test(
        self,
        request: CompileAndTestRequest,
        validated: ValidatedSourceBundle,
        workspace: Path,
        artifact: Path,
        test: CppTestCase,
    ) -> DockerTestResult:
        phase = f"test-{test.visibility.lower()}-{test.test_case_id}"
        mount = f"type=bind,source={artifact},target=/opt/yaya/skill,readonly"
        try:
            execution = self._run_step(
                request,
                validated,
                workspace,
                phase=phase,
                entrypoint="/opt/yaya/skill",
                arguments=test.arguments,
                mounts=(mount,),
                stdin=test.stdin,
                timeout_ms=self._limits.test_wall_ms,
            )
        except _PipelineAbort as error:
            test_stage: Literal["PUBLIC_TEST", "HIDDEN_TEST"] = (
                "PUBLIC_TEST" if test.visibility == "PUBLIC" else "HIDDEN_TEST"
            )
            raise _PipelineAbort(error.code, test_stage, error.diagnostics) from error
        prefix = test.visibility
        if execution.limit_reason == "TIMEOUT":
            return DockerTestResult(
                test.test_case_id, test.visibility, "TIMEOUT", (f"{prefix}_TEST_TIMEOUT",)
            )
        if execution.limit_reason == "OUTPUT_LIMIT":
            return DockerTestResult(
                test.test_case_id, test.visibility, "ERROR", (f"{prefix}_TEST_OUTPUT_LIMIT",)
            )
        if execution.oom_killed:
            return DockerTestResult(
                test.test_case_id, test.visibility, "ERROR", (f"{prefix}_TEST_MEMORY_LIMIT",)
            )
        if execution.exit_code != 0:
            return DockerTestResult(
                test.test_case_id, test.visibility, "FAILED", (f"{prefix}_TEST_FAILED",)
            )
        if test.expected_stdout_sha256 is not None and (
            hashlib.sha256(execution.stdout).hexdigest() != test.expected_stdout_sha256
        ):
            return DockerTestResult(
                test.test_case_id,
                test.visibility,
                "FAILED",
                (f"{prefix}_TEST_OUTPUT_MISMATCH",),
            )
        return DockerTestResult(test.test_case_id, test.visibility, "PASSED", ())

    def _run_step(
        self,
        request: CompileAndTestRequest,
        validated: ValidatedSourceBundle,
        workspace: Path,
        *,
        phase: str,
        entrypoint: str,
        arguments: Sequence[str],
        mounts: Sequence[str],
        stdin: bytes,
        timeout_ms: int,
        copy_out: tuple[str, Path] | None = None,
    ) -> _StepExecution:
        expected_container = self._expected_container(
            entrypoint=entrypoint,
            arguments=arguments,
            mounts=mounts,
            timeout_ms=timeout_ms,
            interactive=bool(stdin),
        )
        security_projection = self._container_security_projection(expected_container)
        security_sha256 = hashlib.sha256(_canonical_json_bytes(security_projection)).hexdigest()
        step_identity = hashlib.sha256(
            _canonical_json_bytes(
                {
                    "build_identity": self._identity(request, validated),
                    "phase": phase,
                    "security_protocol": _DOCKER_SECURITY_PROTOCOL,
                    "security_sha256": security_sha256,
                    "stdin_sha256": hashlib.sha256(stdin).hexdigest(),
                }
            )
        ).hexdigest()
        build_identity = self._identity(request, validated)
        container_hash = hashlib.sha256(
            f"{_DOCKER_SECURITY_PROTOCOL}\x00{build_identity}\x00{phase}".encode()
        ).hexdigest()[:24]
        container_name = f"yaya-bld-{container_hash}"
        receipt_path = workspace / "receipts" / f"{step_identity}.json"
        receipt = self._read_step_receipt(receipt_path, step_identity)
        labels = {
            "local.yaya.build": "true",
            "local.yaya.build_id": request.build_id,
            "local.yaya.source_sha256": validated.source_sha256,
            "local.yaya.policy_sha256": self._policy_sha256,
            "local.yaya.step_identity": step_identity,
            "local.yaya.security_protocol": _DOCKER_SECURITY_PROTOCOL,
            "local.yaya.security_sha256": security_sha256,
        }
        if receipt is not None:
            self._remove_matching_container(container_name, labels, expected_container)
            if copy_out is not None:
                destination = copy_out[1]
                if not destination.is_file() or destination.is_symlink():
                    raise _PipelineAbort("STAGED_ARTIFACT_MISSING", "COMPILE")
                current_sha256 = self._hash_regular_staged_artifact(destination)
                if (
                    receipt.copied_artifact_sha256 != current_sha256
                    or receipt.copied_artifact_size != destination.stat().st_size
                ):
                    raise _PipelineAbort("STAGED_ARTIFACT_DRIFT", "COMPILE")
            return receipt

        inspected = self._inspect_container(container_name)
        if inspected is None:
            create = self._runner_call(
                self._create_command(
                    container_name,
                    labels,
                    entrypoint,
                    arguments,
                    mounts,
                    timeout_ms,
                    interactive=bool(stdin),
                ),
                timeout_seconds=30,
                max_output_bytes=16_384,
                stage="COMPILE",
            )
            if create.returncode != 0:
                inspected = self._inspect_container(container_name)
                if inspected is None:
                    raise _PipelineAbort("DOCKER_CREATE_FAILED", "COMPILE")
            else:
                inspected = self._inspect_container(container_name)
                if inspected is None:
                    raise _PipelineAbort("DOCKER_CREATE_RECONCILIATION_FAILED", "COMPILE")
        self._validate_container(inspected, labels, expected_container)
        state = self._container_state(inspected)
        status = state.get("Status")
        start_output: CommandResult | None = None
        limit_reason: Literal["TIMEOUT", "OUTPUT_LIMIT"] | None = None
        if status == "created":
            try:
                start_output = self._runner.run(
                    [
                        self._docker,
                        "start",
                        "--attach",
                        *(("--interactive",) if stdin else ()),
                        container_name,
                    ],
                    timeout_seconds=timeout_ms / 1000,
                    max_output_bytes=self._limits.max_output_bytes,
                    input_bytes=stdin if stdin else None,
                )
            except CommandTimeoutError as error:
                start_output = CommandResult(-1, error.stdout, error.stderr)
                limit_reason = "TIMEOUT"
                self._kill_container(container_name)
            except CommandOutputLimitError as error:
                start_output = CommandResult(-1, error.stdout, error.stderr)
                limit_reason = "OUTPUT_LIMIT"
                self._kill_container(container_name)
            except CommandUnavailableError as error:
                # ``docker start --attach`` may lose its control-plane response
                # after the container has already run.  Reconcile the stable,
                # fully-labelled container before deciding whether another
                # worker may safely retry.  A recovered running/exited state is
                # consumed below through wait/inspect/logs; a still-created or
                # temporarily uninspectable container remains retryable and is
                # never converted into a student Build rejection.
                try:
                    reconciled = self._inspect_container(container_name)
                except _PipelineAbort as inspection_error:
                    raise _PipelineAbort("DOCKER_OUTCOME_UNKNOWN", "COMPILE") from inspection_error
                if reconciled is None:
                    raise _PipelineAbort("DOCKER_OUTCOME_UNKNOWN", "COMPILE") from error
                self._validate_container(reconciled, labels, expected_container)
                reconciled_status = self._container_state(reconciled).get("Status")
                if reconciled_status == "created":
                    raise _PipelineAbort("DOCKER_OUTCOME_UNKNOWN", "COMPILE") from error
                if reconciled_status not in {"running", "exited"}:
                    raise _PipelineAbort("CONTAINER_STATE_UNRECOVERABLE", "COMPILE") from error
                inspected = reconciled
        elif status == "running":
            try:
                waited = self._runner.run(
                    [self._docker, "wait", container_name],
                    timeout_seconds=timeout_ms / 1000,
                    max_output_bytes=4096,
                )
                if waited.returncode != 0:
                    raise _PipelineAbort("DOCKER_WAIT_FAILED", "COMPILE")
            except CommandTimeoutError as error:
                start_output = CommandResult(-1, error.stdout, error.stderr)
                limit_reason = "TIMEOUT"
                self._kill_container(container_name)
            except (CommandUnavailableError, CommandOutputLimitError) as error:
                raise _PipelineAbort("DOCKER_UNAVAILABLE", "COMPILE") from error
        elif status != "exited":
            raise _PipelineAbort("CONTAINER_STATE_UNRECOVERABLE", "COMPILE")

        waited = self._runner_call(
            [self._docker, "wait", container_name],
            timeout_seconds=15,
            max_output_bytes=4096,
            stage="COMPILE",
        )
        if waited.returncode != 0:
            raise _PipelineAbort("DOCKER_WAIT_FAILED", "COMPILE")
        inspected = self._inspect_container(container_name)
        if inspected is None:
            raise _PipelineAbort("CONTAINER_STATE_MISSING", "COMPILE")
        self._validate_container(inspected, labels, expected_container)
        state = self._container_state(inspected)
        raw_exit_code = state.get("ExitCode")
        if isinstance(raw_exit_code, bool) or not isinstance(raw_exit_code, (int, str)):
            raise _PipelineAbort("CONTAINER_STATE_INVALID", "COMPILE")
        try:
            exit_code = int(raw_exit_code)
        except ValueError as error:
            raise _PipelineAbort("CONTAINER_STATE_INVALID", "COMPILE") from error
        oom_killed = state.get("OOMKilled") is True
        # The attach process is only a control-plane observation.  Docker may
        # return a non-zero status or lose that response after the container
        # has already exited successfully, and its captured stdout can then be
        # partial.  Once the exact labelled container reaches a terminal state,
        # reconcile the bounded output from Docker's local log authority.  A
        # timeout/output-limit remains the terminal observation for that step.
        if limit_reason is None:
            try:
                logs = self._runner.run(
                    [self._docker, "logs", container_name],
                    timeout_seconds=15,
                    max_output_bytes=self._limits.max_output_bytes,
                )
                if logs.returncode != 0:
                    raise _PipelineAbort("DOCKER_LOG_RECONCILIATION_FAILED", "COMPILE")
                start_output = logs
            except CommandOutputLimitError as error:
                start_output = CommandResult(-1, error.stdout, error.stderr)
                limit_reason = "OUTPUT_LIMIT"
            except (CommandUnavailableError, CommandTimeoutError) as error:
                raise _PipelineAbort("DOCKER_LOG_RECONCILIATION_FAILED", "COMPILE") from error
        if start_output is None:
            raise _PipelineAbort("DOCKER_LOG_RECONCILIATION_FAILED", "COMPILE")
        if copy_out is not None and exit_code == 0 and limit_reason is None:
            container_path, destination = copy_out
            descriptor, copy_name = tempfile.mkstemp(
                prefix=".artifact-copy-", suffix=".tmp", dir=destination.parent
            )
            os.close(descriptor)
            copied_candidate = Path(copy_name)
            copied_candidate.unlink()
            try:
                copied = self._runner_call(
                    [
                        self._docker,
                        "cp",
                        f"{container_name}:{container_path}",
                        str(copied_candidate),
                    ],
                    timeout_seconds=30,
                    max_output_bytes=16_384,
                    stage="COMPILE",
                )
                if copied.returncode != 0:
                    self._remove_matching_container(container_name, labels, expected_container)
                    raise _PipelineAbort("ARTIFACT_COPY_FAILED", "COMPILE")
                self._hash_regular_staged_artifact(copied_candidate)
                if destination.exists():
                    if (
                        destination.is_symlink()
                        or not destination.is_file()
                        or not self._files_equal(copied_candidate, destination)
                    ):
                        raise _PipelineAbort("STAGED_ARTIFACT_DRIFT", "COMPILE")
                else:
                    try:
                        os.link(copied_candidate, destination)
                    except FileExistsError:
                        if (
                            destination.is_symlink()
                            or not destination.is_file()
                            or not self._files_equal(copied_candidate, destination)
                        ):
                            raise _PipelineAbort("STAGED_ARTIFACT_DRIFT", "COMPILE")
            finally:
                copied_candidate.unlink(missing_ok=True)
        copied_sha256: str | None = None
        copied_size: int | None = None
        if copy_out is not None and exit_code == 0 and limit_reason is None:
            copied_sha256 = self._hash_regular_staged_artifact(copy_out[1])
            copied_size = copy_out[1].stat().st_size
        execution = _StepExecution(
            step_identity,
            exit_code,
            start_output.stdout,
            start_output.stderr,
            oom_killed,
            limit_reason,
            copied_sha256,
            copied_size,
        )
        self._write_step_receipt(receipt_path, execution)
        self._remove_matching_container(container_name, labels, expected_container)
        return execution

    def _create_command(
        self,
        container_name: str,
        labels: Mapping[str, str],
        entrypoint: str,
        arguments: Sequence[str],
        mounts: Sequence[str],
        timeout_ms: int,
        *,
        interactive: bool,
    ) -> list[str]:
        command = [
            self._docker,
            "create",
            "--pull=never",
            "--name",
            container_name,
        ]
        for key, value in sorted(labels.items()):
            command.extend(("--label", f"{key}={value}"))
        command.extend(
            (
                "--log-driver",
                "local",
                "--log-opt",
                f"max-size={self._limits.max_output_bytes + 1}",
                "--log-opt",
                "max-file=1",
                "--log-opt",
                "compress=false",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                _DOCKER_CAP_DROP[0],
                "--security-opt",
                _DOCKER_SECURITY_OPTIONS[0],
                "--user",
                _DOCKER_USER,
                "--pids-limit",
                str(self._limits.max_processes),
                "--memory",
                str(self._limits.memory_bytes),
                "--memory-swap",
                str(self._limits.memory_bytes),
                "--cpus",
                f"{float(self._limits.cpus):.3f}",
                "--ipc",
                "none",
                "--ulimit",
                "core=0:0",
                "--ulimit",
                "nofile=64:64",
                "--ulimit",
                f"fsize={self._limits.max_artifact_bytes}:{self._limits.max_artifact_bytes}",
                "--ulimit",
                f"cpu={max(1, math.ceil(timeout_ms / 1000))}:{max(1, math.ceil(timeout_ms / 1000))}",
                "--tmpfs",
                f"/tmp:rw,noexec,nosuid,nodev,size={self._limits.tmpfs_bytes}",
                "--workdir",
                _DOCKER_WORKDIR,
            )
        )
        if interactive:
            command.append("--interactive")
        for mount in mounts:
            command.extend(("--mount", mount))
        command.extend(("--entrypoint", entrypoint, self._image, *arguments))
        return command

    def _runner_call(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        max_output_bytes: int,
        stage: Literal["VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST"],
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        try:
            return self._runner.run(
                command,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
                input_bytes=input_bytes,
            )
        except CommandUnavailableError as error:
            raise _PipelineAbort("DOCKER_UNAVAILABLE", stage) from error
        except CommandTimeoutError as error:
            raise _PipelineAbort("DOCKER_CONTROL_TIMEOUT", stage) from error
        except CommandOutputLimitError as error:
            raise _PipelineAbort("DOCKER_CONTROL_OUTPUT_LIMIT", stage) from error

    def _inspect_container(self, container_name: str) -> Mapping[str, object] | None:
        result = self._runner_call(
            [self._docker, "inspect", container_name, "--format", "{{json .}}"],
            timeout_seconds=15,
            max_output_bytes=65_536,
            stage="COMPILE",
        )
        if result.returncode != 0:
            return None
        try:
            value = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _PipelineAbort("CONTAINER_INSPECT_INVALID", "COMPILE") from error
        if not isinstance(value, Mapping):
            raise _PipelineAbort("CONTAINER_INSPECT_INVALID", "COMPILE")
        return cast(Mapping[str, object], value)

    @staticmethod
    def _container_state(inspected: Mapping[str, object]) -> Mapping[str, object]:
        state = inspected.get("State")
        if not isinstance(state, Mapping):
            raise _PipelineAbort("CONTAINER_STATE_INVALID", "COMPILE")
        return cast(Mapping[str, object], state)

    def _expected_container(
        self,
        *,
        entrypoint: str,
        arguments: Sequence[str],
        mounts: Sequence[str],
        timeout_ms: int,
        interactive: bool,
    ) -> _ExpectedContainer:
        image_id = self._verified_image_id
        if image_id is None:
            raise _PipelineAbort("COMPILER_IMAGE_INSPECT_FAILED", "COMPILE")
        expected_mounts: list[_ExpectedBindMount] = []
        targets: set[str] = set()
        prefix = "type=bind,source="
        for mount in mounts:
            if not mount.startswith(prefix) or ",target=" not in mount:
                raise _PipelineAbort("CONTAINER_SECURITY_PROJECTION_INVALID", "COMPILE")
            source, target_wire = mount[len(prefix) :].rsplit(",target=", 1)
            read_only = target_wire.endswith(",readonly")
            target = target_wire[: -len(",readonly")] if read_only else target_wire
            source_path = Path(source)
            try:
                resolved_source = source_path.resolve(strict=True)
            except OSError as error:
                raise _PipelineAbort("CONTAINER_SECURITY_PROJECTION_INVALID", "COMPILE") from error
            if (
                not source
                or not target.startswith("/")
                or "," in target
                or target in targets
                or source_path.is_symlink()
                or resolved_source != source_path.absolute()
            ):
                raise _PipelineAbort("CONTAINER_SECURITY_PROJECTION_INVALID", "COMPILE")
            targets.add(target)
            expected_mounts.append(_ExpectedBindMount(str(resolved_source), target, read_only))
        return _ExpectedContainer(
            image_id=image_id,
            image_reference=self._image,
            entrypoint=entrypoint,
            arguments=tuple(arguments),
            interactive=interactive,
            timeout_cpu_seconds=max(1, math.ceil(timeout_ms / 1000)),
            mounts=tuple(expected_mounts),
        )

    def _container_security_projection(self, expected: _ExpectedContainer) -> Mapping[str, object]:
        return {
            "protocol": _DOCKER_SECURITY_PROTOCOL,
            "profile_sha256": self._security_profile_sha256,
            "image_id": expected.image_id,
            "image_reference": expected.image_reference,
            "config": {
                "entrypoint": [expected.entrypoint],
                "cmd": list(expected.arguments),
                "user": _DOCKER_USER,
                "working_dir": _DOCKER_WORKDIR,
                "open_stdin": expected.interactive,
                "attach_stdin": expected.interactive,
                "stdin_once": expected.interactive,
            },
            "host_config": {
                "network_mode": "none",
                "read_only_rootfs": True,
                "cap_add": [],
                "cap_drop": list(_DOCKER_CAP_DROP),
                "security_options": list(_DOCKER_SECURITY_OPTIONS),
                "privileged": False,
                "pids_limit": self._limits.max_processes,
                "memory": self._limits.memory_bytes,
                "memory_swap": self._limits.memory_bytes,
                "nano_cpus": int(round(float(f"{float(self._limits.cpus):.3f}") * 1_000_000_000)),
                "ipc_mode": "none",
                "tmpfs": {"/tmp": (f"rw,noexec,nosuid,nodev,size={self._limits.tmpfs_bytes}")},
                "ulimits": {
                    "core": [0, 0],
                    "cpu": [expected.timeout_cpu_seconds, expected.timeout_cpu_seconds],
                    "fsize": [
                        self._limits.max_artifact_bytes,
                        self._limits.max_artifact_bytes,
                    ],
                    "nofile": [64, 64],
                },
                "log_config": {
                    "type": "local",
                    "config": {
                        "compress": "false",
                        "max-file": "1",
                        "max-size": str(self._limits.max_output_bytes + 1),
                    },
                },
            },
            "mounts": [
                {
                    "type": "bind",
                    "source": mount.source,
                    "target": mount.target,
                    "read_only": mount.read_only,
                }
                for mount in expected.mounts
            ],
        }

    def _validate_container(
        self,
        inspected: Mapping[str, object],
        labels: Mapping[str, str],
        expected: _ExpectedContainer,
    ) -> None:
        config = inspected.get("Config")
        if not isinstance(config, Mapping):
            raise _PipelineAbort("CONTAINER_IDENTITY_CONFLICT", "COMPILE")
        typed_config = cast(Mapping[str, object], config)
        actual = typed_config.get("Labels")
        if not isinstance(actual, Mapping):
            raise _PipelineAbort("CONTAINER_IDENTITY_CONFLICT", "COMPILE")
        typed_labels = cast(Mapping[str, object], actual)
        yaya_labels = {
            key: value
            for key, value in typed_labels.items()
            if isinstance(key, str) and key.startswith("local.yaya.")
        }
        if yaya_labels != dict(labels):
            raise _PipelineAbort("CONTAINER_IDENTITY_CONFLICT", "COMPILE")

        if not self._container_security_matches(inspected, typed_config, expected):
            raise _PipelineAbort("CONTAINER_SECURITY_DRIFT", "COMPILE")

    def _container_security_matches(
        self,
        inspected: Mapping[str, object],
        config: Mapping[str, object],
        expected: _ExpectedContainer,
    ) -> bool:
        host_raw = inspected.get("HostConfig")
        mounts_raw = inspected.get("Mounts")
        if not isinstance(host_raw, Mapping) or not isinstance(mounts_raw, list):
            return False
        host = cast(Mapping[str, object], host_raw)

        def string_tuple(value: object, *, none_is_empty: bool = False) -> tuple[str, ...] | None:
            if value is None and none_is_empty:
                return ()
            if not isinstance(value, list):
                return None
            items = cast(list[object], value)
            if any(not isinstance(item, str) for item in items):
                return None
            return tuple(cast(str, item) for item in items)

        def exact_int(value: object, expected_value: int) -> bool:
            return type(value) is int and value == expected_value

        if (
            inspected.get("Image") != expected.image_id
            or inspected.get("Path") != expected.entrypoint
            or string_tuple(inspected.get("Args"), none_is_empty=True) != expected.arguments
            or config.get("Image") != expected.image_reference
            or string_tuple(config.get("Entrypoint")) != (expected.entrypoint,)
            or string_tuple(config.get("Cmd"), none_is_empty=True) != expected.arguments
            or config.get("User") != _DOCKER_USER
            or config.get("WorkingDir") != _DOCKER_WORKDIR
            or config.get("OpenStdin") is not expected.interactive
            or config.get("AttachStdin") is not expected.interactive
            or config.get("StdinOnce") is not expected.interactive
            or config.get("Tty") is not False
            or host.get("NetworkMode") != "none"
            or host.get("ReadonlyRootfs") is not True
            or string_tuple(host.get("CapAdd"), none_is_empty=True) != ()
            or string_tuple(host.get("CapDrop")) != _DOCKER_CAP_DROP
            or string_tuple(host.get("SecurityOpt")) != _DOCKER_SECURITY_OPTIONS
            or host.get("Privileged") is not False
            or host.get("IpcMode") != "none"
            or host.get("PidMode") not in (None, "")
            or host.get("UTSMode") not in (None, "")
            or host.get("Binds") not in (None, [])
            or host.get("VolumesFrom") not in (None, [])
            or host.get("Devices") not in (None, [])
            or host.get("DeviceRequests") not in (None, [])
            or not exact_int(host.get("PidsLimit"), self._limits.max_processes)
            or not exact_int(host.get("Memory"), self._limits.memory_bytes)
            or not exact_int(host.get("MemorySwap"), self._limits.memory_bytes)
            or not exact_int(
                host.get("NanoCpus"),
                int(round(float(f"{float(self._limits.cpus):.3f}") * 1_000_000_000)),
            )
        ):
            return False

        expected_tmpfs = {
            "/tmp": frozenset(
                (
                    "rw",
                    "noexec",
                    "nosuid",
                    "nodev",
                    f"size={self._limits.tmpfs_bytes}",
                )
            )
        }
        tmpfs_raw = host.get("Tmpfs")
        if not isinstance(tmpfs_raw, Mapping):
            return False
        actual_tmpfs: dict[str, frozenset[str]] = {}
        for key, value in cast(Mapping[object, object], tmpfs_raw).items():
            if not isinstance(key, str) or not isinstance(value, str):
                return False
            actual_tmpfs[key] = frozenset(value.split(","))
        if actual_tmpfs != expected_tmpfs:
            return False

        expected_ulimits = {
            "core": (0, 0),
            "cpu": (expected.timeout_cpu_seconds, expected.timeout_cpu_seconds),
            "fsize": (self._limits.max_artifact_bytes, self._limits.max_artifact_bytes),
            "nofile": (64, 64),
        }
        ulimits_raw = host.get("Ulimits")
        if not isinstance(ulimits_raw, list):
            return False
        actual_ulimits: dict[str, tuple[int, int]] = {}
        for raw in cast(list[object], ulimits_raw):
            if not isinstance(raw, Mapping):
                return False
            item = cast(Mapping[str, object], raw)
            name = item.get("Name")
            soft = item.get("Soft")
            hard = item.get("Hard")
            if (
                not isinstance(name, str)
                or type(soft) is not int
                or type(hard) is not int
                or name in actual_ulimits
            ):
                return False
            actual_ulimits[name] = (soft, hard)
        if actual_ulimits != expected_ulimits:
            return False

        log_raw = host.get("LogConfig")
        if not isinstance(log_raw, Mapping):
            return False
        log = cast(Mapping[str, object], log_raw)
        log_config = log.get("Config")
        expected_log_config = {
            "compress": "false",
            "max-file": "1",
            "max-size": str(self._limits.max_output_bytes + 1),
        }
        if not isinstance(log_config, Mapping):
            return False
        typed_log_config = cast(Mapping[str, object], log_config)
        if log.get("Type") != "local" or (
            set(typed_log_config) != set(expected_log_config)
            or any(typed_log_config.get(key) != value for key, value in expected_log_config.items())
        ):
            return False

        if not self._mounts_match(
            cast(list[object], mounts_raw), host.get("Mounts"), expected.mounts
        ):
            return False
        return True

    @staticmethod
    def _mounts_match(
        actual_raw: list[object],
        host_raw: object,
        expected: tuple[_ExpectedBindMount, ...],
    ) -> bool:
        if host_raw is None:
            host_mount_items: list[object] = []
        elif isinstance(host_raw, list):
            host_mount_items = cast(list[object], host_raw)
        else:
            return False
        if len(actual_raw) != len(expected) or len(host_mount_items) != len(expected):
            return False
        actual: dict[str, tuple[str, bool]] = {}
        for raw in actual_raw:
            if not isinstance(raw, Mapping):
                return False
            item = cast(Mapping[str, object], raw)
            target = item.get("Destination")
            source = item.get("Source")
            read_write = item.get("RW")
            if (
                item.get("Type") != "bind"
                or not isinstance(target, str)
                or not isinstance(source, str)
                or not isinstance(read_write, bool)
                or target in actual
                or item.get("Propagation") != "rprivate"
            ):
                return False
            actual[target] = (source, not read_write)
        expected_by_target = {mount.target: (mount.source, mount.read_only) for mount in expected}
        if actual != expected_by_target:
            return False
        host_mounts: dict[str, tuple[str, bool]] = {}
        for raw in host_mount_items:
            if not isinstance(raw, Mapping):
                return False
            item = cast(Mapping[str, object], raw)
            target = item.get("Target")
            source = item.get("Source")
            if (
                item.get("Type") != "bind"
                or not isinstance(target, str)
                or not isinstance(source, str)
                or target in host_mounts
            ):
                return False
            host_mounts[target] = (source, item.get("ReadOnly") is True)
        return host_mounts == expected_by_target

    def _kill_container(self, container_name: str) -> None:
        try:
            self._runner.run(
                [self._docker, "kill", container_name],
                timeout_seconds=15,
                max_output_bytes=4096,
            )
        except CommandExecutionError:
            pass

    def _remove_matching_container(
        self,
        container_name: str,
        labels: Mapping[str, str],
        expected: _ExpectedContainer,
    ) -> None:
        inspected = self._inspect_container(container_name)
        if inspected is None:
            return
        self._validate_container(inspected, labels, expected)
        removed = self._runner_call(
            [self._docker, "rm", "--force", container_name],
            timeout_seconds=15,
            max_output_bytes=4096,
            stage="COMPILE",
        )
        if removed.returncode != 0 or self._inspect_container(container_name) is not None:
            raise _PipelineAbort("DOCKER_CLEANUP_FAILED", "COMPILE")

    def _write_step_receipt(self, path: Path, execution: _StepExecution) -> None:
        payload = _canonical_json_bytes(
            {
                "version": 1,
                "identity": execution.identity,
                "exit_code": execution.exit_code,
                "stdout": base64.b64encode(execution.stdout).decode("ascii"),
                "stderr": base64.b64encode(execution.stderr).decode("ascii"),
                "stdout_sha256": hashlib.sha256(execution.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(execution.stderr).hexdigest(),
                "oom_killed": execution.oom_killed,
                "limit_reason": execution.limit_reason,
                "copied_artifact_sha256": execution.copied_artifact_sha256,
                "copied_artifact_size": execution.copied_artifact_size,
            }
        )
        self._write_reconcilable_file(path, payload, mode=0o400)

    @staticmethod
    def _read_step_receipt(path: Path, identity: str) -> _StepExecution | None:
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise _PipelineAbort("STEP_RECEIPT_CORRUPT", "COMPILE")
        try:
            payload = json.loads(path.read_bytes())
            stdout = base64.b64decode(payload["stdout"], validate=True)
            stderr = base64.b64decode(payload["stderr"], validate=True)
            limit_reason = payload["limit_reason"]
            if set(payload) != {
                "version",
                "identity",
                "exit_code",
                "stdout",
                "stderr",
                "stdout_sha256",
                "stderr_sha256",
                "oom_killed",
                "limit_reason",
                "copied_artifact_sha256",
                "copied_artifact_size",
            }:
                raise ValueError
            if (
                payload["version"] != 1
                or payload["identity"] != identity
                or hashlib.sha256(stdout).hexdigest() != payload["stdout_sha256"]
                or hashlib.sha256(stderr).hexdigest() != payload["stderr_sha256"]
                or not isinstance(payload["exit_code"], int)
                or not isinstance(payload["oom_killed"], bool)
                or limit_reason not in (None, "TIMEOUT", "OUTPUT_LIMIT")
                or (
                    payload["copied_artifact_sha256"] is not None
                    and (
                        not isinstance(payload["copied_artifact_sha256"], str)
                        or _SHA256.fullmatch(payload["copied_artifact_sha256"]) is None
                    )
                )
                or (
                    payload["copied_artifact_size"] is not None
                    and (
                        isinstance(payload["copied_artifact_size"], bool)
                        or not isinstance(payload["copied_artifact_size"], int)
                        or payload["copied_artifact_size"] < 1
                    )
                )
                or (
                    (payload["copied_artifact_sha256"] is None)
                    != (payload["copied_artifact_size"] is None)
                )
            ):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise _PipelineAbort("STEP_RECEIPT_CORRUPT", "COMPILE") from error
        return _StepExecution(
            identity,
            payload["exit_code"],
            stdout,
            stderr,
            payload["oom_killed"],
            limit_reason,
            payload["copied_artifact_sha256"],
            payload["copied_artifact_size"],
        )

    @staticmethod
    def _raise_for_step_limit(
        execution: _StepExecution,
        stage: Literal["VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST"],
    ) -> None:
        if execution.limit_reason == "TIMEOUT":
            raise _PipelineAbort(f"{stage}_TIMEOUT", stage)
        if execution.limit_reason == "OUTPUT_LIMIT":
            raise _PipelineAbort(f"{stage}_OUTPUT_LIMIT", stage)

    def _hash_regular_staged_artifact(self, path: Path) -> str:
        if path.is_symlink() or not path.is_file():
            raise _PipelineAbort("STAGED_ARTIFACT_MISSING", "COMPILE")
        size = path.stat().st_size
        if size < 1 or size > self._limits.max_artifact_bytes:
            raise _PipelineAbort("STAGED_ARTIFACT_SIZE_LIMIT", "COMPILE")
        digest = hashlib.sha256()
        with path.open("rb") as reader:
            while chunk := reader.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _files_equal(left: Path, right: Path) -> bool:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_reader, right.open("rb") as right_reader:
            while True:
                left_chunk = left_reader.read(1024 * 1024)
                right_chunk = right_reader.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    artifact_sha256: str
    path: Path
    size_bytes: int
    artifact_uri: str


class ContentAddressedArtifactPublisher:
    """Publish immutable artifacts with an atomic, no-replace content-addressed link."""

    def __init__(self, artifact_root: Path, *, uri_prefix: str = "artifact://sha256/") -> None:
        root = artifact_root.expanduser().resolve()
        if not root.is_dir() or artifact_root.is_symlink():
            raise ValueError("artifact_root must be an existing non-symlink directory")
        if not uri_prefix or "\x00" in uri_prefix:
            raise ValueError("uri_prefix must be a non-empty NUL-free string")
        self._root = root
        self._uri_prefix = uri_prefix

    def artifact_path(self, artifact_sha256: str) -> Path:
        if _SHA256.fullmatch(artifact_sha256) is None:
            raise ValueError("artifact_sha256 must be lowercase SHA-256")
        return self._root / artifact_sha256[:2] / artifact_sha256

    def publish(self, source: Path) -> PublishedArtifact:
        source_path = source.expanduser()
        if source_path.is_symlink() or not source_path.is_file():
            raise ArtifactPublicationError(
                "INVALID_ARTIFACT_SOURCE", "artifact source must be a regular non-symlink file"
            )
        first_digest, source_size = self._hash_path(source_path)
        target = self.artifact_path(first_digest)
        shard = target.parent
        shard.mkdir(mode=0o755, exist_ok=True)
        if shard.is_symlink() or shard.resolve() != self._root / first_digest[:2]:
            raise ArtifactPublicationError(
                "ARTIFACT_PATH_ESCAPE", "artifact shard is not canonical"
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{first_digest}.", suffix=".tmp", dir=shard
        )
        temporary = Path(temporary_name)
        copied_digest = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as writer, source_path.open("rb") as reader:
                while chunk := reader.read(1024 * 1024):
                    writer.write(chunk)
                    copied_digest.update(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if copied_digest.hexdigest() != first_digest or temporary.stat().st_size != source_size:
                raise ArtifactPublicationError(
                    "ARTIFACT_SOURCE_DRIFT", "artifact source changed while it was being published"
                )
            temporary.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            try:
                if sys.platform == "win32":
                    # Windows rename is atomic and refuses an existing destination.  Unlike an
                    # auxiliary hard link, it also preserves the read-only attribute without
                    # leaving a second read-only pathname that cannot be unlinked.
                    os.rename(temporary, target)
                else:
                    os.link(temporary, target)
                self._fsync_directory(shard)
            except FileExistsError:
                existing = self.verify(first_digest)
                if existing.size_bytes != source_size or not self._files_equal(temporary, target):
                    raise ArtifactIntegrityError(
                        "EXISTING_ARTIFACT_DRIFT",
                        "existing content-addressed artifact is not byte-identical",
                    )
            published = self.verify(first_digest)
            comparison_source = temporary if temporary.exists() else source_path
            if published.size_bytes != source_size or not self._files_equal(
                comparison_source, target
            ):
                raise ArtifactIntegrityError(
                    "PUBLISHED_ARTIFACT_DRIFT", "published artifact failed byte reconciliation"
                )
            return published
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            if temporary.exists():
                try:
                    temporary.unlink()
                except PermissionError:
                    temporary.chmod(stat.S_IWRITE | stat.S_IREAD)
                    temporary.unlink()

    def verify(self, artifact_sha256: str) -> PublishedArtifact:
        target = self.artifact_path(artifact_sha256)
        if target.is_symlink() or not target.is_file():
            raise ArtifactIntegrityError(
                "INVALID_ARTIFACT_PATH", "artifact must be a regular non-symlink file"
            )
        try:
            resolved = target.resolve(strict=True)
        except OSError as error:
            raise ArtifactIntegrityError(
                "INVALID_ARTIFACT_PATH", "artifact path cannot be resolved"
            ) from error
        if resolved != target.absolute() or resolved.parent != self._root / artifact_sha256[:2]:
            raise ArtifactIntegrityError(
                "ARTIFACT_PATH_ESCAPE", "artifact path is not canonical under artifact_root"
            )
        metadata = resolved.stat()
        if metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise ArtifactIntegrityError(
                "WRITABLE_ARTIFACT", "published artifact must be read-only"
            )
        actual_digest, size = self._hash_path(resolved)
        if actual_digest != artifact_sha256:
            raise ArtifactIntegrityError(
                "ARTIFACT_DIGEST_MISMATCH", "artifact bytes do not match the address digest"
            )
        return PublishedArtifact(
            artifact_sha256,
            resolved,
            size,
            f"{self._uri_prefix}{artifact_sha256}",
        )

    @staticmethod
    def _hash_path(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as reader:
            while chunk := reader.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        return digest.hexdigest(), size

    @staticmethod
    def _files_equal(left: Path, right: Path) -> bool:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_reader, right.open("rb") as right_reader:
            while True:
                left_chunk = left_reader.read(1024 * 1024)
                right_chunk = right_reader.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return
        try:
            try:
                os.fsync(descriptor)
            except OSError:
                # Windows does not expose fsync for directory handles; the artifact file itself was
                # fsynced before the atomic hard-link publication.
                pass
        finally:
            os.close(descriptor)


__all__ = [
    "ArtifactIntegrityError",
    "ArtifactPublicationError",
    "BuildDiagnostic",
    "BuildResourceLimits",
    "CPP20_SAFE_V1_FLAGS",
    "CPP20_SAFE_V1_PROFILE",
    "CommandExecutionError",
    "CommandOutputLimitError",
    "CommandResult",
    "CommandRunner",
    "CommandTimeoutError",
    "CommandUnavailableError",
    "ContentAddressedArtifactPublisher",
    "CppTestCase",
    "CppTestSuite",
    "DigestPinnedDockerCppBuilder",
    "DockerBuildFailure",
    "DockerBuildResult",
    "DockerTestResult",
    "PublishedArtifact",
    "SourceBundleValidationError",
    "SubprocessCommandRunner",
    "ValidatedSourceBundle",
    "ValidatedSourceFile",
    "canonical_source_bundle_sha256",
    "validate_source_bundle",
]
