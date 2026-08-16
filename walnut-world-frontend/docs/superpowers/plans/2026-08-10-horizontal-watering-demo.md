# Horizontal Watering Demo Implementation Plan（初版，已由循环魔法改造取代）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-contained frontend Demo that teaches one `for` loop by turning five manual watering clicks into an automatic 0–4 watering run.

**Architecture:** Compose a dedicated Node3D level from a reusable preauthored `WateringPlot` scene, Sprite3D art, an orthographic camera, and a CanvasLayer HUD. A level controller owns the finite phase flow and deterministic local code simulator; no backend or Agent calls are made.

**Tech Stack:** Godot 4.7.1, GDScript, Node3D/Sprite3D/Control containers, Tween, standalone SceneTree tests.

---

### Task 1: Preserve requirements and complete the art set

**Files:**
- Create: `docs/level-demo/横向自动浇水器_Demo关卡方案_精简版_v1.0.md`
- Create: `docs/superpowers/specs/2026-08-10-horizontal-watering-demo-design.md`
- Create: `assets/art/generated/crops/young_seedling.png`
- Create: `assets/art/generated/facilities/horizontal_watering_rig.png`（初版资源，已在第二版删除）

- [x] Read and map the supplied level document.
- [x] Compare requirements with the existing generated-art catalog.
- [x] Generate only the missing seedling and five-nozzle horizontal rig with Image Gen.
- [x] Remove chroma key, validate transparency, and commit the requirements/art package.

### Task 2: Specify the scene contract in failing tests

**Files:**
- Create: `tests/level_demo/horizontal_watering_demo_test.gd`
- Create: `tests/level_demo/horizontal_watering_flow_test.gd`

- [x] Assert the preauthored level, rows, ten plots, cast, HUD, code drawer, tech tree, Bug card, and completion card.
- [x] Assert manual order, phase transition, autosave/reset, error simulation, Bug streak, correct trace, and guarded completion.
- [x] Run both tests and confirm RED because the production scenes do not exist.

### Task 3: Build reusable plot and level composition

**Files:**
- Create: `scenes/level_demo/watering_plot.gd`
- Create: `scenes/level_demo/watering_plot.tscn`
- Create: `scenes/level_demo/horizontal_watering_demo.gd`
- Create: `scenes/level_demo/horizontal_watering_demo.tscn`

- [x] Preauthor one reusable plot with tilled/watered surfaces, seedling, label, and Area3D input.
- [x] Preauthor two rows of five plot instances and all character/facility Sprite3D nodes.
- [x] Preauthor edge HUD, dialogue, unlock/error/completion cards, and code drawer with containers.
- [x] Implement deterministic phase transitions and local simulator.
- [x] Add replace-safe Tweens for plot, character, drawer, unlock, run, and completion feedback.
- [x] Run the two tests until GREEN.

### Task 4: Render and verify the playable Demo

**Files:**
- Create: `docs/design/verification/horizontal-watering-demo-manual.png`
- Create: `docs/design/verification/horizontal-watering-demo-code.png`
- Create: `docs/design/verification/horizontal-watering-demo-complete.png`
- Modify: `docs/superpowers/specs/2026-08-10-horizontal-watering-demo-design.md`

- [x] Import the project in headless editor mode.
- [x] Capture the real Godot states and inspect visual hierarchy and alpha edges.
- [x] Run all frontend tests and Godot code review.
- [x] Record evidence, split Chinese feature commits, archive the conversation, and report paths.
