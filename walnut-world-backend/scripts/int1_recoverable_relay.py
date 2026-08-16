"""Deterministic localhost relay fixture for the INT1 wiring diagnostic.

This process is test infrastructure.  It never calls a real Provider and must
never be used as evidence for the real-Provider acceptance gate.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
import os
import re
import socket
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from yaya_agent_runtime import llm_recovery_sha256
from yaya_agent_runtime.schema_validation import (
    validate_instance,
    validate_schema_definition,
)

PROTOCOL = "YAYA_RECOVERABLE_LLM_V1"
HOST = "127.0.0.1"
MAX_REQUEST_BYTES = 4_194_304
MAX_RESPONSE_BYTES = 4_194_304
CAPABILITIES_PATH = "/v1/llm/capabilities"
DISPATCH_PATH = "/v1/llm/dispatches/"
STATS_PATH = "/__int1_diagnostic__/stats"
API_KEY_ENV = "WALNUT_INT1_RELAY_API_KEY"
PROVIDER_ENV = "WALNUT_INT1_RELAY_PROVIDER"
MODEL_ENV = "WALNUT_INT1_RELAY_MODEL"
DROP_ACK_ENV = "WALNUT_INT1_RELAY_DROP_FIRST_PUT_ACK"
FAIL_RECONCILE_ENV = "WALNUT_INT1_RELAY_FAIL_FIRST_RECONCILE"
_SHA256 = re.compile(r"[a-f0-9]{64}")
_DISPATCH_ID = re.compile(r"llmdsp_[a-f0-9]{40}")
_PUT_FIELDS = {
    "schema_version",
    "dispatch_id",
    "request_sha256",
    "context_sha256",
    "completion_sha256",
    "provider",
    "model",
    "completion",
}
_JSON_OBJECT_SCHEMA_INSTRUCTION = (
    "Return exactly one JSON object valid under output_schema. "
    "All required properties, const values, oneOf branches, "
    "patterns, bounds, and additionalProperties rules are mandatory. "
    "The first character must be { and the last non-whitespace character "
    "must be }. Never emit Markdown fences, <think> tags, or prose outside JSON."
)
_INT2_EIGHT_HARVEST_ENTRYPOINT = """#include <iostream>
#include <stdexcept>
#include <string>

