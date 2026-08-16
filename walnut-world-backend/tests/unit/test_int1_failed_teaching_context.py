from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    EvidenceRef,
    EvidenceType,
    HarvestIntent,
    OperationContext,
    RequestContext,
    SkillRef,
    canonical_json_sha256,
)
from yaya_agent_runtime import (
    LEARNER_PROJECTION_POLICY_VERSION,
    REVIEW_POLICY_VERSION,
    CompileResultSnapshot,
    ContextBuilder,
    CounterexampleSnapshot,
    GameEvent,
    LearnerProfileSnapshot,
    LearnerProjectionPolicy,
    PackagedRoleConfigProvider,
    ProjectionEvidence,
    ProjectionInput,
    ProjectionOutcome,
    PromptBuilder,
    RunResultSnapshot,
    SessionSnapshot,
    SkillSnapshot,
    SkillVersionSummary,
    TaskRelation,
    TaskSnapshot,
    derive_run_outcome_event,
    side_effect_execution_id,
)

from walnut_backend.adapters.postgres.models import json_value
from walnut_backend.bootstrap import Settings
from walnut_backend.domain.world.engine import WorldEngine
from walnut_backend.domain.world.rules import WorldRules
from walnut_backend.int1_e2e_authority import (
    ACTOR_ID,
    CONTENT_UNIT_ID,
    CONTENT_VERSION,
    PINNED_GCC_IMAGE,
    SEED_TIMESTAMP,
    SKILL_ID,
    TASK_ID,
    TENANT_ID,
    WORLD_ID,
    Int1AuthoritySeedConfig,
    build_int1_e2e_fixture,
)
from walnut_backend.workers.turn_projection import _canonical_teaching_directive

BACKEND_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = BACKEND_ROOT.parent / "agent"
JWT_SECRET = "int1-test-only-hs256-secret-value"
TEACHING_SPEC_VERSION = "agent-teaching-v1"


class _Int1FirstFailureReads:
    """Adapter-shaped immutable reads for the first formal INT1 failed Run."""

    def __init__(
        self,
        *,
        task: TaskSnapshot,
        session: SessionSnapshot,
        skill: SkillSnapshot,
        run: RunResultSnapshot,
        profile: LearnerProfileSnapshot,
    ) -> None:
        self.task = task
        self.session = session
        self.skill = skill
        self.run = run
        self.profile = profile
        self.calls: list[str] = []

    async def get_task(self, task_id: str, context: OperationContext) -> TaskSnapshot:
        del context
        assert task_id == self.task.task_id
        self.calls.append("task")
        return self.task

    async def get_session(self, session_id: str, context: OperationContext) -> SessionSnapshot:
        del context
        assert session_id == self.session.session_id
        self.calls.append("session")
        return self.session

    async def get_bound_skill(
        self, skill_ref: SkillRef, context: OperationContext
    ) -> SkillSnapshot:
        del context
        assert skill_ref == self.skill.ref
        self.calls.append("skill")
        return self.skill

    async def get_run(self, run_id: str, context: OperationContext) -> RunResultSnapshot:
        del context
        assert run_id == self.run.run_id
        self.calls.append("run")
        return self.run

    async def get_profile(
        self,
        student_id: str,
        knowledge_points: tuple[str, ...],
        context: OperationContext,
    ) -> LearnerProfileSnapshot:
        del context
        assert student_id == self.profile.student_id
        assert knowledge_points == self.task.knowledge_points
        self.calls.append("profile")
        return self.profile

    async def list_recent(
        self, session_id: str, limit: int, context: OperationContext
    ) -> tuple[()]:
        del context
        assert session_id == self.session.session_id
        assert limit == 8
        self.calls.append("recent")
        return ()

    async def list_active_skills(
        self, student_id: str, context: OperationContext
    ) -> tuple[SkillSnapshot, ...]:
        del student_id, context
        raise AssertionError("teaching context must not fetch active skills")

    async def list_skill_history(
        self, skill_id: str, session_id: str, context: OperationContext
    ) -> tuple[SkillVersionSummary, ...]:
        del skill_id, session_id, context
        raise AssertionError("teaching context must not fetch skill history")

    async def get_compile_result(
        self, build_id: str, context: OperationContext
    ) -> CompileResultSnapshot:
        del build_id, context
        raise AssertionError("run_failed context must not fetch a compile result")

    async def list_same_failure_runs(
        self,
        session_id: str,
        failure_key: str,
        through_run_id: str,
        limit: int,
        context: OperationContext,
    ) -> tuple[RunResultSnapshot, ...]:
        del session_id, failure_key, through_run_id, limit, context
        raise AssertionError("first-failure teaching context must not fetch failure history")

    async def list_session_runs(
        self,
        session_id: str,
        through_run_id: str,
        context: OperationContext,
    ) -> tuple[RunResultSnapshot, ...]:
        del session_id, through_run_id, context
        raise AssertionError("teaching context must not fetch session history")

    async def list_counterexamples(
        self, task_id: str, failure_key: str, context: OperationContext
    ) -> tuple[CounterexampleSnapshot, ...]:
        del task_id, failure_key, context
        raise AssertionError("first-failure teaching context must not fetch counterexamples")

    async def get_snapshot(self, world_id: str, context: OperationContext) -> Any:
        del world_id, context
        raise AssertionError("teaching context must not fetch the World")


