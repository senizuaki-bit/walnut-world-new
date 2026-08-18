extends SceneTree

const BridgeScript := preload("res://scenes/app/horizontal_watering_agent_bridge.gd")


class FakeStore:
	extends Node
	signal content_changed(content: Dictionary)
	signal draft_changed(source: String, state: int)
	signal world_replaced(snapshot: Dictionary)
	signal error_reported(error: Dictionary)

	var content: Dictionary = {
		"content_ref": {
			"unit_id": "TASK_HORIZONTAL_001",
			"version": "1.0.0",
			"content_hash": "b".repeat(64),
		},
		"status": "PUBLISHED",
		"unit_type": "TASK",
		"audiences": ["LEARNER"],
		"task": {
			"name": "权威横向浇水任务",
			"goal": "使用循环完成每块地的操作",
			"instructions": ["补全循环，并提交给小核桃。"],
			"story": {"opening": "幼苗正在等水。", "success": "整排幼苗都喝到水了。"},
		},
	}
	var local_source := "// recovered draft"
	var draft_state := WalnutClientStore.DraftState.CLEAN
	var flow_state := WalnutClientStore.FlowState.READY
	var active_skill_tuple: Dictionary = {}
	var objective_result: Dictionary = {}
	var world_snapshot: Dictionary = {
		"world_id": "world_horizontal",
		"revision": 7,
		"last_event_sequence": 12,
		"state_schema_version": "1.0.0",
		"state_hash": "a".repeat(64),
		"world_rules_version": "rules_demo",
		"state": {"plots": [{"plot_id": "plot_authority_0"}]},
	}
	var dirty_sources: Array[String] = []

	func mark_draft_dirty(source: String) -> void:
		dirty_sources.append(source)
		local_source = source
		draft_state = WalnutClientStore.DraftState.DIRTY
		draft_changed.emit(source, draft_state)

	func set_flow(value: int) -> void:
		flow_state = value


class FakeSession:
	extends Node
	signal interactions_recovered(interactions: Array[Dictionary])
	signal capability_unavailable(capability: String, message: String)
	signal world_playback_state_changed(state: String)

	var store: FakeStore
	var calls: Array[String] = []
	var fail_stage := ""
	var ordering_violation := false

	func _init(value: FakeStore) -> void:
		store = value

	func request_build() -> void:
		calls.append("save_draft")
		if store.draft_state != WalnutClientStore.DraftState.DIRTY:
			ordering_violation = true
		if fail_stage == "save":
			store.set_flow(WalnutClientStore.FlowState.ERROR)
			return
		store.draft_state = WalnutClientStore.DraftState.CLEAN
		calls.append("build")
		if fail_stage == "build":
			store.set_flow(WalnutClientStore.FlowState.BUILD_FAILED)
			return
		store.set_flow(WalnutClientStore.FlowState.CERTIFIED)

	func request_activation() -> void:
		calls.append("activate")
		if store.flow_state != WalnutClientStore.FlowState.CERTIFIED:
			ordering_violation = true
		if fail_stage == "activate":
			store.set_flow(WalnutClientStore.FlowState.ERROR)
			return
		store.active_skill_tuple = {
			"skill_id": "skill_horizontal",
			"skill_version_id": "skillver_horizontal",
		}
		store.set_flow(WalnutClientStore.FlowState.ACTIVE)

	func request_submit_and_run() -> Dictionary:
		calls.append("turn")
		if store.flow_state != WalnutClientStore.FlowState.ACTIVE:
			ordering_violation = true
		if fail_stage == "turn":
			return {"ok": false, "stage": "RUN", "message": "权威 Turn 失败"}
		var interactions: Array[Dictionary] = [{
			"interaction_id": "interaction_product_horizontal",
			"role": "teaching_agent",
			"response_type": "message",
			"question": null,
			"hint_level": null,
			"feedback": {"message": "这是 Product AgentInteraction 原文。", "degraded": false, "evidence_refs": []},
		}]
		interactions_recovered.emit(interactions)
		if fail_stage == "objective":
			store.objective_result = {
				"summary": "权威目标没有通过。",
				"objective_succeeded": false,
				"run_id": "run_horizontal",
			}
		else:
			store.objective_result = {"summary": "Run/Evidence/Snapshot/Interaction 权威闭环完成。"}
		store.set_flow(WalnutClientStore.FlowState.COMPLETED)
		return {"ok": true, "stage": "RUN", "message": "closed"}

	func request_hint(message: String) -> void:
		calls.append("hint:%s" % message)
		var interactions: Array[Dictionary] = [{
			"interaction_id": "interaction_hint_horizontal",
			"role": "teaching_agent",
			"response_type": "hint",
			"question": null,
			"hint_level": 2,
			"feedback": {"message": "观察循环边界。", "degraded": false, "evidence_refs": []},
		}]
		interactions_recovered.emit(interactions)


