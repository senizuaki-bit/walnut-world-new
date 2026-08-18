from __future__ import annotations

import ast
import asyncio
import base64
import copy
import http.client
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from yaya_agent_contracts import (
    ActorRef,
    ActorType,
    ContentRef,
    FrozenJsonObject,
    LlmMessage,
    LlmRequest,
    OperationContext,
    Success,
    VersionSet,
)
from yaya_agent_runtime import (
    PEDAGOGY_POLICY_VERSION,
    LlmDispatchIdentity,
    TeachingDirective,
    TeachingPhase,
    freeze_object,
    llm_request_sha256,
    operation_context_sha256,
    provider_dispatch_id,
)
from yaya_agent_runtime.adapters import (
    OpenAICompatibleConfig,
    RecoverableOpenAIRelayAdapter,
    RecoverableOpenAIRelayConfig,
    RelayDependencyUnavailable,
    UrllibRelayHttpTransport,
)
from yaya_agent_runtime.adapters.openai_compatible import prepare_openai_completion
from yaya_agent_runtime.model_output import build_model_output_schema
from yaya_agent_runtime.schema_validation import validate_instance

BACKEND_ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_SEED = BACKEND_ROOT / "src" / "walnut_backend" / "int1_e2e_authority.py"
sys.path.insert(0, str(BACKEND_ROOT / "scripts"))

import int1_recoverable_relay as relay  # pyright: ignore[reportMissingImports]  # noqa: E402

API_KEY = secrets.token_hex(32)
PROVIDER = "int1-local-relay"
MODEL = "int1-local-model-v1"
NOW = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)