def test_first_int1_failed_run_builds_the_canonical_teaching_context(
    tmp_path: Path,
) -> None:
    fixture = build_int1_e2e_fixture(_config(tmp_path))
    task_wire = cast(dict[str, Any], fixture.content_json["task"])
    actor = ActorRef(TENANT_ID, ACTOR_ID, ActorType.STUDENT, ("game:player",))
    content_ref = ContentRef(CONTENT_UNIT_ID, CONTENT_VERSION, fixture.content_hash)
    requested_at = SEED_TIMESTAMP + timedelta(minutes=10)
    command_id = "cmd_int1_failure_0001"
    turn_id = "turn_int1_failure_0001"
    session_id = "session_int1_failure_0001"
    operation = OperationContext(
        request_id="req_int1_failure_0001",
        correlation_id="corr_int1_failure_0001",
        trace_id="trace_int1_failure_0001",
        requested_at=requested_at,
        actor=actor,
        content_ref=content_ref,
        command_id=command_id,
        causation_id=None,
        deadline_at=requested_at + timedelta(minutes=3),
    )
    operation_provenance = _request_context(
        "failure", requested_at, actor=actor, content_ref=content_ref
    )
    task = TaskSnapshot(
        task_id=TASK_ID,
        title=cast(str, task_wire["name"]),
        goal=cast(str, task_wire["goal"]),
        story=cast(str, cast(dict[str, Any], task_wire["story"])["opening"]),
        knowledge_points=tuple(cast(list[str], task_wire["knowledge_points"])),
        request_context=operation_provenance,
        max_hint_level=cast(int, cast(dict[str, Any], task_wire["hint_policy"])["max_level"]),
    )
    session = SessionSnapshot(
        session_id=session_id,
        student_id=ACTOR_ID,
        task_id=TASK_ID,
        world_id=WORLD_ID,
        request_context=_request_context(
            "session", SEED_TIMESTAMP + timedelta(minutes=1), actor=actor, content_ref=content_ref
        ),
    )
    starter = cast(dict[str, Any], task_wire["starter_skill"])
    source_bundle = cast(dict[str, Any], starter["source_bundle"])
    starter_file = cast(dict[str, Any], cast(list[Any], source_bundle["files"])[0])
    failure_source = _failure_source(cast(str, starter_file["content"]))
    skill_ref = SkillRef(
        skill_id=SKILL_ID,
        skill_version_id="skill_version_int1_failure_0001",
        artifact_sha256="a" * 64,
        certification_id="certification_int1_failure_0001",
    )
    skill = SkillSnapshot(
        ref=skill_ref,
        source_code=failure_source,
        source_sha256=hashlib.sha256(failure_source.encode("utf-8")).hexdigest(),
        entrypoint=cast(str, source_bundle["entrypoint"]),
        parameter_schema=cast(dict[str, Any], fixture.build_policy_json["parameter_schema"]),
        request_context=_request_context(
            "build", SEED_TIMESTAMP + timedelta(minutes=5), actor=actor, content_ref=content_ref
        ),
    )
    invocation_id = side_effect_execution_id(command_id, turn_id)
    run_id = f"run_{hashlib.sha256(invocation_id.encode('utf-8')).hexdigest()[:24]}"
    intents = tuple(
        HarvestIntent(
            intent_id=f"intent_harvest_{index:04d}",
            actor_entity_id="avatar_0001",
            expected_world_revision=0,
            plot_id=f"plot_{index:04d}",
        )
        for index in range(1, 8)
    )
    transition = WorldEngine().apply(
        cast(dict[str, Any], fixture.world_snapshot_json["state"]),
        intents,
        WorldRules(CONTENT_VERSION, 8, 0, 31, 0, 31, 2, 8),
    )
    assert transition.score == 7
    assert not transition.success
    failure_key = None if transition.success else "task_incomplete"
    world_status = "COMMITTED" if transition.success else "REJECTED"
    evidence_at = requested_at + timedelta(seconds=2)
    evidence = EvidenceRef(
        evidence_id=(
            "evidence_run_" + hashlib.sha256(invocation_id.encode("utf-8")).hexdigest()[:24]
        ),
        evidence_type=EvidenceType.SANDBOX_LOG,
        created_at=evidence_at,
        sha256=canonical_json_sha256(
            {
                "evidence_kind": "SKILL_RUN",
                "run_id": run_id,
                "sandbox_status": "SUCCEEDED",
                "world_status": world_status,
                "intent_count": len(transition.applied_intent_ids),
            }
        ),
    )
    run = RunResultSnapshot(
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        world_id=WORLD_ID,
        skill_ref=skill_ref,
        task_success=transition.success,
        world_revision_before=0,
        world_revision_after=0,
        world_difference={
            "score": transition.score,
            "intent_count": len(transition.applied_intent_ids),
            "applied_intent_ids": transition.applied_intent_ids,
        },
        failed_actions=(() if transition.success else ({"reason": failure_key},)),
        failure_key=failure_key,
        evidence_refs=(evidence,),
        world_commit=None,
        request_context=operation_provenance,
    )
    root_event = GameEvent(
        event_id="event_int1_failure_root_0001",
        event_type="run_skill_requested",
        student_id=ACTOR_ID,
        task_id=TASK_ID,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        occurred_at=requested_at,
        expected_world_revision=0,
        skill_ref=skill_ref,
        payload={"input": {"type": "MESSAGE", "text": "Run the objective."}},
    )
    outcome = derive_run_outcome_event(
        root_event=root_event,
        run=run,
        task=task,
        failure_count=1,
        occurred_at=max(requested_at + timedelta(seconds=3), evidence.created_at),
    )
    profile = LearnerProfileSnapshot(
        student_id=ACTOR_ID,
        revision=cast(int, fixture.learner_profile_json["revision"]),
        competencies=cast(dict[str, Any], fixture.learner_profile_json["competencies"]),
        request_context=operation_provenance,
        evidence_refs=(),
    )
    reads = _Int1FirstFailureReads(
        task=task,
        session=session,
        skill=skill,
        run=run,
        profile=profile,
    )
    reads_port = cast(Any, reads)
    roles = PackagedRoleConfigProvider.load()
    context = asyncio.run(
        ContextBuilder(
            tasks=reads_port,
            sessions=reads_port,
            skills=reads_port,
            runs=reads_port,
            counterexamples=reads_port,
            learners=reads_port,
            messages=reads_port,
            worlds=reads_port,
            role_configs=roles,
            teaching_spec_version=TEACHING_SPEC_VERSION,
        ).build(outcome, "teaching_agent", operation)
    )

    assert reads.calls == ["task", "session", "skill", "run", "profile", "recent"]
    assert context.run_result == run
    assert context.teaching_directive is not None
    assert json_value(context.teaching_directive) == _canonical_teaching_directive(
        outcome=outcome,
        role="teaching_agent",
        task=task_wire,
        profile=fixture.learner_profile_json,
        teaching_spec_version=TEACHING_SPEC_VERSION,
    )

    messages = PromptBuilder().initial_messages(roles.get("teaching_agent"), context, ())
    assert len(messages) == 2
    prompt = json.loads(messages[1].content)
    prompt_context = cast(dict[str, Any], prompt["turn_context"])
    assert prompt_context["evidence_catalog"] == [{"ref": "evidence_001", "type": "SANDBOX_LOG"}]
    assert cast(dict[str, Any], prompt_context["event"])["evidence_refs"] == ["evidence_001"]
    assert cast(dict[str, Any], prompt_context["run_result"])["evidence_refs"] == ["evidence_001"]
    assert cast(dict[str, Any], prompt_context["teaching_directive"])["required_evidence_refs"] == [
        "evidence_001"
    ]