class FakePlayer:
	extends Node
	signal event_started(event: Dictionary)
	signal event_finished(event: Dictionary)
	signal playback_recovery_required(after_sequence: int)
	var speed_multiplier := 1.0

	func get_speed_multiplier() -> float:
		return speed_multiplier


class FakeLevel:
	extends HorizontalWateringDemo

	var agent_mode := false
	var content_received: Dictionary = {}
	var drafts_received: Array[String] = []
	var draft_states: Array[int] = []
	var snapshots_received: Array[Dictionary] = []
	var stages: Array[String] = []
	var failures: Array[String] = []
	var completions: Array[String] = []
	var interactions_received: Array[Dictionary] = []
	var errors: Array[String] = []
	var events_started: Array[Dictionary] = []
	var event_speeds: Array[float] = []
	var events_finished: Array[Dictionary] = []
	var playback_states: Array[String] = []
	var recovery_sequences: Array[int] = []

	func configure_agent_mode(enabled: bool) -> void:
		agent_mode = enabled

	func load_agent_content(value: Dictionary) -> bool:
		content_received = value.duplicate(true)
		return true

	func load_agent_draft(source: String) -> void:
		drafts_received.append(source)

	func update_agent_draft_state(state: int) -> void:
		draft_states.append(state)

	func replace_authoritative_world(snapshot: Dictionary) -> bool:
		snapshots_received.append(snapshot.duplicate(true))
		return true

	func begin_agent_submission(message: String) -> void:
		stages.append(message)

	func update_agent_submission_stage(message: String, _busy: bool = true) -> void:
		stages.append(message)

	func complete_agent_submission(message: String) -> void:
		completions.append(message)

	func fail_agent_submission(stage: String, message: String) -> void:
		failures.append("%s:%s" % [stage, message])

	func present_agent_interactions(interactions: Array[Dictionary]) -> void:
		interactions_received = interactions.duplicate(true)

	func present_agent_error(message: String) -> void:
		errors.append(message)

	func present_world_playback_state(state: String) -> void:
		playback_states.append(state)

	func present_verified_world_event(event: Dictionary, speed: float = 1.0) -> Dictionary:
		events_started.append(event.duplicate(true))
		event_speeds.append(speed)
		return {"ok": true, "duration_seconds": 0.0}

	func finish_verified_world_event(event: Dictionary, _skipped: bool = false) -> bool:
		events_finished.append(event.duplicate(true))
		return true

	func require_world_playback_recovery(after_sequence: int) -> void:
		recovery_sequences.append(after_sequence)


func _initialize() -> void:
	var failures: Array[String] = []
	await _test_initial_binding_and_ui_intents(failures)
	await _test_failed_initial_projection_stays_closed(failures)
	await _test_strict_success_chain(failures)
	await _test_failure_short_circuit(failures)
	await _test_verified_harvest_only(failures)
	if failures.is_empty():
		print("HORIZONTAL_WATERING_AGENT_BRIDGE_TEST_PASS: INT2 authority adapter is fail-closed")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)


