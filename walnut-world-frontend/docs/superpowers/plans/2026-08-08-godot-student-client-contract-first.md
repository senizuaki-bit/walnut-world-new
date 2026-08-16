# Godot Student Client (Contract-first) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Godot student client that lets a learner edit a cloud draft, build and activate a Skill, initiate an Agent turn, observe authoritative world changes, and explicitly decide a structured AI patch.

**Architecture:** Keep the existing `CodeWorld` 3D farm as the world presentation layer and add a `Control`-based task workspace around it. All server data enters through validated contract gateways, then a single store and controllers drive pre-authored UI scenes; Game Command REST remains authoritative for jobs/runs/world facts, Product REST remains authoritative for content/drafts/interactions, and WSS only delivers committed world events.

**Tech Stack:** Godot 4.5.2, GDScript, `Control`/Containers, CodeEdit, HTTPRequest through `YayaAgentApiGateway`, WebSocketPeer, JSON Schema projection, headless Godot tests, contract fixtures.

## Global Constraints

- Do not implement historical `/api/*` calls from documents 01–04; document 05 and the published `contracts/manifest.json` are the Wire authority.
- No scene or UI controller may create `HTTPRequest` or parse unvalidated backend JSON; it must call a validated Gateway.
- Preserve the existing pre-authored 3D scenes under `scenes/`; favour pre-authored nodes for HUD, dialogs, characters and static world props over runtime node creation.
- The client never compiles C++, evaluates world rules, determines task success, routes Agent roles, updates learner data or calls LLM/Sandbox/DB/Feishu directly.
- Each Game/Product request has a fresh `X-Request-Id`, `X-Trace-Id`, `X-Correlation-Id`, `X-Schema-Version: 1.0.0`; each write has a stable business `Idempotency-Key`.
- A `202 Accepted` is only durable acceptance. Reconcile Game writes with `Command` at `Location`, then linked `Build`/`Activation`/`Run`; use `503 UNKNOWN_COMMIT_STATE` Location only.
- Draft writes use revision/hash CAS. Do not replace local CodeEdit text after a network failure or conflict.
- Client WSS only consumes `/v1/realtime` WorldEvents. It ACKs only the highest continuously applied sequence; a gap is backfilled with HTTP Events then Snapshot recovery.
- Historical premise: Product Experience endpoints/schemas were then absent from sibling `../agent/contracts`. They are now published; current status is documented elsewhere.

---

## Planned File Structure

```text
CodeWorld/
├── autoload/
│   ├── client_store.gd                 # typed client state and state transitions
│   ├── request_context_factory.gd      # attempt contexts and stable write keys
│   └── session_controller.gd           # only coordinator that mutates the store
├── addons/yaya_contract_client/         # versioned projection from sibling agent
│   ├── agent_api_gateway.gd
│   ├── agent_api_transport.gd
│   ├── contract_validator.gd
│   ├── http_agent_api_transport.gd
│   └── strict_json_object_scanner.gd
├── contracts/product-experience/        # generated/locked Product DTO validator assets
├── scenes/task/
│   ├── task_workspace.tscn              # pre-authored Control workspace
│   ├── task_workspace.gd
│   ├── world_viewport.tscn              # embedded current 3D farm presentation
│   ├── world_viewport.gd
│   ├── code_editor_panel.tscn/.gd
│   ├── run_control_panel.tscn/.gd
│   ├── result_panel.tscn/.gd
│   ├── dialogue_panel.tscn/.gd
│   └── patch_decision_dialog.tscn/.gd
├── scripts/client/
│   ├── product_api_gateway.gd
│   ├── command_poller.gd
│   ├── world_realtime_client.gd
│   ├── world_snapshot_renderer.gd
│   ├── world_event_player.gd
│   └── response_reconciler.gd
└── tests/client/
    ├── client_store_test.gd
    ├── product_gateway_test.gd
    ├── command_poller_test.gd
    ├── world_realtime_client_test.gd
    ├── world_event_player_test.gd
    └── fixtures/
```

