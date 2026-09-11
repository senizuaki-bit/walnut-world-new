"""Local credential validation before starting the playable stack; no provider calls."""

import hashlib
import json
import os

from yaya_agent_runtime.adapters.doubao_realtime import DoubaoRealtimeConfig

from walnut_backend.adapters.doubao_tts import BookSpeechError, _configured_key


def main() -> int:
    try:
        book_key = _configured_key()
    except BookSpeechError as error:
        print(json.dumps({"status": "INVALID", "code": error.code}))
        return 1
    try:
        voice = DoubaoRealtimeConfig.from_env()
    except (ValueError, OSError):
        print(json.dumps({"status": "INVALID", "code": "VOICE_CONFIGURATION_INVALID"}))
        return 1
    print(
        json.dumps(
            {
                "status": "CONFIGURED",
                "book_speech": "CONFIGURED",
                "realtime_voice": "CONFIGURED" if voice is not None else "DISABLED",
                "provider_access": "NOT_CHECKED",
                # Only a digest is recorded; raw credentials are never persisted or printed.
                # Include every inherited voice setting, including explicit key sources.
                "configuration_sha256": hashlib.sha256(
                    json.dumps(
                        {
                            "resolved_book_key": book_key,
                            "resolved_voice_key": voice.api_key if voice else None,
                            "environment": {
                                name: value
                                for name, value in os.environ.items()
                                if name.startswith(
                                    ("YAYA_BOOK_TTS_", "YAYA_DOUBAO_VOICE", "YAYA_VOICE_")
                                )
                            },
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