func _test_initial_binding_and_ui_intents(failures: Array[String]) -> void:
	var fixture := _new_fixture()
	var store: FakeStore = fixture.store
	var session: FakeSession = fixture.session
	var level: FakeLevel = fixture.level
	if not level.agent_mode:
		failures.append("configure 必须启用 CONNECTING Agent 模式。")
	if not level.content_received.is_empty() or not level.drafts_received.is_empty() or not level.snapshots_received.is_empty():
		failures.append("configure 只能接线，不得投影尚未完成启动重校验的缓存权威。")
	level.draft_changed.emit("// edit only")
	level.hint_requested.emit("请给正式提示")
	level.submit_requested.emit("// blocked submission")
	await process_frame
	if store.local_source != "// recovered draft" or not session.calls.is_empty():
		failures.append("首次投影激活前所有 UI intent 必须 fail closed。")
	var recovered_interactions: Array[Dictionary] = [{"feedback": {"message": "恢复内容"}}]
	session.interactions_recovered.emit(recovered_interactions)
	if not level.interactions_received.is_empty():
		failures.append("首次投影激活前恢复的 Interaction 不得提前显示。")
	var projection: Dictionary = fixture.bridge.activate_initial_projection()
	if not projection.get("ok", false):
		failures.append("完整启动权威应允许 AppRoot 激活首次投影。")
	if level.content_received != store.content:
		failures.append("激活首次投影必须同步 Product Content。")
	if level.drafts_received != [store.local_source] or level.draft_states != [store.draft_state]:
		failures.append("激活首次投影必须同步完整 Draft 与 DraftState。")
	if level.snapshots_received != [store.world_snapshot]:
		failures.append("激活首次投影必须同步权威 World Snapshot。")
	if level.interactions_received != recovered_interactions:
		failures.append("激活首次投影必须显示启动期间已验证恢复的 Interaction。")
	level.draft_changed.emit("// edit only")
	if (
		store.local_source != "// edit only"
		or not session.calls.is_empty()
		or level.drafts_received != ["// recovered draft"]
	):
		failures.append("激活后 draft_changed 只能标脏 Draft，不得提前 Build、Activation 或 Turn。")
	level.hint_requested.emit("请给正式提示")
	await process_frame
	if session.calls != ["hint:请给正式提示"] or level.interactions_received.is_empty():
		failures.append("hint_requested 必须只走 request_hint，并展示 Product AgentInteraction。")
	_dispose_fixture(fixture)
	await process_frame



func _test_failed_initial_projection_stays_closed(failures: Array[String]) -> void:
	var fixture := _new_fixture()
	var store: FakeStore = fixture.store
	var session: FakeSession = fixture.session
	var level: FakeLevel = fixture.level
	store.world_snapshot.clear()
	var projection: Dictionary = fixture.bridge.activate_initial_projection()
	if projection.get("ok", true) or level.errors.is_empty():
		failures.append("首次投影缺少权威资源时必须进入预置恢复错误。")
	level.draft_changed.emit("// rejected edit")
	level.hint_requested.emit("rejected hint")
	level.submit_requested.emit("// rejected submit")
	await process_frame
	if store.local_source != "// recovered draft" or not session.calls.is_empty():
		failures.append("首次投影失败后 UI intent 必须持续 fail closed。")
	_dispose_fixture(fixture)
	await process_frame


func _test_strict_success_chain(failures: Array[String]) -> void:
	var fixture := _new_fixture()
	var store: FakeStore = fixture.store
	var session: FakeSession = fixture.session
	var level: FakeLevel = fixture.level
	fixture.bridge.activate_initial_projection()
	level.submit_requested.emit("// authoritative submission")
	await process_frame
	if session.calls != ["save_draft", "build", "activate", "turn"] or session.ordering_violation:
		failures.append("提交必须严格串行 Draft 保存→Build→Activation→Agent Turn。")
	if store.local_source != "// authoritative submission":
		failures.append("提交必须先将界面完整源码写入 Draft。")
	if level.completions != ["Run/Evidence/Snapshot/Interaction 权威闭环完成。"]:
		failures.append("只有 COMPLETED Flow 与 store.objective_result 闭合后才能完成提交。")
	if level.interactions_received.is_empty():
		failures.append("正式提交结果必须展示 Product AgentInteractions。")
	_dispose_fixture(fixture)
	await process_frame


