from __future__ import annotations

import asyncio
import hashlib
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    make_operation,
    make_world_snapshot,
    make_world_state,
)
from test_agent_native_cpp_watering import _compile_cpp  # noqa: E402
from yaya_agent_backend.world import (  # noqa: E402
    WateringWorldEngine,
    WorldRuleViolation,
)
from yaya_agent_contracts import (  # noqa: E402
    Failure,
    SandboxLimits,
    SandboxRunRequest,
    SkillRef,
    Success,
    WaterIntent,
    canonical_json_sha256,
)
from yaya_agent_sandbox import ProductionCppSandbox  # noqa: E402

_PRODUCTION_SANDBOX_SOURCE = r"""
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
    if (length == -2) {
        std::cout << "{\"watered\":8,\"total\":8,\"task_success\":true}";
        return 0;
    }
    if (length < 0 || length > 8) {
        return 3;
    }
    std::cout << "{\"actions\":[";
    for (int index = 1; index <= length; ++index) {
        if (index != 1) {
            std::cout << ',';
        }
        std::cout
            << "{\"intent_id\":\"intent_water_000" << index
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


def _request(
    skill_ref: SkillRef,
    length: int,
    *,
    run_id: str,
    wall_ms: int = 2_000,
) -> SandboxRunRequest:
    state = make_world_state(plot_count=8, hydration=0)
    world_snapshot = replace(
        make_world_snapshot(state=state),
        state_hash=canonical_json_sha256(state),
    )
    return SandboxRunRequest(
        run_id=run_id,
        skill_ref=skill_ref,
        world_id="world_watering_0001",
        world_snapshot=world_snapshot,
        input={"length": length},
        deterministic_seed="watering-seed-0001",
        limits=SandboxLimits(
            cpu_ms=1_000,
            wall_ms=wall_ms,
            memory_bytes=67_108_864,
            max_intents=8,
            max_output_bytes=65_536,
            max_processes=1,
        ),
    )


class ProductionCppSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_cpp_intents_timeout_cleanup_and_fake_success_rejection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yaya-production-sandbox-test-") as raw_root:
            root = Path(raw_root).resolve()
            build_root = root / "build"
            artifact_root = root / "artifacts"
            temp_root = root / "work"
            build_root.mkdir()
            artifact_root.mkdir()
            temp_root.mkdir()
            executable = _compile_cpp(_PRODUCTION_SANDBOX_SOURCE, build_root)
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            artifact = artifact_root / digest
            shutil.copyfile(executable, artifact)
            artifact.chmod(stat.S_IREAD)
            skill_ref = SkillRef(
                skill_id="skill_sandbox_watering_0001",
                skill_version_id="skill_version_sandbox_watering_0001",
                artifact_sha256=digest,
                certification_id="certification_sandbox_watering_0001",
            )
            sandbox = ProductionCppSandbox(artifact_root, temp_root=temp_root)
            world_engine = WateringWorldEngine()
            operation = make_operation()
            try:
                seven_request = _request(
                    skill_ref,
                    7,
                    run_id="run_sandbox_seven_0001",
                )
                seven = await sandbox.run(seven_request, operation)
                self.assertIsInstance(seven, Success)
                assert isinstance(seven, Success)
                self.assertEqual(len(seven.value.action_intents), 7)
                self.assertTrue(
                    all(isinstance(intent, WaterIntent) for intent in seven.value.action_intents)
                )
                self.assertEqual(
                    [intent.plot_id for intent in seven.value.action_intents],
                    [f"plot_{index:04d}" for index in range(1, 8)],
                )
                seven_proposal = world_engine.stage(
                    seven_request.world_snapshot,
                    skill_ref,
                    seven.value.action_intents,
                )
                self.assertFalse(seven_proposal.task_success)
                self.assertFalse(seven_proposal.commit_eligible)
                self.assertEqual(seven_proposal.failure_key, "watering_loop_short")
                self.assertEqual(seven_proposal.revision_before, 5)
                self.assertEqual(seven_proposal.revision_after, 5)
                self.assertEqual(seven_proposal.sequence_before, 40)
                self.assertEqual(seven_proposal.sequence_after, 40)
                self.assertTrue(
                    all(
                        plot["hydration"] == 0
                        for plot in seven_request.world_snapshot.state["plots"]
                    )
                )

                eight_request = _request(
                    skill_ref,
                    8,
                    run_id="run_sandbox_eight_0001",
                )
                eight = await sandbox.run(eight_request, operation)
                self.assertIsInstance(eight, Success)
                assert isinstance(eight, Success)
                self.assertEqual(len(eight.value.action_intents), 8)
                eight_proposal = world_engine.stage(
                    eight_request.world_snapshot,
                    skill_ref,
                    eight.value.action_intents,
                )
                self.assertTrue(eight_proposal.task_success)
                self.assertTrue(eight_proposal.commit_eligible)
                self.assertIsNone(eight_proposal.failure_key)
                self.assertEqual(eight_proposal.revision_before, 5)
                self.assertEqual(eight_proposal.revision_after, 6)
                self.assertEqual(eight_proposal.sequence_before, 40)
                self.assertEqual(eight_proposal.sequence_after, 41)
                self.assertTrue(
                    all(plot["hydration"] == 100 for plot in eight_proposal.staged_state["plots"])
                )
                stale_intents = (
                    replace(
                        eight.value.action_intents[0],
                        expected_world_revision=4,
                    ),
                    *eight.value.action_intents[1:],
                )
                with self.assertRaises(WorldRuleViolation) as stale_error:
                    world_engine.stage(
                        eight_request.world_snapshot,
                        skill_ref,
                        stale_intents,
                    )
                self.assertEqual(stale_error.exception.code, "WORLD_REVISION_CONFLICT")
                self.assertEqual(stale_error.exception.reason, "WORLD_REVISION_CONFLICT")
                self.assertTrue(
                    all(
                        plot["hydration"] == 0
                        for plot in eight_request.world_snapshot.state["plots"]
                    )
                )

                real_popen = subprocess.Popen
                spawned: list[subprocess.Popen[bytes]] = []

                def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
                    process = real_popen(*args, **kwargs)  # type: ignore[arg-type]
                    spawned.append(process)
                    return process

                with patch(
                    "yaya_agent_sandbox.native.subprocess.Popen",
                    side_effect=recording_popen,
                ):
                    timed_out = await sandbox.run(
                        _request(
                            skill_ref,
                            -1,
                            run_id="run_sandbox_timeout_0001",
                            wall_ms=250,
                        ),
                        operation,
                    )

                self.assertIsInstance(timed_out, Failure)
                assert isinstance(timed_out, Failure)
                self.assertEqual(timed_out.error.code, "SANDBOX_RESOURCE_LIMIT")
                self.assertEqual(timed_out.error.details["reason"], "WALL_TIMEOUT")
                self.assertEqual(len(spawned), 1)
                self.assertIsNotNone(spawned[0].returncode)
                self.assertIsNotNone(spawned[0].stdout)
                self.assertIsNotNone(spawned[0].stderr)
                assert spawned[0].stdout is not None
                assert spawned[0].stderr is not None
                self.assertTrue(spawned[0].stdout.closed)
                self.assertTrue(spawned[0].stderr.closed)
                self.assertFalse(
                    any(
                        thread.is_alive() and thread.name.startswith("run_sandbox_timeout_0001-")
                        for thread in threading.enumerate()
                    )
                )
                self.assertEqual(sandbox._active, {})

                cancellable_request = _request(
                    skill_ref,
                    -1,
                    run_id="run_sandbox_cancel_0001",
                    wall_ms=5_000,
                )
                running = asyncio.create_task(sandbox.run(cancellable_request, operation))
                for _ in range(200):
                    if sandbox._active:
                        break
                    await asyncio.sleep(0.005)
                else:
                    self.fail("Sandbox run never became active")
                other_tenant = replace(
                    operation,
                    actor=replace(operation.actor, tenant_id="tenant_other"),
                )
                hidden_cancel = await sandbox.cancel(
                    cancellable_request.run_id,
                    "TEST_CROSS_TENANT",
                    other_tenant,
                )
                self.assertIsInstance(hidden_cancel, Success)
                await asyncio.sleep(0.05)
                self.assertFalse(running.done())
                own_cancel = await sandbox.cancel(
                    cancellable_request.run_id,
                    "TEST_OWNER",
                    operation,
                )
                self.assertIsInstance(own_cancel, Success)
                cancelled = await asyncio.wait_for(running, timeout=5)
                self.assertIsInstance(cancelled, Failure)
                assert isinstance(cancelled, Failure)
                self.assertEqual(cancelled.error.code, "SANDBOX_RUNTIME_ERROR")
                self.assertEqual(cancelled.error.details["reason"], "CANCELLED")
                self.assertEqual(sandbox._active, {})

                fake_success = await sandbox.run(
                    _request(skill_ref, -2, run_id="run_sandbox_fake_success_0001"),
                    operation,
                )
                self.assertIsInstance(fake_success, Failure)
                assert isinstance(fake_success, Failure)
                self.assertEqual(fake_success.error.code, "SANDBOX_RUNTIME_ERROR")
                self.assertEqual(fake_success.error.details["reason"], "INVALID_ACTION_ARRAY")
                self.assertEqual(sandbox._active, {})
            finally:
                artifact.chmod(stat.S_IWRITE | stat.S_IREAD)


if __name__ == "__main__":
    unittest.main()
