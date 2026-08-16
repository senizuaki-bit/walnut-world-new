from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    EvidenceRef,
    EvidenceType,
    LlmReply,
    OperationContext,
    SkillRef,
    Success,
    VersionSet,
    WorldCommitReceipt,
    WorldSnapshot,
)
from yaya_agent_runtime import (
    BUG_FAILURE_THRESHOLD,
    PEDAGOGY_POLICY_VERSION,
    AgentDecision,
    AgentPersistenceError,
    AgentToolExecutionError,
    AgentTraceEvent,
    AgentTurnClaimReceipt,
    AgentTurnCommitReceipt,
    CommittedAgentTurn,
    DecisionDraft,
    GameEvent,
    LearnerProfileSnapshot,
    RoleConfig,
    RoleLimits,
    RoleRoute,
    RunResultSnapshot,
    SessionSnapshot,
    SkillInvocationRequest,
    SkillInvocationResult,
    SkillSnapshot,
    TaskSnapshot,
    TeachingDirective,
    TeachingPhase,
    TurnContext,
    summarize_world,
    world_commit_receipt_sha256,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
STUDENT_ID = "student_0001"
TASK_ID = "task_watering_0001"
SESSION_ID = "session_watering_0001"
TURN_ID = "turn_watering_0001"
COMMAND_ID = "cmd_watering_0001"
WORLD_ID = "world_watering_0001"


def make_evidence(
    evidence_id: str = "evidence_runtime_0001",
    evidence_type: EvidenceType = EvidenceType.ACTION_LOG,
) -> EvidenceRef:
    return EvidenceRef(evidence_id, evidence_type, NOW, sha256="e" * 64)


def make_skill_ref() -> SkillRef:
    return SkillRef(
        skill_id="skill_watering_0001",
        skill_version_id="skill_version_0001",
        artifact_sha256="b" * 64,
        certification_id="certification_0001",
    )


def make_skill(operation: OperationContext | None = None) -> SkillSnapshot:
    source_code = (
        "def water_plots(length):\n    for index in range(length):\n        water(index)\n"
    )
    return SkillSnapshot(
        ref=make_skill_ref(),
        source_code=source_code,
        source_sha256=hashlib.sha256(source_code.encode("utf-8")).hexdigest(),
        entrypoint="main.py",
        parameter_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["length"],
            "properties": {
                "length": {"type": "integer", "minimum": 1, "maximum": 8},
            },
        },
        request_context=operation or make_operation(),
    )


def make_operation(
    *,
    actor_id: str = STUDENT_ID,
    command_id: str = COMMAND_ID,
) -> OperationContext:
    return OperationContext(
        request_id="req_runtime_0001",
        correlation_id="corr_runtime_0001",
        trace_id="trace_runtime_0001",
        requested_at=NOW,
        actor=ActorRef(
            tenant_id="tenant_yaya",
            actor_id=actor_id,
            actor_type=ActorType.STUDENT,
            roles=("game:player",),
        ),
        content_ref=ContentRef("YAYA_FARM_001", "1.0.0", "a" * 64),
        command_id=command_id,
        causation_id=None,
        deadline_at=NOW + timedelta(seconds=30),
    )


def make_event(
    event_type: str,
    *,
    failure_count: int | None = None,
    student_id: str = STUDENT_ID,
    task_id: str = TASK_ID,
    session_id: str = SESSION_ID,
    turn_id: str = TURN_ID,
    command_id: str = COMMAND_ID,
    expected_world_revision: int = 5,
) -> GameEvent:
    skill_ref = None
    run_id = None
    build_id = None
    failure_key = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    count = 0 if failure_count is None else failure_count

    if event_type in {
        "compile_failed",
        "run_skill_requested",
        "run_failed",
        "task_completed",
        "hint_requested",
    }:
        skill_ref = make_skill_ref()
    if event_type in {"run_succeeded", "run_failed", "task_completed"}:
        run_id = "run_watering_0001"
    if event_type in {"compile_succeeded", "compile_failed"}:
        build_id = "build_watering_0001"
    if event_type == "run_failed":
        count = 1 if failure_count is None else failure_count
        failure_key = "watering_loop_short"
    if event_type == "hint_requested" and count > 0:
        failure_key = "watering_loop_short"
        if count >= BUG_FAILURE_THRESHOLD:
            run_id = "run_watering_0001"
            evidence_refs = (make_evidence(),)
    if event_type in {"compile_failed", "run_failed", "task_completed"}:
        evidence_refs = (make_evidence(),)

    return GameEvent(
        event_id=f"event_{event_type}_0001",
        event_type=event_type,
        student_id=student_id,
        task_id=task_id,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
        occurred_at=NOW,
        expected_world_revision=expected_world_revision,
        skill_ref=skill_ref,
        run_id=run_id,
        build_id=build_id,
        failure_count=count,
        failure_key=failure_key,
        evidence_refs=evidence_refs,
        payload={},
    )


