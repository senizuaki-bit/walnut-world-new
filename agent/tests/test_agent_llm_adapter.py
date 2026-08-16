from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "python"
TEST_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(TEST_ROOT))

from agent_runtime_fixtures import (  # noqa: E402
    NOW,
    StaticRoleConfigs,
    TraceSink,
    make_context,
    make_operation,
    make_role_config,
    make_versions,
)
from yaya_agent_contracts import (  # noqa: E402
    ActorRef,
    ActorType,
    ContentRef,
    Failure,
    LlmMessage,
    LlmRequest,
    OperationContext,
    Success,
    VersionSet,
)
from yaya_agent_runtime import (  # noqa: E402
    PromptBuilder,
    SharedAgentRuntime,
    ToolRegistry,
)
from yaya_agent_runtime.adapters import (  # noqa: E402
    HttpResponse,
    OpenAICompatibleConfig,
    OpenAICompatibleLlmAdapter,
    UrllibHttpTransport,
)
from yaya_agent_runtime.adapters.openai_compatible import (  # noqa: E402
    ProviderTransportError,
)
from yaya_agent_runtime.domain import thaw_value  # noqa: E402


class FakeTransport:
    def __init__(
        self, response: HttpResponse | None = None, error: Exception | None = None
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict[str, str], dict[str, object], int]] = []

    async def post_json(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, object],
        timeout_ms: int,
    ) -> HttpResponse:
        self.calls.append((url, dict(headers), dict(body), timeout_ms))
        if self.error is not None:
            raise self.error
        if self.response is None:
            raise AssertionError("fake response is missing")
        return self.response


@contextmanager
def local_provider(
    response_body: bytes,
    *,
    content_type: str = "application/json",
    delay_seconds: float = 0.0,
) -> Iterator[str]:
    """Serve provider bytes over a real loopback TCP/HTTP transport."""

    requests: list[bytes] = []

    class ProviderHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("content-length", "0"))
            requests.append(self.rfile.read(length))
            if delay_seconds:
                time.sleep(delay_seconds)
            try:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                # A real client timeout is expected to close the connection
                # before this deterministic fixture writes its delayed body.
                return

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1/chat/completions"
        if not requests:
            raise AssertionError("real provider fixture received no HTTP request")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("real provider fixture did not stop")


def operation_context() -> OperationContext:
    now = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)
    return OperationContext(
        request_id="req_adapter_00000001",
        correlation_id="corr_adapter_00000001",
        trace_id="trace_adapter_00000001",
        requested_at=now,
        actor=ActorRef("tenant_yaya", "student_0001", ActorType.STUDENT, ("game:player",)),
        content_ref=ContentRef("YAYA_FARM_001", "1.0.0", "a" * 64),
        command_id="cmd_adapter_00000001",
        causation_id=None,
    )


def llm_request() -> LlmRequest:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["message"],
        "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 100}},
    }
    return LlmRequest(
        messages=(LlmMessage("system", "Return strict JSON."), LlmMessage("user", "hello")),
        output_schema=schema,
        temperature=0,
        max_output_tokens=128,
        timeout_ms=5000,
        versions=VersionSet("v1", "v1", "p1", "w1", "t1", prompt_version="prompt-v1"),
    )


def provider_response(content: str, *, content_type: str = "application/json") -> HttpResponse:
    body = {
        "model": "provider-model-v1",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 4},
    }
    return HttpResponse(
        200,
        {"content-type": content_type},
        json.dumps(body, ensure_ascii=False).encode("utf-8"),
    )


