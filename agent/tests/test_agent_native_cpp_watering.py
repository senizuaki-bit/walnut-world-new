from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    WORLD_ID,
    CommitStore,
    RecordingReads,
    SequenceLlm,
    TraceSink,
    decision_output,
    make_event,
    make_operation,
    make_reply,
    make_versions,
    make_world_state,
    tool_calls_output,
)
from yaya_agent_contracts import (  # noqa: E402
    EvidenceRef,
    EvidenceType,
    OperationContext,
    SkillRef,
    Success,
    WorldCommitReceipt,
    WorldSnapshot,
    canonical_json_sha256,
)
from yaya_agent_runtime import (  # noqa: E402
    AgentHub,
    AgentToolExecutionError,
    ContextBuilder,
    PackagedRoleConfigProvider,
    PromptBuilder,
    RoleRouter,
    RunResultSnapshot,
    SharedAgentRuntime,
    SkillInvocationRequest,
    SkillInvocationResult,
    SkillSnapshot,
    build_default_tool_registry,
    freeze_object,
    skill_invocation_request_sha256,
    world_commit_receipt_sha256,
)

_CPP_SOURCE = r"""
#include <chrono>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

int main(int argc, char** argv) {
    if (argc != 2) {
        return 3;
    }
    int length = 0;
    try {
        std::size_t parsed = 0;
        const std::string raw(argv[1]);
        length = std::stoi(raw, &parsed);
        if (parsed != raw.size()) {
            return 3;
        }
    } catch (const std::exception&) {
        return 3;
    }
    if (length == -1) {
        for (;;) {
            std::this_thread::sleep_for(std::chrono::hours(1));
        }
    }
    constexpr int total = 8;
    if (length < 0 || length > total) {
        return 3;
    }
    std::cout << "{\"actions\":[";
    for (int index = 0; index < length; ++index) {
        if (index != 0) {
            std::cout << ',';
        }
        std::cout << "{\"type\":\"water_plot\",\"plot_index\":" << index << '}';
    }
    std::cout << "]}";
    return 0;
}
""".strip()

_FAKE_SUMMARY_SOURCE = r"""
#include <iostream>
int main() {
    std::cout << "{\"watered\":8,\"total\":8,\"task_success\":true}";
    return 0;
}
""".strip()

_DUPLICATE_JSON_SOURCE = r"""
#include <iostream>
int main() {
    std::cout << "{\"actions\":[],\"actions\":[]}";
    return 0;
}
""".strip()

_LARGE_OUTPUT_SOURCE = r"""
#include <iostream>
int main() {
    for (int index = 0; index < 70000; ++index) {
        std::cout << 'x';
    }
    return 0;
}
""".strip()