def make_task(operation: OperationContext | None = None) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=TASK_ID,
        title="Water every plot",
        goal="Use one loop to water all eight plots.",
        story="The garden needs water before sunset.",
        knowledge_points=("for_loop", "sequence"),
        request_context=operation or make_operation(),
    )


def make_session(
    *,
    operation: OperationContext | None = None,
    student_id: str = STUDENT_ID,
    task_id: str = TASK_ID,
    session_id: str = SESSION_ID,
    world_id: str = WORLD_ID,
) -> SessionSnapshot:
    return SessionSnapshot(
        session_id,
        student_id,
        task_id,
        world_id,
        operation or make_operation(),
    )


def make_world_state(*, plot_count: int = 8, hydration: int = 0) -> dict[str, object]:
    plots = [
        {
            "plot_id": f"plot_{index:04d}",
            "position": {"x": index, "y": 0},
            "soil_state": "TILLED",
            "hydration": hydration,
            "crop": None,
            "last_updated_event_sequence": 0,
        }
        for index in range(1, plot_count + 1)
    ]
    return {
        "clock": {"day": 1, "minute_of_day": 480, "tick": 10},
        "avatar": {
            "entity_id": "avatar_0001",
            "position": {"x": 0, "y": 0},
            "energy": 100,
        },
        "inventory": [],
        "plots": plots,
        "agents": [],
    }


def make_world_snapshot(
    operation: OperationContext | None = None,
    *,
    state: Mapping[str, object] | None = None,
    revision: int = 5,
    world_id: str = WORLD_ID,
) -> WorldSnapshot:
    return WorldSnapshot(
        request_context=operation or make_operation(),
        world_id=world_id,
        revision=revision,
        last_event_sequence=40,
        state_hash="d" * 64,
        generated_at=NOW,
        world_rules_version="farm-rules-1",
        state=state or make_world_state(),
    )


def make_learner_profile(
    operation: OperationContext | None = None,
    *,
    revision: int = 0,
) -> LearnerProfileSnapshot:
    return LearnerProfileSnapshot(
        student_id=STUDENT_ID,
        revision=revision,
        competencies={},
        request_context=operation or make_operation(),
        evidence_refs=(),
    )


def make_teaching_directive(*, learner_revision: int = 0) -> TeachingDirective:
    return TeachingDirective(
        phase=TeachingPhase.REVIEW,
        target_concept="for_loop",
        hint_level=0,
        allowed_response_types=("message",),
        patch_eligible=False,
        full_solution_eligible=False,
        required_evidence_ids=(),
        reason_codes=(
            "LEARNER_REVISION_ZERO" if learner_revision == 0 else "LEARNER_CONCEPT_UNOBSERVED",
            "PATCH_DISABLED_RUNTIME_STAGE",
            "FULL_SOLUTION_DISABLED",
        ),
        pedagogy_policy_version=PEDAGOGY_POLICY_VERSION,
        learner_revision=learner_revision,
        teaching_spec_version="teaching-1",
    )


