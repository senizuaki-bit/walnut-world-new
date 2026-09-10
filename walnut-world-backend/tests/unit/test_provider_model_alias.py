"""Provider model aliases must survive parsing and durable reply validation."""

import json

import pytest
from yaya_agent_contracts import Success
from yaya_agent_runtime import LlmDispatchIdentity
from yaya_agent_runtime.adapters import HttpResponse, OpenAICompatibleConfig
from yaya_agent_runtime.adapters.openai_compatible import parse_openai_completion_response

from walnut_backend.adapters.postgres.durable_llm import _validate_reply_authority
from walnut_backend.adapters.postgres.workflow_jobs import WorkflowInvariantError


def parsed_reply(provider: str, returned_model: str):
    config = OpenAICompatibleConfig(
        endpoint="https://api.deepseek.com/chat/completions",
        api_key="test-key",
        model="deepseek-v4-flash",
        provider=provider,
    )
    response = HttpResponse(
        200,
        {"content-type": "application/json"},
        json.dumps(
            {
                "model": returned_model,
                "choices": [{"message": {"content": '{"message":"hint"}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ).encode(),
    )
    identity = LlmDispatchIdentity("llmdsp_" + "a" * 40, "b" * 64, "c" * 64, provider, config.model)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
    }
    result = parse_openai_completion_response(config, response, schema)
    assert isinstance(result, Success), result
    return result, identity


def test_deepseek_flash_response_alias_preserves_durable_dispatch_identity():
    result, identity = parsed_reply("deepseek", "deepseek-flash")
    _validate_reply_authority(result, identity)


@pytest.mark.parametrize(
    ("provider", "returned_model"),
    [("deepseek", "unrelated-model"), ("other-provider", "deepseek-flash")],
)
def test_unrecognized_model_changes_still_fail_closed(provider, returned_model):
    result, identity = parsed_reply(provider, returned_model)
    with pytest.raises(WorkflowInvariantError, match="Provider reply authority drifted"):
        _validate_reply_authority(result, identity)
