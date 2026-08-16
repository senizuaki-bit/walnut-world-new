from __future__ import annotations

import asyncio
import copy
import json
import sys
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    WORLD_ID,
    StaticRoleConfigs,
    TraceSink,
    decision_output,
    make_context,
    make_event,
    make_learner_profile,
    make_operation,
    make_role_config,
    make_session,
    make_skill,
    make_skill_ref,
    make_task,
    make_versions,
)
from yaya_agent_contracts import (  # noqa: E402
    EvidenceRef,
    EvidenceType,
    LlmPort,
    LlmReply,
    LlmRequest,
    OperationContext,
    Result,
    Success,
    WorldCommitReceipt,
)
from yaya_agent_runtime import (  # noqa: E402
    PEDAGOGY_POLICY_VERSION,
    AgentDependencyError,
    PromptBuilder,
    RunResultSnapshot,
    SharedAgentRuntime,
    SkillVersionSummary,
    TeachingDirective,
    TeachingPhase,
    ToolRegistry,
    TurnContext,
    world_commit_receipt_sha256,
)
from yaya_agent_runtime.adapters import (  # noqa: E402
    HttpResponse,
    OpenAICompatibleConfig,
    OpenAICompatibleLlmAdapter,
)
from yaya_agent_runtime.adapters.openai_compatible import (  # noqa: E402
    ProviderProtocolError,
    ProviderTransportError,
)


class _SequenceTransport:
    def __init__(self, *outcomes: HttpResponse | Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[Mapping[str, object]] = []

    async def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
        timeout_ms: int,
    ) -> HttpResponse:
        del url, headers, timeout_ms
        self.calls.append(body)
        if not self._outcomes:
            raise AssertionError("provider received more requests than declared")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _SlowLlm(LlmPort):
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        request: LlmRequest,
        context: OperationContext,
    ) -> Result[LlmReply]:
        del request, context
        self.calls += 1
        await asyncio.sleep(60)
        raise AssertionError("runtime timeout failed to cancel the provider request")


class _DegradedLlm(LlmPort):
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        request: LlmRequest,
        context: OperationContext,
    ) -> Result[LlmReply]:
        del request, context
        self.calls += 1
        return Success(
            LlmReply(
                output={},
                provider="fixture-provider",
                model="fixture-model",
                source="provider_fallback",
                degraded=True,
                fallback_reason="DEPENDENCY_UNAVAILABLE",
                input_tokens=0,
                output_tokens=0,
                evidence_refs=(),
            )
        )


def _provider_response(output: Mapping[str, object]) -> HttpResponse:
    return _provider_content(
        json.dumps(
            output,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=True,
        )
    )


