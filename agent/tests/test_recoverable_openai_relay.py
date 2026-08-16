from __future__ import annotations

import base64
import copy
import hashlib
import json
import socket
import sys
import threading
import unittest
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PACKAGE_ROOT))

from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    ActorType,
    ContentRef,
    Failure,
    FrozenJsonObject,
    LlmMessage,
    LlmRequest,
    OperationContext,
    Success,
    VersionSet,
)
from yaya_agent_runtime import (  # noqa: E402
    LlmDispatchIdentity,
    RecoverableLlmPort,
    llm_recovery_sha256,
    llm_request_sha256,
    operation_context_sha256,
    provider_dispatch_id,
)
from yaya_agent_runtime.adapters import (  # noqa: E402
    OpenAICompatibleConfig,
    OpenAICompatibleLlmAdapter,
    RecoverableOpenAIRelayAdapter,
    RecoverableOpenAIRelayConfig,
    RelayCapabilityError,
    RelayConflictError,
    RelayDependencyUnavailable,
    RelayProtocolError,
    RelayResultExpired,
    UrllibRelayHttpTransport,
)

NOW = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)


def operation_context() -> OperationContext:
    return OperationContext(
        request_id="req_relay_contract_0001",
        correlation_id="corr_relay_contract_0001",
        trace_id="trace_relay_contract_0001",
        requested_at=NOW,
        actor=ActorRef(
            "tenant_yaya",
            "student_relay_0001",
            ActorType.STUDENT,
            ("game:player",),
        ),
        content_ref=ContentRef("YAYA_FARM_001", "1.0.0", "a" * 64),
        command_id="cmd_relay_contract_0001",
        causation_id=None,
    )


def llm_request() -> LlmRequest:
    return LlmRequest(
        messages=(
            LlmMessage("system", "Return strict JSON."),
            LlmMessage("user", "recover this request"),
        ),
        output_schema=cast(
            FrozenJsonObject,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ("message",),
                "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 100}},
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
            model_version="configured-model-v1",
        ),
    )


def dispatch_identity(
    request: LlmRequest | None = None,
    context: OperationContext | None = None,
    *,
    job_id: str = "job_relay_contract_0001",
) -> LlmDispatchIdentity:
    request = request or llm_request()
    context = context or operation_context()
    request_sha256 = llm_request_sha256(request)
    return LlmDispatchIdentity(
        dispatch_id=provider_dispatch_id(
            context.actor.tenant_id,
            job_id,
            1,
            request_sha256,
        ),
        request_sha256=request_sha256,
        context_sha256=operation_context_sha256(context),
        provider="fixture-provider",
        model="configured-model-v1",
    )


