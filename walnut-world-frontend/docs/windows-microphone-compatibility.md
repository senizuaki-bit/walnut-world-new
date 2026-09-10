# Windows 多声道麦克风兼容记录

## 问题与结论

2026-09-10，用户点击「问叮当」后没有文字框。实际游戏日志大量报告
`WASAPI: unsupported channel count in microphone!`。本机唯一启用的录音设备是
Intel 数字麦克风，当前设备格式为四声道。Godot WASAPI 的录音读取分支仅处理
单声道和双声道；其他声道数写入零采样。因此，先前注入合成 PCM 的三端测试通过，
不能证明这台机器原生录音正常。关闭播放声音本身不会关闭字幕。

另一个前端问题是：连接后只在转写/回复到来时显示 ReplyPanel，麦克风失败时看起来没有反应。

## 修复与边界

- 点击时立即显示连接状态，ready 后提示对着麦克风说话、说完稍等自动回答。
- 提前结束且没有文字时保留说明；服务错误显示在对话框内；已收到的字幕在结束后保留。
- 为纸色面板设置深色文字，状态文字自动换行。
- 原生 AudioStreamMicrophone 仍为默认采集方式。对本机已确认不兼容的设备，采用可选
  FFmpeg DirectShow 兼容采集，输出 16 kHz、单声道、S16LE，经内存管道按 640 字节分帧。
- 兼容采集仅在服务器 ready 后启动，不启动 Godot 原生麦克风；关闭、失败、场景退出时终止
  本次采集子进程。不保存音频，不修改 Windows 默认设备或系统格式。
- FFmpeg 是本机已有依赖，未下载或打包进项目。游戏/供应商凭据不会传给采集子进程。
  缺少组件、设备失联、启动后无数据或积压时，返回明确错误并释放采集。
- 后端和 Agent 继续保持 origin/main / 00052e9，改动仅前端；本地提交，不推送。

Godot 源码依据：[WASAPI 录音分支](https://github.com/godotengine/godot/blob/master/drivers/wasapi/audio_driver_wasapi.cpp)。

## 本机配置

配置位置为 `user://voice-input.cfg`。当前项目在 Windows 的实际路径是
`%APPDATA%/Godot/app_userdata/核桃代码世界/voice-input.cfg`。
此文件不含凭据，不进入 Git。示例中的可执行文件和设备名须使用本机真实值：

```ini
[capture]
mode="ffmpeg"
executable="D:/tools/ffmpeg/bin/ffmpeg.exe"
device="麦克风的 DirectShow 完整名称"
```

使用 `ffmpeg -hide_banner -list_devices true -f dshow -i dummy` 查询设备名。
更换设备后更新配置；把 mode 改为 `native` 可恢复原生采集。没有这份配置的机器默认使用原生采集。

## 验证

- `tests/level_demo/mentor_question_test.gd`：连接/聆听阶段显示、无回复提前结束、结束后保留字幕、
  服务失败展示、协议和 PCM 生命周期通过。
- `scripts/testing/voice_capture_live_test.gd`：设置 `WALNUT_LIVE_MIC_CHECK=1` 后才访问真实麦克风。
  本机四秒检查得到 156 个 20 ms 帧、13853 个非零样本；生产客户端采集和发送分帧检查通过，
  关闭后无采集子进程残留。该检查的网络层是测试替身，不声称识别人说了什么。
- 完整 MentorQuestion 界面→main Gateway→豆包，以合成录音验证两轮回答和上下文更新，
  结束前后 ReplyPanel 可见且包含真实返回文字。
- 真实渲染截图确认聆听阶段对话框位于叮当师傅旁。

本机证据位于组合工作树的 `audit-results/voice-real-microphone.log`、
`voice-visible-status-test.log`、`live-doubao-frontend-after-fix.log`、`voice-listening-visible.png`。
真人朗读后的识别质量、回声和耳机听感仍需人工检查。
