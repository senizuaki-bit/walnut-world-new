extends SceneTree

const LEVEL_PATH := "res://scenes/level_demo/horizontal_watering_demo.tscn"
const SCRIPT_PATH := "res://scenes/level_demo/horizontal_watering_demo.gd"


func _initialize() -> void:
	var failures: Array[String] = []
	var level := (load(LEVEL_PATH) as PackedScene).instantiate()
	level.start_on_ready = false
	root.add_child(level)
	await process_frame
	await process_frame
	for required_path in [
		"ManualRow", "AutoRow", "MagicWateringCan", "Guidance/VariableBadge",
		"Hud/CodeDrawer", "Hud/StoryDialogueOverlay", "Hud/AgentStatusPanel",
		"Hud/AgentStatusPanel/Margin/Content/Title", "Hud/AgentStatusPanel/Margin/Content/Body",
		"Hud/RecoveryPanel", "Hud/RecoveryPanel/Panel/Margin/Content/Message",
	]:
		if level.get_node_or_null(required_path) == null:
			failures.append("缺少预置节点：%s" % required_path)
	if level.visible or (level.get_node("Hud") as CanvasLayer).visible:
		failures.append("显式启动前必须同时隐藏 3D 根节点和 Hud CanvasLayer。")
	if level.process_mode != Node.PROCESS_MODE_DISABLED:
		failures.append("显式启动前必须禁用关卡处理。")
	level.start_level()
	await process_frame
	if not level.visible or not (level.get_node("Hud") as CanvasLayer).visible:
		failures.append("start_level() 必须显示 3D 与独立 Hud。")
	level.set_demo_active(false)
	if level.visible or (level.get_node("Hud") as CanvasLayer).visible:
		failures.append("停用时必须隐藏 3D 与独立 Hud。")
	var source := FileAccess.get_file_as_string(SCRIPT_PATH)
	for forbidden in [
		"STARTER_CODE", "CORRECT_CODE", "INTRO_LINES", "DING_DANG_LINES",
		"evaluate_code", "submit_code_for_test", "missing_first", "missing_last",
		"agent_submit_requested", "agent_hint_requested", "agent_draft_changed",
		"begin_presentation_event", "finish_presentation_event",
	]:
		if source.contains(forbidden):
			failures.append("正式场景仍包含本地权威或旧信号：%s" % forbidden)
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("HORIZONTAL_WATERING_DEMO_TEST_PASS: 预置 Agent/Recovery UI、生命周期与去本地权威合同完整")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
