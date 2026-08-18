# Edge Farm HUD Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a polished edge-only farm HUD with an on-demand code drawer, generated UI artwork, and non-blocking Tween feedback while preserving the existing frontend task flow.

**Architecture:** Keep `TaskWorkspace` as the composition root but move HUD presentation into preauthored edge controls. Reuse the existing editor, dialogue, run controls, store projections, autosave, and submit chain; the script only coordinates signals and animation. The level Demo is intentionally a separate deliverable because its source document is not present in the frontend workspace.

**Tech Stack:** Godot 4.7.1, GDScript, Control/Container scenes, CanvasLayer, Tween, headless SceneTree tests.

## Global Constraints

- Only modify the repository root containing this plan (`../../..`).
- Prefer preauthored Godot nodes; do not construct UI trees dynamically.
- Keep backend and Agent integration out of the frontend Demo.
- Git commit messages use Chinese and the format `feat: 提交信息`.
- Do not invent the missing level Demo document's requirements.

---

### Task 1: Lock visual references and generated artwork

**Files:**
- Create: `docs/design/references/walnut-world-edge-hud-closed.png`
- Create: `docs/design/references/walnut-world-edge-hud-editor-open.png`
- Create: `assets/art/generated/ui/task_seed_banner.png`
- Create: `docs/superpowers/specs/2026-08-10-edge-farm-hud-redesign.md`

**Interfaces:**
- Produces: `res://assets/art/generated/ui/task_seed_banner.png` for the preauthored task tag.

- [x] **Step 1: Generate the closed and editor-open concept states with Image Gen**
- [x] **Step 2: Generate a blank task banner on a flat magenta chroma key**
- [x] **Step 3: Remove the chroma key and inspect the alpha PNG**
- [x] **Step 4: Save the fixed design specification**
- [x] **Step 5: Commit**

```powershell
git add docs/design/references assets/art/generated/ui docs/superpowers/specs/2026-08-10-edge-farm-hud-redesign.md docs/superpowers/plans/2026-08-10-edge-farm-hud-redesign.md
git commit -m "feat: 固化橡果农场边缘界面设计"
```

### Task 2: Test the edge-only scene contract

**Files:**
- Modify: `tests/client/task_workspace_smoke_test.gd`

**Interfaces:**
- Consumes: `TaskWorkspace` scene.
- Produces: assertions for `Hud/SafeArea/TaskTag`, no `Hud/ActionBar`, drawer-local run controls, task-tag fold behavior, and hidden drawer input.

- [x] **Step 1: Update node-path assertions to the desired preauthored tree**
- [x] **Step 2: Assert the legacy bottom ActionBar is absent**
- [x] **Step 3: Assert the task tag toggles between compact and expanded states**
- [x] **Step 4: Run the smoke test and confirm it fails because the scene is still legacy**

```powershell
D:\Godot\godot.cmd --headless --path . -s res://tests/client/task_workspace_smoke_test.gd
```

### Task 3: Implement the edge HUD and drawer-local actions

**Files:**
- Modify: `scenes/task/task_workspace.tscn`
- Modify: `scenes/task/task_workspace.gd`
- Modify: `scenes/task/run_control_panel.tscn`
- Modify: `scenes/task/code_editor_panel.tscn`

**Interfaces:**
- Produces: `toggle_task_tag()`, `show_code_drawer()`, `hide_code_drawer()`, `animate_button_press(Control)`, and toast auto-dismiss behavior.
- Preserves: `reset_code_to_starter()`, autosave debounce, `request_submit_and_run()`, store projections, hint and patch flows.

- [x] **Step 1: Replace the legacy TaskCard and ActionBar with preauthored TaskTag, SafeArea, tool rail, save pill, and drawer-local RunControlPanel**
- [x] **Step 2: Apply the generated task banner through a TextureRect and overlay runtime labels**
- [x] **Step 3: Move reset/submit node references into the drawer and preserve the existing flow handlers**
- [x] **Step 4: Add replace-safe Tweens for intro, task fold, drawer, button press, and toast dismissal**
- [x] **Step 5: Run the smoke test until it passes**
- [x] **Step 6: Run autosave and submit flow regression tests**

```powershell
D:\Godot\godot.cmd --headless --path . -s res://tests/client/task_workspace_autosave_test.gd
D:\Godot\godot.cmd --headless --path . -s res://tests/client/submit_and_run_flow_test.gd
```

- [x] **Step 7: Commit**

```powershell
git add scenes/task tests/client/task_workspace_smoke_test.gd
git commit -m "feat: 重制无遮挡主世界边缘界面"
```

### Task 4: Implement the level Demo after its document arrives

**Files:**
- Create: `docs/superpowers/specs/2026-08-10-level-demo-design.md`
- Create: exact level scene and test paths determined by the supplied document.

**Interfaces:**
- Consumes: the user's level Demo document and existing `assets/art/generated` catalog.
- Produces: a standalone frontend-only level scene and a headless scene contract test.

- [x] **Step 1: Import and read the level Demo document from the frontend workspace**
- [x] **Step 2: Inventory required characters, crops, facilities, and terrain against existing assets**
- [x] **Step 3: Generate only documented missing art through Image Gen and validate each output**
- [x] **Step 4: Write a document-specific scene contract test and verify RED**
- [x] **Step 5: Build the level from preauthored nodes and existing scene instances**
- [x] **Step 6: Verify GREEN and commit the level as its own feature**

### Task 5: Review and verify

**Files:**
- Modify: `docs/superpowers/specs/2026-08-10-edge-farm-hud-redesign.md`
- Create: a conversation archive under `D:/NOTE/NOTE/ZK/90-待整理`.

**Interfaces:**
- Produces: test evidence, rendered screenshot comparison, code-review findings, and knowledge-base archive.

- [x] **Step 1: Run Godot project import and the full frontend test suite**
- [x] **Step 2: Capture the real Godot screen and compare it with both Image Gen references**
- [x] **Step 3: Run the Godot code-review checklist and fix material findings with tests first**
- [x] **Step 4: Record verification evidence and commit documentation separately**
- [x] **Step 5: Archive the completed conversation and report the absolute path**