@contextmanager
def running_relay(*, drop_ack: bool = True, fail_reconcile: bool = False):
    state = relay.DiagnosticRelayState(
        api_key=API_KEY,
        provider=PROVIDER,
        model=MODEL,
        drop_first_put_ack=drop_ack,
        fail_first_reconcile_after_drop=fail_reconcile,
    )
    server = relay.DiagnosticRelayServer((relay.HOST, 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_fixture_matches_recoverable_client_and_recovers_one_lost_put_ack() -> None:
    request = _llm_request()
    context = _operation_context()
    request_hash = llm_request_sha256(request)
    identity = LlmDispatchIdentity(
        dispatch_id=provider_dispatch_id("tenant_yaya", "job_int1_fixture_0001", 1, request_hash),
        request_sha256=request_hash,
        context_sha256=operation_context_sha256(context),
        provider=PROVIDER,
        model=MODEL,
    )

    with running_relay() as (state, port):
        adapter = RecoverableOpenAIRelayAdapter(
            RecoverableOpenAIRelayConfig(
                relay_endpoint=f"http://127.0.0.1:{port}",
                api_key=API_KEY,
                provider=PROVIDER,
                model=MODEL,
                response_format="json_schema",
                allow_insecure_localhost=True,
                max_response_bytes=4096,
            ),
            UrllibRelayHttpTransport(max_response_bytes=65_536),
        )
        recovered = asyncio.run(adapter.dispatch(identity, request, context))
        replayed = asyncio.run(adapter.dispatch(identity, request, context))

    assert recovered.state == "SUCCEEDED"
    assert isinstance(recovered.result, Success)
    assert recovered.result.value.output["kind"] == "tool_calls"
    assert recovered.result.value.output["decision"] is None
    assert replayed.result == recovered.result
    assert replayed.replayed is True
    assert state.acknowledgement_drops == 1
    assert state.reconcile_gets == 1
    assert state.dispatch_puts == 2
    assert len(state.resources) == 1
    assert next(iter(state.resources.values()))["generation_count"] == 1
    stored_request = state.requests[identity.dispatch_id]
    completion = stored_request["completion"]
    assert isinstance(completion, dict)
    assert "n" not in completion


def test_fixture_capabilities_conflict_stats_and_secret_hygiene() -> None:
    with running_relay(drop_ack=False) as (state, port):
        capabilities = _request(port, "GET", relay.CAPABILITIES_PATH)[1]
        assert capabilities == state.capabilities()

        body = _put_body("llmdsp_" + "a" * 40)
        status, created = _request(
            port,
            "PUT",
            f"{relay.DISPATCH_PATH}{body['dispatch_id']}",
            body,
        )
        assert status == 201
        assert created["generation_count"] == 1
        raw = created["provider_response"]
        assert isinstance(raw, dict)
        provider_body = json.loads(base64.b64decode(raw["body_base64"]))
        assert provider_body["model"] == MODEL

        conflicting = copy.deepcopy(body)
        conflicting["request_sha256"] = "f" * 64
        assert (
            _request(
                port,
                "PUT",
                f"{relay.DISPATCH_PATH}{body['dispatch_id']}",
                conflicting,
            )[0]
            == 409
        )

        status, statistics = _request(port, "GET", relay.STATS_PATH)
        assert status == 200
        assert statistics["classification"] == "DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER"
        assert statistics["total_generations"] == 1
        assert statistics["max_generation_count"] == 1
        assert API_KEY not in json.dumps(statistics)
        assert API_KEY not in repr(state)

        # Worker process restart performs one fresh capability proof before it
        # may claim jobs.  That read-only probe changes only its diagnostic
        # counter and must not be mistaken for another Provider generation.
        assert _request(port, "GET", relay.CAPABILITIES_PATH)[0] == 200
        after_capability_probe = _request(port, "GET", relay.STATS_PATH)[1]
        assert after_capability_probe["capability_gets"] == statistics["capability_gets"] + 1
        assert {
            key: value for key, value in after_capability_probe.items() if key != "capability_gets"
        } == {key: value for key, value in statistics.items() if key != "capability_gets"}

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "GET",
            relay.CAPABILITIES_PATH,
            headers={"x-yaya-llm-protocol": relay.PROTOCOL},
        )
        assert connection.getresponse().status == 401
        connection.close()


def test_fixture_obeys_initial_tool_then_final_decision_protocol() -> None:
    tool_envelope = {
        "kind": "tool_calls",
        "decision": None,
        "required_call_fields": ["call_id", "name", "arguments"],
    }
    decision_envelope = {
        "kind": "decision",
        "decision": {"role": "book_agent"},
        "tool_calls": [],
    }
    schema = _root_agent_output_schema()
    initial = {
        "response_format": _response_format(schema),
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "turn_context": {
                            "required_first_tool": {
                                "name": "invoke_skill",
                                "skill_id": "bound_skill",
                                "envelope": tool_envelope,
                            }
                        },
                    }
                ),
            }
        ],
    }
    first = relay._closed_output(initial)
    assert first["kind"] == "tool_calls"
    assert first["decision"] is None
    assert first["tool_calls"] == [
        {
            "call_id": "call_int1_local_relay_0001",
            "name": "invoke_skill",
            "arguments": {"skill_id": "bound_skill", "arguments": {"length": 8}},
        }
    ]

    repair_before_tool = {
        "response_format": _response_format(schema),
        "messages": [
            initial["messages"][0],
            {
                "role": "user",
                "content": json.dumps(
                    {"validation_failed": True, "required_final_envelope_shape": decision_envelope}
                ),
            },
        ],
    }
    assert relay._closed_output(repair_before_tool)["kind"] == "tool_calls"

    after_tool = {
        "response_format": _response_format(schema),
        "messages": [
            initial["messages"][0],
            {"role": "assistant", "content": json.dumps(first)},
            {
                "role": "user",
                "content": json.dumps({"required_final_envelope_shape": decision_envelope}),
            },
        ],
    }
    final = relay._closed_output(after_tool)
    validate_instance(final, schema)
    assert final["kind"] == "decision"
    assert cast(dict[str, object], final["decision"])["role"] == "xiaohutao"
    assert final["tool_calls"] == []


def test_fixture_reads_json_object_schema_instruction_for_root_and_final_roles() -> None:
    root_request = _llm_request()
    root_completion = _prepared_completion(root_request, response_format="json_object")
    root_output = relay._closed_output(root_completion)
    validate_instance(root_output, _root_agent_output_schema())
    assert root_output["kind"] == "tool_calls"

    teaching_schema = _formal_agent_output_schema("teaching_agent")
    teaching_request = LlmRequest(
        messages=(
            LlmMessage("system", "Return a grounded teaching decision."),
            LlmMessage("user", json.dumps({"event_type": "run_failed"})),
        ),
        output_schema=cast(FrozenJsonObject, teaching_schema),
        temperature=0.2,
        max_output_tokens=256,
        timeout_ms=5_000,
        versions=root_request.versions,
    )
    teaching_output = relay._closed_output(
        _prepared_completion(teaching_request, response_format="json_object")
    )
    validate_instance(teaching_output, teaching_schema)
    assert teaching_output["kind"] == "decision"
    assert cast(dict[str, object], teaching_output["decision"])["role"] == "teaching_agent"


