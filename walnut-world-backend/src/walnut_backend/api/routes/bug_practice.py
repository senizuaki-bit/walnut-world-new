"""Authenticated demo practice endpoints; never accept client success counters."""

import logging
import re

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from walnut_backend.adapters.doubao_tts import BookSpeechError
from walnut_backend.api.dependencies import get_operation_context
from walnut_backend.application.game.skill_builds import parse_strict_object, validate_source_bundle

router = APIRouter()
logger = logging.getLogger(__name__)


def _answer_source(body):
    if "source_bundle" not in body:
        return body.get("source")
    try:
        validate_source_bundle(body)
        bundle = body["source_bundle"]
        if (
            bundle.get("language") != "CPP20"
            or bundle.get("entrypoint") != "main.cpp"
            or len(bundle["files"]) != 1
        ):
            raise ValueError()
        if (
            body.get("compiler_profile", "YAYA_CPP20_SAFE_V1") != "YAYA_CPP20_SAFE_V1"
            or body.get("test_suite_version", "bug-practice-v1") != "bug-practice-v1"
        ):
            raise ValueError()
        source = bundle["files"][0]["content"]
        if "source" in body and body["source"] != source:
            raise ValueError()
        return source
    except (ValueError, KeyError, TypeError):
        raise ValueError("PRACTICE_SOURCE_INVALID") from None


@router.post("/product-experience/v1/sessions/{session_id}/practice-entries/{entry_id}/{action}")
async def practice(session_id: str, entry_id: str, action: str, request: Request):
    context = get_operation_context(request)
    service = request.app.state.bug_practice
    try:
        raw = await request.body()
        if len(raw) > 40000:
            raise ValueError("PRACTICE_REQUEST_TOO_LARGE")
        try:
            body = parse_strict_object(raw or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise ValueError("PRACTICE_REQUEST_INVALID") from None
        if not isinstance(body, dict):
            raise ValueError("PRACTICE_REQUEST_INVALID")
        if action == "start":
            value = await service.start(session_id, entry_id, context)
        elif action == "status":
            value = await service.status(session_id, entry_id, context)
        elif action == "prepare":
            value = await service.prepare(session_id, entry_id, body.get("run_id"), context)
        elif action == "answer":
            answer_id = body.get("answer_id") or request.headers.get("Idempotency-Key")
            if (
                request.headers.get("Idempotency-Key")
                and request.headers["Idempotency-Key"] != answer_id
            ):
                raise ValueError("PRACTICE_ANSWER_CONFLICT")
            value = await service.answer(
                session_id,
                entry_id,
                body.get("challenge_id"),
                answer_id,
                _answer_source(body),
                context,
            )
        elif action == "summary":
            value = await service.summary(session_id, entry_id, body.get("challenge_id"), context)
        else:
            raise ValueError("PRACTICE_ACTION_INVALID")
        return JSONResponse(value, headers={"Cache-Control": "no-store"})
    except (ValueError, BookSpeechError) as error:
        code = str(error)
        if not re.fullmatch(r"(?:PRACTICE|BOOK_SPEECH)_[A-Z_]+", code):
            code = "PRACTICE_UNAVAILABLE"
        retryable = (
            error.retryable
            if isinstance(error, BookSpeechError)
            else code
            in {
                "PRACTICE_UNAVAILABLE",
                "PRACTICE_MODEL_UNAVAILABLE",
                "PRACTICE_JUDGE_UNAVAILABLE",
                "PRACTICE_PROBLEM_INVALID",
            }
        )
        status = 503 if retryable or isinstance(error, BookSpeechError) else 409
        if code == "PRACTICE_ENTRY_EXPIRED":
            status = 410
        elif code == "PRACTICE_SESSION_UNAVAILABLE":
            status = 404
        elif not isinstance(error, BookSpeechError) and (
            code.endswith("_INVALID") or code == "PRACTICE_REQUEST_TOO_LARGE"
        ):
            status = 503 if retryable else 400
        logger.warning(
            "Practice request ended: action=%s code=%s retryable=%s", action, code, retryable
        )
        return JSONResponse(
            {"code": code, "retryable": retryable},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as error:
        logger.error("Practice dependency failed: %s", type(error).__name__)
        return JSONResponse(
            {"code": "PRACTICE_UNAVAILABLE", "retryable": True},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
