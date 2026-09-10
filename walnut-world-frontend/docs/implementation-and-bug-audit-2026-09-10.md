# 当前界面接入完整性与缺陷检查（2026-09-10）

## 范围与结论

检查当前 `fronted-art` 前端（基线 d638aa4）及正在运行的本地 main 联调工作树。
两边前端在检查开始时一致。检查代码、预置场景、全部离线回归，并对疑点运行隔离探针。
本轮仅检查，不修改业务代码、不变更后端配置、不提交真实 Run、不改变学习进度。

主提交链此前已验证成功，但不能据此认为全部 UI 功能已完成。本轮发现两个明确的界面状态
缺陷、一个语音事件顺序缺陷，以及正式联网模式中尚未接入的功能。

## 已复现缺陷

### P2：重置代码没有同步草稿，重玩会恢复旧代码

- 触发：联网模式打开已有草稿，点击“重置”，不提交，随后重玩或返回后重新进入。
- 结果：编辑器一度显示初始代码，但 `_agent_source` 仍是原草稿；重进后原代码回来。
- 原因：`crop_adaptive_watering_demo.gd:961` 的 `_reset_code()` 直接设置 TextEdit.text，
  未主动执行草稿变更逻辑。当前 Godot 实测没有发出 `agent_draft_changed`；
  `_flush_autosave()` 只更新提示文字，没有同步草稿。
- 证据：探针记录 `draft_events=0`、`source_matches_editor=false`，
  重玩后 `editor_restored_old_source=true`。
- 建议：前端将重置作为一次明确编辑，走现有草稿变更/持久化链路；同步失效旧认证状态。
  如果点击重置后直接运行，桥接层会从编辑器取代码并补交草稿，因此不能描述成“重置后必定运行旧代码”。

### P2：预告状态污染下一次通关卡，按钮一直禁用

- 触发：联网模式成功通关 → 点击“下一关”查看预告 → 重玩本关 → 再次成功。
- 结果：完成卡仍显示“下一关正在准备中”，并保留预告正文、`next_disabled=true`。
- 原因：`show_next_level_preview()` 修改了完成卡文字、按钮状态和布局；
  `restart_level()` 没有重置这些字段；`complete_agent_submission()`（1371 行）只显示卡片，
  没有重新配置本次通关的标题、正文、按钮和布局。
- 建议：前端为正式通关、预告分别完整设置界面状态，覆盖重玩/返回后的第二次完成场景。

### P2：特定语音结束消息顺序会清空已显示回答

- 触发：同一 response_id 先收到文字增量，再收到 `response.output_audio.done`，
  最后收到空的 `response.output_text.done`。
- 结果：已经显示“你好，同一个下标。”，最后回答变成空字符串。
- 原因：`scripts/client/dingdang_voice_client.gd:216` 将音频结束设为 `_new_response=true`；
  后续同一回答的 text.done 被当作新回答，触发 `mentor_question.gd:143`
  `_on_response_started()` 清空 `_answer`，空文本又不会补回。
- 建议：按 response_id 区分新回答和同一回答不同通道的完成事件，空结束消息不得清空增量。
- 证据边界：隔离事件序列可复现；本轮未在真实豆包会话捕获这个顺序。
  现有测试只覆盖 text.done 先于 audio.done，因此通过测试仍不能排除此分支。

## 尚未完整接入的功能

| 内容 | 实际状态 | 需要处理的位置 |
|---|---|---|
| 其他角色及普通对话的 TTS | 只有“问叮当”走实时语音；普通教学、世界反馈、书书总结仍是文字 | 前端播报控制及后端公开朗读入口；底层已有 speak(text)，不必修改判题 |
| 正式联网模式的 AI 修改建议 | Crop 场景 `_can_request_patch()` 明确要求 `not _agent_mode`，`_on_patch_requested()` 在联网模式直接返回 | 将旧 TaskWorkspace 已有的能力门控、提案及接受/拒绝链接入当前 Crop 场景；不能只显示按钮 |
| 通关后的成长归档卡和技能解锁流程 | 正式成功进入 COMPLETED，下一步直接触发预告；演示版 OBJECTIVE_COMPLETE 才进入成长归档、技能解锁、自由复习 | 当前前端流程接线；书书真实反馈已经存在，不应说后端没有总结 |
| 下一关 | 明确只有预告，没有下一关可玩内容 | 需要课程/世界内容与前端场景共同交付 |
| WATER 演出与播放控制 | 默认候选兼容未配置，速度/跳过/重播受限；当前成功路径没有调用作物逐步候选演出 | 前端呈现方案与 WATER 合同边界需继续对齐；不能用演示动画代替真实世界结果 |

成长归档演示 UI 当前使用本地 `_used_hint_levels` / `_used_ai_patch` 和固定“代码变化、验证记录”文案。
正式联网提示在 `_on_hint_pressed()` 的前置分支返回，不更新这些演示计数。因此接通归档入口时，
还必须消费真实记录，不能直接展示演示汇总并宣称它是学生的真实学习路径。

