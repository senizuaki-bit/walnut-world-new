# fronted-art 语音文字与打断按钮优化

2026-09-10：`fronted-art` 原本仍是长按、固定台词的原型；先将本地联调分支已验证的
29 个前端文件同步回来，再优化语音 UI。未迁入后端或 Agent，未推送远端。
运行联调仍使用 `D:/FeishuAIreview/walnut-interface-audit-20260909` 的固定 main 后端。

## 行为

- 预置 ReplyPanel 保持在叮当旁的侧栏，不扩大横向范围遮挡题板或代码编辑器。
- 使用可选择文字的 RichTextLabel，区分「你说」与「叮当师傅」，回答字号 17，增加行间距。
  用户/模型原文通过 add_text 写入，不解析为富文本命令。
- 文字区独立滚动。手动向上翻阅或选择文字时暂停自动跟随；点「最新」回到末尾并恢复跟随。
  新片段到来时保留手动滚动位置；选中文字时暂缓重绘，恢复跟随后显示已收到的全部内容。
- 「打断回答」与「收起」固定在文字区外，操作行最小高度 42，采用独立 Theme 的清晰状态样式。
  「收起」只隐藏文字；底部「结束对话」仍关闭语音。
- 聆听或断开时不能打断；生成或播放回答时可打断。生成结束不等于播放结束，已排队或已进入
  AudioStreamGenerator 的音频尚未播完时仍保留打断能力。打断后保留文字和连接。

## 验证

- `mentor_question_test.gd` 通过：协议生命周期、空 done、取消、手动滚动、恢复跟随、原文显示、
  播放积压时打断、按钮最小高度与滚动区隔离。
- `crop_agent_bridge_test.gd` 通过，确认同步的前端正式交互入口可用。
- Godot 实际渲染 1280×720 的回答/完成状态及 960×540 的回答状态，检查按钮与文字边界。
- 本轮为 UI 与播放状态改动，没有重跑会产生费用的完整模型工作流。

主要文件：`scenes/ui/mentor_question.tscn`、`scenes/ui/mentor_question.gd`、
`resources/ui/voice_actions.tres`、`scripts/client/dingdang_voice_client.gd`。