Sibling `agent` owns source contracts and the shared gateway tests. The frontend owns scenes, controllers, its adapter composition root, and client-behaviour tests.

### Task 1: Release the missing Product contract before coding Product calls

**Files:**
- Create: `../agent/contracts/openapi/product-experience.openapi.json`
- Create: `../agent/contracts/schemas/product-experience/{content-unit,session-workspace,skill-draft,skill-draft-upsert-request,agent-interaction,agent-interaction-page,skill-patch,patch-decision-request,patch-decision-receipt,product-write-reconciliation}.schema.json`
- Modify: `../agent/contracts/manifest.json`
- Modify: `../agent/clients/godot/contract_validator.gd`
- Test: `../agent/tests/**/product-experience*`

**Interfaces:**
- Produces the exact Product operation IDs and strict validators for `getProductContentUnit`, `getProductSessionWorkspace`, `getProductSkillDraft`, `upsertProductSkillDraft`, `listProductAgentInteractions`, `getProductAgentInteraction`, and `recordProductPatchDecision`.
- Blocks all later Product API tasks until `npm run verify` succeeds.

- [ ] **Step 1: Add failing legal and illegal fixture tests for each Product schema.**

```json
{"draft_id":"draft_demo","revision":3,"content_hash":"sha256:...","source":"void run(){}"}
```

Add counterpart cases for an extra field, stale revision/hash, unknown Patch operation, and mismatched path/body identifiers.

- [ ] **Step 2: Run contract verification and confirm it fails because the Product surface is absent.**

Run: `npm run verify`

Expected: failure naming the absent Product OpenAPI/Schema or its missing validator projection.

- [ ] **Step 3: Define the closed Product OpenAPI and schemas, then generate the Godot projection.**

The upsert request must carry a complete source, `base_revision`, `base_hash`, and immutable request context; the decision request must carry `ACCEPT` or `REJECT`, interaction/patch/draft/skill identities and base CAS values. Set `additionalProperties: false` on every v1 object.

- [ ] **Step 4: Run the strict contract suites.**

Run: `npm run validate; npm run test:godot; npm run verify`

Expected: all Product and existing Game validators accept legal examples and reject every negative case.

- [ ] **Step 5: Commit the frozen contract surface.**

```powershell
git add contracts clients/godot tests
git commit -m "feat: 发布产品体验接口合同"
```

### Task 2: Bring the shared validated gateway into the Godot project

**Files:**
- Create: `addons/yaya_contract_client/*.gd`
- Create: `scripts/client/client_composition_root.gd`
- Modify: `project.godot`
- Test: `tests/client/gateway_boundary_test.gd`

**Interfaces:**
- Consumes the verified files from Task 1.
- Produces `ClientCompositionRoot.game_api: YayaAgentApiGateway` and a Product gateway for `SessionController`.

- [ ] **Step 1: Write a boundary test that proves UI code receives only validated result unions.**

```gdscript
func test_invalid_response_is_not_exposed_as_a_value() -> void:
	var result := await gateway.get_bootstrap(attempt)
	assert_false(result.ok)
	assert_eq(result.status, 0)
	assert_eq(result.error.scope, "CLIENT_LOCAL")
```

- [ ] **Step 2: Run the test and confirm it fails before the composition root exists.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/gateway_boundary_test.gd`

Expected: failure because the test gateway/composition root cannot be loaded.

- [ ] **Step 3: Vendor the verified shared gateway under `addons/yaya_contract_client`, preserving its tests and revision metadata.**

Set every internal preload to `res://addons/yaya_contract_client/...`; inject API base URL and Bearer at runtime, never in scenes. Register only `ClientStore` and `SessionController` as autoloads.

- [ ] **Step 4: Rerun boundary and upstream contract tests.**

