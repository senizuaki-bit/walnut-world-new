# 叮当师傅长按提问原型

## 问题与结论

在保留现有固定台词、教学步骤和技能卷轴位置的前提下，让小朋友体验“向叮当师傅说话 → 师傅回复 → 我懂了”的流程。

2026-09-09 经项目负责人明确，本次仅做 Godot 前端原型。长按与回复均为本地演示：**不会读取麦克风，不会识别所说内容，不会产生真实语音，也不会发出后端或 Agent 请求**。示例回答根据当前教学步骤选择，不能视为真实 Agent 回答。

## 交互决定

- 实验页固定台词结束后，在叮当师傅下方显示带麦克风图标的“长按提问”。回到农田编写、修改代码阶段，同样显示师傅和入口。
- 原工具栏的“问叮当师傅”隐藏，通过预置占位节点保持技能卷轴及其他按钮原有横向位置。
- 鼠标、触屏或聚焦按钮后按住确认键，持续 0.35 秒进入聆听状态；短按仅提示继续按住。
- 松开后短暂显示“师傅思考中…”，随后逐字展示当前步骤的示例解释；回答期间禁用重复提问。
- 回复框支持滚动。持续生成时跟随末尾，用户向上翻阅时暂停跟随；全部文字展示后显示“我懂了”。点击后仅关闭回复框，保留提问入口。也可以直接再次长按开始下一轮。
- 移出按钮、取消触摸、Esc 或窗口失去焦点会取消长按；连续按住最长 30 秒后自动结束。
- 修改预览页也保留师傅下方入口，使用修改比较的示例解释；预览背景拦截底层操作，预览窗口允许操作右侧提问区。打开或关闭预览时清理上一轮回复。
- 固定台词、技能卷轴、技能树、Bug 反例、成长总结与完成卡显示时隐藏入口并清理未完成回复。切换教学步骤、切页、重开关卡不会带入上一轮文字。
- 实验错误提示放在代码板下方独立区域，随文字高度下移检查按钮并扩展纸面；清除错误后恢复原布局。
- 农田各阶段沿用已确认的缩小比例（横向 0.825、纵向 0.778）和下移位置。此前仅手动比较页使用该布局，其他阶段会重置为原尺寸；现已统一，打开/关闭技能卷轴也不再改变农田大小。
- 网格高度和行间距同样固定，始终预留浇水结果标签区域，避免结果页使第二排作物下移。手动比较说明区调整到农田下方，两行说明完整显示且与水量按钮分开。回归检查逐一比较 13 个阶段的八块作物位置，而不只检查外层缩放比例。

## 重要文件

- `scenes/ui/mentor_question.tscn`：预置回复框、滚动区、确认按钮、长按按钮及计时器。界面主体采用容器排版，沿用现有美术按钮纹理。
- `scenes/ui/mentor_question.gd`：本地交互状态、输入取消、逐字展示和回复生命周期。
- `scenes/level_demo/crop_adaptive_watering_demo.tscn`：预置问题组件、农田叮当立绘及原工具栏占位。
- `scenes/level_demo/crop_adaptive_watering_demo.gd`：依据教学阶段选择示例文字、协调固定台词与模态弹窗显示。
- `tests/level_demo/mentor_question_test.gd`：原生鼠标与触摸输入、短按、失焦取消、长回复、确认关闭、固定台词打断、卷轴位置和重开清理测试。

## 本地验收

用 Godot 4.7.1 打开前端工程，直接运行 `scenes/level_demo/crop_adaptive_watering_demo.tscn`（F6），完成开场、观察与手动比较后进入叮当实验。固定台词结束后即可体验提问。进入完整练习并关闭技能卷轴，可以检查农田入口。

自动交互测试：

```powershell
& $GodotExe --headless --path walnut-world-frontend --script res://tests/level_demo/mentor_question_test.gd
```

截图脚本新增 `question_idle`、`question_listening`、`question_answering`、`question_complete`、`question_farm` 五种状态。例：

```powershell
& $GodotExe --path walnut-world-frontend --script res://tests/level_demo/capture_crop_adaptive_demo.gd -- --state=question_complete --output=res://docs/design/verification/mentor-question/question_complete.png
```

实际截图：[入口](verification/mentor-question/question_idle.png)、[长按](verification/mentor-question/question_listening.png)、[回复中](verification/mentor-question/question_answering.png)、[完成](verification/mentor-question/question_complete.png)、[农田](verification/mentor-question/question_farm.png)、[固定台词](verification/mentor-question/workshop_dialogue.png)、[技能卷轴](verification/mentor-question/code.png)。

验收结果（2026-09-09）：Godot 4.7.1 离线全量首轮 81 项中 80 项通过，1 项 `crop_agent_presentation_queue_test.gd` 因固定等待 0.22 秒不足以等待动画结束而失败；改为最多等待 2 秒的实际消失状态后，单项复跑通过，未修改军团运行逻辑。最终新增交互用例再次独立通过，包含原生鼠标/触屏长按、长回复末尾可见、固定台词优先、卷轴位置和重开清理。正式网关的两项 opt-in 测试未运行。本次没有执行真实 STT/TTS 联调。

## 后续行动与未解决问题

正式语音接入不属于本次交付。未来需要用实际 STT/Agent 返回替换本地示例，并增加音频缓冲、取消与错误恢复；接入 TTS 后，“我懂了”应同时等待文字完成及最后一段语音播放结束。其他角色的固定台词和语音播放不在本次修改范围内。

## 关联分支核对

2026-09-09 直接查询远端，实际分支为 `codex/bug-legion-production-chain`，不存在精确名为 `codex/bug-legion` 的分支。其最新提交 `27f77bc` 是远端 `fronted-art` 当前提交 `6c7fd2b` 的祖先；分支比较为 `fronted-art` 多 20 个提交、Bug 分支独有 0 个。因此保留 `fronted-art` 时，可以删除该旧远端分支而不丢失独有代码。

该分支相对 `main` 仍有 24 个提交未包含，不能理解为已经全部合入 `main`。本任务只核查，不执行远端删除；所有本次提交仅保留本地。