class OpenAICompatibleAdapterTests(unittest.IsolatedAsyncioTestCase):
    def config(self, **changes: object) -> OpenAICompatibleConfig:
        values: dict[str, object] = {
            "endpoint": "https://provider.example/v1/chat/completions",
            "api_key": "secret-key-for-tests",
            "model": "configured-model-v1",
            "provider": "test-provider",
            "response_format": "json_schema",
        }
        values.update(changes)
        return OpenAICompatibleConfig(**values)  # type: ignore[arg-type]

    async def test_valid_output_is_schema_checked_and_identity_is_not_sent(self) -> None:
        transport = FakeTransport(provider_response('{"message":"有证据的反馈"}'))
        adapter = OpenAICompatibleLlmAdapter(self.config(), transport)

        result = await adapter.generate(llm_request(), operation_context())

        self.assertIsInstance(result, Success)
        assert isinstance(result, Success)
        self.assertEqual(result.value.output["message"], "有证据的反馈")
        self.assertEqual(result.value.input_tokens, 11)
        _, headers, body, timeout_ms = transport.calls[0]
        self.assertEqual(headers["authorization"], "Bearer secret-key-for-tests")
        self.assertEqual(timeout_ms, 5000)
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("student_0001", serialized)
        self.assertNotIn("n", body)
        self.assertEqual(body["response_format"]["type"], "json_schema")  # type: ignore[index]

    async def test_json_object_mode_transmits_exact_schema_without_actor_identity(self) -> None:
        transport = FakeTransport(provider_response('{"message":"schema guided"}'))
        adapter = OpenAICompatibleLlmAdapter(
            self.config(response_format="json_object"),
            transport,
        )

        result = await adapter.generate(llm_request(), operation_context())

        self.assertIsInstance(result, Success)
        _, _, body, _ = transport.calls[0]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("thinking", body)
        messages = body["messages"]
        self.assertIsInstance(messages, list)
        schema_message = messages[1]  # type: ignore[index]
        self.assertEqual(schema_message["role"], "system")  # type: ignore[index]
        instruction = json.loads(schema_message["content"])  # type: ignore[index]
        self.assertEqual(
            instruction["output_schema"],
            thaw_value(llm_request().output_schema),
        )
        self.assertNotIn("student_0001", json.dumps(body, ensure_ascii=False))

    async def test_explicit_thinking_mode_is_sent_without_changing_generic_default(self) -> None:
        transport = FakeTransport(provider_response('{"message":"strict json"}'))
        adapter = OpenAICompatibleLlmAdapter(
            self.config(response_format="json_object", thinking_mode="disabled"),
            transport,
        )

        result = await adapter.generate(llm_request(), operation_context())

        self.assertIsInstance(result, Success)
        _, _, body, _ = transport.calls[0]
        self.assertEqual(body["thinking"], {"type": "disabled"})

    async def test_extra_output_field_and_duplicate_key_fail_explicitly(self) -> None:
        for content in (
            '{"message":"ok","extra":true}',
            '{"message":"first","message":"second"}',
        ):
            with self.subTest(content=content):
                adapter = OpenAICompatibleLlmAdapter(
                    self.config(),
                    FakeTransport(provider_response(content)),
                )
                result = await adapter.generate(llm_request(), operation_context())
                self.assertIsInstance(result, Failure)
                assert isinstance(result, Failure)
                self.assertEqual(result.error.code, "INVARIANT_VIOLATION")
                self.assertEqual(result.error.stage, "MODEL_OUTPUT")

    async def test_transport_http_and_content_type_failures_are_not_silent(self) -> None:
        cases = (
            (
                FakeTransport(error=ProviderTransportError("offline")),
                "DEPENDENCY_UNAVAILABLE",
            ),
            (
                FakeTransport(HttpResponse(503, {"content-type": "application/json"}, b"{}")),
                "DEPENDENCY_UNAVAILABLE",
            ),
            (
                FakeTransport(provider_response('{"message":"ok"}', content_type="text/plain")),
                "INVARIANT_VIOLATION",
            ),
        )
        for transport, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                result = await OpenAICompatibleLlmAdapter(self.config(), transport).generate(
                    llm_request(),
                    operation_context(),
                )
                self.assertIsInstance(result, Failure)
                assert isinstance(result, Failure)
                self.assertEqual(result.error.code, expected_code)

    async def test_real_transport_oversize_response_returns_result_failure(self) -> None:
        oversized_body = provider_response('{"message":"ok"}').body + (b" " * 1024)
        with local_provider(oversized_body) as endpoint:
            adapter = OpenAICompatibleLlmAdapter(
                self.config(
                    endpoint=endpoint,
                    allow_insecure_localhost=True,
                ),
                UrllibHttpTransport(max_response_bytes=256),
            )

            result = await adapter.generate(llm_request(), operation_context())

        self.assertIsInstance(result, Failure)
        assert isinstance(result, Failure)
        self.assertEqual(result.error.code, "INVARIANT_VIOLATION")
        self.assertEqual(result.error.stage, "MODEL_OUTPUT")
        self.assertIn("max_response_bytes", result.error.details["validation_error"])

    async def test_real_transport_oversize_response_enters_runtime_fallback(self) -> None:
        oversized_body = provider_response('{"message":"ok"}').body + (b" " * 1024)
        with local_provider(oversized_body) as endpoint:
            trace = TraceSink()
            runtime = SharedAgentRuntime(
                llm=OpenAICompatibleLlmAdapter(
                    self.config(
                        endpoint=endpoint,
                        allow_insecure_localhost=True,
                    ),
                    UrllibHttpTransport(max_response_bytes=256),
                ),
                role_configs=StaticRoleConfigs(make_role_config("world_agent")),
                tools=ToolRegistry(trace),
                prompts=PromptBuilder(),
                trace=trace,
                versions=make_versions(),
                clock=lambda: NOW,
            )

            decision = await runtime.run("world_agent", make_context(), make_operation())

        self.assertTrue(decision.degraded)
        self.assertEqual(decision.source, "provider_fallback")
        self.assertEqual(decision.fallback_reason, "MODEL_OUTPUT_INVALID")
        self.assertEqual(decision.provider, "runtime")
        self.assertEqual(
            [event.name for event in trace.events],
            ["agent.turn.started", "agent.model.requested", "agent.turn.finished"],
        )
        self.assertTrue(trace.events[-1].fields["fallback"])

    async def test_real_transport_timeout_and_invalid_json_fail_explicitly(self) -> None:
        timeout_request = llm_request()
        timeout_request = type(timeout_request)(
            messages=timeout_request.messages,
            output_schema=timeout_request.output_schema,
            temperature=timeout_request.temperature,
            max_output_tokens=timeout_request.max_output_tokens,
            timeout_ms=50,
            versions=timeout_request.versions,
        )
        with local_provider(
            provider_response('{"message":"late"}').body,
            delay_seconds=0.2,
        ) as endpoint:
            timeout_result = await OpenAICompatibleLlmAdapter(
                self.config(endpoint=endpoint, allow_insecure_localhost=True),
                UrllibHttpTransport(),
            ).generate(timeout_request, operation_context())

        self.assertIsInstance(timeout_result, Failure)
        assert isinstance(timeout_result, Failure)
        self.assertEqual(timeout_result.error.code, "DEPENDENCY_UNAVAILABLE")
        self.assertEqual(timeout_result.error.stage, "MODEL_PROVIDER")

        with local_provider(b'{"choices":') as endpoint:
            invalid_result = await OpenAICompatibleLlmAdapter(
                self.config(endpoint=endpoint, allow_insecure_localhost=True),
                UrllibHttpTransport(),
            ).generate(llm_request(), operation_context())

        self.assertIsInstance(invalid_result, Failure)
        assert isinstance(invalid_result, Failure)
        self.assertEqual(invalid_result.error.code, "INVARIANT_VIOLATION")
        self.assertEqual(invalid_result.error.stage, "MODEL_OUTPUT")

    def test_endpoint_policy_rejects_remote_plain_http_and_userinfo(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            self.config(endpoint="http://provider.example/v1/chat/completions")
        with self.assertRaisesRegex(ValueError, "userinfo"):
            self.config(endpoint="https://user:pass@provider.example/v1/chat/completions")
        local = self.config(
            endpoint="http://127.0.0.1:8799/v1/chat/completions",
            allow_insecure_localhost=True,
        )
        self.assertEqual(local.endpoint, "http://127.0.0.1:8799/v1/chat/completions")

        with self.assertRaisesRegex(ValueError, "thinking_mode"):
            self.config(thinking_mode="automatic")


if __name__ == "__main__":
    unittest.main()
