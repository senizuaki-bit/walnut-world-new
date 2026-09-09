# 问叮当：豆包实时语音前后端接口

Demo V2 · 2026-09-09。**后端已实现，前端待接入。**

交互以最新确认的需求为准：点击“问叮当”开始连续自动语音对话，再点击一次关闭。此版本取代此前“长按 + 独立 STT + DS 回答”草案，附件作为背景需求。

只有主动“问叮当”的新语音连接使用豆包。小胡桃、Bug Agent、成长总结及自动教学反馈仍走 DS，原有文字接口保留。语音会话不提交 Agent Turn，不生成 Run、Evidence 或世界操作，不持久化录音／文字；重新打开即开始新会话。

教学辅导统一归入“问叮当”：文字和豆包语音共用 `PedagogyPolicy`，后端根据已保存的失败次数、错误详情和学习档案计算教学阶段与提示等级。内部 `teaching_agent` 标识保留用于原文字接口和历史记录兼容，前端不需要新增或切换另一个教学角色；语音不会为分层额外调用 DS。

官方依据：[实时语音 3.0 全双工接口](https://docs.volcengine.com/docs/6561/2549778?lang=zh)、[接入必读](https://docs.volcengine.com/docs/6561/2549732?lang=zh)。复用现有 JSON WebSocket 适配器，模型 `1.2.6.1`，无需独立 STT 或 TTS。

## 1. 地址与鉴权

```text
WS /product-experience/v1/sessions/{session_id}/dingdang-voice
```

主机、端口与游戏 REST API 相同。默认本地示例：

```text
ws://127.0.0.1:8790/product-experience/v1/sessions/{session_id}/dingdang-voice
```

`session_id` 使用游戏已恢复的当前 AgentSession。HTTP 对应 `ws://`，HTTPS 对应 `wss://`。

连接后 10 秒内发送 `start` JSON 文本帧，使用游戏已有登录 token，**不带 `Bearer ` 前缀**。token 不放在 URL，也不使用豆包 API Key。后端复用已有 JWT／显式开发鉴权，验证会话归属、当前有效会话及关卡后才连接豆包。

此接口不使用世界 WSS 的 `yaya.runtime.v1` 子协议及 `X-Stream-Protocol-Version` 等请求头，也不需要 REST 幂等键和 attempt headers；世界事件原接口保持原协议。

```json
{
  "type": "start",
  "token": "游戏现有登录token",
  "context": {
    "code": "int gap = target[i] - moisture[i];",
    "observation": "正在编辑；上次本地候选检查提示番茄少浇水。"
  }
}
```

`context` 可省略。`code`、`observation` 均为可选字符串，分别取前 1800、400 字符，适合本 Demo 的短代码场景；较长内容会截断。这些字段仅作学习材料，不写入草稿，也不当作正式运行结果。token 仅用于首帧鉴权。

## 2. 上行帧

| 帧 | 格式 | 用途 |
| --- | --- | --- |
| `start` | JSON 文本 | 每个连接恰好一次 |
| 麦克风音频 | WebSocket 二进制 | `voice.ready` 后持续上传 |
| `context` | JSON 文本 | 更新编辑器内容和客户端观察 |
| `interrupt` | JSON 文本 | 主动打断当前回复，保持连接 |
| `close` | JSON 文本 | 结束整个语音会话 |

音频：**16 kHz、单声道、有符号 int16、小端 PCM；每帧 20 ms = 320 个采样 = 640 字节**。直接发送 PCM 字节，不带 WAV 头，不包成 JSON/Base64。按实时采集节奏发送；安静时也持续传采集音频。模型自动判断说话结束，无需逐句 `commit`。

```json
{
  "type": "context",
  "context": {
    "code": "int gap = 60 - moisture[i];",
    "observation": "编辑器刚修改，还没有再次运行。"
  }
}
```

`context` 整体替换上一份客户端上下文，更新时应一并发送希望保留的字段。建议编辑停止约 300～500 ms 后发送；运行或本地检查变化时同步观察。

```json
{"type":"interrupt"}
```

```json
{"type":"close"}
```

`interrupt` 后继续正常收音。`close` 结束会话；后端也会清理直接断开的连接。

## 3. 下行帧

全部为 JSON 文本帧。连接期间显示“连接中”，收到以下事件才开始上传音频：

```json
{"type":"voice.ready","input_rate":16000,"output_rate":24000}
```

供应商事件归一化为 `type`、`text`、`response_id`，有音频时额外包含 `audio`：

```json
{"type":"response.output_text.delta","text":"先看看两个数组","response_id":"response_demo"}
```

```json
{"type":"response.output_audio.delta","text":"","response_id":"response_demo","audio":"AQABAA=="}
```

`audio` 为 Base64，解码后是 **24 kHz、单声道、有符号 int16、小端 PCM**，按顺序排队播放，不是完整音频文件。示例仅为短采样，实际长度不固定。部分事件的 `response_id` 可能为空字符串。

| `type` | 前端处理 |
| --- | --- |
| `conversation.item.input_audio_transcription.started` | 开始新一条用户转写 |
| `conversation.item.input_audio_transcription.delta` | 追加 `text` |
| `conversation.item.input_audio_transcription.completed` | 非空 `text` 替换为完整转写 |
| `conversation.item.input_audio_transcription.failed` | 当前转写失败，允许继续说话 |
| `response.output_text.delta` | 追加到当前回答字幕 |
| `response.output_text.done` | 文本结束；非空 `text` 可校准完整回答 |
| `response.output_audio.started` | 开始接收回答音频 |
| `response.output_audio.delta` | 解码并排队播放 |
| `response.output_audio.done` | 音频生成结束，本地可能尚未播完 |
| `response.done` | 本轮模型响应结束，连接继续保持 |
| `response.canceled` | 清空被取消回复的播放队列 |
| `session.closed` 或连接关闭 | 停止录音和播放，复位按钮 |
| `voice.error` | 显示错误并释放资源 |
| `session.updated` | 豆包已确认上下文更新，不影响持续录音 |

其他事件可忽略。不要用空 `text` 清空已有字幕。主动打断时立即停止本地播放，不必等待取消确认；忽略被取消回复已在途的后续音频。全双工依靠持续麦克风输入处理学生插话，收到取消事件时同样清理播放。

## 4. 关卡上下文

建立连接时后端读取当前关卡、任务状态、最近正式 Run、当前及历史编译报错，并使用与文字辅导相同的策略计算教学阶段和提示等级。编辑器代码和客户端观察来自 `start.context`。失败次数来自正式构建记录或运行结果回执；普通聊天不增加失败次数。运行回执尚未生成时明确标注计数待整理，不把暂时的零解释为运行成功。

前端每次发送 `context`，后端重新读取关卡和最近 Run，并通过豆包 `session.update` 主动更新当前会话提示词，收到 `session.updated` 表示上游确认。更新保留同一语音会话和历史对话，不依赖模型是否主动调用工具。

连接保持期间，后端每 1 秒用一次 SQL 检查最近 Run、构建状态、运行计数回执和学习档案的变更标识。标识变化才重新加载上下文并发送 `session.update`；没有 Run 的编译失败也会触发更新。这条自动更新不需要前端发消息或模型调用工具。运行成功提交后即可同步，不等待成长总结；总结稍后生成时会再次同步。刷新延迟为轮询间隔加数据库读取与上游确认时间，不保证刚提交的同一瞬间已进入模型上下文。

任务完成状态由最近正式 Run 的成功状态与世界提交结果派生。尚未生成总结会明确标注，不会因此把已成功运行说成未完成。编辑器代码仍需前端主动发送 `context`，后台运行刷新不会替代编辑器同步。客户端更新与后台更新串行处理，断线后停止轮询；音频包不会触发数据库读取。

另提供只读工具 `get_game_context`，供模型回答游戏相关问题时再次查询。调用、授权、结果回传均由后端完成，前端无需处理工具事件。

查询限定当前学生、会话与关卡版本。Run 摘要包含状态、沙箱错误、世界应用状态与错误及已有反馈；没有 Run 时为空。首帧接收的代码/观察上限仍为 1800/400 字符；为给教学与错误详情留空间，模型上下文中的代码、Run 摘要、观察分别取前 1200、450、200 字符，当前编译报错和最近六次历史摘要各限 400 字符。完整编译详情保留在后台记录中。不会递归读取整份交互历史。

语音叮当只解释和指导。教学阶段和等级由共享策略计算，语音模型通过提示词遵循这些限制，没有逐句复用 DS 的结构化输出校验。代码修改建议仍由既有显式确认接口处理。前端无需发送失败计数、历史对话、完整世界快照或学习画像。

## 5. 点击开关的接入流程

1. 第一次点击：打开 WebSocket，发 `start`，显示“连接中”。
2. 收到 `voice.ready`：持续录音、转采样、上传并播放回复，显示“正在对话”。
3. 回答播完后继续收音，允许多轮自动交谈。
4. 第二次点击：立即停止麦克风与播放、清空队列，发 `close` 后关闭连接，恢复“问叮当”。
5. 切换关卡、退出场景、网络断开或 `voice.error` 时执行同样的清理。连接中也允许取消。

前端建议等待 ready 超时约 20 秒；超时后允许重新点击，不自动重放旧音频。重连是新会话。桌面使用扬声器需处理回声，联调可先使用耳机。

## 6. 错误

```json
{"type":"voice.error","code":"VOICE_DISABLED"}
```

错误后后端关闭连接；没有错误帧的网络断开按普通连接中断处理。

| `code` | 含义 |
| --- | --- |
| `VOICE_DISABLED` | 后端未启用语音 |
| `VOICE_CONFIGURATION_INVALID` | 后端语音配置需检查 |
| `VOICE_AUTH_FAILED` | 游戏登录或上游语音凭据鉴权失败 |
| `VOICE_SESSION_UNAVAILABLE` | 当前游戏会话不可用 |
| `VOICE_INVALID_START` / `VOICE_INVALID_CONTEXT` / `VOICE_INVALID_FRAME` | 前端帧格式错误 |
| `VOICE_AUDIO_REQUIRES_20MS_PCM` | 音频包不是 640 字节 |
| `VOICE_DEPENDENCY_MISSING` | 后端缺少 websockets |
| 其他 `VOICE_*` | 语音暂时中断，可重新连接 |

语音失败不触发 DS 自动替答，也不改变原有游戏操作。

## 7. 后端配置

使用主后端原有启动方式，不需要另开浏览器 demo 的 8764／8765 端口。从项目根目录准备环境变量：

```powershell
$env:YAYA_VOICE_MODE = 'doubao'
$env:YAYA_DOUBAO_VOICE_API_KEY_FILE = (Resolve-Path './agent/doubao-voice-api.key').Path
# 随后按原方式启动主后端。
```

也支持 `YAYA_DOUBAO_VOICE_API_KEY`，与 `_FILE` 二选一。密钥只留后端。默认音色为 `ICL_uranus_zh_male_youmodaye_tob`；音色、语速、音量选项见 [适配器说明](DOUBAO_REALTIME_VOICE.md)。

`walnut-world-backend/scripts/start-persistent-play.ps1` 在未显式配置时启用豆包语音，并在存在时使用 `agent/doubao-voice-api.key`；DS provider/model 保持原配置。普通主后端启动没有 `YAYA_VOICE_MODE` 时默认关闭语音，其余业务正常。已运行进程需重启读取新配置。

## 8. 实现与验收

- [WebSocket 路由](../../walnut-world-backend/src/walnut_backend/api/routes/dingdang_voice.py)
- [数据库上下文](../../walnut-world-backend/src/walnut_backend/adapters/postgres/dingdang_voice.py)
- [主应用装配](../../walnut-world-backend/src/walnut_backend/api/app.py)
- [豆包适配器](../python/yaya_agent_runtime/adapters/doubao_realtime.py)
- [离线路由测试](../../walnut-world-backend/tests/unit/test_dingdang_voice.py)
- [PostgreSQL 集成测试](../../walnut-world-backend/tests/integration/test_dingdang_voice_context.py)
- [本次验证记录](../../outputs/dingdang-backend-voice-validation-2026-09-09.md)
- [运行与成长总结自动刷新的真实豆包日志](../../outputs/voice-freshness-live.jsonl)
- [最终上下文查询耗时与 SQL 计数](../../outputs/dingdang-freshness-context-benchmark.json)

2026-09-09 实测：同一语音连接在新 Run 成功且成长总结尚未生成时收到一次 `session.updated`，总结生成后再次收到更新；两次距首次数据库观察分别为 0.797 秒和 0.078 秒，使用默认 1 秒后台检测。观测脚本每 200 ms 读取数据库；上游 ACK 本身不含 Run ID，日志按 ACK 时的数据库最新 Run 关联。这验证了上下文更新确认，没有录制或验证模型对新结果的口头复述。

前端验收：同一连接连续说两轮、编辑代码后再问、回答中打断、连接中取消、再次点击关闭、切关卡关闭、语音失败后原有游戏功能仍可用。
