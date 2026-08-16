"""Provider-neutral Docker C++ Sandbox with an enforceable isolation boundary."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast

from yaya_agent_contracts import (
    CertificationEvidence,
    CompileAndTestRequest,
    ContractError,
    ErrorCategory,
    Failure,
    FrozenJsonObject,
    OperationContext,
    Result,
    SandboxRunRequest,
    SandboxRunResult,
    SandboxUsage,
    Success,
    canonical_json_sha256,
    canonical_json_v1,
)

from .native import (
    ArgumentBuilder,
    _default_arguments,  # pyright: ignore[reportPrivateUsage]
    _failure,  # pyright: ignore[reportPrivateUsage]
    _parse_action_intents,  # pyright: ignore[reportPrivateUsage]
    _SandboxRejected,  # pyright: ignore[reportPrivateUsage]
)

_PINNED_IMAGE = re.compile(r"^[a-z0-9./:_-]+@sha256:[a-f0-9]{64}$")
_RunKey = tuple[str, str, str, str, str, str]
_RECOVERY_PROTOCOL = "yaya-docker-sandbox-recovery-v1"


class SandboxResultIntegrityError(RuntimeError):
    """Durable Sandbox state conflicts with the exact logical run identity."""


class SandboxOutcomeUnknownError(RuntimeError):
    """Docker control state is unavailable before a terminal outcome is durable."""


@dataclass(frozen=True, slots=True)
class _RecoveryIdentity:
    run_key_sha256: str
    request_sha256: str
    launch_path: Path
    receipt_path: Path
    labels: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _SuccessfulExecution:
    result: SandboxRunResult
    stdout: bytes
    stderr: bytes


@dataclass(slots=True)
class _ContainerControl:
    container_name: str
    docker_executable: str
    cancelled: threading.Event
    process: subprocess.Popen[bytes] | None = None

    def terminate(self) -> None:
        self.cancelled.set()
        subprocess.run(
            [self.docker_executable, "kill", self.container_name],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        process = self.process
        if process is not None and process.poll() is None:
            process.kill()


class DockerCppSandbox:
    """Execute a certified Linux artifact inside a pinned, networkless container.

    The container has a read-only root, a read-only artifact mount, no Linux
    capabilities, no network namespace interfaces, a non-root identity and
    bounded pids/memory/CPU.  Docker is therefore part of the production TCB;
    absence or image-digest drift fails construction instead of falling back to
    host execution.
    """

    def __init__(
        self,
        artifact_root: Path,
        *,
        image: str,
        result_root: Path,
        docker_executable: str = "docker",
        temp_root: Path | None = None,
        argument_builder: ArgumentBuilder = _default_arguments,
    ) -> None:
        root = artifact_root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError("artifact_root must identify an existing directory")
        if _PINNED_IMAGE.fullmatch(image) is None:
            raise ValueError("Sandbox image must be pinned by an exact sha256 digest")
        resolved_temp = None if temp_root is None else temp_root.expanduser().resolve()
        if resolved_temp is not None and not resolved_temp.is_dir():
            raise ValueError("temp_root must identify an existing directory")
        resolved_results = result_root.expanduser().resolve()
        if (
            not resolved_results.is_dir()
            or result_root.is_symlink()
            or resolved_results != result_root.absolute()
        ):
            raise ValueError(
                "result_root must identify an existing canonical non-symlink directory"
            )
        inspected = subprocess.run(
            [docker_executable, "image", "inspect", image, "--format", "{{.Os}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if inspected.returncode != 0 or inspected.stdout.strip() != "linux":
            raise RuntimeError("pinned Linux Sandbox image is unavailable locally")
        self._artifact_root = root
        self._image = image
        self._docker = docker_executable
        self._temp_root = resolved_temp
        self._result_root = resolved_results
        self._argument_builder = argument_builder
        self._active: dict[_RunKey, _ContainerControl] = {}
        self._active_lock = threading.Lock()

    async def compile_and_test(
        self,
        request: CompileAndTestRequest,
        context: OperationContext,
    ) -> Result[CertificationEvidence]:
        del request, context
        return _failure(
            "DEPENDENCY_UNAVAILABLE",
            "The runtime Sandbox does not own the certified build pipeline.",
            {"reason": "BUILD_PIPELINE_NOT_CONFIGURED"},
        )

    async def run(
        self,
        request: SandboxRunRequest,
        context: OperationContext,
    ) -> Result[SandboxRunResult]:
        identity_failure = self._validate_identity(request, context)
        if identity_failure is not None:
            return identity_failure
        key = self._run_key(request.run_id, context)
        identity = self._recovery_identity(request, context, key)
        durable = self._read_result_receipt(identity, request)
        if durable is not None:
            return durable
        self._ensure_launch_receipt(identity)
        name_hash = identity.run_key_sha256[:24]
        control = _ContainerControl(
            container_name=f"yaya-sbx-{name_hash}",
            docker_executable=self._docker,
            cancelled=threading.Event(),
        )
        with self._active_lock:
            if key in self._active:
                return _failure(
                    "INVALID_REQUEST",
                    "A Sandbox run with this identity is already active.",
                    {"reason": "DUPLICATE_ACTIVE_RUN", "run_id": request.run_id},
                )
            self._active[key] = control
        try:
            return await asyncio.to_thread(self._run_sync, request, control, identity)
        except asyncio.CancelledError:
            await asyncio.to_thread(control.terminate)
            raise
        finally:
            with self._active_lock:
                if self._active.get(key) is control:
                    del self._active[key]

    async def reconcile(
        self,
        request: SandboxRunRequest,
        context: OperationContext,
    ) -> Result[SandboxRunResult] | None:
        """Recover one terminal outcome without dispatching another logical run."""

        identity_failure = self._validate_identity(request, context)
        if identity_failure is not None:
            return identity_failure
        key = self._run_key(request.run_id, context)
        identity = self._recovery_identity(request, context, key)
        durable = self._read_result_receipt(identity, request)
        if durable is not None:
            return durable
        self._ensure_launch_receipt(identity)
        container_name = f"yaya-sbx-{identity.run_key_sha256[:24]}"
        return await asyncio.to_thread(
            self._reconcile_container,
            request,
            identity,
            container_name,
        )

    async def cancel(
        self,
        run_id: str,
        reason_code: str,
        context: OperationContext,
    ) -> Result[None]:
        del reason_code
        key = self._run_key(run_id, context)
        with self._active_lock:
            control = self._active.get(key)
        if control is not None:
            await asyncio.to_thread(control.terminate)
        return Success(None)

    @staticmethod
    def _run_key(run_id: str, context: OperationContext) -> _RunKey:
        return (
            context.actor.tenant_id,
            context.actor.actor_id,
            context.content_ref.unit_id,
            context.content_ref.version,
            context.content_ref.content_hash,
            run_id,
        )

    @staticmethod
    def _validate_identity(
        request: SandboxRunRequest,
        context: OperationContext,
    ) -> Failure | None:
        snapshot_context = request.world_snapshot.request_context
        snapshot_actor = snapshot_context.actor
        current_actor = context.actor
        if (
            snapshot_actor.tenant_id,
            snapshot_actor.actor_id,
            snapshot_actor.actor_type,
        ) != (
            current_actor.tenant_id,
            current_actor.actor_id,
            current_actor.actor_type,
        ):
            return _failure(
                "AUTHORIZATION_DENIED",
                "Sandbox World identity does not match the authenticated actor.",
                {"reason": "WORLD_ACTOR_MISMATCH"},
            )
        if snapshot_context.content_ref != context.content_ref:
            return _failure(
                "CONTENT_VERSION_MISMATCH",
                "Sandbox World content version does not match the operation.",
                {"reason": "WORLD_CONTENT_MISMATCH"},
            )
        return None

    def _run_sync(
        self,
        request: SandboxRunRequest,
        control: _ContainerControl,
        identity: _RecoveryIdentity,
    ) -> Result[SandboxRunResult]:
        outcome: Result[SandboxRunResult]
        stdout = b""
        stderr = b""
        durable = False
        try:
            artifact = self._resolve_verified_artifact(request)
            arguments = tuple(self._argument_builder(request))
            if any(not isinstance(item, str) or "\x00" in item for item in arguments):
                raise _SandboxRejected(
                    "INVALID_REQUEST",
                    "Sandbox arguments must be NUL-free strings.",
                    {"reason": "INVALID_ARGUMENT"},
                )
            execution = self._execute(
                artifact,
                arguments,
                request,
                control,
                identity,
            )
            stdout = execution.stdout
            stderr = execution.stderr
            outcome = Success(execution.result)
        except _SandboxRejected as error:
            outcome = _failure(error.code, error.message, error.details)
        except SandboxResultIntegrityError:
            raise
        except SandboxOutcomeUnknownError:
            recovered = self._reconcile_container(
                request,
                identity,
                control.container_name,
                dispatch_if_absent=False,
            )
            if recovered is not None:
                return recovered
            raise
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            try:
                recovered = self._reconcile_container(
                    request,
                    identity,
                    control.container_name,
                    dispatch_if_absent=False,
                )
            except (OSError, subprocess.SubprocessError) as recovery_error:
                raise SandboxOutcomeUnknownError(
                    "Docker infrastructure is unavailable before terminal reconciliation"
                ) from recovery_error
            if recovered is not None:
                return recovered
            raise SandboxOutcomeUnknownError(
                "Docker control acknowledgement is unavailable and no outcome is terminal"
            ) from error
        except Exception as error:  # defensive adapter boundary
            outcome = _failure(
                "INTERNAL_ERROR",
                "The container Sandbox failed at its adapter boundary.",
                {"reason": type(error).__name__},
            )
        try:
            self._write_result_receipt(identity, request, outcome, stdout, stderr)
            durable = True
            return outcome
        finally:
            if durable:
                self._remove_completed_container(control.container_name, identity)

    def _resolve_verified_artifact(self, request: SandboxRunRequest) -> Path:
        digest = request.skill_ref.artifact_sha256
        candidates = tuple(
            candidate
            for candidate in (
                self._artifact_root / digest[:2] / digest,
                self._artifact_root / digest,
            )
            if candidate.exists()
        )
        if len(candidates) != 1:
            raise _SandboxRejected(
                "ACTIVE_SKILL_ARTIFACT_MISMATCH",
                "Certified artifact resolution was missing or ambiguous.",
                {"reason": "ARTIFACT_RESOLUTION", "candidate_count": len(candidates)},
            )
        source = candidates[0]
        if source.is_symlink() or not source.is_file() or source.name != digest:
            raise _SandboxRejected(
                "ACTIVE_SKILL_ARTIFACT_MISMATCH",
                "Certified artifact is not a regular content-addressed file.",
                {"reason": "INVALID_ARTIFACT_PATH"},
            )
        resolved = source.resolve(strict=True)
        try:
            resolved.relative_to(self._artifact_root)
        except ValueError as error:
            raise _SandboxRejected(
                "ACTIVE_SKILL_ARTIFACT_MISMATCH",
                "Certified artifact escaped the configured artifact root.",
                {"reason": "ARTIFACT_PATH_ESCAPE"},
            ) from error
        if resolved != source.absolute() or resolved.name != digest:
            raise _SandboxRejected(
                "ACTIVE_SKILL_ARTIFACT_MISMATCH",
                "Certified artifact path is not canonical.",
                {"reason": "NON_CANONICAL_ARTIFACT_PATH"},
            )
        if resolved.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise _SandboxRejected(
                "ACTIVE_SKILL_ARTIFACT_MISMATCH",
                "Certified artifact must be read-only before execution.",
                {"reason": "WRITABLE_ARTIFACT"},
            )
        actual = hashlib.sha256()
        with resolved.open("rb") as reader:
            while chunk := reader.read(1024 * 1024):
                actual.update(chunk)
        if actual.hexdigest() != digest:
            raise _SandboxRejected(
                "ACTIVE_SKILL_ARTIFACT_MISMATCH",
                "Certified artifact bytes do not match SkillRef.artifact_sha256.",
                {"reason": "ARTIFACT_DIGEST_MISMATCH"},
            )
        return resolved

    def _execute(
        self,
        artifact: Path,
        arguments: Sequence[str],
        request: SandboxRunRequest,
        control: _ContainerControl,
        identity: _RecoveryIdentity,
    ) -> _SuccessfulExecution:
        started_at = datetime.now(UTC)
        started_clock = time.monotonic_ns()
        inspected = self._ensure_recovery_container(
            artifact,
            arguments,
            request,
            identity,
            control.container_name,
        )
        if _container_state(inspected).get("Status") != "created":
            raise SandboxOutcomeUnknownError(
                "the exact Sandbox container was already dispatched and must be reconciled"
            )
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        readers: tuple[threading.Thread, ...] = ()
        overflow = threading.Event()
        output_lock = threading.Lock()
        total_output = 0

        def drain(stream: BinaryIO, target: bytearray) -> None:
            nonlocal total_output
            while chunk := stream.read(4096):
                with output_lock:
                    remaining = request.limits.max_output_bytes - total_output
                    accepted = chunk[: max(0, remaining)]
                    target.extend(accepted)
                    total_output += len(accepted)
                    exceeded = len(accepted) != len(chunk)
                if exceeded:
                    overflow.set()
                    control.terminate()
                    return

        try:
            if control.cancelled.is_set():
                raise _SandboxRejected(
                    "SANDBOX_RUNTIME_ERROR",
                    "Sandbox run was cancelled before container start.",
                    {"reason": "CANCELLED"},
                )
            process = subprocess.Popen(
                [self._docker, "start", "--attach", control.container_name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            control.process = process
            if process.stdout is None or process.stderr is None:
                control.terminate()
                raise SandboxOutcomeUnknownError(
                    "docker start acknowledgement did not expose attach streams"
                )
            readers = (
                threading.Thread(target=drain, args=(process.stdout, stdout_buffer), daemon=True),
                threading.Thread(target=drain, args=(process.stderr, stderr_buffer), daemon=True),
            )
            for reader in readers:
                reader.start()
            try:
                exit_code = process.wait(timeout=request.limits.wall_ms / 1000)
            except subprocess.TimeoutExpired as error:
                control.terminate()
                process.wait(timeout=10)
                raise _SandboxRejected(
                    "SANDBOX_RESOURCE_LIMIT",
                    "Sandbox exceeded its wall-clock limit.",
                    {"reason": "WALL_TIMEOUT", "wall_ms": request.limits.wall_ms},
                ) from error
            for reader in readers:
                reader.join(timeout=5)
            if any(reader.is_alive() for reader in readers):
                control.terminate()
                raise SandboxOutcomeUnknownError(
                    "Sandbox attach pipes did not close after Docker control returned"
                )
            if overflow.is_set():
                raise _SandboxRejected(
                    "SANDBOX_RESOURCE_LIMIT",
                    "Sandbox output exceeded its combined byte limit.",
                    {"reason": "OUTPUT_LIMIT"},
                )
            if control.cancelled.is_set():
                raise _SandboxRejected(
                    "SANDBOX_RUNTIME_ERROR",
                    "Sandbox run was cancelled.",
                    {"reason": "CANCELLED"},
                )
            if exit_code != 0:
                current = self._inspect_container(control.container_name)
                if current is None:
                    raise SandboxOutcomeUnknownError(
                        "Sandbox container disappeared after docker start returned"
                    )
                self._validate_recovery_container(current, identity)
                state = _container_state(current)
                if state.get("Status") != "exited":
                    raise SandboxOutcomeUnknownError(
                        "docker start acknowledgement was lost before terminal state"
                    )
                actual_exit = state.get("ExitCode")
                if actual_exit == 0:
                    raise SandboxOutcomeUnknownError(
                        "docker start returned failure after the Sandbox exited successfully"
                    )
                if state.get("OOMKilled") is True:
                    raise _SandboxRejected(
                        "SANDBOX_RESOURCE_LIMIT",
                        "Sandbox exceeded its memory limit.",
                        {"reason": "MEMORY_LIMIT"},
                    )
                if isinstance(actual_exit, bool) or not isinstance(actual_exit, int):
                    raise SandboxResultIntegrityError(
                        "Sandbox container exit code is invalid after docker start"
                    )
                raise _SandboxRejected(
                    "SANDBOX_RUNTIME_ERROR",
                    "Sandbox executable returned a non-zero exit code.",
                    {"reason": "NON_ZERO_EXIT", "exit_code": actual_exit},
                )
            intents = _parse_action_intents(bytes(stdout_buffer), request)
            finished_at = datetime.now(UTC)
            wall_ms = max(0, (time.monotonic_ns() - started_clock) // 1_000_000)
            return _SuccessfulExecution(
                SandboxRunResult(
                    run_id=request.run_id,
                    started_at=started_at,
                    finished_at=finished_at,
                    action_intents=intents,
                    stdout_ref=None,
                    stderr_ref=None,
                    usage=SandboxUsage(cpu_ms=0, wall_ms=wall_ms, peak_memory_bytes=0),
                    evidence_refs=(),
                ),
                bytes(stdout_buffer),
                bytes(stderr_buffer),
            )
        finally:
            process = control.process
            if process is not None and process.poll() is None:
                control.terminate()
            for reader in readers:
                reader.join(timeout=5)
            if process is not None:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def _ensure_recovery_container(
        self,
        artifact: Path,
        arguments: Sequence[str],
        request: SandboxRunRequest,
        identity: _RecoveryIdentity,
        container_name: str,
    ) -> Mapping[str, object]:
        existing = self._inspect_container(container_name)
        if existing is not None:
            self._validate_recovery_container(existing, identity)
            return existing
        cpu_fraction = max(
            0.01,
            min(1.0, request.limits.cpu_ms / max(1, request.limits.wall_ms)),
        )
        tmpfs_size = max(1_048_576, min(67_108_864, request.limits.memory_bytes // 4))
        mount = f"type=bind,source={artifact},target=/opt/yaya/skill,readonly"
        create_command = [
            self._docker,
            "create",
            "--pull=never",
            "--name",
            container_name,
            "--label",
            "local.yaya.sandbox=true",
            "--label",
            f"local.yaya.run_id={request.run_id}",
            "--label",
            f"local.yaya.recovery_protocol={_RECOVERY_PROTOCOL}",
            "--label",
            f"local.yaya.run_key_sha256={identity.run_key_sha256}",
            "--label",
            f"local.yaya.request_sha256={identity.request_sha256}",
            "--log-driver",
            "local",
            "--log-opt",
            "compress=false",
            "--log-opt",
            "max-file=1",
            "--log-opt",
            f"max-size={request.limits.max_output_bytes + 1}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--user",
            "65534:65534",
            "--pids-limit",
            str(request.limits.max_processes),
            "--memory",
            str(request.limits.memory_bytes),
            "--memory-swap",
            str(request.limits.memory_bytes),
            "--cpus",
            f"{cpu_fraction:.3f}",
            "--ipc",
            "none",
            "--ulimit",
            "core=0:0",
            "--ulimit",
            "nofile=32:32",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={tmpfs_size}",
            "--workdir",
            "/tmp",
            "--env",
            f"YAYA_DETERMINISTIC_SEED={request.deterministic_seed}",
            "--mount",
            mount,
            "--entrypoint",
            "/opt/yaya/skill",
            self._image,
            *arguments,
        ]
        created = subprocess.run(
            create_command,
            check=False,
            capture_output=True,
            timeout=30,
        )
        inspected = self._inspect_container(container_name)
        if inspected is None:
            raise SandboxOutcomeUnknownError(
                "docker create did not leave an observable Sandbox container"
            )
        self._validate_recovery_container(inspected, identity)
        if created.returncode != 0 and _container_state(inspected).get("Status") == "removing":
            raise SandboxOutcomeUnknownError(
                "docker create acknowledgement was lost while the container was removing"
            )
        return inspected

    def _recovery_identity(
        self,
        request: SandboxRunRequest,
        context: OperationContext,
        key: _RunKey,
    ) -> _RecoveryIdentity:
        run_key_sha256 = hashlib.sha256(
            canonical_json_v1(
                {
                    "protocol": _RECOVERY_PROTOCOL,
                    "tenant_id": key[0],
                    "actor_id": key[1],
                    "content_unit_id": key[2],
                    "content_version": key[3],
                    "content_hash": key[4],
                    "run_id": key[5],
                }
            ).encode("utf-8")
        ).hexdigest()
        request_sha256 = canonical_json_sha256(
            _sandbox_request_projection(request, context, self._image)
        )
        labels = {
            "local.yaya.sandbox": "true",
            "local.yaya.run_id": request.run_id,
            "local.yaya.recovery_protocol": _RECOVERY_PROTOCOL,
            "local.yaya.run_key_sha256": run_key_sha256,
            "local.yaya.request_sha256": request_sha256,
        }
        return _RecoveryIdentity(
            run_key_sha256=run_key_sha256,
            request_sha256=request_sha256,
            launch_path=self._result_root / run_key_sha256[:2] / f"{run_key_sha256}.launch.json",
            receipt_path=self._result_root / run_key_sha256[:2] / f"{run_key_sha256}.json",
            labels=labels,
        )

    def _ensure_launch_receipt(self, identity: _RecoveryIdentity) -> None:
        payload: dict[str, object] = {
            "protocol": _RECOVERY_PROTOCOL,
            "run_key_sha256": identity.run_key_sha256,
            "request_sha256": identity.request_sha256,
        }
        envelope: dict[str, object] = {
            "version": 1,
            "payload": payload,
            "payload_sha256": canonical_json_sha256(payload),
        }
        content = canonical_json_v1(envelope).encode("utf-8")
        path = identity.launch_path
        shard = path.parent
        shard.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shard.is_symlink() or shard.resolve() != self._result_root / identity.run_key_sha256[:2]:
            raise SandboxResultIntegrityError("Sandbox launch shard escaped result_root")
        if path.exists():
            self._validate_launch_receipt(identity, content)
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{identity.run_key_sha256}.", suffix=".launch.tmp", dir=shard
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as writer:
                writer.write(content)
                writer.flush()
                os.fsync(writer.fileno())
            temporary.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            try:
                if os.name == "nt":
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
                _fsync_directory(shard)
            except FileExistsError:
                self._validate_launch_receipt(identity, content)
            self._validate_launch_receipt(identity, content)
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

    @staticmethod
    def _validate_launch_receipt(identity: _RecoveryIdentity, expected: bytes) -> None:
        path = identity.launch_path
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o222:
            raise SandboxResultIntegrityError("Sandbox launch receipt is not immutable")
        try:
            raw = path.read_bytes()
            decoded = json.loads(raw)
            if not isinstance(decoded, Mapping):
                raise ValueError("launch receipt is not an object")
            envelope = cast(Mapping[str, object], decoded)
            payload_value = envelope.get("payload")
            if not isinstance(payload_value, Mapping):
                raise ValueError("launch receipt payload is not an object")
            payload = cast(Mapping[str, object], payload_value)
            if (
                set(envelope) != {"version", "payload", "payload_sha256"}
                or envelope["version"] != 1
                or raw != canonical_json_v1(envelope).encode("utf-8")
                or envelope["payload_sha256"] != canonical_json_sha256(payload)
                or raw != expected
            ):
                raise ValueError("launch receipt bytes differ")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise SandboxResultIntegrityError("Sandbox launch receipt is corrupt") from error

    def _read_result_receipt(
        self,
        identity: _RecoveryIdentity,
        request: SandboxRunRequest,
    ) -> Result[SandboxRunResult] | None:
        path = identity.receipt_path
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o222:
            raise SandboxResultIntegrityError("Sandbox result receipt is not immutable")
        try:
            raw = path.read_bytes()
            decoded = json.loads(raw)
            if not isinstance(decoded, Mapping):
                raise ValueError("receipt is not an object")
            envelope = cast(Mapping[str, object], decoded)
            if set(envelope) != {"version", "payload", "payload_sha256"}:
                raise ValueError("receipt envelope fields differ")
            if envelope["version"] != 1:
                raise ValueError("receipt version differs")
            payload_value = envelope["payload"]
            if not isinstance(payload_value, Mapping):
                raise ValueError("receipt payload is not an object")
            payload = cast(Mapping[str, object], payload_value)
            expected_bytes = canonical_json_v1(envelope).encode("utf-8")
            if raw != expected_bytes:
                raise ValueError("receipt bytes are not canonical")
            if envelope["payload_sha256"] != canonical_json_sha256(payload):
                raise ValueError("receipt payload hash differs")
            if set(payload) != {
                "protocol",
                "run_key_sha256",
                "request_sha256",
                "outcome",
            }:
                raise ValueError("receipt payload fields differ")
            if (
                payload["protocol"] != _RECOVERY_PROTOCOL
                or payload["run_key_sha256"] != identity.run_key_sha256
                or payload["request_sha256"] != identity.request_sha256
            ):
                raise SandboxResultIntegrityError(
                    "Sandbox result receipt belongs to different request bytes"
                )
            outcome = payload["outcome"]
            if not isinstance(outcome, Mapping):
                raise ValueError("receipt outcome is not an object")
            return _decode_result_outcome(cast(Mapping[str, object], outcome), request)
        except SandboxResultIntegrityError:
            raise
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise SandboxResultIntegrityError("Sandbox result receipt is corrupt") from error

    def _write_result_receipt(
        self,
        identity: _RecoveryIdentity,
        request: SandboxRunRequest,
        outcome: Result[SandboxRunResult],
        stdout: bytes,
        stderr: bytes,
    ) -> None:
        encoded_outcome = _encode_result_outcome(outcome, request, stdout, stderr)
        payload: dict[str, object] = {
            "protocol": _RECOVERY_PROTOCOL,
            "run_key_sha256": identity.run_key_sha256,
            "request_sha256": identity.request_sha256,
            "outcome": encoded_outcome,
        }
        envelope: dict[str, object] = {
            "version": 1,
            "payload": payload,
            "payload_sha256": canonical_json_sha256(payload),
        }
        content = canonical_json_v1(envelope).encode("utf-8")
        path = identity.receipt_path
        shard = path.parent
        shard.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shard.is_symlink() or shard.resolve() != self._result_root / identity.run_key_sha256[:2]:
            raise SandboxResultIntegrityError("Sandbox result shard escaped result_root")
        if path.exists():
            existing = self._read_result_receipt(identity, request)
            if existing != outcome or path.read_bytes() != content:
                raise SandboxResultIntegrityError("Sandbox result receipt conflicts with replay")
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{identity.run_key_sha256}.", suffix=".tmp", dir=shard
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as writer:
                writer.write(content)
                writer.flush()
                os.fsync(writer.fileno())
            temporary.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            try:
                if os.name == "nt":
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
                _fsync_directory(shard)
            except FileExistsError:
                existing = self._read_result_receipt(identity, request)
                if existing != outcome or path.read_bytes() != content:
                    raise SandboxResultIntegrityError(
                        "concurrent Sandbox result receipt conflicts with replay"
                    )
            durable = self._read_result_receipt(identity, request)
            if durable != outcome:
                raise SandboxResultIntegrityError("Sandbox result receipt failed reconciliation")
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

    def _inspect_container(self, container_name: str) -> Mapping[str, object] | None:
        inspected = subprocess.run(
            [self._docker, "inspect", container_name, "--format", "{{json .}}"],
            check=False,
            capture_output=True,
            timeout=15,
        )
        if inspected.returncode != 0:
            stderr = inspected.stderr.decode("utf-8", errors="replace").lower()
            if "no such object:" in stderr or "no such container:" in stderr:
                return None
            raise SandboxOutcomeUnknownError(
                "Docker container state is unavailable for exact reconciliation"
            )
        try:
            value = json.loads(inspected.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SandboxResultIntegrityError("Sandbox container inspect is invalid") from error
        if not isinstance(value, Mapping):
            raise SandboxResultIntegrityError("Sandbox container inspect is not an object")
        return cast(Mapping[str, object], value)

    def _validate_recovery_container(
        self,
        inspected: Mapping[str, object],
        identity: _RecoveryIdentity,
    ) -> None:
        config_value = inspected.get("Config")
        host_value = inspected.get("HostConfig")
        if not isinstance(config_value, Mapping) or not isinstance(host_value, Mapping):
            raise SandboxResultIntegrityError("Sandbox container has no security projection")
        config = cast(Mapping[str, object], config_value)
        host = cast(Mapping[str, object], host_value)
        labels_value = config.get("Labels")
        if not isinstance(labels_value, Mapping):
            raise SandboxResultIntegrityError("Sandbox container has no recovery labels")
        labels = cast(Mapping[object, object], labels_value)
        actual_yaya = {
            key: value
            for key, value in labels.items()
            if isinstance(key, str) and key.startswith("local.yaya.")
        }
        log_value = host.get("LogConfig")
        if not isinstance(log_value, Mapping):
            raise SandboxResultIntegrityError("Sandbox container has no bounded log config")
        log_config = cast(Mapping[str, object], log_value)
        if (
            actual_yaya != dict(identity.labels)
            or config.get("Image") != self._image
            or config.get("User") != "65534:65534"
            or config.get("WorkingDir") != "/tmp"
            or host.get("NetworkMode") != "none"
            or host.get("ReadonlyRootfs") is not True
            or host.get("Privileged") is not False
            or log_config.get("Type") != "local"
        ):
            raise SandboxResultIntegrityError(
                "Sandbox container identity or security projection drifted"
            )

    def _reconcile_container(
        self,
        request: SandboxRunRequest,
        identity: _RecoveryIdentity,
        container_name: str,
        *,
        dispatch_if_absent: bool = True,
    ) -> Result[SandboxRunResult] | None:
        durable = self._read_result_receipt(identity, request)
        if durable is not None:
            return durable
        inspected = self._inspect_container(container_name)
        if inspected is None:
            if not dispatch_if_absent:
                return None
            artifact = self._resolve_verified_artifact(request)
            arguments = tuple(self._argument_builder(request))
            if any(not isinstance(item, str) or "\x00" in item for item in arguments):
                outcome = _failure(
                    "INVALID_REQUEST",
                    "Sandbox arguments must be NUL-free strings.",
                    {"reason": "INVALID_ARGUMENT"},
                )
                self._write_result_receipt(identity, request, outcome, b"", b"")
                return outcome
            inspected = self._ensure_recovery_container(
                artifact,
                arguments,
                request,
                identity,
                container_name,
            )
        self._validate_recovery_container(inspected, identity)
        state = _container_state(inspected)
        status = state.get("Status")
        timed_out = False
        if status == "created":
            started = subprocess.run(
                [self._docker, "start", container_name],
                check=False,
                capture_output=True,
                timeout=30,
            )
            inspected = self._inspect_container(container_name)
            if inspected is None:
                raise SandboxResultIntegrityError(
                    "Sandbox container disappeared after recovery start"
                )
            self._validate_recovery_container(inspected, identity)
            state = _container_state(inspected)
            status = state.get("Status")
            if started.returncode != 0 and status == "created":
                return None
        if status == "running":
            started_at = _docker_timestamp(state.get("StartedAt"), "StartedAt")
            elapsed = max(0.0, (datetime.now(UTC) - started_at).total_seconds())
            remaining = max(0.05, request.limits.wall_ms / 1000 - elapsed)
            waited: subprocess.CompletedProcess[bytes] | None = None
            try:
                waited = subprocess.run(
                    [self._docker, "wait", container_name],
                    check=False,
                    capture_output=True,
                    timeout=remaining,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                subprocess.run(
                    [self._docker, "kill", container_name],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            inspected = self._inspect_container(container_name)
            if inspected is None:
                raise SandboxResultIntegrityError(
                    "Sandbox container disappeared before outcome reconciliation"
                )
            self._validate_recovery_container(inspected, identity)
            state = _container_state(inspected)
            status = state.get("Status")
            if waited is not None and waited.returncode != 0 and status == "running":
                return None
        if status != "exited":
            return None
        logs = subprocess.run(
            [self._docker, "logs", container_name],
            check=False,
            capture_output=True,
            timeout=15,
        )
        if logs.returncode != 0:
            return None
        stdout = logs.stdout
        stderr = logs.stderr
        started_at = _docker_timestamp(state.get("StartedAt"), "StartedAt")
        finished_at = _docker_timestamp(state.get("FinishedAt"), "FinishedAt")
        wall_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
        raw_exit = state.get("ExitCode")
        if isinstance(raw_exit, bool) or not isinstance(raw_exit, int):
            raise SandboxResultIntegrityError("Sandbox container exit code is invalid")
        if timed_out:
            outcome: Result[SandboxRunResult] = _failure(
                "SANDBOX_RESOURCE_LIMIT",
                "Sandbox exceeded its wall-clock limit.",
                {"reason": "WALL_TIMEOUT", "wall_ms": request.limits.wall_ms},
            )
        elif len(stdout) + len(stderr) > request.limits.max_output_bytes:
            outcome = _failure(
                "SANDBOX_RESOURCE_LIMIT",
                "Sandbox output exceeded its combined byte limit.",
                {"reason": "OUTPUT_LIMIT"},
            )
        elif state.get("OOMKilled") is True:
            outcome = _failure(
                "SANDBOX_RESOURCE_LIMIT",
                "Sandbox exceeded its memory limit.",
                {"reason": "MEMORY_LIMIT"},
            )
        elif raw_exit != 0:
            outcome = _failure(
                "SANDBOX_RUNTIME_ERROR",
                "Sandbox executable returned a non-zero exit code.",
                {"reason": "NON_ZERO_EXIT", "exit_code": raw_exit},
            )
        else:
            try:
                intents = _parse_action_intents(stdout, request)
                outcome = Success(
                    SandboxRunResult(
                        run_id=request.run_id,
                        started_at=started_at,
                        finished_at=finished_at,
                        action_intents=intents,
                        stdout_ref=None,
                        stderr_ref=None,
                        usage=SandboxUsage(
                            cpu_ms=0,
                            wall_ms=wall_ms,
                            peak_memory_bytes=0,
                        ),
                        evidence_refs=(),
                    )
                )
            except _SandboxRejected as error:
                outcome = _failure(error.code, error.message, error.details)
        self._write_result_receipt(identity, request, outcome, stdout, stderr)
        self._remove_completed_container(container_name, identity)
        return outcome

    def _remove_completed_container(
        self,
        container_name: str,
        identity: _RecoveryIdentity,
    ) -> None:
        inspected = self._inspect_container(container_name)
        if inspected is None:
            return
        self._validate_recovery_container(inspected, identity)
        removed = subprocess.run(
            [self._docker, "rm", "--force", container_name],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if removed.returncode != 0 and self._inspect_container(container_name) is not None:
            raise RuntimeError("durable Sandbox container cleanup did not complete")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _request_context_projection(context: object) -> dict[str, object]:
    request = cast(OperationContext, context)
    return {
        "request_id": request.request_id,
        "correlation_id": request.correlation_id,
        "trace_id": request.trace_id,
        "requested_at": _iso(request.requested_at),
        "actor": {
            "tenant_id": request.actor.tenant_id,
            "actor_id": request.actor.actor_id,
            "actor_type": request.actor.actor_type.value,
            "roles": list(request.actor.roles),
        },
        "content_ref": {
            "unit_id": request.content_ref.unit_id,
            "version": request.content_ref.version,
            "content_hash": request.content_ref.content_hash,
        },
        "schema_version": request.schema_version,
        "command_id": request.command_id,
        "causation_id": request.causation_id,
        "deadline_at": _iso(request.deadline_at),
    }


def _snapshot_context_projection(request: SandboxRunRequest) -> dict[str, object]:
    context = request.world_snapshot.request_context
    return {
        "request_id": context.request_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "requested_at": _iso(context.requested_at),
        "actor": {
            "tenant_id": context.actor.tenant_id,
            "actor_id": context.actor.actor_id,
            "actor_type": context.actor.actor_type.value,
            "roles": list(context.actor.roles),
        },
        "content_ref": {
            "unit_id": context.content_ref.unit_id,
            "version": context.content_ref.version,
            "content_hash": context.content_ref.content_hash,
        },
        "schema_version": context.schema_version,
    }


def _sandbox_request_projection(
    request: SandboxRunRequest,
    context: OperationContext,
    image: str,
) -> dict[str, object]:
    limits = request.limits
    snapshot = request.world_snapshot
    return {
        "protocol": _RECOVERY_PROTOCOL,
        "image": image,
        "request": {
            "run_id": request.run_id,
            "skill_ref": {
                "skill_id": request.skill_ref.skill_id,
                "skill_version_id": request.skill_ref.skill_version_id,
                "artifact_sha256": request.skill_ref.artifact_sha256,
                "certification_id": request.skill_ref.certification_id,
            },
            "world_id": request.world_id,
            "world_snapshot": {
                "request_context": _snapshot_context_projection(request),
                "world_id": snapshot.world_id,
                "revision": snapshot.revision,
                "last_event_sequence": snapshot.last_event_sequence,
                "state_hash": snapshot.state_hash,
                "generated_at": _iso(snapshot.generated_at),
                "world_rules_version": snapshot.world_rules_version,
                "state": snapshot.state,
                "state_schema_version": snapshot.state_schema_version,
            },
            "input": request.input,
            "deterministic_seed": request.deterministic_seed,
            "limits": {
                "cpu_ms": limits.cpu_ms,
                "wall_ms": limits.wall_ms,
                "memory_bytes": limits.memory_bytes,
                "max_intents": limits.max_intents,
                "max_output_bytes": limits.max_output_bytes,
                "max_processes": limits.max_processes,
                "network_access": limits.network_access,
            },
        },
        "context": _request_context_projection(context),
    }


def _encode_result_outcome(
    outcome: Result[SandboxRunResult],
    request: SandboxRunRequest,
    stdout: bytes,
    stderr: bytes,
) -> dict[str, object]:
    if isinstance(outcome, Success):
        result = outcome.value
        if (
            not isinstance(result, SandboxRunResult)
            or result.run_id != request.run_id
            or result.stdout_ref is not None
            or result.stderr_ref is not None
            or result.evidence_refs
            or result.status != "SUCCEEDED"
            or result.exit_code != 0
            or len(stdout) + len(stderr) > request.limits.max_output_bytes
            or _parse_action_intents(stdout, request) != result.action_intents
        ):
            raise SandboxResultIntegrityError(
                "Sandbox success cannot be represented by its durable output bytes"
            )
        return {
            "kind": "SUCCESS",
            "run_id": result.run_id,
            "started_at": _iso(result.started_at),
            "finished_at": _iso(result.finished_at),
            "stdout": base64.b64encode(stdout).decode("ascii"),
            "stderr": base64.b64encode(stderr).decode("ascii"),
            "usage": {
                "cpu_ms": result.usage.cpu_ms,
                "wall_ms": result.usage.wall_ms,
                "peak_memory_bytes": result.usage.peak_memory_bytes,
            },
            "status": result.status,
            "exit_code": result.exit_code,
        }
    error = outcome.error
    return {
        "kind": "FAILURE",
        "error": {
            "code": error.code,
            "category": error.category.value,
            "retryable": error.retryable,
            "user_message_key": error.user_message_key,
            "stage": error.stage,
            "message": error.message,
            "details": error.details,
            "evidence_ids": list(error.evidence_ids),
        },
    }


def _decode_result_outcome(
    value: Mapping[str, object],
    request: SandboxRunRequest,
) -> Result[SandboxRunResult]:
    kind = value.get("kind")
    if kind == "SUCCESS":
        if set(value) != {
            "kind",
            "run_id",
            "started_at",
            "finished_at",
            "stdout",
            "stderr",
            "usage",
            "status",
            "exit_code",
        }:
            raise ValueError("Sandbox success receipt fields differ")
        stdout_value = value["stdout"]
        stderr_value = value["stderr"]
        usage_value = value["usage"]
        if (
            not isinstance(stdout_value, str)
            or not isinstance(stderr_value, str)
            or not isinstance(usage_value, Mapping)
            or value["run_id"] != request.run_id
            or value["status"] != "SUCCEEDED"
            or value["exit_code"] != 0
        ):
            raise ValueError("Sandbox success receipt identity differs")
        stdout = base64.b64decode(stdout_value, validate=True)
        stderr = base64.b64decode(stderr_value, validate=True)
        if len(stdout) + len(stderr) > request.limits.max_output_bytes:
            raise ValueError("Sandbox success receipt exceeds output limit")
        usage = cast(Mapping[str, object], usage_value)
        if set(usage) != {"cpu_ms", "wall_ms", "peak_memory_bytes"}:
            raise ValueError("Sandbox success usage fields differ")
        if any(isinstance(usage[name], bool) or not isinstance(usage[name], int) for name in usage):
            raise ValueError("Sandbox success usage is invalid")
        return Success(
            SandboxRunResult(
                run_id=request.run_id,
                started_at=_receipt_timestamp(value["started_at"], "started_at"),
                finished_at=_receipt_timestamp(value["finished_at"], "finished_at"),
                action_intents=_parse_action_intents(stdout, request),
                stdout_ref=None,
                stderr_ref=None,
                usage=SandboxUsage(
                    cpu_ms=cast(int, usage["cpu_ms"]),
                    wall_ms=cast(int, usage["wall_ms"]),
                    peak_memory_bytes=cast(int, usage["peak_memory_bytes"]),
                ),
                evidence_refs=(),
            )
        )
    if kind == "FAILURE":
        if set(value) != {"kind", "error"} or not isinstance(value["error"], Mapping):
            raise ValueError("Sandbox failure receipt fields differ")
        raw = cast(Mapping[str, object], value["error"])
        if set(raw) != {
            "code",
            "category",
            "retryable",
            "user_message_key",
            "stage",
            "message",
            "details",
            "evidence_ids",
        }:
            raise ValueError("Sandbox failure error fields differ")
        details = raw["details"]
        evidence_ids = raw["evidence_ids"]
        if not isinstance(details, Mapping) or not isinstance(evidence_ids, list):
            raise ValueError("Sandbox failure error payload is invalid")
        return Failure(
            ContractError(
                code=cast(str, raw["code"]),
                category=ErrorCategory(cast(str, raw["category"])),
                retryable=cast(bool, raw["retryable"]),
                user_message_key=cast(str, raw["user_message_key"]),
                stage=cast(str, raw["stage"]),
                message=cast(str | None, raw["message"]),
                details=cast(FrozenJsonObject, details),
                evidence_ids=tuple(cast(list[str], evidence_ids)),
            )
        )
    raise ValueError("Sandbox outcome kind is invalid")


def _receipt_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include an offset")
    return parsed.astimezone(UTC)


def _docker_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise SandboxResultIntegrityError(f"Sandbox container {field_name} is invalid")
    normalized = value
    match = re.fullmatch(r"(.+\.\d{6})\d*(Z|[+-]\d\d:\d\d)", value)
    if match is not None:
        normalized = f"{match.group(1)}{match.group(2)}"
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise SandboxResultIntegrityError(f"Sandbox container {field_name} is invalid") from error
    if parsed.tzinfo is None:
        raise SandboxResultIntegrityError(f"Sandbox container {field_name} has no offset")
    return parsed.astimezone(UTC)


def _container_state(inspected: Mapping[str, object]) -> Mapping[str, object]:
    value = inspected.get("State")
    if not isinstance(value, Mapping):
        raise SandboxResultIntegrityError("Sandbox container state is invalid")
    return cast(Mapping[str, object], value)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["DockerCppSandbox", "SandboxResultIntegrityError"]
