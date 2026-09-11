"""Synthesize the exact published book text; never use conversational generation."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4

import httpx
from yaya_agent_runtime.adapters.doubao_realtime import DoubaoRealtimeConfig

SPEAKER = "ICL_uranus_zh_male_bujiqingnian_tob"
RESOURCE = "seed-tts-2.0"
ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse"
MAX_AUDIO_BYTES = 8 * 1024 * 1024


class BookSpeechError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    @property
    def retryable(self) -> bool:
        return self.code in {
            "BOOK_SPEECH_PROVIDER_UNAVAILABLE",
            "BOOK_SPEECH_INCOMPLETE",
            "BOOK_SPEECH_INVALID_RESPONSE",
        }


class BookSpeech:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport
        self._cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._lock = asyncio.Lock()

    async def synthesize(self, identity: str, text: str) -> bytes:
        if not text.strip() or len(text) > 420:
            raise BookSpeechError("BOOK_SPEECH_TEXT_INVALID")
        key = (identity, hashlib.sha256(text.encode()).hexdigest())
        # Cached responses need no provider lock; this path has no await/mutation race.
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        async with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            api_key = _configured_key()
            try:
                async with asyncio.timeout(60):
                    audio = await self._request(api_key, text)
            except (httpx.HTTPError, TimeoutError):
                raise BookSpeechError("BOOK_SPEECH_PROVIDER_UNAVAILABLE") from None
            self._cache[key] = audio
            while len(self._cache) > 8 or sum(map(len, self._cache.values())) > 32 * 1024 * 1024:
                self._cache.popitem(last=False)
            return audio

    async def _request(self, api_key: str, text: str) -> bytes:
        audio = bytearray()
        completed = False
        async with httpx.AsyncClient(transport=self._transport, timeout=20) as client:
            async with client.stream(
                "POST",
                ENDPOINT,
                headers={
                    "X-Api-Key": api_key,
                    "X-Api-Resource-Id": RESOURCE,
                    "X-Api-Request-Id": str(uuid4()),
                },
                json={
                    "user": {"uid": "walnut-book"},
                    "req_params": {
                        "text": text,
                        "speaker": SPEAKER,
                        "audio_params": {"format": "pcm", "sample_rate": 24000},
                    },
                },
            ) as response:
                if response.status_code in (401, 403):
                    raise BookSpeechError("BOOK_SPEECH_AUTH_FAILED")
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    if len(line) > MAX_AUDIO_BYTES * 2:
                        raise BookSpeechError("BOOK_SPEECH_AUDIO_TOO_LARGE")
                    try:
                        event = json.loads(line[5:])
                        code = event["code"]
                        if code == 45000030:
                            raise BookSpeechError("BOOK_SPEECH_RESOURCE_NOT_GRANTED")
                        if code not in (0, 20000000):
                            raise BookSpeechError("BOOK_SPEECH_PROVIDER_REJECTED")
                        if event.get("data"):
                            audio.extend(base64.b64decode(event["data"], validate=True))
                    except (ValueError, KeyError, TypeError, binascii.Error):
                        raise BookSpeechError("BOOK_SPEECH_INVALID_RESPONSE") from None
                    if len(audio) > MAX_AUDIO_BYTES:
                        raise BookSpeechError("BOOK_SPEECH_AUDIO_TOO_LARGE")
                    if code == 20000000:
                        completed = True
                        break
        if not completed or not audio or len(audio) % 2:
            raise BookSpeechError("BOOK_SPEECH_INCOMPLETE")
        return bytes(audio)


def _configured_key() -> str:
    key = os.environ.get("YAYA_BOOK_TTS_API_KEY", "").strip()
    path = os.environ.get("YAYA_BOOK_TTS_API_KEY_FILE", "").strip()
    try:
        if key and path:
            raise ValueError("ambiguous key source")
        if path:
            key = Path(path).read_text(encoding="utf-8-sig").strip()
            if not key:
                raise ValueError("empty key file")
        if not key:
            config = DoubaoRealtimeConfig.from_env()
            if config is None:
                raise BookSpeechError("BOOK_SPEECH_DISABLED")
            key = config.api_key
        if any(char in key for char in "\r\n"):
            raise ValueError("invalid key")
        return key
    except (ValueError, OSError):
        raise BookSpeechError("BOOK_SPEECH_CONFIGURATION_INVALID") from None