@pytest.mark.parametrize(
    ("role", "response_type"),
    (
        ("teaching_agent", "question"),
        ("bug_agent", "question"),
        ("book_agent", "growth_summary"),
    ),
)
def test_fixture_relays_every_formal_a8_final_role_shape(role: str, response_type: str) -> None:
    schema = _formal_agent_output_schema(role)
    completion = {
        "response_format": _response_format(schema),
        "messages": [
            {
                "role": "user",
                "content": json.dumps({"event_type": "run_failed"}),
            }
        ],
    }

    output = relay._closed_output(completion)
    validate_instance(output, schema)
    decision = cast(dict[str, object], output["decision"])
    assert output["kind"] == "decision"
    assert decision["role"] == role
    assert decision["response_type"] == response_type
    assert cast(dict[str, object], decision["learner_inference"])["evidence_ids"] == [
        "evidence_001"
    ]
    assert output["tool_calls"] == []


def test_fixture_skill_patch_restores_exact_compilable_eight_harvest_entrypoint(
    tmp_path: Path,
) -> None:
    schema = _formal_skill_patch_output_schema()
    completion = {
        "response_format": _response_format(schema),
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "event_type": "skill_patch_requested",
                        "selected_failure_count": 4,
                    }
                ),
            }
        ],
    }

    output = relay._closed_output(completion)
    validate_instance(output, schema)
    decision = cast(dict[str, object], output["decision"])
    patch = cast(dict[str, object], decision["skill_patch"])
    seed_tree = ast.parse(AUTHORITY_SEED.read_text(encoding="utf-8"))
    harvest_functions = [
        node
        for node in seed_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_harvest_source"
    ]
    seed_sources = [
        node.value.value
        for function in harvest_functions
        for node in ast.walk(function)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and "int main(int argc, char** argv)" in node.value.value
    ]
    assert len(seed_sources) == 1
    seed_source = seed_sources[0]

    assert output["kind"] == "decision"
    assert output["tool_calls"] == []
    assert decision["role"] == "teaching_agent"
    assert decision["response_type"] == "skill_patch"
    assert decision["hint_level"] == 4
    assert decision["requires_student_confirmation"] is True
    assert patch["replacement_content"] == seed_source
    assert patch["replacement_content"] != "fixture"
    assert "int main(int argc, char** argv)" in seed_source
    assert "for (int index = 1; index <= length; ++index)" in seed_source
    assert '\\"action_type\\":\\"HARVEST\\"' in seed_source
    assert patch["rationale"] == (
        "Restore the exact canonical loop so all eight mature plots produce ordered "
        "HARVEST intents."
    )

    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is not None:
        source_path = tmp_path / "main.cpp"
        executable_path = tmp_path / ("main.exe" if os.name == "nt" else "main")
        source_path.write_text(seed_source, encoding="utf-8")
        compiled = subprocess.run(
            [compiler, "-std=c++20", str(source_path), "-o", str(executable_path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr
        executed = subprocess.run(
            [str(executable_path), "8"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert executed.returncode == 0, executed.stdout + executed.stderr
        actions = json.loads(executed.stdout)["actions"]
        assert len(actions) == 8
        assert all(action["action_type"] == "HARVEST" for action in actions)


def test_fixture_can_force_retry_boundary_after_lost_ack_without_second_generation() -> None:
    request = _llm_request()
    context = _operation_context()
    request_hash = llm_request_sha256(request)
    identity = LlmDispatchIdentity(
        dispatch_id=provider_dispatch_id("tenant_yaya", "job_int1_retry_0001", 1, request_hash),
        request_sha256=request_hash,
        context_sha256=operation_context_sha256(context),
        provider=PROVIDER,
        model=MODEL,
    )
    with running_relay(fail_reconcile=True) as (state, port):
        adapter = RecoverableOpenAIRelayAdapter(
            RecoverableOpenAIRelayConfig(
                relay_endpoint=f"http://127.0.0.1:{port}",
                api_key=API_KEY,
                provider=PROVIDER,
                model=MODEL,
                response_format="json_schema",
                allow_insecure_localhost=True,
                max_response_bytes=4096,
            )
        )
        with pytest.raises(RelayDependencyUnavailable):
            asyncio.run(adapter.dispatch(identity, request, context))
        recovered = asyncio.run(adapter.reconcile(identity, request, context))

    assert recovered.state == "SUCCEEDED"
    assert state.dispatch_puts == 1
    assert state.acknowledgement_drops == 1
    assert state.reconcile_gets == 2
    assert state.reconcile_unavailable == 1
    assert len(state.resources) == 1


def test_fixture_rejects_noncanonical_completion_hash_without_generation() -> None:
    with running_relay(drop_ack=False) as (state, port):
        body = _put_body("llmdsp_" + "b" * 40)
        body["completion_sha256"] = "0" * 64
        status, value = _request(
            port,
            "PUT",
            f"{relay.DISPATCH_PATH}{body['dispatch_id']}",
            body,
        )

    assert status == 400
    assert value["code"] == "INVALID_REQUEST"
    assert state.resources == {}
    assert state.requests == {}


def test_fixture_invalid_generation_is_atomic_and_exposes_only_safe_reason() -> None:
    dispatch_id = "llmdsp_" + "c" * 40
    invalid = _put_body(dispatch_id)
    completion = cast(dict[str, object], invalid["completion"])
    completion["response_format"] = {"type": "json_object"}
    invalid["completion_sha256"] = relay._canonical_sha256(
        {
            "schema_version": "1.0.0",
            "provider": PROVIDER,
            "model": MODEL,
            "completion": completion,
        }
    )

    with running_relay(drop_ack=False) as (state, port):
        status, rejected = _request(
            port,
            "PUT",
            f"{relay.DISPATCH_PATH}{dispatch_id}",
            invalid,
        )
        corrected = _put_body(dispatch_id)
        created_status, created = _request(
            port,
            "PUT",
            f"{relay.DISPATCH_PATH}{dispatch_id}",
            corrected,
        )
        replay_status, replayed = _request(
            port,
            "PUT",
            f"{relay.DISPATCH_PATH}{dispatch_id}",
            corrected,
        )

    assert status == 400
    assert rejected == {"code": "INVALID_REQUEST", "reason": "completion_schema_missing"}
    assert created_status == 201
    assert replay_status == 200
    assert replayed["replayed"] is True
    assert created["generation_count"] == replayed["generation_count"] == 1
    assert len(state.requests) == len(state.resources) == 1


def test_fixture_cli_reads_secret_only_from_environment_and_stays_silent() -> None:
    port = _free_port()
    environment = os.environ.copy()
    environment.update(
        {
            relay.API_KEY_ENV: API_KEY,
            relay.PROVIDER_ENV: PROVIDER,
            relay.MODEL_ENV: MODEL,
            relay.DROP_ACK_ENV: "false",
            relay.FAIL_RECONCILE_ENV: "false",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(BACKEND_ROOT / "scripts" / "int1_recoverable_relay.py"),
            "--port",
            str(port),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                status, value = _request(port, "GET", relay.CAPABILITIES_PATH)
                break
            except (ConnectionError, OSError):
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        assert status == 200
        assert value["protocol"] == relay.PROTOCOL
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
    assert API_KEY not in stdout
    assert API_KEY not in stderr


def _llm_request() -> LlmRequest:
    return LlmRequest(
        messages=(
            LlmMessage("system", "Return strict JSON."),
            LlmMessage(
                "user",
                json.dumps(
                    {
                        "required_first_tool": {
                            "name": "invoke_skill",
                            "skill_id": "bound_skill",
                        }
                    },
                    separators=(",", ":"),
                ),
            ),
        ),
        output_schema=cast(FrozenJsonObject, _root_agent_output_schema()),
        temperature=0,
        max_output_tokens=128,
        timeout_ms=5_000,
        versions=VersionSet(
            "1.0.0",
            "1",
            "worker-runtime-v1",
            "farm-rules-1",
            "agent-teaching-v1",
            prompt_version="int1-prompt-v1",
            model_version=MODEL,
        ),
    )


def _operation_context() -> OperationContext:
    return OperationContext(
        request_id="req_int1_fixture_0001",
        correlation_id="corr_int1_fixture_0001",
        trace_id="trace_int1_fixture_0001",
        requested_at=NOW,
        actor=ActorRef("tenant_yaya", "student_0001", ActorType.STUDENT, ("game:player",)),
        content_ref=ContentRef("YAYA_FARM_001", "1.0.0", "a" * 64),
        command_id="cmd_int1_fixture_0001",
        causation_id=None,
    )


def _put_body(dispatch_id: str) -> dict[str, object]:
    schema = _root_agent_output_schema()
    completion: dict[str, object] = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "required_first_tool": {
                            "name": "invoke_skill",
                            "skill_id": "bound_skill",
                        }
                    }
                ),
            }
        ],
        "temperature": 0,
        "response_format": _response_format(schema),
    }
    completion_sha256 = relay._canonical_sha256(
        {
            "schema_version": "1.0.0",
            "provider": PROVIDER,
            "model": MODEL,
            "completion": completion,
        }
    )
    return {
        "schema_version": "1.0.0",
        "dispatch_id": dispatch_id,
        "request_sha256": "1" * 64,
        "context_sha256": "2" * 64,
        "completion_sha256": completion_sha256,
        "provider": PROVIDER,
        "model": MODEL,
        "completion": completion,
    }


def _response_format(schema: Mapping[str, object]) -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "yaya_agent_output",
            "strict": True,
            "schema": dict(schema),
        },
    }


