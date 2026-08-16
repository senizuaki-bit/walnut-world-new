# 橡果农场边缘 HUD 重制设计

## 目标

把主界面从“工具覆盖游戏”的工作台改成“农场是舞台、工具按需浮出”的游戏 HUD。常态画面至少 88% 保持为可见主世界，不设置常驻左侧栏或底部操作条；代码编辑、重置、提交和教学反馈统一进入右侧抽屉。

## 已固定的视觉方案

- 视觉名：橡果农场边缘 HUD。
- 参考对象：《编程农场》（The Farmer Was Replaced）的世界优先层级和按需代码窗口，不复制其暗色工业 UI。
- 项目语言：奶油色布面、薄荷绿木框、蜂蜜橙木材、深森林绿文字；圆润、厚实、轻手绘，匹配现有小核桃、花园小屋和种子站素材。
- 主状态参考：`docs/design/references/walnut-world-edge-hud-closed.png`。
- 编辑状态参考：`docs/design/references/walnut-world-edge-hud-editor-open.png`。
- 运行时任务签使用 Image Gen 生成并经色键去背的 `assets/art/generated/ui/task_seed_banner.png`，任务文字由 Godot 节点叠加，避免图片文字失真。

## 信息层级

### 常态

1. 世界视图铺满整个 `TaskWorkspace`。
2. 左上角只显示一枚 400×116 左右的任务签；默认只展示任务名和单行目标，点击可展开进度，不占用整条左侧。
3. 右上角只保留“代码”和“提示”两个 58×58 的实体感按钮。
4. 自动保存状态压缩成右上角一枚叶片状态胶囊，状态稳定后降低存在感。
5. 底部没有常驻按钮，只允许短时结果通知从底部中央浮出并自动消失。

### 代码抽屉

1. 抽屉从右侧覆盖进入，不改变世界视图尺寸；1280×720 下宽度为 460 像素，约占 36%。
2. 顶部包含代码卷轴标识、标题、自动保存状态和关闭按钮。
3. 中间由深森林绿代码区和紧凑教学反馈区组成。
4. 底部仅保留“重置”和“提交并运行”两个动作；提交继续沿用保存、编译、激活、运行、播放动画的一条龙链路。

## 预置节点结构

```text
TaskWorkspace (Control)
├── WorldViewport (instance)
├── Hud (CanvasLayer)
│   └── SafeArea (MarginContainer)
│       └── EdgeLayer (Control)
│           ├── TaskTag (Control)
│           ├── ToolRail (HBoxContainer)
│           ├── AutoSavePill (PanelContainer)
│           └── Toast (instance)
├── DrawerLayer (CanvasLayer)
│   └── CodeDrawer (Control)
│       └── DrawerSurface (PanelContainer)
│           └── DrawerMargin (MarginContainer)
│               └── Content (VBoxContainer)
│                   ├── DrawerHeader
│                   ├── CodeEditorPanel (instance)
│                   ├── DialoguePanel (instance)
│                   └── RunControlPanel (instance)
├── ConfirmationDialog / AcceptDialog
├── AutoSaveTimer
└── ToastTimer
```

UI 以 `.tscn` 预置节点为主；脚本只负责状态投影、信号连接和 Tween，不在运行时搭建控件树。

## 补间动画

- 首次进入：任务签从左侧 18 像素处以 `TRANS_BACK + EASE_OUT` 滑入并淡入；工具按钮以 0.05 秒间隔轻微错峰出现。
- 任务签展开：只改变预置文本容器的最大高度和透明度，0.22 秒 `TRANS_CUBIC + EASE_OUT`；收起后恢复紧凑状态。
- 抽屉打开：0.30 秒 `TRANS_QUINT + EASE_OUT` 从屏幕右侧滑入；内部内容延迟 0.08 秒淡入上移。
- 抽屉关闭：0.20 秒 `TRANS_CUBIC + EASE_IN` 滑出；旧 Tween 必须先 `kill()`。
- 按钮反馈：按下时缩放到 0.94，0.12 秒回弹到 1.0；不使用循环呼吸和无意义闪烁。
- 通知：0.18 秒上浮淡入，`ToastTimer` 停留 2.6 秒后 0.20 秒淡出。

## 前端 Demo 边界

- 在后端和 Agent 接口未联调前，主界面不伪造成功返回；已有调用失败继续显示真实的能力不可用通知。
- 关卡 Demo 必须按用户提供的关卡文档另建场景，优先组合 `assets/art/generated` 中现有角色、作物、设施和地块资源。
- 当前 frontend 内尚未发现该关卡 Demo 文档，因此关卡的布局、目标、角色和交互不得自行猜测；文档到位后另写关卡实现计划。

## 验收标准

1. 1280×720 常态下没有常驻左侧栏、底栏和代码编辑器；底部中间不被固定操作 UI 遮挡。
2. 任务签、工具入口和保存状态位于屏幕边缘，世界主视觉中心保持无遮挡。
3. 打开抽屉后可编辑代码、查看教学反馈、重置和一键提交；关闭后抽屉不接收鼠标输入。
4. 所有新 UI 控件均在场景文件预置，脚本只处理行为。
5. 任务签确实使用本轮 Image Gen 生成的透明素材。
6. 场景加载、抽屉、任务签、自动保存、重置和提交链路测试通过。

## 验证记录（2026-08-10）

- 关闭状态实机图：`docs/design/verification/edge-hud-closed-render.png`。
- 抽屉状态实机图：`docs/design/verification/edge-hud-drawer-open-render.png`。
- 视觉对比确认：关闭状态没有左侧栏和底栏；打开状态抽屉宽 460 像素，完整显示代码、教学反馈、重置和提交动作。
- `D:\Godot\godot.cmd --headless --path . --editor --quit`：退出码 0。
- `tests/**/*_test.gd`：30 个脚本全部退出码 0，汇总为 `passed=30 failed=0 total=30`。
- Godot 代码审查：无 Critical；已用独立 `CanvasLayer` 修正抽屉层级，以 `PanelContainer → MarginContainer → VBoxContainer` 消除隐藏初始化时的高度膨胀，并为可竞争的按钮、抽屉、任务签和通知 Tween 执行替换或终止。
- 外部布局参考：[Steam《编程农场》页面](https://store.steampowered.com/app/2060160/The_Farmer_Was_Replaced/?l=schinese)。只借鉴“世界优先、编辑器按需出现”的信息层级，没有复制其资源或界面。
