"""Bounded practice authoring through the configured recoverable LLM relay."""

import asyncio
import hashlib
import json

from yaya_agent_contracts import Failure, LlmMessage, LlmRequest, VersionSet
from yaya_agent_runtime.provider_recovery import (
    LlmDispatchIdentity,
    llm_request_sha256,
    operation_context_sha256,
)

from walnut_backend.provider_config import RecoverableProviderSettings
from walnut_backend.provider_wiring import create_recoverable_provider


class PracticeModel:
    async def generate(self, purpose, payload, schema, context, logical_id):
        settings = RecoverableProviderSettings.from_env()
        provider = await create_recoverable_provider(settings)
        request = LlmRequest(
            messages=(
                LlmMessage("system", purpose),
                LlmMessage("user", json.dumps(payload, ensure_ascii=False)),
            ),
            output_schema=schema,
            temperature=0.5,
            max_output_tokens=1600,
            timeout_ms=45000,
            versions=VersionSet(
                api_version="1.0.0",
                event_version="1",
                policy_version="bug-practice-v1",
                world_rules_version="farm-rules-1",
                teaching_spec_version="agent-teaching-v1",
                prompt_version="bug-practice-v1",
                model_version=settings.model,
            ),
        )
        # The caller keeps the original context throughout a retried logical request.
        identity = LlmDispatchIdentity(
            dispatch_id="llmdsp_" + hashlib.sha256(logical_id.encode()).hexdigest()[:40],
            request_sha256=llm_request_sha256(request),
            context_sha256=operation_context_sha256(context),
            provider=settings.provider,
            model=settings.model,
        )
        async with asyncio.timeout(65):
            result = await provider.dispatch(identity, request, context)
            while result.state == "PENDING":
                await asyncio.sleep(min(result.retry_after_seconds or 1, 2))
                result = await provider.reconcile(identity, request, context)
        if result.state != "SUCCEEDED" or isinstance(result.result, Failure):
            raise ValueError("PRACTICE_MODEL_UNAVAILABLE")
        from yaya_agent_backend.codec import encode

        return encode(result.result.value.output)
