from __future__ import annotations

import hashlib
import json
import sys
import unittest
from dataclasses import replace
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
    make_evidence,
    make_learner_profile,
    make_operation,
    make_role_config,
    make_session,
    make_skill,
    make_skill_ref,
    make_task,
    make_versions,
)
from yaya_agent_backend.codec import decode_as, encode  # noqa: E402
from yaya_agent_contracts import ActorType  # noqa: E402
from yaya_agent_runtime import (  # noqa: E402
    AgentConfigurationError,
    AgentContextError,
    AgentDependencyError,
    CompileResultSnapshot,
    ContextBuilder,
    DraftAuthority,
    DraftReadPort,
    DraftSnapshot,
    FailedInteractionSnapshot,
    GameEvent,
    InteractionReadPort,
    PedagogyEvidence,
    PedagogyEvidenceOutcome,
    PedagogyInput,
    PedagogyPolicy,
    PromptBuilder,
    RoleLimits,
    RoleRouter,
    RunResultSnapshot,
    SharedAgentRuntime,
    SkillPatchProposal,
    TeachingPhase,
    ToolRegistry,
    build_default_tool_registry,
)
from yaya_agent_runtime.model_output import (  # noqa: E402
    build_model_output_schema,
    parse_model_envelope,
)

SOURCE = "int main() {\n    harvest(0);\n    return 0;\n}\n"
SOURCE_SHA = hashlib.sha256(SOURCE.encode("utf-8")).hexdigest()
REPLACEMENT = (
    "int main() {\n"
    "    for (int index = 0; index < 8; ++index) {\n"
    "        harvest(index);\n"
    "    }\n"
    "    return 0;\n"
    "}\n"
)
REPLACEMENT_SHA = hashlib.sha256(REPLACEMENT.encode("utf-8")).hexdigest()
RATIONALE = "Iterate over all eight plots from the failed Run Evidence."
STUDENT_ID = "student_harvest_0001"
TASK_ID = "task_harvest_0001"
SESSION_ID = "session_harvest_0001"
TURN_ID = "turn_harvest_request_0001"
COMMAND_ID = "cmd_harvest_request_0001"
WORLD_ID = "world_harvest_0001"
FAILED_TURN_ID = "turn_harvest_failed_0004"
FAILED_COMMAND_ID = "cmd_harvest_failed_0004"


def _operation():
    return make_operation(actor_id=STUDENT_ID, command_id=COMMAND_ID)


def _skill_ref():
    return replace(
        make_skill_ref(),
        skill_id="skill_harvest_0001",
        skill_version_id="skill_version_harvest_0001",
        certification_id="certification_harvest_0001",
    )


def _authority(**changes: object) -> DraftAuthority:
    values: dict[str, object] = {
        "draft_id": "draft_student_0001",
        "session_id": SESSION_ID,
        "skill_id": _skill_ref().skill_id,
        "draft_revision": 3,
        "draft_sha256": "d" * 64,
        "source_bundle_sha256": "b" * 64,
        "entrypoint": "main.cpp",
        "entrypoint_sha256": SOURCE_SHA,
    }
    values.update(changes)
    return DraftAuthority(**values)  # type: ignore[arg-type]


def _patch_payload(
    authority: DraftAuthority | None = None,
    *,
    feature_enabled: bool = True,
    capability_enabled: bool = True,
) -> dict[str, object]:
    target = authority or _authority()
    return {
        "source_event_type": "UI_ACTION",
        "action_id": "request_ai_patch",
        "requested_interaction_id": "interaction_failed_0001",
        "feature_enabled": feature_enabled,
        "capability_enabled": capability_enabled,
        "effective_hint_level": 4,
        "draft_authority": {
            "draft_id": target.draft_id,
            "session_id": target.session_id,
            "skill_id": target.skill_id,
            "draft_revision": target.draft_revision,
            "draft_sha256": target.draft_sha256,
            "source_bundle_sha256": target.source_bundle_sha256,
            "entrypoint": target.entrypoint,
            "entrypoint_sha256": target.entrypoint_sha256,
        },
    }


