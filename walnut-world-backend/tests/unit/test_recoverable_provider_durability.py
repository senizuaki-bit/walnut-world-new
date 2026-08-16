from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    ContractError,
    ErrorCategory,
    EvidenceRef,
    EvidenceType,
    Failure,
    FrozenJsonObject,
    LlmMessage,
    LlmReply,
    LlmRequest,
    OperationContext,
    Success,
    VersionSet,
)
from yaya_agent_runtime import (
    LlmDispatchIdentity,
    LlmDispatchResource,
    LlmRelayCapabilities,
    RecoverableLlmExpired,
    RecoverableLlmUnavailable,
    llm_request_sha256,
    provider_dispatch_id,
)
from yaya_agent_runtime.adapters import (
    HttpResponse,
    RecoverableOpenAIRelayAdapter,
    RelayCapabilityError,
)

from walnut_backend.adapters.postgres.durable_llm import (
    DurableLlmDispatchExpired,
    DurableLlmDispatchPending,
    DurableLlmDispatchUnknown,
    PostgresDurableLlm,
)
from walnut_backend.adapters.postgres.models import request_context_data
from walnut_backend.adapters.postgres.run_outcomes import validate_provider_decision_wire
from walnut_backend.adapters.postgres.workflow_jobs import (
    ClaimedWorkflowJob,
    WorkflowInvariantError,
    workflow_receipt_sha256,
)
from walnut_backend.provider_config import RecoverableProviderSettings
from walnut_backend.provider_wiring import create_recoverable_provider

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
PROVIDER = "fixture-provider"
MODEL = "fixture-model-v1"


def _context() -> OperationContext:
    return OperationContext(
        request_id="req_provider_durable_0001",
        correlation_id="corr_provider_durable_0001",
        trace_id="trace_provider_durable_0001",
        requested_at=NOW,
        actor=ActorRef(
            "tenant_provider_durable",
            "student_provider_durable",
            ActorType.STUDENT,
            ("game:player",),
        ),
        content_ref=ContentRef("UNIT_PROVIDER", "1.0.0", "a" * 64),
        command_id="cmd_provider_durable_0001",
        causation_id=None,
    )


def _request() -> LlmRequest:
    return LlmRequest(
        messages=(
            LlmMessage("system", "Return strict JSON."),
            LlmMessage("user", "recover one generation"),
        ),
        output_schema=cast(
            FrozenJsonObject,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ("decision",),
                "properties": {"decision": {"type": "string"}},
            },
        ),
        temperature=0,
        max_output_tokens=128,
        timeout_ms=5_000,
        versions=VersionSet(
            "v1",
            "v1",
            "policy-v1",
            "world-v1",
            "teaching-v1",
            prompt_version="prompt-v1",
            model_version=MODEL,
        ),
    )


def _claim(context: OperationContext) -> ClaimedWorkflowJob:
    return ClaimedWorkflowJob(
        job_id="job_provider_durable_0001",
        tenant_id=context.actor.tenant_id,
        command_id=cast(str, context.command_id),
        operation="EXECUTE_AGENT_TURN",
        subject_type="AGENT_TURN",
        subject_id="turn_provider_durable_0001",
        phase="EXECUTE",
        status="CLAIMED",
        attempt=1,
        fencing_token=7,
        lease_owner="worker-provider-durable",
        lease_expires_at=NOW + timedelta(minutes=10),
        request_sha256="e" * 64,
        job={"request_context": request_context_data(context)},
    )


@dataclass
class _MemoryReceipt:
    input_sha256: str
    receipt_json: dict[str, Any]
    output_sha256: str


@dataclass
class _MemorySessions:
    receipts: dict[str, _MemoryReceipt] = field(default_factory=dict)
    lose_commit_for: str | None = None
    commit_was_lost: bool = False

    def __call__(self) -> _MemorySession:
        return _MemorySession(self)