def _provider_content(content: str) -> HttpResponse:
    body = {
        "model": "provider-model-v1",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    return HttpResponse(
        200,
        {"content-type": "application/json; charset=utf-8"},
        json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )


def _provider(transport: _SequenceTransport) -> OpenAICompatibleLlmAdapter:
    return OpenAICompatibleLlmAdapter(
        OpenAICompatibleConfig(
            endpoint="https://provider.example/v1/chat/completions",
            api_key="provider-secret-for-tests",
            model="provider-model-v1",
            provider="matrix-provider",
            response_format="json_schema",
        ),
        transport,
    )


def _runtime(
    llm: LlmPort,
    *,
    role: str,
    event_type: str,
    timeout_ms: int = 1_000,
) -> tuple[SharedAgentRuntime, TraceSink]:
    trace = TraceSink()
    config = replace(
        make_role_config(
            role,
            allowed_events=(event_type,),
            allowed_tools=(),
        ),
        timeout_ms=timeout_ms,
    )
    runtime = SharedAgentRuntime(
        llm=llm,
        role_configs=StaticRoleConfigs(config),
        tools=ToolRegistry(trace),
        prompts=PromptBuilder(),
        trace=trace,
        versions=make_versions(),
        clock=lambda: NOW,
    )
    return runtime, trace


def _teaching_context() -> TurnContext:
    operation = make_operation()
    event = make_event("run_failed")
    evidence = event.evidence_refs[0]
    learner = make_learner_profile(operation, revision=1)
    directive = TeachingDirective(
        phase=TeachingPhase.RECTIFICATION,
        target_concept="for_loop",
        hint_level=1,
        allowed_response_types=("question", "hint"),
        patch_eligible=False,
        full_solution_eligible=False,
        required_evidence_ids=(evidence.evidence_id,),
        reason_codes=(
            "CURRENT_RUN_FAILED",
            "PATCH_DISABLED_RUNTIME_STAGE",
            "FULL_SOLUTION_DISABLED",
        ),
        pedagogy_policy_version=PEDAGOGY_POLICY_VERSION,
        learner_revision=learner.revision,
        teaching_spec_version="teaching-1",
    )
    run = RunResultSnapshot(
        run_id=cast(str, event.run_id),
        session_id=event.session_id,
        turn_id=event.turn_id,
        command_id=event.command_id,
        world_id="world_watering_0001",
        skill_ref=make_skill_ref(),
        task_success=False,
        world_revision_before=event.expected_world_revision,
        world_revision_after=event.expected_world_revision,
        world_difference={"watered_plots": 7},
        failed_actions=({"reason": "short_loop"},),
        failure_key=event.failure_key,
        evidence_refs=(evidence,),
        world_commit=None,
        request_context=operation,
    )
    return TurnContext(
        role="teaching_agent",
        event=event,
        task=make_task(operation),
        session=make_session(operation=operation),
        hint_level=directive.hint_level,
        skill=make_skill(operation),
        run_result=run,
        learner_profile=learner,
        teaching_directive=directive,
    )


def _teaching_output() -> dict[str, object]:
    return {
        "kind": "decision",
        "decision": {
            "role": "teaching_agent",
            "response_type": "question",
            "message": "The current run stopped after seven plots.",
            "question": "Which loop bound controls the eighth iteration?",
            "hint_level": None,
            "learner_inference": {
                "concept": "for_loop",
                "score_delta": 0.123456,
                "confidence": 0.875001,
                "reason": "The failed run provides direct loop-bound evidence.",
                "evidence_ids": ["evidence_001"],
            },
            "skill_patch": None,
            "requires_student_confirmation": False,
        },
        "tool_calls": [],
    }


def _bug_context() -> TurnContext:
    operation = make_operation()
    event = make_event("run_failed", failure_count=3)
    evidence = event.evidence_refs[0]
    learner = make_learner_profile(operation, revision=1)
    run = RunResultSnapshot(
        run_id=cast(str, event.run_id),
        session_id=event.session_id,
        turn_id=event.turn_id,
        command_id=event.command_id,
        world_id=WORLD_ID,
        skill_ref=make_skill_ref(),
        task_success=False,
        world_revision_before=event.expected_world_revision,
        world_revision_after=event.expected_world_revision,
        world_difference={"watered_plots": 7},
        failed_actions=({"reason": "short_loop"},),
        failure_key=event.failure_key,
        evidence_refs=(evidence,),
        world_commit=None,
        request_context=operation,
    )
    directive = TeachingDirective(
        phase=TeachingPhase.RECTIFICATION,
        target_concept="for_loop",
        hint_level=3,
        allowed_response_types=("question",),
        patch_eligible=False,
        full_solution_eligible=False,
        required_evidence_ids=(evidence.evidence_id,),
        reason_codes=(
            "REPEATED_FAILURE_THRESHOLD_REACHED",
            "PATCH_DISABLED_RUNTIME_STAGE",
            "FULL_SOLUTION_DISABLED",
        ),
        pedagogy_policy_version=PEDAGOGY_POLICY_VERSION,
        learner_revision=learner.revision,
        teaching_spec_version="teaching-1",
    )
    return TurnContext(
        role="bug_agent",
        event=event,
        task=make_task(operation),
        session=make_session(operation=operation),
        hint_level=directive.hint_level,
        skill=make_skill(operation),
        run_result=run,
        failure_history=(run,),
        learner_profile=learner,
        teaching_directive=directive,
    )


def _book_context() -> TurnContext:
    operation = make_operation()
    event = make_event("task_completed")
    learner = make_learner_profile(operation, revision=1)
    world_commit = WorldCommitReceipt(
        world_id=WORLD_ID,
        previous_revision=event.expected_world_revision,
        world_revision=event.expected_world_revision + 1,
        first_event_sequence=41,
        last_event_sequence=48,
        committed_at=NOW,
        state_hash="f" * 64,
    )
    world_evidence = EvidenceRef(
        "evidence_book_world_commit_matrix_0001",
        EvidenceType.WORLD_COMMIT,
        NOW,
        sha256=world_commit_receipt_sha256(world_commit),
    )
    success = RunResultSnapshot(
        run_id=cast(str, event.run_id),
        session_id=event.session_id,
        turn_id=event.turn_id,
        command_id=event.command_id,
        world_id=WORLD_ID,
        skill_ref=make_skill_ref(),
        task_success=True,
        world_revision_before=event.expected_world_revision,
        world_revision_after=event.expected_world_revision + 1,
        world_difference={"watered_plots": 8},
        failed_actions=(),
        failure_key=None,
        evidence_refs=(*event.evidence_refs, world_evidence),
        world_commit=world_commit,
        request_context=operation,
    )
    skill = make_skill(operation)
    directive = TeachingDirective(
        phase=TeachingPhase.SUMMARIZATION,
        target_concept="for_loop",
        hint_level=0,
        allowed_response_types=("growth_summary",),
        patch_eligible=False,
        full_solution_eligible=False,
        required_evidence_ids=tuple(item.evidence_id for item in event.evidence_refs),
        reason_codes=(
            "TASK_COMPLETED_WITH_SUCCESS_EVIDENCE",
            "PATCH_DISABLED_RUNTIME_STAGE",
            "FULL_SOLUTION_DISABLED",
        ),
        pedagogy_policy_version=PEDAGOGY_POLICY_VERSION,
        learner_revision=learner.revision,
        teaching_spec_version="teaching-1",
    )
    return TurnContext(
        role="book_agent",
        event=event,
        task=make_task(operation),
        session=make_session(operation=operation),
        hint_level=0,
        run_result=success,
        learner_profile=learner,
        session_runs=(success,),
        skill_history=(
            SkillVersionSummary(
                event.session_id,
                skill.ref.skill_id,
                skill.ref.skill_version_id,
                skill.source_sha256,
                "Recorded certified Skill version.",
                operation,
            ),
        ),
        teaching_directive=directive,
    )


def _directive_context(role: str) -> TurnContext:
    if role == "bug_agent":
        return _bug_context()
    if role == "book_agent":
        return _book_context()
    raise AssertionError(f"unsupported directive role {role}")


def _decision(value: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], value["decision"])


