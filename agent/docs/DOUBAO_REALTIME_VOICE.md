# AgentHub 豆包实时语音接入

实现依据：[豆包 3.0 全双工接口](https://docs.volcengine.com/docs/6561/2549778?lang=zh)、[接入必读](https://docs.volcengine.com/docs/6561/2549732?lang=zh)及页面内的官方 Python 示例，核对日期 2026-09-08。

## 当前范围

`AgentHub.open_voice(event, operation_context)` 提供经过现有 ContextBuilder 授权的独立实时语音会话。ProductionSettings/composition 可选择装配豆包适配器，默认关闭。协议为 JSON WebSocket，model 固定 `1.2.6.1`，使用 `X-Api-Key`，不是旧版二进制语音协议。输出显式请求官方示例采用的 `pcm_s16le`，前端按 24 kHz、单声道、16 位小端播放。

支持：20 ms PCM 上行、流式音频下行、ASR 和回复文本事件、主动打断、静音/恢复、指定文本播报、通过 `update_instructions()` 更新当前会话提示词、超时和会话关闭。提示词更新保留音色、音频格式及工具声明，接收端会收到 `session.updated`。凭据只在连接握手发送；错误不输出供应商响应正文；音频及转录不写入数据库。

AgentHub 的 Python 接口与主后端独立语音路由均已接入。主后端提供 `/product-experience/v1/sessions/{session_id}/dingdang-voice`，供主动“问叮当”连续语音对话使用；Godot 麦克风／播放仍由前端接入。协议见 [前后端接口文档](DINGDANG_VOICE_FRONTEND_BACKEND_API.md)。没有替换 Walnut durable workflow worker 的 DeepSeek；`YAYA_*` 语音配置不会改变 `WALNUT_LLM_*` relay。

## 配置和真实验证

在 agent 目录执行：

```powershell
uv sync --extra voice
$env:YAYA_VOICE_MODE = 'doubao'
$env:YAYA_DOUBAO_VOICE_API_KEY_FILE = (Resolve-Path './doubao-voice-api.key').Path
uv run --extra voice python scripts/check-doubao-voice.py
```

将语音服务的 API Key 放入上述本机文件（`*.key` 已被 Git 忽略），或使用 `YAYA_DOUBAO_VOICE_API_KEY`，两者只能设置一个。可选音色变量 `YAYA_DOUBAO_VOICE`，默认 `ICL_uranus_zh_male_youmodaye_tob`（官方「幽默大爷 2.0」，用于叮当大叔）。程序不会自动加载 `.env`。

真实检查会连接官方服务、播报一句测试文本并验证收到非空 PCM 音频，会产生少量调用用量。脚本不属于离线测试集。成功输出 `VOICE_CONNECTED` 和 `VOICE_AUDIO_OK`；`VOICE_AUTH_FAILED` 表示服务拒绝了凭据。请使用[语音控制台 API Key 管理](https://console.volcengine.com/speech/new/setting/apikeys?projectName=default.)中对该服务有效的密钥，不能仅凭 Key 外观判断可用性。

当前本机凭据已完成真实握手，并验证收到非空的 24 kHz PCM 音频。

原生语速和音量分别通过 `YAYA_DOUBAO_VOICE_SPEED`、`YAYA_DOUBAO_VOICE_LOUDNESS` 配置，范围均为 -50～100，AgentHub 和浏览器 demo 默认均为 0。前端采用 1.0 倍播放，保留「幽默大爷 2.0」原生音高与节奏。该音色列于[官方 S2S 全双工音色库](https://docs.volcengine.com/docs/6561/1257544?lang=zh)，已通过当前 `1.2.6.1` 实时接口生成有效的 24 kHz PCM 音频。

## 调用约定

```python
async with hub.open_voice(trusted_event, operation_context) as voice:
    receiver = asyncio.create_task(play_events(voice.events()))
    try:
        await voice.set_muted(False)
        # 麦克风按实际时钟每 20 ms 提交 640 bytes：16 kHz / mono / int16 LE。
        await capture_microphone(voice.send_audio)
        await voice.set_muted(True)
    finally:
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receiver
```

`trusted_event` 必须来自服务端验证过的会话状态。接收端按 `VoiceEvent.type` 处理：

- `response.output_audio.delta`：播放 `audio`，24 kHz / mono / int16 LE。
- `conversation.item.input_audio_transcription.*`：展示用户识别文字 `text`。
- `response.output_text.*`：展示模型文字 `text`。
- `response.canceled`：清空本地待播音频；主动打断时也应立即清空播放器。

发送端必须按真实音频节奏发送，适配器只验证包长，不为历史积压音频重新计时。离开上下文前停止并等待接收任务，让关闭流程等待 `session.closed`。当前不自动重连/重放语音，避免把不确定的对话重复提交。

## 替代 DeepSeek 的边界

目前 DeepSeek 通过可恢复 relay 输出结构化决策/工具请求，运行时校验并持久化后才执行游戏流程。语音模型拥有 Function Calling 能力，但不是可直接替换该 HTTP 接口的实现。

若以实时语音模型作为主对话模型，需要把 `response.function_call_arguments.done` 接到经过授权、带幂等与恢复机制的 AgentHub 工具入口，按 `call_id` 回传结果，并确保音频播报与已提交决策一致。适配器已支持声明工具和返回结果；浏览器 demo 的 `report_hint` 只把教学信息展示到页面，不写学习档案，也不执行游戏操作。目前 DeepSeek 仍负责文字工作流。

## 浏览器 demo 与普通问答

`examples/doubao_voice_demo/server.py` 在本机提供 HTTP 8764 和 WebSocket `/voice` 8765。文字 `ask` 事件提交到既有 `POST /v1/agent-sessions/{session_id}/turns`，读取已提交的 AgentInteraction 后发送 `dingdang.response` 并朗读；麦克风 PCM 由豆包直接回答，转录和回复通过上述语音事件回传。两条链路均允许问候、身份、日常聊天及课程外知识问答。

文字普通问答复用公开契约已有的 `response_type=message`，仅对实际包含学生 MESSAGE 的轮次放行。其 `question`、`hint_level`、`learner_inference`、`skill_patch` 均为 null；不生成 Run、Evidence、学习能力推断或世界操作。课程问题继续使用 `question/hint`。原有历史 TeachingDirective 仍可读取，前端按 `feedback.message` 展示文本即可。