class _MemorySession:
    def __init__(self, owner: _MemorySessions) -> None:
        self.owner = owner
        self.last_step: str | None = None

    async def __aenter__(self) -> _MemorySession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc_type, exc, traceback
        return False

    def begin(self) -> _MemoryTransaction:
        return _MemoryTransaction(self)


class _MemoryTransaction:
    def __init__(self, session: _MemorySession) -> None:
        self.session = session

    async def __aenter__(self) -> _MemoryTransaction:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc, traceback
        owner = self.session.owner
        if (
            exc_type is None
            and owner.lose_commit_for == self.session.last_step
            and not owner.commit_was_lost
        ):
            owner.commit_was_lost = True
            raise ConnectionError("database commit acknowledgement lost")
        return False


class _MemoryJobs:
    def __init__(self, sessions: _MemorySessions) -> None:
        self.sessions = sessions

    async def start_step_in_session(
        self, session: _MemorySession, *args: Any, **kwargs: Any
    ) -> None:
        del session, args, kwargs

    async def record_step_in_session(
        self,
        session: _MemorySession,
        claim: ClaimedWorkflowJob,
        *,
        step_name: str,
        input_sha256: str,
        output: Mapping[str, Any],
    ) -> Any:
        del claim
        value = dict(output)
        existing = self.sessions.receipts.get(step_name)
        if existing is not None and (
            existing.input_sha256 != input_sha256 or existing.receipt_json != value
        ):
            raise WorkflowInvariantError("memory receipt conflicts with durable bytes")
        created = existing is None
        if created:
            self.sessions.receipts[step_name] = _MemoryReceipt(
                input_sha256,
                value,
                workflow_receipt_sha256(value),
            )
        session.last_step = step_name
        return SimpleNamespace(created=created)


@dataclass
class _RelayState:
    resources: dict[str, LlmDispatchResource] = field(default_factory=dict)
    generation_count: dict[str, int] = field(default_factory=dict)