def _inference(value: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], _decision(value)["learner_inference"])


class ProviderFailureMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_inference_enabled_schema_crosses_provider_boundary(self) -> None:
        context = _teaching_context()
        transport = _SequenceTransport(_provider_response(_teaching_output()))
        runtime, trace = _runtime(
            _provider(transport),
            role="teaching_agent",
            event_type="run_failed",
        )

        result = await runtime.run("teaching_agent", context, make_operation())

        self.assertFalse(result.degraded)
        self.assertEqual(len(transport.calls), 1)
        response_format = cast(dict[str, object], transport.calls[0]["response_format"])
        json_schema = cast(dict[str, object], response_format["json_schema"])
        schema = cast(dict[str, object], json_schema["schema"])
        envelope_properties = cast(dict[str, object], schema["properties"])
        decision_schema = cast(dict[str, object], envelope_properties["decision"])
        raw_variants = decision_schema.get("oneOf")
        decision_variants = (
            cast(list[dict[str, object]], raw_variants)
            if isinstance(raw_variants, list)
            else [decision_schema]
        )
        self.assertEqual(len(decision_variants), 2)
        for variant in decision_variants:
            decision_properties = cast(dict[str, object], variant["properties"])
            inference_schema = cast(dict[str, object], decision_properties["learner_inference"])
            self.assertEqual(inference_schema["type"], "object")
            self.assertNotIn("oneOf", inference_schema)
        inference = result.draft.learner_inference
        self.assertIsNotNone(inference)
        assert inference is not None
        self.assertEqual(inference.evidence_ids, (context.event.evidence_refs[0].evidence_id,))
        self.assertEqual(
            [event.name for event in trace.events],
            ["agent.turn.started", "agent.model.requested", "agent.turn.finished"],
        )

    def test_final_repair_shape_requires_evidence_backed_inference(self) -> None:
        context = _teaching_context()
        messages = PromptBuilder().after_validation_error(
            (),
            role="teaching_agent",
            error_code="INVARIANT_VIOLATION",
            details={"validation_path": "$"},
            final_only=True,
            directive=context.teaching_directive,
            required_evidence_aliases=("evidence_001",),
        )

        instruction = cast(dict[str, object], json.loads(messages[-1].content))
        shape = cast(dict[str, object], instruction["required_final_envelope_shape"])
        decision = cast(dict[str, object], shape["decision"])
        inference = cast(dict[str, object], decision["learner_inference"])
        self.assertEqual(inference["concept"], "for_loop")
        self.assertEqual(inference["evidence_ids"], ["evidence_001"])
        self.assertIn("第一个字符必须是 {", cast(str, instruction["instruction"]))

    async def test_invalid_json_repairs_once_and_repair_exhaustion_falls_back(self) -> None:
        valid = _provider_response(decision_output("world_agent"))
        invalid = _provider_content("{this-is-not-json")
        repaired_transport = _SequenceTransport(invalid, valid)
        repaired_runtime, repaired_trace = _runtime(
            _provider(repaired_transport),
            role="world_agent",
            event_type="task_started",
        )

        repaired = await repaired_runtime.run(
            "world_agent",
            make_context(),
            make_operation(),
        )

        self.assertFalse(repaired.degraded)
        self.assertEqual(len(repaired_transport.calls), 2)
        invalid_traces = [
            event for event in repaired_trace.events if event.name == "agent.output.invalid"
        ]
        self.assertEqual(len(invalid_traces), 1)
        self.assertEqual(invalid_traces[0].fields["error_code"], "INVARIANT_VIOLATION")

        exhausted_transport = _SequenceTransport(invalid, invalid)
        exhausted_runtime, _ = _runtime(
            _provider(exhausted_transport),
            role="world_agent",
            event_type="task_started",
        )

        exhausted = await exhausted_runtime.run(
            "world_agent",
            make_context(),
            make_operation(),
        )

        self.assertTrue(exhausted.degraded)
        self.assertEqual(exhausted.fallback_reason, "MODEL_OUTPUT_INVALID")
        self.assertEqual(exhausted.source, "provider_fallback")
        self.assertEqual(len(exhausted_transport.calls), 2)

    async def test_oversized_provider_output_is_nonrepairable_and_explicit(self) -> None:
        transport = _SequenceTransport(
            ProviderProtocolError("provider response exceeds max_response_bytes")
        )
        runtime, trace = _runtime(
            _provider(transport),
            role="world_agent",
            event_type="task_started",
        )

        result = await runtime.run("world_agent", make_context(), make_operation())

        self.assertTrue(result.degraded)
        self.assertEqual(result.fallback_reason, "MODEL_OUTPUT_INVALID")
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("agent.output.invalid", [event.name for event in trace.events])

    async def test_role_directive_evidence_and_concept_violations_fail_closed(self) -> None:
        cases: list[tuple[str, dict[str, object]]] = []

        wrong_role = copy.deepcopy(_teaching_output())
        _decision(wrong_role)["role"] = "bug_agent"
        cases.append(("wrong_role", wrong_role))

        smuggled_directive = copy.deepcopy(_teaching_output())
        _decision(smuggled_directive)["phase"] = "SUMMARIZATION"
        cases.append(("directive_phase_smuggling", smuggled_directive))

        escalated_hint = copy.deepcopy(_teaching_output())
        _decision(escalated_hint).update(
            {
                "response_type": "hint",
                "question": None,
                "hint_level": 2,
            }
        )
        cases.append(("directive_hint_escalation", escalated_hint))

        fabricated_evidence = copy.deepcopy(_teaching_output())
        _inference(fabricated_evidence)["evidence_ids"] = ["evidence_999"]
        cases.append(("fabricated_evidence", fabricated_evidence))

        missing_inference = copy.deepcopy(_teaching_output())
        _decision(missing_inference)["learner_inference"] = None
        cases.append(("missing_required_inference", missing_inference))

        unauthorized_concept = copy.deepcopy(_teaching_output())
        _inference(unauthorized_concept)["concept"] = "while_loop"
        cases.append(("unauthorized_concept", unauthorized_concept))

        for name, invalid in cases:
            with self.subTest(name=name):
                response = _provider_response(invalid)
                transport = _SequenceTransport(response, response)
                runtime, trace = _runtime(
                    _provider(transport),
                    role="teaching_agent",
                    event_type="run_failed",
                )

                result = await runtime.run(
                    "teaching_agent",
                    _teaching_context(),
                    make_operation(),
                )

                self.assertTrue(result.degraded)
                self.assertEqual(result.fallback_reason, "MODEL_OUTPUT_INVALID")
                self.assertEqual(len(transport.calls), 2)
                invalid_traces = [
                    event for event in trace.events if event.name == "agent.output.invalid"
                ]
                self.assertEqual(len(invalid_traces), 1)
                self.assertEqual(
                    invalid_traces[0].fields["error_code"],
                    "INVARIANT_VIOLATION",
                )

    async def test_score_and_confidence_numeric_contract_fails_closed(self) -> None:
        cases = (
            ("score_nan", "score_delta", float("nan")),
            ("score_positive_infinity", "score_delta", float("inf")),
            ("confidence_negative_infinity", "confidence", float("-inf")),
            ("score_below_minimum", "score_delta", -0.300001),
            ("score_above_maximum", "score_delta", 0.300001),
            ("confidence_below_minimum", "confidence", -0.000001),
            ("confidence_above_maximum", "confidence", 1.000001),
            ("score_excess_precision", "score_delta", 0.1234567),
            ("confidence_excess_precision", "confidence", 0.7654321),
        )
        for name, field, value in cases:
            with self.subTest(name=name):
                invalid = _teaching_output()
                _inference(invalid)[field] = value
                response = _provider_response(invalid)
                transport = _SequenceTransport(response, response)
                runtime, _ = _runtime(
                    _provider(transport),
                    role="teaching_agent",
                    event_type="run_failed",
                )

                result = await runtime.run(
                    "teaching_agent",
                    _teaching_context(),
                    make_operation(),
                )

                self.assertTrue(result.degraded)
                self.assertEqual(result.fallback_reason, "MODEL_OUTPUT_INVALID")
                self.assertEqual(len(transport.calls), 2)

    async def test_runtime_timeout_and_provider_unavailability_are_explicit(self) -> None:
        slow = _SlowLlm()
        timeout_runtime, _ = _runtime(
            slow,
            role="world_agent",
            event_type="task_started",
            timeout_ms=10,
        )

        timed_out = await timeout_runtime.run(
            "world_agent",
            make_context(),
            make_operation(),
        )

        self.assertTrue(timed_out.degraded)
        self.assertEqual(timed_out.fallback_reason, "LLM_TIMEOUT")
        self.assertEqual(slow.calls, 1)

        unavailable_transport = _SequenceTransport(
            ProviderTransportError("provider is unavailable")
        )
        unavailable_runtime, _ = _runtime(
            _provider(unavailable_transport),
            role="world_agent",
            event_type="task_started",
        )

        unavailable = await unavailable_runtime.run(
            "world_agent",
            make_context(),
            make_operation(),
        )

        self.assertTrue(unavailable.degraded)
        self.assertEqual(unavailable.fallback_reason, "DEPENDENCY_UNAVAILABLE")
        self.assertEqual(len(unavailable_transport.calls), 1)

    async def test_bug_and_book_dependency_failures_raise_without_publication(self) -> None:
        for role, event_type in (
            ("bug_agent", "run_failed"),
            ("book_agent", "task_completed"),
        ):
            with self.subTest(role=role, failure="timeout"):
                slow = _SlowLlm()
                runtime, trace = _runtime(
                    slow,
                    role=role,
                    event_type=event_type,
                    timeout_ms=10,
                )
                with self.assertRaises(AgentDependencyError) as raised:
                    await runtime.run(role, _directive_context(role), make_operation())
                self.assertEqual(raised.exception.code, "DIRECTIVE_PROVIDER_OUTPUT_UNTRUSTED")
                self.assertEqual(raised.exception.details["reason"], "LLM_TIMEOUT")
                self.assertEqual(slow.calls, 1)
                self.assertNotIn("agent.turn.finished", [item.name for item in trace.events])
                self.assertEqual(trace.events[-1].name, "agent.turn.failed")

            with self.subTest(role=role, failure="unavailable"):
                transport = _SequenceTransport(ProviderTransportError("provider is unavailable"))
                runtime, trace = _runtime(
                    _provider(transport),
                    role=role,
                    event_type=event_type,
                )
                with self.assertRaises(AgentDependencyError) as raised:
                    await runtime.run(role, _directive_context(role), make_operation())
                self.assertEqual(raised.exception.code, "DIRECTIVE_PROVIDER_OUTPUT_UNTRUSTED")
                self.assertEqual(
                    raised.exception.details["reason"],
                    "DEPENDENCY_UNAVAILABLE",
                )
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn("agent.turn.finished", [item.name for item in trace.events])
                self.assertEqual(trace.events[-1].name, "agent.turn.failed")

            with self.subTest(role=role, failure="degraded_reply"):
                degraded = _DegradedLlm()
                runtime, trace = _runtime(
                    degraded,
                    role=role,
                    event_type=event_type,
                )
                with self.assertRaises(AgentDependencyError) as raised:
                    await runtime.run(role, _directive_context(role), make_operation())
                self.assertEqual(raised.exception.code, "DIRECTIVE_PROVIDER_OUTPUT_UNTRUSTED")
                self.assertEqual(
                    raised.exception.details["reason"],
                    "DEPENDENCY_UNAVAILABLE",
                )
                self.assertEqual(degraded.calls, 1)
                self.assertNotIn("agent.turn.finished", [item.name for item in trace.events])
                self.assertEqual(trace.events[-1].name, "agent.turn.failed")

    async def test_bug_and_book_repair_exhaustion_raises_without_publication(self) -> None:
        invalid_json = _provider_content("{this-is-not-json")
        for role, event_type in (
            ("bug_agent", "run_failed"),
            ("book_agent", "task_completed"),
        ):
            with self.subTest(role=role):
                transport = _SequenceTransport(invalid_json, invalid_json)
                runtime, trace = _runtime(
                    _provider(transport),
                    role=role,
                    event_type=event_type,
                )
                with self.assertRaises(AgentDependencyError) as raised:
                    await runtime.run(role, _directive_context(role), make_operation())
                self.assertEqual(raised.exception.code, "DIRECTIVE_PROVIDER_OUTPUT_UNTRUSTED")
                self.assertEqual(
                    raised.exception.details["reason"],
                    "MODEL_OUTPUT_INVALID",
                )
                self.assertEqual(len(transport.calls), 2)
                self.assertNotIn("agent.turn.finished", [item.name for item in trace.events])
                self.assertEqual(trace.events[-1].name, "agent.turn.failed")

    def test_provider_configuration_cannot_be_missing_or_blank(self) -> None:
        valid = {
            "endpoint": "https://provider.example/v1/chat/completions",
            "api_key": "provider-secret-for-tests",
            "model": "provider-model-v1",
            "provider": "matrix-provider",
        }
        for name, missing in (
            ("endpoint", ""),
            ("api_key", ""),
            ("model", ""),
            ("provider", ""),
        ):
            with self.subTest(name=name):
                values = {**valid, name: missing}
                with self.assertRaises(ValueError) as raised:
                    OpenAICompatibleConfig(**values)  # type: ignore[arg-type]
                self.assertTrue(str(raised.exception))


if __name__ == "__main__":
    unittest.main()