int main(int argc, char** argv) {
    if (argc != 2) {
        return 3;
    }
    int length = 0;
    try {
        std::size_t parsed = 0;
        const std::string raw(argv[1]);
        length = std::stoi(raw, &parsed);
        if (parsed != raw.size()) {
            return 3;
        }
    } catch (const std::exception&) {
        return 3;
    }
    if (length < 0 || length > 8) {
        return 3;
    }
    std::cout << "{\\\"actions\\\":[";
    for (int index = 1; index <= length; ++index) {
        if (index != 1) {
            std::cout << ',';
        }
        std::cout
            << "{\\\"intent_id\\\":\\\"intent_harvest_000" << index
            << "\\\",\\\"action_type\\\":\\\"HARVEST\\\""
            << ",\\\"actor_entity_id\\\":\\\"avatar_0001\\\""
            << ",\\\"expected_world_revision\\\":0"
            << ",\\\"plot_id\\\":\\\"plot_000" << index
            << "\\\"}";
    }
    std::cout << "]}";
    return 0;
}
"""
_INT2_PATCH_RATIONALE = "Restore the exact canonical loop so all eight mature plots produce ordered HARVEST intents."


class RelayRequestError(ValueError):
    """A client request did not satisfy the closed fixture protocol."""

    def __init__(self, message: str, *, reason: str = "invalid_request") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(slots=True)
class DiagnosticRelayState:
    """In-memory immutable dispatch resources and sanitized diagnostic counters."""

    api_key: str = field(repr=False)
    provider: str
    model: str
    drop_first_put_ack: bool = True
    fail_first_reconcile_after_drop: bool = False
    requests: dict[str, dict[str, object]] = field(default_factory=dict)
    resources: dict[str, dict[str, object]] = field(default_factory=dict)
    capability_gets: int = 0
    dispatch_puts: int = 0
    reconcile_gets: int = 0
    acknowledgement_drops: int = 0
    reconcile_unavailable: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not 8 <= len(self.api_key) <= 4096:
            raise ValueError("diagnostic relay API key length is invalid")
        _bounded_text(self.provider, "provider")
        _bounded_text(self.model, "model")

    def capabilities(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "protocol": PROTOCOL,
            "result_retention_seconds": 604_800,
            "max_request_bytes": MAX_REQUEST_BYTES,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "atomic_put_by_dispatch_id": True,
            "linearizable_get": True,
            "immutable_request_hash": True,
            "max_generation_count": 1,
        }

    def put(
        self, dispatch_id: str, request: dict[str, object]
    ) -> tuple[int, dict[str, object], bool]:
        """Atomically create or replay one immutable logical generation."""

        _validate_put(dispatch_id, request, self.provider, self.model)
        with self.lock:
            self.dispatch_puts += 1
            existing = self.requests.get(dispatch_id)
            if existing is not None:
                if existing != request:
                    return 409, {"code": "DISPATCH_CONFLICT"}, False
                resource = copy.deepcopy(self.resources[dispatch_id])
                resource["replayed"] = True
                return 200, resource, False

            resource = _terminal_resource(request)
            self.requests[dispatch_id] = copy.deepcopy(request)
            self.resources[dispatch_id] = copy.deepcopy(resource)
            drop = self.drop_first_put_ack and self.acknowledgement_drops == 0
            if drop:
                self.acknowledgement_drops += 1
            return 201, resource, drop

    def get(self, dispatch_id: str) -> tuple[int, dict[str, object]]:
        if _DISPATCH_ID.fullmatch(dispatch_id) is None:
            raise RelayRequestError("dispatch identity is invalid")
        with self.lock:
            self.reconcile_gets += 1
            if (
                self.fail_first_reconcile_after_drop
                and self.acknowledgement_drops > 0
                and self.reconcile_unavailable == 0
            ):
                self.reconcile_unavailable += 1
                return 503, {"code": "TEMPORARILY_UNAVAILABLE"}
            resource = self.resources.get(dispatch_id)
            if resource is None:
                return 404, {
                    "schema_version": "1.0.0",
                    "code": "DISPATCH_NOT_FOUND",
                    "dispatch_id": dispatch_id,
                }
            return 200, copy.deepcopy(resource)

    def statistics(self) -> dict[str, object]:
        """Return recovery evidence without credentials or prompt/response bodies."""

        with self.lock:
            identities = [
                {
                    "dispatch_id": dispatch_id,
                    "request_sha256": request["request_sha256"],
                    "context_sha256": request["context_sha256"],
                    "completion_sha256": request["completion_sha256"],
                    "generation_count": 1,
                }
                for dispatch_id, request in sorted(self.requests.items())
            ]
            return {
                "schema_version": "1.0.0",
                "classification": "DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER",
                "protocol": PROTOCOL,
                "capability_gets": self.capability_gets,
                "dispatch_puts": self.dispatch_puts,
                "reconcile_gets": self.reconcile_gets,
                "acknowledgement_drops": self.acknowledgement_drops,
                "reconcile_unavailable": self.reconcile_unavailable,
                "unique_dispatches": len(self.requests),
                "total_generations": len(self.requests),
                "max_generation_count": 1 if self.requests else 0,
                "dispatches": identities,
            }


class DiagnosticRelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], state: DiagnosticRelayState) -> None:
        self.state = state
        super().__init__(address, DiagnosticRelayHandler)


class DiagnosticRelayHandler(BaseHTTPRequestHandler):
    """Closed GET/PUT surface for the recoverable relay protocol."""

    protocol_version = "HTTP/1.1"

    @property
    def _relay_server(self) -> DiagnosticRelayServer:
        if not isinstance(self.server, DiagnosticRelayServer):
            raise RuntimeError("diagnostic relay handler has an unexpected server")
        return self.server

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler hook
        if not self._authorized():
            return
        if self.path == CAPABILITIES_PATH:
            with self._relay_server.state.lock:
                self._relay_server.state.capability_gets += 1
            self._send(200, self._relay_server.state.capabilities())
            return
        if self.path == STATS_PATH:
            self._send(200, self._relay_server.state.statistics())
            return
        if self.path.startswith(DISPATCH_PATH):
            dispatch_id = self.path[len(DISPATCH_PATH) :]
            try:
                status, value = self._relay_server.state.get(dispatch_id)
            except RelayRequestError:
                self._send(400, {"code": "INVALID_DISPATCH_ID"})
                return
            self._send(status, value)
            return
        self._send(404, {"code": "NOT_FOUND"})

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler hook
        if not self._authorized():
            return
        if not self.path.startswith(DISPATCH_PATH):
            self._send(404, {"code": "NOT_FOUND"})
            return
        dispatch_id = self.path[len(DISPATCH_PATH) :]
        try:
            request = self._request_object()
            status, resource, drop = self._relay_server.state.put(dispatch_id, request)
        except RelayRequestError as error:
            self._send(400, {"code": "INVALID_REQUEST", "reason": error.reason})
            return
        if drop:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            self.close_connection = True
            return
        self._send(status, resource)

    def _authorized(self) -> bool:
        protocol = self.headers.get("x-yaya-llm-protocol", "")
        supplied = self.headers.get("authorization", "")
        expected = f"Bearer {self._relay_server.state.api_key}"
        if protocol != PROTOCOL or not hmac.compare_digest(supplied, expected):
            self._send(401, {"code": "UNAUTHORIZED"}, {"WWW-Authenticate": "Bearer"})
            return False
        return True

    def _request_object(self) -> dict[str, object]:
        media_type = self.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise RelayRequestError("content type must be application/json")
        raw_length = self.headers.get("content-length")
        if raw_length is None or not raw_length.isdigit():
            raise RelayRequestError("content length is required")
        length = int(raw_length)
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise RelayRequestError("request body length is invalid")
        try:
            value = json.loads(
                self.rfile.read(length).decode("utf-8", errors="strict"),
                object_pairs_hook=_closed_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError, RelayRequestError) as error:
            raise RelayRequestError("request body is not strict JSON") from error
        if not isinstance(value, dict):
            raise RelayRequestError("request body must be an object")
        return cast(dict[str, object], value)

    def _send(
        self,
        status: int,
        value: Mapping[str, object],
        headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, header_value in (headers or {}).items():
                self.send_header(name, header_value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _validate_put(
    dispatch_id: str,
    request: Mapping[str, object],
    provider: str,
    model: str,
) -> None:
    if set(request) != _PUT_FIELDS:
        raise RelayRequestError("dispatch request fields are not closed")
    if request.get("schema_version") != "1.0.0":
        raise RelayRequestError("dispatch schema is unsupported")
    if _DISPATCH_ID.fullmatch(dispatch_id) is None or request.get("dispatch_id") != dispatch_id:
        raise RelayRequestError("dispatch identity is invalid")
    for name in ("request_sha256", "context_sha256", "completion_sha256"):
        value = request.get(name)
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise RelayRequestError(f"{name} is invalid")
    if request.get("provider") != provider or request.get("model") != model:
        raise RelayRequestError("Provider or model differs from fixture authority")
    completion = request.get("completion")
    if not isinstance(completion, Mapping):
        raise RelayRequestError("completion must be an object")
    expected_hash = llm_recovery_sha256(
        {
            "schema_version": "1.0.0",
            "provider": provider,
            "model": model,
            "completion": dict(completion),
        }
    )
    if request["completion_sha256"] != expected_hash:
        raise RelayRequestError("completion hash differs from immutable request bytes")


def _terminal_resource(request: Mapping[str, object]) -> dict[str, object]:
    raw = _provider_response(request)
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
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
            "body_base64": base64.b64encode(raw).decode("ascii"),
            "body_sha256": hashlib.sha256(raw).hexdigest(),
        },
    }


def _provider_response(request: Mapping[str, object]) -> bytes:
    completion = request.get("completion")
    if not isinstance(completion, Mapping):
        raise RelayRequestError("completion must be an object")
    output = _closed_output(completion)
    value = {
        "id": f"int1_{str(request['dispatch_id'])[7:]}",
        "model": request["model"],
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        output,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
    }
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _closed_output(completion: Mapping[str, object]) -> dict[str, object]:
    schema = _completion_schema(completion)
    messages = completion.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        raise RelayRequestError("completion messages must be an array")
    tool_round_complete = False
    for message in messages:
        if isinstance(message, Mapping) and message.get("role") == "assistant":
            tool_round_complete = True
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            value = json.loads(
                content,
                object_pairs_hook=_closed_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, RelayRequestError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get("runtime_tool_result") is True:
            tool_round_complete = True

    decision_schema, invoke_schema = _agent_envelope_schemas(schema)
    if invoke_schema is not None and not tool_round_complete:
        output = cast(dict[str, object], _schema_value(invoke_schema, "envelope"))
    elif decision_schema is not None:
        output = cast(dict[str, object], _schema_value(decision_schema, "envelope"))
    else:
        raise RelayRequestError(
            "completion schema has no deterministic Agent decision branch",
            reason="role_shape_missing",
        )
    try:
        validate_instance(output, schema)
    except Exception as error:
        raise RelayRequestError(
            "fixture output does not satisfy completion schema",
            reason="generated_output_schema_invalid",
        ) from error
    return output


def _completion_schema(completion: Mapping[str, object]) -> Mapping[str, object]:
    response_format = completion.get("response_format")
    if not isinstance(response_format, Mapping):
        raise RelayRequestError(
            "completion response_format must be an object",
            reason="completion_schema_missing",
        )
    response_type = response_format.get("type")
    schema: object
    if response_type == "json_schema":
        json_schema = response_format.get("json_schema")
        schema = json_schema.get("schema") if isinstance(json_schema, Mapping) else None
    elif response_type == "json_object":
        schema = _json_object_message_schema(completion)
    else:
        raise RelayRequestError(
            "completion response format is unsupported",
            reason="completion_schema_missing",
        )
    if not isinstance(schema, Mapping):
        raise RelayRequestError(
            "completion response schema must be an object",
            reason="completion_schema_missing",
        )
    try:
        validate_schema_definition(schema)
    except Exception as error:
        raise RelayRequestError(
            "completion response schema is invalid",
            reason="completion_schema_invalid",
        ) from error
    return cast(Mapping[str, object], schema)


def _json_object_message_schema(completion: Mapping[str, object]) -> object:
    messages = completion.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        raise RelayRequestError(
            "completion messages must be an array",
            reason="completion_schema_missing",
        )
    candidates: list[object] = []
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "system":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            value = json.loads(
                content,
                object_pairs_hook=_closed_object,
                parse_constant=_reject_json_constant,
            )
        except json.JSONDecodeError:
            continue
        except RelayRequestError as error:
            raise RelayRequestError(
                "completion schema instruction is not strict JSON",
                reason="completion_schema_invalid",
            ) from error
        if not isinstance(value, dict) or set(value) != {"output_schema", "instruction"}:
            continue
        if value.get("instruction") != _JSON_OBJECT_SCHEMA_INSTRUCTION:
            raise RelayRequestError(
                "completion schema instruction is not authoritative",
                reason="completion_schema_invalid",
            )
        candidates.append(value.get("output_schema"))
    if len(candidates) != 1:
        raise RelayRequestError(
            "completion has no unique schema instruction",
            reason="completion_schema_missing",
        )
    return candidates[0]


def _agent_envelope_schemas(
    schema: Mapping[str, object],
) -> tuple[Mapping[str, object] | None, Mapping[str, object] | None]:
    raw_variants = schema.get("oneOf", [schema])
    if not isinstance(raw_variants, Sequence) or isinstance(
        raw_variants, (str, bytes, bytearray)
    ):
        raise RelayRequestError("Agent output schema variants are invalid")
    decision = None
    invoke = None
    for raw_variant in raw_variants:
        if not isinstance(raw_variant, Mapping):
            continue
        properties = raw_variant.get("properties")
        if not isinstance(properties, Mapping):
            continue
        kind = properties.get("kind")
        if not isinstance(kind, Mapping):
            continue
        if kind.get("const") == "decision":
            decision = cast(Mapping[str, object], raw_variant)
        elif kind.get("const") == "tool_calls" and _contains_const(raw_variant, "invoke_skill"):
            invoke = cast(Mapping[str, object], raw_variant)
    return decision, invoke


def _contains_const(value: object, expected: str) -> bool:
    if isinstance(value, Mapping):
        if value.get("const") == expected:
            return True
        return any(_contains_const(item, expected) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_const(item, expected) for item in value)
    return False


def _schema_value(schema: Mapping[str, object], field_name: str) -> object:
    if "const" in schema:
        return copy.deepcopy(schema["const"])
    raw_one_of = schema.get("oneOf")
    if isinstance(raw_one_of, Sequence) and not isinstance(
        raw_one_of, (str, bytes, bytearray)
    ):
        variants = [item for item in raw_one_of if isinstance(item, Mapping)]
        null_variant = next((item for item in variants if item.get("type") == "null"), None)
        selected = null_variant or (variants[0] if variants else None)
        if selected is None:
            raise RelayRequestError("schema oneOf has no supported branch")
        return _schema_value(cast(Mapping[str, object], selected), field_name)
    schema_type = schema.get("type")
    if schema_type == "null":
        return None
    if schema_type == "boolean":
        return False
    if schema_type == "string":
        enum = schema.get("enum")
        if isinstance(enum, Sequence) and not isinstance(enum, (str, bytes, bytearray)) and enum:
            return enum[0]
        if field_name == "replacement_content":
            return _INT2_EIGHT_HARVEST_ENTRYPOINT
        if field_name == "rationale":
            return _INT2_PATCH_RATIONALE
        values = {
            "call_id": "call_int1_local_relay_0001",
            "message": "deterministic evidence-grounded decision",
            "question": "What observable result differs from the objective?",
            "reason": "Bounded inference from the required Runtime Evidence.",
        }
        return values.get(field_name, "fixture")
    if schema_type in {"integer", "number"}:
        if field_name == "length":
            return 8
        if field_name == "score_delta":
            return -0.1
        if field_name == "confidence":
            return 0.8
        minimum = schema.get("minimum", 0)
        return minimum if isinstance(minimum, int | float) and not isinstance(minimum, bool) else 0
    if schema_type == "array":
        prefix = schema.get("prefixItems")
        if isinstance(prefix, Sequence) and not isinstance(prefix, (str, bytes, bytearray)):
            return [
                _schema_value(cast(Mapping[str, object], item), field_name)
                for item in prefix
                if isinstance(item, Mapping)
            ]
        minimum = schema.get("minItems", 0)
        if not isinstance(minimum, int) or isinstance(minimum, bool):
            raise RelayRequestError("array minItems is invalid")
        item_schema = schema.get("items")
        if minimum and not isinstance(item_schema, Mapping):
            raise RelayRequestError("required array items schema is unsupported")
        if field_name == "tool_calls" and isinstance(item_schema, Mapping):
            invoke_schema = _select_const_variant(item_schema, "invoke_skill")
            if invoke_schema is not None:
                item_schema = invoke_schema
        return [
            _schema_value(cast(Mapping[str, object], item_schema), field_name)
            for _ in range(minimum)
        ]
    if schema_type == "object":
        required = schema.get("required")
        properties = schema.get("properties")
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes, bytearray)):
            raise RelayRequestError("object required fields are invalid")
        if not isinstance(properties, Mapping):
            raise RelayRequestError("object properties are invalid")
        result: dict[str, object] = {}
        for key in required:
            if not isinstance(key, str) or not isinstance(properties.get(key), Mapping):
                raise RelayRequestError("required object property schema is missing")
            result[key] = _schema_value(
                cast(Mapping[str, object], properties[key]),
                key,
            )
        return result
    raise RelayRequestError(f"unsupported fixture schema type for {field_name}")


def _select_const_variant(
    schema: Mapping[str, object], expected: str
) -> Mapping[str, object] | None:
    one_of = schema.get("oneOf")
    if not isinstance(one_of, Sequence) or isinstance(one_of, (str, bytes, bytearray)):
        return schema if _contains_const(schema, expected) else None
    for variant in one_of:
        if isinstance(variant, Mapping) and _contains_const(variant, expected):
            return cast(Mapping[str, object], variant)
    return None


def _canonical_sha256(value: Mapping[str, object]) -> str:
    return llm_recovery_sha256(value)


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RelayRequestError("JSON object contains duplicate keys")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise RelayRequestError(f"JSON constant {value} is invalid")


def _bounded_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value.strip() != value:
        raise ValueError(f"{name} is invalid")


def _boolean_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def build_state_from_env() -> DiagnosticRelayState:
    return DiagnosticRelayState(
        api_key=_required_env(API_KEY_ENV),
        provider=os.getenv(PROVIDER_ENV, "int1-local-relay"),
        model=os.getenv(MODEL_ENV, "int1-local-model-v1"),
        drop_first_put_ack=_boolean_env(DROP_ACK_ENV, True),
        fail_first_reconcile_after_drop=_boolean_env(FAIL_RECONCILE_ENV, False),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="localhost-only INT1 recoverable relay fixture")
    parser.add_argument("--port", type=int, default=58792)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    server = DiagnosticRelayServer((HOST, arguments.port), build_state_from_env())
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()


__all__ = [
    "CAPABILITIES_PATH",
    "DISPATCH_PATH",
    "DiagnosticRelayServer",
    "DiagnosticRelayState",
    "PROTOCOL",
    "STATS_PATH",
    "_canonical_sha256",
]