def test_second_same_failure_context_uses_revision_one_source_run_evidence(
    tmp_path: Path,
) -> None:
    fixture = build_int1_e2e_fixture(_config(tmp_path))
    task_wire = cast(dict[str, Any], fixture.content_json["task"])
    actor = ActorRef(TENANT_ID, ACTOR_ID, ActorType.STUDENT, ("game:player",))
    content_ref = ContentRef(CONTENT_UNIT_ID, CONTENT_VERSION, fixture.content_hash)
    first_failure_at = SEED_TIMESTAMP + timedelta(minutes=10)
    second_failure_at = SEED_TIMESTAMP + timedelta(minutes=20)
    command_id = "cmd_int1_failure_0002"
    turn_id = "turn_int1_failure_0002"
    session_id = "session_int1_failure_0001"
    operation = OperationContext(
        request_id="req_int1_failure_0002",
        correlation_id="corr_int1_failure_0002",
        trace_id="trace_int1_failure_0002",
        requested_at=second_failure_at,
        actor=actor,
        content_ref=content_ref,
        command_id=command_id,
        causation_id=None,
        deadline_at=second_failure_at + timedelta(minutes=3),
    )
    task = TaskSnapshot(
        task_id=TASK_ID,
        title=cast(str, task_wire["name"]),
        goal=cast(str, task_wire["goal"]),
        story=cast(str, cast(dict[str, Any], task_wire["story"])["opening"]),
        knowledge_points=tuple(cast(list[str], task_wire["knowledge_points"])),
        request_context=_request_context(
            "failure_second_task",
            second_failure_at,
            actor=actor,
            content_ref=content_ref,
        ),
        max_hint_level=cast(int, cast(dict[str, Any], task_wire["hint_policy"])["max_level"]),
    )
    session = SessionSnapshot(
        session_id=session_id,
        student_id=ACTOR_ID,
        task_id=TASK_ID,
        world_id=WORLD_ID,
        request_context=_request_context(
            "session", SEED_TIMESTAMP + timedelta(minutes=1), actor=actor, content_ref=content_ref
        ),
    )
    starter = cast(dict[str, Any], task_wire["starter_skill"])
    source_bundle = cast(dict[str, Any], starter["source_bundle"])
    starter_file = cast(dict[str, Any], cast(list[Any], source_bundle["files"])[0])
    failure_source = _failure_source(cast(str, starter_file["content"]))
    skill_ref = SkillRef(
        skill_id=SKILL_ID,
        skill_version_id="skill_version_int1_failure_0001",
        artifact_sha256="a" * 64,
        certification_id="certification_int1_failure_0001",
    )
    skill = SkillSnapshot(
        ref=skill_ref,
        source_code=failure_source,
        source_sha256=hashlib.sha256(failure_source.encode("utf-8")).hexdigest(),
        entrypoint=cast(str, source_bundle["entrypoint"]),
        parameter_schema=cast(dict[str, Any], fixture.build_policy_json["parameter_schema"]),
        request_context=_request_context(
            "build", SEED_TIMESTAMP + timedelta(minutes=5), actor=actor, content_ref=content_ref
        ),
    )

    first_source_evidence = EvidenceRef(
        evidence_id="evidence_run_int1_failure_0001",
        evidence_type=EvidenceType.SANDBOX_LOG,
        created_at=first_failure_at + timedelta(seconds=2),
        sha256=canonical_json_sha256(
            {"run_id": "run_int1_failure_0001", "failure_key": "task_incomplete"}
        ),
    )
    concept = task.knowledge_points[0]
    first_projection = LearnerProjectionPolicy().project(
        ProjectionInput(
            learner_revision=0,
            learner_projection_policy_version=LEARNER_PROJECTION_POLICY_VERSION,
            review_policy_version=REVIEW_POLICY_VERSION,
            evidence=ProjectionEvidence(
                evidence_ids=(first_source_evidence.evidence_id,),
                concept=concept,
                outcome=ProjectionOutcome.PARTIAL,
                task_relation=TaskRelation.STANDARD,
                assistance_level=0,
                occurred_at=first_failure_at + timedelta(seconds=3),
                source_sequence=1,
            ),
        )
    )
    competency = first_projection.competency
    profile = LearnerProfileSnapshot(
        student_id=ACTOR_ID,
        revision=1,
        competencies={
            concept: {
                "concept": competency.concept,
                "evidence_stage": competency.evidence_stage.value,
                "assistance_level": competency.assistance_level,
                "last_observed_at": competency.last_observed_at.isoformat(),
                "next_review_at": competency.next_review_at.isoformat(),
                "evidence_ids": list(competency.evidence_ids),
            }
        },
        request_context=_request_context(
            "learner_after_first_failure",
            first_failure_at + timedelta(seconds=4),
            actor=actor,
            content_ref=content_ref,
        ),
        evidence_refs=(first_source_evidence,),
    )

    second_invocation_id = side_effect_execution_id(command_id, turn_id)
    second_run_id = "run_" + hashlib.sha256(second_invocation_id.encode("utf-8")).hexdigest()[:24]
    second_evidence = EvidenceRef(
        evidence_id=(
            "evidence_run_" + hashlib.sha256(second_invocation_id.encode("utf-8")).hexdigest()[:24]
        ),
        evidence_type=EvidenceType.SANDBOX_LOG,
        created_at=second_failure_at + timedelta(seconds=2),
        sha256=canonical_json_sha256({"run_id": second_run_id, "failure_key": "task_incomplete"}),
    )
    run_provenance = _request_context(
        "failure_second_run",
        second_failure_at,
        actor=actor,
        content_ref=content_ref,
    )
    second_run = RunResultSnapshot(
        run_id=second_run_id,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        world_id=WORLD_ID,
        skill_ref=skill_ref,
        task_success=False,
        world_revision_before=0,
        world_revision_after=0,
        world_difference={"score": 7, "intent_count": 7},
        failed_actions=({"reason": "task_incomplete"},),
        failure_key="task_incomplete",
        evidence_refs=(second_evidence,),
        world_commit=None,
        request_context=run_provenance,
    )
    root_event = GameEvent(
        event_id="event_int1_failure_root_0002",
        event_type="run_skill_requested",
        student_id=ACTOR_ID,
        task_id=TASK_ID,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        occurred_at=second_failure_at,
        expected_world_revision=0,
        skill_ref=skill_ref,
        payload={"input": {"type": "MESSAGE", "text": "Try the same objective again."}},
    )
    outcome = derive_run_outcome_event(
        root_event=root_event,
        run=second_run,
        task=task,
        failure_count=2,
        occurred_at=second_failure_at + timedelta(seconds=3),
    )
    reads = _Int1FirstFailureReads(
        task=task,
        session=session,
        skill=skill,
        run=second_run,
        profile=profile,
    )
    reads_port = cast(Any, reads)
    roles = PackagedRoleConfigProvider.load()
    context = asyncio.run(
        ContextBuilder(
            tasks=reads_port,
            sessions=reads_port,
            skills=reads_port,
            runs=reads_port,
            counterexamples=reads_port,
            learners=reads_port,
            messages=reads_port,
            worlds=reads_port,
            role_configs=roles,
            teaching_spec_version=TEACHING_SPEC_VERSION,
        ).build(outcome, "teaching_agent", operation)
    )

    assert reads.calls == ["task", "session", "skill", "run", "profile", "recent"]
    assert context.learner_profile == profile
    assert context.teaching_directive is not None
    assert context.teaching_directive.learner_revision == 1
    assert context.teaching_directive.required_evidence_ids == (second_evidence.evidence_id,)
    assert competency.evidence_ids == (first_source_evidence.evidence_id,)
    assert tuple(item.evidence_id for item in profile.evidence_refs) == (
        first_source_evidence.evidence_id,
    )

    messages = PromptBuilder().initial_messages(roles.get("teaching_agent"), context, ())
    prompt = json.loads(messages[1].content)
    prompt_context = cast(dict[str, Any], prompt["turn_context"])
    assert prompt_context["evidence_catalog"] == [
        {"ref": "evidence_001", "type": "SANDBOX_LOG"},
        {"ref": "evidence_002", "type": "SANDBOX_LOG"},
    ]
    assert cast(dict[str, Any], prompt_context["teaching_directive"])["required_evidence_refs"] == [
        "evidence_001"
    ]