Run from the frontend root, then sibling Agent: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/gateway_boundary_test.gd`; `Set-Location ..\agent`; `npm run test:godot`.

Expected: malformed/unknown wire data is rejected before any controller observes it.

- [ ] **Step 5: Commit the client boundary.**

```powershell
git add addons scripts project.godot tests
git commit -m "feat: 接入合同校验网关"
```

### Task 3: Implement state, request identity, and reconciliation primitives

**Files:**
- Create: `autoload/client_store.gd`
- Create: `autoload/request_context_factory.gd`
- Create: `scripts/client/response_reconciler.gd`
- Test: `tests/client/client_store_test.gd`

**Interfaces:**
- Produces `ClientStore.set_workspace(workspace)`, `set_draft(draft)`, `mark_draft_dirty(source)`, `set_world(snapshot)`, and `replace_world(snapshot)`.
- Produces `RequestContextFactory.new_attempt()` and `idempotency_key_for(operation, business_id)`.

- [ ] **Step 1: Write state tests for no-data-loss and write reconciliation.**

```gdscript
func test_cas_conflict_keeps_local_dirty_source() -> void:
	store.mark_draft_dirty("new local source")
	store.record_draft_conflict(server_draft)
	assert_eq(store.local_source, "new local source")
	assert_eq(store.draft_state, ClientStore.DraftState.CONFLICT)
```

- [ ] **Step 2: Run the store tests and confirm they fail before state types exist.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/client_store_test.gd`

Expected: load failure for `ClientStore`.

- [ ] **Step 3: Implement typed, single-owner state transitions.**

```gdscript
enum DraftState { CLEAN, DIRTY, SAVING, CONFLICT, SAVE_FAILED }
enum FlowState { BOOTSTRAPPING, READY, BUILDING, ACTIVATING, TURN_RUNNING, PLAYING, ERROR }
```

Keep `current_world_revision`, `last_applied_sequence`, `applied_event_ids`, canonical resource references and pending command location in the Store. Do not place arbitrary response dictionaries in UI nodes.

- [ ] **Step 4: Rerun state tests.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/client_store_test.gd`

Expected: dirty text survives conflict/network error; snapshot replacement resets sequence atomically.

- [ ] **Step 5: Commit state primitives.**

```powershell
git add autoload scripts/client tests/client
git commit -m "feat: 建立学生端状态与对账基础"
```

### Task 4: Build the pre-authored task workspace around the existing farm

**Files:**
- Create: `scenes/task/task_workspace.tscn`
- Create: `scenes/task/task_workspace.gd`
- Create: `scenes/task/world_viewport.tscn`
- Create: `scenes/task/{code_editor_panel,run_control_panel,result_panel,dialogue_panel}.tscn`
- Modify: `scenes/main/main.tscn` only to expose the farm as an instantiable world root
- Test: `tests/client/task_workspace_smoke_test.gd`

**Interfaces:**
- Produces node paths `TaskTitle`, `TaskGoal`, `WorldViewport`, `DialoguePanel`, `CodeEditorPanel`, `RunControlPanel`, and `ResultPanel`.
- Consumes `ClientStore` signals only.

- [ ] **Step 1: Write a smoke test for required pre-authored nodes.**

```gdscript
func test_task_workspace_has_all_learning_surfaces() -> void:
	var page := preload("res://scenes/task/task_workspace.tscn").instantiate()
	for path in ["TopBar/TaskTitle", "MainSplit/WorldViewport", "MainSplit/RightPanel/CodeEditorPanel", "ResultPanel"]:
		assert_not_null(page.get_node_or_null(path))
```

- [ ] **Step 2: Run the smoke test and confirm it fails before the scene exists.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/task_workspace_smoke_test.gd`

Expected: missing `task_workspace.tscn`.

- [ ] **Step 3: Assemble the workspace with `Control` Containers and a pre-authored world viewport.**

Use `HSplitContainer` with a left `SubViewportContainer`/WorldViewport and right `VBoxContainer` (dialogue, editor, controls); place result/progress panels under the split. Do not dynamically construct static UI trees in GDScript.

