"""Claim-scoped recoverable LLM dispatch with immutable PostgreSQL receipts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaya_agent_contracts import (
    EvidenceRef,
    EvidenceType,
    Failure,
    FrozenJsonObject,
    LlmReply,
    LlmRequest,
    OperationContext,
    Result,
    Success,
)
from yaya_agent_runtime import (
    LlmDispatchIdentity,
    LlmDispatchResource,
    LlmRelayCapabilities,
    RecoverableLlmConflict,
    RecoverableLlmError,
    RecoverableLlmExpired,
    RecoverableLlmPort,
    RecoverableLlmProtocolError,
    RecoverableLlmUnavailable,
    llm_request_sha256,
    operation_context_sha256,
    provider_dispatch_id,
)

from .models import (
    JobStepReceiptRow,
    error_data,
    error_from_data,
    json_value,
    request_context_data,
)
from .workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
    WorkflowInvariantError,
    WorkflowReconciliationPending,
    workflow_receipt_sha256,
)

_RECEIPT_SCHEMA_VERSION = "2.0.0"


class DurableLlmDispatchUnknown(WorkflowReconciliationPending):
    """The relay cannot currently prove the state of one stable dispatch."""


class DurableLlmDispatchExpired(WorkflowInvariantError):
    """The relay discarded a result that durable workflow state still references."""


class DurableLlmDispatchAbsent(WorkflowInvariantError):
    """A same-ID PUT was still absent after one linearizable reconciliation."""


class DurableLlmDispatchPending(WorkflowReconciliationPending):
    """One stable dispatch exists but has not reached a terminal Provider state."""

    def __init__(self, dispatch_id: str, retry_after_seconds: int) -> None:
        super().__init__(
            f"provider dispatch {dispatch_id} is pending",
            retry_after_seconds=retry_after_seconds,
        )
        self.dispatch_id = dispatch_id


class DurableLlmReceiptCommitUnknown(WorkflowReconciliationPending):
    """A database commit acknowledgement was lost and no receipt can be reread."""


class PostgresDurableLlm:
    """Expose ``LlmPort.generate`` over a client-addressable relay resource."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        jobs: PostgresWorkflowJobStore,
        claim: ClaimedWorkflowJob,
        provider: object,
        lease_seconds: int,
        provider_name: str | None = None,
        model_version: str | None = None,
        max_calls: int = 3,
        receipt_namespace: str = "",
        ordinal_base: int = 0,
    ) -> None:
        if claim.operation != "EXECUTE_AGENT_TURN":
            raise ValueError("durable LLM requires one Agent Turn claim")
        if not 30 <= lease_seconds <= 3600:
            raise ValueError("LLM lease must be between 30 and 3600 seconds")
        if not isinstance(provider, RecoverableLlmPort):
            raise TypeError("production durable LLM requires RecoverableLlmPort")
        if not _bounded_text(provider_name) or not _bounded_text(model_version):
            raise ValueError("durable LLM provider and model must be explicitly configured")
        if isinstance(max_calls, bool) or not 1 <= max_calls <= 12:
            raise ValueError("durable LLM max_calls must be between 1 and 12")
        if receipt_namespace and (
            len(receipt_namespace) > 16
            or not receipt_namespace.replace("_", "").isalnum()
            or receipt_namespace.upper() != receipt_namespace
        ):
            raise ValueError("durable LLM receipt_namespace must be bounded uppercase text")
        if isinstance(ordinal_base, bool) or not 0 <= ordinal_base <= 980:
            raise ValueError("durable LLM ordinal_base must be between 0 and 980")
        if ordinal_base + max_calls > 999:
            raise ValueError("durable LLM effective ordinal exceeds the protocol bound")
        self._sessions = session_factory
        self._jobs = jobs
        self._claim = claim
        self._provider = provider
        self._lease_seconds = lease_seconds
        self._provider_name = cast(str, provider_name)
        self._model_version = cast(str, model_version)
        self._max_calls = max_calls
        self._receipt_prefix = f"{receipt_namespace}_" if receipt_namespace else ""
        self._ordinal_base = ordinal_base
        self._ordinal = 0

    async def generate(self, request: LlmRequest, context: OperationContext) -> Result[LlmReply]:
        _validate_context(self._claim, context)
        if request.versions.model_version != self._model_version:
            raise WorkflowInvariantError("provider model differs from LlmRequest authority")
        self._ordinal += 1
        if self._ordinal > self._max_calls:
            raise WorkflowInvariantError("Agent runtime exceeded its bounded provider calls")
        local_ordinal = self._ordinal
        ordinal = self._ordinal_base + local_ordinal
        dispatch_name = f"{self._receipt_prefix}PROVIDER_DISPATCH_{local_ordinal:02d}"
        result_name = f"{self._receipt_prefix}PROVIDER_RESULT_{local_ordinal:02d}"
        request_hash = llm_request_sha256(request)
        context_hash = operation_context_sha256(context)
        identity = LlmDispatchIdentity(
            dispatch_id=provider_dispatch_id(
                self._claim.tenant_id,
                self._claim.job_id,
                ordinal,
                request_hash,
            ),
            request_sha256=request_hash,
            context_sha256=context_hash,
            provider=self._provider_name,
            model=self._model_version,
        )
        dispatch_data = _dispatch_receipt_data(
            identity,
            ordinal,
            self._claim,
            context,
            request,
        )
        completed, dispatched = await self._read_receipts(result_name, dispatch_name)
        if completed is not None:
            if dispatched is None:
                raise WorkflowInvariantError("provider result exists without dispatch receipt")
            _validate_dispatch_receipt(dispatched, request_hash, dispatch_data)
            return await self._replay_terminal(
                completed,
                identity,
                request_hash,
                request,
                context,
            )
        if dispatched is not None:
            _validate_dispatch_receipt(dispatched, request_hash, dispatch_data)

        await self._validate_capabilities()
        dispatch_created = False
        if dispatched is None:
            dispatch_created = await self._record_dispatch(
                dispatch_name,
                request_hash,
                dispatch_data,
            )
            completed, dispatched = await self._read_receipts(result_name, dispatch_name)
            if completed is not None:
                return await self._replay_terminal(
                    completed,
                    identity,
                    request_hash,
                    request,
                    context,
                )
            if dispatched is None:
                raise WorkflowInvariantError("provider dispatch receipt disappeared")
            _validate_dispatch_receipt(dispatched, request_hash, dispatch_data)

        resource = (
            await self._provider_dispatch(identity, request, context)
            if dispatch_created
            else await self._provider_reconcile(identity, request, context)
        )
        if resource.state == "ABSENT":
            # A linearizable GET proves no generation exists.  Reissuing PUT
            # with the same immutable ID is safe and may create at most one.
            resource = await self._provider_dispatch(identity, request, context)
            if resource.state == "ABSENT":
                raise DurableLlmDispatchAbsent(
                    f"provider dispatch {identity.dispatch_id} remained absent after PUT"
                )
        if resource.state == "PENDING":
            retry_after = resource.retry_after_seconds
            if retry_after is None:
                raise WorkflowInvariantError("pending Provider resource has no retry delay")
            raise DurableLlmDispatchPending(identity.dispatch_id, retry_after)
        if resource.state not in {"SUCCEEDED", "FAILED"} or resource.result is None:
            raise WorkflowInvariantError("Provider relay returned a non-terminal result")
        _validate_terminal_resource(resource, identity)
        return await self._record_terminal(
            result_name,
            request_hash,
            identity,
            resource,
        )

    async def _replay_terminal(
        self,
        receipt: JobStepReceiptRow,
        identity: LlmDispatchIdentity,
        request_hash: str,
        request: LlmRequest,
        context: OperationContext,
    ) -> Result[LlmReply]:
        # PostgreSQL is durable workflow state, but it is not an independent
        # authority against coordinated JSON+digest corruption.  The relay's
        # immutable GET is the second authority for every terminal replay.
        await self._validate_capabilities()
        resource = await self._provider_reconcile(identity, request, context)
        if resource.state not in {"SUCCEEDED", "FAILED"} or resource.result is None:
            raise WorkflowInvariantError(
                "durable Provider result has no matching terminal relay resource"
            )
        _validate_terminal_resource(resource, identity)
        expected = _terminal_receipt_data(identity, resource)
        if receipt.input_sha256 != request_hash or receipt.receipt_json != expected:
            raise WorkflowInvariantError(
                "provider terminal receipt differs from immutable relay authority"
            )
        parsed = _result_from_receipt(receipt, identity, request_hash)
        if parsed != resource.result:
            raise WorkflowInvariantError("provider terminal Result differs from relay authority")
        return parsed

    async def _validate_capabilities(self) -> LlmRelayCapabilities:
        try:
            capabilities = await self._provider.validate_capabilities()
        except RecoverableLlmUnavailable as error:
            raise DurableLlmDispatchUnknown(
                "Provider relay capabilities are unavailable"
            ) from error
        except RecoverableLlmProtocolError as error:
            raise WorkflowInvariantError("Provider relay capabilities are invalid") from error
        if not isinstance(capabilities, LlmRelayCapabilities):
            raise WorkflowInvariantError("Provider returned invalid recovery capabilities")
        return capabilities

    async def _provider_dispatch(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource:
        try:
            resource = await self._provider.dispatch(identity, request, context)
        except RecoverableLlmExpired as error:
            raise DurableLlmDispatchExpired(
                f"provider dispatch {identity.dispatch_id} expired"
            ) from error
        except RecoverableLlmUnavailable as error:
            raise DurableLlmDispatchUnknown(
                f"provider dispatch {identity.dispatch_id} acknowledgement is unknown"
            ) from error
        except (RecoverableLlmConflict, RecoverableLlmProtocolError) as error:
            raise WorkflowInvariantError(
                "Provider dispatch identity or bytes conflicted"
            ) from error
        except RecoverableLlmError as error:
            raise WorkflowInvariantError("Provider recovery failed closed") from error
        return _validate_resource(resource, identity)

    async def _provider_reconcile(
        self,
        identity: LlmDispatchIdentity,
        request: LlmRequest,
        context: OperationContext,
    ) -> LlmDispatchResource:
        try:
            resource = await self._provider.reconcile(identity, request, context)
        except RecoverableLlmExpired as error:
            raise DurableLlmDispatchExpired(
                f"provider dispatch {identity.dispatch_id} expired"
            ) from error
        except RecoverableLlmUnavailable as error:
            raise DurableLlmDispatchUnknown(
                f"provider dispatch {identity.dispatch_id} cannot be reconciled"
            ) from error
        except (RecoverableLlmConflict, RecoverableLlmProtocolError) as error:
            raise WorkflowInvariantError(
                "Provider reconciliation violated immutable bytes"
            ) from error
        except RecoverableLlmError as error:
            raise WorkflowInvariantError("Provider reconciliation failed closed") from error
        return _validate_resource(resource, identity)

    async def _record_dispatch(
        self,
        step_name: str,
        request_hash: str,
        output: Mapping[str, Any],
    ) -> bool:
        created = False
        commit_error: Exception | None = None
        try:
            async with self._sessions() as session, session.begin():
                await self._jobs.start_step_in_session(
                    session,
                    self._claim,
                    phase=step_name,
                    lease_seconds=self._lease_seconds,
                )
                receipt = await self._jobs.record_step_in_session(
                    session,
                    self._claim,
                    step_name=step_name,
                    input_sha256=request_hash,
                    output=output,
                )
                created = receipt.created
        except Exception as error:
            commit_error = error
        persisted = await self._read_receipt(step_name)
        if persisted is not None:
            _validate_dispatch_receipt(persisted, request_hash, output)
            # After an acknowledgement loss, reconcile before any PUT.  A
            # confirmed ABSENT resource will safely return to the same-ID PUT.
            return created and commit_error is None
        if commit_error is not None:
            if not isinstance(
                commit_error,
                (ConnectionError, OSError, TimeoutError, SQLAlchemyError),
            ):
                raise commit_error
            raise DurableLlmReceiptCommitUnknown(
                f"database acknowledgement for {step_name} is unknown"
            ) from commit_error
        raise WorkflowInvariantError("provider dispatch commit did not materialize")

    async def _record_terminal(
        self,
        step_name: str,
        request_hash: str,
        identity: LlmDispatchIdentity,
        resource: LlmDispatchResource,
    ) -> Result[LlmReply]:
        output = _terminal_receipt_data(identity, resource)
        commit_error: Exception | None = None
        try:
            async with self._sessions() as session, session.begin():
                await self._jobs.start_step_in_session(
                    session,
                    self._claim,
                    phase=f"{step_name}_RECEIPT",
                    lease_seconds=self._lease_seconds,
                )
                await self._jobs.record_step_in_session(
                    session,
                    self._claim,
                    step_name=step_name,
                    input_sha256=request_hash,
                    output=output,
                )
        except Exception as error:
            commit_error = error
        persisted = await self._read_receipt(step_name)
        if persisted is not None:
            if persisted.input_sha256 != request_hash or persisted.receipt_json != output:
                raise WorkflowInvariantError(
                    "provider terminal commit differs from immutable relay result"
                )
            parsed = _result_from_receipt(persisted, identity, request_hash)
            if parsed != resource.result:
                raise WorkflowInvariantError("provider terminal receipt differs from relay result")
            return parsed
        if commit_error is not None:
            if not isinstance(
                commit_error,
                (ConnectionError, OSError, TimeoutError, SQLAlchemyError),
            ):
                raise commit_error
            raise DurableLlmReceiptCommitUnknown(
                f"database acknowledgement for {step_name} is unknown"
            ) from commit_error
        raise WorkflowInvariantError("provider terminal commit did not materialize")

    async def _read_receipts(
        self,
        result_name: str,
        dispatch_name: str,
    ) -> tuple[JobStepReceiptRow | None, JobStepReceiptRow | None]:
        async with self._sessions() as session:
            return (
                await _step(session, self._claim, result_name),
                await _step(session, self._claim, dispatch_name),
            )

    async def _read_receipt(self, name: str) -> JobStepReceiptRow | None:
        async with self._sessions() as session:
            return await _step(session, self._claim, name)


def _validate_context(claim: ClaimedWorkflowJob, context: OperationContext) -> None:
    """Bind every provider dispatch and replay to immutable Turn authority."""

    if context.command_id != claim.command_id:
        raise WorkflowInvariantError("provider context command does not match its claim")
    if context.actor.tenant_id != claim.tenant_id:
        raise WorkflowInvariantError("provider context tenant does not match its claim")
    expected = claim.job.get("request_context")
    if not isinstance(expected, Mapping) or dict(expected) != request_context_data(context):
        raise WorkflowInvariantError("provider context does not match durable Turn authority")


def _dispatch_receipt_data(
    identity: LlmDispatchIdentity,
    ordinal: int,
    claim: ClaimedWorkflowJob,
    context: OperationContext,
    request: LlmRequest,
) -> dict[str, Any]:
    return {
        "schema_version": _RECEIPT_SCHEMA_VERSION,
        "ordinal": ordinal,
        "dispatch_id": identity.dispatch_id,
        "request_sha256": identity.request_sha256,
        "context_sha256": identity.context_sha256,
        "provider": identity.provider,
        "model": identity.model,
        "command_id": context.command_id,
        "turn_id": claim.subject_id,
        "timeout_ms": request.timeout_ms,
    }


def _validate_dispatch_receipt(
    receipt: JobStepReceiptRow,
    request_hash: str,
    expected: Mapping[str, Any],
) -> None:
    _validate_receipt_hash(receipt)
    if receipt.input_sha256 != request_hash or receipt.receipt_json != dict(expected):
        raise WorkflowInvariantError("provider dispatch receipt differs from immutable authority")


def _terminal_receipt_data(
    identity: LlmDispatchIdentity,
    resource: LlmDispatchResource,
) -> dict[str, Any]:
    if resource.result is None:
        raise WorkflowInvariantError("terminal Provider resource has no Result")
    return {
        "schema_version": _RECEIPT_SCHEMA_VERSION,
        "dispatch": {
            "dispatch_id": identity.dispatch_id,
            "request_sha256": identity.request_sha256,
            "context_sha256": identity.context_sha256,
            "provider": identity.provider,
            "model": identity.model,
            "completion_sha256": resource.completion_sha256,
            "state": resource.state,
            "generation_count": resource.generation_count,
            "raw_response_sha256": resource.raw_response_sha256,
        },
        "result": _result_data(resource.result),
    }


def _result_from_receipt(
    receipt: JobStepReceiptRow,
    identity: LlmDispatchIdentity,
    request_hash: str,
) -> Result[LlmReply]:
    _validate_receipt_hash(receipt)
    if receipt.input_sha256 != request_hash:
        raise WorkflowInvariantError("provider result ordinal changed on replay")
    value = receipt.receipt_json
    if set(value) != {"schema_version", "dispatch", "result"}:
        raise WorkflowInvariantError("provider terminal receipt fields are not closed")
    if value.get("schema_version") != _RECEIPT_SCHEMA_VERSION:
        raise WorkflowInvariantError("provider receipt schema is unsupported")
    dispatch = _object(value.get("dispatch"), "provider dispatch authority")
    if set(dispatch) != {
        "dispatch_id",
        "request_sha256",
        "context_sha256",
        "provider",
        "model",
        "completion_sha256",
        "state",
        "generation_count",
        "raw_response_sha256",
    }:
        raise WorkflowInvariantError("provider terminal authority fields are not closed")
    expected = {
        "dispatch_id": identity.dispatch_id,
        "request_sha256": identity.request_sha256,
        "context_sha256": identity.context_sha256,
        "provider": identity.provider,
        "model": identity.model,
    }
    if any(dispatch.get(name) != item for name, item in expected.items()):
        raise WorkflowInvariantError("provider terminal receipt identity drifted")
    _require_sha256(dispatch.get("completion_sha256"), "completion_sha256")
    state = dispatch.get("state")
    if state not in {"SUCCEEDED", "FAILED"}:
        raise WorkflowInvariantError("provider terminal receipt state is invalid")
    generation_count = dispatch.get("generation_count")
    if isinstance(generation_count, bool) or not isinstance(generation_count, int):
        raise WorkflowInvariantError("provider generation_count must be an integer")
    if not 0 <= generation_count <= 1 or (state == "SUCCEEDED" and generation_count != 1):
        raise WorkflowInvariantError("provider generation_count exceeds exactly-once bounds")
    raw_hash = dispatch.get("raw_response_sha256")
    if state == "SUCCEEDED":
        _require_sha256(raw_hash, "raw_response_sha256")
    elif raw_hash is not None:
        raise WorkflowInvariantError("failed Provider receipt cannot contain response bytes")
    result = _result_from_data(_object(value.get("result"), "provider Result"))
    _validate_reply_authority(result, identity)
    if state == "FAILED" and not isinstance(result, Failure):
        raise WorkflowInvariantError("failed Provider state must contain Failure")
    return result


def _validate_receipt_hash(receipt: JobStepReceiptRow) -> None:
    if receipt.output_sha256 != workflow_receipt_sha256(receipt.receipt_json):
        raise WorkflowInvariantError("provider receipt JSON differs from its durable digest")


def _validate_resource(
    value: object,
    identity: LlmDispatchIdentity,
) -> LlmDispatchResource:
    if not isinstance(value, LlmDispatchResource):
        raise WorkflowInvariantError("Provider returned outside LlmDispatchResource")
    if value.identity != identity:
        raise WorkflowInvariantError("Provider resource identity drifted")
    return value


def _validate_terminal_resource(
    resource: LlmDispatchResource,
    identity: LlmDispatchIdentity,
) -> None:
    if resource.result is None:
        raise WorkflowInvariantError("terminal Provider resource has no Result")
    _validate_reply_authority(resource.result, identity)
    if resource.state == "FAILED" and not isinstance(resource.result, Failure):
        raise WorkflowInvariantError("failed Provider resource must contain Failure")
    if resource.state == "SUCCEEDED" and resource.raw_response_sha256 is None:
        raise WorkflowInvariantError("successful Provider resource must retain response bytes")


def _validate_reply_authority(
    result: Result[LlmReply],
    identity: LlmDispatchIdentity,
) -> None:
    if isinstance(result, Success):
        if not isinstance(result.value, LlmReply):
            raise WorkflowInvariantError("Provider returned outside Result[LlmReply]")
        if result.value.provider != identity.provider or result.value.model != identity.model:
            raise WorkflowInvariantError("Provider reply authority drifted")
        if (
            result.value.source != "provider"
            or result.value.degraded
            or result.value.fallback_reason is not None
        ):
            raise WorkflowInvariantError(
                "recoverable live Provider result must be source=provider and degraded=false"
            )
    elif not isinstance(result, Failure):
        raise WorkflowInvariantError("Provider returned outside Result[LlmReply]")


def _result_data(result: Result[LlmReply]) -> dict[str, Any]:
    if isinstance(result, Failure):
        return {
            "schema_version": "1.0.0",
            "outcome": "FAILURE",
            "error": error_data(result.error),
        }
    if not isinstance(result, Success) or not isinstance(result.value, LlmReply):
        raise WorkflowInvariantError("provider returned outside Result[LlmReply]")
    reply = result.value
    return {
        "schema_version": "1.0.0",
        "outcome": "SUCCESS",
        "reply": {
            "output": json_value(reply.output),
            "provider": reply.provider,
            "model": reply.model,
            "source": reply.source,
            "degraded": reply.degraded,
            "fallback_reason": reply.fallback_reason,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "evidence_refs": [_evidence_ref(item) for item in reply.evidence_refs],
        },
    }


def _result_from_data(value: Mapping[str, Any]) -> Result[LlmReply]:
    if value.get("schema_version") != "1.0.0":
        raise WorkflowInvariantError("provider Result schema is unsupported")
    if value.get("outcome") == "FAILURE":
        error = error_from_data(cast(dict[str, Any] | None, value.get("error")))
        if error is None:
            raise WorkflowInvariantError("provider failure receipt has no error")
        return Failure(error)
    if value.get("outcome") != "SUCCESS":
        raise WorkflowInvariantError("provider receipt outcome is invalid")
    raw = value.get("reply")
    if not isinstance(raw, Mapping):
        raise WorkflowInvariantError("provider success receipt has no reply")
    evidence = raw.get("evidence_refs")
    if not isinstance(evidence, list):
        raise WorkflowInvariantError("provider Evidence receipt is invalid")
    return Success(
        LlmReply(
            output=cast(FrozenJsonObject, _object(raw.get("output"), "provider output")),
            provider=_text(raw, "provider"),
            model=_text(raw, "model"),
            source=cast(Any, _text(raw, "source")),
            degraded=_boolean(raw, "degraded"),
            fallback_reason=cast(str | None, raw.get("fallback_reason")),
            input_tokens=_integer(raw, "input_tokens"),
            output_tokens=_integer(raw, "output_tokens"),
            evidence_refs=tuple(
                EvidenceRef(
                    evidence_id=_text(_object(item, "EvidenceRef"), "evidence_id"),
                    evidence_type=EvidenceType(
                        _text(_object(item, "EvidenceRef"), "evidence_type")
                    ),
                    created_at=_datetime(_text(_object(item, "EvidenceRef"), "created_at")),
                    sha256=cast(str | None, _object(item, "EvidenceRef").get("sha256")),
                    uri=cast(str | None, _object(item, "EvidenceRef").get("uri")),
                )
                for item in evidence
            ),
        )
    )


def validated_provider_result_data(value: Mapping[str, Any]) -> Result[LlmReply]:
    """Decode and round-trip one closed Provider Result receipt payload."""

    result = _result_from_data(value)
    if _result_data(result) != dict(value):
        raise WorkflowInvariantError("provider Result receipt is not canonical")
    return result


def validated_provider_terminal_receipt(
    receipt: JobStepReceiptRow,
) -> Result[LlmReply]:
    """Validate one stored terminal receipt without trusting its embedded identity."""

    envelope = receipt.receipt_json
    dispatch = _object(envelope.get("dispatch"), "provider dispatch authority")
    try:
        identity = LlmDispatchIdentity(
            dispatch_id=_text(dispatch, "dispatch_id"),
            request_sha256=_text(dispatch, "request_sha256"),
            context_sha256=_text(dispatch, "context_sha256"),
            provider=_text(dispatch, "provider"),
            model=_text(dispatch, "model"),
        )
    except ValueError as error:
        raise WorkflowInvariantError("provider terminal identity is invalid") from error
    result = _result_from_receipt(receipt, identity, identity.request_sha256)
    result_wire = _object(envelope.get("result"), "provider Result")
    if _result_data(result) != result_wire:
        raise WorkflowInvariantError("provider Result receipt is not canonical")
    return result


async def _step(
    session: AsyncSession, claim: ClaimedWorkflowJob, name: str
) -> JobStepReceiptRow | None:
    return await session.scalar(
        select(JobStepReceiptRow).where(
            JobStepReceiptRow.tenant_id == claim.tenant_id,
            JobStepReceiptRow.job_id == claim.job_id,
            JobStepReceiptRow.step_name == name,
        )
    )


def _evidence_ref(value: EvidenceRef) -> dict[str, object]:
    result: dict[str, object] = {
        "evidence_id": value.evidence_id,
        "evidence_type": value.evidence_type.value,
        "created_at": value.created_at.isoformat(),
    }
    if value.sha256 is not None:
        result["sha256"] = value.sha256
    if value.uri is not None:
        result["uri"] = value.uri
    return result


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowInvariantError(f"{label} must be an object")
    return dict(value)


def _text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise WorkflowInvariantError(f"{key} must be text")
    return item


def _integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise WorkflowInvariantError(f"{key} must be an integer")
    return item


def _boolean(value: Mapping[str, Any], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise WorkflowInvariantError(f"{key} must be a boolean")
    return item


def _datetime(value: str) -> Any:
    from datetime import datetime

    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise WorkflowInvariantError("provider Evidence timestamp has no offset")
    return result


def _require_sha256(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WorkflowInvariantError(f"provider {label} is not a SHA-256 digest")


def _bounded_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(ord(character) >= 0x20 and ord(character) != 0x7F for character in value)
    )


__all__ = [
    "DurableLlmDispatchAbsent",
    "DurableLlmDispatchExpired",
    "DurableLlmDispatchPending",
    "DurableLlmDispatchUnknown",
    "DurableLlmReceiptCommitUnknown",
    "PostgresDurableLlm",
    "validated_provider_result_data",
    "validated_provider_terminal_receipt",
]
