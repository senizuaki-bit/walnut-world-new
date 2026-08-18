extends SceneTree

const LEVEL_PATH := "res://scenes/level_demo/crop_adaptive_watering_demo.tscn"
const BridgeScript := preload("res://scenes/app/crop_agent_bridge.gd")


class FakeStore:
	extends Node
	signal draft_changed(source: String, state: int)
	signal flow_changed(state: int)
	signal error_reported(error: Dictionary)

	var local_source := ""
	var draft_state := 0
	var content: Dictionary = {
		"unit_id": "TASK_CROP_ADAPTIVE_WATERING",
		"task": {"name": "Water every thirsty plot"},
	}
	var world_snapshot: Dictionary = {
		"world_id": "world_demo", "revision": 1, "state_hash": "state_hash_1",
	}
	var flow_state := 1
	var active_skill_tuple: Dictionary = {}
	var objective_result: Dictionary = {}

	func mark_draft_dirty(source: String) -> void:
		local_source = source
		draft_state = 1
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
	var stages: Array[String] = []
	var fail_next_turn := false

	func _init(value: FakeStore) -> void:
		store = value

	func request_build() -> void:
		stages.append("build")
		store.set_flow(4)

	func request_activation() -> void:
		stages.append("activate")
		store.active_skill_tuple = {"skill_id": "skill_demo", "skill_version_id": "skillver_demo"}
		store.set_flow(6)

	func request_submit_and_run() -> Dictionary:
		stages.append("turn")
		if fail_next_turn:
			fail_next_turn = false
			store.set_flow(WalnutClientStore.FlowState.ERROR)
			store.error_reported.emit({
				"scope": "CLIENT_LOCAL",
				"code": "RESOURCE_RECONCILIATION_TIMEOUT",
				"message": "The resource did not reach a terminal state before the total polling deadline.",
				"retryable": true,
			})
			return {"ok": false, "stage": "RUN", "message": "Run closure did not complete."}
		var interactions: Array[Dictionary] = [{
			"interaction_id": "interaction_demo",
			"role": "xiaohutao",
			"response_type": "message",
			"hint_level": null,
			"question": null,
			"feedback": {"message": "正式 Agent 已验证这次提交。"},
		}]
		interactions_recovered.emit(interactions)
		store.world_snapshot = {"world_id": "world_demo", "revision": 2, "state_hash": "state_hash_2"}
		store.objective_result = {"objective_succeeded": true, "summary": "权威 Run 已闭环。"}
		store.set_flow(9)
		return {"ok": true, "stage": "RUN", "message": "closed"}

	func request_hint(_message: String) -> void:
		stages.append("hint")
		var interactions: Array[Dictionary] = [{
			"interaction_id": "interaction_hint",
			"role": "teaching_agent",
			"response_type": "hint",
			"hint_level": 1,
			"question": null,
			"feedback": {"message": "请比较同一下标的目标值与当前值。"},
		}]
		interactions_recovered.emit(interactions)


func _initialize() -> void:
	var failures: Array[String] = []
	var level := (load(LEVEL_PATH) as PackedScene).instantiate() as CropAdaptiveWateringDemo
	level.timing_scale = 0.05
	root.add_child(level)
	var store := FakeStore.new()
	root.add_child(store)
	var session := FakeSession.new(store)
	root.add_child(session)
	var bridge := BridgeScript.new()
	root.add_child(bridge)
	await process_frame
	store.local_source = CropAdaptiveWateringDemo.STARTER_CODE
	bridge.configure(store, session, level)
	var activation: Dictionary = bridge.activate_initial_projection()
	if not activation.get("ok", false):
		failures.append("权威恢复完成后必须打开作物适配关卡的首次投影门禁。")
	level.call("_enter_code_phase")
	var source := CropAdaptiveWateringDemo.CORRECT_CODE
	(level.get_node("CodeDrawer/Surface/Margin/Content/CodeEditor") as CodeEdit).text = source
	var action_stages: Array[String] = []
	var action_results: Array[Dictionary] = []
	bridge.build_action_finished.connect(func(result: Dictionary) -> void:
		action_stages.append("BUILD")
		action_results.append(result.duplicate(true))
	)
	bridge.activation_action_finished.connect(func(result: Dictionary) -> void:
		action_stages.append("ACTIVATION")
		action_results.append(result.duplicate(true))
	)
	bridge.submit_action_finished.connect(func(result: Dictionary) -> void:
		action_stages.append("SUBMIT")
		action_results.append(result.duplicate(true))
	)
	var run_button := level.get_node("CodeDrawer/Surface/Margin/Content/Actions/RunButton") as Button
	run_button.pressed.emit()
	for _frame in range(10):
		await process_frame
	if session.stages != ["build", "activate", "turn"]:
		failures.append("一次直接运行必须在后台严格串行完成 Draft→Build→Activation→Agent Turn。")
	if store.local_source != source:
		failures.append("正式链路必须提交代码界面的当前草稿。")
	if (level.get_node("Hud/WateringCan") as AnimatedSprite2D).visible:
		failures.append("WATER 权威协议未发布前不得播放本地浇水动画冒充 Agent 结果。")
	var evidence := level.get_node("Hud/EvidencePanel/Margin/Content/EvidenceBody") as RichTextLabel
	if not evidence.text.contains("权威 Run 已闭环") or not evidence.text.contains("正式 Run"):
		failures.append("正式闭环完成后必须显示权威结果来源。")
	if not evidence.text.contains("revision 2") or not evidence.text.contains("state_hash_2"):
		failures.append("新关卡必须将最终权威 Snapshot 身份投影到可见证据。")
	var projection: Dictionary = level.formal_projection_state()
	if (
		projection.get("content") != store.content
		or projection.get("snapshot") != store.world_snapshot
		or str(projection.get("source", "")) != source
		or (level.get_node("Hud/TaskCard/Margin/Content/TaskTitle") as Label).text != "Water every thirsty plot"
	):
		failures.append("CropAdaptiveWateringDemo 必须精确投影 Content、Draft 与 Snapshot。")
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	run_button.pressed.emit()
	for _frame in range(10):
		await process_frame
	if session.stages != ["build", "activate", "turn", "turn"] or action_stages != ["BUILD", "ACTIVATION", "SUBMIT", "SUBMIT"]:
		failures.append("同源重试必须复用已认证/已激活 tuple，只再产生 Submit。")
	session.fail_next_turn = true
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	run_button.pressed.emit()
	for _frame in range(10):
		await process_frame
	var failed_action: Dictionary = action_results.back()
	if (
		bool(failed_action.get("ok", true))
		or str(failed_action.get("code", "")) != "RESOURCE_RECONCILIATION_TIMEOUT"
		or not str(failed_action.get("message", "")).contains("did not reach a terminal state")
	):
		failures.append("资源轮询超时必须保留净化后的 code/message，不得折叠成无结构结果。")
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	level.call("_on_hint_pressed")
	for _frame in range(5):
		await process_frame
	if session.stages.back() != "hint" or not evidence.text.contains("同一下标"):
		failures.append("问叮当必须通过正式 Hint Turn 展示 AgentInteraction。")
	bridge.queue_free()
	session.queue_free()
	store.queue_free()
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("CROP_AGENT_BRIDGE_TEST_PASS: 合并交付与正式提示链路通过")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