- [ ] **Step 4: Rerun smoke and existing terrain tests.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/task_workspace_smoke_test.gd; Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/smoke_test.gd`

Expected: workspace loads and existing farm still passes its smoke test.

- [ ] **Step 5: Commit the workspace shell.**

```powershell
git add scenes tests/client
git commit -m "feat: 搭建任务学习工作台"
```

### Task 5: Implement Product workspace and CAS-safe cloud draft editing

**Files:**
- Create: `scripts/client/product_api_gateway.gd`
- Modify: `autoload/session_controller.gd`
- Modify: `scenes/task/code_editor_panel.gd`
- Test: `tests/client/product_gateway_test.gd`

**Interfaces:**
- Produces `ProductApiGateway.load_workspace(session_id)` and `save_draft(session_id, draft_id, source, base_revision, base_hash, idempotency_key)`.
- Consumes frozen Product validators from Task 1 and Store transitions from Task 3.

- [ ] **Step 1: Write tests for open/recover, debounce save, 409 conflict, and uncertain-write reconciliation.**

```gdscript
func test_compile_flushes_current_editor_text_before_build() -> void:
	editor.set_text("latest source")
	await controller.prepare_build()
	assert_eq(fake_product.last_upsert.source, "latest source")
```

- [ ] **Step 2: Run tests and confirm missing Product gateway failure.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/product_gateway_test.gd`

Expected: failed preload or unimplemented method.

- [ ] **Step 3: Implement the Product read sequence and CAS writer.**

Read immutable content by `(unit_id, content_version, content_hash)`, then Workspace, Draft and paged Interaction history. Debounce to 1.5 seconds; save a complete source with the last canonical revision/hash; on ambiguous response, GET the canonical Location before a byte-identical replay.

- [ ] **Step 4: Rerun Product tests.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/product_gateway_test.gd`

Expected: current editor text is built, conflicts preserve local text, and old Product fields are rejected.

- [ ] **Step 5: Commit Product and editor integration.**

```powershell
git add autoload scripts/client scenes/task tests/client
git commit -m "feat: 支持云端草稿与版本冲突处理"
```

### Task 6: Implement Game command polling for build and activation

**Files:**
- Create: `scripts/client/command_poller.gd`
- Modify: `autoload/session_controller.gd`
- Modify: `scenes/task/run_control_panel.gd`
- Modify: `scenes/task/result_panel.gd`
- Test: `tests/client/command_poller_test.gd`

**Interfaces:**
- Produces `CommandPoller.reconcile(accepted_result)` and signals `resource_ready(resource)` / `command_failed(error)`.
- Consumes `YayaAgentApiGateway.get_command`, `get_skill_build`, and `get_skill_activation`.

- [ ] **Step 1: Write polling tests for normal 202, `UNKNOWN_COMMIT_STATE`, terminal command failure and build hash mismatch.**

```gdscript
func test_unknown_commit_state_polls_returned_location_without_resubmit() -> void:
	var result := await poller.reconcile(unknown_commit_error)
	assert_eq(fake_api.command_gets, ["cmd_12345678"])
	assert_eq(fake_api.build_submits, 0)
```

- [ ] **Step 2: Run the poller test and confirm failure.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/command_poller_test.gd`

Expected: `CommandPoller` missing.

- [ ] **Step 3: Implement build then activation orchestration.**

On build: flush Draft → `submit_skill_build` → reconcile Command → fetch Build → require returned source hash equals submitted hash. On certified version: `activate_skill_version` → reconcile Command → fetch immutable activation → mark the version active. Button state must permit no parallel write in the same business action.

- [ ] **Step 4: Rerun polling tests.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/command_poller_test.gd`

Expected: only activated, certified versions enable Run; each failure has a retriable/non-retriable presentation.

- [ ] **Step 5: Commit command orchestration.**

```powershell
git add autoload scripts/client scenes/task tests/client
git commit -m "feat: 支持构建激活与命令轮询"
```

### Task 7: Implement turns, Runs, interactions and Patch decision

**Files:**
- Modify: `autoload/session_controller.gd`
- Create: `scenes/task/patch_decision_dialog.tscn`
- Create: `scenes/task/patch_decision_dialog.gd`
- Modify: `scenes/task/{dialogue_panel,result_panel,run_control_panel}.gd`
- Test: `tests/client/agent_turn_and_patch_test.gd`

**Interfaces:**
- Produces `SessionController.request_turn(input)`, `request_hint()`, `decide_patch(accept: bool)`.
- Consumes `Command.links.run`, `get_run`, Product Interaction reads and Product patch decision writes.

- [ ] **Step 1: Write tests for a degraded Agent result, feedback/run identity mismatch, accepted Patch, rejected Patch, and duplicate decision.**

```gdscript
func test_rejecting_patch_keeps_current_draft() -> void:
	await controller.decide_patch(false)
	assert_eq(store.local_source, original_source)
	assert_eq(fake_product.last_decision.decision, "REJECT")
