import asyncio
import json
from types import SimpleNamespace

import pytest
from yaya_agent_runtime.adapters.openai_compatible import parse_openai_completion_response

from walnut_backend.adapters import practice_model

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["message"],
    "properties": {"message": {"type": "string", "minLength": 30, "maxLength": 420}},
}


@pytest.mark.parametrize(
    ("always_invalid", "http_status"), [(False, 200), (True, 200), (False, 503)]
)
def test_summary_extra_type_is_repaired_with_bounded_new_dispatch(
    monkeypatch, always_invalid, http_status
):
    calls = []
    settings = SimpleNamespace(model="test-model", provider="test")

    class Provider:
        async def dispatch(self, identity, request, context):
            calls.append((identity, request))
            output = {
                "message": "你用循环和同一下标的数组计算缺口，并通过分级条件正确处理了零和三十的边界。"
            }
            if always_invalid or len(calls) == 1:
                output["type"] = "growth_summary"
            body = {
                "model": "test-model",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 30},
            }
            result = parse_openai_completion_response(
                settings,
                SimpleNamespace(
                    status=http_status,
                    headers={"content-type": "application/json"},
                    body=json.dumps(body).encode(),
                ),
                SCHEMA,
            )
            return SimpleNamespace(state="SUCCEEDED", result=result)

    async def provider(_settings):
        return Provider()

    monkeypatch.setattr(practice_model.RecoverableProviderSettings, "from_env", lambda: settings)
    monkeypatch.setattr(practice_model, "create_recoverable_provider", provider)
    monkeypatch.setattr(practice_model, "operation_context_sha256", lambda _: "c" * 64)

    async def run():
        operation = practice_model.PracticeModel().generate(
            "仅输出总结 JSON。", {"attempt_count": 2}, SCHEMA, object(), "summary-test"
        )
        if always_invalid or http_status != 200:
            with pytest.raises(ValueError, match="PRACTICE_MODEL_UNAVAILABLE"):
                await operation
        else:
            value = await operation
            assert set(value) == {"message"}
        expected_calls = 1 if http_status != 200 else 3 if always_invalid else 2
        assert len(calls) == expected_calls
        assert len({identity.dispatch_id for identity, _ in calls}) == len(calls)
        assert all(request.output_schema == calls[0][1].output_schema for _, request in calls)
        if expected_calls > 1:
            assert calls[1][1].messages != calls[0][1].messages

    asyncio.run(run())
