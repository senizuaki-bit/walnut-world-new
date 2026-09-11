import asyncio
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from yaya_agent_runtime.bug_practice import expected_output, starter_source, validate_problem

from walnut_backend.application.product.bug_practice import BugPractice

PROBLEM = {
    "title": "雨后的新试验田",
    "brief": "另一片试验田也需要按各自的目标湿度照顾，请把已掌握的浇水规则用于这组新数据。",
    "focus": "同一下标读取两个数组，并检查零缺口与三十缺口的边界。",
    "moisture": [10, 50, 70, 30, 60, 20, 55, 65],
    "target": [50, 65, 60, 60, 60, 70, 70, 70],
}


class Reads:
    def __init__(self):
        self.records = [(datetime.now(UTC) - timedelta(days=1), "old failure")] * 5
        self.passed = False

    async def authorize(self, session_id, context):
        if session_id != "session_test":
            raise ValueError("PRACTICE_SESSION_UNAVAILABLE")

    async def completed_context(self, session_id, run_id, started, context):
        if not self.passed:
            raise ValueError("PRACTICE_MAIN_NOT_PASSED")
        return {
            "run_id": run_id,
            "main_source": "correct main source",
            "failures": [value for at, value in self.records if at >= started],
        }


class Model:
    def __init__(self):
        self.calls = []

    async def generate(self, prompt, payload, schema, context, logical_id):
        self.calls.append(copy.deepcopy(payload))
        if "message" in schema["properties"]:
            return {
                "message": "你把主关里学会的数组配对和分级判断用到了新的数据上，变式代码通过了公开数据和边界验证。这次练习展示了规则可以在不同农田里重复使用。"
            }
        return {k: copy.deepcopy(PROBLEM[k]) for k in schema["required"]}


class Judge:
    def __init__(self):
        self.calls = 0

    async def grade(self, problem, source):
        self.calls += 1
        return {"correct": source == "correct", "message": "checked", "stage": "TEST"}


class Speech:
    def __init__(self):
        self.calls = 0
        self.fail = False

    async def synthesize(self, identity, text):
        self.calls += 1
        if self.fail:
            raise ValueError("SPEECH_UNAVAILABLE")
        return b"\0\0" * 48


def setup():
    reads, model, judge, speech = Reads(), Model(), Judge(), Speech()
    context = SimpleNamespace(actor=SimpleNamespace(tenant_id="tenant", actor_id="student"))
    return BugPractice(reads, model, judge, speech), context


def test_main_success_practice_answer_then_book_and_audio():
    async def run():
        service, context = setup()
        entry = "a" * 32
        await service.start("session_test", entry, context)
        assert (await service.status("session_test", entry, context))["phase"] == "WAITING_MAIN"
        with pytest.raises(ValueError, match="MAIN_NOT_PASSED"):
            await service.prepare("session_test", entry, "run_1", context)
        assert not service.model.calls
        service.reads.passed = True
        problem = await service.prepare("session_test", entry, "run_1", context)
        assert (await service.status("session_test", entry, context))["phase"] == "CHALLENGE_READY"
        assert (
            problem["failure_count"] == 0
        )  # Five historical failures cannot leak into this entry.
        assert await service.prepare("session_test", entry, "run_1", context) == problem
        assert len(service.model.calls) == 1
        with pytest.raises(ValueError, match="NOT_PASSED"):
            await service.summary("session_test", entry, problem["challenge_id"], context)
        wrong = await service.answer(
            "session_test", entry, problem["challenge_id"], "b" * 32, "wrong", context
        )
        assert not wrong["correct"] and wrong["attempts"] == 1
        assert (
            await service.answer(
                "session_test", entry, problem["challenge_id"], "b" * 32, "wrong", context
            )
            == wrong
        )
        assert service.judge.calls == 1
        with pytest.raises(ValueError, match="ANSWER_CONFLICT"):
            await service.answer(
                "session_test", entry, problem["challenge_id"], "b" * 32, "correct", context
            )
        assert service.speech.calls == 0
        correct = await service.answer(
            "session_test", entry, problem["challenge_id"], "c" * 32, "correct", context
        )
        assert correct["correct"] and correct["attempts"] == 2
        service.speech.fail = True
        with pytest.raises(ValueError, match="SPEECH_UNAVAILABLE"):
            await service.summary("session_test", entry, problem["challenge_id"], context)
        assert len(service.model.calls) == 2
        assert (await service.status("session_test", entry, context))["phase"] == "SUMMARY_PENDING"
        service.speech.fail = False
        result = await service.summary("session_test", entry, problem["challenge_id"], context)
        assert result["audio_base64"] and result["message"]
        status = await service.start("session_test", entry, context)
        assert status["phase"] == "COMPLETED" and status["attempts"] == 2
        assert await service.prepare("session_test", entry, "run_1", context) == problem
        with pytest.raises(ValueError, match="RUN_CONFLICT"):
            await service.prepare("session_test", entry, "another_run", context)
        with pytest.raises(ValueError, match="ALREADY_PASSED"):
            await service.answer(
                "session_test", entry, problem["challenge_id"], "d" * 32, "correct", context
            )
        assert len(service.model.calls) == 2  # One challenge and one summary in this game.
        assert (
            len(service.model.calls) == 2
        )  # Audio retry never rewrites the summary or reruns code.
        assert len(service.model.calls[-1]["attempts"]) == 2
        assert service.model.calls[-1]["practice_source"] == "correct"

    asyncio.run(run())


