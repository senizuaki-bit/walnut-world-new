"""Explicit live check; never run by the offline test gate; never prints credentials."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from yaya_agent_runtime.adapters.doubao_realtime import (  # noqa: E402
    DoubaoRealtimeAdapter,
    DoubaoRealtimeConfig,
)
from yaya_agent_runtime.voice import VoiceError  # noqa: E402


async def main() -> int:
    try:
        config = DoubaoRealtimeConfig.from_env()
        if config is None:
            print("VOICE_DISABLED: set YAYA_VOICE_MODE=doubao")
            return 2
        async with DoubaoRealtimeAdapter(config).open_session(
            "你是友好的中文学习伙伴。"
        ) as session:
            print("VOICE_CONNECTED")
            await session.speak("你好，语音连接成功。")
            audio_bytes = 0
            stream = session.events()
            try:
                async with asyncio.timeout(20):
                    async for event in stream:
                        audio_bytes += len(event.audio)
                        if event.type == "response.output_audio.done":
                            break
            finally:
                await stream.aclose()
            if audio_bytes == 0:
                raise VoiceError("VOICE_NO_AUDIO")
            print(f"VOICE_AUDIO_OK pcm_24000_bytes={audio_bytes}")
        return 0
    except VoiceError as error:
        print(error.code)
        return 1
    except TimeoutError:
        print("VOICE_SMOKE_TIMEOUT")
        return 1
    except ValueError:
        print("VOICE_CONFIG_INVALID")
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
