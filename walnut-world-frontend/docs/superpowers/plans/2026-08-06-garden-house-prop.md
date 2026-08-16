# 花园小屋摆件接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将花园小屋制作为与 `grass_patch` 对齐的透明广告牌预置，并放入 Main 场景。

**Architecture:** 以图像编辑工具生成保持构图的洋红背景版本，再通过官方去色键脚本得到 alpha PNG。独立 `garden_house.tscn` 复用 `grass_patch.tscn` 的 `Sprite3D` 参数，Main 只持有预置实例，不使用运行时生成。

**Tech Stack:** Godot 4.5.2、PNG RGBA、Sprite3D、内置 Image Gen、Pillow 色键去背辅助脚本。

## Global Constraints

- 输出文件必须为 `1254×1254` RGBA PNG，四角 alpha 为 `0`。
- 使用 `pixel_size = 0.0016`、`billboard = 1`、`transparent = true`、`shaded = false`。
- 只增加预置场景和 Main 中的预置实例；不添加运行时节点生成或碰撞。
- 不改动现有草丛、地皮、角色资源及其未提交改动。

---

### Task 1: 处理透明花园小屋贴图

**Files:**
- Create: `assets/art/environment/garden_house.png`
- Generate: `assets/art/environment/garden_house.png.import`
- Source: `D:\Chrome\ChromeDownloads\ChatGPT Image 2026年8月6日 21_42_56.png`

**Interfaces:**
- Consumes: 现有 `1254×1254` 洋红背景建筑插画。
- Produces: `res://assets/art/environment/garden_house.png`，供 `garden_house.tscn` 直接引用。

- [x] **Step 1: 生成保持构图的纯色背景编辑结果**

调用内置 Image Gen，使用以下约束：

```text
Use case: background-extraction
Asset type: Godot 3D billboard prop
Input image: Image 1 is the edit target.
Primary request: preserve the supplied hand-painted garden house exactly; change only the background to a perfectly flat #ff00ff chroma-key color for alpha extraction.
Constraints: preserve every house detail, the white outline, framing, scale, and 1254×1254 square composition; no crop, no repositioning, no style changes, no new elements, no text, no watermark, no shadows or floor plane.
Avoid: any magenta color inside the house or its white outline.
```

- [x] **Step 2: 转换为透明 PNG 并对齐有效像素基线**

将 Image Gen 输出复制到临时文件后，运行：

```powershell
python C:\Users\30114\.codex\skills\.system\imagegen\scripts\remove_chroma_key.py --input <generated-source.png> --out assets\art\environment\garden_house.png --auto-key border --soft-matte --transparent-threshold 12 --opaque-threshold 220 --despill
```

然后在保持 `1254×1254` 画布的前提下，把有效 alpha 边界框水平居中并移动到底部保留 4px 安全边距；不得缩放主体。

- [x] **Step 3: 校验输出**

使用 Pillow 读取输出，断言：`size == (1254, 1254)`、模式含 alpha、四角 alpha 为 `0`、有效像素的最大 y 坐标为 `1249` 或 `1250`，且不含大面积洋红背景。

- [x] **Step 4: 让 Godot 导入图片**

运行：

```powershell
$engine = 'D:\Godot\Godot_v4.5.2-stable\Godot_v4.5.2-stable_win64_console.exe'
& $engine --headless --path . --editor --import --quit
```

预期：成功生成 `garden_house.png.import`，无导入错误。

### Task 2: 创建小屋预置并接入 Main

**Files:**
- Create: `scenes/environment/garden_house.tscn`
- Modify: `scenes/main/main.tscn`
- Test: `tests/smoke_test.gd`

**Interfaces:**
- Consumes: `res://assets/art/environment/garden_house.png`。
- Produces: `GardenHouse` 预置实例与 Main 下的 `GardenHouseInstance`。

- [x] **Step 1: 增加 Main 场景验证断言**

在 `tests/smoke_test.gd` 的场景节点检查处增加：

```gdscript
var garden_house := main.get_node_or_null("GardenHouseInstance") as Node3D
if garden_house == null:

	failures.append("Main 场景缺少花园小屋预置实例")
elif garden_house.get_node_or_null("Billboard") == null:

	failures.append("花园小屋缺少 Billboard")
```

- [x] **Step 2: 创建预置小屋场景**

创建 `garden_house.tscn`，结构和参数如下：

```text
GardenHouse (Node3D)
└── Billboard (Sprite3D)
    position = (0, 1.0, 0)
    texture = res://assets/art/environment/garden_house.png
    billboard = 1
    pixel_size = 0.0016
    transparent = true
    shaded = false
```

- [x] **Step 3: 在 Main 中实例化**

为 `garden_house.tscn` 添加外部资源，并将 `GardenHouseInstance` 放在 `Vector3(2.8, 0, -1.2)`。该位置位于现有世界边界内，且与四处草丛摆件的根节点位置保持至少约 2m 间距。

- [x] **Step 4: 验证场景与冒烟测试**

运行：

```powershell
$engine = 'D:\Godot\Godot_v4.5.2-stable\Godot_v4.5.2-stable_win64_console.exe'
& $engine --headless --path . --editor --import --quit
& $engine --headless --path . --script res://tests/smoke_test.gd
```

预期：主场景能导入；若冒烟测试仍报告 `walk_down 不是 6 帧`，将其记录为既存角色资源不匹配，不修改角色资源。

- [x] **Step 5: 按功能提交**

先暂存新资源、预置、测试和计划。`main.tscn` 已含用户的未提交序列化改动，因此只使用定向补丁暂存新增外部资源和 `GardenHouseInstance` 两个小屋 hunk；不要整文件暂存。

```powershell
git add assets/art/environment/garden_house.png assets/art/environment/garden_house.png.import scenes/environment/garden_house.tscn tests/smoke_test.gd docs/superpowers/plans/2026-08-06-garden-house-prop.md
git diff --cached --check
git commit -m "feat: 接入花园小屋摆件"
```