def test_new_entry_isolates_history_and_cannot_read_another_actor():
    async def run():
        service, context = setup()
        await service.start("session_test", "a" * 32, context)
        service.reads.records.append((datetime.now(UTC), "this entry's mistake"))
        service.reads.passed = True
        first = await service.prepare("session_test", "a" * 32, "run_1", context)
        assert first["failure_count"] == 1
        await service.start("session_test", "b" * 32, context)
        second = await service.prepare("session_test", "b" * 32, "run_2", context)
        assert second["failure_count"] == 0 and first["challenge_id"] != second["challenge_id"]
        stranger = SimpleNamespace(actor=SimpleNamespace(tenant_id="tenant", actor_id="stranger"))
        with pytest.raises(ValueError, match="ENTRY_EXPIRED"):
            await service.prepare("session_test", "a" * 32, "run_1", stranger)

    asyncio.run(run())


def test_concurrent_prepares_publish_only_one_challenge_per_game():
    async def run():
        service, context = setup()
        service.reads.passed = True
        await service.start("session_test", "a" * 32, context)
        problems = await asyncio.gather(
            *[service.prepare("session_test", "a" * 32, "run_1", context) for _ in range(5)]
        )
        assert len({p["challenge_id"] for p in problems}) == 1
        assert len(service.model.calls) == 1

    asyncio.run(run())


def test_summary_retry_uses_new_dispatch_after_rejected_model_output():
    async def run():
        service, context = setup()
        service.reads.passed = True
        await service.start("session_test", "a" * 32, context)
        problem = await service.prepare("session_test", "a" * 32, "run_1", context)
        await service.answer(
            "session_test", "a" * 32, problem["challenge_id"], "b" * 32, "correct", context
        )
        original = service.model.generate
        identities = []

        async def generate(*args):
            identities.append(args[-1])
            if len(identities) == 1:
                raise ValueError("PRACTICE_MODEL_UNAVAILABLE")
            return await original(*args)

        service.model.generate = generate
        with pytest.raises(ValueError, match="MODEL_UNAVAILABLE"):
            await service.summary("session_test", "a" * 32, problem["challenge_id"], context)
        assert (await service.status("session_test", "a" * 32, context))[
            "phase"
        ] == "SUMMARY_PENDING"
        result = await service.summary("session_test", "a" * 32, problem["challenge_id"], context)
        assert result["audio_base64"] and identities[0] != identities[1]
        assert service.judge.calls == 1
        assert service.model.calls[-1]["attempt_count"] == 1
        assert service.model.calls[-1]["attempts"][0]["source"] == "correct"

    asyncio.run(run())


