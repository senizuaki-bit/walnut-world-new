extends SceneTree

const LEVEL_PATH := "res://scenes/level_demo/crop_adaptive_watering_demo.tscn"
const BridgeScript := preload("res://scenes/app/crop_agent_bridge.gd")
const CONTENT_REF := {
	"unit_id": "YAYA_FARM_001",
	"version": "1.4.0",
	"content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
}


class FakeStore:
	extends Node
	signal draft_changed(source: String, state: int)
	signal flow_changed(state: int)
	signal error_reported(error: Dictionary)

	var local_source := CropAdaptiveWateringDemo.CORRECT_CODE
	var draft_state := 0
	var content: Dictionary = {
		"content_ref": CONTENT_REF.duplicate(true),
		"task": {"name": "Water every thirsty plot"},
	}
	var world_snapshot: Dictionary
	var flow_state := WalnutClientStore.FlowState.READY
	var active_skill_tuple: Dictionary = {}
	var objective_result: Dictionary = {}

	func _init(snapshot: Dictionary) -> void:
		world_snapshot = snapshot.duplicate(true)

	func mark_draft_dirty(source: String) -> void:
		local_source = source
		draft_state = WalnutClientStore.DraftState.DIRTY
		draft_changed.emit(source, draft_state)

	func set_flow(value: int) -> void:
		flow_state = value
		flow_changed.emit(value)


class FakeSession:
	extends Node
	signal capability_unavailable(capability: String, message: String)
	signal interactions_recovered(interactions: Array[Dictionary])
	signal run_resolved(run: Dictionary)

	var store: FakeStore
	var hint_message := ""
	var authoritative_snapshot_before_run: Dictionary = {}

	func _init(value: FakeStore) -> void:
		store = value

	func request_build() -> void:
		store.draft_state = WalnutClientStore.DraftState.CLEAN
		store.set_flow(WalnutClientStore.FlowState.CERTIFIED)

	func request_activation() -> void:
		# Simulate a legitimate realtime authority update while Build/Activation is
		# in flight. Candidate evaluation must bind to the Snapshot immediately
		# before Run, not the earlier submission-click Snapshot.
		store.world_snapshot.revision = 5
		store.world_snapshot.last_event_sequence = 4
		store.world_snapshot.state_hash = "d".repeat(64)
		store.active_skill_tuple = _active_skill()
		store.set_flow(WalnutClientStore.FlowState.ACTIVE)

	func request_submit_and_run() -> Dictionary:
		authoritative_snapshot_before_run = store.world_snapshot.duplicate(true)
		var automatic_interactions: Array[Dictionary] = [{
			"interaction_id": "interaction_auto_0001",
			"role": "teaching_agent",
			"response_type": "message",
			"hint_level": null,
			"question": null,
			"feedback": {"message": "AUTO_TASK_INCOMPLETE_SHOULD_BE_FILTERED"},
		}]
		interactions_recovered.emit(automatic_interactions)
		var run := _rejected_run()
		store.objective_result = {
			"objective_succeeded": false,
			"summary": "后端通用目标未完成。",
			"run_id": run.run_id,
		}
		run_resolved.emit(run)
		store.set_flow(WalnutClientStore.FlowState.COMPLETED)
		return {"ok": true, "stage": "RUN", "message": "closed"}

	func request_hint(message: String) -> void:
		hint_message = message
		var hint_interactions: Array[Dictionary] = [{
			"interaction_id": "interaction_hint_0001",
			"role": "teaching_agent",
			"response_type": "hint",
			"hint_level": 1,
			"question": null,
			"feedback": {"message": "请检查每一块土地使用的数组下标。"},
		}]
		interactions_recovered.emit(hint_interactions)

	func _active_skill() -> Dictionary:
		return {
			"skill_id": "skill_candidate_0001",
			"skill_version_id": "skillver_candidate_0001",
			"artifact_sha256": "c".repeat(64),
			"certification_id": "cert_candidate_0001",
		}

	func _rejected_run() -> Dictionary:
		var intents: Array[Dictionary] = []
		for index: int in range(8):
			intents.append({
				"intent_id": "intent_water_%04d" % (index + 1),
				"action_type": "WATER",
				"actor_entity_id": "avatar_demo_0001",
				"expected_world_revision": int(store.world_snapshot.revision),
				"plot_id": "farm_plot_%04d" % (index + 1),
				"amount_ml": 100,
			})
		return {
			"run_id": "run_candidate_0001",
			"status": "REJECTED",
			"terminal": true,
			"skill": _active_skill(),
			"sandbox": {
				"invocation_id": "invocation_candidate_0001",
				"status": "SUCCEEDED",
				"action_intents": intents,
			},
			"world_application": {
				"status": "REJECTED",
				"receipt": null,
				"failure": {
					"code": "WORLD_RULE_REJECTED",
					"stage": "WORLD_VALIDATE",
					"details": {"reason": "TASK_INCOMPLETE"},
				},
			},
		}