def make_context(role: str = "world_agent") -> TurnContext:
    if role == "xiaohutao":
        event = make_event("run_skill_requested")
        skill = make_skill()
        return TurnContext(
            role="xiaohutao",
            event=event,
            task=make_task(),
            session=make_session(),
            hint_level=0,
            world=summarize_world(make_world_snapshot()),
            skill=skill,
            available_skills=(skill,),
        )
    operation = make_operation()
    event = make_event("task_started")
    learner_profile = make_learner_profile(operation)
    return TurnContext(
        role="world_agent",
        event=event,
        task=make_task(operation),
        session=make_session(operation=operation),
        hint_level=0,
        world=summarize_world(make_world_snapshot(operation)),
        learner_profile=learner_profile,
        teaching_directive=make_teaching_directive(learner_revision=learner_profile.revision),
    )


def make_role_config(
    role: str = "world_agent",
    *,
    allowed_events: tuple[str, ...] | None = None,
    allowed_tools: tuple[str, ...] = (),
    max_tool_calls: int = 1,
) -> RoleConfig:
    events = allowed_events
    if events is None:
        events = ("run_skill_requested",) if role == "xiaohutao" else ("task_started",)
    return RoleConfig(
        id=role,
        display_name=role,
        purpose="Test the bounded Agent runtime.",
        allowed_events=events,
        allowed_tools=allowed_tools,
        response_schema="AgentDecisionV1",
        temperature=0.0,
        max_output_tokens=256,
        timeout_ms=1_000,
        prompt="Return only a closed Agent decision.",
        limits=RoleLimits(
            max_tool_calls=max_tool_calls,
            max_message_chars=500,
            allow_skill_patch=False,
            require_confirmation_for_patch=False,
        ),
    )


class StaticRoleConfigs:
    def __init__(self, *configs: RoleConfig) -> None:
        self.configs = {config.id: config for config in configs}

    def get(self, role: str) -> RoleConfig:
        return self.configs[role]


class TraceSink:
    def __init__(self) -> None:
        self.events: list[AgentTraceEvent] = []

    async def record(self, event: AgentTraceEvent, context: OperationContext) -> None:
        del context
        self.events.append(event)


class SequenceLlm:
    def __init__(self, replies: Sequence[object]) -> None:
        self._replies = list(replies)
        self.requests: list[object] = []

    async def generate(self, request: object, context: OperationContext) -> object:
        del context
        self.requests.append(request)
        if not self._replies:
            raise AssertionError("LLM received more requests than the test declared")
        return self._replies.pop(0)


def make_reply(
    output: Mapping[str, object],
    *,
    input_tokens: int = 7,
    output_tokens: int = 5,
) -> Success[LlmReply]:
    return Success(
        LlmReply(
            output=output,
            provider="fixture-provider",
            model="fixture-model",
            source="provider",
            degraded=False,
            fallback_reason=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            evidence_refs=(),
        )
    )


def decision_output(role: str, message: str = "Verified response.") -> dict[str, object]:
    return {
        "kind": "decision",
        "decision": {
            "role": role,
            "response_type": "message",
            "message": message,
            "question": None,
            "hint_level": None,
            "learner_inference": None,
            "skill_patch": None,
            "requires_student_confirmation": False,
        },
        "tool_calls": [],
    }


def tool_calls_output(
    name: str,
    arguments: Mapping[str, object],
    *,
    call_id: str = "call_runtime_0001",
) -> dict[str, object]:
    return {
        "kind": "tool_calls",
        "decision": None,
        "tool_calls": [{"call_id": call_id, "name": name, "arguments": arguments}],
    }


def make_versions() -> VersionSet:
    return VersionSet(
        api_version="1.0.0",
        event_version="1",
        policy_version="policy-1",
        world_rules_version="farm-rules-1",
        teaching_spec_version="teaching-1",
        prompt_version="prompt-1",
        model_version="fixture-model",
    )


def make_agent_decision(message: str = "Persist this exact decision.") -> AgentDecision:
    return AgentDecision(
        draft=DecisionDraft(
            role="world_agent",
            response_type="message",
            message=message,
            question=None,
            hint_level=None,
            learner_inference=None,
            skill_patch=None,
            requires_student_confirmation=False,
        ),
        message_key="agent.world_agent.message",
        source="provider",
        degraded=False,
        fallback_reason=None,
        provider="fixture-provider",
        model="fixture-model",
        input_tokens=1,
        output_tokens=1,
        tool_calls=(),
        evidence_refs=(),
        completed_at=NOW,
        teaching_directive=make_teaching_directive(),
    )