func _test_failure_short_circuit(failures: Array[String]) -> void:
	for fail_stage in ["save", "build", "activate", "turn", "objective"]:
		var fixture := _new_fixture()
		var session: FakeSession = fixture.session
		var level: FakeLevel = fixture.level
		fixture.bridge.activate_initial_projection()
		session.fail_stage = fail_stage
		level.submit_requested.emit("// fail %s" % fail_stage)
		await process_frame
		var forbidden := []
		if fail_stage in ["save", "build"]:
			forbidden = ["activate", "turn"]
		elif fail_stage == "activate":
			forbidden = ["turn"]
		for call in forbidden:
			if call in session.calls:
				failures.append("%s 失败后不得继续请求 %s。" % [fail_stage, call])
		if level.failures.is_empty() or not level.completions.is_empty():
			failures.append("%s 失败必须可见且不得模拟成功。" % fail_stage)
		_dispose_fixture(fixture)
		await process_frame


func _test_verified_harvest_only(failures: Array[String]) -> void:
	var fixture := _new_fixture()
	var session: FakeSession = fixture.session
	var player: FakePlayer = fixture.player
	var level: FakeLevel = fixture.level
	fixture.bridge.activate_initial_projection()
	var harvest := {
		"event_type": "world.action.harvested",
		"event_version": 1,
		"payload": {"plot_id": "plot_authority_0"},
	}
	fixture.bridge.begin_presentation_event({"event_type": "world.action.watered", "event_version": 1})
	fixture.bridge.finish_presentation_event({"event_type": "world.action.harvested", "event_version": 2})
	if not level.events_started.is_empty() or not level.events_finished.is_empty():
		failures.append("非 HARVEST 或非 v1 事件不得进入 Horizontal 表现层。")
	fixture.bridge.begin_presentation_event(harvest)
	fixture.bridge.finish_presentation_event(harvest)
	session.world_playback_state_changed.emit("PLAYING")
	player.playback_recovery_required.emit(12)
	if level.events_started != [harvest] or level.events_finished != [harvest]:
		failures.append("HARVEST 只能经 AppRoot 配置的 Bridge renderer 进入关卡 begin/finish。")
	if level.event_speeds != [1.0]:
		failures.append("Bridge 必须将 renderer 的 1 倍速传给关卡。")
	fixture.bridge.begin_presentation_event(harvest, 2.0)
	if level.event_speeds != [1.0, 2.0]:
		failures.append("Bridge 必须将 renderer 的 2 倍速传给关卡。")
	if not fixture.bridge.project_replay_snapshot(fixture.store.world_snapshot):
		failures.append("Bridge 必须把权威 replay Snapshot 投影到横向浇水关卡。")
	if not fixture.bridge.last_authoritative_projection_succeeded(fixture.store.world_snapshot):
		failures.append("Bridge 必须确认最后一次横向关卡 Snapshot 投影成功。")
	if level.playback_states != ["PLAYING"] or level.recovery_sequences != [12]:
		failures.append("Bridge 必须转发权威 Playback 状态与恢复请求。")
	_dispose_fixture(fixture)
	await process_frame


func _new_fixture() -> Dictionary:
	var store := FakeStore.new()
	var session := FakeSession.new(store)
	var player := FakePlayer.new()
	var level := FakeLevel.new()
	var bridge := BridgeScript.new()
	root.add_child(bridge)
	bridge.configure(store, session, level, player)
	return {"store": store, "session": session, "player": player, "level": level, "bridge": bridge}


func _dispose_fixture(fixture: Dictionary) -> void:
	(fixture.bridge as Node).queue_free()
	(fixture.level as Node).free()
	(fixture.player as Node).free()
	(fixture.session as Node).free()
	(fixture.store as Node).free()