def _request_context(
    scope: str,
    requested_at: Any,
    *,
    actor: ActorRef,
    content_ref: ContentRef,
) -> RequestContext:
    return RequestContext(
        request_id=f"req_int1_{scope}_0001",
        correlation_id=f"corr_int1_{scope}_0001",
        trace_id=f"trace_int1_{scope}_0001",
        requested_at=requested_at,
        actor=actor,
        content_ref=content_ref,
    )


def _failure_source(source: str) -> str:
    include_anchor = "#include <iostream>"
    loop_anchor = "    for (int index = 1; index <= length; ++index) {"
    assert source.count(include_anchor) == 1
    assert source.count(loop_anchor) == 1
    mutated = source.replace(include_anchor, f"#include <cstdlib>\n{include_anchor}", 1)
    mutated = mutated.replace(
        loop_anchor,
        (
            '    if (std::getenv("YAYA_DETERMINISTIC_SEED") != nullptr && length > 0) {\n'
            "        --length;\n"
            "    }\n"
            f"{loop_anchor}"
        ),
        1,
    )
    return f"{mutated.rstrip()}\n// INT1_REAL_GATEWAY_FAILURE_DRAFT_V1\n"


def _config(tmp_path: Path) -> Int1AuthoritySeedConfig:
    settings = replace(
        Settings.for_test(contract_path=AGENT_ROOT),
        development_auth_enabled=False,
        auth_hmac_secret=JWT_SECRET,
        auth_issuer="walnut-int1-test",
        auth_audience="walnut-int1-client",
    )
    return Int1AuthoritySeedConfig(
        settings=settings,
        artifact_root=tmp_path / "artifacts",
        sandbox_image=PINNED_GCC_IMAGE,
        provider_identifier="deepseek",
        model_identifier="deepseek-v4-flash",
        prompt_version="int1-prompt-v1",
        teaching_spec_version=TEACHING_SPEC_VERSION,
        world_rules_version="farm-rules-1",
        world_success_score=8,
    )