class RecordingReads:
    def __init__(
        self,
        *,
        operation: OperationContext | None = None,
        task: TaskSnapshot | None = None,
        session: SessionSnapshot | None = None,
        skill: SkillSnapshot | None = None,
        world: WorldSnapshot | None = None,
        learner_profile: LearnerProfileSnapshot | None = None,
    ) -> None:
        self.operation = operation or make_operation()
        self.task = task or make_task(self.operation)
        self.session = session or make_session(operation=self.operation)
        self.skill = skill or make_skill(self.operation)
        self.world = world or make_world_snapshot(self.operation)
        self.learner_profile = learner_profile or make_learner_profile(self.operation)
        self.calls: list[str] = []

    async def get_task(self, task_id: str, context: OperationContext) -> TaskSnapshot:
        del task_id, context
        self.calls.append("get_task")
        return self.task

    async def get_session(self, session_id: str, context: OperationContext) -> SessionSnapshot:
        del session_id, context
        self.calls.append("get_session")
        return self.session

    async def get_snapshot(
        self, world_id: str, context: OperationContext
    ) -> Success[WorldSnapshot]:
        del world_id, context
        self.calls.append("get_snapshot")
        return Success(self.world)

    async def get_bound_skill(
        self,
        skill_ref: SkillRef,
        context: OperationContext,
    ) -> SkillSnapshot:
        del skill_ref, context
        self.calls.append("get_bound_skill")
        return self.skill

    async def list_active_skills(
        self,
        student_id: str,
        context: OperationContext,
    ) -> tuple[SkillSnapshot, ...]:
        del student_id, context
        self.calls.append("list_active_skills")
        return (self.skill,)

    async def get_profile(
        self,
        student_id: str,
        knowledge_points: tuple[str, ...],
        context: OperationContext,
    ) -> LearnerProfileSnapshot:
        del student_id, knowledge_points, context
        self.calls.append("get_profile")
        return self.learner_profile

    def __getattr__(self, name: str) -> object:
        if name.startswith(("get_", "list_")):
            raise AssertionError(f"unexpected context dependency access: {name}")
        raise AttributeError(name)


