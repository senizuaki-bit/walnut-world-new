# Book summary and speech

Book summaries use Doubao speech synthesis 2.0 with `seed-tts-2.0` and the official “不羁青年 2.0” speaker `ICL_uranus_zh_male_bujiqingnian_tob`. The old speaker without `uranus` belongs to another resource and failed against this account's enabled products. The corrected pair returned complete PCM in a direct provider probe.

The local book credential is read from `YAYA_BOOK_TTS_API_KEY_FILE`; it is outside the repository and is removed from the visible game's environment. The existing realtime-conversation credential remains separate.

The authenticated POST endpoint `/product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}/speech` reads and authorizes the persisted book interaction before synthesizing its exact text. The client cannot override the text or speaker. A bounded in-memory cache prevents duplicate synthesis of an unchanged, authorized interaction. Partial audio and provider errors are rejected.

The frontend waits for the complete PCM payload and verifies the interaction, text digest, voice, sample rate, and format. It exposes the summary card and starts audio together. If synthesis fails, the successful world result is retained and “重试总结语音” retries only synthesis, without rebuilding or rerunning the program. Leaving the card stops playback.

Validation: 15 backend tests passed, including existing realtime-voice coverage. The Godot bridge test covers delayed audio, hidden text before readiness, synthesis failure, audio-only retry, simultaneous display/playback, and stopping playback. The presentation-queue test also passed.

Real acceptance: a rendered Godot test instance submitted through the normal frontend button signal and completed run `run_875e6a83705f0cee1282f796`, advancing world revision 14 to 15 without changing the source. The persisted book interaction `interaction_13a29c7af42bf2f10ceefcf3` and its complete audio appeared together, with AudioStreamPlayer playing. The speech endpoint returned HTTP 200 and 1,690,662 PCM bytes (35.22 seconds of playback). Evidence: `success-real-ui.png` and `book-summary.wav`. This verifies the rendered UI and playback state, rather than a physical microphone/speaker listening test.

Performance: audit timestamps put the book model request-to-finish interval at approximately 2.97 seconds. The 35.22-second figure above is audio duration, not synthesis latency; synthesis latency was not separately instrumented in this run. The workflow also encountered a runtime trace-authority validation failure and succeeded on its second attempt, adding avoidable waiting. The cause of that retry is not yet established or fixed. Existing bounded speech caching avoids repeated provider synthesis for the same authorized interaction while the backend process remains alive. Further candidates are beginning synthesis as soon as an authorized book result is available while remaining workflow bookkeeping finishes, and reducing unnecessary narration length while retaining concrete learning feedback. These candidates are not implemented in this change.

Official references:
- https://www.volcengine.com/docs/6561/1257544 (current speaker list; retrieved through its public documentation API)
- https://www.volcengine.com/docs/6561/1598757 (V3 single-direction SSE)
- https://github.com/bytedance/agentkit-samples/blob/main/skills/byted-text-to-speech/scripts/text_to_speech.py (official request example)