def _prepared_completion(
    request: LlmRequest,
    *,
    response_format: Literal["json_object", "json_schema"],
) -> dict[str, object]:
    prepared = prepare_openai_completion(
        OpenAICompatibleConfig(
            endpoint="http://127.0.0.1/v1/chat/completions",
            api_key="fixture-provider-key",
            model=MODEL,
            provider=PROVIDER,
            response_format=response_format,
            allow_insecure_localhost=True,
        ),
        request,
    )
    assert isinstance(prepared, tuple)
    return prepared[0]


def _root_agent_output_schema() -> dict[str, object]:
    return build_model_output_schema(
        (
            freeze_object(
                {
                    "name": "invoke_skill",
                    "input_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["skill_id", "arguments"],
                        "properties": {
                            "skill_id": {"type": "string", "const": "bound_skill"},
                            "arguments": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["length"],
                                "properties": {
                                    "length": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 8,
                                    }
                                },
                            },
                        },
                    },
                }
            ),
        ),
        max_tool_calls=1,
        role="xiaohutao",
    )


def _formal_agent_output_schema(role: str) -> dict[str, object]:
    phase = {
        "teaching_agent": TeachingPhase.REVIEW,
        "bug_agent": TeachingPhase.RECTIFICATION,
        "book_agent": TeachingPhase.SUMMARIZATION,
    }[role]
    allowed = {
        "teaching_agent": ("question", "hint"),
        "bug_agent": ("question",),
        "book_agent": ("growth_summary",),
    }[role]
    hint_level = {"teaching_agent": 0, "bug_agent": 2, "book_agent": 0}[role]
    directive = TeachingDirective(
        phase=phase,
        target_concept="for_loop",
        hint_level=hint_level,
        allowed_response_types=allowed,
        patch_eligible=False,
        full_solution_eligible=False,
        required_evidence_ids=("evidence_run_aaaaaaaaaaaaaaaaaaaaaaaa",),
        reason_codes=(
            "CURRENT_RUN_FAILED",
            "PATCH_DISABLED_RUNTIME_STAGE",
            "FULL_SOLUTION_DISABLED",
        ),
        pedagogy_policy_version=PEDAGOGY_POLICY_VERSION,
        learner_revision=0,
        teaching_spec_version="teaching-1",
    )
    return build_model_output_schema(
        (),
        max_tool_calls=0,
        role=cast(Any, role),
        directive=directive,
        required_evidence_aliases=("evidence_001",),
    )


