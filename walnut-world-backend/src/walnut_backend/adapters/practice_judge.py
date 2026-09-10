"""Compile and grade practice in the existing pinned, networkless Docker builder."""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

from yaya_agent_build import (
    BuildResourceLimits,
    CppTestCase,
    CppTestSuite,
    DigestPinnedDockerCppBuilder,
)
from yaya_agent_contracts import (
    CompileAndTestRequest,
    SandboxLimits,
    SkillSourceBundle,
    SkillSourceFile,
)
from yaya_agent_runtime.bug_practice import expected_output, source_digest


class PracticeJudge:
    async def grade(self, problem, source):
        if not isinstance(source, str) or not source.strip() or len(source.encode()) > 32000:
            raise ValueError("PRACTICE_SOURCE_INVALID")
        return await asyncio.to_thread(self._grade, problem, source)

    def _grade(self, problem, source):
        root = Path(os.environ["WALNUT_RUNTIME_ROOT"]) / "practice-builds"
        root.mkdir(parents=True, exist_ok=True)
        m, t = problem["moisture"], problem["target"]
        # Additional inputs keep a hard-coded transcript from passing the exercise.
        hidden_m = [60, 29, 30, 31, 0, 90, 50, 45]
        hidden_t = [60, 60, 60, 60, 40, 65, 49, 46]

        def case(name, visibility, moisture, target):
            return CppTestCase(
                name,
                visibility,
                stdin=(" ".join(map(str, moisture + target)) + "\n").encode(),
                expected_stdout_sha256=source_digest(expected_output(moisture, target)),
            )

        suite = CppTestSuite(
            "bug-practice-v1",
            (case("practice_public", "PUBLIC", m, t),),
            (case("practice_boundaries", "HIDDEN", hidden_m, hidden_t),),
        )
        limits = BuildResourceLimits(compile_wall_ms=30000, test_wall_ms=3000)
        builder = DigestPinnedDockerCppBuilder(
            root,
            image=os.environ["WALNUT_SANDBOX_IMAGE"],
            compiler_version="14.2.0",
            test_suites=(suite,),
            limits=limits,
            docker_executable=os.getenv("WALNUT_DOCKER_EXECUTABLE", "docker"),
        )
        request = CompileAndTestRequest(
            build_id="build_" + uuid4().hex,
            skill_id="skill_bug_practice",
            compiler_profile="YAYA_CPP20_SAFE_V1",
            test_suite_version=suite.version,
            source_bundle=SkillSourceBundle(
                "main.cpp", (SkillSourceFile("main.cpp", source, source_digest(source)),)
            ),
            limits=SandboxLimits(
                cpu_ms=30000,
                wall_ms=30000,
                memory_bytes=limits.memory_bytes,
                max_intents=8,
                max_output_bytes=65536,
                max_processes=64,
                network_access=False,
            ),
        )
        result = builder.build(request)
        if result.failure is not None and result.failure.retryable:
            raise ValueError("PRACTICE_JUDGE_UNAVAILABLE")
        correct = result.status == "SUCCEEDED"
        stage = "PASSED" if correct else result.failure.stage if result.failure else "TEST"
        message = (
            "变式挑战通过！公开数据和边界测试都正确。"
            if correct
            else (
                "代码还没有编译通过，请检查语法后再试。"
                if stage in {"COMPILE", "VALIDATE_SOURCE"}
                else "输出还没有符合全部规则。检查同一下标、缺口为 0 和 30 时的动作，以及每行 WATER 的格式。"
            )
        )
        return {
            "correct": correct,
            "stage": stage,
            "message": message,
            "build_id": result.build_id,
            "status": "SUCCEEDED" if correct else "REJECTED",
            "compiler_profile": result.compiler_profile,
            "test_suite_version": result.test_suite_version,
            "diagnostics": [{"code": d.code, "message": d.message} for d in result.diagnostics],
        }