## 验证结果

- Godot 4.7.1 全部离线套件：84 项执行，83 通过，1 失败；2 个真实 Gateway opt-in 套件按规则未执行。
  已隔离 APPDATA，避免测试覆盖当前玩家本地缓存。
- 唯一失败是 `tests/client/objective_failure_feedback_flow_test.gd:355` 仍期待
  `TURN_COMMAND_FAILED`。当前实现按之前修复保留 `PROVIDER_UNAVAILABLE`。
  在临时测试副本仅修改这一预期后，包含读取次数、游标、反馈和清理断言的整个测试通过。
  这是测试预期过期，本轮没有复现该断言文案所说的服务失败被误当目标失败。
- Agent 语音适配器离线测试：19 项通过。
- 自定义 UI 探针复现上述重置、重复通关和语音结束顺序问题；正式归档入口及 Patch 缺口有代码证据。
- 无头场景退出仍有 2 个 ObjectDB / 1 个资源未释放警告，尚未定位归属；
  不据此断言持续运行时存在内存增长。

证据位于本地工作树 `D:/FeishuAIreview/walnut-interface-audit-20260909/audit-results/`：
`full-offline-audit-20260910.log`、`audit-ui-probes.gd`、`audit-ui-probes.log`、
`objective-failure-updated-expectation.gd` 和同名 `.log`。未将包含其他历史联调资料的整个目录提交。

## 建议修复顺序与边界

1. 前端重置同步、通关卡状态重置，补用户操作序列回归。
2. 前端语音 response_id 生命周期修复，补不同通道完成顺序测试。
3. 更新过期测试预期，恢复离线总套件全绿。
4. 正式成长归档接入真实反馈/记录；再接入受控 AI 修改建议。
5. 协调 TTS 公共入口、WATER 演出、下一关内容。

前三项可只改前端与测试；正式归档和 Patch 优先复用已有后端能力，仍需核对投影是否提供全部展示字段。
本轮未重新进行收费模型长流程、真人语音体验测试或飞书/秒搭生产部署验收，不能把这些部分记为本轮通过。

关联：`service-failure-ui-and-backend-followup.md`（模型迁移和 Agent 校验修复已完成）。

## 补充：仅前端范围检查

在用户明确只考虑前端后，补查启动门禁、主动提示和语音阅读状态。本节不要求修改后端。

### P1：启动失败仍可进入农场，且没有应用内重试

- `app_root.gd` 的 `_start()` 设置 `_starting=true` 后，失败路径不恢复；
  `_finish()` 设置 `_startup_reported=true`。再次调用 `_start()` 不会重新启动。
- `game_start_screen.gd` 的进入按钮在动画开始时就可点击，没有绑定启动就绪状态。
  `CropAgentBridge` 未取得初始投影时又会直接忽略提交请求。
- 隔离无 token 场景复现：`AUTH_TOKEN_MISSING`，`enter_disabled=false`；点击后
  `level_visible_after_enter=true`，但 `projection_active=false`；第二次启动未产生新结果。
- 改进：前端启动中/失败/已就绪三种状态明确展示；进入门禁跟随就绪状态；
  对可恢复连接失败提供受控重试。缺失或过期凭据仍应明确提示所需配置，不伪造登录成功。

### P2：结束后的语音文字会因切换窗口而被清空

- `mentor_question.gd` 将窗口 `focus_exited` 直接连接 `reset()`；
  reset 同时清空 `_answer`、`_transcript` 并隐藏对话框。
- 复现：回答已经结束、语音处于 IDLE，仍可见的文字在模拟失焦后变为空字符串。
- 改进：失焦时停止录音/播放即可；保留已经完成的文字，直到用户明确清除或切换会话。
  暂停采集与删除阅读内容应分开处理。

### 主动分层提示入口缺失

- Crop 场景 `_set_phase()` 无条件设置 `hint_button.visible=false`；本轮探针也确认 CODE
  阶段不可见。旧的正式 `agent_hint_requested → request_hint` 链路有实现，但没有可见按钮触发。
- 当前“问叮当”是独立实时语音，不会提交正式 Hint Turn，不能视为原分层提示入口的等价替代。
- 改进：按交互设计保留一个清晰的“给我提示”入口，复用现有前端控制器，不增加后端接口。

### 前端体验待完善（不冒充已复现故障）

- 完成/准备阶段仍有 Run、Receipt、Snapshot、world revision、state_hash 等工程信息进入
  学生主界面；应将正常文案改为儿童能理解的描述，诊断细节放到可展开区域。
- 当前语音面板没有选择麦克风、输入电平检测或键盘输入替代入口。麦克风兼容设置依赖
  `user://voice-input.cfg`。可先补选麦和电平反馈；恢复现有文字提示按钮即可提供无麦帮助。

补充证据：本地 `audit-results/frontend-extra-probes.gd` 及 `frontend-extra-probes.log`。
探针未连接服务、未采集真实音频、未修改当前玩家状态；本节仍为检查结果，尚未修复。