def test_problem_keeps_difficulty_and_has_executable_data_contract():
    from yaya_agent_runtime.bug_practice import exercise_data

    generated = [exercise_data(str(i)) for i in range(256)]
    for i, data in enumerate(generated):
        validate_problem({**PROBLEM, **data})
        assert data == exercise_data(str(i))
    assert len({tuple(data["moisture"]) for data in generated}) == 256
    validate_problem(PROBLEM)
    assert "int moisture[8]" in starter_source(PROBLEM)
    assert "cin >> target[i]" in starter_source(PROBLEM)
    assert (
        expected_output([50, 30, 31, 29], [50, 60, 60, 60]) == "WATER 1 2\nWATER 2 1\nWATER 3 2\n"
    )
    changed = copy.deepcopy(PROBLEM)
    changed["target"] = [90] * 8
    with pytest.raises(ValueError, match="DIFFICULTY"):
        validate_problem(changed)


def test_invalid_problem_is_repaired_before_publication():
    async def run():
        service, context = setup()
        original_generate = service.model.generate

        async def generate(*args):
            value = await original_generate(*args)
            if len(service.model.calls) == 1:
                value["title"] = ""
            return value

        service.model.generate = generate
        service.reads.passed = True
        await service.start("session_test", "a" * 32, context)
        problem = await service.prepare("session_test", "a" * 32, "run_1", context)
        validate_problem({k: problem[k] for k in PROBLEM})
        assert service.model.calls[1]["previous_draft"]["title"] == ""
        assert "repair" in service.model.calls[1]

    asyncio.run(run())


def test_problem_numbers_are_supplied_before_model_and_published_unchanged():
    async def run():
        service, context = setup()
        original = service.model.generate

        async def author(prompt, payload, schema, context, logical_id):
            assert set(schema["required"]) == {"title", "brief", "focus"}
            validate_problem({**PROBLEM, **payload["exercise_data"]})
            return await original(prompt, payload, schema, context, logical_id)

        service.model.generate = author
        service.reads.passed = True
        await service.start("session_test", "a" * 32, context)
        value = await service.prepare("session_test", "a" * 32, "run_1", context)
        assert value["moisture"] == service.model.calls[0]["exercise_data"]["moisture"]
        assert value["target"] == service.model.calls[0]["exercise_data"]["target"]
        assert len(service.model.calls) == 1

    asyncio.run(run())


def test_http_auth_gating_and_invalid_requests():
    from pathlib import Path

    from fastapi.testclient import TestClient

    from walnut_backend.api.app import create_app
    from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

    app = create_app(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=Path(__file__).resolve().parents[2] / "contract-release.json",
        )
    )
    headers = {
        "Authorization": "Bearer tenant_yaya:student_0001",
        "X-Request-Id": "req_practice_test_0001",
        "X-Trace-Id": "trace_practice_test_0001",
        "X-Correlation-Id": "corr_practice_test_0001",
        "X-Schema-Version": "1.0.0",
    }
    base = "/product-experience/v1/sessions/session_test/practice-entries/" + "a" * 32 + "/"
    with TestClient(app) as client:
        service, _ = setup()
        app.state.bug_practice = service
        assert (
            client.post(
                base + "start",
                headers={k: v for k, v in headers.items() if k != "Authorization"},
                json={},
            ).status_code
            == 401
        )
        assert not service.entries
        assert (
            client.post(base + "start", headers=headers, json={}).json()["phase"] == "WAITING_MAIN"
        )
        assert client.post(base + "summary", headers=headers, json={}).json() == {
            "code": "PRACTICE_NOT_PASSED",
            "retryable": False,
        }
        assert not service.model.calls and not service.speech.calls
        denied = client.post(
            base + "status",
            headers={**headers, "Authorization": "Bearer tenant_yaya:student_0002"},
            json={},
        )
        assert denied.status_code == 410
        invalid = client.post(base + "answer", headers=headers, content=b"{")
        assert invalid.status_code == 400 and invalid.json()["code"] == "PRACTICE_REQUEST_INVALID"
        duplicate = client.post(base + "start", headers=headers, content=b'{"x":1,"x":2}')
        assert duplicate.status_code == 400
        service.reads.passed = True
        problem = client.post(base + "prepare", headers=headers, json={"run_id": "run_1"}).json()
        bundle = problem["starter_skill"]["source_bundle"]
        assert bundle["language"] == "CPP20" and bundle["entrypoint"] == "main.cpp"
        payload = {"challenge_id": problem["challenge_id"], "source_bundle": bundle}
        answer_headers = {**headers, "Idempotency-Key": "b" * 32}
        first = client.post(base + "answer", headers=answer_headers, json=payload)
        assert first.status_code == 200 and first.json()["attempts"] == 1
        assert (
            client.post(base + "answer", headers=answer_headers, json=payload).json()
            == first.json()
        )
        assert service.judge.calls == 1
        bundle["files"][0]["content"] = "tampered"
        assert (
            client.post(base + "answer", headers=answer_headers, json=payload).json()["code"]
            == "PRACTICE_SOURCE_INVALID"
        )
        entry = next(iter(service.entries.values()))
        entry.started -= timedelta(hours=7)
        assert client.post(base + "start", headers=headers, json={}).status_code == 410