def _patch_event(
    authority: DraftAuthority | None = None,
    *,
    feature_enabled: bool = True,
    capability_enabled: bool = True,
) -> GameEvent:
    evidence = make_evidence("evidence_patch_fail_0001")
    return GameEvent(
        event_id="event_skill_patch_requested_0001",
        event_type="skill_patch_requested",
        student_id=STUDENT_ID,
        task_id=TASK_ID,
        session_id=SESSION_ID,
        turn_id=TURN_ID,
        command_id=COMMAND_ID,
        occurred_at=NOW,
        expected_world_revision=5,
        skill_ref=_skill_ref(),
        run_id="run_harvest_0004",
        build_id="build_harvest_0004",
        failure_count=4,
        failure_key="harvest_loop_short",
        evidence_refs=(evidence,),
        payload=_patch_payload(
            authority,
            feature_enabled=feature_enabled,
            capability_enabled=capability_enabled,
        ),
    )


def _patch_config():
    base = make_role_config(
        "teaching_agent",
        allowed_events=("run_failed", "hint_requested", "skill_patch_requested"),
        allowed_tools=(),
        max_tool_calls=0,
    )
    return replace(
        base,
        limits=RoleLimits(
            max_tool_calls=0,
            max_message_chars=500,
            allow_skill_patch=True,
            require_confirmation_for_patch=True,
        ),
    )


def _failed_run(operation, event: GameEvent, *, authority: DraftAuthority | None = None):
    del authority
    return RunResultSnapshot(
        run_id=event.run_id,
        session_id=event.session_id,
        turn_id=FAILED_TURN_ID,
        command_id=FAILED_COMMAND_ID,
        world_id=WORLD_ID,
        skill_ref=event.skill_ref,
        task_success=False,
        world_revision_before=5,
        world_revision_after=5,
        world_difference={"harvested": 0},
        failed_actions=({"reason": "short_loop"},),
        failure_key=event.failure_key,
        evidence_refs=event.evidence_refs,
        world_commit=None,
        request_context=operation,
        build_id=event.build_id,
    )


class _PatchReads:
    def __init__(self, operation, event: GameEvent, *, authority: DraftAuthority | None = None):
        self.operation = operation
        self.event = event
        self.authority = authority or _authority()
        self.draft = DraftSnapshot(
            authority=self.authority,
            source_code=SOURCE,
            request_context=operation,
        )
        skill = make_skill(operation)
        self.skill = replace(
            skill,
            ref=_skill_ref(),
            source_code=SOURCE,
            source_sha256=SOURCE_SHA,
            entrypoint=self.authority.entrypoint,
            parameter_schema={"type": "object", "additionalProperties": False},
        )
        self.run = _failed_run(operation, event)
        self.interaction = FailedInteractionSnapshot(
            interaction_id="interaction_failed_0001",
            interaction_revision=4,
            interaction_sequence=4,
            same_failure_suffix_end_sequence=4,
            session_id=event.session_id,
            turn_id=self.run.turn_id,
            command_id=self.run.command_id,
            run_id=self.run.run_id,
            build_id=event.build_id,
            task_id=event.task_id,
            world_id=self.run.world_id,
            skill_ref=self.run.skill_ref,
            failure_count=event.failure_count,
            failure_key=event.failure_key,
            evidence_refs=self.run.evidence_refs,
            feedback_event_id="event_failed_feedback_0004",
            projection_receipt_id="receipt_failed_projection_0004",
            request_context=operation,
        )
        self.compile = CompileResultSnapshot(
            build_id=event.build_id,
            skill_ref=event.skill_ref,
            succeeded=True,
            diagnostics=(),
            evidence_refs=(),
            request_context=operation,
            draft_authority=self.authority,
        )

    async def get_task(self, _task_id, _context):
        return replace(
            make_task(self.operation),
            task_id=TASK_ID,
            title="Harvest every ready crop",
            goal="Use one loop to harvest all eight ready plots.",
            story="The farm crops are ready before market opens.",
        )

    async def get_session(self, _session_id, _context):
        return make_session(
            operation=self.operation,
            student_id=STUDENT_ID,
            task_id=TASK_ID,
            session_id=SESSION_ID,
            world_id=WORLD_ID,
        )

    async def get_bound_skill(self, _skill_ref, _context):
        return self.skill

    async def get_current_draft(self, _session_id, _draft_id, _context):
        return self.draft

    async def get_current_failed_interaction(self, _session_id, _interaction_id, _context):
        return self.interaction

    async def get_compile_result(self, _build_id, _context):
        return self.compile

    async def get_run(self, _run_id, _context):
        return self.run

    async def get_profile(self, _student_id, _knowledge_points, _context):
        return replace(
            make_learner_profile(self.operation),
            student_id=STUDENT_ID,
        )

    async def list_recent(self, _session_id, _limit, _context):
        raise AssertionError("patch context must not over-fetch conversational history")

    def __getattr__(self, name: str):
        if name.startswith(("get_", "list_")):
            raise AssertionError(f"unexpected patch context read: {name}")
        raise AttributeError(name)


