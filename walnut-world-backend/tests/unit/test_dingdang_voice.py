"""Exercise the public voice route and real Doubao codec without the network."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from yaya_agent_runtime.adapters.doubao_realtime import DoubaoRealtimeAdapter, DoubaoRealtimeConfig
from yaya_agent_runtime.voice import VoiceError

from walnut_backend.api.app import create_app
from walnut_backend.bootstrap import DEFAULT_CONTRACT_PATH, Settings

PATH = "/product-experience/v1/sessions/session_voice_demo/dingdang-voice"
TOKEN = "tenant_yaya:student_voice"


class ContextReader:
    def __init__(self):
        self.calls = []
        self.run_revision = None
        self.run_status = ""
        self.revision_reads = 0

    async def latest_run_revision(self, session_id, context):
        self.revision_reads += 1
        return self.run_revision

    async def load(self, session_id, context, client_context):
        self.calls.append((session_id, context.actor, client_context))
        if context.actor.actor_id != "student_voice":
            raise VoiceError("VOICE_SESSION_UNAVAILABLE")
        return "关卡：浇水；当前代码：" + client_context.get("code", "") + self.run_status


class ProviderSocket:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.closed = False

    async def send(self, raw):
        value = json.loads(raw)
        self.sent.append(value)
        kind = value["type"]
        if kind == "session.create":
            self.incoming.put_nowait({"type": "session.created"})
        elif kind == "session.update":
            self.incoming.put_nowait({"type": "session.updated"})
        elif kind == "session.close":
            self.incoming.put_nowait({"type": "session.closed"})
        elif kind == "input_audio_buffer.append":
            for event in (
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "代码怎么改？",
                },
                {
                    "type": "response.function_call_arguments.done",
                    "items": [
                        {"call_id": "call_context", "name": "get_game_context", "arguments": "{}"}
                    ],
                },
            ):
                self.incoming.put_nowait(event)
        elif kind == "conversation.item.create":
            self.incoming.put_nowait(
                {
                    "type": "response.output_text.delta",
                    "delta": value["items"][0]["content"][0]["text"],
                }
            )
            self.incoming.put_nowait(
                {
                    "type": "response.output_audio.delta",
                    "delta": base64.b64encode(b"\x01\x02" * 240).decode(),
                    "response_id": "response_demo",
                }
            )
        elif kind == "response.cancel":
            self.incoming.put_nowait({"type": "response.canceled", "response_id": "response_demo"})

    async def recv(self):
        return json.dumps(await self.incoming.get(), ensure_ascii=False)

    @asynccontextmanager
    async def connect(self, config):
        try:
            yield self
        finally:
            self.closed = True


@pytest.fixture
def harness():
    settings = Settings.for_test(
        contract_path=DEFAULT_CONTRACT_PATH,
        contract_release_path=Path(__file__).resolve().parents[2] / "contract-release.json",
    )
    app = create_app(settings)
    provider = ProviderSocket()
    reader = ContextReader()
    with TestClient(app) as client:
        app.state.dingdang_voice_context = reader
        app.state.dingdang_voice_factory = lambda: DoubaoRealtimeAdapter(
            DoubaoRealtimeConfig(api_key="offline-test-key"), provider.connect
        )
        yield client, provider, reader


def test_continuous_audio_context_refresh_interrupt_and_close(harness):
    client, provider, reader = harness
    with client.websocket_connect(PATH) as ws:
        ws.send_json({"type": "start", "token": TOKEN, "context": {"code": "old code"}})
        assert ws.receive_json() == {
            "type": "voice.ready",
            "input_rate": 16000,
            "output_rate": 24000,
        }
        ws.send_json(
            {"type": "context", "context": {"code": "new code", "observation": "本地候选失败"}}
        )
        assert ws.receive_json()["type"] == "session.updated"
        assert "new code" in provider.sent[-1]["session"]["instructions"]
        ws.send_bytes(b"\x01\x00" * 320)
        assert ws.receive_json()["text"] == "代码怎么改？"
        assert "new code" in ws.receive_json()["text"]
        audio = ws.receive_json()
        assert base64.b64decode(audio["audio"]) == b"\x01\x02" * 240
        assert audio["response_id"] == "response_demo"
        ws.send_json({"type": "interrupt"})
        assert ws.receive_json()["type"] == "response.canceled"
        # A second input is accepted on the same connection, without committing
        # a turn or toggling mute between utterances.
        ws.send_bytes(bytes(640))
        assert ws.receive_json()["type"].endswith("transcription.completed")
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "close"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert provider.closed
    kinds = [item["type"] for item in provider.sent]
    assert kinds.count("session.create") == 1
    assert kinds.count("input_audio_unmute.commit") == 1
    assert kinds.count("input_audio_buffer.append") == 2
    assert kinds[-1] == "session.close"
    assert reader.calls[0][1].actor_id == "student_voice"
    assert reader.calls[-1][2]["code"] == "new code"
    instructions = provider.sent[0]["session"]["instructions"]
    assert "old code" in instructions and TOKEN not in instructions


def test_open_voice_pushes_new_run_without_client_context_or_model_tool(harness, monkeypatch):
    from walnut_backend.api.routes import dingdang_voice

    monkeypatch.setattr(dingdang_voice, "CONTEXT_POLL_SECONDS", 0.02, raising=False)
    client, provider, reader = harness

    async def wait_for_update():
        async with asyncio.timeout(0.5):
            while not any(item["type"] == "session.update" for item in provider.sent):
                await asyncio.sleep(0.01)

    with client.websocket_connect(PATH) as ws:
        ws.send_json({"type": "start", "token": TOKEN, "context": {"code": "current code"}})
        assert ws.receive_json()["type"] == "voice.ready"
        client.portal.call(asyncio.sleep, 0.08)
        assert len(reader.calls) == 1, "unchanged state must not reload the entire context"
        reader.run_revision = ("run_new", "2026-09-09T10:00:00Z", None)
        reader.run_status = "；本次任务已完成；成长总结生成中"
        client.portal.call(wait_for_update)
        assert ws.receive_json()["type"] == "session.updated"
        instructions = provider.sent[-1]["session"]["instructions"]
        assert "本次任务已完成" in instructions
        assert "current code" in instructions
        assert len(reader.calls) == 2
        client.portal.call(asyncio.sleep, 0.08)
        assert len(reader.calls) == 2, "unchanged Run must not trigger repeated refreshes"
        assert not any(item["type"] == "conversation.item.create" for item in provider.sent)
        ws.send_json({"type": "close"})
    assert provider.closed
    reads_after_close = reader.revision_reads
    client.portal.call(asyncio.sleep, 0.06)
    assert reader.revision_reads == reads_after_close, "closed voice must stop polling"


def test_background_run_refresh_does_not_overwrite_newer_editor_context(harness, monkeypatch):
    from walnut_backend.api.routes import dingdang_voice

    monkeypatch.setattr(dingdang_voice, "CONTEXT_POLL_SECONDS", 0.01)
    client, provider, reader = harness
    started = asyncio.Event()
    release = asyncio.Event()
    original_load = reader.load

    async def blocked_load(session_id, context, client_context):
        if client_context.get("code") == "old code":
            started.set()
            await release.wait()
        return await original_load(session_id, context, client_context)

    async def wait_for_updates():
        async with asyncio.timeout(1):
            while sum(item["type"] == "session.update" for item in provider.sent) < 2:
                await asyncio.sleep(0.01)

    with client.websocket_connect(PATH) as ws:
        ws.send_json({"type": "start", "token": TOKEN, "context": {"code": "old code"}})
        assert ws.receive_json()["type"] == "voice.ready"
        monkeypatch.setattr(reader, "load", blocked_load)
        reader.run_revision = ("run_new", "new timestamp", None)
        client.portal.call(asyncio.wait_for, started.wait(), 1)
        ws.send_json({"type": "context", "context": {"code": "new code"}})
        client.portal.call(asyncio.sleep, 0.03)
        client.portal.call(release.set)
        client.portal.call(wait_for_updates)
        assert ws.receive_json()["type"] == "session.updated"
        assert ws.receive_json()["type"] == "session.updated"
        final_instructions = provider.sent[-1]["session"]["instructions"]
        assert "new code" in final_instructions
        assert "old code" not in final_instructions
        ws.send_json({"type": "close"})


@pytest.mark.parametrize(
    "start,code",
    [
        ({"type": "start"}, "VOICE_AUTH_FAILED"),
        ({"type": "start", "token": "tenant_yaya:student_other"}, "VOICE_SESSION_UNAVAILABLE"),
        ({"type": "ask", "token": TOKEN}, "VOICE_INVALID_START"),
        ({"type": "start", "token": TOKEN, "context": {"code": 17}}, "VOICE_INVALID_CONTEXT"),
    ],
)
def test_invalid_start_never_opens_provider(harness, start, code):
    client, provider, _ = harness
    with client.websocket_connect(PATH) as ws:
        ws.send_json(start)
        assert ws.receive_json() == {"type": "voice.error", "code": code}
    assert provider.sent == []


def test_invalid_audio_closes_provider(harness):
    client, provider, _ = harness
    with client.websocket_connect(PATH) as ws:
        ws.send_json({"type": "start", "token": TOKEN})
        ws.receive_json()
        ws.send_bytes(b"bad")
        assert ws.receive_json() == {"type": "voice.error", "code": "VOICE_AUDIO_REQUIRES_20MS_PCM"}
    assert provider.closed


def test_provider_error_is_redacted_and_other_routes_keep_their_protocol(harness):
    client, provider, _ = harness
    with client.websocket_connect(PATH) as ws:
        ws.send_json({"type": "start", "token": TOKEN})
        ws.receive_json()
        client.portal.call(
            provider.incoming.put, {"type": "error", "message": "private upstream response"}
        )
        assert ws.receive_json() == {"type": "voice.error", "code": "VOICE_PROVIDER_ERROR"}
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect("/v1/realtime"):
            pass
    assert rejected.value.code == 4400


def test_disabled_voice_does_not_break_backend_startup(harness, monkeypatch):
    from walnut_backend.api.routes.dingdang_voice import configured_voice

    client, _, _ = harness
    monkeypatch.setenv("YAYA_VOICE_MODE", "disabled")
    client.app.state.dingdang_voice_factory = configured_voice
    with client.websocket_connect(PATH) as ws:
        ws.send_json({"type": "start", "token": TOKEN})
        assert ws.receive_json() == {"type": "voice.error", "code": "VOICE_DISABLED"}
