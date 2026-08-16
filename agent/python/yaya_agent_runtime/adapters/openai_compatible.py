"""Best-effort OpenAI-compatible adapter with strict structured output.

The core runtime depends only on ``LlmPort``.  This adapter contains all HTTP
and provider-specific behavior and can be configured for any compatible
endpoint without hard-coding a model name or reading secrets in the domain.
It deliberately does not implement ``RecoverableLlmPort``: a plain
chat-completions POST has no client-addressable durable result after response
loss.  Production exactly-once orchestration uses the separate relay adapter.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

from yaya_agent_contracts import (
    ContractError,
    ErrorCategory,
    Failure,
    FrozenJsonObject,
    LlmPort,
    LlmReply,
    LlmRequest,
    OperationContext,
    Result,
    Success,
)

from ..domain import thaw_value
from ..errors import AgentConfigurationError, AgentToolInputError
from ..schema_validation import validate_instance, validate_schema_definition


class ProviderProtocolError(ValueError):
    """Provider bytes or JSON violated the configured response contract."""


class ProviderTransportError(ConnectionError):
    """The remote endpoint could not be reached or timed out."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep bearer credentials pinned to the configured authority."""

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not 100 <= self.status <= 599:
            raise ValueError("HTTP status must be an integer between 100 and 599")
        if not isinstance(self.body, bytes):
            raise TypeError("HTTP body must be bytes")
        object.__setattr__(
            self,
            "headers",
            {str(key).lower(): str(value) for key, value in self.headers.items()},
        )


class HttpTransport(Protocol):
    async def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
        timeout_ms: int,
    ) -> HttpResponse: ...


class UrllibHttpTransport:
    """Small standard-library transport; blocking I/O is isolated in a worker thread."""

    def __init__(self, *, max_response_bytes: int = 2_097_152) -> None:
        if isinstance(max_response_bytes, bool) or not 1 <= max_response_bytes <= 16_777_216:
            raise ValueError("max_response_bytes must be between 1 and 16777216")
        self._max_response_bytes = max_response_bytes
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    async def post_json(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
        timeout_ms: int,
    ) -> HttpResponse:
        return await asyncio.to_thread(self._post, url, headers, body, timeout_ms)

    def _post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, object],
        timeout_ms: int,
    ) -> HttpResponse:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(url, data=encoded, headers=dict(headers), method="POST")
        try:
            with self._opener.open(request, timeout=timeout_ms / 1000) as response:
                response_body = response.read(self._max_response_bytes + 1)
                if len(response_body) > self._max_response_bytes:
                    raise ProviderProtocolError("provider response exceeds max_response_bytes")
                return HttpResponse(response.status, dict(response.headers.items()), response_body)
        except urllib.error.HTTPError as error:
            response_body = error.read(self._max_response_bytes + 1)
            if len(response_body) > self._max_response_bytes:
                response_body = b""
            return HttpResponse(error.code, dict(error.headers.items()), response_body)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ProviderTransportError("provider transport failed") from error


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    endpoint: str
    api_key: str = field(repr=False)
    model: str
    provider: str
    response_format: Literal["json_object", "json_schema"] = "json_object"
    allow_insecure_localhost: bool = False
    thinking_mode: Literal["enabled", "disabled"] | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("provider endpoint cannot contain userinfo")
        if parsed.query or parsed.fragment or not parsed.hostname or not parsed.path:
            raise ValueError("provider endpoint must be an absolute URL without query or fragment")
        localhost = parsed.hostname.lower() in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (
            self.allow_insecure_localhost and localhost and parsed.scheme == "http"
        ):
            raise ValueError("provider endpoint must use HTTPS except explicit localhost tests")
        if not 8 <= len(self.api_key) <= 4096:
            raise ValueError("api_key length is invalid")
        if not 1 <= len(self.model) <= 128 or not 1 <= len(self.provider) <= 128:
            raise ValueError("model and provider must be non-empty bounded strings")
        if self.response_format not in {"json_object", "json_schema"}:
            raise ValueError("response_format is not supported")
        if self.thinking_mode not in {None, "enabled", "disabled"}:
            raise ValueError("thinking_mode is not supported")
        if not isinstance(self.allow_insecure_localhost, bool):
            raise ValueError("allow_insecure_localhost must be boolean")


