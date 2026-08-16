from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    SequenceLlm,
    StaticRoleConfigs,
    TraceSink,
    decision_output,
    make_context,
    make_operation,
    make_reply,
    make_role_config,
    make_versions,
)
from yaya_agent_runtime import (  # noqa: E402
    AgentDependencyError,
    AgentRuntimeError,
    PromptBuilder,
    RuntimeBoundaryError,
    RuntimeBoundaryStage,
    SharedAgentRuntime,
    ToolRegistry,
)


def _runtime() -> SharedAgentRuntime:
    trace = TraceSink()
    return SharedAgentRuntime(
        llm=SequenceLlm([make_reply(decision_output("world_agent"))]),
        role_configs=StaticRoleConfigs(make_role_config("world_agent")),
        tools=ToolRegistry(trace),
        prompts=PromptBuilder(),
        trace=trace,
        versions=make_versions(),
        clock=lambda: NOW,
    )


class RuntimeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_value_error_becomes_fixed_redacted_substage(self) -> None:
        secret = "secret result receipt at C:/private/provider.json"
        runtime = _runtime()
        with patch.object(
            runtime._llm,  # noqa: SLF001 - boundary fault injection
            "generate",
            new=AsyncMock(side_effect=ValueError(secret)),
        ):
            with self.assertRaises(RuntimeBoundaryError) as raised:
                await runtime.run("world_agent", make_context(), make_operation())

        error = raised.exception
        self.assertEqual(error.stage, RuntimeBoundaryStage.LLM_GENERATE)
        self.assertNotIn(secret, repr(error.args))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    async def test_raw_value_errors_become_fixed_redacted_substages(self) -> None:
        secret = "secret prompt at C:/private/student.json"
        cases = (
            (
                "yaya_agent_runtime.runtime.parse_model_envelope",
                RuntimeBoundaryStage.PARSE_MODEL_ENVELOPE,
            ),
            (
                "yaya_agent_runtime.runtime.validate_decision",
                RuntimeBoundaryStage.VALIDATE_DECISION,
            ),
            (
                "yaya_agent_runtime.runtime._merge_evidence",
                RuntimeBoundaryStage.MERGE_EVIDENCE,
            ),
            (
                "yaya_agent_runtime.runtime._decision_completed_at",
                RuntimeBoundaryStage.DECISION_TIME,
            ),
            (
                "yaya_agent_runtime.runtime.AgentDecision",
                RuntimeBoundaryStage.CONSTRUCT_AGENT_DECISION,
            ),
        )

        for target, expected_stage in cases:
            with self.subTest(stage=expected_stage):
                with patch(target, side_effect=ValueError(secret)):
                    with self.assertRaises(RuntimeBoundaryError) as raised:
                        await _runtime().run(
                            "world_agent",
                            make_context(),
                            make_operation(),
                        )

                error = raised.exception
                self.assertEqual(error.stage, expected_stage)
                self.assertEqual(
                    str(error),
                    "agent runtime rejected a value at a fixed boundary",
                )
                self.assertNotIn(secret, repr(error.args))
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)

    def test_boundary_failure_is_not_an_agent_runtime_policy_error(self) -> None:
        self.assertFalse(issubclass(RuntimeBoundaryError, AgentRuntimeError))

    async def test_agent_runtime_error_from_llm_is_not_reclassified(self) -> None:
        expected = AgentDependencyError(
            "FIXTURE_AGENT_ERROR",
            "existing typed runtime failure",
            {"role": "world_agent"},
        )
        runtime = _runtime()
        with patch.object(
            runtime._llm,  # noqa: SLF001 - boundary fault injection
            "generate",
            new=AsyncMock(side_effect=expected),
        ):
            with self.assertRaises(AgentDependencyError) as raised:
                await runtime.run("world_agent", make_context(), make_operation())

        self.assertIs(raised.exception, expected)
        self.assertEqual(raised.exception.code, "FIXTURE_AGENT_ERROR")
        self.assertEqual(dict(raised.exception.details), {"role": "world_agent"})

    async def test_invalid_agent_output_semantics_remain_repairable(self) -> None:
        trace = TraceSink()
        runtime = SharedAgentRuntime(
            llm=SequenceLlm(
                [
                    make_reply(decision_output("xiaohutao", "Wrong role.")),
                    make_reply(decision_output("world_agent", "Repaired role.")),
                ]
            ),
            role_configs=StaticRoleConfigs(make_role_config("world_agent")),
            tools=ToolRegistry(trace),
            prompts=PromptBuilder(),
            trace=trace,
            versions=make_versions(),
            clock=lambda: NOW,
        )

        decision = await runtime.run("world_agent", make_context(), make_operation())

        self.assertFalse(decision.degraded)
        invalid = [event for event in trace.events if event.name == "agent.output.invalid"]
        self.assertEqual([event.fields["error_code"] for event in invalid], ["ROLE_MISMATCH"])


if __name__ == "__main__":
    unittest.main()