class InMemoryWateringInvocations:
    """Deterministic in-memory Sandbox + CAS World application adapter."""

    def __init__(
        self,
        operation: OperationContext,
        skill: SkillSnapshot,
        state: Mapping[str, object],
        *,
        revision: int = 5,
        last_event_sequence: int = 40,
    ) -> None:
        self.operation = operation
        self.skill = skill
        self.state = deepcopy(dict(state))
        self.revision = revision
        self.last_event_sequence = last_event_sequence
        self.call_count = 0
        self.execution_count = 0
        self.fail_after_next_commit = False
        self.requests: list[SkillInvocationRequest] = []
        self._receipts: dict[
            tuple[str, str],
            tuple[str, SkillInvocationResult],
        ] = {}

    async def get_snapshot(
        self,
        world_id: str,
        context: OperationContext,
    ) -> Success[WorldSnapshot]:
        if world_id != WORLD_ID or context.actor != self.operation.actor:
            raise AgentToolExecutionError(
                "TOOL_WORLD_IDENTITY_MISMATCH",
                "in-memory World read crossed its canonical identity",
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
                "receipt lookup crossed its actor or content authority",
            )
        receipt = self._receipts.get((context.actor.tenant_id, invocation_id))
        return None if receipt is None else receipt[1]

    async def invoke(
        self,
        request: SkillInvocationRequest,
        context: OperationContext,
    ) -> SkillInvocationResult:
        self.call_count += 1
        self.requests.append(request)
        if (
            context.actor != self.operation.actor
            or context.content_ref != self.operation.content_ref
            or request.tenant_id != context.actor.tenant_id
            or request.world_id != WORLD_ID
            or request.skill_ref != self.skill.ref
        ):
            raise AgentToolExecutionError(
                "TOOL_INVOCATION_IDENTITY_MISMATCH",
                "Sandbox/World invocation crossed its tenant, content, World or Skill identity",
            )
        replay_key = (request.tenant_id, request.invocation_id)
        replay = self._receipts.get(replay_key)
        if replay is not None:
            request_sha256, result = replay
            if request_sha256 != request.request_sha256:
                raise AgentToolExecutionError(
                    "TOOL_IDEMPOTENCY_KEY_REUSED",
                    "same invocation identity was reused with a different request hash",
                )
            return result
        if request.expected_world_revision != self.revision:
            raise AgentToolExecutionError(
                "TOOL_WORLD_REVISION_CONFLICT",
                "in-memory World CAS rejected a stale expected revision",
            )
        length = request.arguments["length"]
        if isinstance(length, bool) or not isinstance(length, int):
            raise TypeError("length must be an integer")
        working_state = deepcopy(self.state)
        raw_plots = working_state["plots"]
        if not isinstance(raw_plots, list):
            raise TypeError("canonical in-memory plots must be a list")

        def water(index: int) -> None:
            plot = raw_plots[index]
            if not isinstance(plot, dict):
                raise TypeError("plot must be a mutable object")
            plot["hydration"] = 100
            plot["last_updated_event_sequence"] = self.last_event_sequence + index + 1

        sandbox_globals: dict[str, object] = {
            "__builtins__": {"range": range},
            "water": water,
        }
        exec(self.skill.source_code, sandbox_globals)  # noqa: S102 - fixed test fixture Sandbox
        entrypoint = sandbox_globals.get("water_plots")
        if not callable(entrypoint):
            raise TypeError("certified fixture Skill has no water_plots entrypoint")
        entrypoint(length)
        self.execution_count += 1

        watered = sum(isinstance(plot, dict) and plot.get("hydration") == 100 for plot in raw_plots)
        task_success = watered == len(raw_plots)
        revision_after = self.revision
        sequence_after = self.last_event_sequence
        world_commit = None
        if task_success:
            revision_after = self.revision + 1
            sequence_after = self.last_event_sequence + watered
            next_state_hash = hashlib.sha256(
                json.dumps(
                    working_state,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            world_commit = WorldCommitReceipt(
                world_id=request.world_id,
                previous_revision=self.revision,
                world_revision=revision_after,
                first_event_sequence=self.last_event_sequence + 1,
                last_event_sequence=sequence_after,
                committed_at=NOW,
                state_hash=next_state_hash,
            )
            evidence = EvidenceRef(
                "evidence_world_commit_0001",
                EvidenceType.WORLD_COMMIT,
                NOW,
                sha256=world_commit_receipt_sha256(world_commit),
            )
        else:
            evidence = make_evidence(
                "evidence_test_report_0001",
                EvidenceType.TEST_REPORT,
            )
        run = RunResultSnapshot(
            run_id="run_watering_0001",
            session_id=request.session_id,
            turn_id=request.turn_id,
            command_id=request.command_id,
            world_id=request.world_id,
            skill_ref=request.skill_ref,
            task_success=task_success,
            world_revision_before=request.expected_world_revision,
            world_revision_after=revision_after,
            world_difference={
                "watered_plots": watered,
                "total_plots": len(raw_plots),
                "loop_length": length,
            },
            failed_actions=(
                ()
                if task_success
                else ({"reason": "not_all_plots_watered", "watered_plots": watered},)
            ),
            failure_key=None if task_success else "watering_loop_short",
            evidence_refs=(evidence,),
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
        if task_success:
            self.state = working_state
            self.revision = revision_after
            self.last_event_sequence = sequence_after
        self._receipts[replay_key] = (request.request_sha256, result)
        if self.fail_after_next_commit:
            self.fail_after_next_commit = False
            raise ConnectionError("simulated response loss after atomic commit")
        return result


class CommitStore:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: NOW,
        lease_seconds: int = 30,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._claim_sequence = 0
        self.commits: list[tuple[GameEvent, AgentDecision, OperationContext]] = []
        self.records: dict[tuple[str, str, str, str, str], CommittedAgentTurn] = {}
        self.claims: dict[tuple[str, str, str, str, str], tuple[str, datetime]] = {}

    @staticmethod
    def _key(event: GameEvent, context: OperationContext) -> tuple[str, str, str, str, str]:
        return (
            context.actor.tenant_id,
            event.event_id,
            event.session_id,
            event.turn_id,
            event.command_id,
        )

    async def get_committed(
        self,
        event: GameEvent,
        context: OperationContext,
    ) -> CommittedAgentTurn | None:
        return self.records.get(self._key(event, context))

    async def claim(
        self,
        event: GameEvent,
        context: OperationContext,
    ) -> AgentTurnClaimReceipt:
        key = self._key(event, context)
        existing = self.records.get(key)
        if existing is not None:
            return AgentTurnClaimReceipt(None, None, existing)
        now = self._clock()
        live_claim = self.claims.get(key)
        if live_claim is not None and live_claim[1] > now:
            raise AgentPersistenceError(
                "AGENT_TURN_IN_PROGRESS",
                "another worker owns the live Agent turn claim",
                {"claim_expires_at": live_claim[1].isoformat()},
            )
        self._claim_sequence += 1
        identity = f"{':'.join(key)}:{self._claim_sequence}"
        claim_id = f"claim_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
        expires_at = now + timedelta(seconds=self._lease_seconds)
        self.claims[key] = (claim_id, expires_at)
        return AgentTurnClaimReceipt(claim_id, expires_at, None)

    async def abandon(
        self,
        event: GameEvent,
        claim_id: str,
        context: OperationContext,
    ) -> None:
        key = self._key(event, context)
        live_claim = self.claims.get(key)
        if live_claim is None:
            return
        if live_claim[0] != claim_id:
            raise AgentPersistenceError(
                "AGENT_TURN_CLAIM_LOST",
                "Agent turn abandon does not own the durable claim",
            )
        del self.claims[key]

    async def renew(
        self,
        event: GameEvent,
        claim_id: str,
        minimum_ttl_ms: int,
        context: OperationContext,
    ) -> AgentTurnClaimReceipt:
        key = self._key(event, context)
        existing = self.records.get(key)
        if existing is not None:
            return AgentTurnClaimReceipt(None, None, existing)
        live_claim = self.claims.get(key)
        now = self._clock()
        if live_claim is None or live_claim[0] != claim_id:
            raise AgentPersistenceError(
                "AGENT_TURN_CLAIM_LOST",
                "Agent turn renew does not own the durable claim",
            )
        if live_claim[1] <= now:
            raise AgentPersistenceError(
                "AGENT_TURN_CLAIM_EXPIRED",
                "Agent turn lease expired before Runtime renewal",
            )
        renewed_expiry = max(
            live_claim[1],
            now + timedelta(milliseconds=minimum_ttl_ms),
        )
        self.claims[key] = (claim_id, renewed_expiry)
        return AgentTurnClaimReceipt(claim_id, renewed_expiry, None)

    async def commit(
        self,
        event: GameEvent,
        route: RoleRoute,
        decision: AgentDecision,
        claim_id: str,
        context: OperationContext,
    ) -> AgentTurnCommitReceipt:
        key = self._key(event, context)
        existing = self.records.get(key)
        if existing is not None:
            return AgentTurnCommitReceipt(existing, False)
        live_claim = self.claims.get(key)
        if live_claim is None or live_claim[0] != claim_id:
            raise AgentPersistenceError(
                "AGENT_TURN_CLAIM_LOST",
                "Agent turn commit does not own the durable claim",
            )
        if live_claim[1] <= self._clock():
            raise AgentPersistenceError(
                "AGENT_TURN_CLAIM_EXPIRED",
                "Agent turn commit lease expired before the durable commit",
                {"claim_expires_at": live_claim[1].isoformat()},
            )
        record = CommittedAgentTurn(event, context.actor, context.content_ref, route, decision)
        self.records[key] = record
        del self.claims[key]
        self.commits.append((event, decision, context))
        return AgentTurnCommitReceipt(record, True)
