"""Adversarial regressions for retries, cancellation and speech response latency."""

import asyncio

import pytest
from test_bug_practice import setup

from walnut_backend.adapters.doubao_tts import BookSpeech


def test_cancelled_answer_keeps_inflight_grade_for_same_id_retry():
    async def run():
        service, context = setup()
        entry_id = "a" * 32
        service.reads.passed = True
        await service.start("session_test", entry_id, context)
        problem = await service.prepare("session_test", entry_id, "run_1", context)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def grade(problem, source):
            calls.append(source)
            started.set()
            await release.wait()
            return {"correct": True, "stage": "PASSED", "message": "ok"}

        service.judge.grade = grade

        async def submit():
            return await service.answer(
                "session_test", entry_id, problem["challenge_id"], "b" * 32, "correct", context
            )

        first = asyncio.create_task(submit())
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        result = await submit()
        assert result["correct"] and result["attempts"] == 1
        assert len(calls) == 1, "retry must reuse the grade already started before disconnect"

    asyncio.run(run())


def test_cached_book_audio_is_not_blocked_by_other_generation(monkeypatch):
    monkeypatch.setattr("walnut_backend.adapters.doubao_tts._configured_key", lambda: "fake")

    async def run():
        service = BookSpeech()
        busy, release = asyncio.Event(), asyncio.Event()

        async def request(key, text):
            if text == "other":
                busy.set()
                await release.wait()
            return b"\0\0" * 10

        service._request = request
        await service.synthesize("cached", "first")
        other = asyncio.create_task(service.synthesize("other", "other"))
        await busy.wait()
        cached = asyncio.create_task(service.synthesize("cached", "first"))
        try:
            done, _ = await asyncio.wait({cached}, timeout=0.1)
            assert cached in done, "cached audio must not wait for another student's TTS"
            assert await cached == b"\0\0" * 10
        finally:
            release.set()
            await asyncio.gather(cached, other)

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["prepare", "summary"])
def test_cancelled_model_request_reuses_inflight_generation(operation):
    async def run():
        service, context = setup()
        entry_id = "a" * 32
        service.reads.passed = True
        await service.start("session_test", entry_id, context)
        if operation == "summary":
            problem = await service.prepare("session_test", entry_id, "run_1", context)
            await service.answer(
                "session_test", entry_id, problem["challenge_id"], "b" * 32, "correct", context
            )
        started, release = asyncio.Event(), asyncio.Event()
        original = service.model.generate
        calls = []

        async def generate(*args):
            calls.append(args[-1])
            started.set()
            await release.wait()
            return await original(*args)

        service.model.generate = generate

        async def submit():
            if operation == "prepare":
                return await service.prepare("session_test", entry_id, "run_1", context)
            return await service.summary("session_test", entry_id, problem["challenge_id"], context)

        first = asyncio.create_task(submit())
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        result = await submit()
        assert len(calls) == 1
        assert result["challenge_id"]
        assert not service.tasks

    asyncio.run(run())


@pytest.mark.parametrize("run_id", [None, {}, [], False, "", " " * 10])
def test_invalid_run_id_never_reaches_evidence_or_model(run_id):
    async def run():
        service, context = setup()
        service.reads.passed = True
        await service.start("session_test", "a" * 32, context)
        with pytest.raises(ValueError, match="PRACTICE_RUN_INVALID"):
            await service.prepare("session_test", "a" * 32, run_id, context)
        assert not service.model.calls

    asyncio.run(run())
