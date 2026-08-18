# Pastel Billboard Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个可运行的 Godot 4.5.2 3D 广告牌精灵演示。

**Architecture:** 预置主场景组合玩家、地面、草丛和相机。角色移动在物理帧执行，精灵表由预置 `SpriteFrames` 管理。

**Tech Stack:** Godot 4.5.2、GDScript、Image Gen PNG 资源、Git。

## Global Constraints

- 使用 3D `Camera3D` 弱透视，不使用正交投影。
- 角色必须是 `CharacterBody3D`，移动在 `_physics_process`。
- 输入只使用 `move_up`、`move_down`、`move_left`、`move_right` InputMap 动作。
- 不运行时生成场景对象。

### Task 1: 基础项目与资源

**Files:** `project.godot`、Git 元数据、`assets/art/**`、美术文档。

- [x] 创建目录、Git 初始化、远端和忽略规则。
- [x] 生成行走、待机、草丛 PNG 并去除键控背景。

### Task 2: 预置场景与脚本

**Files:** `resources/sprites/player_frames.tres`、`scenes/**`、`scripts/**`。

- [x] 创建 Atlas 帧、玩家、草丛和主场景。
- [x] 配置 InputMap 与相机跟随。

### Task 3: 验证

**Files:** 所有项目文件。

- [x] 通过 Godot 4.5.2 headless 静态加载验证资源、场景与脚本。
- [x] 在运行时验证 InputMap、移动、动画选择、草丛复用与错误日志。