def provider_bytes(content: str = '{"message":"recovered"}') -> bytes:
    return json.dumps(
        {
            "id": "completion_fixture_0001",
            "model": "configured-model-v1",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass
class RelayFixtureState:
    capabilities: dict[str, object] = field(
        default_factory=lambda: {
            "schema_version": "1.0.0",
            "protocol": "YAYA_RECOVERABLE_LLM_V1",
            "result_retention_seconds": 604_800,
            "max_request_bytes": 4_194_304,
            "max_response_bytes": 4_194_304,
            "atomic_put_by_dispatch_id": True,
            "linearizable_get": True,
            "immutable_request_hash": True,
            "max_generation_count": 1,
        }
    )
    capability_status: int = 200
    response_content_type: str = "application/json"
    put_status: int | None = None
    get_status: int | None = None
    first_state: str = "SUCCEEDED"
    drop_first_put_response: bool = False
    provider_body: bytes = field(default_factory=provider_bytes)
    mutate_resource: Callable[[dict[str, object]], None] | None = None
    resources: dict[str, dict[str, object]] = field(default_factory=lambda: {})
    requests: dict[str, dict[str, object]] = field(default_factory=lambda: {})
    generation_count: dict[str, int] = field(default_factory=lambda: {})
    capability_gets: int = 0
    dispatch_puts: int = 0
    reconcile_gets: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


@contextmanager
def local_relay(state: RelayFixtureState) -> Generator[str, None, None]:
    class RelayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/llm/capabilities":
                with state.lock:
                    state.capability_gets += 1
                    status = state.capability_status
                    value = copy.deepcopy(state.capabilities)
                self._send(status, value)
                return
            prefix = "/v1/llm/dispatches/"
            if not self.path.startswith(prefix):
                self._send(404, {"code": "NOT_FOUND"})
                return
            dispatch_id = self.path[len(prefix) :]
            with state.lock:
                state.reconcile_gets += 1
                status_override = state.get_status
                resource = copy.deepcopy(state.resources.get(dispatch_id))
                mutator = state.mutate_resource
            if status_override == 404 or (status_override is None and resource is None):
                self._send(
                    404,
                    {
                        "schema_version": "1.0.0",
                        "code": "DISPATCH_NOT_FOUND",
                        "dispatch_id": dispatch_id,
                    },
                )
                return
            if status_override in {410, 503}:
                self._send(status_override, {"code": f"HTTP_{status_override}"})
                return
            if resource is None:
                raise AssertionError("fixture override requires one resource")
            if status_override == 202:
                resource["state"] = "PENDING"
                resource.pop("provider_response", None)
                resource.pop("failure", None)
                if mutator is not None:
                    mutator(resource)
                self._send(202, resource, {"Retry-After": "2"})
                return
            if mutator is not None:
                mutator(resource)
            self._send(200, resource)

        def do_PUT(self) -> None:  # noqa: N802
            prefix = "/v1/llm/dispatches/"
            if not self.path.startswith(prefix):
                self._send(404, {"code": "NOT_FOUND"})
                return
            length = int(self.headers.get("content-length", "0"))
            decoded = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(decoded, dict):
                self._send(400, {"code": "INVALID_BODY"})
                return
            value = cast(dict[str, object], decoded)
            dispatch_id = self.path[len(prefix) :]
            with state.lock:
                state.dispatch_puts += 1
                status_override = state.put_status
                existing_request = state.requests.get(dispatch_id)
                if status_override is None and existing_request is not None:
                    if existing_request != value:
                        status_override = 409
                        resource = None
                    else:
                        resource = copy.deepcopy(state.resources[dispatch_id])
                        resource["replayed"] = True
                elif status_override is None:
                    state.requests[dispatch_id] = copy.deepcopy(value)
                    state.generation_count[dispatch_id] = 1
                    resource = _resource(value, state)
                    state.resources[dispatch_id] = copy.deepcopy(resource)
                else:
                    resource = None
                drop = state.drop_first_put_response and state.dispatch_puts == 1
                mutator = state.mutate_resource
            if status_override in {409, 503}:
                self._send(status_override, {"code": f"HTTP_{status_override}"})
                return
            if resource is None:
                raise AssertionError("fixture failed to materialize a resource")
            if mutator is not None:
                mutator(resource)
            if drop:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                self.close_connection = True
                return
            if resource["state"] == "PENDING":
                self._send(201 if existing_request is None else 202, resource, {"Retry-After": "2"})
            else:
                self._send(201 if existing_request is None else 200, resource)

        def _send(
            self,
            status: int,
            value: Mapping[str, object],
            headers: Mapping[str, str] | None = None,
        ) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", state.response_content_type)
                self.send_header("Content-Length", str(len(body)))
                for name, header_value in (headers or {}).items():
                    self.send_header(name, header_value)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host = str(server.server_address[0])
        port = int(server.server_address[1])
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("recoverable relay fixture did not stop")


def _resource(request: Mapping[str, object], state: RelayFixtureState) -> dict[str, object]:
    now = NOW.isoformat().replace("+00:00", "Z")
    resource: dict[str, object] = {
        "schema_version": "1.0.0",
        "dispatch_id": request["dispatch_id"],
        "request_sha256": request["request_sha256"],
        "context_sha256": request["context_sha256"],
        "completion_sha256": request["completion_sha256"],
        "provider": request["provider"],
        "model": request["model"],
        "state": state.first_state,
        "generation_count": 1,
        "replayed": False,
        "created_at": now,
        "updated_at": now,
    }
    if state.first_state == "SUCCEEDED":
        resource["provider_response"] = {
            "http_status": 200,
            "content_type": "application/json; charset=utf-8",
            "body_base64": base64.b64encode(state.provider_body).decode("ascii"),
            "body_sha256": hashlib.sha256(state.provider_body).hexdigest(),
        }
    elif state.first_state == "FAILED":
        resource["failure"] = {"code": "UPSTREAM_REJECTED", "retryable": False}
    elif state.first_state != "PENDING":
        raise AssertionError("fixture first_state is invalid")
    return resource


def adapter(endpoint: str, **changes: object) -> RecoverableOpenAIRelayAdapter:
    values: dict[str, object] = {
        "relay_endpoint": endpoint,
        "api_key": "relay-secret-for-tests",
        "model": "configured-model-v1",
        "provider": "fixture-provider",
        "response_format": "json_schema",
        "allow_insecure_localhost": True,
        "required_retention_seconds": 604_800,
        "max_response_bytes": 4096,
    }
    values.update(changes)
    config = RecoverableOpenAIRelayConfig(**values)  # type: ignore[arg-type]
    return RecoverableOpenAIRelayAdapter(
        config,
        UrllibRelayHttpTransport(max_response_bytes=65_536),
    )


class RecoverableOpenAIRelayContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_put_response_loss_gets_terminal_and_exact_replay_generates_once(self) -> None:
        state = RelayFixtureState(drop_first_put_response=True)
        request = llm_request()
        context = operation_context()
        identity = dispatch_identity(request, context)
        with local_relay(state) as endpoint:
            client = adapter(endpoint)
            recovered = await client.dispatch(identity, request, context)
            replay = await client.dispatch(identity, request, context)

        self.assertEqual(recovered.state, "SUCCEEDED")
        self.assertIsInstance(recovered.result, Success)
        assert isinstance(recovered.result, Success)
        self.assertEqual(recovered.result.value.output["message"], "recovered")
        self.assertEqual(replay.result, recovered.result)
        self.assertTrue(replay.replayed)
        self.assertEqual(state.generation_count[identity.dispatch_id], 1)
        self.assertEqual(state.dispatch_puts, 2)
        self.assertEqual(state.reconcile_gets, 1)
        self.assertEqual(state.capability_gets, 1)
        relay_request = state.requests[identity.dispatch_id]
        completion = relay_request["completion"]
        self.assertIsInstance(completion, dict)
        completion_value = cast(dict[str, object], completion)
        self.assertNotIn("n", completion_value)
        self.assertEqual(
            relay_request["completion_sha256"],
            llm_recovery_sha256(
                {
                    "schema_version": "1.0.0",
                    "provider": "fixture-provider",
                    "model": "configured-model-v1",
                    "completion": completion_value,
                }
            ),
        )
        self.assertIsInstance(client, RecoverableLlmPort)

        direct = OpenAICompatibleLlmAdapter(
            OpenAICompatibleConfig(
                endpoint="https://provider.example/v1/chat/completions",
                api_key="direct-secret-for-tests",
                model="configured-model-v1",
                provider="fixture-provider",
            ),
            object(),  # type: ignore[arg-type]
        )
        self.assertNotIsInstance(direct, RecoverableLlmPort)

    async def test_identity_and_context_drift_fail_before_dispatch(self) -> None:
        state = RelayFixtureState()
        request = llm_request()
        context = operation_context()
        identity = dispatch_identity(request, context)
        cases = (
            replace(identity, request_sha256="b" * 64),
            replace(identity, context_sha256="c" * 64),
            replace(identity, provider="other-provider"),
            replace(identity, model="other-model"),
        )
        with local_relay(state) as endpoint:
            client = adapter(endpoint)
            for drifted in cases:
                with self.subTest(identity=drifted):
                    with self.assertRaises(RelayProtocolError):
                        await client.dispatch(drifted, request, context)
            changed_context = replace(context, command_id="cmd_relay_contract_changed")
            with self.assertRaises(RelayProtocolError):
                await client.dispatch(identity, request, changed_context)

        self.assertEqual(state.dispatch_puts, 0)

    async def test_pending_absent_conflict_expired_and_unavailable_statuses(self) -> None:
        request = llm_request()
        context = operation_context()

        pending_state = RelayFixtureState(first_state="PENDING")
        pending_identity = dispatch_identity(request, context, job_id="job_relay_pending_0001")
        with local_relay(pending_state) as endpoint:
            client = adapter(endpoint)
            pending = await client.dispatch(pending_identity, request, context)
            pending_state.get_status = 202
            reconciled = await client.reconcile(pending_identity, request, context)
        self.assertEqual((pending.state, reconciled.state), ("PENDING", "PENDING"))
        self.assertEqual(reconciled.retry_after_seconds, 2)
        self.assertEqual(pending_state.generation_count[pending_identity.dispatch_id], 1)

        absent_state = RelayFixtureState(get_status=404)
        absent_identity = dispatch_identity(request, context, job_id="job_relay_absent_0001")
        with local_relay(absent_state) as endpoint:
            absent = await adapter(endpoint).reconcile(absent_identity, request, context)
        self.assertEqual(absent.state, "ABSENT")
        self.assertEqual(absent.generation_count, 0)

        for status, expected in (
            (410, RelayResultExpired),
            (503, RelayDependencyUnavailable),
        ):
            status_state = RelayFixtureState(get_status=status)
            status_identity = dispatch_identity(
                request,
                context,
                job_id=f"job_relay_status_{status}",
            )
            with self.subTest(status=status), local_relay(status_state) as endpoint:
                with self.assertRaises(expected):
                    await adapter(endpoint).reconcile(status_identity, request, context)

        conflict_state = RelayFixtureState(put_status=409)
        conflict_identity = dispatch_identity(request, context, job_id="job_relay_conflict_0001")
        with local_relay(conflict_state) as endpoint:
            with self.assertRaises(RelayConflictError):
                await adapter(endpoint).dispatch(conflict_identity, request, context)
        self.assertEqual(conflict_state.generation_count, {})

    async def test_resource_identity_drift_and_corrupt_raw_bytes_fail_closed(self) -> None:
        request = llm_request()
        context = operation_context()
        mutations: tuple[tuple[str, Callable[[dict[str, object]], None]], ...] = (
            ("dispatch_id", lambda value: value.__setitem__("dispatch_id", "llmdsp_" + "f" * 40)),
            ("request_sha256", lambda value: value.__setitem__("request_sha256", "b" * 64)),
            ("context_sha256", lambda value: value.__setitem__("context_sha256", "c" * 64)),
            ("completion_sha256", lambda value: value.__setitem__("completion_sha256", "d" * 64)),
            ("provider", lambda value: value.__setitem__("provider", "other-provider")),
            ("model", lambda value: value.__setitem__("model", "other-model")),
            ("generation_count", lambda value: value.__setitem__("generation_count", 2)),
        )
        for index, (label, mutation) in enumerate(mutations):
            state = RelayFixtureState(mutate_resource=mutation)
            identity = dispatch_identity(
                request,
                context,
                job_id=f"job_relay_drift_{index:02d}",
            )
            with self.subTest(label=label), local_relay(state) as endpoint:
                with self.assertRaises(RelayProtocolError):
                    await adapter(endpoint).dispatch(identity, request, context)

        def corrupt_base64(value: dict[str, object]) -> None:
            response = value["provider_response"]
            assert isinstance(response, dict)
            response["body_base64"] = "%%%"

        def corrupt_hash(value: dict[str, object]) -> None:
            response = value["provider_response"]
            assert isinstance(response, dict)
            response["body_sha256"] = "e" * 64

        for index, mutation in enumerate((corrupt_base64, corrupt_hash)):
            state = RelayFixtureState(mutate_resource=mutation)
            identity = dispatch_identity(
                request,
                context,
                job_id=f"job_relay_corrupt_{index:02d}",
            )
            with self.subTest(corruption=index), local_relay(state) as endpoint:
                with self.assertRaises(RelayProtocolError):
                    await adapter(endpoint).dispatch(identity, request, context)

    async def test_recovered_provider_bytes_use_the_direct_strict_parser(self) -> None:
        state = RelayFixtureState(provider_body=provider_bytes('{"message":"one","message":"two"}'))
        request = llm_request()
        context = operation_context()
        identity = dispatch_identity(request, context, job_id="job_relay_parser_0001")
        with local_relay(state) as endpoint:
            result = await adapter(endpoint).dispatch(identity, request, context)
        self.assertEqual(result.state, "SUCCEEDED")
        self.assertIsInstance(result.result, Failure)
        assert isinstance(result.result, Failure)
        self.assertEqual(result.result.error.code, "INVARIANT_VIOLATION")
        self.assertEqual(
            result.raw_response_sha256, hashlib.sha256(state.provider_body).hexdigest()
        )

    async def test_capability_contract_fails_fast(self) -> None:
        invalid_capabilities = (
            {"atomic_put_by_dispatch_id": False},
            {"linearizable_get": False},
            {"immutable_request_hash": False},
            {"max_generation_count": 2},
            {"result_retention_seconds": 60},
            {"max_response_bytes": 1024},
            {"protocol": "UNSAFE_CHAT_POST_V1"},
        )
        for changes in invalid_capabilities:
            state = RelayFixtureState()
            state.capabilities.update(changes)
            with self.subTest(changes=changes), local_relay(state) as endpoint:
                with self.assertRaises(RelayCapabilityError):
                    await adapter(endpoint).validate_capabilities()
                self.assertEqual(state.dispatch_puts, 0)

        unavailable = RelayFixtureState(capability_status=503)
        with local_relay(unavailable) as endpoint:
            with self.assertRaises(RelayDependencyUnavailable):
                await adapter(endpoint).validate_capabilities()

        wrong_media = RelayFixtureState(response_content_type="text/plain")
        with local_relay(wrong_media) as endpoint:
            with self.assertRaises(RelayProtocolError):
                await adapter(endpoint).validate_capabilities()

    def test_configuration_rejects_direct_or_unsafe_endpoints(self) -> None:
        values: dict[str, object] = {
            "api_key": "relay-secret-for-tests",
            "model": "configured-model-v1",
            "provider": "fixture-provider",
        }
        with self.assertRaisesRegex(ValueError, "chat-completions"):
            RecoverableOpenAIRelayConfig(
                relay_endpoint="https://provider.example/v1/chat/completions",
                **values,  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            RecoverableOpenAIRelayConfig(
                relay_endpoint="http://relay.example",
                **values,  # type: ignore[arg-type]
            )

    def test_dispatch_identity_is_stable_but_bound_to_ordinal_and_request(self) -> None:
        request_hash = llm_request_sha256(llm_request())
        first = provider_dispatch_id("tenant_yaya", "job_stable_0001", 1, request_hash)
        self.assertEqual(
            first,
            provider_dispatch_id("tenant_yaya", "job_stable_0001", 1, request_hash),
        )
        self.assertNotEqual(
            first,
            provider_dispatch_id("tenant_yaya", "job_stable_0001", 2, request_hash),
        )
        self.assertNotEqual(
            first,
            provider_dispatch_id("tenant_yaya", "job_stable_0001", 1, "f" * 64),
        )


if __name__ == "__main__":
    unittest.main()
