# Main World HUD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the farm world the primary task screen with an animated code drawer and a single safe submit flow.

**Architecture:** Keep all visuals as preauthored `Control` nodes in `task_workspace.tscn`. `TaskWorkspace` owns HUD projection and UI-only tweens; `SessionController` gains a single coroutine that sequences existing authoritative save/build/activation/turn APIs without making the scene call HTTP.

**Tech Stack:** Godot 4.5, GDScript, preauthored Control/Container nodes, Tween, existing `WalnutClientStore` and `SessionController`.

## Global Constraints

- Preserve the ClientStore/SessionController/Gateway authority boundary.
- Use preauthored nodes and Containers; do not generate HUD controls at runtime.
- Reset source is `content.task.starter_skill.source_bundle`'s entrypoint file.
- No fictional activation context: submit stops with the authoritative capability error when the backend has not delivered it.
- All commits use Chinese `feat:` messages.

---

### Task 1: Add the preauthored world-first HUD and drawer nodes

**Files:**
- Modify: `scenes/task/task_workspace.tscn`
- Modify: `scenes/task/code_editor_panel.tscn`
- Modify: `scenes/task/run_control_panel.tscn`
- Modify: `tests/client/task_workspace_smoke_test.gd`

**Interfaces:**
- Produces node paths `Hud/TaskCard`, `Hud/ToolRail/CodeDrawerButton`, `CodeDrawer`, `Hud/ActionBar/ResetButton`, and `Hud/ActionBar/SubmitButton`.

- [ ] Write the smoke assertions for those preauthored paths and verify the existing scene fails them.
- [ ] Replace the `HSplitContainer` workspace shell with a full-area `WorldViewport`, overlay `Hud`, and offscreen `CodeDrawer` while retaining dialogs and `AutoSaveTimer`.
- [ ] Replace the control panel's five execution buttons with the named reset and submit controls; move CodeEdit beneath the drawer's close button and save state.
- [ ] Run `godot --headless --path . -s res://tests/client/task_workspace_smoke_test.gd` and verify the new paths load.
- [ ] Commit the scene and smoke-test changes as `feat: 重构主世界沉浸式任务界面`.

### Task 2: Implement drawer and HUD tween presentation

**Files:**
- Modify: `scenes/task/task_workspace.gd`
- Test: `tests/client/task_workspace_smoke_test.gd`

**Interfaces:**
- Produces `toggle_code_drawer() -> void`, `show_code_drawer() -> void`, `hide_code_drawer() -> void`, and `show_toast(message: String, is_error: bool = false) -> void`.

- [ ] Add a test that opens and closes the drawer and asserts its visible state and final horizontal position.
- [ ] Implement one-killable outer drawer tween (`QUINT`, ease-out) and a delayed content fade/up tween; disable stale tween callbacks by killing the previous tween before making a new one.
- [ ] Add task-card, action-bar, and toast entrance presentation with non-looping tweens; bind drawer close/open and reset/submit press signals only once in `_ready`.
- [ ] Run the smoke test and inspect a 1280×720 screenshot to ensure the world remains the main visible surface.
- [ ] Commit as `feat: 增加任务 HUD 抽屉补间动画`.

### Task 3: Preserve starter code and provide safe reset

**Files:**
- Modify: `scenes/task/task_workspace.gd`
- Test: `tests/client/task_workspace_smoke_test.gd`

**Interfaces:**
- Produces `_starter_source_from_content() -> String` and `reset_code_to_starter() -> void`.

- [ ] Add a test fixture where `content.task.starter_skill.source_bundle` has an entrypoint source, then assert reset updates CodeEdit and marks the draft dirty.
- [ ] Implement source extraction only from the verified ContentUnit starter bundle; if absent, disable reset and report a precise UI message.
- [ ] Require the preauthored confirmation dialog before destructive reset; on confirmation set editor text, call `store.mark_draft_dirty`, and start the existing debounce timer.
- [ ] Run autosave and workspace smoke tests, then commit as `feat: 支持代码重置与自动保存提示`.

### Task 4: Replace fragmented actions with an authoritative submit pipeline

**Files:**
- Modify: `autoload/session_controller.gd`
- Modify: `scenes/task/task_workspace.gd`
- Modify: `scenes/task/run_control_panel.gd`
- Create: `tests/client/submit_and_run_flow_test.gd`

**Interfaces:**
- Produces `SessionController.request_submit_and_run() -> Dictionary`.

- [ ] Write a fake gateway/product test that records the order save → build → activation → turn and verifies that the first failed stage stops later calls.
- [ ] Change `request_build`, `request_activation`, and `request_turn` to return `{ok: bool, stage: String, error?: Dictionary}` while preserving their current signal/state behavior.
- [ ] Implement `request_submit_and_run` to save dirty content, then await build, activation, and turn in that order; return an authoritative failure for unavailable server capabilities.
- [ ] Bind only `SubmitButton` to that method, update its label from `flow_state`, and present stage/failure via the toast rather than a permanent result panel.
- [ ] Run the new flow test plus the existing build/activation/turn tests; commit as `feat: 合并提交编译运行操作链路`.

### Task 5: Verify and document

**Files:**
- Modify: `docs/superpowers/specs/2026-08-10-main-world-hud-design.md`

- [ ] Run all focused workspace, draft-save, build, activation, and agent-turn tests under headless Godot.
- [ ] Capture the final primary screen with drawer closed and open, compare it with the accepted concept and reference video's thick-outline motion language, and fix observable mismatches.
- [ ] Record actual test commands and results in the design spec.
- [ ] Commit as `feat: 验证主世界任务界面体验`.
