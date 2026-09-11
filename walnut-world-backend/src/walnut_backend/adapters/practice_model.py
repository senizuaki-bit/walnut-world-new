"""Bounded practice authoring through the configured recoverable LLM relay."""

import asyncio
import hashlib
import json
import logging
from dataclasses import replace

from yaya_agent_contracts import Failure, LlmMessage, LlmRequest, VersionSet
from yaya_agent_runtime.provider_recovery import (
    LlmDispatchIdentity,
    llm_request_sha256,
    operation_context_sha256,
)

from walnut_backend.provider_config import RecoverableProviderSettings
from walnut_backend.provider_wiring import create_recoverable_provider

logger = logging.getLogger(__name__)


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
        for attempt in range(3):
            dispatch_key = logical_id if attempt == 0 else f"{logical_id}:format-repair:{attempt}"
            identity = LlmDispatchIdentity(
                dispatch_id="llmdsp_" + hashlib.sha256(dispatch_key.encode()).hexdigest()[:40],
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
            if result.state == "SUCCEEDED" and not isinstance(result.result, Failure):
                from yaya_agent_backend.codec import encode

                return encode(result.result.value.output)
            # Only a definitively rejected model output gets a new generation.
            # Uncertain transport outcomes retain the existing reconciliation path.
            error = result.result.error if isinstance(result.result, Failure) else None
            repairable = (
                error is not None
                and error.stage == "MODEL_OUTPUT"
                and error.details.get("repairable") is True
            )
            logger.warning(
                "Practice model rejected: stage=%s format_repair=%s attempt=%d",
                error.stage if error else "RELAY",
                repairable,
                attempt + 1,
            )
            if not repairable or attempt == 2:
                raise ValueError("PRACTICE_MODEL_UNAVAILABLE")
            request = replace(
                request,
                messages=(
                    LlmMessage(
                        "system",
                        purpose
                        + "\n上次输出未通过格式校验。只返回 schema 的 required 字段，禁止额外的 type 等键，遵守每个字段的类型和长度限制。",
                    ),
                    request.messages[1],
                ),
                temperature=0.0,
            )
