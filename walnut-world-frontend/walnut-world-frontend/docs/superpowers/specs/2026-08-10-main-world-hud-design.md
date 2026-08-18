# 主世界 HUD 与代码抽屉设计

## 目标

把学习任务界面从左右并列的工具工作台，改为以 3D 农场世界为主画面的游戏 HUD；代码编辑器仅在玩家主动打开时从右侧出现。

## 视觉方向

- 保持项目已有的柔和 3D 农场画面为唯一主视觉，HUD 不覆盖超过必要的世界区域。
- 参考《Lexispell》UI 动画视频的封面语言：奶油色表面、深墨绿/蓝灰描边、厚实圆角、小面积黄绿色强调和有实体感的阴影。
- 不复制视频中的角色、文字或图形资源；只复用层级、轮廓和运动原则。

## 主界面结构

- 世界视图填满 `TaskWorkspace` 的可用区域。
- 左上角为紧凑任务卡：任务名、目标和学习进度。
- 右上角为纵向工具入口：代码抽屉开关与提示。抽屉关闭时仅显示自动保存状态。
- 底部居中为操作条：次要按钮“重置代码”和主按钮“提交并运行”。删除手动保存、编译、激活、运行和停止等独立操作。
- 代码抽屉从右侧覆盖约 40% 宽度，内部含代码编辑器、自动保存状态、关闭入口和“一键提交会自动编译、运行并播放动画”的说明。
- 结果和对话不再常驻占用主界面；结果以底部轻量通知显示，对话继续通过任务流呈现。

## 状态与交互

- 输入停止 0.8 秒后自动保存；保留“保存中 / 已自动保存 / 保存失败”的可见状态。
- 重置会将当前编辑器内容恢复为当前任务从服务端下发的初始 `starter_skill` 源码；执行前要求确认，并使其重新进入自动保存队列。
- “提交并运行”顺序为：等待正在进行的保存完成 → 保存脏代码 → 编译 → 激活 → 运行。任一阶段失败时停止后续阶段，并将失败原因显示为结果通知。
- 成功运行后由既有世界事件播放器接管世界动画；提交按钮在链路运行时禁用并显示当前阶段。

## 补间动画

- 代码抽屉：关闭时位于屏幕右外，透明度为 0；打开时 0.32 秒 `TRANS_QUINT + EASE_OUT` 滑入，关闭时 0.20 秒平滑滑出。
- 抽屉内主内容在打开后以 0.16 秒、12px 上移和淡入出现，避免与外层位移竞争。
- 底部操作条首次出现使用 0.42 秒轻微上移和淡入；主按钮按下缩放到 0.96 后回弹到 1.0。
- 任务卡、工具入口和结果通知采用短促的淡入/位移，不循环、不闪烁，也不为纯装饰添加动画。
- 若玩家设置了减少动画，或界面处于小窗口，所有移动动画应缩短或退化为淡入。

## 验收

1. 1280×720 下首次进入时可见完整农场世界，代码编辑器不常驻。
2. 点击代码入口可开关抽屉，动画不阻塞输入且不改变世界尺寸。
3. 代码修改后无需手动保存；保存状态随 `WalnutClientStore.draft_state` 更新。
4. 重置恢复服务端初始代码，提交只需一个按钮并按保存、编译、激活、运行顺序执行。
5. Godot 4.5 能加载场景，现有与工作台、草稿保存和指令流有关的测试全部通过。

## 验证记录（2026-08-10）

- `godot --headless --path . -s res://tests/client/task_workspace_smoke_test.gd`
- `godot --headless --path . -s res://tests/client/task_workspace_autosave_test.gd`
- `godot --headless --path . -s res://tests/client/draft_save_test.gd`
- `godot --headless --path . -s res://tests/client/build_command_flow_test.gd`
- `godot --headless --path . -s res://tests/client/activation_command_flow_test.gd`
- `godot --headless --path . -s res://tests/client/agent_turn_run_flow_test.gd`
- `godot --headless --path . -s res://tests/client/submit_and_run_flow_test.gd`
- `godot --headless --path . -s res://tests/client/world_event_player_test.gd`
- `godot --headless --path . --editor --quit`

以上命令均以退出码 `0` 完成。由于 B 站参考页的实时画面无法被当前无头渲染环境捕获，视觉比对基于可读取的封面与视频主题；项目场景加载由最后一条命令验证。
