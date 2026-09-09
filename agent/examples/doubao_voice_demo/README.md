# 问叮当实时语音浏览器测试页

这个本地页面有两条输入链路：文字通过 Walnut Backend 提交到“问叮当”，后端生成的 `AgentInteraction.feedback.message` 同时显示并交给豆包朗读；麦克风音频交给豆包实时模型直接回答，页面展示其文字和音频。语音链路目前读取已授权的课程背景，回复不写入学习档案，也不执行游戏操作。API Key 和后端 JWT 只由本机 Python 代理读取，不发送到页面。

两条链路都可以回答问候、身份、日常聊天和课程外知识问题，不强制转回代码提问。文字普通问答复用现有 `response_type=message`，不产生能力推断、Run、Evidence 或世界变化；课程问题继续使用 `question/hint` 和原有提示分级。

默认使用官方「幽默大爷 2.0」音色 `ICL_uranus_zh_male_youmodaye_tob`，叮当以亲切幽默、温和从容的大叔风格交流。原生合成语速 `speed=0`、音量 `loudness=0`，页面按 1.0 倍速播放，保留音色本身的音高和节奏；后端 PCM 为 24 kHz。可通过 `YAYA_DOUBAO_VOICE`、`YAYA_DOUBAO_VOICE_SPEED` 和 `YAYA_DOUBAO_VOICE_LOUDNESS` 覆盖音色、语速及音量；后两项范围均为 -50～100。服务端配置修改后需重启代理并重新连接页面。该音色已在当前实时接口验证返回有效音频。

在 `agent` 目录执行：

```powershell
uv sync --extra voice
uv run --extra voice python examples/doubao_voice_demo/server.py
```

先启动项目的 persistent-play 后端，再打开 <http://127.0.0.1:8764> 并点击“连接服务”。可直接在输入框问叮当；麦克风测试需允许浏览器权限，点击“开始说话”，说完点击“停止说话”。建议佩戴耳机。

默认从 `agent/doubao-voice-api.key` 读取密钥。也可设置 `YAYA_DOUBAO_VOICE_API_KEY_FILE` 或 `YAYA_DOUBAO_VOICE_API_KEY`，两者只使用一个。服务只监听 `127.0.0.1`，属于本机连通性演示，不是生产接口。