```

- [ ] **Step 2: Run the test and confirm it fails.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/agent_turn_and_patch_test.gd`

Expected: missing controller/dialog implementation.

- [ ] **Step 3: Implement objective-result-first presentation.**

Create Agent Session/Turn through Game API, reconcile its command, fetch linked Run/Evidence and then Product Interaction. Validate same session, turn, command, run and evidence identities. Render Run objective facts before dialogue; on `degraded=true`, show the fallback reason and never wait indefinitely.

- [ ] **Step 4: Implement structured Patch decision.**

Render exact server operations, explanation and base revision/hash. POST exactly one `ACCEPT`/`REJECT` decision with a stable key; accept reloads canonical Draft then returns to the Build flow, reject leaves the source unchanged.

- [ ] **Step 5: Rerun the Agent/Patch tests.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/agent_turn_and_patch_test.gd`

Expected: LLM failure preserves Run visibility; no client-side textual replacement occurs.

- [ ] **Step 6: Commit Agent interaction integration.**

```powershell
git add autoload scenes/task tests/client
git commit -m "feat: 接入教学反馈与补丁确认"
```

### Task 8: Render snapshots and replay only committed world events

**Files:**
- Create: `scripts/client/world_snapshot_renderer.gd`
- Create: `scripts/client/world_event_player.gd`
- Modify: `scenes/task/world_viewport.gd`
- Test: `tests/client/world_event_player_test.gd`

**Interfaces:**
- Produces `WorldSnapshotRenderer.render(snapshot)` and `WorldEventPlayer.play(events)`.
- Consumes validated World Snapshot/Event objects and existing pre-authored 3D farm nodes.

- [ ] **Step 1: Write playback tests for action order, failed movement, skip, and final snapshot correction.**

```gdscript
func test_failed_move_does_not_teleport_drone_to_target() -> void:
	await player.play([failed_move])
	assert_eq(world.drone_cell, Vector2i(3, 0))
```

- [ ] **Step 2: Run the test and confirm renderer/player absence.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/world_event_player_test.gd`

Expected: missing renderer/player classes.

- [ ] **Step 3: Implement presentation-only world mapping.**

Map snapshot cells to current terrain coordinates; update pre-authored Drone/Plot/Crop/Obstacle nodes. Animate only data supplied by committed events; failed actions show feedback but do not alter simulated state. At completion/skip load the authoritative snapshot, not a locally inferred state.

- [ ] **Step 4: Rerun playback and existing terrain tests.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/world_event_player_test.gd; Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/terrain/terrain_manager_test.gd`

Expected: action visualisation is correct without regressing existing terrain behavior.

- [ ] **Step 5: Commit world presentation.**

```powershell
git add scripts/client scenes/task tests/client
git commit -m "feat: 回放权威世界事件"
```

### Task 9: Add the WSS state machine and recovery gates

**Files:**
- Create: `scripts/client/world_realtime_client.gd`
- Modify: `autoload/session_controller.gd`
- Test: `tests/client/world_realtime_client_test.gd`

**Interfaces:**
- Produces `WorldRealtimeClient.connect_stream(bootstrap)`, `resume()`, and `close()`.
- Consumes `get_world_events`, `get_world_snapshot`, Store sequence APIs, and `WorldEventPlayer`.

- [ ] **Step 1: Write state-machine tests for subscribed-before-consumption, duplicate IDs, gap backfill, unrecoverable gap snapshot replacement, ACK and heartbeat.**

```gdscript
func test_gap_backfills_before_ack() -> void:
	await client.ingest_event(event_sequence_12)
	assert_eq(fake_game.requested_after_sequences, [10])
	assert_eq(fake_socket.last_ack_sequence, 12)
