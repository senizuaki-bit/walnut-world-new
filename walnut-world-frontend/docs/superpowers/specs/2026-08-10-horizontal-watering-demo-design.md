# 循环浇水魔法 Demo 固定设计

> 2026-08-10 根据试玩反馈完成第二版：旧横向装置、科技树大文本弹窗和调试面板已删除，以下内容以新流程为准。

## 体验目标

把“重复点击五块土地”的身体记忆转换成“让变量 `i` 依次代表五块土地”的代码理解。场景必须先让玩家完成五次手动浇水，再开放代码抽屉，成功运行以前不展示完成话术。

## 场景构图

- 使用独立的 `HorizontalWateringDemo` 2.5D 场景，不接入后端或 Agent。
- 固定正交相机俯看两排五列土地。第一排用于手动浇水，第二排用于循环自动浇水。
- 小核桃位于地块左侧并随当前地块横向移动；芽芽、叮当、Bug、书书按阶段显隐。
- 自动阶段由书书先施放“循环浇水魔法”，随后大水壶沿第二排 0—4 号地块逐块浇灌。
- 常态 UI 仅在屏幕边缘显示任务签、阶段进度和代码入口；中央世界与两排土地不被固定 UI 遮挡。

## 预置节点职责

```text
HorizontalWateringDemo (Node3D)
├── WorldEnvironment / DirectionalLight3D / Camera3D
├── Ground (MeshInstance3D)
├── ManualRow (5 × WateringPlot instance)
├── AutoRow (5 × WateringPlot instance)
├── Cast (Sprite3D / AnimatedSprite3D characters)
├── ManualWateringCan / MagicWateringCan (AnimatedSprite3D)
├── Hud (CanvasLayer)
│   ├── TaskCard / PhaseStrip / HintButton
│   ├── StoryDialogueOverlay / MagicSpellOverlay / CompletionCard
│   └── CodeDrawer (固定编程台)
│       └── 3 × LineEdit 填空 / Trace / Reset / Submit
└── Timers
```

`WateringPlot` 是可复用的预置子场景，包含耕地面、湿润面、幼苗、编号与点击碰撞区。主场景只组合实例；脚本只连接信号、切换预置节点、更新文本和播放 Tween。

## 状态流

1. `MANUAL_WATERING`：芽芽三句全屏对话结束后，只接受下一块连续土地；正确点击播放水壶动画并让幼苗弹出。
2. `DISCOVER_REPEAT`：第二排淡入，叮当师傅用三句全屏对话引出循环。
3. `CODE_CHALLENGE`：解锁并打开代码抽屉，自动保存本地草稿；重置恢复三个空位。
4. `RUNNING`：按解析结果逐个播放 `i` 与地块浇水，不伪造真实成功。
5. `COMPLETED`：只有 5/5、重复 0、越界 0、遗漏 0 时进入；幼苗抬叶、设备蓝纹发光、书书保存 Skill。

## 代码模拟契约

- 正确答案由三个方格构造，必须包含初值 0、上界 5、递增 `i++` 与输出变量 `i`。
- `i = 1` 产生遗漏 0 的结果。
- `i < 4` 产生遗漏 4 的结果；相同边界错误连续两次触发 Bug，显示真实记录 `0、1、2、3`。
- 输出常量 0 产生重复 0 与遗漏 1—4 的结果。
- 其他错误返回温和的通用提示，不引入本关范围外知识。

## 动效

- 世界与边缘 HUD 入场使用 `TRANS_CUBIC + EASE_OUT`。
- 地块浇水时水壶播放 8 帧浇水序列，幼苗以弹性缩放、位移和星光从土地中长出。
- 对话使用独立可复用的灰色全屏遮罩、居中人物、树叶边框、打字机和跳动继续提示。
- 正确运行先播放书书 8 帧施法与高饱和水纹叠层，再由大水壶按 0—4 顺序逐格执行。

## 美术复用与补充

- 复用：小核桃、芽芽、叮当、Bug、书书、耕地、湿润土地。
- 新增：`young_seedling.png`、`watering_can_sequence.png`、`shu_shu_magic_sequence.png` 和 `leaf_dialogue_frame.png`。新增序列与 UI 均由 Image Gen 生成并完成透明背景处理。

## 验收标准

1. 两排各五块预置地块，编号清楚，第一阶段只能依次手动浇 0—4。
2. 五次手动浇水后自动进入发现重复与科技解锁，再开放代码抽屉。
3. 重置、自动保存、提交并运行均为前端闭环；没有多余保存、编译、运行按钮。
4. 三类指定错误与连续两次 `i < 4` 的 Bug 反馈可复现。
5. 正确代码依次显示 `i = 0..4`，统计为 5/5、0、0、0 后才结算。
6. 1280×720 实机画面中央土地无遮挡，生成资产透明边缘干净。

## 验证记录（2026-08-10）

- 手动浇水：`docs/design/verification/horizontal-watering-demo-manual.png`。
- 发现重复与科技解锁：`docs/design/verification/horizontal-watering-demo-unlock.png`。
- 代码挑战：`docs/design/verification/horizontal-watering-demo-code.png`。
- 客观完成：`docs/design/verification/horizontal-watering-demo-complete.png`。
- 两张新增透明 PNG 的四角 alpha 均为 0；幼苗主体包围盒为 `(242, 313, 1003, 989)`，横向浇水器主体包围盒为 `(66, 300, 1501, 814)`。
- Godot 导入使用 VRAM 压缩、mipmap 与 Fix Alpha Border，适配 Sprite3D 远近缩放。
- TDD 已观察到生产场景缺失时两个测试退出 1；实现后结构与完整教学流测试退出 0。
- 全部 `tests/**/*_test.gd` 共 32 项通过，旧主界面、保存、提交、网络边界和地形测试未回归。
- Godot 代码审查无 Critical；审查中补齐五条手动指令、后续锁定科技和同控件 Tween 互斥，并以新增断言验证。
