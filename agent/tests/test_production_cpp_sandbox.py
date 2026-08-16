from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    ActorType,
    ContentRef,
    Failure,
    OperationContext,
    SandboxLimits,
    SandboxRunRequest,
    SkillRef,
    Success,
    WorldSnapshot,
    canonical_json_sha256,
)
from yaya_agent_sandbox import ProductionCppSandbox  # noqa: E402

_CPP_SOURCE = r"""
#include <chrono>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

int main(int argc, char** argv) {
    if (argc != 2) return 3;
    int length = 0;
    try {
        std::size_t parsed = 0;
        std::string raw(argv[1]);
        length = std::stoi(raw, &parsed);
        if (parsed != raw.size()) return 3;
    } catch (const std::exception&) {
        return 3;
    }
    if (length == -1) {
        for (;;) std::this_thread::sleep_for(std::chrono::hours(1));
    }
    if (length == -2) {
        for (int i = 0; i < 100000; ++i) std::cout << 'x';
        return 0;
    }
    if (length == -3) {
        std::cout << "{\"actions\":[],\"actions\":[]}";
        return 0;
    }
    if (length == -4) {
        std::cout << "{\"actions\":[],\"task_success\":true,\"final_state\":{}}";
        return 0;
    }
    if (length < 0 || length > 80) return 3;
    std::cout << "{\"actions\":[";
    for (int index = 1; index <= length; ++index) {
        if (index != 1) std::cout << ',';
        std::cout
            << "{\"intent_id\":\"intent_water_" << std::setfill('0') << std::setw(4)
            << index
            << "\",\"action_type\":\"WATER\",\"actor_entity_id\":\"avatar_0001\","
            << "\"expected_world_revision\":5,\"plot_id\":\"plot_" << std::setfill('0')
            << std::setw(4) << index << "\",\"amount_ml\":100}";
    }
    std::cout << "]}";
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
        raise AssertionError("ProgramFiles(x86) is unavailable; the native C++ test cannot run")
    vswhere = Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        raise AssertionError("MSVC Build Tools are required; vswhere.exe is missing")
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
    source_path = build_root / "sandbox_fixture.cpp"
    executable = build_root / "sandbox_fixture.exe"
    source_path.write_text(source, encoding="utf-8", newline="\n")
    if os.name == "nt":
        command_file = build_root / "build.cmd"
        command_file.write_text(
            textwrap.dedent(
                f"""
                @echo off
                call "{_find_vsdevcmd()}" -no_logo -arch=x64 >nul
                if errorlevel 1 exit /b %errorlevel%
                cl /nologo /std:c++20 /EHsc /W4 /WX /utf-8 /Fe:"{executable}" "{source_path}"
                """
            ).strip(),
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


def _operation() -> OperationContext:
    now = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
    return OperationContext(
        request_id="req_sandbox_0001",
        correlation_id="corr_sandbox_0001",
        trace_id="trace_sandbox_0001",
        requested_at=now,
        actor=ActorRef(
            tenant_id="tenant_yaya",
            actor_id="student_0001",
            actor_type=ActorType.STUDENT,
            roles=("game:player",),
        ),
        content_ref=ContentRef("YAYA_FARM_001", "1.0.0", "a" * 64),
        command_id="cmd_sandbox_0001",
        causation_id=None,
    )


def _state() -> dict[str, object]:
    return {
        "clock": {"day": 1, "minute_of_day": 480, "tick": 10},
        "avatar": {
            "entity_id": "avatar_0001",
            "position": {"x": 0, "y": 0},
            "energy": 100,
        },
        "inventory": [],
        "plots": [
            {
                "plot_id": f"plot_{index:04d}",
                "position": {"x": index, "y": 0},
                "soil_state": "TILLED",
                "hydration": 0,
                "crop": None,
                "last_updated_event_sequence": 0,
            }
            for index in range(1, 9)
        ],
        "agents": [],
    }


def _snapshot(operation: OperationContext) -> WorldSnapshot:
    state = _state()
    return WorldSnapshot(
        request_context=operation,
        world_id="world_watering_0001",
        revision=5,
        last_event_sequence=40,
        state_hash=canonical_json_sha256(state),
        generated_at=operation.requested_at,
        world_rules_version="farm-rules-1",
        state=state,
    )


def _install_artifact(executable: Path, artifact_root: Path) -> tuple[str, Path]:
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    target = artifact_root / digest[:2] / digest
    target.parent.mkdir(parents=True)
    shutil.copyfile(executable, target)
    target.chmod(stat.S_IREAD)
    return digest, target


class ProductionCppSandboxTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._build_directory = tempfile.TemporaryDirectory(prefix="yaya-sandbox-build-")
        cls.executable = _compile_cpp(_CPP_SOURCE, Path(cls._build_directory.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._build_directory.cleanup()

    def setUp(self) -> None:
        self._root_directory = tempfile.TemporaryDirectory(prefix="yaya-sandbox-test-")
        root = Path(self._root_directory.name)
        self.artifact_root = root / "artifacts"
        self.temp_root = root / "work"
        self.artifact_root.mkdir()
        self.temp_root.mkdir()
        self.digest, self.artifact = _install_artifact(self.executable, self.artifact_root)
        self.operation = _operation()
        self.sandbox = ProductionCppSandbox(
            self.artifact_root,
            temp_root=self.temp_root,
        )

    def tearDown(self) -> None:
        self.artifact.chmod(stat.S_IWRITE | stat.S_IREAD)
        self._root_directory.cleanup()

    def request(
        self,
        length: int,
        *,
        digest: str | None = None,
        wall_ms: int = 2_000,
        max_intents: int = 64,
        max_output_bytes: int = 65_536,
        run_id: str = "run_sandbox_0001",
    ) -> SandboxRunRequest:
        artifact_sha256 = self.digest if digest is None else digest
        return SandboxRunRequest(
            run_id=run_id,
            skill_ref=SkillRef(
                skill_id="skill_watering_0001",
                skill_version_id="skill_version_0001",
                artifact_sha256=artifact_sha256,
                certification_id="certification_0001",
            ),
            world_id="world_watering_0001",
            world_snapshot=_snapshot(self.operation),
            input={"length": length},
            deterministic_seed="watering-seed-0001",
            limits=SandboxLimits(
                cpu_ms=1_000,
                wall_ms=wall_ms,
                memory_bytes=67_108_864,
                max_intents=max_intents,
                max_output_bytes=max_output_bytes,
                max_processes=1,
            ),
        )

    async def test_real_cpp_returns_closed_typed_intents_from_isolated_copy(self) -> None:
        result = await self.sandbox.run(self.request(8), self.operation)
        self.assertIsInstance(result, Success)
        assert isinstance(result, Success)
        self.assertEqual(len(result.value.action_intents), 8)
        self.assertEqual(result.value.action_intents[0].plot_id, "plot_0001")
        self.assertEqual(result.value.action_intents[-1].plot_id, "plot_0008")
        if os.name == "nt":
            self.assertGreater(result.value.usage.peak_memory_bytes, 0)
        self.assertEqual(list(self.temp_root.iterdir()), [])

    async def test_timeout_kills_reaps_closes_and_cleans_work_directory(self) -> None:
        result = await self.sandbox.run(self.request(-1, wall_ms=100), self.operation)
        self.assertIsInstance(result, Failure)
        assert isinstance(result, Failure)
        self.assertEqual(result.error.code, "SANDBOX_RESOURCE_LIMIT")
        self.assertEqual(result.error.details["reason"], "WALL_TIMEOUT")
        self.assertEqual(list(self.temp_root.iterdir()), [])

    async def test_output_action_and_strict_json_limits_fail_closed(self) -> None:
        cases = (
            (-2, 64, 4_096, "SANDBOX_RESOURCE_LIMIT", "OUTPUT_LIMIT"),
            (-3, 64, 65_536, "SANDBOX_RUNTIME_ERROR", "INVALID_JSON"),
            (-4, 64, 65_536, "SANDBOX_RUNTIME_ERROR", "INVALID_ACTION_ARRAY"),
            (65, 64, 65_536, "SANDBOX_RESOURCE_LIMIT", "ACTION_LIMIT"),
        )
        for ordinal, (length, max_intents, max_output, code, reason) in enumerate(cases):
            with self.subTest(length=length):
                result = await self.sandbox.run(
                    self.request(
                        length,
                        max_intents=max_intents,
                        max_output_bytes=max_output,
                        run_id=f"run_sandbox_case_{ordinal:04d}",
                    ),
                    self.operation,
                )
                self.assertIsInstance(result, Failure)
                assert isinstance(result, Failure)
                self.assertEqual(result.error.code, code)
                self.assertEqual(result.error.details["reason"], reason)
                self.assertEqual(list(self.temp_root.iterdir()), [])

    async def test_mismatched_or_writable_artifacts_never_execute(self) -> None:
        wrong_digest = "f" * 64
        wrong = self.artifact_root / wrong_digest[:2] / wrong_digest
        wrong.parent.mkdir(parents=True)
        shutil.copyfile(self.executable, wrong)
        wrong.chmod(stat.S_IREAD)
        mismatch = await self.sandbox.run(self.request(8, digest=wrong_digest), self.operation)
        self.assertIsInstance(mismatch, Failure)
        assert isinstance(mismatch, Failure)
        self.assertEqual(mismatch.error.code, "ACTIVE_SKILL_ARTIFACT_MISMATCH")
        self.assertEqual(mismatch.error.details["reason"], "ARTIFACT_DIGEST_MISMATCH")
        wrong.chmod(stat.S_IWRITE | stat.S_IREAD)

        self.artifact.chmod(stat.S_IWRITE | stat.S_IREAD)
        writable = await self.sandbox.run(
            self.request(8, run_id="run_sandbox_writable_0001"),
            self.operation,
        )
        self.assertIsInstance(writable, Failure)
        assert isinstance(writable, Failure)
        self.assertEqual(writable.error.details["reason"], "WRITABLE_ARTIFACT")
        self.artifact.chmod(stat.S_IREAD)
        self.assertEqual(list(self.temp_root.iterdir()), [])

    async def test_source_replacement_after_copy_cannot_change_executed_bytes(self) -> None:
        def replace_source_after_materialization(request: SandboxRunRequest) -> tuple[str, ...]:
            self.artifact.chmod(stat.S_IWRITE | stat.S_IREAD)
            self.artifact.write_bytes(b"not an executable")
            return (str(request.input["length"]),)

        sandbox = ProductionCppSandbox(
            self.artifact_root,
            temp_root=self.temp_root,
            argument_builder=replace_source_after_materialization,
        )
        result = await sandbox.run(self.request(8), self.operation)
        self.assertIsInstance(result, Success)
        assert isinstance(result, Success)
        self.assertEqual(len(result.value.action_intents), 8)
        self.assertEqual(list(self.temp_root.iterdir()), [])

    async def test_intent_revision_is_rechecked_at_the_sandbox_boundary(self) -> None:
        request = self.request(1)
        current_state = _state()
        newer_snapshot = WorldSnapshot(
            request_context=self.operation,
            world_id=request.world_id,
            revision=6,
            last_event_sequence=41,
            state_hash=canonical_json_sha256(current_state),
            generated_at=self.operation.requested_at,
            world_rules_version="farm-rules-1",
            state=current_state,
        )
        result = await self.sandbox.run(
            replace(request, world_snapshot=newer_snapshot),
            self.operation,
        )
        self.assertIsInstance(result, Failure)
        assert isinstance(result, Failure)
        self.assertEqual(result.error.details["reason"], "INTENT_REVISION_MISMATCH")

    async def test_only_exact_actor_and_content_identity_can_cancel(self) -> None:
        request = self.request(-1, wall_ms=5_000)
        running = asyncio.create_task(self.sandbox.run(request, self.operation))
        for _ in range(100):
            if self.sandbox._active:
                break
            await asyncio.sleep(0.005)
        other_actor = replace(
            self.operation,
            actor=replace(self.operation.actor, actor_id="student_0002"),
        )
        hidden = await self.sandbox.cancel(request.run_id, "TEST", other_actor)
        self.assertIsInstance(hidden, Success)
        await asyncio.sleep(0.05)
        self.assertFalse(running.done())
        cancelled = await self.sandbox.cancel(request.run_id, "TEST", self.operation)
        self.assertIsInstance(cancelled, Success)
        result = await asyncio.wait_for(running, timeout=5)
        self.assertIsInstance(result, Failure)
        assert isinstance(result, Failure)
        self.assertEqual(result.error.details["reason"], "CANCELLED")
        self.assertEqual(self.sandbox._active, {})


if __name__ == "__main__":
    unittest.main()