```

- [ ] **Step 2: Run the WSS test and confirm it fails.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/world_realtime_client_test.gd`

Expected: missing `WorldRealtimeClient`.

- [ ] **Step 3: Implement the closed frame protocol.**

Superseded/excluded for v0.4: `StudentBootstrapV2` does not mount `world_event_stream`, `client_event_batch`, `stream_url`, `stream_id`, or a WSS protocol response block. Do not wire this planned WSS flow into AppRoot; INT1 recovery remains HTTP Command/Run, Events, and Snapshot only.

- [ ] **Step 4: Rerun WSS tests.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/world_realtime_client_test.gd`

Expected: at-least-once delivery is idempotent and a gap cannot leave a partial world state.

- [ ] **Step 5: Commit realtime recovery.**

```powershell
git add autoload scripts/client tests/client
git commit -m "feat: 支持世界实时同步与断线恢复"
```

### Task 10: Run end-to-end gates, then document operational handoff

**Files:**
- Create: `tests/client/student_loop_e2e_test.gd`
- Create: `docs/testing/godot-student-client-contract-gates.md`
- Modify: `README.md`

**Interfaces:**
- Verifies Content → Workspace → Draft → Build → Activation → Turn → Run → Snapshot/Event → Evidence → Interaction → Patch Decision → next Draft.

- [ ] **Step 1: Write the complete fake-server E2E case with identity assertions.**

```gdscript
func test_student_loop_checks_same_session_turn_command_run_and_evidence() -> void:
	await controller.open_session(fixture_session)
	await controller.build_activate_and_run()
	assert_eq(store.current_interaction.run_id, store.current_run.run_id)
	assert_eq(store.current_run.evidence_id, store.current_interaction.evidence_id)
```

- [ ] **Step 2: Run it and confirm a pre-E2E failure.**

Run: `Godot_v4.5.2-stable_win64_console.exe --headless --path . --script res://tests/client/student_loop_e2e_test.gd`

Expected: test fails until all previous client controllers are wired.

- [ ] **Step 3: Add a CI-ready test command and operational documentation.**

Document required secure configuration (`api_base_url`, injected Bearer), local mock restrictions, product contract update workflow, and no-secret logging. The test runner must execute fixtures, HTTP transport, WSS state machine and existing headless terrain tests.

- [ ] **Step 4: Run all release gates.**

Historical run sequence (use sibling-relative paths): from `../agent`, run `npm run verify` and `npm run test:godot`; return to the frontend root, then run the two Godot headless scripts.

Expected: every contract, cross-identity, HTTP, event recovery, E2E and existing world test passes.

- [ ] **Step 5: Commit the verification gate.**

```powershell
git add README.md docs tests
git commit -m "feat: 完成学生端联调验证门禁"
```

## Self-Review

- **Spec coverage:** Tasks 1–3 implement the contract-first, identity, CAS, idempotency and reconciliation rules; Tasks 4–5 cover the task workspace and cloud drafts; Task 6 covers build/activation; Task 7 covers Run/feedback/Patch; Tasks 8–9 cover world presentation and WSS recovery; Task 10 covers all mandatory test gates.
- **Intentional exclusions:** historical `/api/*`, local C++ compilation, client world rules/Agent routing, internal event bus frames, uncontracted Product fields, multiplayer, IDE debugging and offline execution are excluded by Global Constraints.
- **Type consistency:** `ClientStore`, `RequestContextFactory`, `ProductApiGateway`, `CommandPoller`, `WorldSnapshotRenderer`, `WorldEventPlayer`, `WorldRealtimeClient`, and `SessionController` are each introduced before their consuming task.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-08-godot-student-client-contract-first.md`.

1. **Subagent-Driven (recommended)** — dispatch a fresh implementation agent per task and review between tasks.
2. **Inline Execution** — implement task-by-task in this session with checkpoints.
