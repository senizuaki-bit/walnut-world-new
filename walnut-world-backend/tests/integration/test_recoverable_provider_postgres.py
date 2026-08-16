"""Fresh PostgreSQL proof for recoverable Provider response loss and takeover."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    CommandRecord,
    CommandStatus,
    ContentRef,
    FrozenJsonObject,
    LlmMessage,
    LlmRequest,
    OperationContext,
    RequestContext,
    Success,
    VersionSet,
)
from yaya_agent_runtime.adapters import (
    RecoverableOpenAIRelayAdapter,
    RecoverableOpenAIRelayConfig,
    UrllibRelayHttpTransport,
)

from walnut_backend.adapters.postgres.command_store import PostgresCommandStore
from walnut_backend.adapters.postgres.durable_llm import (
    DurableLlmDispatchUnknown,
    PostgresDurableLlm,
)
from walnut_backend.adapters.postgres.models import (
    CommandRow,
    JobStepReceiptRow,
    WorkflowJobRow,
    command_record_data,
    request_context_data,
)
from walnut_backend.adapters.postgres.session import (
    create_session_factory,
    normalize_database_url,
)
from walnut_backend.adapters.postgres.workflow_jobs import (
    ClaimedWorkflowJob,
    PostgresWorkflowJobStore,
    WorkflowInvariantError,
    workflow_receipt_sha256,
)
from walnut_backend.workers.workflow_worker import WorkflowWorker

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROVIDER = "fixture-relay-provider"
MODEL = "fixture-relay-model-v1"


@dataclass
class _RelayState:
    requests: dict[str, dict[str, object]] = field(default_factory=dict)
    resources: dict[str, dict[str, object]] = field(default_factory=dict)
    generation_count: dict[str, int] = field(default_factory=dict)
    capability_gets: int = 0
    dispatch_puts: int = 0
    dispatch_gets: int = 0
    drop_first_put_response: bool = True
    drop_first_dispatch_get: bool = True
    put_returns_pending: bool = False
    pending_gets_remaining: int = 0
    retry_after_seconds: int = 120
    lock: threading.Lock = field(default_factory=threading.Lock)


def test_fresh_postgres_takeover_recovers_lost_provider_response_once() -> None:
    base_url = _required_test_database_url()
    database_name = f"walnut_provider_{uuid4().hex[:20]}"
    normalized_base = make_url(normalize_database_url(base_url))
    target_url = normalized_base.set(database=database_name)
    asyncio.run(_create_database(normalized_base, database_name))
    try:
        _migrate(target_url)
        state = _RelayState()
        with _local_relay(state) as endpoint:
            asyncio.run(_exercise_takeover(target_url, endpoint, state))
        pending = _RelayState(
            drop_first_put_response=False,
            drop_first_dispatch_get=False,
            put_returns_pending=True,
            pending_gets_remaining=7,
            retry_after_seconds=120,
        )
        with _local_relay(pending) as endpoint:
            asyncio.run(_exercise_pending_retry_after(target_url, endpoint, pending))
    finally:
        asyncio.run(_drop_database(normalized_base, database_name))


async def _exercise_takeover(target_url: URL, endpoint: str, relay: _RelayState) -> None:
    database_url = target_url.render_as_string(hide_password=False)
    sessions = create_session_factory(database_url)
    jobs = PostgresWorkflowJobStore(sessions)
    context, request, claim = await _seed_claim(sessions, jobs)
    first_provider = _provider(endpoint)
    try:
        with pytest.raises(DurableLlmDispatchUnknown, match="acknowledgement is unknown"):
            await PostgresDurableLlm(
                session_factory=sessions,
                jobs=jobs,
                claim=claim,
                provider=first_provider,
                provider_name=PROVIDER,
                model_version=MODEL,
                lease_seconds=60,
            ).generate(request, context)

        async with sessions() as session:
            first_receipts = list(
                await session.scalars(
                    select(JobStepReceiptRow).where(JobStepReceiptRow.job_id == claim.job_id)
                )
            )
        assert [row.step_name for row in first_receipts] == ["PROVIDER_DISPATCH_01"]

        async with sessions() as session, session.begin():
            await session.execute(
                update(WorkflowJobRow)
                .where(WorkflowJobRow.job_id == claim.job_id)
                .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        takeover = await jobs.claim_next(
            tenant_id=claim.tenant_id,
            worker_id="worker_provider_restarted",
            lease_seconds=60,
            operation="EXECUTE_AGENT_TURN",
        )
        assert takeover is not None
        assert takeover.job_id == claim.job_id
        assert takeover.fencing_token == claim.fencing_token + 1

        ack_loss_sessions = _CommitAckLossSessionFactory(sessions)
        restarted_provider = _provider(endpoint)
        recovered = await PostgresDurableLlm(
            session_factory=cast(Any, ack_loss_sessions),
            jobs=jobs,
            claim=takeover,
            provider=restarted_provider,
            provider_name=PROVIDER,
            model_version=MODEL,
            lease_seconds=60,
        ).generate(request, context)
        assert isinstance(recovered, Success)
        assert recovered.value.output["decision"] == "invoke_skill"
        assert ack_loss_sessions.ack_was_lost

        async with sessions() as session:
            receipts = list(
                await session.scalars(
                    select(JobStepReceiptRow)
                    .where(JobStepReceiptRow.job_id == claim.job_id)
                    .order_by(JobStepReceiptRow.step_name)
                )
            )
        assert [row.step_name for row in receipts] == [
            "PROVIDER_DISPATCH_01",
            "PROVIDER_RESULT_01",
        ]
        assert receipts[0].fencing_token == claim.fencing_token
        assert receipts[1].fencing_token == takeover.fencing_token
        result_authority = cast(dict[str, Any], receipts[1].receipt_json["dispatch"])
        dispatch_id = str(result_authority["dispatch_id"])
        assert result_authority["generation_count"] == 1
        assert relay.generation_count == {dispatch_id: 1}
        assert relay.dispatch_puts == 1
        assert relay.dispatch_gets == 2

        original = copy.deepcopy(receipts[1].receipt_json)
        for _label, mutation in (
            ("completion", _tamper_completion_hash),
            ("result", _tamper_result_output),
            ("provider-bytes", _tamper_provider_response_hash),
        ):
            value = copy.deepcopy(original)
            mutation(value)
            await _replace_result_receipt(sessions, claim.job_id, value)
            with pytest.raises(
                WorkflowInvariantError,
                match="differs from immutable relay authority",
            ):
                await PostgresDurableLlm(
                    session_factory=sessions,
                    jobs=jobs,
                    claim=takeover,
                    provider=_provider(endpoint),
                    provider_name=PROVIDER,
                    model_version=MODEL,
                    lease_seconds=60,
                ).generate(request, context)
        await _replace_result_receipt(sessions, claim.job_id, original)

        # A third process revalidates the terminal PostgreSQL bytes against
        # the relay's immutable GET; neither authority is sufficient alone.
        replayed = await PostgresDurableLlm(
            session_factory=sessions,
            jobs=jobs,
            claim=takeover,
            provider=_provider(endpoint),
            provider_name=PROVIDER,
            model_version=MODEL,
            lease_seconds=60,
        ).generate(request, context)
        assert replayed == recovered
        assert relay.generation_count == {dispatch_id: 1}
        assert relay.dispatch_puts == 1
    finally:
        await sessions.kw["bind"].dispose()


async def _exercise_pending_retry_after(
    target_url: URL,
    endpoint: str,
    relay: _RelayState,
) -> None:
    database_url = target_url.render_as_string(hide_password=False)
    sessions = create_session_factory(database_url)
    jobs = PostgresWorkflowJobStore(sessions)
    context, request, tenant_value = await _seed_claim(sessions, jobs, worker_id=None)
    tenant_id = cast(str, tenant_value)
    try:
        await _run_pending_worker(
            sessions,
            jobs,
            endpoint,
            context,
            request,
            tenant_id,
            worker_id="worker_pending_first",
        )
        first = await _workflow_row(sessions, tenant_id)
        _assert_retry_wait(first, attempt=1, fencing_token=1, delay_seconds=120)

        # Simulate the database clock reaching Retry-After, followed by a
        # worker crash after claim but before it can reconcile the relay.
        await _make_retry_due(sessions, first.job_id)
        crashed = await jobs.claim_next(
            tenant_id=tenant_id,
            worker_id="worker_pending_crashed",
            lease_seconds=60,
            operation="EXECUTE_AGENT_TURN",
        )
        assert crashed is not None
        assert crashed.attempt == 2
        assert crashed.fencing_token == 2
        await _expire_claim(sessions, crashed)

        # Each subsequent call represents a fresh worker process and relay
        # adapter.  The expired lease is taken over with a new fence, while
        # every Provider reconciliation remains a GET for the stable ID.
        await _run_pending_worker(
            sessions,
            jobs,
            endpoint,
            context,
            request,
            tenant_id,
            worker_id="worker_pending_takeover",
        )
        third = await _workflow_row(sessions, tenant_id)
        _assert_retry_wait(third, attempt=3, fencing_token=3, delay_seconds=120)

        current = third
        for attempt in range(4, 10):
            await _make_retry_due(sessions, current.job_id)
            await _run_pending_worker(
                sessions,
                jobs,
                endpoint,
                context,
                request,
                tenant_id,
                worker_id=f"worker_pending_{attempt}",
            )
            current = await _workflow_row(sessions, tenant_id)
            _assert_retry_wait(
                current,
                attempt=attempt,
                fencing_token=attempt,
                delay_seconds=120,
            )

        await _make_retry_due(sessions, current.job_id)
        await _run_pending_worker(
            sessions,
            jobs,
            endpoint,
            context,
            request,
            tenant_id,
            worker_id="worker_pending_terminal",
        )
        terminal = await _workflow_row(sessions, tenant_id)
        assert terminal.status == "SUCCEEDED"
        assert terminal.attempt == 10
        assert terminal.fencing_token == 10
        assert terminal.lease_owner is None
        assert terminal.lease_expires_at is None
        assert terminal.next_attempt_at is None
        assert terminal.last_error_json is None

        async with sessions() as session:
            receipts = list(
                await session.scalars(
                    select(JobStepReceiptRow)
                    .where(JobStepReceiptRow.job_id == terminal.job_id)
                    .order_by(JobStepReceiptRow.step_name)
                )
            )
        assert [row.step_name for row in receipts] == [
            "PROVIDER_DISPATCH_01",
            "PROVIDER_RESULT_01",
            "WORKER_RECONCILE_1",
            "WORKER_RECONCILE_3",
            "WORKER_RECONCILE_4",
            "WORKER_RECONCILE_5",
            "WORKER_RECONCILE_6",
            "WORKER_RECONCILE_7",
            "WORKER_RECONCILE_8",
            "WORKER_RECONCILE_9",
        ]
        result_receipt = next(row for row in receipts if row.step_name == "PROVIDER_RESULT_01")
        result_authority = cast(dict[str, Any], result_receipt.receipt_json["dispatch"])
        dispatch_id = str(result_authority["dispatch_id"])
        assert result_authority["generation_count"] == 1
        assert relay.generation_count == {dispatch_id: 1}
        assert relay.dispatch_puts == 1
        assert relay.dispatch_gets == 8
    finally:
        await sessions.kw["bind"].dispose()


class _PendingProviderHandler:
    operations = frozenset({"EXECUTE_AGENT_TURN"})

    def __init__(
        self,
        sessions: Any,
        jobs: PostgresWorkflowJobStore,
        endpoint: str,
        context: OperationContext,
        request: LlmRequest,
    ) -> None:
        self._sessions = sessions
        self._jobs = jobs
        self._provider = _provider(endpoint)
        self._context = context
        self._request = request

    async def execute(self, claim: ClaimedWorkflowJob) -> None:
        recovered = await PostgresDurableLlm(
            session_factory=self._sessions,
            jobs=self._jobs,
            claim=claim,
            provider=self._provider,
            provider_name=PROVIDER,
            model_version=MODEL,
            lease_seconds=60,
        ).generate(self._request, self._context)
        assert isinstance(recovered, Success)
        assert recovered.value.output["decision"] == "invoke_skill"
        async with self._sessions() as session, session.begin():
            await self._jobs.finish_in_session(
                session,
                claim,
                status="SUCCEEDED",
                phase="COMPLETE",
            )


async def _run_pending_worker(
    sessions: Any,
    jobs: PostgresWorkflowJobStore,
    endpoint: str,
    context: OperationContext,
    request: LlmRequest,
    tenant_id: str,
    *,
    worker_id: str,
) -> None:
    worker = WorkflowWorker(
        session_factory=sessions,
        jobs=jobs,
        commands=PostgresCommandStore(sessions),
        handlers=(_PendingProviderHandler(sessions, jobs, endpoint, context, request),),
        worker_id=worker_id,
        lease_seconds=60,
        maximum_attempts=5,
        retry_base_seconds=2,
        retry_max_seconds=60,
    )
    assert await worker.run_once(tenant_id)


async def _workflow_row(sessions: Any, tenant_id: str) -> WorkflowJobRow:
    async with sessions() as session:
        row = await session.scalar(
            select(WorkflowJobRow).where(WorkflowJobRow.tenant_id == tenant_id)
        )
        assert row is not None
        return row


def _assert_retry_wait(
    row: WorkflowJobRow,
    *,
    attempt: int,
    fencing_token: int,
    delay_seconds: int,
) -> None:
    assert row.status == "RETRY_WAIT"
    assert row.attempt == attempt
    assert row.fencing_token == fencing_token
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert row.next_attempt_at is not None
    assert row.updated_at is not None
    assert (row.next_attempt_at - row.updated_at).total_seconds() == pytest.approx(
        delay_seconds,
        abs=0.001,
    )
    assert row.last_error_json == {
        "code": "WORKFLOW_EXECUTION_FAILED",
        "exception_type": "DurableLlmDispatchPending",
        "attempt": attempt,
        "retry_after_seconds": delay_seconds,
    }


async def _make_retry_due(sessions: Any, job_id: str) -> None:
    async with sessions() as session, session.begin():
        await session.execute(
            update(WorkflowJobRow)
            .where(WorkflowJobRow.job_id == job_id)
            .values(next_attempt_at=func.clock_timestamp() - timedelta(seconds=1))
        )


async def _expire_claim(sessions: Any, claim: ClaimedWorkflowJob) -> None:
    async with sessions() as session, session.begin():
        await session.execute(
            update(WorkflowJobRow)
            .where(
                WorkflowJobRow.job_id == claim.job_id,
                WorkflowJobRow.fencing_token == claim.fencing_token,
            )
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )


async def _replace_result_receipt(
    sessions: Any,
    job_id: str,
    value: dict[str, Any],
) -> None:
    async with sessions() as session, session.begin():
        row = await session.scalar(
            select(JobStepReceiptRow).where(
                JobStepReceiptRow.job_id == job_id,
                JobStepReceiptRow.step_name == "PROVIDER_RESULT_01",
            )
        )
        assert row is not None
        row.receipt_json = value
        row.output_sha256 = workflow_receipt_sha256(value)


def _tamper_completion_hash(value: dict[str, Any]) -> None:
    dispatch = cast(dict[str, Any], value["dispatch"])
    dispatch["completion_sha256"] = "e" * 64


def _tamper_result_output(value: dict[str, Any]) -> None:
    result = cast(dict[str, Any], value["result"])
    reply = cast(dict[str, Any], result["reply"])
    reply["output"] = {"decision": "tampered"}


def _tamper_provider_response_hash(value: dict[str, Any]) -> None:
    dispatch = cast(dict[str, Any], value["dispatch"])
    dispatch["raw_response_sha256"] = "f" * 64


async def _seed_claim(
    sessions: Any,
    jobs: PostgresWorkflowJobStore,
    *,
    worker_id: str | None = "worker_provider_first",
) -> tuple[OperationContext, LlmRequest, Any]:
    suffix = uuid4().hex[:20]
    tenant_id = f"tenant_provider_{suffix}"
    actor_id = f"student_provider_{suffix}"
    command_id = f"cmd_provider_{suffix}"
    turn_id = f"turn_provider_{suffix}"
    job_hash = hashlib.sha256(f"provider-job:{suffix}".encode()).hexdigest()
    async with sessions() as session, session.begin():
        now = await session.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime) and now.tzinfo is not None
        actor = ActorRef(tenant_id, actor_id, ActorType.STUDENT, ("game:player",))
        content = ContentRef(f"UNIT_PROVIDER_{suffix.upper()}", "1.0.0", "a" * 64)
        origin = RequestContext(
            request_id=f"req_provider_{suffix}",
            correlation_id=f"corr_provider_{suffix}",
            trace_id=f"trace_provider_{suffix}",
            requested_at=now,
            actor=actor,
            content_ref=content,
        )
        context = OperationContext(
            request_id=origin.request_id,
            correlation_id=origin.correlation_id,
            trace_id=origin.trace_id,
            requested_at=origin.requested_at,
            actor=origin.actor,
            content_ref=origin.content_ref,
            command_id=command_id,
            causation_id=None,
        )
        versions = VersionSet(
            "1.0.0",
            "1",
            "provider-recovery-v1",
            "world-v1",
            "teaching-v1",
            prompt_version="prompt-provider-v1",
            model_version=MODEL,
        )
        command = CommandRecord(
            request_context=origin,
            command_id=command_id,
            command_type="EXECUTE_AGENT_TURN",
            status=CommandStatus.ACCEPTED,
            stage="ACCEPT",
            terminal=False,
            accepted_at=now,
            updated_at=now,
            result=None,
            error=None,
            evidence_refs=(),
            versions=versions,
            links=cast(FrozenJsonObject, {"self": f"/v1/commands/{command_id}"}),
        )
        request = LlmRequest(
            messages=(
                LlmMessage("system", "Return strict JSON."),
                LlmMessage("user", "recover after response loss"),
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
            versions=versions,
        )
        session.add(
            CommandRow(
                command_id=command.command_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                command_type=command.command_type,
                status=command.status.value,
                revision=command.revision,
                terminal=command.terminal,
                accepted_at=now,
                updated_at=now,
                record_json=command_record_data(command),
            )
        )
        await session.flush()
        await jobs.enqueue_in_session(
            session,
            tenant_id=tenant_id,
            command_id=command_id,
            operation="EXECUTE_AGENT_TURN",
            subject_type="AGENT_TURN",
            subject_id=turn_id,
            request_sha256=job_hash,
            job={
                "schema_version": "1.0.0",
                "turn_id": turn_id,
                "request_context": request_context_data(context),
            },
        )
    if worker_id is None:
        return context, request, tenant_id
    claim = await jobs.claim_next(
        tenant_id=tenant_id,
        worker_id=worker_id,
        lease_seconds=60,
        operation="EXECUTE_AGENT_TURN",
    )
    assert claim is not None
    return context, request, claim


def _provider(endpoint: str) -> RecoverableOpenAIRelayAdapter:
    return RecoverableOpenAIRelayAdapter(
        RecoverableOpenAIRelayConfig(
            relay_endpoint=endpoint,
            api_key="fixture-relay-secret",
            model=MODEL,
            provider=PROVIDER,
            response_format="json_schema",
            allow_insecure_localhost=True,
            max_response_bytes=4096,
        ),
        UrllibRelayHttpTransport(max_response_bytes=65_536),
    )


@contextmanager
def _local_relay(state: _RelayState) -> Generator[str, None, None]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/llm/capabilities":
                with state.lock:
                    state.capability_gets += 1
                self._send(
                    200,
                    {
                        "schema_version": "1.0.0",
                        "protocol": "YAYA_RECOVERABLE_LLM_V1",
                        "result_retention_seconds": 604_800,
                        "max_request_bytes": 4_194_304,
                        "max_response_bytes": 4_194_304,
                        "atomic_put_by_dispatch_id": True,
                        "linearizable_get": True,
                        "immutable_request_hash": True,
                        "max_generation_count": 1,
                    },
                )
                return
            prefix = "/v1/llm/dispatches/"
            if not self.path.startswith(prefix):
                self._send(404, {"code": "NOT_FOUND"})
                return
            dispatch_id = self.path[len(prefix) :]
            with state.lock:
                state.dispatch_gets += 1
                drop = state.drop_first_dispatch_get
                state.drop_first_dispatch_get = False
                resource = copy.deepcopy(state.resources.get(dispatch_id))
                pending = resource is not None and state.pending_gets_remaining > 0
                if pending:
                    state.pending_gets_remaining -= 1
            if drop:
                self._drop()
                return
            if resource is None:
                self._send(
                    404,
                    {
                        "schema_version": "1.0.0",
                        "code": "DISPATCH_NOT_FOUND",
                        "dispatch_id": dispatch_id,
                    },
                )
                return
            resource["replayed"] = True
            if pending:
                self._send(
                    202,
                    _pending_resource(resource),
                    headers={"Retry-After": str(state.retry_after_seconds)},
                )
                return
            self._send(200, resource)

        def do_PUT(self) -> None:  # noqa: N802
            prefix = "/v1/llm/dispatches/"
            if not self.path.startswith(prefix):
                self._send(404, {"code": "NOT_FOUND"})
                return
            length = int(self.headers.get("content-length", "0"))
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                self._send(400, {"code": "INVALID_BODY"})
                return
            request = cast(dict[str, object], value)
            dispatch_id = self.path[len(prefix) :]
            if request.get("dispatch_id") != dispatch_id:
                self._send(409, {"code": "IDENTITY_CONFLICT"})
                return
            with state.lock:
                state.dispatch_puts += 1
                existing = state.requests.get(dispatch_id)
                if existing is not None and existing != request:
                    self._send(409, {"code": "IDENTITY_CONFLICT"})
                    return
                if existing is None:
                    state.requests[dispatch_id] = copy.deepcopy(request)
                    state.generation_count[dispatch_id] = 1
                    state.resources[dispatch_id] = _resource(request)
                resource = copy.deepcopy(state.resources[dispatch_id])
                drop = state.drop_first_put_response
                state.drop_first_put_response = False
                pending = state.put_returns_pending
                state.put_returns_pending = False
            if drop:
                self._drop()
                return
            resource["replayed"] = existing is not None
            if pending:
                self._send(
                    202,
                    _pending_resource(resource),
                    headers={"Retry-After": str(state.retry_after_seconds)},
                )
                return
            self._send(200 if existing is not None else 201, resource)

        def _send(
            self,
            status: int,
            value: Mapping[str, object],
            *,
            headers: Mapping[str, str] | None = None,
        ) -> None:
            body = json.dumps(value, separators=(",", ":")).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for name, header_value in (headers or {}).items():
                    self.send_header(name, header_value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def _drop(self) -> None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            self.close_connection = True

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("fixture relay did not stop")


def _resource(request: Mapping[str, object]) -> dict[str, object]:
    provider_body = json.dumps(
        {
            "id": "completion_provider_recovery",
            "model": MODEL,
            "choices": [
                {"message": {"role": "assistant", "content": '{"decision":"invoke_skill"}'}}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        },
        separators=(",", ":"),
    ).encode()
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "1.0.0",
        "dispatch_id": request["dispatch_id"],
        "request_sha256": request["request_sha256"],
        "context_sha256": request["context_sha256"],
        "completion_sha256": request["completion_sha256"],
        "provider": request["provider"],
        "model": request["model"],
        "state": "SUCCEEDED",
        "generation_count": 1,
        "replayed": False,
        "created_at": now,
        "updated_at": now,
        "provider_response": {
            "http_status": 200,
            "content_type": "application/json; charset=utf-8",
            "body_base64": base64.b64encode(provider_body).decode(),
            "body_sha256": hashlib.sha256(provider_body).hexdigest(),
        },
    }


def _pending_resource(resource: Mapping[str, object]) -> dict[str, object]:
    value = copy.deepcopy(dict(resource))
    value["state"] = "PENDING"
    value.pop("provider_response", None)
    value.pop("failure", None)
    return value


class _CommitAckLossSessionFactory:
    def __init__(self, sessions: Any) -> None:
        self._sessions = sessions
        self.ack_was_lost = False

    def __call__(self) -> _CommitAckLossSession:
        return _CommitAckLossSession(self, self._sessions())


class _CommitAckLossSession:
    def __init__(self, owner: _CommitAckLossSessionFactory, context: Any) -> None:
        self._owner = owner
        self._context = context
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> _CommitAckLossSession:
        self._session = await self._context.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> object:
        return await self._context.__aexit__(*args)

    def begin(self) -> _CommitAckLossTransaction:
        assert self._session is not None
        return _CommitAckLossTransaction(self._owner, self._session.begin())

    def __getattr__(self, name: str) -> Any:
        if self._session is None:
            raise AttributeError(name)
        return getattr(self._session, name)


class _CommitAckLossTransaction:
    def __init__(self, owner: _CommitAckLossSessionFactory, transaction: Any) -> None:
        self._owner = owner
        self._transaction = transaction

    async def __aenter__(self) -> object:
        return await self._transaction.__aenter__()

    async def __aexit__(self, *args: object) -> object:
        result = await self._transaction.__aexit__(*args)
        if args[0] is None and not self._owner.ack_was_lost:
            self._owner.ack_was_lost = True
            raise ConnectionError("PostgreSQL commit acknowledgement lost")
        return result


def _required_test_database_url() -> str:
    value = os.getenv("WALNUT_TEST_DATABASE_URL")
    if value is None:
        raise RuntimeError(
            "set WALNUT_TEST_DATABASE_URL for required recoverable Provider PostgreSQL coverage"
        )
    return value


def _migrate(target_url: URL) -> None:
    environment = dict(os.environ)
    environment["WALNUT_DATABASE_URL"] = target_url.render_as_string(hide_password=False)
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr


async def _create_database(base_url: URL, database_name: str) -> None:
    _assert_scratch_database_name(database_name)
    engine = create_async_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}" TEMPLATE template0'))
    finally:
        await engine.dispose()


async def _drop_database(base_url: URL, database_name: str) -> None:
    _assert_scratch_database_name(database_name)
    engine = create_async_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:database_name AND pid<>pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    finally:
        await engine.dispose()


def _assert_scratch_database_name(value: str) -> None:
    if re.fullmatch(r"walnut_provider_[a-f0-9]{20}", value) is None:
        raise AssertionError("refusing to mutate a non-scratch Provider database")
