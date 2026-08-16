from __future__ import annotations

import sys
import unittest
from pathlib import Path

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
    tool_calls_output,
)
from yaya_agent_runtime import (  # noqa: E402
    AgentTool,
    LearnerInference,
    PromptBuilder,
    SharedAgentRuntime,
    ToolRegistry,
    ToolResult,
)


class _CountingToolHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, arguments, turn_context, execution, operation_context):
        del arguments, turn_context, execution, operation_context
        self.calls += 1
        return ToolResult(value={"ok": True}, summary={"ok": True})


def _empty_schema(_context) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {},
    }


def _make_runtime(
    llm: SequenceLlm,
    configs: StaticRoleConfigs,
    tools: ToolRegistry,
    trace: TraceSink,
) -> SharedAgentRuntime:
    return SharedAgentRuntime(
        llm=llm,
        role_configs=configs,
        tools=tools,
        prompts=PromptBuilder(),
        trace=trace,
        versions=make_versions(),
        clock=lambda: NOW,
    )


class SharedAgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_learner_inference_rejects_contract_precision_and_bounds(self) -> None:
        valid = LearnerInference(
            concept="for_loop",
            score_delta=0.123456,
            confidence=0.875001,
            reason="Bounded evidence-based inference.",
            evidence_ids=("evidence_runtime_0001",),
        )
        self.assertEqual(valid.score_delta, 0.123456)
        invalid_values = (
            (0.1234567, 0.8, "six decimal"),
            (0.1, 0.1234567, "six decimal"),
            (0.300001, 0.8, "between -0.3 and 0.3"),
            (0.1, 1.000001, "between 0 and 1"),
        )
        for score_delta, confidence, message in invalid_values:
            with self.subTest(score_delta=score_delta, confidence=confidence):
                with self.assertRaisesRegex(ValueError, message):
                    LearnerInference(
                        concept="for_loop",
                        score_delta=score_delta,
                        confidence=confidence,
                        reason="Invalid bounded inference.",
                        evidence_ids=("evidence_runtime_0001",),
                    )

    async def test_normal_valid_decision_finishes_without_degradation(self) -> None:
        trace = TraceSink()
        llm = SequenceLlm(
            [make_reply(decision_output("world_agent"), input_tokens=11, output_tokens=13)]
        )
        configs = StaticRoleConfigs(make_role_config("world_agent"))
        runtime = _make_runtime(llm, configs, ToolRegistry(trace), trace)

        decision = await runtime.run("world_agent", make_context(), make_operation())

        self.assertFalse(decision.degraded)
        self.assertEqual(decision.source, "provider")
        self.assertEqual(decision.input_tokens, 11)
        self.assertEqual(decision.output_tokens, 13)
        self.assertEqual(decision.tool_calls, ())
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(
            [event.name for event in trace.events],
            ["agent.turn.started", "agent.model.requested", "agent.turn.finished"],
        )

    async def test_one_invalid_output_is_repaired_once(self) -> None:
        trace = TraceSink()
        llm = SequenceLlm(
            [
                make_reply(decision_output("xiaohutao", "Wrong routed role.")),
                make_reply(decision_output("world_agent", "Corrected role.")),
            ]
        )
        configs = StaticRoleConfigs(make_role_config("world_agent"))
        runtime = _make_runtime(llm, configs, ToolRegistry(trace), trace)

        decision = await runtime.run("world_agent", make_context(), make_operation())

        self.assertFalse(decision.degraded)
        self.assertIn("可观察目标", decision.message)
        self.assertNotIn("Corrected role.", decision.message)
        self.assertEqual(len(llm.requests), 2)
        invalid = [event for event in trace.events if event.name == "agent.output.invalid"]
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0].fields["error_code"], "ROLE_MISMATCH")
        self.assertEqual(invalid[0].fields["repair_attempt"], 1)

    async def test_second_invalid_output_returns_explicit_fallback(self) -> None:
        trace = TraceSink()
        invalid = make_reply(decision_output("xiaohutao", "Wrong routed role."))
        llm = SequenceLlm([invalid, invalid])
        configs = StaticRoleConfigs(make_role_config("world_agent"))
        runtime = _make_runtime(llm, configs, ToolRegistry(trace), trace)

        decision = await runtime.run("world_agent", make_context(), make_operation())

        self.assertTrue(decision.degraded)
        self.assertEqual(decision.source, "provider_fallback")
        self.assertEqual(decision.fallback_reason, "MODEL_OUTPUT_INVALID")
        self.assertEqual(decision.provider, "runtime")
        self.assertEqual(len(llm.requests), 2)
        finished = [event for event in trace.events if event.name == "agent.turn.finished"]
        self.assertTrue(finished[-1].fields["fallback"])

    async def test_second_tool_round_is_never_executed_and_eventually_falls_back(self) -> None:
        trace = TraceSink()
        handler = _CountingToolHandler()
        tools = ToolRegistry(trace)
        tools.register(
            AgentTool(
                name="probe_tool",
                description="Count executions to enforce the one-round invariant.",
                schema_factory=_empty_schema,
                allowed_roles=frozenset({"world_agent"}),
                handler=handler,
            )
        )
        configs = StaticRoleConfigs(
            make_role_config(
                "world_agent",
                allowed_tools=("probe_tool",),
                max_tool_calls=1,
            )
        )
        llm = SequenceLlm(
            [
                make_reply(tool_calls_output("probe_tool", {}, call_id="call_round_0001")),
                make_reply(tool_calls_output("probe_tool", {}, call_id="call_round_0002")),
                make_reply(tool_calls_output("probe_tool", {}, call_id="call_round_0003")),
            ]
        )
        runtime = _make_runtime(llm, configs, tools, trace)

        decision = await runtime.run("world_agent", make_context(), make_operation())

        self.assertTrue(decision.degraded)
        self.assertEqual(decision.fallback_reason, "MODEL_OUTPUT_INVALID")
        self.assertEqual(handler.calls, 1)
        self.assertEqual(len(decision.tool_calls), 1)
        self.assertEqual(len(llm.requests), 3)
        invalid = [event for event in trace.events if event.name == "agent.output.invalid"]
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0].fields["error_code"], "TOOL_LOOP_LIMIT")

    async def test_xiaohutao_cannot_succeed_without_invoke_skill(self) -> None:
        trace = TraceSink()
        handler = _CountingToolHandler()
        tools = ToolRegistry(trace)
        tools.register(
            AgentTool(
                name="invoke_skill",
                description="A fixture side-effect tool that must be explicitly invoked.",
                schema_factory=_empty_schema,
                allowed_roles=frozenset({"xiaohutao"}),
                handler=handler,
            )
        )
        configs = StaticRoleConfigs(
            make_role_config(
                "xiaohutao",
                allowed_tools=("invoke_skill",),
                max_tool_calls=1,
            )
        )
        direct = make_reply(decision_output("xiaohutao", "I claim success without a run."))
        llm = SequenceLlm([direct, direct])
        runtime = _make_runtime(llm, configs, tools, trace)

        decision = await runtime.run("xiaohutao", make_context("xiaohutao"), make_operation())

        self.assertTrue(decision.degraded)
        self.assertEqual(decision.fallback_reason, "MODEL_OUTPUT_INVALID")
        self.assertEqual(decision.tool_calls, ())
        self.assertEqual(handler.calls, 0)
        invalid = [event for event in trace.events if event.name == "agent.output.invalid"]
        self.assertEqual(invalid[0].fields["error_code"], "SKILL_INVOCATION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