def test_configuration_failure_is_not_retryable_and_file_repair_preserves_pass(
    monkeypatch, tmp_path
):
    import base64
    import json
    from pathlib import Path

    import httpx
    from fastapi.testclient import TestClient

    from walnut_backend.adapters.doubao_tts import BookSpeech
    from walnut_backend.api.app import create_app
    from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

    missing = tmp_path / "book.key"
    monkeypatch.setenv("YAYA_BOOK_TTS_API_KEY_FILE", str(missing))
    monkeypatch.delenv("YAYA_BOOK_TTS_API_KEY", raising=False)
    provider_calls = []

    def provider(request):
        provider_calls.append(request)
        return httpx.Response(
            200,
            text="data: "
            + json.dumps(
                {
                    "code": 0,
                    "data": base64.b64encode(b"\x01\x00" * 240).decode(),
                }
            )
            + '\n\ndata: {"code":20000000}\n\n',
        )

    app = create_app(
        Settings.for_test(
            contract_path=DEFAULT_CONTRACT_PATH,
            contract_release_path=Path(__file__).resolve().parents[2] / "contract-release.json",
        )
    )
    headers = {
        "Authorization": "Bearer tenant_yaya:student_0001",
        "X-Schema-Version": "1.0.0",
        "X-Request-Id": "req_speech_config_0001",
        "X-Trace-Id": "trace_speech_config_0001",
        "X-Correlation-Id": "corr_speech_config_0001",
    }
    url = "/product-experience/v1/sessions/session_test/practice-entries/" + "a" * 32 + "/"
    with TestClient(app) as client:
        service, _ = setup()
        service.speech = BookSpeech(httpx.MockTransport(provider))
        app.state.bug_practice = service
        client.post(url + "start", headers=headers, json={}).raise_for_status()
        service.reads.passed = True
        challenge = client.post(url + "prepare", headers=headers, json={"run_id": "run_1"}).json()
        body = {"challenge_id": challenge["challenge_id"]}
        answer = client.post(
            url + "answer",
            headers=headers,
            json={**body, "answer_id": "b" * 32, "source": "correct"},
        )
        assert answer.json()["correct"]
        for _ in range(2):
            failed = client.post(url + "summary", headers=headers, json=body)
            assert failed.status_code == 503
            assert failed.json() == {
                "code": "BOOK_SPEECH_CONFIGURATION_INVALID",
                "retryable": False,
            }
        assert not provider_calls
        assert client.post(url + "status", headers=headers, json={}).json()["passed"]
        missing.write_text("test-only-key")
        recovered = client.post(url + "summary", headers=headers, json=body)
        assert recovered.status_code == 200 and recovered.json()["audio_base64"]
        assert service.judge.calls == 1 and len(service.model.calls) == 2
        assert len(provider_calls) == 1
