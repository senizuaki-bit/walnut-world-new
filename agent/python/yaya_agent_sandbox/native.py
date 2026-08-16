"""Provider-neutral content-addressed native C++ Sandbox adapter.

The adapter deliberately has no World write capability.  A successful run
means only that a certified executable emitted a closed array of typed
``ActionIntent`` values.  World rules and persistence remain separate ports.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import importlib
import json
import os
import platform
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from yaya_agent_contracts import (
    ActionIntent,
    CompileAndTestRequest,
    ContractError,
    ErrorCategory,
    Failure,
    FrozenJsonObject,
    HarvestIntent,
    InteractIntent,
    MoveIntent,
    OperationContext,
    PlantIntent,
    Result,
    SandboxRunRequest,
    SandboxRunResult,
    SandboxUsage,
    SpeakIntent,
    Success,
    WaterIntent,
    WorldPosition,
)

type ArgumentBuilder = Callable[[SandboxRunRequest], Sequence[str]]
type _RunKey = tuple[str, str, str, str, str, str]


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _signal_number(name: str, default: int) -> int:
    return int(getattr(signal, name, default))


def _kill_process_group(pid: int) -> None:
    killpg = cast(Callable[[int, int], None], getattr(os, "killpg"))
    killpg(pid, _signal_number("SIGKILL", 9))


class _ResourceModule(Protocol):
    RLIMIT_CPU: int
    RLIMIT_AS: int
    RLIMIT_NPROC: int

    def setrlimit(self, resource: int, limits: tuple[int, int]) -> None: ...


class _SandboxRejected(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {} if details is None else dict(details)


_ERROR_METADATA: Mapping[str, tuple[ErrorCategory, bool, str]] = {
    "INVALID_REQUEST": (ErrorCategory.VALIDATION, False, "request.invalid"),
    "AUTHORIZATION_DENIED": (
        ErrorCategory.AUTHORIZATION,
        False,
        "auth.permission_denied",
    ),
    "CONTENT_VERSION_MISMATCH": (
        ErrorCategory.VALIDATION,
        False,
        "content.version_mismatch",
    ),
    "ACTIVE_SKILL_ARTIFACT_MISMATCH": (
        ErrorCategory.INVARIANT,
        False,
        "skill.artifact_mismatch",
    ),
    "SANDBOX_RUNTIME_ERROR": (
        ErrorCategory.SANDBOX,
        False,
        "sandbox.runtime_error",
    ),
    "SANDBOX_RESOURCE_LIMIT": (
        ErrorCategory.SANDBOX,
        False,
        "sandbox.resource_limit",
    ),
    "DEPENDENCY_UNAVAILABLE": (
        ErrorCategory.DEPENDENCY,
        True,
        "dependency.temporarily_unavailable",
    ),
    "INTERNAL_ERROR": (ErrorCategory.INTERNAL, False, "system.internal_error"),
}


def _failure(
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> Failure:
    category, retryable, user_message_key = _ERROR_METADATA[code]
    return Failure(
        ContractError(
            code=code,
            category=category,
            retryable=retryable,
            user_message_key=user_message_key,
            stage="SANDBOX",
            message=message,
            details=cast(FrozenJsonObject, {} if details is None else details),
        )
    )


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _exact_object(
    value: object,
    expected: set[str],
    field_name: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must contain exactly {sorted(expected)}")
    mapping = cast(Mapping[object, object], value)
    if set(mapping) != expected or not all(isinstance(key, str) for key in mapping):
        raise ValueError(f"{field_name} must contain exactly {sorted(expected)}")
    return cast(Mapping[str, object], mapping)


def _string(value: Mapping[str, object], key: str) -> str:
    item = value[key]
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string")
    return item


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value[key]
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"{key} must be an integer")
    return item


def _parse_intent(value: object) -> ActionIntent:
    if not isinstance(value, Mapping):
        raise ValueError("action intent must be an object")
    mapping = cast(Mapping[str, object], value)
    action_type = mapping.get("action_type")
    common = {"intent_id", "action_type", "actor_entity_id", "expected_world_revision"}
    if action_type == "MOVE":
        raw = _exact_object(mapping, common | {"destination"}, "MOVE intent")
        destination = _exact_object(raw["destination"], {"x", "y"}, "destination")
        return MoveIntent(
            intent_id=_string(raw, "intent_id"),
            actor_entity_id=_string(raw, "actor_entity_id"),
            expected_world_revision=_integer(raw, "expected_world_revision"),
            destination=WorldPosition(
                x=_integer(destination, "x"),
                y=_integer(destination, "y"),
            ),
        )
    if action_type == "PLANT":
        raw = _exact_object(mapping, common | {"plot_id", "crop_type"}, "PLANT intent")
        return PlantIntent(
            intent_id=_string(raw, "intent_id"),
            actor_entity_id=_string(raw, "actor_entity_id"),
            expected_world_revision=_integer(raw, "expected_world_revision"),
            plot_id=_string(raw, "plot_id"),
            crop_type=_string(raw, "crop_type"),
        )
    if action_type == "WATER":
        raw = _exact_object(mapping, common | {"plot_id", "amount_ml"}, "WATER intent")
        return WaterIntent(
            intent_id=_string(raw, "intent_id"),
            actor_entity_id=_string(raw, "actor_entity_id"),
            expected_world_revision=_integer(raw, "expected_world_revision"),
            plot_id=_string(raw, "plot_id"),
            amount_ml=_integer(raw, "amount_ml"),
        )
    if action_type == "HARVEST":
        raw = _exact_object(mapping, common | {"plot_id"}, "HARVEST intent")
        return HarvestIntent(
            intent_id=_string(raw, "intent_id"),
            actor_entity_id=_string(raw, "actor_entity_id"),
            expected_world_revision=_integer(raw, "expected_world_revision"),
            plot_id=_string(raw, "plot_id"),
        )
    if action_type == "INTERACT":
        raw = _exact_object(
            mapping,
            common | {"target_entity_id", "interaction"},
            "INTERACT intent",
        )
        return InteractIntent(
            intent_id=_string(raw, "intent_id"),
            actor_entity_id=_string(raw, "actor_entity_id"),
            expected_world_revision=_integer(raw, "expected_world_revision"),
            target_entity_id=_string(raw, "target_entity_id"),
            interaction=_string(raw, "interaction"),
        )
    if action_type == "SPEAK":
        raw = _exact_object(mapping, common | {"text", "audience"}, "SPEAK intent")
        audience = _string(raw, "audience")
        if audience not in ("LEARNER", "NEARBY_ENTITIES"):
            raise ValueError("audience is unsupported")
        return SpeakIntent(
            intent_id=_string(raw, "intent_id"),
            actor_entity_id=_string(raw, "actor_entity_id"),
            expected_world_revision=_integer(raw, "expected_world_revision"),
            text=_string(raw, "text"),
            audience=audience,
        )
    raise ValueError("action_type is not supported")


def _parse_action_intents(
    stdout: bytes,
    request: SandboxRunRequest,
) -> tuple[ActionIntent, ...]:
    try:
        text = stdout.decode("utf-8", errors="strict")
        decoded = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise _SandboxRejected(
            "SANDBOX_RUNTIME_ERROR",
            "Sandbox stdout is not one strict JSON document.",
            {"reason": "INVALID_JSON"},
        ) from error
    try:
        root = _exact_object(decoded, {"actions"}, "Sandbox output")
    except ValueError as error:
        raise _SandboxRejected(
            "SANDBOX_RUNTIME_ERROR",
            "Sandbox output must contain only the action-intent array.",
            {"reason": "INVALID_ACTION_ARRAY"},
        ) from error
    actions = root["actions"]
    if isinstance(actions, (str, bytes, bytearray)) or not isinstance(actions, Sequence):
        raise _SandboxRejected(
            "SANDBOX_RUNTIME_ERROR",
            "Sandbox output actions must be an array.",
            {"reason": "INVALID_ACTION_ARRAY"},
        )
    action_items = cast(Sequence[object], actions)
    if len(action_items) > request.limits.max_intents:
        raise _SandboxRejected(
            "SANDBOX_RESOURCE_LIMIT",
            "Sandbox emitted more action intents than allowed.",
            {
                "reason": "ACTION_LIMIT",
                "max_intents": request.limits.max_intents,
            },
        )
    try:
        parsed = tuple(_parse_intent(item) for item in action_items)
    except (TypeError, ValueError) as error:
        raise _SandboxRejected(
            "SANDBOX_RUNTIME_ERROR",
            "Sandbox emitted an invalid or open action intent.",
            {"reason": "INVALID_ACTION_INTENT"},
        ) from error
    intent_ids: set[str] = set()
    for intent in parsed:
        intent_id = cast(str, getattr(intent, "intent_id"))
        if intent_id in intent_ids:
            raise _SandboxRejected(
                "SANDBOX_RUNTIME_ERROR",
                "Sandbox emitted duplicate intent identifiers.",
                {"reason": "DUPLICATE_INTENT_ID"},
            )
        intent_ids.add(intent_id)
        if getattr(intent, "expected_world_revision") != request.world_snapshot.revision:
            raise _SandboxRejected(
                "SANDBOX_RUNTIME_ERROR",
                "Sandbox intent targets a different World revision.",
                {"reason": "INTENT_REVISION_MISMATCH"},
            )
    return parsed


def _default_arguments(request: SandboxRunRequest) -> Sequence[str]:
    """The certified watering ABI receives its loop length as one argument."""

    length = request.input.get("length")
    if isinstance(length, bool) or not isinstance(length, int):
        raise _SandboxRejected(
            "INVALID_REQUEST",
            "The watering Sandbox input requires an integer length.",
            {"reason": "INVALID_WATERING_LENGTH"},
        )
    return (str(length),)


def _minimal_environment(work_dir: Path, deterministic_seed: str) -> Mapping[str, str]:
    environment = {
        "YAYA_DETERMINISTIC_SEED": deterministic_seed,
        "TMP": str(work_dir),
        "TEMP": str(work_dir),
    }
    if _is_windows():
        system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if system_root:
            environment["SystemRoot"] = system_root
            environment["WINDIR"] = system_root
    else:
        environment["LANG"] = "C.UTF-8"
    return environment


class _WindowsJob:
    """Small Win32 Job Object wrapper used only on Windows."""

    _JOB_TIME = 0x00000004
    _ACTIVE_PROCESS = 0x00000008
    _JOB_MEMORY = 0x00000200
    _KILL_ON_CLOSE = 0x00002000
    _BASIC_ACCOUNTING_INFORMATION = 1
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, *, cpu_ms: int, memory_bytes: int, max_processes: int) -> None:
        if not _is_windows():
            raise RuntimeError("Windows Job Objects are unavailable")
        from ctypes import wintypes

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = (
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            )

        class _IoCounters(ctypes.Structure):
            _fields_ = tuple(
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            )

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = (
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            )

        class _BasicAccountingInformation(ctypes.Structure):
            _fields_ = (
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self._kernel32 = kernel32
        self._handle_type = wintypes.HANDLE
        self._handle = handle
        self._information_type = _ExtendedLimitInformation
        self._accounting_type = _BasicAccountingInformation
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.PerJobUserTimeLimit = cpu_ms * 10_000
        information.BasicLimitInformation.ActiveProcessLimit = max_processes
        information.BasicLimitInformation.LimitFlags = (
            self._JOB_TIME | self._ACTIVE_PROCESS | self._JOB_MEMORY | self._KILL_ON_CLOSE
        )
        information.JobMemoryLimit = memory_bytes
        configured = kernel32.SetInformationJobObject(
            handle,
            self._EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not configured:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            self._handle = None
            raise OSError(error, "SetInformationJobObject failed")

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        process_handle = getattr(process, "_handle", None)
        native_process_handle = (
            None if process_handle is None else self._handle_type(int(process_handle))
        )
        if native_process_handle is None or not self._kernel32.AssignProcessToJobObject(
            self._handle,
            native_process_handle,
        ):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def resume(self, process: subprocess.Popen[bytes]) -> None:
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise OSError("Sandbox process handle is unavailable")
        native_process_handle = self._handle_type(int(process_handle))
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtResumeProcess.argtypes = (self._handle_type,)
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = ntdll.NtResumeProcess(native_process_handle)
        if status != 0:
            raise OSError(status, "NtResumeProcess failed")

    def usage(self, process: subprocess.Popen[bytes]) -> tuple[int, int]:
        del process
        cpu_ms = 0
        accounting = self._accounting_type()
        if self._kernel32.QueryInformationJobObject(
            self._handle,
            self._BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            cpu_ms = (accounting.TotalKernelTime + accounting.TotalUserTime) // 10_000
        information = self._information_type()
        peak_memory_bytes = 0
        if self._kernel32.QueryInformationJobObject(
            self._handle,
            self._EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            peak_memory_bytes = int(information.PeakJobMemoryUsed)
        return int(cpu_ms), peak_memory_bytes

    def terminate(self) -> None:
        if self._handle is not None:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _posix_limit_setup(cpu_ms: int, memory_bytes: int, max_processes: int) -> Callable[[], None]:
    def apply() -> None:
        import math

        resource = cast(_ResourceModule, importlib.import_module("resource"))
        cpu_seconds = max(1, math.ceil(cpu_ms / 1000))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (max_processes, max_processes))

    return apply


def _read_linux_usage(pid: int) -> tuple[int, int]:
    """Read one process sample without adding a runtime dependency."""

    if not sys_platform_linux():
        return 0, 0
    try:
        stat_fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii").split()
        sysconf = cast(Callable[[str], int], getattr(os, "sysconf"))
        ticks_per_second = sysconf("SC_CLK_TCK")
        cpu_ms = (int(stat_fields[13]) + int(stat_fields[14])) * 1000 // ticks_per_second
        peak_memory_bytes = 0
        status = (Path("/proc") / str(pid) / "status").read_text(encoding="ascii")
        for line in status.splitlines():
            if line.startswith("VmHWM:"):
                peak_memory_bytes = int(line.split()[1]) * 1024
                break
        return cpu_ms, peak_memory_bytes
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return 0, 0


def sys_platform_linux() -> bool:
    return not _is_windows() and Path("/proc/self/stat").is_file()


@dataclass(slots=True)
class _RunControl:
    tenant_id: str
    actor_id: str
    content_identity: tuple[str, str, str]
    cancelled: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    process: subprocess.Popen[bytes] | None = None
    job: _WindowsJob | None = None

    def attach(
        self,
        process: subprocess.Popen[bytes],
        job: _WindowsJob | None,
    ) -> None:
        with self.lock:
            self.process = process
            self.job = job
            if self.cancelled.is_set():
                self._terminate_locked()

    def terminate(self) -> None:
        self.cancelled.set()
        with self.lock:
            self._terminate_locked()

    def _terminate_locked(self) -> None:
        if self.job is not None:
            self.job.terminate()
        if self.process is not None and self.process.poll() is None:
            if not _is_windows():
                try:
                    _kill_process_group(self.process.pid)
                except ProcessLookupError:
                    pass
            else:
                self.process.kill()


class ProductionCppSandbox:
    """Run certified native artifacts without importing them into the backend."""

    def __init__(
        self,
        artifact_root: Path,
        *,
        temp_root: Path | None = None,
        argument_builder: ArgumentBuilder = _default_arguments,
    ) -> None:
        root = artifact_root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError("artifact_root must identify an existing directory")
        if temp_root is not None:
            resolved_temp = temp_root.expanduser().resolve()
            if not resolved_temp.is_dir():
                raise ValueError("temp_root must identify an existing directory")
        else:
            resolved_temp = None
        self._artifact_root = root
        self._temp_root = resolved_temp
        self._argument_builder = argument_builder
        self._active: dict[_RunKey, _RunControl] = {}
        self._active_lock = threading.Lock()

    async def compile_and_test(
        self,
        request: CompileAndTestRequest,
        context: OperationContext,
    ) -> Result[object]:
        del request, context
        return _failure(
            "DEPENDENCY_UNAVAILABLE",
            "The runtime artifact Sandbox does not own the separate build pipeline.",
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
        control = _RunControl(
            context.actor.tenant_id,
            context.actor.actor_id,
            (
                context.content_ref.unit_id,
                context.content_ref.version,
                context.content_ref.content_hash,
            ),
        )
        with self._active_lock:
            if key in self._active:
                return _failure(
                    "INVALID_REQUEST",
                    "A Sandbox run with this identifier is already active.",
                    {"reason": "DUPLICATE_ACTIVE_RUN", "run_id": request.run_id},
                )
            self._active[key] = control
        try:
            return await asyncio.to_thread(self._run_sync, request, control)
        except asyncio.CancelledError:
            control.terminate()
            raise
        finally:
            with self._active_lock:
                if self._active.get(key) is control:
                    del self._active[key]

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
        if control is None:
            return Success(None)
        control.terminate()
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

    def _validate_identity(
        self,
        request: SandboxRunRequest,
        context: OperationContext,
    ) -> Failure | None:
        snapshot_context = request.world_snapshot.request_context
        if snapshot_context.actor != context.actor:
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
        control: _RunControl,
    ) -> Result[SandboxRunResult]:
        try:
            with tempfile.TemporaryDirectory(
                prefix="yaya-sandbox-",
                dir=self._temp_root,
            ) as raw_work_dir:
                work_dir = Path(raw_work_dir).resolve()
                executable = self._materialize_verified_artifact(request, work_dir)
                arguments = tuple(self._argument_builder(request))
                if any(not isinstance(item, str) or "\x00" in item for item in arguments):
                    raise _SandboxRejected(
                        "INVALID_REQUEST",
                        "Sandbox arguments must be NUL-free strings.",
                        {"reason": "INVALID_ARGUMENT"},
                    )
                return Success(self._execute(executable, arguments, work_dir, request, control))
        except _SandboxRejected as error:
            return _failure(error.code, error.message, error.details)
        except (OSError, subprocess.SubprocessError) as error:
            return _failure(
                "DEPENDENCY_UNAVAILABLE",
                "The native Sandbox process could not be started safely.",
                {"reason": type(error).__name__},
            )
        except Exception as error:  # defensive adapter boundary
            return _failure(
                "INTERNAL_ERROR",
                "The native Sandbox failed at its adapter boundary.",
                {"reason": type(error).__name__},
            )

    def _artifact_candidates(self, digest: str) -> tuple[Path, ...]:
        candidates = (
            self._artifact_root / digest[:2] / digest,
            self._artifact_root / digest,
        )
        return tuple(candidate for candidate in candidates if candidate.exists())

    def _materialize_verified_artifact(
        self,
        request: SandboxRunRequest,
        work_dir: Path,
    ) -> Path:
        digest = request.skill_ref.artifact_sha256
        candidates = self._artifact_candidates(digest)
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
        if resolved.name != digest or resolved != source.absolute():
            raise _SandboxRejected(
                "ACTIVE_SKILL_ARTIFACT_MISMATCH",
                "Certified artifact path is not canonical.",
                {"reason": "NON_CANONICAL_ARTIFACT_PATH"},
            )
        mode = resolved.stat().st_mode
        if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise _SandboxRejected(
                "ACTIVE_SKILL_ARTIFACT_MISMATCH",
                "Certified artifact must be read-only before execution.",
                {"reason": "WRITABLE_ARTIFACT"},
            )
        destination = work_dir / ("skill.exe" if _is_windows() else "skill")
        actual = hashlib.sha256()
        with resolved.open("rb") as reader, destination.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
                actual.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if actual.hexdigest() != digest:
            raise _SandboxRejected(
                "ACTIVE_SKILL_ARTIFACT_MISMATCH",
                "Certified artifact bytes do not match SkillRef.artifact_sha256.",
                {"reason": "ARTIFACT_DIGEST_MISMATCH"},
            )
        destination.chmod(stat.S_IREAD | (0 if _is_windows() else stat.S_IXUSR))
        copied_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        if copied_digest != digest:
            raise _SandboxRejected(
                "ACTIVE_SKILL_ARTIFACT_MISMATCH",
                "Isolated artifact copy failed its post-copy digest check.",
                {"reason": "COPIED_ARTIFACT_DIGEST_MISMATCH"},
            )
        return destination

    def _execute(
        self,
        executable: Path,
        arguments: Sequence[str],
        work_dir: Path,
        request: SandboxRunRequest,
        control: _RunControl,
    ) -> SandboxRunResult:
        started_at = datetime.now(UTC)
        started_clock = time.monotonic_ns()
        creationflags = 0
        start_new_session = not _is_windows()
        job: _WindowsJob | None = None
        if _is_windows():
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | 0x00000004  # CREATE_SUSPENDED: assign Job before first instruction.
            )
            job = _WindowsJob(
                cpu_ms=request.limits.cpu_ms,
                memory_bytes=request.limits.memory_bytes,
                max_processes=request.limits.max_processes,
            )
        process: subprocess.Popen[bytes] | None = None
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        readers: tuple[threading.Thread, ...] = ()
        overflow = threading.Event()
        budget_lock = threading.Lock()
        output_total = 0
        metric_stop = threading.Event()
        metric_values = [0, 0]
        metric_reader: threading.Thread | None = None

        def drain(stream: BinaryIO, target: bytearray) -> None:
            nonlocal output_total
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                with budget_lock:
                    remaining = request.limits.max_output_bytes - output_total
                    accepted = chunk[: max(0, remaining)]
                    target.extend(accepted)
                    output_total += len(accepted)
                    exceeded = len(accepted) != len(chunk)
                if exceeded:
                    overflow.set()
                    control.terminate()
                    return

        def monitor_metrics(pid: int) -> None:
            while not metric_stop.is_set():
                cpu_ms, peak_memory_bytes = _read_linux_usage(pid)
                metric_values[0] = max(metric_values[0], cpu_ms)
                metric_values[1] = max(metric_values[1], peak_memory_bytes)
                metric_stop.wait(0.005)

        try:
            if control.cancelled.is_set():
                raise _SandboxRejected(
                    "SANDBOX_RUNTIME_ERROR",
                    "Sandbox run was cancelled before process start.",
                    {"reason": "CANCELLED"},
                )
            process = subprocess.Popen(
                [str(executable), *arguments],
                cwd=work_dir,
                env=_minimal_environment(work_dir, request.deterministic_seed),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creationflags,
                start_new_session=start_new_session,
                preexec_fn=(
                    None
                    if _is_windows()
                    else _posix_limit_setup(
                        request.limits.cpu_ms,
                        request.limits.memory_bytes,
                        request.limits.max_processes,
                    )
                ),
            )
            if process.stdout is None or process.stderr is None:
                process.kill()
                process.wait(timeout=5)
                raise OSError("Sandbox process pipes were not created")
            if job is not None:
                job.assign(process)
            control.attach(process, job)
            if control.cancelled.is_set():
                raise _SandboxRejected(
                    "SANDBOX_RUNTIME_ERROR",
                    "Sandbox run was cancelled before execution.",
                    {"reason": "CANCELLED"},
                )
            if job is not None:
                job.resume(process)
            else:
                metric_reader = threading.Thread(
                    target=monitor_metrics,
                    args=(process.pid,),
                    daemon=True,
                    name=f"{request.run_id}-metrics",
                )
                metric_reader.start()
            readers = (
                threading.Thread(
                    target=drain,
                    args=(process.stdout, stdout_buffer),
                    daemon=True,
                    name=f"{request.run_id}-stdout",
                ),
                threading.Thread(
                    target=drain,
                    args=(process.stderr, stderr_buffer),
                    daemon=True,
                    name=f"{request.run_id}-stderr",
                ),
            )
            for reader in readers:
                reader.start()
            try:
                exit_code = process.wait(timeout=request.limits.wall_ms / 1000)
            except subprocess.TimeoutExpired as error:
                control.terminate()
                self._reap(process)
                raise _SandboxRejected(
                    "SANDBOX_RESOURCE_LIMIT",
                    "Sandbox exceeded its wall-clock limit.",
                    {"reason": "WALL_TIMEOUT", "wall_ms": request.limits.wall_ms},
                ) from error
            for reader in readers:
                reader.join(timeout=5)
            if any(reader.is_alive() for reader in readers):
                control.terminate()
                self._reap(process)
                raise _SandboxRejected(
                    "SANDBOX_RUNTIME_ERROR",
                    "Sandbox output pipes did not close after process exit.",
                    {"reason": "PIPE_DRAIN_FAILED"},
                )
            if overflow.is_set():
                self._reap(process)
                raise _SandboxRejected(
                    "SANDBOX_RESOURCE_LIMIT",
                    "Sandbox stdout and stderr exceeded their combined byte limit.",
                    {
                        "reason": "OUTPUT_LIMIT",
                        "max_output_bytes": request.limits.max_output_bytes,
                    },
                )
            if control.cancelled.is_set():
                raise _SandboxRejected(
                    "SANDBOX_RUNTIME_ERROR",
                    "Sandbox run was cancelled.",
                    {"reason": "CANCELLED"},
                )
            try:
                bytes(stderr_buffer).decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise _SandboxRejected(
                    "SANDBOX_RUNTIME_ERROR",
                    "Sandbox stderr was not valid UTF-8.",
                    {"reason": "INVALID_STDERR_ENCODING"},
                ) from error
            if exit_code != 0:
                cpu_ms, peak_memory_bytes = (
                    job.usage(process) if job is not None else (metric_values[0], metric_values[1])
                )
                if (
                    cpu_ms >= request.limits.cpu_ms
                    or peak_memory_bytes >= request.limits.memory_bytes
                    or (
                        not _is_windows()
                        and exit_code
                        in {
                            -_signal_number("SIGKILL", 9),
                            -_signal_number("SIGXCPU", 24),
                        }
                    )
                ):
                    raise _SandboxRejected(
                        "SANDBOX_RESOURCE_LIMIT",
                        "Sandbox exceeded a CPU, memory, or process limit.",
                        {
                            "reason": "PROCESS_RESOURCE_LIMIT",
                            "cpu_ms": cpu_ms,
                            "peak_memory_bytes": peak_memory_bytes,
                        },
                    )
                raise _SandboxRejected(
                    "SANDBOX_RUNTIME_ERROR",
                    "Sandbox executable returned a non-zero exit code.",
                    {"reason": "NON_ZERO_EXIT", "exit_code": exit_code},
                )
            parsed = _parse_action_intents(bytes(stdout_buffer), request)
            finished_at = datetime.now(UTC)
            wall_ms = max(0, (time.monotonic_ns() - started_clock) // 1_000_000)
            cpu_ms, peak_memory_bytes = (
                job.usage(process) if job is not None else (metric_values[0], metric_values[1])
            )
            return SandboxRunResult(
                run_id=request.run_id,
                started_at=started_at,
                finished_at=finished_at,
                action_intents=parsed,
                stdout_ref=None,
                stderr_ref=None,
                usage=SandboxUsage(
                    cpu_ms=cpu_ms,
                    wall_ms=wall_ms,
                    peak_memory_bytes=peak_memory_bytes,
                ),
                evidence_refs=(),
            )
        finally:
            metric_stop.set()
            if metric_reader is not None:
                metric_reader.join(timeout=5)
            if process is not None and process.poll() is None:
                control.terminate()
                self._reap(process)
            for reader in readers:
                reader.join(timeout=5)
            if process is not None:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            if job is not None:
                job.close()

    @staticmethod
    def _reap(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


__all__ = ["ArgumentBuilder", "ProductionCppSandbox"]
