# 镜头转向与缩放 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为主场景加入 Q/E 离散转向以及有范围限制的鼠标滚轮缩放。

**Architecture:** `CameraRig` 继续跟随 Player 的 XZ 坐标，并在自身维护目标 Y 轴旋转。`Camera3D` 作为预置子节点，仅按初始局部偏移方向改变距离。输入映射归入 `project.godot`，场景测试通过真实 `Input` 事件调用 Rig 的公开行为。

**Tech Stack:** Godot 4.5、GDScript、headless SceneTree 冒烟测试。

## Global Constraints

- 使用预置 `CameraRig` 与 `Camera3D`，不得动态创建相机节点。
- Q/E 的固定步长为 45 度；缩放范围为 6.0 至 16.0 米，默认距离为 10.0 米。
- 保持玩家世界坐标移动方向与现有相机跟随行为不变。

---

### Task 1: 先定义相机控制行为测试

**Files:**
- Modify: `tests/smoke_test.gd`
- Modify: `project.godot`

**Interfaces:**
- Consumes: `CameraRig` 节点与 `Camera3D` 子节点。
- Produces: 对 `camera_rotate_left`、`camera_rotate_right`、滚轮输入和距离边界的自动验证。

- [ ] **Step 1: 写入失败测试**

在 `tests/smoke_test.gd` 中检查两个 Input Map 动作，并实例化主场景后发送 Q/E 与滚轮事件；断言 Y 旋转变化为 45 度、距离限制保持在 6 至 16。

- [ ] **Step 2: 运行并确认失败**

运行：`Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/smoke_test.gd`

预期：失败原因是缺少新的输入映射与 CameraRig 控制行为。

### Task 2: 实现相机 Rig 输入控制

**Files:**
- Modify: `project.godot`
- Modify: `scripts/camera/camera_follow.gd`
- Test: `tests/smoke_test.gd`

**Interfaces:**
- Consumes: `InputEventAction`、`InputEventMouseButton` 与子节点 `Camera3D`。
- Produces: `rotate_left()`、`rotate_right()`、`zoom_by_steps(steps: int)` 以及受限距离的相机交互。

- [ ] **Step 1: 添加 Q/E Input Map 动作**

在 `[input]` 下添加 `camera_rotate_left`（Q）和 `camera_rotate_right`（E）。

- [ ] **Step 2: 实现最小控制逻辑**

让 Rig 缓存初始局部偏移方向和相机节点；使用 `rotation.y += deg_to_rad(45.0)` 进行离散转向，并以 `clampf(current_distance + steps, 6.0, 16.0)` 更新相机局部位置。

- [ ] **Step 3: 运行完整冒烟测试**

运行：`Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/smoke_test.gd`

预期：退出码 0，输出包含 `SMOKE_TEST_PASS`。

- [ ] **Step 4: 提交功能**

```bash
git add project.godot scripts/camera/camera_follow.gd tests/smoke_test.gd
git commit -m "feat: 添加镜头转向与缩放"
```