class OpenAICompatibleLlmAdapter(LlmPort):
    """Direct chat-completions transport; response loss is not reconcilable."""

    def __init__(self, config: OpenAICompatibleConfig, transport: HttpTransport) -> None:
        self._config = config
        self._transport = transport

    async def generate(
        self,
        request: LlmRequest,
        context: OperationContext,
    ) -> Result[LlmReply]:
        del context  # identity is deliberately not sent unless a policy-approved adapter adds it
        prepared = prepare_openai_completion(self._config, request)
        if isinstance(prepared, Failure):
            return prepared
        body, output_schema_mapping = prepared
        try:
            response = await self._transport.post_json(
                self._config.endpoint,
                {
                    "authorization": f"Bearer {self._config.api_key}",
                    "content-type": "application/json; charset=utf-8",
                    "accept": "application/json",
                },
                body,
                request.timeout_ms,
            )
        except ProviderProtocolError as error:
            # A bounded transport can reject a response before it can return an
            # HttpResponse (for example, max_response_bytes).  Keep that path in
            # the Result algebra so SharedAgentRuntime can select its explicit
            # MODEL_OUTPUT_INVALID fallback instead of losing the whole turn.
            return Failure(_model_output_error(str(error)[:300], repairable=False))
        except (ProviderTransportError, TimeoutError, ConnectionError) as error:
            return Failure(_dependency_error(type(error).__name__))
        return parse_openai_completion_response(
            self._config,
            response,
            output_schema_mapping,
        )