def _find_vsdevcmd() -> Path:
    configured = os.environ.get("YAYA_VSDEVCMD")
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate
        raise AssertionError(f"YAYA_VSDEVCMD does not identify a file: {candidate}")
    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    if not program_files_x86:
        raise AssertionError("ProgramFiles(x86) is unavailable; native MSVC gate requires Windows")
    vswhere = Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        raise AssertionError(
            "vswhere.exe is missing; install MSVC Build Tools or set YAYA_VSDEVCMD"
        )
    discovery = subprocess.run(
        [
            str(vswhere),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    installation = discovery.stdout.strip()
    if discovery.returncode != 0 or not installation:
        raise AssertionError("MSVC C++ Build Tools were not discovered")
    candidate = Path(installation) / "Common7" / "Tools" / "VsDevCmd.bat"
    if not candidate.is_file():
        raise AssertionError(f"VsDevCmd.bat is missing from {installation}")
    return candidate


def _compile_cpp(source: str, build_root: Path) -> Path:
    source_path = build_root / "watering.cpp"
    executable = build_root / "watering.exe"
    source_path.write_text(source, encoding="utf-8", newline="\n")
    if os.name == "nt":
        command_file = build_root / "build.cmd"
        command_file.write_text(
            "\n".join(
                (
                    "@echo off",
                    f'call "{_find_vsdevcmd()}" -no_logo -arch=x64 >nul',
                    "if errorlevel 1 exit /b %errorlevel%",
                    (
                        f"cl /nologo /std:c++20 /EHsc /W4 /WX /utf-8 "
                        f'/Fe:"{executable}" "{source_path}"'
                    ),
                )
            ),
            encoding="utf-8",
            newline="\r\n",
        )
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(command_file)]
    else:
        compiler = shutil.which("g++")
        if compiler is None:
            raise AssertionError("g++ is required for the native C++ gate on POSIX")
        command = [
            compiler,
            "-std=c++20",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-O2",
            "-o",
            str(executable),
            str(source_path),
        ]
    build = subprocess.run(
        command,
        cwd=build_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if build.returncode != 0 or not executable.is_file():
        output = (build.stdout + "\n" + build.stderr)[-4000:]
        raise AssertionError(f"native C++ compilation failed ({build.returncode}):\n{output}")
    return executable


def _run_cpp(executable: Path, length: int, *, timeout: float = 3.0) -> dict[str, object]:
    command = [str(executable), str(length)]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise AssertionError("native process pipes were not created")
    output_buffers: dict[str, bytearray] = {
        "stdout": bytearray(),
        "stderr": bytearray(),
    }
    output_overflow = threading.Event()

    def drain(name: str, stream: object, maximum: int) -> None:
        reader = getattr(stream, "read")
        while True:
            chunk = reader(4096)
            if not chunk:
                return
            buffer = output_buffers[name]
            remaining = maximum - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                output_overflow.set()
                process.kill()

    readers = (
        threading.Thread(target=drain, args=("stdout", process.stdout, 65_536), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr, 16_384), daemon=True),
    )
    for reader in readers:
        reader.start()

    def close_pipes() -> None:
        process.stdout.close()
        process.stderr.close()

    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
        close_pipes()
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=bytes(output_buffers["stdout"]),
            stderr=bytes(output_buffers["stderr"]),
        ) from error
    for reader in readers:
        reader.join(timeout=5)
    if any(reader.is_alive() for reader in readers):
        process.kill()
        close_pipes()
        for reader in readers:
            reader.join(timeout=5)
        raise AgentToolExecutionError(
            "TOOL_NATIVE_PIPE_FAILED",
            "native output reader did not terminate",
        )
    close_pipes()
    if output_overflow.is_set():
        raise AgentToolExecutionError(
            "TOOL_NATIVE_OUTPUT_TOO_LARGE",
            "native stdout or stderr exceeded its fixed byte limit",
        )
    try:
        stdout = bytes(output_buffers["stdout"]).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AgentToolExecutionError(
            "TOOL_NATIVE_OUTPUT_INVALID",
            "native watering output is not valid UTF-8",
        ) from error
    if return_code != 0:
        raise AgentToolExecutionError(
            "TOOL_NATIVE_PROCESS_FAILED",
            "native watering process failed outside its declared result states",
            {"return_code": return_code},
        )

    def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        value = json.loads(
            stdout,
            object_pairs_hook=strict_object,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise AgentToolExecutionError(
            "TOOL_NATIVE_OUTPUT_INVALID",
            "native watering process did not return strict JSON",
        ) from error
    if not isinstance(value, dict) or set(value) != {"actions"}:
        raise AgentToolExecutionError(
            "TOOL_NATIVE_OUTPUT_INVALID",
            "native watering output fields are not closed",
        )
    return value


def _validate_action_intents(
    value: object,
    *,
    expected_count: int,
    plot_count: int,
) -> tuple[int, ...]:
    if not isinstance(value, Mapping) or set(value) != {"actions"}:
        raise AgentToolExecutionError(
            "TOOL_NATIVE_OUTPUT_INVALID",
            "native output must contain only a closed action-intent array",
        )
    raw_actions = value["actions"]
    if not isinstance(raw_actions, list) or len(raw_actions) > 64:
        raise AgentToolExecutionError(
            "TOOL_NATIVE_OUTPUT_INVALID",
            "native action-intent array is missing or exceeds its bound",
        )
    indexes: list[int] = []
    for action in raw_actions:
        if not isinstance(action, Mapping) or set(action) != {"type", "plot_index"}:
            raise AgentToolExecutionError(
                "TOOL_NATIVE_OUTPUT_INVALID",
                "each native action intent must use the exact supported fields",
            )
        plot_index = action["plot_index"]
        if (
            action["type"] != "water_plot"
            or isinstance(plot_index, bool)
            or not isinstance(plot_index, int)
            or not 0 <= plot_index < plot_count
        ):
            raise AgentToolExecutionError(
                "TOOL_NATIVE_ACTION_REJECTED",
                "World rules rejected an invalid watering action intent",
            )
        indexes.append(plot_index)
    if len(indexes) != expected_count or len(set(indexes)) != len(indexes):
        raise AgentToolExecutionError(
            "TOOL_NATIVE_ACTION_REJECTED",
            "action trace does not match the invocation length or repeats a plot",
        )
    return tuple(indexes)


class NativeCppWateringApplication:
    def __init__(
        self,
        operation: OperationContext,
        skill: SkillSnapshot,
        state: Mapping[str, object],
        executable: Path,
    ) -> None:
        if operation != skill.request_context:
            raise AgentToolExecutionError(
                "TOOL_SKILL_PROVENANCE_MISMATCH",
                "native fixture Skill belongs to another actor or content version",
            )
        if not executable.is_file():
            raise AgentToolExecutionError(
                "TOOL_ARTIFACT_MISSING",
                "certified native Skill artifact is unavailable",
            )
        artifact_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
        if artifact_sha256 != skill.ref.artifact_sha256:
            raise AgentToolExecutionError(
                "TOOL_ARTIFACT_HASH_MISMATCH",
                "native executable bytes do not match the certified Skill artifact",
            )
        self.operation = operation
        self.skill = skill
        self.state = deepcopy(dict(state))
        self.executable = executable
        self.revision = 5
        self.last_event_sequence = 40
        self.execution_count = 0
        self.fail_after_next_commit = False
        self.evidence_payloads: dict[str, Mapping[str, object]] = {}
        self._receipts: dict[tuple[str, str], tuple[str, SkillInvocationResult]] = {}

    @property
    def state_sha256(self) -> str:
        encoded = json.dumps(
            self.state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def get_result(
        self,
        invocation_id: str,
        context: OperationContext,
    ) -> SkillInvocationResult | None:
        if (
            context.actor != self.operation.actor
            or context.content_ref != self.operation.content_ref
        ):
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH",
                "native receipt lookup crossed its actor or content authority",
            )
        receipt = self._receipts.get((context.actor.tenant_id, invocation_id))
        return None if receipt is None else receipt[1]

    async def get_snapshot(
        self,
        world_id: str,
        context: OperationContext,
    ) -> Success[WorldSnapshot]:
        if world_id != WORLD_ID or context.actor != self.operation.actor:
            raise AgentToolExecutionError(
                "TOOL_WORLD_IDENTITY_MISMATCH",
                "native fixture World identity mismatch",
            )
        return Success(
            WorldSnapshot(
                request_context=self.operation,
                world_id=WORLD_ID,
                revision=self.revision,
                last_event_sequence=self.last_event_sequence,
                state_hash=self.state_sha256,
                generated_at=NOW,
                world_rules_version="farm-rules-1",
                state=self.state,
            )
        )

    async def invoke(
        self,
        request: SkillInvocationRequest,
        context: OperationContext,
    ) -> SkillInvocationResult:
        if (
            context.actor != self.operation.actor
            or context.content_ref != self.operation.content_ref
            or request.tenant_id != context.actor.tenant_id
            or request.skill_ref != self.skill.ref
            or request.world_id != WORLD_ID
        ):
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH",
                "native C++ invocation crossed its canonical identity",
            )
        key = (request.tenant_id, request.invocation_id)
        replay = self._receipts.get(key)
        if replay is not None:
            if replay[0] != request.request_sha256:
                raise AgentToolExecutionError(
                    "TOOL_IDEMPOTENCY_KEY_REUSED",
                    "native invocation identity was reused with another request hash",
                )
            return replay[1]
        if request.expected_world_revision != self.revision:
            raise AgentToolExecutionError(
                "TOOL_WORLD_REVISION_CONFLICT",
                "native fixture World CAS rejected the revision",
            )
        length = request.arguments["length"]
        if isinstance(length, bool) or not isinstance(length, int):
            raise AgentToolExecutionError(
                "TOOL_INPUT_INVALID",
                "native watering length must be an integer",
            )
        if (
            hashlib.sha256(self.executable.read_bytes()).hexdigest()
            != self.skill.ref.artifact_sha256
        ):
            raise AgentToolExecutionError(
                "TOOL_ARTIFACT_HASH_MISMATCH",
                "native executable changed after certified artifact resolution",
            )
        native = _run_cpp(self.executable, length)
        self.execution_count += 1
        plots = self.state.get("plots")
        if not isinstance(plots, list):
            raise AgentToolExecutionError(
                "TOOL_WORLD_STATE_INVALID",
                "native fixture World plots must be a mutable list",
            )
        action_indexes = _validate_action_intents(
            native, expected_count=length, plot_count=len(plots)
        )
        before = self.revision
        before_sequence = self.last_event_sequence
        next_state = deepcopy(self.state)
        next_plots = next_state.get("plots")
        if not isinstance(next_plots, list):
            raise AssertionError("validated native fixture lost its plots list")
        for ordinal, plot_index in enumerate(action_indexes, start=1):
            plot = next_plots[plot_index]
            if not isinstance(plot, dict):
                raise AgentToolExecutionError(
                    "TOOL_WORLD_STATE_INVALID",
                    "native fixture World plot must be a mutable object",
                )
            plot["hydration"] = 100
            plot["last_updated_event_sequence"] = before_sequence + ordinal
        watered = sum(
            isinstance(plot, Mapping) and plot.get("hydration") == 100 for plot in next_plots
        )
        total = len(next_plots)
        success = watered == total
        suffix = hashlib.sha256(request.invocation_id.encode("utf-8")).hexdigest()[:16]
        run_id = f"run_native_{suffix}"
        run_evidence_payload: Mapping[str, object] = {
            "evidence_kind": "SKILL_RUN",
            "run_id": run_id,
            "sandbox_status": "SUCCEEDED",
            "world_status": "COMMITTED" if success else "NOT_ATTEMPTED",
            "intent_count": len(action_indexes),
        }
        run_evidence_id = (
            f"evidence_native_sandbox_{suffix}"
            if success
            else f"evidence_native_test_report_{suffix}"
        )
        run_evidence = EvidenceRef(
            run_evidence_id,
            EvidenceType.SANDBOX_LOG if success else EvidenceType.TEST_REPORT,
            NOW,
            sha256=canonical_json_sha256(run_evidence_payload),
        )
        staged_evidence_payloads: dict[str, Mapping[str, object]] = {
            run_evidence_id: freeze_object(run_evidence_payload, "run evidence payload")
        }
        world_commit = None
        revision_after = before
        sequence_after = before_sequence
        evidence: tuple[EvidenceRef, ...] = (run_evidence,)
        if success:
            revision_after = before + 1
            sequence_after = before_sequence + len(action_indexes)
            next_hash = hashlib.sha256(
                json.dumps(
                    next_state,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            world_commit = WorldCommitReceipt(
                world_id=WORLD_ID,
                previous_revision=before,
                world_revision=revision_after,
                first_event_sequence=before_sequence + 1,
                last_event_sequence=sequence_after,
                committed_at=NOW,
                state_hash=next_hash,
            )
            world_evidence_id = f"evidence_native_world_commit_{suffix}"
            world_evidence_payload: Mapping[str, object] = {
                "evidence_kind": "WORLD_COMMIT",
                "world_id": world_commit.world_id,
                "previous_revision": world_commit.previous_revision,
                "world_revision": world_commit.world_revision,
                "first_event_sequence": world_commit.first_event_sequence,
                "last_event_sequence": world_commit.last_event_sequence,
                "state_hash": world_commit.state_hash,
            }
            world_evidence = EvidenceRef(
                world_evidence_id,
                EvidenceType.WORLD_COMMIT,
                NOW,
                sha256=world_commit_receipt_sha256(world_commit),
            )
            evidence = (run_evidence, world_evidence)
            staged_evidence_payloads[world_evidence_id] = freeze_object(
                world_evidence_payload,
                "World evidence payload",
            )
        run = RunResultSnapshot(
            run_id=run_id,
            session_id=request.session_id,
            turn_id=request.turn_id,
            command_id=request.command_id,
            world_id=request.world_id,
            skill_ref=request.skill_ref,
            task_success=success,
            world_revision_before=before,
            world_revision_after=revision_after,
            world_difference={
                "watered_plots": watered,
                "total_plots": total,
                "intent_count": len(action_indexes),
            },
            failed_actions=() if success else ({"reason": "short_loop"},),
            failure_key=None if success else "watering_loop_short",
            evidence_refs=evidence,
            world_commit=world_commit,
            request_context=context,
        )
        result = SkillInvocationResult(
            request.invocation_id,
            request.tenant_id,
            request.request_sha256,
            request.arguments,
            run,
        )
        # Everything above is staged and validated.  With no await in this
        # section, the state, receipt and Evidence become visible together to
        # other asyncio workers, modelling one World/Run/Evidence transaction.
        if success:
            self.state = next_state
            self.revision = revision_after
            self.last_event_sequence = sequence_after
        self.evidence_payloads.update(staged_evidence_payloads)
        self._receipts[key] = (request.request_sha256, result)
        if self.fail_after_next_commit:
            self.fail_after_next_commit = False
            raise ConnectionError("simulated response loss after atomic commit")
        return result


class NativeCppAgentTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_cpp_skill_runs_through_agent_and_world_cas(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-native-watering-") as raw_root:
            build_root = Path(raw_root).resolve()
            executable = _compile_cpp(_CPP_SOURCE, build_root)

            failed_boundary = _run_cpp(executable, 7)
            self.assertEqual(
                failed_boundary,
                {"actions": [{"type": "water_plot", "plot_index": index} for index in range(7)]},
            )
            real_popen = subprocess.Popen
            real_thread = threading.Thread
            spawned: list[subprocess.Popen[bytes]] = []
            pipe_readers: list[threading.Thread] = []

            def recording_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.append(process)
                return process

            def recording_thread(*args, **kwargs):
                reader = real_thread(*args, **kwargs)
                pipe_readers.append(reader)
                return reader

            with (
                patch(f"{__name__}.subprocess.Popen", side_effect=recording_popen),
                patch(f"{__name__}.threading.Thread", side_effect=recording_thread),
                self.assertRaises(subprocess.TimeoutExpired) as native_timeout,
            ):
                _run_cpp(executable, -1, timeout=0.5)

            self.assertEqual(native_timeout.exception.cmd, [str(executable), "-1"])
            self.assertEqual(native_timeout.exception.timeout, 0.5)
            self.assertEqual(len(spawned), 1)
            timed_out_process = spawned[0]
            self.assertIsNotNone(timed_out_process.returncode)
            self.assertIsNotNone(timed_out_process.stdout)
            self.assertIsNotNone(timed_out_process.stderr)
            assert timed_out_process.stdout is not None
            assert timed_out_process.stderr is not None
            self.assertTrue(timed_out_process.stdout.closed)
            self.assertTrue(timed_out_process.stderr.closed)
            self.assertEqual(len(pipe_readers), 2)
            self.assertTrue(all(not reader.is_alive() for reader in pipe_readers))

            fake_root = build_root / "fake-summary"
            fake_root.mkdir()
            fake_executable = _compile_cpp(_FAKE_SUMMARY_SOURCE, fake_root)
            with self.assertRaises(AgentToolExecutionError) as fake_summary_error:
                _run_cpp(fake_executable, 1)
            self.assertEqual(fake_summary_error.exception.code, "TOOL_NATIVE_OUTPUT_INVALID")

            duplicate_root = build_root / "duplicate-json"
            duplicate_root.mkdir()
            duplicate_executable = _compile_cpp(_DUPLICATE_JSON_SOURCE, duplicate_root)
            with self.assertRaises(AgentToolExecutionError) as duplicate_error:
                _run_cpp(duplicate_executable, 1)
            self.assertEqual(duplicate_error.exception.code, "TOOL_NATIVE_OUTPUT_INVALID")

            large_root = build_root / "large-output"
            large_root.mkdir()
            large_executable = _compile_cpp(_LARGE_OUTPUT_SOURCE, large_root)
            with self.assertRaises(AgentToolExecutionError) as large_output_error:
                _run_cpp(large_executable, 1)
            self.assertEqual(
                large_output_error.exception.code,
                "TOOL_NATIVE_OUTPUT_TOO_LARGE",
            )

            operation = make_operation()
            skill_ref = SkillRef(
                "skill_native_watering_0001",
                "skill_version_native_0001",
                hashlib.sha256(executable.read_bytes()).hexdigest(),
                "certification_native_0001",
            )
            skill = SkillSnapshot(
                ref=skill_ref,
                source_code=_CPP_SOURCE,
                source_sha256=hashlib.sha256(_CPP_SOURCE.encode("utf-8")).hexdigest(),
                entrypoint="watering.cpp",
                parameter_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["length"],
                    "properties": {"length": {"type": "integer", "minimum": 1, "maximum": 8}},
                },
                request_context=operation,
            )
            wrong_artifact_ref = SkillRef(
                skill_ref.skill_id,
                skill_ref.skill_version_id,
                "a" * 64,
                skill_ref.certification_id,
            )
            wrong_artifact_skill = SkillSnapshot(
                ref=wrong_artifact_ref,
                source_code=_CPP_SOURCE,
                source_sha256=hashlib.sha256(_CPP_SOURCE.encode("utf-8")).hexdigest(),
                entrypoint="watering.cpp",
                parameter_schema=skill.parameter_schema,
                request_context=operation,
            )
            with self.assertRaises(AgentToolExecutionError) as artifact_error:
                NativeCppWateringApplication(
                    operation,
                    wrong_artifact_skill,
                    make_world_state(plot_count=8, hydration=0),
                    executable,
                )
            self.assertEqual(artifact_error.exception.code, "TOOL_ARTIFACT_HASH_MISMATCH")

            toctou_root = build_root / "toctou"
            toctou_root.mkdir()
            toctou_executable = toctou_root / "watering.exe"
            shutil.copyfile(executable, toctou_executable)
            toctou_application = NativeCppWateringApplication(
                operation,
                skill,
                make_world_state(plot_count=8, hydration=0),
                toctou_executable,
            )
            shutil.copyfile(fake_executable, toctou_executable)
            event = make_event("run_skill_requested")
            event = type(event)(
                event_id=event.event_id,
                event_type=event.event_type,
                student_id=event.student_id,
                task_id=event.task_id,
                session_id=event.session_id,
                turn_id=event.turn_id,
                command_id=event.command_id,
                occurred_at=event.occurred_at,
                expected_world_revision=event.expected_world_revision,
                skill_ref=skill_ref,
                payload={},
            )
            toctou_identity = {
                "invocation_id": "invoke_native_toctou_0001",
                "tenant_id": operation.actor.tenant_id,
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "command_id": event.command_id,
                "world_id": WORLD_ID,
                "expected_world_revision": 5,
                "skill_ref": skill_ref,
                "arguments": {"length": 8},
            }
            toctou_request = SkillInvocationRequest(
                **toctou_identity,
                request_sha256=skill_invocation_request_sha256(**toctou_identity),
            )
            with self.assertRaises(AgentToolExecutionError) as toctou_error:
                await toctou_application.invoke(toctou_request, operation)
            self.assertEqual(toctou_error.exception.code, "TOOL_ARTIFACT_HASH_MISMATCH")
            self.assertEqual(toctou_application.revision, 5)
            self.assertEqual(toctou_application.execution_count, 0)
            state = make_world_state(plot_count=8, hydration=0)
            application = NativeCppWateringApplication(
                operation,
                skill,
                state,
                executable,
            )
            failed_application = NativeCppWateringApplication(
                operation,
                skill,
                state,
                executable,
            )
            seven_of_eight_identity = {
                "invocation_id": "invoke_native_seven_of_eight_0001",
                "tenant_id": operation.actor.tenant_id,
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "command_id": event.command_id,
                "world_id": WORLD_ID,
                "expected_world_revision": 5,
                "skill_ref": skill_ref,
                "arguments": {"length": 7},
            }
            seven_of_eight_request = SkillInvocationRequest(
                **seven_of_eight_identity,
                request_sha256=skill_invocation_request_sha256(
                    **seven_of_eight_identity,
                ),
            )

            seven_of_eight = await failed_application.invoke(
                seven_of_eight_request,
                operation,
            )

            self.assertFalse(seven_of_eight.run.task_success)
            self.assertEqual(seven_of_eight.run.world_revision_before, 5)
            self.assertEqual(seven_of_eight.run.world_revision_after, 5)
            self.assertIsNone(seven_of_eight.run.world_commit)
            self.assertEqual(
                [item.evidence_type for item in seven_of_eight.run.evidence_refs],
                [EvidenceType.TEST_REPORT],
            )
            self.assertEqual(failed_application.execution_count, 1)
            self.assertEqual(failed_application.revision, 5)
            self.assertEqual(failed_application.last_event_sequence, 40)
            self.assertTrue(
                all(plot["hydration"] == 0 for plot in failed_application.state["plots"])
            )
            stale_identity = {
                "invocation_id": "invoke_native_stale_0001",
                "tenant_id": operation.actor.tenant_id,
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "command_id": event.command_id,
                "world_id": WORLD_ID,
                "expected_world_revision": 4,
                "skill_ref": skill_ref,
                "arguments": {"length": 8},
            }
            stale_request = SkillInvocationRequest(
                **stale_identity,
                request_sha256=skill_invocation_request_sha256(**stale_identity),
            )
            with self.assertRaises(AgentToolExecutionError) as stale_error:
                await application.invoke(stale_request, operation)
            self.assertEqual(stale_error.exception.code, "TOOL_WORLD_REVISION_CONFLICT")
            self.assertEqual(application.revision, 5)
            self.assertEqual(application.execution_count, 0)

            invalid_application = NativeCppWateringApplication(
                operation,
                skill,
                state,
                executable,
            )
            invalid_native_results = (
                (
                    {"watered": 8, "total": 8, "task_success": True},
                    "TOOL_NATIVE_OUTPUT_INVALID",
                ),
                (
                    {
                        "actions": [
                            {"type": "water_plot", "plot_index": index} for index in range(9)
                        ]
                    },
                    "TOOL_NATIVE_ACTION_REJECTED",
                ),
                (
                    {
                        "actions": [
                            {"type": "water_plot", "plot_index": index} for index in range(7)
                        ]
                    },
                    "TOOL_NATIVE_ACTION_REJECTED",
                ),
            )
            for index, (invalid_native, expected_code) in enumerate(
                invalid_native_results, start=1
            ):
                identity = {
                    "invocation_id": f"invoke_native_invalid_{index:04d}",
                    "tenant_id": operation.actor.tenant_id,
                    "session_id": event.session_id,
                    "turn_id": event.turn_id,
                    "command_id": event.command_id,
                    "world_id": WORLD_ID,
                    "expected_world_revision": 5,
                    "skill_ref": skill_ref,
                    "arguments": {"length": 8},
                }
                request = SkillInvocationRequest(
                    **identity,
                    request_sha256=skill_invocation_request_sha256(**identity),
                )
                with (
                    self.subTest(native=invalid_native),
                    patch(f"{__name__}._run_cpp", return_value=invalid_native),
                    self.assertRaises(AgentToolExecutionError) as invalid_error,
                ):
                    await invalid_application.invoke(request, operation)
                self.assertEqual(
                    invalid_error.exception.code,
                    expected_code,
                )
            self.assertEqual(invalid_application.revision, 5)
            self.assertTrue(
                all(plot["hydration"] == 0 for plot in invalid_application.state["plots"])
            )

            atomic_application = NativeCppWateringApplication(
                operation,
                skill,
                state,
                executable,
            )
            atomic_identity = {
                "invocation_id": "invoke_native_atomic_0001",
                "tenant_id": operation.actor.tenant_id,
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "command_id": event.command_id,
                "world_id": WORLD_ID,
                "expected_world_revision": 5,
                "skill_ref": skill_ref,
                "arguments": {"length": 8},
            }
            atomic_request = SkillInvocationRequest(
                **atomic_identity,
                request_sha256=skill_invocation_request_sha256(**atomic_identity),
            )
            with (
                patch(
                    f"{__name__}.world_commit_receipt_sha256",
                    side_effect=RuntimeError("hash stage failed"),
                ),
                self.assertRaises(RuntimeError),
            ):
                await atomic_application.invoke(atomic_request, operation)
            self.assertEqual(atomic_application.revision, 5)
            self.assertEqual(atomic_application.last_event_sequence, 40)
            self.assertEqual(len(atomic_application._receipts), 0)
            self.assertEqual(len(atomic_application.evidence_payloads), 0)

            response_loss_application = NativeCppWateringApplication(
                operation,
                skill,
                state,
                executable,
            )
            response_loss_application.fail_after_next_commit = True
            with self.assertRaises(ConnectionError):
                await response_loss_application.invoke(atomic_request, operation)
            replayed_run = await response_loss_application.invoke(atomic_request, operation)
            self.assertTrue(replayed_run.run.task_success)
            self.assertEqual(response_loss_application.execution_count, 1)
            self.assertEqual(response_loss_application.revision, 6)
            reads = RecordingReads(operation=operation, skill=skill)
            configs = PackagedRoleConfigProvider.load()
            contexts = ContextBuilder(
                tasks=reads,
                sessions=reads,
                skills=reads,
                runs=reads,
                counterexamples=reads,
                learners=reads,
                messages=reads,
                worlds=application,
                role_configs=configs,
            )
            trace = TraceSink()
            llm = SequenceLlm(
                [
                    make_reply(
                        tool_calls_output(
                            "invoke_skill",
                            {"skill_id": "bound_skill", "arguments": {"length": 8}},
                            call_id="call_native_0001",
                        )
                    ),
                    make_reply(decision_output("xiaohutao", "Untrusted provider success prose.")),
                ]
            )
            runtime = SharedAgentRuntime(
                llm=llm,
                role_configs=configs,
                tools=build_default_tool_registry(trace, application),
                prompts=PromptBuilder(),
                trace=trace,
                versions=make_versions(),
                clock=lambda: NOW,
            )
            turns = CommitStore()
            result = await AgentHub(
                router=RoleRouter(),
                contexts=contexts,
                runtime=runtime,
                turns=turns,
                invocations=application,
            ).handle(event, operation)

            self.assertTrue(result.persisted)
            self.assertFalse(result.replayed)
            self.assertIsNotNone(result.decision)
            decision = result.decision
            assert decision is not None
            self.assertFalse(decision.degraded)
            self.assertIn("任务结果成功", decision.message)
            self.assertEqual(application.execution_count, 1)
            world = await application.get_snapshot(WORLD_ID, operation)
            self.assertEqual(world.value.revision, 6)
            self.assertEqual(world.value.last_event_sequence, 48)
            plots = world.value.state["plots"]
            self.assertEqual(sum(plot["hydration"] == 100 for plot in plots), 8)
            self.assertEqual(
                [ref.evidence_type for ref in decision.evidence_refs],
                [EvidenceType.SANDBOX_LOG, EvidenceType.WORLD_COMMIT],
            )
            self.assertTrue(
                all(ref.sha256 not in {"c" * 64, "d" * 64} for ref in decision.evidence_refs)
            )
            for evidence_ref in decision.evidence_refs:
                evidence_payload = application.evidence_payloads[evidence_ref.evidence_id]
                self.assertEqual(
                    canonical_json_sha256(evidence_payload),
                    evidence_ref.sha256,
                )
            run_payload = application.evidence_payloads[decision.evidence_refs[0].evidence_id]
            with self.assertRaises(TypeError):
                run_payload["intent_count"] = 0  # type: ignore[index]
            self.assertEqual(len(turns.commits), 1)


if __name__ == "__main__":
    unittest.main()