class _MemoryRecoverableProvider:
    def __init__(
        self,
        state: _RelayState | None = None,
        *,
        lose_after_generation: bool = False,
        first_put_absent: bool = False,
        pending: bool = False,
        expire_on_get: bool = False,
        unavailable_on_get: bool = False,
        drift_resource: bool = False,
    ) -> None:
        self.state = state or _RelayState()
        self.lose_after_generation = lose_after_generation
        self.first_put_absent = first_put_absent
        self.pending = pending
        self.expire_on_get = expire_on_get
        self.unavailable_on_get = unavailable_on_get
        self.drift_resource = drift_resource
        self.capability_calls = 0
        self.put_identities: list[LlmDispatchIdentity] = []
        self.get_identities: list[LlmDispatchIdentity] = []

    async def validate_capabilities(self) -> LlmRelayCapabilities:
        self.capability_calls += 1
        return LlmRelayCapabilities(
            protocol="YAYA_RECOVERABLE_LLM_V1",
            result_retention_seconds=604_800,
            max_request_bytes=4_194_304,
            max_response_bytes=4_194_304,
            atomic_put_by_dispatch_id=True,
            linearizable_get=True,
            immutable_request_hash=True,
            max_generation_count=1,
        )

    async def dispatch(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource:
        del request, context
        self.put_identities.append(identity)
        if self.first_put_absent and len(self.put_identities) == 1:
            return _absent(identity)
        existing = self.state.resources.get(identity.dispatch_id)
        if existing is None:
            self.state.generation_count[identity.dispatch_id] = 1
            existing = _pending(identity) if self.pending else _success(identity)
            self.state.resources[identity.dispatch_id] = existing
            if self.lose_after_generation:
                self.lose_after_generation = False
                raise RecoverableLlmUnavailable("relay response and immediate GET were lost")
        if self.drift_resource:
            return replace(
                existing,
                identity=replace(identity, context_sha256="b" * 64),
            )
        return replace(existing, replayed=len(self.put_identities) > 1)

    async def reconcile(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource:
        del request, context
        self.get_identities.append(identity)
        if self.expire_on_get:
            raise RecoverableLlmExpired("relay result expired")
        if self.unavailable_on_get:
            raise RecoverableLlmUnavailable("relay GET unavailable")
        return self.state.resources.get(identity.dispatch_id, _absent(identity))


class _MemoryDurableLlm(PostgresDurableLlm):
    def __init__(self, sessions: _MemorySessions, jobs: _MemoryJobs, **kwargs: Any) -> None:
        self._memory_sessions = sessions
        super().__init__(
            session_factory=cast(Any, sessions),
            jobs=cast(Any, jobs),
            **kwargs,
        )

    async def _read_receipts(self, result_name: str, dispatch_name: str) -> tuple[Any, Any]:
        return (
            self._memory_sessions.receipts.get(result_name),
            self._memory_sessions.receipts.get(dispatch_name),
        )

    async def _read_receipt(self, name: str) -> Any:
        return self._memory_sessions.receipts.get(name)


def _durable(
    sessions: _MemorySessions,
    jobs: _MemoryJobs,
    claim: ClaimedWorkflowJob,
    provider: object,
) -> _MemoryDurableLlm:
    return _MemoryDurableLlm(
        sessions,
        jobs,
        claim=claim,
        provider=provider,
        provider_name=PROVIDER,
        model_version=MODEL,
        lease_seconds=60,
    )


def _absent(identity: LlmDispatchIdentity) -> LlmDispatchResource:
    return LlmDispatchResource(
        identity=identity,
        completion_sha256="c" * 64,
        state="ABSENT",
        generation_count=0,
        replayed=False,
    )


def _pending(identity: LlmDispatchIdentity) -> LlmDispatchResource:
    return LlmDispatchResource(
        identity=identity,
        completion_sha256="c" * 64,
        state="PENDING",
        generation_count=1,
        replayed=False,
        retry_after_seconds=2,
    )


def _success(identity: LlmDispatchIdentity) -> LlmDispatchResource:
    return LlmDispatchResource(
        identity=identity,
        completion_sha256="c" * 64,
        state="SUCCEEDED",
        generation_count=1,
        replayed=False,
        result=Success(
            LlmReply(
                output=cast(FrozenJsonObject, {"decision": "invoke_skill"}),
                provider=PROVIDER,
                model=MODEL,
                source="provider",
                degraded=False,
                fallback_reason=None,
                input_tokens=3,
                output_tokens=2,
                evidence_refs=(),
            )
        ),
        created_at=NOW,
        updated_at=NOW,
        raw_response_sha256="d" * 64,
    )


def _teaching_success(identity: LlmDispatchIdentity) -> LlmDispatchResource:
    return LlmDispatchResource(
        identity=identity,
        completion_sha256="c" * 64,
        state="SUCCEEDED",
        generation_count=1,
        replayed=False,
        result=Success(
            LlmReply(
                output=cast(
                    FrozenJsonObject,
                    {
                        "kind": "decision",
                        "decision": {
                            "role": "teaching_agent",
                            "response_type": "question",
                            "message": "Review the failed run evidence.",
                            "question": "What observable result differs from the objective?",
                            "hint_level": None,
                            "learner_inference": {
                                "concept": "for_loop",
                                "score_delta": -0.1,
                                "confidence": 0.8,
                                "reason": "The failed run is direct evidence.",
                                "evidence_ids": ["evidence_001"],
                            },
                            "skill_patch": None,
                            "requires_student_confirmation": False,
                        },
                        "tool_calls": [],
                    },
                ),
                provider=PROVIDER,
                model=MODEL,
                source="provider",
                degraded=False,
                fallback_reason=None,
                input_tokens=8,
                output_tokens=13,
                evidence_refs=(),
            )
        ),
        created_at=NOW,
        updated_at=NOW,
        raw_response_sha256="d" * 64,
    )


def _successful_generation_with_invalid_output(
    identity: LlmDispatchIdentity,
) -> LlmDispatchResource:
    return LlmDispatchResource(
        identity=identity,
        completion_sha256="c" * 64,
        state="SUCCEEDED",
        generation_count=1,
        replayed=False,
        result=Failure(
            ContractError(
                code="INVARIANT_VIOLATION",
                category=ErrorCategory.INVARIANT,
                retryable=False,
                user_message_key="system.invariant_violation",
                stage="MODEL_OUTPUT",
                details=cast(FrozenJsonObject, {"repairable": True}),
            )
        ),
        created_at=NOW,
        updated_at=NOW,
        raw_response_sha256="d" * 64,
    )


def test_successful_generation_with_invalid_model_output_is_durable_and_replayable() -> None:
    async def exercise() -> None:
        context = _context()
        request = _request()
        claim = _claim(context)
        sessions = _MemorySessions()
        jobs = _MemoryJobs(sessions)
        state = _RelayState()

        class InvalidOutputProvider(_MemoryRecoverableProvider):
            async def dispatch(
                self,
                identity: LlmDispatchIdentity,
                request: LlmRequest,
                context: OperationContext,
            ) -> LlmDispatchResource:
                del request, context
                self.put_identities.append(identity)
                resource = _successful_generation_with_invalid_output(identity)
                self.state.resources[identity.dispatch_id] = resource
                self.state.generation_count[identity.dispatch_id] = 1
                return resource

        generated = await _durable(
            sessions,
            jobs,
            claim,
            InvalidOutputProvider(state),
        ).generate(request, context)
        assert isinstance(generated, Failure)
        assert set(sessions.receipts) == {"PROVIDER_DISPATCH_01", "PROVIDER_RESULT_01"}

        replayed = await _durable(
            sessions,
            jobs,
            claim,
            _MemoryRecoverableProvider(state),
        ).generate(request, context)
        assert replayed == generated
        assert state.generation_count == {
            provider_dispatch_id(
                claim.tenant_id,
                claim.job_id,
                1,
                llm_request_sha256(request),
            ): 1
        }

    asyncio.run(exercise())


def test_formal_teaching_terminal_receipt_with_floats_round_trips_and_tamper_fails() -> None:
    async def exercise() -> None:
        context = _context()
        request = _request()
        claim = _claim(context)
        sessions = _MemorySessions()
        jobs = _MemoryJobs(sessions)
        state = _RelayState()

        class TeachingProvider(_MemoryRecoverableProvider):
            async def dispatch(
                self,
                identity: LlmDispatchIdentity,
                request: LlmRequest,
                context: OperationContext,
            ) -> LlmDispatchResource:
                del request, context
                self.put_identities.append(identity)
                resource = _teaching_success(identity)
                self.state.resources[identity.dispatch_id] = resource
                self.state.generation_count[identity.dispatch_id] = 1
                return resource

        generated = await _durable(
            sessions,
            jobs,
            claim,
            TeachingProvider(state),
        ).generate(request, context)
        assert isinstance(generated, Success)
        assert generated.value.evidence_refs == ()

        replayed = await _durable(
            sessions,
            jobs,
            claim,
            _MemoryRecoverableProvider(state),
        ).generate(request, context)
        assert replayed == generated
        learner = cast(
            Mapping[str, object],
            cast(Mapping[str, object], generated.value.output["decision"])[
                "learner_inference"
            ],
        )
        assert learner["score_delta"] == -0.1
        assert learner["confidence"] == 0.8

        terminal = sessions.receipts["PROVIDER_RESULT_01"]
        evidence = EvidenceRef(
            evidence_id="evidence_run_aaaaaaaaaaaaaaaaaaaaaaaa",
            evidence_type=EvidenceType.SANDBOX_LOG,
            created_at=NOW,
            sha256="a" * 64,
        )
        durable_draft = dict(cast(Mapping[str, object], generated.value.output["decision"]))
        durable_inference = dict(
            cast(Mapping[str, object], durable_draft["learner_inference"])
        )
        durable_inference["evidence_ids"] = [evidence.evidence_id]
        durable_draft["learner_inference"] = durable_inference
        durable_draft["message"] = "Canonical evidence-grounded teaching feedback."
        durable_draft["question"] = "Which observed result differs from the task goal?"
        validate_provider_decision_wire(
            cast(Any, (terminal,)),
            decision_draft=durable_draft,
            evidence_refs=(evidence,),
            decision={
                "provider": PROVIDER,
                "model": MODEL,
                "input_tokens": 8,
                "output_tokens": 13,
                "tool_calls": [],
            },
        )
        drifted_draft = {**durable_draft, "requires_student_confirmation": True}
        with pytest.raises(WorkflowInvariantError, match="Provider authority"):
            validate_provider_decision_wire(
                cast(Any, (terminal,)),
                decision_draft=drifted_draft,
                evidence_refs=(evidence,),
                decision={
                    "provider": PROVIDER,
                    "model": MODEL,
                    "input_tokens": 8,
                    "output_tokens": 13,
                    "tool_calls": [],
                },
            )
        terminal.output_sha256 = "0" * 64
        with pytest.raises(WorkflowInvariantError, match="durable digest"):
            await _durable(
                sessions,
                jobs,
                claim,
                _MemoryRecoverableProvider(state),
            ).generate(request, context)

    asyncio.run(exercise())


def test_response_loss_recovers_by_get_without_second_generation() -> None:
    asyncio.run(_exercise_response_loss_recovery())


async def _exercise_response_loss_recovery() -> None:
    context = _context()
    request = _request()
    claim = _claim(context)
    sessions = _MemorySessions()
    jobs = _MemoryJobs(sessions)
    relay = _RelayState()
    first = _MemoryRecoverableProvider(relay, lose_after_generation=True)

    with pytest.raises(DurableLlmDispatchUnknown, match="acknowledgement is unknown"):
        await _durable(sessions, jobs, claim, first).generate(request, context)
    assert set(sessions.receipts) == {"PROVIDER_DISPATCH_01"}
    assert len(first.put_identities) == 1

    restarted = _MemoryRecoverableProvider(relay)
    recovered = await _durable(sessions, jobs, claim, restarted).generate(request, context)
    assert isinstance(recovered, Success)
    assert recovered.value.output["decision"] == "invoke_skill"
    assert len(restarted.put_identities) == 0
    assert restarted.get_identities == first.put_identities
    dispatch_id = first.put_identities[0].dispatch_id
    assert dispatch_id == provider_dispatch_id(
        claim.tenant_id,
        claim.job_id,
        1,
        llm_request_sha256(request),
    )
    assert relay.generation_count[dispatch_id] == 1
    assert set(sessions.receipts) == {"PROVIDER_DISPATCH_01", "PROVIDER_RESULT_01"}

    replay_provider = _MemoryRecoverableProvider(relay)
    replayed = await _durable(sessions, jobs, claim, replay_provider).generate(request, context)
    assert replayed == recovered
    assert replay_provider.capability_calls == 1
    assert len(replay_provider.get_identities) == 1


def test_absent_reuses_same_put_id_and_pending_unknown_expired_are_distinct() -> None:
    asyncio.run(_exercise_recovery_states())


async def _exercise_recovery_states() -> None:
    context = _context()
    request = _request()
    claim = _claim(context)

    absent_sessions = _MemorySessions()
    absent_jobs = _MemoryJobs(absent_sessions)
    absent_provider = _MemoryRecoverableProvider(first_put_absent=True)
    result = await _durable(absent_sessions, absent_jobs, claim, absent_provider).generate(
        request, context
    )
    assert isinstance(result, Success)
    assert len(absent_provider.put_identities) == 2
    assert absent_provider.put_identities[0] == absent_provider.put_identities[1]
    dispatch_id = absent_provider.put_identities[0].dispatch_id
    assert absent_provider.state.generation_count[dispatch_id] == 1

    pending_sessions = _MemorySessions()
    pending_jobs = _MemoryJobs(pending_sessions)
    pending_state = _RelayState()
    pending_provider = _MemoryRecoverableProvider(pending_state, pending=True)
    with pytest.raises(DurableLlmDispatchPending) as pending_error:
        await _durable(pending_sessions, pending_jobs, claim, pending_provider).generate(
            request, context
        )
    assert pending_error.value.retry_after_seconds == 2
    restarted_pending = _MemoryRecoverableProvider(pending_state)
    with pytest.raises(DurableLlmDispatchPending):
        await _durable(pending_sessions, pending_jobs, claim, restarted_pending).generate(
            request, context
        )
    assert len(pending_provider.put_identities) == 1
    assert len(restarted_pending.get_identities) == 1

    expired = _MemoryRecoverableProvider(pending_state, expire_on_get=True)
    with pytest.raises(DurableLlmDispatchExpired):
        await _durable(pending_sessions, pending_jobs, claim, expired).generate(request, context)

    unknown = _MemoryRecoverableProvider(pending_state, unavailable_on_get=True)
    with pytest.raises(DurableLlmDispatchUnknown, match="cannot be reconciled"):
        await _durable(pending_sessions, pending_jobs, claim, unknown).generate(request, context)


def test_hash_context_provider_model_and_resource_drift_fail_closed() -> None:
    asyncio.run(_exercise_authority_drift())


async def _exercise_authority_drift() -> None:
    context = _context()
    request = _request()
    claim = _claim(context)
    sessions = _MemorySessions()
    jobs = _MemoryJobs(sessions)
    provider = _MemoryRecoverableProvider()
    await _durable(sessions, jobs, claim, provider).generate(request, context)

    dispatch = sessions.receipts["PROVIDER_DISPATCH_01"]
    original = dict(dispatch.receipt_json)
    for name, drift in (
        ("request_sha256", "1" * 64),
        ("context_sha256", "2" * 64),
        ("provider", "other-provider"),
        ("model", "other-model"),
    ):
        dispatch.receipt_json = {**original, name: drift}
        dispatch.output_sha256 = workflow_receipt_sha256(dispatch.receipt_json)
        with pytest.raises(WorkflowInvariantError, match="dispatch receipt differs"):
            await _durable(sessions, jobs, claim, provider).generate(request, context)
    dispatch.receipt_json = original
    dispatch.output_sha256 = workflow_receipt_sha256(original)

    changed_context = replace(context, command_id="cmd_provider_durable_changed")
    with pytest.raises(WorkflowInvariantError, match="context command"):
        await _durable(sessions, jobs, claim, provider).generate(request, changed_context)

    fresh_sessions = _MemorySessions()
    drift_provider = _MemoryRecoverableProvider(drift_resource=True)
    with pytest.raises(WorkflowInvariantError, match="resource identity drifted"):
        await _durable(
            fresh_sessions,
            _MemoryJobs(fresh_sessions),
            claim,
            drift_provider,
        ).generate(request, context)

    class _DegradedProvider(_MemoryRecoverableProvider):
        async def dispatch(
            self,
            identity: LlmDispatchIdentity,
            request: LlmRequest,
            context: OperationContext,
        ) -> LlmDispatchResource:
            resource = await super().dispatch(identity, request, context)
            assert isinstance(resource.result, Success)
            return replace(
                resource,
                result=Success(
                    replace(
                        resource.result.value,
                        source="provider_fallback",
                        degraded=True,
                        fallback_reason="NOT_LIVE",
                    )
                ),
            )

    degraded_sessions = _MemorySessions()
    with pytest.raises(WorkflowInvariantError, match="source=provider"):
        await _durable(
            degraded_sessions,
            _MemoryJobs(degraded_sessions),
            claim,
            _DegradedProvider(),
        ).generate(request, context)

    class _DirectProvider:
        async def generate(self, request: Any, context: Any) -> Any:
            del request, context

    with pytest.raises(TypeError, match="RecoverableLlmPort"):
        _durable(_MemorySessions(), _MemoryJobs(_MemorySessions()), claim, _DirectProvider())


def test_terminal_commit_ack_loss_rereads_exact_receipt() -> None:
    asyncio.run(_exercise_terminal_commit_ack_loss())


async def _exercise_terminal_commit_ack_loss() -> None:
    context = _context()
    request = _request()
    claim = _claim(context)

    dispatch_sessions = _MemorySessions(lose_commit_for="PROVIDER_DISPATCH_01")
    dispatch_jobs = _MemoryJobs(dispatch_sessions)
    dispatch_provider = _MemoryRecoverableProvider()
    dispatch_result = await _durable(
        dispatch_sessions,
        dispatch_jobs,
        claim,
        dispatch_provider,
    ).generate(request, context)
    assert isinstance(dispatch_result, Success)
    assert dispatch_sessions.commit_was_lost
    assert len(dispatch_provider.get_identities) == 1
    assert len(dispatch_provider.put_identities) == 1
    assert dispatch_provider.get_identities == dispatch_provider.put_identities

    sessions = _MemorySessions(lose_commit_for="PROVIDER_RESULT_01")
    jobs = _MemoryJobs(sessions)
    provider = _MemoryRecoverableProvider()

    result = await _durable(sessions, jobs, claim, provider).generate(request, context)
    assert isinstance(result, Success)
    assert sessions.commit_was_lost
    assert len(provider.put_identities) == 1
    dispatch_id = provider.put_identities[0].dispatch_id
    assert provider.state.generation_count[dispatch_id] == 1
    assert "PROVIDER_RESULT_01" in sessions.receipts


class _CapabilityTransport:
    def __init__(self, *, atomic: bool) -> None:
        self.atomic = atomic
        self.calls = 0

    async def request_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object] | None,
        timeout_ms: int,
    ) -> HttpResponse:
        del url, headers, body, timeout_ms
        self.calls += 1
        assert method == "GET"
        value = {
            "schema_version": "1.0.0",
            "protocol": "YAYA_RECOVERABLE_LLM_V1",
            "result_retention_seconds": 604_800,
            "max_request_bytes": 4_194_304,
            "max_response_bytes": 4_194_304,
            "atomic_put_by_dispatch_id": self.atomic,
            "linearizable_get": True,
            "immutable_request_hash": True,
            "max_generation_count": 1,
        }
        return HttpResponse(200, {"content-type": "application/json"}, json.dumps(value).encode())


