"""Doubao 3.0 / Seeduplex JSON WebSocket protocol (not the legacy binary API)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from ..voice import (
    VoiceError,
    VoiceEvent,
    VoiceSession,
    VoiceTool,
    VoiceToolCall,
    validate_tool_result,
)

ENDPOINT = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"


@dataclass(frozen=True, slots=True)
class DoubaoRealtimeConfig:
    api_key: str = field(repr=False)
    voice: str = "ICL_uranus_zh_male_youmodaye_tob"
    connect_timeout: float = 10.0
    close_timeout: float = 3.0
    max_message_bytes: int = 1_048_576
    speed: float = 0.0
    loudness: float = 0.0

    def __post_init__(self) -> None:
        if not self.api_key.strip() or any(c in self.api_key for c in "\r\n"):
            raise ValueError("Doubao voice requires a nonempty, single-line API key")
        if not self.voice.strip():
            raise ValueError("Doubao voice ID must not be empty")
        for name, value in (("speed", self.speed), ("loudness", self.loudness)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not -50 <= value <= 100
            ):
                raise ValueError(f"voice {name} must be a number between -50 and 100")
        for value in (self.connect_timeout, self.close_timeout):
            if not 0 < value <= 300:
                raise ValueError("voice timeouts must be between 0 and 300 seconds")
        if not 1024 <= self.max_message_bytes <= 4_194_304:
            raise ValueError("voice message limit is out of range")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DoubaoRealtimeConfig | None:
        source = os.environ if env is None else env
        mode = source.get("YAYA_VOICE_MODE", "disabled").strip().lower()
        if mode == "disabled":
            return None
        if mode != "doubao":
            raise ValueError("YAYA_VOICE_MODE must be disabled or doubao")
        key = source.get("YAYA_DOUBAO_VOICE_API_KEY", "").strip()
        key_file = source.get("YAYA_DOUBAO_VOICE_API_KEY_FILE", "").strip()
        if key and key_file:
            raise ValueError("configure only one Doubao voice key source")
        if key_file:
            try:
                key = Path(key_file).read_text(encoding="utf-8-sig").strip()
            except OSError:
                raise ValueError("cannot read Doubao voice API key file") from None
        return cls(
            api_key=key,
            voice=source.get("YAYA_DOUBAO_VOICE", "ICL_uranus_zh_male_youmodaye_tob"),
            speed=float(source.get("YAYA_DOUBAO_VOICE_SPEED", "0")),
            loudness=float(source.get("YAYA_DOUBAO_VOICE_LOUDNESS", "0")),
        )


class VoiceSocket(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...


class VoiceConnector(Protocol):
    def __call__(
        self, config: DoubaoRealtimeConfig
    ) -> AbstractAsyncContextManager[VoiceSocket]: ...


@asynccontextmanager
async def _connect(config: DoubaoRealtimeConfig) -> AsyncGenerator[VoiceSocket]:
    # Optional dependency: text-only Agent installations do not import websockets.
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
    except ImportError:
        raise VoiceError("VOICE_DEPENDENCY_MISSING") from None
    try:
        async with connect(
            ENDPOINT,
            additional_headers={"X-Api-Key": config.api_key},
            open_timeout=config.connect_timeout,
            close_timeout=config.close_timeout,
            max_size=config.max_message_bytes,
            max_queue=16,
            ping_interval=20,
            proxy=None,
        ) as socket:
            yield cast(VoiceSocket, socket)
    except InvalidStatus as error:
        status = error.response.status_code
        code = "VOICE_AUTH_FAILED" if status in {401, 403} else "VOICE_CONNECT_FAILED"
        raise VoiceError(code) from None


FUNCTION_CALL_DONE = "response.function_call_arguments.done"


def _tool_declarations(tools: Sequence[VoiceTool]) -> tuple[dict[str, object], ...]:
    names: set[str] = set()
    declarations: list[dict[str, object]] = []
    for tool in tools:
        if not isinstance(tool, VoiceTool):
            raise VoiceError("VOICE_INVALID_TOOL")
        if tool.name in names:
            raise VoiceError("VOICE_DUPLICATE_TOOL")
        names.add(tool.name)
        declarations.append(
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters),
            }
        )
    return tuple(declarations)


def _tool_calls(event: Mapping[str, object]) -> tuple[VoiceToolCall, ...]:
    """Read the `items` array the provider sends with a completed function call."""

    items = event.get("items")
    if not isinstance(items, list):
        raise VoiceError("VOICE_INVALID_TOOL_CALL")
    calls: list[VoiceToolCall] = []
    for item in items:
        if not isinstance(item, dict):
            raise VoiceError("VOICE_INVALID_TOOL_CALL")
        call_id = item.get("call_id")
        name = item.get("name")
        arguments = item.get("arguments", "")
        if (
            not isinstance(call_id, str)
            or not isinstance(name, str)
            or not isinstance(arguments, str)
        ):
            raise VoiceError("VOICE_INVALID_TOOL_CALL")
        try:
            calls.append(VoiceToolCall(call_id, name, arguments))
        except ValueError:
            raise VoiceError("VOICE_INVALID_TOOL_CALL") from None
    if not calls:
        raise VoiceError("VOICE_INVALID_TOOL_CALL")
    return tuple(calls)


class DoubaoRealtimeAdapter:
    def __init__(self, config: DoubaoRealtimeConfig, connector: VoiceConnector = _connect) -> None:
        self._config = config
        self._connector = connector

    @asynccontextmanager
    async def open_session(
        self,
        instructions: str,
        tools: Sequence[VoiceTool] = (),
    ) -> AsyncGenerator[VoiceSession]:
        if not instructions.strip() or len(instructions) > 16_000:
            raise VoiceError("VOICE_INVALID_INSTRUCTIONS")
        declarations = _tool_declarations(tools)
        async with AsyncExitStack() as stack:
            try:
                socket = await stack.enter_async_context(self._connector(self._config))
                session = _DoubaoSession(socket, self._config)
                await session.start(instructions, declarations)
            except VoiceError:
                raise
            except Exception:
                raise VoiceError("VOICE_CONNECTION_FAILED") from None
            try:
                yield session
            finally:
                await session.close()


class _DoubaoSession:
    def __init__(self, socket: VoiceSocket, config: DoubaoRealtimeConfig) -> None:
        self._socket = socket
        self._config = config
        self._write_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._closed = False
        self._muted = True
        self._receiving = False
        self._session_options: dict[str, object] = {}

    async def _send(self, event_type: str, **fields: object) -> None:
        if self._closed:
            raise VoiceError("VOICE_SESSION_CLOSED")
        event = {"type": event_type, "event_id": f"evt_{uuid4().hex}", **fields}
        try:
            async with self._write_lock:
                async with asyncio.timeout(self._config.connect_timeout):
                    await self._socket.send(json.dumps(event, ensure_ascii=False))
        except Exception:
            raise VoiceError("VOICE_SEND_FAILED") from None

    async def _receive(self, *, deadline_seconds: float | None) -> dict[str, object]:
        """Read one event. `deadline_seconds` is None while streaming.

        A healthy full-duplex session is silent whenever the student is not
        speaking and the model is not replying, so a wall-clock bound on "time
        until the next event" would kill working sessions.  Liveness of an idle
        stream is the WebSocket keepalive's job (ping_interval/ping_timeout);
        only receives that expect a specific prompt reply are bounded here.
        """
        try:
            async with self._read_lock:
                async with asyncio.timeout(deadline_seconds):
                    raw = await self._socket.recv()
            if (
                not isinstance(raw, str)
                or len(raw.encode("utf-8")) > self._config.max_message_bytes
            ):
                raise VoiceError("VOICE_INVALID_FRAME")
            value: object = json.loads(raw)
            if not isinstance(value, dict):
                raise VoiceError("VOICE_INVALID_FRAME")
            event = cast(dict[str, object], value)
            if not isinstance(event.get("type"), str):
                raise VoiceError("VOICE_INVALID_FRAME")
            if event["type"] == "error":
                raise VoiceError("VOICE_PROVIDER_ERROR")
            return event
        except VoiceError:
            raise
        except TimeoutError:
            raise VoiceError("VOICE_RECEIVE_TIMEOUT") from None
        except Exception:
            raise VoiceError("VOICE_RECEIVE_FAILED") from None

    async def start(
        self,
        instructions: str,
        tools: tuple[dict[str, object], ...] = (),
    ) -> None:
        self._session_options = {
            "model": "1.2.6.1",
            "instructions": instructions,
            "audio": {
                "input": {"format": {"type": "pcm", "rate": 16000}},
                "output": {
                    "format": {"type": "pcm_s16le", "rate": 24000},
                    "voice": self._config.voice,
                    "speed": self._config.speed,
                    "loudness": self._config.loudness,
                },
            },
            "tools": list(tools),
        }
        await self._send("session.create", session=self._session_options)
        async with asyncio.timeout(self._config.connect_timeout):
            event = await self._receive(deadline_seconds=self._config.connect_timeout)
            if event["type"] != "session.created":
                raise VoiceError("VOICE_SESSION_NOT_CREATED")
        # No microphone is attached yet. Explicit mute prevents upstream input timeout.
        await self._send("input_audio_mute.commit")

    async def send_audio(self, pcm: bytes) -> None:
        if len(pcm) != 640:
            raise VoiceError("VOICE_AUDIO_REQUIRES_20MS_PCM")
        if self._muted:
            raise VoiceError("VOICE_MICROPHONE_MUTED")
        await self._send("input_audio_buffer.append", audio=base64.b64encode(pcm).decode("ascii"))

    async def set_muted(self, muted: bool) -> None:
        if self._closed:
            raise VoiceError("VOICE_SESSION_CLOSED")
        if muted != self._muted:
            await self._send("input_audio_mute.commit" if muted else "input_audio_unmute.commit")
            self._muted = muted

    async def commit_audio(self) -> None:
        await self._send("input_audio_buffer.commit")

    async def interrupt(self) -> None:
        await self._send("response.cancel")

    async def update_instructions(self, instructions: str) -> None:
        """Refresh context on the same session; events() receives session.updated."""
        if not instructions.strip() or len(instructions) > 16_000:
            raise VoiceError("VOICE_INVALID_INSTRUCTIONS")
        self._session_options = {**self._session_options, "instructions": instructions}
        await self._send("session.update", session=self._session_options)

    async def speak(self, text: str) -> None:
        """Read `text` out verbatim. This is TTS, not a question for the model."""
        if not text.strip() or len(text) > 4000:
            raise VoiceError("VOICE_INVALID_TEXT")
        await self._send("speech_text_buffer.commit", text=text)

    async def send_tool_result(self, call_id: str, text: str) -> None:
        """Return one Function Calling result so the model can continue its reply."""
        if not call_id.strip() or len(call_id) > 128:
            raise VoiceError("VOICE_INVALID_TOOL_CALL_ID")
        await self._send(
            "conversation.item.create",
            items=[
                {
                    "call_id": call_id,
                    "role": "tool",
                    "content": [{"type": "input_text", "text": validate_tool_result(text)}],
                }
            ],
        )

    async def events(self) -> AsyncIterator[VoiceEvent]:
        if self._receiving:
            raise VoiceError("VOICE_SINGLE_RECEIVER_REQUIRED")
        self._receiving = True
        try:
            while not self._closed:
                event = await self._receive(deadline_seconds=None)
                kind = str(event["type"])
                if kind == "session.closed":
                    self._closed = True
                text = event.get("delta", event.get("text", event.get("transcript", "")))
                audio = b""
                if kind == "response.output_audio.delta":
                    try:
                        if not isinstance(text, str):
                            raise ValueError
                        audio = base64.b64decode(text, validate=True)
                        if len(audio) % 2:
                            raise ValueError
                    except (ValueError, binascii.Error):
                        raise VoiceError("VOICE_INVALID_AUDIO") from None
                    text = ""
                response_id = event.get("response_id", "")
                yield VoiceEvent(
                    kind,
                    text if isinstance(text, str) else "",
                    audio,
                    response_id if isinstance(response_id, str) else "",
                    _tool_calls(event) if kind == FUNCTION_CALL_DONE else (),
                )
        finally:
            self._receiving = False

    async def close(self) -> None:
        if self._closed:
            return
        # Caller cancels/awaits its receive task before leaving the session scope.
        try:
            async with asyncio.timeout(self._config.close_timeout):
                await self._send("session.close")
                if not self._receiving:
                    while (await self._receive(deadline_seconds=self._config.close_timeout))[
                        "type"
                    ] != "session.closed":
                        pass
        except Exception:
            pass
        finally:
            self._closed = True
