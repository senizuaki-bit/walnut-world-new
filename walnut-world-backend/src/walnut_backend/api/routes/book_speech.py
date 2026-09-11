"""Authorized speech for a persisted book summary, outside world mutation."""

from __future__ import annotations

import base64
import hashlib

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse
from yaya_agent_contracts import Failure

from walnut_backend.adapters.doubao_tts import SPEAKER, BookSpeechError
from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.api.routes.product_interactions import _failure

router = APIRouter()


@router.post(
    "/product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}/speech"
)
async def book_speech(session_id: str, interaction_id: str, request: Request):
    context = get_operation_context(request)
    result = await request.app.state.product_interactions.get(session_id, interaction_id, context)
    if isinstance(result, Failure):
        return _failure(request, result.error.code, result.error.stage, result.error.message)
    interaction = result.value
    if (
        interaction.get("role") != "book_agent"
        or interaction.get("response_type") != "growth_summary"
    ):
        return _failure(request, "INVALID_REQUEST", "VALIDATE", "speech requires a book summary")
    text = interaction.get("feedback", {}).get("message", "")
    # Authorization happens even on cache hits. Neither text nor voice comes from the client.
    identity = f"{context.actor.tenant_id}:{context.actor.actor_id}:{session_id}:{interaction_id}"
    try:
        audio = await request.app.state.book_speech.synthesize(identity, text)
    except BookSpeechError as error:
        return JSONResponse(
            {"code": error.code, "retryable": error.retryable},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {
            "interaction_id": interaction_id,
            "speaker": SPEAKER,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "format": "pcm_s16le",
            "sample_rate": 24000,
            "audio_base64": base64.b64encode(audio).decode("ascii"),
        },
        headers={"Cache-Control": "no-store"},
    )
