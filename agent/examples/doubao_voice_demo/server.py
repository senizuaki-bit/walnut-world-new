"""Local browser demo comparing two DingDang brains on one page.

Speech goes to the Doubao realtime model, which answers the student itself.
That session is authorized by the project's own RealtimeVoiceAuthority: a
`hint_requested` event is routed through the real RoleRouter and ContextBuilder,
and the provider instructions are derived from the authorized lesson context.

Typed text goes to the existing durable Ask-DingDang workflow, whose answer the
realtime model then reads out verbatim.  Text cannot reach the realtime model as
a question: the protocol has no such upstream event.  `response.create` is
rejected with `unknown event name`, `conversation.item.create` with a user text
item produces no response, and `speech_text_buffer.commit` returns
`tts_type=chat_tts_text`, which is verbatim synthesis of the given text.

A realtime session is ephemeral and not resumable, so it never claims or commits
a durable Agent turn: its audio, transcripts and tool arguments are presentation
data, not World authority or Evidence.

Serves static files on 127.0.0.1:8764 and a credential-hiding WebSocket proxy on
127.0.0.1:8765.  Local-only by design.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

DEMO_ROOT = Path(__file__).resolve().parent
AGENT_ROOT = DEMO_ROOT.parents[1]
WORKSPACE_ROOT = AGENT_ROOT.parent
sys.path.insert(0, str(AGENT_ROOT / "python"))
sys.path.insert(0, str(WORKSPACE_ROOT / "walnut-world-backend" / "src"))

from walnut_backend.adapters.postgres.agent_runtime import (  # noqa: E402
    PostgresAgentRuntimeReads,
)
from walnut_backend.adapters.postgres.session import create_session_factory  # noqa: E402
from walnut_backend.adapters.postgres.world import PostgresWorld  # noqa: E402
from websockets.asyncio.server import ServerConnection, serve  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.typing import Origin  # noqa: E402
from yaya_agent_contracts import ActorRef, ActorType, ContentRef, OperationContext  # noqa: E402
from yaya_agent_runtime.adapters.doubao_realtime import (  # noqa: E402
    DoubaoRealtimeAdapter,
    DoubaoRealtimeConfig,
)
from yaya_agent_runtime.context_builder import ContextBuilder  # noqa: E402
from yaya_agent_runtime.domain import GameEvent  # noqa: E402
from yaya_agent_runtime.hub import RealtimeVoiceAuthority  # noqa: E402
from yaya_agent_runtime.role_config import PackagedRoleConfigProvider  # noqa: E402
from yaya_agent_runtime.router import RoleRouter  # noqa: E402
from yaya_agent_runtime.voice import (  # noqa: E402
    VoiceError,
    VoiceSession,
    VoiceTool,
    VoiceToolCall,
)

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8764
WS_HOST = "127.0.0.1"
WS_PORT = 8765
MAX_BROWSER_MESSAGE_BYTES = 8_192
DEFAULT_KEY_FILE = AGENT_ROOT / "doubao-voice-api.key"
DEFAULT_BACKEND_STATE = (
    Path(os.getenv("LOCALAPPDATA", "")) / "WalnutWorld" / "persistent-play" / "state.json"
)
BACKEND_URL = os.getenv("WALNUT_BACKEND_URL", "http://127.0.0.1:8790").rstrip("/")
TENANT_ID = "tenant_yaya"
STUDENT_ID = "student_0001"
AUTH_ISSUER = "walnut-int1-local-diagnostic"
AUTH_AUDIENCE = "walnut-game-client"

# Structured output on this protocol exists only through Function Calling: the
# provider validates `arguments` against this JSON Schema.  It records what the
# spoken hint did.  It is not the schema-validated AgentDecision that the durable
# teaching roles commit, and it authorizes nothing.
REPORT_HINT = VoiceTool(
    name="report_hint",
    description=(
        "仅在回答当前课程的教学问题后调用一次，向测试页面展示提示信息。"
        "问候、日常聊天和课程外问答不要调用。只报告刚才说过的内容，"
        "不写入学习档案，也不能执行游戏操作。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "你刚才对学生说的提示，一到两句话"},
            "hint_level": {"type": "integer", "description": "这次提示的分级", "minimum": 0},
            "knowledge_point": {"type": "string", "description": "本次提示对应的知识点"},
            "reveals_answer": {"type": "boolean", "description": "是否已经给出了完整答案"},
        },
        "required": ["message", "hint_level", "knowledge_point", "reveals_answer"],
    },
)


class BackendError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _load_state() -> dict[str, Any]:
    state_file = Path(os.getenv("WALNUT_PERSISTENT_PLAY_STATE", str(DEFAULT_BACKEND_STATE)))
    try:
        value = json.loads(state_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("Walnut persistent-play state is unavailable") from None
    if not isinstance(value, dict):
        raise ValueError("Walnut persistent-play state is invalid")
    state = cast(dict[str, Any], value)
    for name in ("auth_secret", "database_password", "postgres_port"):
        if name not in state:
            raise ValueError(f"persistent-play state is missing {name}")
    secret = state["auth_secret"]
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValueError("Walnut backend authentication state is invalid")
    return state


def _database_url(state: dict[str, Any]) -> str:
    password = str(state["database_password"])
    port = int(state["postgres_port"])
    return f"postgresql+asyncpg://walnut:{password}@127.0.0.1:{port}/walnut_int1"


class WalnutClient:
    """Authenticated local client for the project's own student-facing HTTP API."""

    def __init__(self, state: dict[str, Any], base_url: str = BACKEND_URL) -> None:
        self._secret = str(state["auth_secret"]).encode("utf-8")
        self._base_url = base_url

    def _token(self) -> str:
        now = int(time.time())
        header = _base64url(b'{"alg":"HS256","typ":"JWT"}')
        claims = _base64url(
            json.dumps(
                {
                    "iss": AUTH_ISSUER,
                    "aud": AUTH_AUDIENCE,
                    "sub": STUDENT_ID,
                    "tenant_id": TENANT_ID,
                    "actor_id": STUDENT_ID,
                    "actor_type": "student",
                    "roles": ["game:player"],
                    "iat": now,
                    "nbf": now,
                    "exp": now + 3600,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        unsigned = f"{header}.{claims}"
        signature = _base64url(
            hmac.new(self._secret, unsigned.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{unsigned}.{signature}"

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        attempt = uuid4().hex
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "X-Request-Id": f"req_voice_{attempt}",
            "X-Trace-Id": f"trace_voice_{attempt}",
            "X-Correlation-Id": f"corr_voice_{attempt}",
            "X-Schema-Version": "1.0.0",
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(f"{self._base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=35) as response:
                value = json.load(response)
        except HTTPError as error:
            try:
                payload = json.load(error)
                code = payload.get("error", {}).get("code", f"HTTP_{error.code}")
            except Exception:
                code = f"HTTP_{error.code}"
            raise BackendError(str(code)) from None
        except (OSError, URLError, TimeoutError):
            raise BackendError("BACKEND_UNAVAILABLE") from None
        if not isinstance(value, dict):
            raise BackendError("BACKEND_INVALID_RESPONSE")
        return cast(dict[str, Any], value)

    async def get(self, path: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "GET", path)

    async def post(self, path: str, body: dict[str, object], key: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "POST", path, body, idempotency_key=key)


class LessonAuthority:
    """Resolve the authorized lesson scope for one realtime voice session.

    Identity comes from the authenticated read API; the task identity comes from
    the pinned ContentUnit through the same adapter the durable worker uses, so a
    voice session cannot drift onto different content than a real hint sees.
    """

    def __init__(self, client: WalnutClient, reads: PostgresAgentRuntimeReads) -> None:
        self._client = client
        self._reads = reads

    async def resolve(self) -> tuple[GameEvent, OperationContext]:
        bootstrap = await self._client.get("/v1/student-bootstrap")
        session_id = str(bootstrap.get("session", {}).get("current_session_id", ""))
        content = bootstrap.get("content", {})
        world = bootstrap.get("world", {})
        if not session_id or not isinstance(content, dict) or not isinstance(world, dict):
            raise BackendError("VOICE_SESSION_UNAVAILABLE")
        try:
            content_ref = ContentRef(
                unit_id=str(content["unit_id"]),
                version=str(content["version"]),
                content_hash=str(content["content_hash"]),
            )
            revision = int(world["revision"])
        except (KeyError, TypeError, ValueError):
            raise BackendError("VOICE_CONTENT_UNAVAILABLE") from None

        attempt = uuid4().hex
        now = datetime.now(UTC)
        operation_context = OperationContext(
            request_id=f"req_voice_{attempt}",
            correlation_id=f"corr_voice_{attempt}",
            trace_id=f"trace_voice_{attempt}",
            requested_at=now,
            actor=ActorRef(
                tenant_id=TENANT_ID,
                actor_id=STUDENT_ID,
                actor_type=ActorType.STUDENT,
                roles=("game:player",),
            ),
            content_ref=content_ref,
            command_id=f"cmd_voice_{attempt}",
            causation_id=None,
        )
        # SessionSnapshot.task_id is read from the pinned ContentUnit, and the
        # same read proves this Session is the current ACTIVE binding for this
        # actor and content.
        snapshot = await self._reads.get_session(session_id, operation_context)
        event = GameEvent(
            event_id=f"gameevent-voice-{attempt}",
            event_type="hint_requested",
            student_id=STUDENT_ID,
            task_id=snapshot.task_id,
            session_id=session_id,
            turn_id=f"turn-voice-{attempt}",
            command_id=operation_context.command_id,
            occurred_at=now,
            expected_world_revision=revision,
        )
        return event, operation_context


class DingDangTextFlow:
    """The existing durable Ask-DingDang workflow, used for typed questions."""

    def __init__(self, client: WalnutClient) -> None:
        self._client = client

    async def ask(self, text: str) -> str:
        question = text.strip()
        if not question or len(question) > 4000:
            raise BackendError("INVALID_QUESTION")
        return await self._ask_current_session(question)

    async def _poll_command(self, command_id: str, deadline: float) -> dict[str, Any]:
        while asyncio.get_running_loop().time() < deadline:
            command = await self._client.get(f"/v1/commands/{command_id}")
            if bool(command.get("terminal", False)):
                return command
            await asyncio.sleep(0.5)
        raise BackendError("DINGDANG_TIMEOUT")

    async def _ask_current_session(self, text: str) -> str:
        for attempt in range(2):
            bootstrap = await self._client.get("/v1/student-bootstrap")
            session_id = str(bootstrap.get("session", {}).get("current_session_id", ""))
            world_id = str(bootstrap.get("world", {}).get("world_id", ""))
            if not session_id or not world_id:
                raise BackendError("DINGDANG_SESSION_UNAVAILABLE")
            workspace = await self._client.get(
                f"/product-experience/v1/sessions/{session_id}/workspace"
            )
            snapshot = await self._client.get(f"/v1/worlds/{world_id}/snapshot")
            turn_id = f"turn_voice_{uuid4().hex[:24]}"
            before_sequence = int(workspace.get("last_interaction_sequence", 0))
            request_body: dict[str, object] = {
                "turn_id": turn_id,
                "expected_world_revision": int(snapshot["revision"]),
                "input": {"type": "MESSAGE", "text": text, "locale": "zh-CN"},
                "skill_bindings": [],
                "client_state": {
                    "last_event_sequence": int(snapshot["last_event_sequence"]),
                    "client_turn_sequence": int(workspace["session"]["last_turn_sequence"]) + 1,
                },
            }
            try:
                accepted = await self._client.post(
                    f"/v1/agent-sessions/{session_id}/turns",
                    request_body,
                    f"voice-{session_id}-{turn_id}",
                )
                break
            except BackendError as error:
                if attempt == 0 and error.code in {
                    "CONFLICT",
                    "INVALID_REQUEST",
                    "STALE_WORLD_REVISION",
                }:
                    continue
                raise
        else:
            raise BackendError("DINGDANG_TURN_REFUSED")

        command_id = str(accepted.get("command_id", ""))
        if not command_id:
            raise BackendError("DINGDANG_COMMAND_MISSING")
        deadline = asyncio.get_running_loop().time() + 360
        command = await self._poll_command(command_id, deadline)
        if command.get("status") != "APPLIED":
            raise BackendError(f"DINGDANG_COMMAND_{command.get('status', 'FAILED')}")

        cursor = before_sequence
        while asyncio.get_running_loop().time() < deadline:
            query = urlencode({"after_sequence": cursor, "limit": 50})
            page = await self._client.get(
                f"/product-experience/v1/sessions/{session_id}/agent-interactions?{query}"
            )
            for interaction in page.get("interactions", []):
                cursor = max(cursor, int(interaction.get("sequence", cursor)))
                if (
                    interaction.get("turn_id") == turn_id
                    and interaction.get("feedback", {}).get("command_id") == command_id
                ):
                    answer = interaction.get("feedback", {}).get("message")
                    if isinstance(answer, str) and answer.strip():
                        return answer.strip()
                    raise BackendError("DINGDANG_EMPTY_RESPONSE")
            await asyncio.sleep(0.5)
        raise BackendError("DINGDANG_TIMEOUT")


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(DEMO_ROOT), **kwargs)

    def end_headers(self) -> None:
        # This page is edited while it is open; a cached copy silently hides
        # the change being tested.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def load_config() -> DoubaoRealtimeConfig:
    key_file = Path(os.getenv("YAYA_DOUBAO_VOICE_API_KEY_FILE", str(DEFAULT_KEY_FILE)))
    key = os.getenv("YAYA_DOUBAO_VOICE_API_KEY", "").strip()
    if key and os.getenv("YAYA_DOUBAO_VOICE_API_KEY_FILE"):
        raise ValueError("configure only one API key source")
    if not key:
        try:
            key = key_file.read_text(encoding="utf-8-sig").strip()
        except OSError:
            raise ValueError(f"API key file is unavailable: {key_file}") from None
    return DoubaoRealtimeConfig(
        api_key=key,
        voice=os.getenv("YAYA_DOUBAO_VOICE", "ICL_uranus_zh_male_youmodaye_tob"),
        speed=float(os.getenv("YAYA_DOUBAO_VOICE_SPEED", "0")),
        loudness=float(os.getenv("YAYA_DOUBAO_VOICE_LOUDNESS", "0")),
    )


async def send_json(websocket: ServerConnection, payload: dict[str, object]) -> None:
    await websocket.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


class VoiceBridge:
    """Relay one browser connection, keeping the two DingDang brains separate."""

    def __init__(
        self,
        websocket: ServerConnection,
        session: VoiceSession,
        text_flow: DingDangTextFlow,
    ) -> None:
        self.websocket = websocket
        self.session = session
        self._text_flow = text_flow
        self._ask_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    def submit_text(self, question: str) -> None:
        task = asyncio.create_task(self._ask(question))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _ask(self, question: str) -> None:
        async with self._ask_lock:
            await self.session.interrupt()
            await send_json(self.websocket, {"type": "dingdang.thinking", "source": "workflow"})
            try:
                answer = await self._text_flow.ask(question)
            except BackendError as error:
                await send_json(
                    self.websocket,
                    {"type": "dingdang.error", "code": error.code, "source": "workflow"},
                )
                return
            await send_json(
                self.websocket,
                {"type": "dingdang.response", "text": answer, "source": "workflow"},
            )
            # The realtime model only reads this out; it did not produce it.
            await self.session.speak(answer)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in tuple(self._tasks):
            with suppress(asyncio.CancelledError):
                await task


async def browser_to_provider(websocket: ServerConnection, bridge: VoiceBridge) -> None:
    session = bridge.session
    async for raw in websocket:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BROWSER_MESSAGE_BYTES:
            raise VoiceError("DEMO_INVALID_FRAME")
        try:
            value: object = json.loads(raw)
        except json.JSONDecodeError:
            raise VoiceError("DEMO_INVALID_JSON") from None
        if not isinstance(value, dict):
            raise VoiceError("DEMO_INVALID_FRAME")
        frame = cast(dict[str, object], value)
        if not isinstance(frame.get("type"), str):
            raise VoiceError("DEMO_INVALID_FRAME")
        kind = frame["type"]
        if kind == "audio":
            encoded = frame.get("audio")
            if not isinstance(encoded, str):
                raise VoiceError("DEMO_INVALID_AUDIO")
            try:
                pcm = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise VoiceError("DEMO_INVALID_AUDIO") from None
            await session.send_audio(pcm)
        elif kind == "mute":
            await session.set_muted(True)
        elif kind == "unmute":
            await session.set_muted(False)
        elif kind == "commit":
            await session.commit_audio()
        elif kind == "interrupt":
            await session.interrupt()
        elif kind == "ask":
            text = frame.get("text")
            if not isinstance(text, str):
                raise VoiceError("DEMO_INVALID_TEXT")
            bridge.submit_text(text)
        elif kind == "close":
            return
        else:
            raise VoiceError("DEMO_UNKNOWN_EVENT")


async def _acknowledge_tool_calls(
    bridge: VoiceBridge,
    calls: tuple[VoiceToolCall, ...],
) -> None:
    for call in calls:
        if call.name != REPORT_HINT.name:
            await bridge.session.send_tool_result(call.call_id, "unsupported tool")
            continue
        try:
            arguments = call.parsed_arguments()
        except VoiceError:
            await bridge.session.send_tool_result(call.call_id, "arguments were not valid JSON")
            continue
        await send_json(
            bridge.websocket,
            {
                "type": "dingdang.hint_report",
                "source": "doubao",
                "call_id": call.call_id,
                "arguments": arguments,
            },
        )
        await bridge.session.send_tool_result(call.call_id, "recorded")


async def provider_to_browser(bridge: VoiceBridge) -> None:
    async for event in bridge.session.events():
        if event.calls:
            await _acknowledge_tool_calls(bridge, event.calls)
            continue
        payload: dict[str, object] = {"type": event.type, "response_id": event.response_id}
        if event.text:
            payload["text"] = event.text
        if event.audio:
            payload["audio"] = base64.b64encode(event.audio).decode("ascii")
        await send_json(bridge.websocket, payload)


async def handle_browser(
    websocket: ServerConnection,
    authority: RealtimeVoiceAuthority,
    lessons: LessonAuthority,
    text_flow: DingDangTextFlow,
) -> None:
    if websocket.request is None or websocket.request.path != "/voice":
        await websocket.close(4404, "voice endpoint required")
        return
    try:
        await send_json(websocket, {"type": "voice.connecting"})
        event, operation_context = await lessons.resolve()
        async with authority.open_session(
            event, operation_context, tools=(REPORT_HINT,)
        ) as session:
            bridge = VoiceBridge(websocket, session, text_flow)
            await send_json(
                websocket,
                {
                    "type": "voice.ready",
                    "session_id": event.session_id,
                    "task_id": event.task_id,
                    "input": {"encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                    "output": {"encoding": "pcm_s16le", "sample_rate": 24000, "channels": 1},
                },
            )
            tasks = {
                asyncio.create_task(browser_to_provider(websocket, bridge)),
                asyncio.create_task(provider_to_browser(bridge)),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()
            await bridge.close()
    except ConnectionClosed:
        pass
    except (VoiceError, BackendError) as error:
        with suppress(ConnectionClosed):
            await send_json(websocket, {"type": "voice.error", "code": error.code})
            await websocket.close(4500, "voice session failed")
    except Exception as error:
        print(f"voice session failed: {type(error).__name__}: {error}", file=sys.stderr)
        with suppress(ConnectionClosed):
            await send_json(websocket, {"type": "voice.error", "code": "DEMO_INTERNAL_ERROR"})
            await websocket.close(4500, "voice session failed")


def start_http_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), QuietStaticHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def run() -> None:
    state = _load_state()
    sessions = create_session_factory(_database_url(state))
    reads = PostgresAgentRuntimeReads(sessions)
    contexts = ContextBuilder(
        tasks=reads,
        sessions=reads,
        skills=reads,
        runs=reads,
        counterexamples=reads,
        learners=reads,
        messages=reads,
        worlds=PostgresWorld(sessions),
        drafts=reads,
        interactions=reads,
        role_configs=PackagedRoleConfigProvider.load(),
    )
    authority = RealtimeVoiceAuthority(
        router=RoleRouter(),
        contexts=contexts,
        voice=DoubaoRealtimeAdapter(load_config()),
    )
    client = WalnutClient(state)
    lessons = LessonAuthority(client, reads)
    text_flow = DingDangTextFlow(client)
    http_server = start_http_server()
    try:
        async with serve(
            lambda websocket: handle_browser(websocket, authority, lessons, text_flow),
            WS_HOST,
            WS_PORT,
            origins=[
                cast(Origin, f"http://{HTTP_HOST}:{HTTP_PORT}"),
                cast(Origin, f"http://localhost:{HTTP_PORT}"),
            ],
            max_size=MAX_BROWSER_MESSAGE_BYTES,
            max_queue=8,
            ping_interval=20,
        ):
            print(f"Demo ready: http://{HTTP_HOST}:{HTTP_PORT}")
            print("Press Ctrl+C to stop.")
            await asyncio.Future()
    finally:
        http_server.shutdown()
        http_server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Doubao voice browser demo")
    parser.parse_args()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 0
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
