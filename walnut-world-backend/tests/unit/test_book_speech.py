"""Book speech completion, exact text/voice, and authorization boundaries."""

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from yaya_agent_contracts import Success

from walnut_backend.adapters.doubao_tts import SPEAKER, BookSpeech, BookSpeechError
from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings


def test_complete_audio_exact_text_voice_and_cache(monkeypatch):
    monkeypatch.setenv("YAYA_VOICE_MODE", "doubao")
    monkeypatch.setenv("YAYA_DOUBAO_VOICE_API_KEY", "test-private-key")
    monkeypatch.delenv("YAYA_DOUBAO_VOICE_API_KEY_FILE", raising=False)
    calls = []

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["req_params"]["speaker"] == SPEAKER
        assert body["req_params"]["text"] == "你用循环检查了地块。"
        return httpx.Response(
            200,
            text="data: "
            + json.dumps({"code": 0, "data": base64.b64encode(b"\x01\x00" * 240).decode()})
            + '\n\ndata: {"code":20000000}\n\n',
        )

    async def run():
        service = BookSpeech(httpx.MockTransport(provider))
        first = await service.synthesize("student:interaction", "你用循环检查了地块。")
        second = await service.synthesize("student:interaction", "你用循环检查了地块。")
        assert first == second == b"\x01\x00" * 240
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "events,code",
    [
        ([{"code": 0, "data": "AQI="}], "BOOK_SPEECH_INCOMPLETE"),
        (
            [{"code": 0, "data": "AQI="}, {"code": 45000030, "message": "secret provider detail"}],
            "BOOK_SPEECH_RESOURCE_NOT_GRANTED",
        ),
        ([{"code": 20000000}], "BOOK_SPEECH_INCOMPLETE"),
    ],
)
def test_partial_or_rejected_audio_never_publishes(monkeypatch, events, code):
    monkeypatch.setenv("YAYA_VOICE_MODE", "doubao")
    monkeypatch.setenv("YAYA_DOUBAO_VOICE_API_KEY", "private-key")
    monkeypatch.delenv("YAYA_DOUBAO_VOICE_API_KEY_FILE", raising=False)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, text="".join("data: " + json.dumps(e) + "\n\n" for e in events)
        )
    )

    async def run():
        with pytest.raises(BookSpeechError) as error:
            await BookSpeech(transport).synthesize("actor:interaction", "本次总结")
        assert error.value.code == code
        assert "secret" not in str(error.value)

    asyncio.run(run())


def test_route_reads_authorized_persisted_text_before_synthesis():
    settings = Settings.for_test(
        contract_path=DEFAULT_CONTRACT_PATH,
        contract_release_path=Path(__file__).resolve().parents[2] / "contract-release.json",
    )
    app = create_app(settings)
    calls = []

    class Reads:
        async def get(self, session, interaction, context):
            calls.append((session, interaction, context.actor.actor_id))
            return Success(
                {
                    "role": "book_agent",
                    "response_type": "growth_summary",
                    "feedback": {"message": "已发布的总结"},
                }
            )

    class Speech:
        async def synthesize(self, identity, text):
            assert text == "已发布的总结"
            return b"\x01\x00" * 240

    headers = {
        "Authorization": "Bearer tenant_yaya:student_0001",
        "X-Request-Id": "req_book_speech_0001",
        "X-Trace-Id": "trace_book_speech_0001",
        "X-Correlation-Id": "corr_book_speech_0001",
        "X-Schema-Version": "1.0.0",
    }
    path = "/product-experience/v1/sessions/session_book_0001/agent-interactions/interaction_book_0001/speech"
    with TestClient(app) as client:
        app.state.product_interactions = Reads()
        app.state.book_speech = Speech()
        response = client.post(path, headers=headers, json={"text": "client replacement"})
        assert response.status_code == 200
        assert response.json()["speaker"] == SPEAKER
        assert len(calls) == 1
        denied = client.post(
            path, headers={k: v for k, v in headers.items() if k != "Authorization"}
        )
        assert denied.status_code == 401
        assert len(calls) == 1