class _NoWorld:
    async def get_snapshot(self, *_args):
        raise AssertionError("patch context must not read World")


class _UnusedInvocations:
    async def invoke(self, *_args):
        raise AssertionError("legacy patch registry test must not invoke a Skill")

    async def recover(self, *_args):
        raise AssertionError("legacy patch registry test must not recover a Skill")


def _builder(reads: _PatchReads) -> ContextBuilder:
    config = StaticRoleConfigs(_patch_config())
    return ContextBuilder(
        tasks=reads,
        sessions=reads,
        skills=reads,
        runs=reads,
        counterexamples=reads,
        learners=reads,
        messages=reads,
        worlds=_NoWorld(),
        drafts=reads,
        interactions=reads,
        role_configs=config,
        teaching_spec_version="teaching-1",
    )


def _patch_output(*, extra_patch_field: bool = False) -> dict[str, object]:
    patch: dict[str, object] = {
        "replacement_content": REPLACEMENT,
        "rationale": RATIONALE,
    }
    if extra_patch_field:
        patch["path"] = "other.cpp"
    return {
        "kind": "decision",
        "decision": {
            "role": "teaching_agent",
            "response_type": "skill_patch",
            "message": "Review this exact replacement before deciding.",
            "question": None,
            "hint_level": 4,
            "learner_inference": None,
            "skill_patch": patch,
            "requires_student_confirmation": True,
        },
        "tool_calls": [],
    }