def _formal_skill_patch_output_schema() -> dict[str, object]:
    directive = TeachingDirective(
        phase=TeachingPhase.RECTIFICATION,
        target_concept="for_loop",
        hint_level=4,
        allowed_response_types=("skill_patch",),
        patch_eligible=True,
        full_solution_eligible=False,
        required_evidence_ids=("evidence_run_aaaaaaaaaaaaaaaaaaaaaaaa",),
        reason_codes=(
            "CURRENT_RUN_FAILED",
            "EXPLICIT_SKILL_PATCH_REQUEST",
            "PATCH_FEATURE_AND_CAPABILITY_ENABLED",
            "DRAFT_BUILD_RUN_AUTHORITY_VALIDATED",
            "FULL_SOLUTION_DISABLED",
        ),
        pedagogy_policy_version=PEDAGOGY_POLICY_VERSION,
        learner_revision=4,
        teaching_spec_version="teaching-1",
    )
    return build_model_output_schema(
        (),
        max_tool_calls=0,
        role="teaching_agent",
        directive=directive,
        required_evidence_aliases=("evidence_001",),
    )


def _request(
    port: int,
    method: str,
    path: str,
    body: Mapping[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {
        "authorization": f"Bearer {API_KEY}",
        "x-yaya-llm-protocol": relay.PROTOCOL,
        "accept": "application/json",
    }
    if encoded is not None:
        headers["content-type"] = "application/json; charset=utf-8"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request(method, path, body=encoded, headers=headers)
    response = connection.getresponse()
    value = json.loads(response.read())
    connection.close()
    assert isinstance(value, dict)
    return response.status, cast(dict[str, object], value)


def _free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])