func _initialize() -> void:
	var failures: Array[String] = []
	var snapshot := _snapshot()
	var level := (load(LEVEL_PATH) as PackedScene).instantiate() as CropAdaptiveWateringDemo
	level.timing_scale = 0.05
	root.add_child(level)
	var store := FakeStore.new(snapshot)
	root.add_child(store)
	var session := FakeSession.new(store)
	root.add_child(session)
	var bridge := BridgeScript.new()
	root.add_child(bridge)
	await process_frame
	bridge.configure(store, session, level, _candidate_config())
	if not bool(bridge.activate_initial_projection().get("ok", false)):
		failures.append("候选兼容测试必须先通过首次权威投影门禁。")
	level.call("_enter_code_phase")
	level.call("_request_run")
	for _frame: int in range(600):
		await process_frame
		if int(level.get("_phase")) == CropAdaptiveWateringDemo.Phase.LOCAL_COMPLETED:
			break
	var evidence := level.get_node("Hud/EvidencePanel/Margin/Content/EvidenceBody") as RichTextLabel
	if int(level.get("_phase")) != CropAdaptiveWateringDemo.Phase.LOCAL_COMPLETED:
		failures.append("合法 WATER-only REJECTED Run 应进入本地候选完成状态。")
	if store.world_snapshot != session.authoritative_snapshot_before_run:
		failures.append("候选演出不得修改 ClientStore.world_snapshot。")
	if int(store.world_snapshot.get("revision", -1)) != 5:
		failures.append("候选判题必须使用紧邻 Run 前捕获的最新权威 Snapshot。")
	if store.objective_result.get("objective_succeeded") != false:
		failures.append("候选完成不得覆盖后端 objective_result=false。")
	if not evidence.text.contains("世界未提交") or evidence.text.contains("AUTO_TASK_INCOMPLETE_SHOULD_BE_FILTERED"):
		failures.append("页面必须显示世界未提交，并过滤 Run 自动 TASK_INCOMPLETE 反馈。")
	var replay_button := level.get_node("Hud/PlaybackRail/ReplayPlaybackButton") as Button
	if replay_button.disabled:
		failures.append("候选演出完成后必须开放重播按钮。")
	level.call("_on_hint_pressed")
	for _frame: int in range(5):
		await process_frame
	if not session.hint_message.contains("候选结果不是后端 objective") or not session.hint_message.contains("Sandbox WATER"):
		failures.append("主动 Hint Turn 必须携带候选摘要与权威边界。")
	if not evidence.text.contains("数组下标"):
		failures.append("候选模式只应展示主动 response_type=hint 的教学反馈。")
	bridge.queue_free()
	session.queue_free()
	store.queue_free()
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("CROP_CANDIDATE_BRIDGE_TEST_PASS")
		quit(0)
		return
	for failure: String in failures:
		push_error(failure)
	quit(1)


func _candidate_config() -> Dictionary:
	var rules: Dictionary = {}
	for index: int in range(8):
		rules["farm_plot_%04d" % (index + 1)] = {
			"ui_index": index, "accepted_min": 100, "accepted_max": 199,
		}
	return {"enabled": true, "content_ref": CONTENT_REF.duplicate(true), "plot_rules": rules}


func _snapshot() -> Dictionary:
	var plots: Array[Dictionary] = []
	for index: int in range(8):
		plots.append({
			"plot_id": "farm_plot_%04d" % (index + 1),
			"position": {"x": index, "y": 0},
			"soil_state": "TILLED",
			"hydration": 0,
			"crop": {
				"crop_type": ["carrot", "tomato", "potato", "corn"][index % 4],
				"growth_stage": 1, "planted_at_tick": 1, "ready_to_harvest": false,
			},
			"last_updated_event_sequence": 3,
		})
	return {
		"request_context": {
			"schema_version": "1.0.0",
			"request_id": "req_candidate_0001",
			"correlation_id": "corr_candidate_0001",
			"trace_id": "trace_candidate_0001",
			"requested_at": "2026-08-17T00:00:00Z",
			"actor": {
				"tenant_id": "tenant_demo", "actor_id": "student_demo",
				"actor_type": "student", "roles": ["game:player"],
			},
			"content_ref": CONTENT_REF.duplicate(true),
		},
		"world_id": "world_demo_0001", "revision": 4, "last_event_sequence": 3,
		"state_schema_version": "1.0.0", "state_hash": "f".repeat(64),
		"generated_at": "2026-08-17T00:00:00Z", "world_rules_version": "farm-rules-12",
		"state": {
			"clock": {"day": 1, "minute_of_day": 10, "tick": 10},
			"avatar": {"entity_id": "avatar_demo_0001", "position": {"x": 0, "y": 0}, "energy": 10000},
			"inventory": [], "plots": plots, "agents": [],
		},
	}