class SkillPatchAuthorityUnitTests(unittest.TestCase):
    def test_event_is_exact_authenticated_ui_action_and_routes_only_teaching(self) -> None:
        event = _patch_event()
        route = RoleRouter().route(event)
        self.assertEqual(route.role, "teaching_agent")

        tampered = _patch_payload()
        tampered["action_id"] = "automatic_patch"
        with self.assertRaisesRegex(ValueError, "request_ai_patch"):
            replace(event, payload=tampered)

        # Level 4 is derived only for this fresh explicit Patch-request Run;
        # no ordinary hint interaction can claim level 4 on the frozen wire.
        ordinary = _patch_output()["decision"]
        assert isinstance(ordinary, dict)
        ordinary["response_type"] = "hint"
        ordinary["skill_patch"] = None
        ordinary["requires_student_confirmation"] = False
        with self.assertRaisesRegex(Exception, "closed domain contract"):
            parse_model_envelope(_patch_output() | {"decision": ordinary})

    def test_eligibility_defaults_false_and_requires_every_trusted_gate(self) -> None:
        evidence = PedagogyEvidence(
            evidence_id="evidence_patch_fail_0001",
            outcome=PedagogyEvidenceOutcome.FAILED,
            occurred_at=NOW,
        )
        base = PedagogyInput(
            role="teaching_agent",
            event_type="skill_patch_requested",
            failure_count=4,
            hint_requested=False,
            teaching_spec_version="teaching-1",
            task_concepts=("for_loop",),
            max_hint_level=4,
            learner_revision=0,
            learner_competencies=(),
            learner_evidence_ids=(),
            current_validated_evidence=(evidence,),
            event_time=NOW,
        )
        disabled = PedagogyPolicy().decide(base)
        assert disabled is not None
        self.assertFalse(disabled.patch_eligible)

        enabled = PedagogyPolicy().decide(
            replace(
                base,
                explicit_skill_patch_request=True,
                skill_patch_feature_enabled=True,
                skill_patch_capability_enabled=True,
                draft_authority_validated=True,
            )
        )
        assert enabled is not None
        self.assertEqual(enabled.phase, TeachingPhase.RECTIFICATION)
        self.assertEqual(enabled.hint_level, 4)
        self.assertEqual(enabled.allowed_response_types, ("skill_patch",))
        self.assertTrue(enabled.patch_eligible)

        below_threshold = PedagogyPolicy().decide(
            replace(
                base,
                failure_count=3,
                explicit_skill_patch_request=True,
                skill_patch_feature_enabled=True,
                skill_patch_capability_enabled=True,
                draft_authority_validated=True,
            )
        )
        assert below_threshold is not None
        self.assertFalse(below_threshold.patch_eligible)

        for field in (
            "explicit_skill_patch_request",
            "skill_patch_feature_enabled",
            "skill_patch_capability_enabled",
            "draft_authority_validated",
        ):
            with self.subTest(field=field):
                enabled_input = replace(
                    base,
                    explicit_skill_patch_request=True,
                    skill_patch_feature_enabled=True,
                    skill_patch_capability_enabled=True,
                    draft_authority_validated=True,
                )
                gated = PedagogyPolicy().decide(replace(enabled_input, **{field: False}))
                assert gated is not None
                self.assertFalse(gated.patch_eligible)

    def test_model_schema_exposes_only_full_content_and_rationale(self) -> None:
        event = _patch_event()
        operation = _operation()
        reads = _PatchReads(operation, event)
        context = _run_async(_builder(reads).build(event, "teaching_agent", operation))
        schema = build_model_output_schema(
            (),
            max_tool_calls=0,
            role="teaching_agent",
            directive=context.teaching_directive,
            required_evidence_aliases=("evidence_001",),
        )
        encoded = json.dumps(schema, sort_keys=True)
        self.assertIn("replacement_content", encoded)
        self.assertIn("rationale", encoded)
        self.assertNotIn('"path"', encoded)
        self.assertNotIn("draft_sha256", encoded)
        self.assertNotIn("run_id", encoded)
        parse_model_envelope(_patch_output(), patch_authority=context.patch_authority)
        with self.assertRaisesRegex(Exception, "exact declared fields"):
            parse_model_envelope(
                _patch_output(extra_patch_field=True),
                patch_authority=context.patch_authority,
            )

    def test_draft_port_is_read_only(self) -> None:
        members = set(DraftReadPort.__dict__)
        self.assertIn("get_current_draft", members)
        self.assertFalse(members & {"write", "upsert", "save", "create", "apply", "delete"})
        interaction_members = set(InteractionReadPort.__dict__)
        self.assertIn("get_current_failed_interaction", interaction_members)
        self.assertFalse(
            interaction_members & {"write", "upsert", "save", "create", "apply", "delete"}
        )

    def test_default_registry_has_no_legacy_patch_tool(self) -> None:
        operation = _operation()
        event = _patch_event()
        reads = _PatchReads(operation, event)
        context = _run_async(_builder(reads).build(event, "teaching_agent", operation))
        registry = build_default_tool_registry(
            TraceSink(),
            _UnusedInvocations(),
        )
        with self.assertRaisesRegex(AgentConfigurationError, "unregistered tool"):
            registry.model_definitions(
                "teaching_agent",
                ("propose_skill_patch",),
                context,
            )

    def test_entrypoint_uses_the_frozen_canonical_source_path(self) -> None:
        for path in (".hidden", "-main.cpp", "dir/file."):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "canonical relative source path"):
                    _authority(entrypoint=path)

    def test_selected_interaction_must_be_latest_same_failure_suffix(self) -> None:
        operation = _operation()
        reads = _PatchReads(operation, _patch_event())
        with self.assertRaisesRegex(ValueError, "latest current same-failure"):
            replace(reads.interaction, same_failure_suffix_end_sequence=5)


class SkillPatchContextAndRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ineligible_patch_request_stops_before_provider_dispatch(self) -> None:
        operation = _operation()
        event = _patch_event(feature_enabled=False)
        # Context construction owns every eligibility gate and completes
        # before SharedAgentRuntime can dispatch an LLM request.
        with self.assertRaisesRegex(AgentContextError, "requires exact"):
            await _builder(_PatchReads(operation, event)).build(event, "teaching_agent", operation)

    async def test_request_turn_is_distinct_from_selected_failed_run(self) -> None:
        operation = _operation()
        event = _patch_event()
        reads = _PatchReads(operation, event)

        context = await _builder(reads).build(event, "teaching_agent", operation)

        assert context.patch_authority is not None
        self.assertEqual(context.event.turn_id, TURN_ID)
        self.assertEqual(context.event.command_id, COMMAND_ID)
        self.assertEqual(context.patch_authority.failed.turn_id, FAILED_TURN_ID)
        self.assertEqual(context.patch_authority.failed.command_id, FAILED_COMMAND_ID)
        self.assertEqual(context.patch_authority.request.turn_id, TURN_ID)
        self.assertEqual(context.patch_authority.request.command_id, COMMAND_ID)
        self.assertEqual(
            context.patch_authority.request.requested_interaction_id,
            context.patch_authority.failed.interaction_id,
        )

    async def test_context_closes_exact_failed_run_build_evidence_and_current_draft(self) -> None:
        operation = _operation()
        event = _patch_event()
        reads = _PatchReads(operation, event)
        context = await _builder(reads).build(event, "teaching_agent", operation)

        self.assertEqual(context.draft, reads.draft)
        self.assertEqual(context.compile_result, reads.compile)
        self.assertEqual(context.run_result, reads.run)
        assert context.teaching_directive is not None
        self.assertTrue(context.teaching_directive.patch_eligible)
        self.assertEqual(context.hint_level, 4)

    async def test_patch_requires_an_authenticated_student_actor(self) -> None:
        base = _operation()
        operation = replace(
            base,
            actor=replace(base.actor, actor_type=ActorType.TEACHER),
        )
        event = _patch_event()
        reads = _PatchReads(operation, event)
        with self.assertRaisesRegex(AgentContextError, "authenticated student"):
            await _builder(reads).build(event, "teaching_agent", operation)

    async def test_every_drift_fails_closed_before_provider(self) -> None:
        operation = _operation()
        event = _patch_event()
        mutations = {
            "draft_revision": lambda reads: setattr(
                reads,
                "draft",
                replace(reads.draft, authority=replace(reads.authority, draft_revision=4)),
            ),
            "draft_hash": lambda reads: setattr(
                reads,
                "draft",
                replace(reads.draft, authority=replace(reads.authority, draft_sha256="f" * 64)),
            ),
            "entrypoint": lambda reads: setattr(
                reads,
                "draft",
                replace(reads.draft, authority=replace(reads.authority, entrypoint="other.py")),
            ),
            "build": lambda reads: setattr(
                reads, "run", replace(reads.run, build_id="build_other_0001")
            ),
            "evidence": lambda reads: setattr(
                reads,
                "run",
                replace(reads.run, evidence_refs=(make_evidence("evidence_other_0001"),)),
            ),
            "selected_interaction": lambda reads: setattr(
                reads,
                "interaction",
                replace(reads.interaction, interaction_id="interaction_other_0001"),
            ),
            "selected_turn": lambda reads: setattr(
                reads,
                "interaction",
                replace(reads.interaction, turn_id="turn_other_failed_0004"),
            ),
            "selected_failure_count": lambda reads: setattr(
                reads,
                "interaction",
                replace(reads.interaction, failure_count=3),
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                reads = _PatchReads(operation, event)
                mutate(reads)
                with self.assertRaises(AgentContextError):
                    await _builder(reads).build(event, "teaching_agent", operation)

    async def test_runtime_injects_one_stable_upsert_and_never_trusts_model_authority(self) -> None:
        operation = _operation()
        event = _patch_event()
        context = await _builder(_PatchReads(operation, event)).build(
            event, "teaching_agent", operation
        )
        trace = TraceSink()
        llm = SequenceLlm([_provider_reply(_patch_output())])
        config = replace(
            _patch_config(),
            allowed_tools=("unregistered_tool",),
            limits=RoleLimits(
                max_tool_calls=2,
                max_message_chars=500,
                allow_skill_patch=True,
                require_confirmation_for_patch=True,
            ),
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

        decision = await runtime.run("teaching_agent", context, operation)
        proposal = decision.draft.skill_patch
        assert proposal is not None
        self.assertEqual(decision.source, "provider")
        self.assertFalse(decision.degraded)
        self.assertEqual(proposal.operation.operation_type, "UPSERT_FILE")
        self.assertEqual(proposal.operation.path, context.draft.authority.entrypoint)
        self.assertEqual(proposal.operation.content, REPLACEMENT)
        self.assertEqual(proposal.operation.content_sha256, REPLACEMENT_SHA)
        self.assertEqual(proposal.target, context.draft.authority)
        self.assertEqual(proposal.failed.build_id, event.build_id)
        self.assertEqual(proposal.failed.run_id, event.run_id)
        self.assertEqual(proposal.failed.evidence_refs, event.evidence_refs)
        self.assertEqual(proposal.failed.tenant_id, operation.actor.tenant_id)
        self.assertEqual(proposal.failed.actor_id, operation.actor.actor_id)
        self.assertEqual(proposal.failed.interaction_id, "interaction_failed_0001")
        self.assertEqual(proposal.failed.turn_id, FAILED_TURN_ID)
        self.assertEqual(proposal.failed.command_id, FAILED_COMMAND_ID)
        self.assertEqual(proposal.request.turn_id, event.turn_id)
        self.assertEqual(proposal.request.command_id, event.command_id)
        self.assertEqual(
            proposal.request.requested_interaction_id,
            proposal.failed.interaction_id,
        )
        self.assertTrue(proposal.proposal_id.startswith("patch_"))
        self.assertEqual(len(proposal.proposal_sha256), 64)
        self.assertEqual(decode_as(encode(proposal), SkillPatchProposal), proposal)
        self.assertEqual(decision.tool_calls, ())
        self.assertEqual(len(llm.requests), 1)

        identities = {
            field: SkillPatchProposal.create(
                replace(
                    context.patch_authority,
                    failed=replace(context.patch_authority.failed, **{field: value}),
                ),
                replacement_content=REPLACEMENT,
                rationale=RATIONALE,
            ).proposal_id
            for field, value in {
                "turn_id": "turn_harvest_other",
                "command_id": "cmd_harvest_other",
            }.items()
        }
        self.assertEqual(len(set(identities.values())), 2)
        self.assertNotIn(proposal.proposal_id, identities.values())

        request_identities = {
            field: SkillPatchProposal.create(
                replace(
                    context.patch_authority,
                    request=replace(context.patch_authority.request, **{field: value}),
                ),
                replacement_content=REPLACEMENT,
                rationale=RATIONALE,
            ).proposal_id
            for field, value in {
                "turn_id": "turn_harvest_request_other",
                "command_id": "cmd_harvest_request_other",
            }.items()
        }
        self.assertEqual(len(set(request_identities.values())), 2)
        self.assertNotIn(proposal.proposal_id, request_identities.values())

        global_scope_identities = {
            field: SkillPatchProposal.create(
                replace(
                    context.patch_authority,
                    request=replace(context.patch_authority.request, **{field: value}),
                    failed=replace(context.patch_authority.failed, **{field: value}),
                ),
                replacement_content=REPLACEMENT,
                rationale=RATIONALE,
            ).proposal_id
            for field, value in {
                "tenant_id": "tenant_other",
                "actor_id": "student_other",
            }.items()
        }
        self.assertEqual(len(set(global_scope_identities.values())), 2)
        self.assertNotIn(proposal.proposal_id, global_scope_identities.values())

    async def test_provider_failure_fallback_contains_no_patch(self) -> None:
        operation = _operation()
        event = _patch_event()
        context = await _builder(_PatchReads(operation, event)).build(
            event, "teaching_agent", operation
        )
        trace = TraceSink()
        runtime = SharedAgentRuntime(
            llm=_RaisingLlm(ConnectionError("provider unavailable")),
            role_configs=StaticRoleConfigs(_patch_config()),
            tools=ToolRegistry(trace),
            prompts=PromptBuilder(),
            trace=trace,
            versions=make_versions(),
            clock=lambda: NOW,
        )

        with self.assertRaises(AgentDependencyError):
            await runtime.run("teaching_agent", context, operation)

    async def test_degraded_provider_reply_is_never_published_as_a_patch(self) -> None:
        operation = _operation()
        event = _patch_event()
        context = await _builder(_PatchReads(operation, event)).build(
            event, "teaching_agent", operation
        )
        reply = _provider_reply(_patch_output())
        degraded = replace(
            reply,
            value=replace(
                reply.value,
                source="provider_fallback",
                degraded=True,
                fallback_reason="PROVIDER_UNAVAILABLE",
            ),
        )
        trace = TraceSink()
        runtime = SharedAgentRuntime(
            llm=SequenceLlm([degraded]),
            role_configs=StaticRoleConfigs(_patch_config()),
            tools=ToolRegistry(trace),
            prompts=PromptBuilder(),
            trace=trace,
            versions=make_versions(),
            clock=lambda: NOW,
        )

        with self.assertRaises(AgentDependencyError):
            await runtime.run("teaching_agent", context, operation)
        self.assertFalse(
            any(
                item.name == "agent.turn.finished" and item.fields.get("validated") is True
                for item in trace.events
            )
        )


def _provider_reply(output: dict[str, object]):
    from agent_runtime_fixtures import make_reply

    return make_reply(output)


class _RaisingLlm:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def generate(self, _request, _context):
        raise self.error


def _run_async(awaitable):
    import asyncio

    return asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()