def test_config_and_wiring_require_a_capable_relay_without_direct_fallback() -> None:
    asyncio.run(_exercise_config_and_wiring())


async def _exercise_config_and_wiring() -> None:
    direct_only_env = {
        "WALNUT_LLM_ENDPOINT": "https://provider.example/v1/chat/completions",
        "WALNUT_LLM_API_KEY": "direct-provider-secret",
        "WALNUT_LLM_MODEL": MODEL,
        "WALNUT_LLM_PROVIDER": PROVIDER,
    }
    with pytest.raises(ValueError, match="WALNUT_LLM_RELAY_ENDPOINT is required"):
        RecoverableProviderSettings.from_env(direct_only_env)

    with pytest.raises(ValueError, match="chat-completions"):
        RecoverableProviderSettings(
            relay_endpoint="https://provider.example/v1/chat/completions",
            relay_api_key="relay-secret",
            model=MODEL,
            provider=PROVIDER,
        )

    settings = RecoverableProviderSettings(
        relay_endpoint="http://127.0.0.1:8792",
        relay_api_key="relay-secret",
        model=MODEL,
        provider=PROVIDER,
        allow_insecure_localhost=True,
        max_response_bytes=4096,
    )
    invalid = _CapabilityTransport(atomic=False)
    with pytest.raises(RelayCapabilityError):
        await create_recoverable_provider(settings, transport=cast(Any, invalid))
    assert invalid.calls == 1

    valid = _CapabilityTransport(atomic=True)
    adapter = await create_recoverable_provider(settings, transport=cast(Any, valid))
    assert isinstance(adapter, RecoverableOpenAIRelayAdapter)
    assert valid.calls == 1
