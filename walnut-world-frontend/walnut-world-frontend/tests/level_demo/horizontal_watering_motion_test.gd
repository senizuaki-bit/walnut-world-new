extends SceneTree

const LEVEL_PATH := "res://scenes/level_demo/horizontal_watering_demo.tscn"


func _initialize() -> void:
	var failures: Array[String] = []
	var level := (load(LEVEL_PATH) as PackedScene).instantiate()
	level.demo_timing_scale = 0.0
	root.add_child(level)
	await process_frame
	var snapshot := _snapshot(4, [
		_plot("plot_e", 40, 10000), _plot("plot_c", 20, 0), _plot("plot_a", 0, 1),
		_plot("plot_d", 30, 0, _crop()), _plot("plot_b", 10, 500),
	])
	if not level.bind_authoritative_snapshot(snapshot):
		failures.append("合法 Snapshot 必须建立权威 plot_id 映射。")
	var expected_snapshot_state := [true, true, false, false, true]
	for index in expected_snapshot_state.size():
		var projected_plot := level.get_node("AutoRow/Plot%d" % index) as WateringPlot
		if projected_plot.is_watered != expected_snapshot_state[index]:
			failures.append("只有 Snapshot、没有事件时，Plot%d 必须按 hydration 完整投影。" % index)
	level.start_level()
	for index in expected_snapshot_state.size():
		var entry_plot := level.get_node("AutoRow/Plot%d" % index) as WateringPlot
		if entry_plot.is_watered != expected_snapshot_state[index]:
			failures.append("进入主流程时不得清空 Plot%d 的权威 Snapshot 投影。" % index)
	var event := _event("plot_c", 20)
	var result: Dictionary = level.present_verified_world_event(event)
	if not result.get("ok", false):
		failures.append("已映射 HARVEST v1 必须启动主题化浇水表现。")
	var badge := level.get_node("Guidance/VariableBadge") as Label3D
	var magic_can := level.get_node("MagicWateringCan") as AnimatedSprite3D
	if badge.text != "plot_c" or not magic_can.visible:
		failures.append("事件必须按真实 plot_id 定位预置土地，不得使用本地下标作 ID。")
	await create_timer(0.08).timeout
	level.finish_verified_world_event(event)
	if not (level.get_node("AutoRow/Plot2") as WateringPlot).is_watered:
		failures.append("HARVEST 主题动画必须按真实 plot_id 映射到 Plot2。")
	var interrupted_event := _event("plot_d", 30)
	level.present_verified_world_event(interrupted_event)
	var final_snapshot := _snapshot(5, [
		_plot("plot_a", 0), _plot("plot_b", 10), _plot("plot_c", 20),
		_plot("plot_d", 30), _plot("plot_e", 40),
	])
	level.bind_authoritative_snapshot(final_snapshot)
	await create_timer(0.08).timeout
	for index in 5:
		if (level.get_node("AutoRow/Plot%d" % index) as WateringPlot).is_watered:
			failures.append("事件后的最终 Snapshot 必须覆盖 Plot%d 的动画状态。" % index)
	if magic_can.visible:
		failures.append("最终 Snapshot 收口时必须停止仍在播放的浇水表现。")
	var unknown: Dictionary = level.present_verified_world_event(_event("plot_unknown", 99))
	if unknown.get("ok", false) or not (level.get_node("Hud/RecoveryPanel") as Control).visible:
		failures.append("未知 plot_id 必须 fail closed 到预置 RecoveryPanel。")
	level.bind_authoritative_snapshot(final_snapshot)
	var mismatched_position: Dictionary = level.present_verified_world_event(_event("plot_c", 99))
	if mismatched_position.get("ok", false):
		failures.append("事件 position 与 Snapshot 映射不一致时必须 fail closed。")
	var unsupported := _event("plot_a", 0)
	unsupported.event_type = "world.action.watered"
	if level.present_verified_world_event(unsupported).get("ok", false):
		failures.append("场景不得臆造或接受 WATER 合同。")
	var status := level.get_node("Hud/AgentStatusPanel/Margin/Content/Body") as Label
	if not status.text.contains(":5:"):
		failures.append("最终 World Snapshot 必须刷新并收口表现状态。")
	for invalid_case in [
		{"field": "soil_state", "value": 7},
		{"field": "hydration", "value": "wet"},
		{"field": "crop", "value": []},
		{"field": "last_updated_event_sequence", "value": -1},
	]:
		var invalid_plots: Array = final_snapshot.state.plots.duplicate(true)
		invalid_plots[0][invalid_case.field] = invalid_case.value
		if level.bind_authoritative_snapshot(_snapshot(6, invalid_plots)):
			failures.append("Snapshot %s 类型或范围错误时必须拒绝投影。" % invalid_case.field)
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("HORIZONTAL_WATERING_MOTION_TEST_PASS: Snapshot plot_id 映射与已验证 HARVEST 主题表现通过")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)


func _plot(plot_id: String, x: int, hydration: int = 0, crop: Variant = null) -> Dictionary:
	return {
		"plot_id": plot_id, "position": {"x": x, "y": 0}, "soil_state": "TILLED",
		"hydration": hydration, "crop": crop, "last_updated_event_sequence": 0,
	}


func _crop() -> Dictionary:
	return {
		"crop_type": "carrot", "growth_stage": 3, "planted_at_tick": 2,
		"ready_to_harvest": true,
	}


func _snapshot(revision: int, plots: Array) -> Dictionary:
	return {
		"world_id": "world_demo", "revision": revision, "last_event_sequence": revision,
		"state_hash": "a".repeat(64), "state": {"plots": plots},
	}


func _event(plot_id: String, x: int) -> Dictionary:
	return {
		"event_type": "world.action.harvested", "event_version": 1,
		"payload": {
			"actor_entity_id": "student_avatar", "plot_id": plot_id,
			"position": {"x": x, "y": 0}, "crop_type": "carrot",
			"growth_stage": 3, "ready_to_harvest": true,
		},
	}
