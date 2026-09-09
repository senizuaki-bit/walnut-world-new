from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "python"),
    str(Path(__file__).resolve().parent),
]

from agent_runtime_fixtures import make_context, make_event, make_operation  # noqa: E402
from test_agent_backend_config import _environment  # noqa: E402
from yaya_agent_backend.config import ProductionSettings  # noqa: E402
from yaya_agent_runtime import AgentContextError, AgentHub, RoleRouter  # noqa: E402
from yaya_agent_runtime.adapters.doubao_realtime import (  # noqa: E402
    DoubaoRealtimeAdapter,
    DoubaoRealtimeConfig,
)
from yaya_agent_runtime.hub import RealtimeVoiceAuthority  # noqa: E402
from yaya_agent_runtime.voice import (  # noqa: E402
    VoiceError,
    VoiceTool,
    VoiceToolCall,
)


class Socket:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.closed = False
        self.queue({"type": "session.created", "session": {"id": "provider-session"}})

    def queue(self, event):
        self.incoming.put_nowait(json.dumps(event))

    async def send(self, message):
        event = json.loads(message)
        self.sent.append(event)
        if event["type"] == "session.close":
            self.queue({"type": "session.closed"})

    async def recv(self):
        return await self.incoming.get()


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, socket=None):
        self.socket = socket or Socket()

        @asynccontextmanager
        async def connector(config):
            self.assertEqual(config.api_key, "test-secret")
            try:
                yield self.socket
            finally:
                self.socket.closed = True

        return DoubaoRealtimeAdapter(
            DoubaoRealtimeConfig("test-secret", close_timeout=0.05, speed=12, loudness=-5),
            connector,
        )

    async def test_duplex_audio_transcripts_controls_and_graceful_close(self):
        adapter = self.adapter()
        async with adapter.open_session("学习伙伴") as session:
            create = self.socket.sent[0]
            self.assertEqual(create["session"]["model"], "1.2.6.1")
            self.assertEqual(create["session"]["audio"]["output"]["speed"], 12)
            self.assertEqual(create["session"]["audio"]["output"]["loudness"], -5)
            self.assertEqual(
                create["session"]["audio"]["output"]["format"],
                {"type": "pcm_s16le", "rate": 24000},
            )
            self.assertEqual(create["session"]["tools"], [])
            await session.set_muted(False)
            pcm = b"\x01\x00" * 320
            await session.send_audio(pcm)
            self.assertEqual(base64.b64decode(self.socket.sent[-1]["audio"]), pcm)
            await session.commit_audio()
            await session.interrupt()
            await session.set_muted(True)
            await session.speak("你好")
            self.socket.queue(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "我该怎么做",
                }
            )
            self.socket.queue(
                {
                    "type": "response.output_audio.delta",
                    "delta": base64.b64encode(pcm).decode(),
                    "response_id": "r1",
                }
            )
            stream = session.events()
            self.assertEqual((await anext(stream)).text, "我该怎么做")
            audio = await anext(stream)
            self.assertEqual(audio.audio, pcm)
            self.assertEqual(audio.response_id, "r1")
            await stream.aclose()
        self.assertTrue(self.socket.closed)
        self.assertEqual(self.socket.sent[-1]["type"], "session.close")
        self.assertTrue(self.socket.incoming.empty())
        self.assertNotIn("test-secret", json.dumps(self.socket.sent))

    async def test_muted_and_wrong_size_audio_rejected_before_send(self):
        async with self.adapter().open_session("instructions") as session:
            for pcm, code in (
                (b"x", "VOICE_AUDIO_REQUIRES_20MS_PCM"),
                (b"\0" * 640, "VOICE_MICROPHONE_MUTED"),
            ):
                with self.assertRaisesRegex(VoiceError, code):
                    await session.send_audio(pcm)
            self.assertEqual(len(self.socket.sent), 2)

    async def test_context_update_preserves_audio_tools_and_open_microphone(self):
        tool = VoiceTool("get_game_context", "read context", {"type": "object"})
        async with self.adapter().open_session("old code", (tool,)) as session:
            original = self.socket.sent[0]["session"]
            await session.set_muted(False)
            await session.update_instructions("new code")
            update = self.socket.sent[-1]
            self.assertEqual(update["type"], "session.update")
            self.assertEqual(update["session"]["instructions"], "new code")
            self.assertEqual(update["session"]["audio"], original["audio"])
            self.assertEqual(update["session"]["tools"], original["tools"])
            await session.send_audio(bytes(640))
            self.assertEqual(self.socket.sent[-1]["type"], "input_audio_buffer.append")
            with self.assertRaisesRegex(VoiceError, "VOICE_INVALID_INSTRUCTIONS"):
                await session.update_instructions("")

    async def test_provider_error_is_redacted_and_socket_closed(self):
        async with self.adapter().open_session("instructions") as session:
            self.socket.queue({"type": "error", "error": {"message": "test-secret"}})
            with self.assertRaisesRegex(VoiceError, "VOICE_PROVIDER_ERROR") as raised:
                await anext(session.events())
            self.assertNotIn("test-secret", str(raised.exception))
        self.assertTrue(self.socket.closed)

    async def test_malformed_audio_and_binary_frames_rejected(self):
        for frame in (
            json.dumps({"type": "response.output_audio.delta", "delta": "!@#$"}),
            b"binary",
            "[]",
            "{broken",
        ):
            with self.subTest(frame=frame):
                async with self.adapter().open_session("instructions") as session:
                    self.socket.incoming.put_nowait(frame)
                    with self.assertRaises(VoiceError):
                        await anext(session.events())

    async def test_idle_stream_waits_instead_of_failing_and_cancel_releases_receiver(self):
        # A full-duplex session is silent whenever nobody is speaking, so an
        # idle receive must not fail the session; the WebSocket keepalive is
        # what detects a dead peer.
        async with self.adapter().open_session("instructions") as session:
            idle = asyncio.create_task(anext(session.events()))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(idle), 0.2)
            self.assertFalse(idle.done())
            idle.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await idle
            task = asyncio.create_task(anext(session.events()))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_single_receiver_enforced(self):
        async with self.adapter().open_session("instructions") as session:
            first = asyncio.create_task(anext(session.events()))
            await asyncio.sleep(0)
            with self.assertRaisesRegex(VoiceError, "VOICE_SINGLE_RECEIVER_REQUIRED"):
                await anext(session.events())
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first

    async def test_failed_start_closes_transport(self):
        socket = Socket()
        socket.incoming.get_nowait()
        socket.queue({"type": "error", "message": "test-secret"})
        with self.assertRaisesRegex(VoiceError, "VOICE_PROVIDER_ERROR"):
            async with self.adapter(socket).open_session("instructions"):
                self.fail("must not yield")
        self.assertTrue(socket.closed)

    async def test_caller_exception_preserved_and_session_closed(self):
        with self.assertRaisesRegex(ValueError, "caller failure"):
            async with self.adapter().open_session("instructions"):
                raise ValueError("caller failure")
        self.assertTrue(self.socket.closed)

    def test_config_defaults_key_file_validation_and_redaction(self):
        self.assertIsNone(DoubaoRealtimeConfig.from_env({}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.key"
            path.write_text("test-secret\n", encoding="utf-8")
            env = {"YAYA_VOICE_MODE": "doubao", "YAYA_DOUBAO_VOICE_API_KEY_FILE": str(path)}
            config = DoubaoRealtimeConfig.from_env(env)
            self.assertEqual(config.api_key, "test-secret")
            self.assertEqual((config.speed, config.loudness), (0, 0))
            tuned = DoubaoRealtimeConfig.from_env(
                {**env, "YAYA_DOUBAO_VOICE_SPEED": "12", "YAYA_DOUBAO_VOICE_LOUDNESS": "-5"}
            )
            self.assertEqual((tuned.speed, tuned.loudness), (12, -5))
            for field in ("speed", "loudness"):
                for invalid in (-51, 101, float("nan"), float("inf"), True):
                    with self.assertRaises(ValueError):
                        replace(config, **{field: invalid})
            self.assertNotIn("test-secret", repr(config))
            settings = ProductionSettings.from_env({**_environment(), **env})
            self.assertEqual(settings.voice_config, config)
            self.assertNotIn("test-secret", repr(settings))
            with self.assertRaises(ValueError):
                DoubaoRealtimeConfig.from_env({**env, "YAYA_DOUBAO_VOICE_API_KEY": "key"})
        for env in ({"YAYA_VOICE_MODE": "bad"}, {"YAYA_VOICE_MODE": "doubao"}):
            with self.assertRaises(ValueError):
                DoubaoRealtimeConfig.from_env(env)

    async def test_hub_authorizes_context_before_provider_without_durable_effects(self):
        contexts = Mock(build=AsyncMock(return_value=make_context()))
        turns, runtime = Mock(), Mock()
        adapter = self.adapter()
        hub = AgentHub(
            router=RoleRouter(), contexts=contexts, runtime=runtime, turns=turns, voice=adapter
        )
        async with hub.open_voice(make_event("task_started"), make_operation()) as session:
            await session.speak("你好")
        contexts.build.assert_awaited_once()
        self.assertIn(make_context().task.goal, self.socket.sent[0]["session"]["instructions"])
        self.assertEqual(turns.mock_calls, [])
        self.assertEqual(runtime.mock_calls, [])

    async def test_hub_rejects_wrong_actor_command_and_context(self):
        contexts = Mock(build=AsyncMock(side_effect=AgentContextError("DENIED", "denied")))
        provider = Mock()
        hub = AgentHub(
            router=RoleRouter(), contexts=contexts, runtime=Mock(), turns=Mock(), voice=provider
        )
        event = make_event("task_started")
        for bad in (
            replace(event, student_id="student_other"),
            replace(event, command_id="cmd_other_0001"),
            event,
        ):
            with self.assertRaises(AgentContextError):
                async with hub.open_voice(bad, make_operation()):
                    self.fail("must not yield")
        self.assertEqual(provider.mock_calls, [])

    async def test_disabled_voice_preserves_default_hub(self):
        hub = AgentHub(router=RoleRouter(), contexts=Mock(), runtime=Mock(), turns=Mock())
        with self.assertRaisesRegex(VoiceError, "VOICE_DISABLED"):
            async with hub.open_voice(make_event("task_started"), make_operation()):
                self.fail("must not yield")

    async def test_declared_tools_reach_session_create_and_reject_duplicates(self):
        tool = VoiceTool("report_hint", "记录提示", {"type": "object", "properties": {}})
        async with self.adapter().open_session("instructions", [tool]):
            declared = self.socket.sent[0]["session"]["tools"]
            self.assertEqual(
                declared,
                [
                    {
                        "type": "function",
                        "name": "report_hint",
                        "description": "记录提示",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            )
        with self.assertRaisesRegex(VoiceError, "VOICE_DUPLICATE_TOOL"):
            async with self.adapter().open_session("instructions", [tool, tool]):
                self.fail("must not yield")

    async def test_function_call_arguments_surface_and_result_is_paired_by_call_id(self):
        async with self.adapter().open_session("instructions") as session:
            self.socket.queue(
                {
                    "type": "response.function_call_arguments.done",
                    "items": [
                        {
                            "call_id": "call_1",
                            "name": "report_hint",
                            "arguments": '{"hint_level": 2}',
                        }
                    ],
                }
            )
            event = await anext(session.events())
            self.assertEqual(
                event.calls,
                (VoiceToolCall("call_1", "report_hint", '{"hint_level": 2}'),),
            )
            self.assertEqual(event.calls[0].parsed_arguments(), {"hint_level": 2})
            await session.send_tool_result("call_1", "recorded")
            returned = self.socket.sent[-1]
            self.assertEqual(returned["type"], "conversation.item.create")
            self.assertEqual(
                returned["items"],
                [
                    {
                        "call_id": "call_1",
                        "role": "tool",
                        "content": [{"type": "input_text", "text": "recorded"}],
                    }
                ],
            )

    async def test_malformed_function_calls_and_tool_results_rejected(self):
        async with self.adapter().open_session("instructions") as session:
            for items in ([], [{"call_id": "c1"}], "not-a-list"):
                self.socket.queue({"type": "response.function_call_arguments.done", "items": items})
                with self.assertRaisesRegex(VoiceError, "VOICE_INVALID_TOOL_CALL"):
                    await anext(session.events())
            self.socket.queue(
                {
                    "type": "response.function_call_arguments.done",
                    "items": [{"call_id": "c1", "name": "report_hint", "arguments": "{oops"}],
                }
            )
            event = await anext(session.events())
            with self.assertRaisesRegex(VoiceError, "VOICE_TOOL_ARGUMENTS_INVALID"):
                event.calls[0].parsed_arguments()
            with self.assertRaisesRegex(VoiceError, "VOICE_INVALID_TOOL_RESULT"):
                await session.send_tool_result("c1", "   ")
            with self.assertRaisesRegex(VoiceError, "VOICE_INVALID_TOOL_CALL_ID"):
                await session.send_tool_result("", "recorded")

    def test_tool_declaration_rejects_unusable_schemas(self):
        for name, description, parameters in (
            ("", "d", {"type": "object"}),
            ("has space", "d", {"type": "object"}),
            ("report", "", {"type": "object"}),
            ("report", "d", {"type": "string"}),
            ("report", "d", {"type": "object", "properties": {"x": {"pad": "y" * 9000}}}),
        ):
            with self.assertRaises(ValueError):
                VoiceTool(name, description, parameters)

    async def test_voice_authority_needs_no_turn_or_runtime_authority(self):
        contexts = Mock(build=AsyncMock(return_value=make_context()))
        authority = RealtimeVoiceAuthority(
            router=RoleRouter(), contexts=contexts, voice=self.adapter()
        )
        async with authority.open_session(make_event("task_started"), make_operation()) as session:
            await session.speak("你好")
        contexts.build.assert_awaited_once()
        self.assertIn(make_context().task.goal, self.socket.sent[0]["session"]["instructions"])

    async def test_voice_authority_refuses_events_that_route_to_no_role(self):
        authority = RealtimeVoiceAuthority(router=RoleRouter(), contexts=Mock(), voice=Mock())
        with self.assertRaisesRegex(VoiceError, "VOICE_ROUTE_REQUIRED"):
            async with authority.open_session(make_event("compile_succeeded"), make_operation()):
                self.fail("must not yield")


if __name__ == "__main__":
    unittest.main()