def prepare_openai_completion(
    config: OpenAICompatibleConfig,
    request: LlmRequest,
) -> tuple[dict[str, object], Mapping[str, object]] | Failure:
    """Build the exact OpenAI-compatible body shared with the recoverable relay."""

    wire_messages: list[dict[str, object]] = []
    for message in request.messages:
        wire: dict[str, object] = {"role": message.role, "content": message.content}
        if message.name is not None:
            wire["name"] = message.name
        if message.tool_call_id is not None:
            wire["tool_call_id"] = message.tool_call_id
        wire_messages.append(wire)
    output_schema = thaw_value(request.output_schema)
    if not isinstance(output_schema, Mapping):
        return Failure(_model_output_error("request output_schema is not an object"))
    output_schema_mapping = cast(Mapping[str, object], output_schema)
    response_format: dict[str, object]
    if config.response_format == "json_schema":
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "yaya_agent_output",
                "strict": True,
                "schema": output_schema_mapping,
            },
        }
    else:
        response_format = {"type": "json_object"}
        # JSON-object mode only guarantees syntactic JSON at most
        # OpenAI-compatible providers; it does not transmit the schema as
        # an API constraint.  Supply the exact closed schema in a system
        # message so required call identities, const values and
        # additionalProperties=false are visible to the model.  The
        # schema is provider-neutral and contains no authenticated actor.
        schema_instruction: dict[str, object] = {
            "output_schema": output_schema_mapping,
            "instruction": (
                "Return exactly one JSON object valid under output_schema. "
                "All required properties, const values, oneOf branches, "
                "patterns, bounds, and additionalProperties rules are mandatory. "
                "The first character must be { and the last non-whitespace character "
                "must be }. Never emit Markdown fences, <think> tags, or prose outside JSON."
            ),
        }
        insertion = 1 if wire_messages and wire_messages[0].get("role") == "system" else 0
        wire_messages.insert(
            insertion,
            {
                "role": "system",
                "content": json.dumps(
                    schema_instruction,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
            },
        )
    body: dict[str, object] = {
        "model": config.model,
        "messages": wire_messages,
        "temperature": request.temperature,
        "max_tokens": request.max_output_tokens,
        "response_format": response_format,
        "stream": False,
    }
    if config.thinking_mode is not None:
        body["thinking"] = {"type": config.thinking_mode}
    return body, output_schema_mapping


def parse_openai_completion_response(
    config: OpenAICompatibleConfig,
    response: HttpResponse,
    output_schema_mapping: Mapping[str, object],
) -> Result[LlmReply]:
    """Parse original Provider bytes; relay and direct adapters share this path."""

    output: dict[str, object] = {}
    if response.status != 200:
        return Failure(_dependency_error("HTTP_STATUS", http_status=response.status))
    content_type = response.headers.get("content-type", "")
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type != "application/json" and not media_type.endswith("+json"):
        return Failure(_model_output_error("provider Content-Type is not application/json"))
    try:
        provider_payload = strict_json_object(response.body, "provider response")
        content, response_model, input_tokens, output_tokens = _extract_completion(
            provider_payload,
            config.model,
        )
        output = strict_json_object(content.encode("utf-8"), "assistant content")
        _validate_output(output, output_schema_mapping)
    except AgentToolInputError as error:
        return Failure(
            _model_output_error(
                str(error)[:300],
                diagnostics={
                    "validation_path": error.details.get("path", "$"),
                    "validation_keyword": error.details.get("keyword", "unknown"),
                    "output_shape": _structural_shape(output),
                },
            )
        )
    except (
        AgentConfigurationError,
        ProviderProtocolError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        return Failure(_model_output_error(str(error)[:300]))
    return Success(
        LlmReply(
            output=cast(FrozenJsonObject, output),
            provider=config.provider,
            model=response_model,
            source="provider",
            degraded=False,
            fallback_reason=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            evidence_refs=(),
        )
    )


def strict_json_object(data: bytes, label: str) -> dict[str, object]:
    try:
        text = data.decode("utf-8", errors="strict")
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProviderProtocolError(f"{label} is not strict UTF-8 JSON") from error
    if not isinstance(parsed, Mapping):
        raise ProviderProtocolError(f"{label} must be a JSON object")
    return cast(dict[str, object], parsed)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _extract_completion(
    payload: Mapping[str, object],
    configured_model: str,
) -> tuple[str, str, int, int]:
    choices = payload.get("choices")
    usage = payload.get("usage")
    if isinstance(choices, (str, bytes, bytearray)) or not isinstance(choices, list):
        raise ProviderProtocolError("provider choices must be an array")
    choice_items = cast(list[object], choices)
    if len(choice_items) != 1 or not isinstance(choice_items[0], Mapping):
        raise ProviderProtocolError("provider must return exactly one choice")
    choice = cast(Mapping[str, object], choice_items[0])
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ProviderProtocolError("provider choice is missing text message.content")
    message_value = cast(Mapping[str, object], message)
    if not isinstance(message_value.get("content"), str):
        raise ProviderProtocolError("provider choice is missing text message.content")
    if not isinstance(usage, Mapping):
        raise ProviderProtocolError("provider response is missing usage")
    usage_value = cast(Mapping[str, object], usage)
    input_tokens = usage_value.get("prompt_tokens")
    output_tokens = usage_value.get("completion_tokens")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens < 0
    ):
        raise ProviderProtocolError("provider token usage must use non-negative integers")
    model = payload.get("model", configured_model)
    if not isinstance(model, str) or not 1 <= len(model) <= 128:
        raise ProviderProtocolError("provider model identity is invalid")
    return cast(str, message_value["content"]), model, input_tokens, output_tokens


def _validate_output(
    output: Mapping[str, object],
    schema: Mapping[str, object],
) -> None:
    validate_schema_definition(schema)
    validate_instance(output, schema)


def _dependency_error(reason: str, *, http_status: int | None = None) -> ContractError:
    details: dict[str, object] = {"reason": reason}
    if http_status is not None:
        details["http_status"] = http_status
    return ContractError(
        code="DEPENDENCY_UNAVAILABLE",
        category=ErrorCategory.DEPENDENCY,
        retryable=True,
        user_message_key="dependency.temporarily_unavailable",
        stage="MODEL_PROVIDER",
        message="model provider is temporarily unavailable",
        details=cast(FrozenJsonObject, details),
    )


def _model_output_error(
    message: str,
    *,
    repairable: bool = True,
    diagnostics: Mapping[str, object] | None = None,
) -> ContractError:
    details: dict[str, object] = {
        "validation_error": message,
        "repairable": repairable,
    }
    if diagnostics is not None:
        details.update(diagnostics)
    return ContractError(
        code="INVARIANT_VIOLATION",
        category=ErrorCategory.INVARIANT,
        retryable=False,
        user_message_key="system.invariant_violation",
        stage="MODEL_OUTPUT",
        message="model output failed structured validation",
        details=cast(FrozenJsonObject, details),
    )


def _structural_shape(value: object, *, depth: int = 0) -> object:
    """Describe untrusted output structure without retaining user/model prose."""

    if depth >= 6:
        return {"type": type(value).__name__}
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        shape: dict[str, object] = {}
        for raw_key, item in mapping.items():
            key = str(raw_key)
            if key in {"kind", "name", "role", "response_type"} and isinstance(item, str):
                shape[key] = item[:64]
            else:
                shape[key] = _structural_shape(item, depth=depth + 1)
        return shape
    if isinstance(value, list):
        items = cast(list[object], value)
        return [_structural_shape(item, depth=depth + 1) for item in items[:4]]
    if value is None:
        return {"type": "null"}
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    return {"type": type(value).__name__}


__all__ = [
    "HttpResponse",
    "HttpTransport",
    "OpenAICompatibleConfig",
    "OpenAICompatibleLlmAdapter",
    "ProviderProtocolError",
    "ProviderTransportError",
    "UrllibHttpTransport",
]
