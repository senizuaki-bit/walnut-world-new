extends SceneTree


func _initialize() -> void:
	var failures: Array[String] = []
	var flow := (load("res://scenes/app/game_flow.tscn") as PackedScene).instantiate()
	root.add_child(flow)
	await process_frame
	var start := flow.get_node_or_null("GameStartScreen") as GameStartScreen
	var level := flow.get_node_or_null("CropAdaptiveWateringDemo") as CropAdaptiveWateringDemo
	var transition_layer := flow.get_node_or_null("TransitionLayer") as CanvasLayer
	if start == null or not start.visible:
		failures.append("游戏启动后必须先显示开始界面。")
	if level == null or level.visible:
		failures.append("点击进入农场前不得提前显示作物适配演示。")
	for required_path in [
		"GameStartScreen/HeroCard/Margin/Content/Copy/EnterButton",
		"CropAdaptiveWateringDemo/Hud/TaskCard",
		"CropAdaptiveWateringDemo/Hud/ToolRail/RequestPatchButton",
		"CropAdaptiveWateringDemo/CodeDrawer",
		"CropAdaptiveWateringDemo/SkillTreeOverlay",
		"CropAdaptiveWateringDemo/WorkshopOverlay",
		"CropAdaptiveWateringDemo/BugChallengeOverlay",
		"CropAdaptiveWateringDemo/GrowthSummaryOverlay",
		"TransitionLayer/Transition",
	]:
		if flow.get_node_or_null(required_path) == null:
			failures.append("关卡流转缺少预置节点：%s" % required_path)
	if transition_layer == null or transition_layer.layer <= 10:
		failures.append("转场 CanvasLayer 必须覆盖作物适配关卡 UI。")
	if flow.get_node_or_null("FirstLevel") != null or flow.get_node_or_null("NextLevelPanel") != null:
		failures.append("唯一演示流程不得保留旧第一关或下一关预告节点。")
	var flow_scene := FileAccess.get_file_as_string("res://scenes/app/game_flow.tscn")
	if (
		not flow_scene.contains("res://scenes/level_demo/crop_adaptive_watering_demo.tscn")
		or not flow_scene.contains("[node name=\"CropAdaptiveWateringDemo\"")
		or flow_scene.contains("horizontal_watering_demo.tscn")
	):
		failures.append("GameFlow 必须只预置作物适配关卡。")
	var project := ConfigFile.new()
	if project.load("res://project.godot") != OK or project.get_value("application", "run/main_scene", "") != "res://scenes/app/app_root.tscn":
		failures.append("项目主场景必须由正式 AppRoot 组合 GameFlow。")
	var app_scene := FileAccess.get_file_as_string("res://scenes/app/app_root.tscn")
	if not app_scene.contains("res://scenes/app/game_flow.tscn"):
		failures.append("正式 AppRoot 必须预置 GameFlow。")
	if app_scene.contains("task_workspace.tscn") or app_scene.contains("main.tscn"):
		failures.append("正式 AppRoot 不得再装配隐藏 TaskWorkspace 或旧 Main 世界。")
	if (
		not app_scene.contains("CropAgentBridge")
		or not app_scene.contains("crop_agent_bridge.gd")
		or not app_scene.contains("world_presentation_enabled = false")
	):
		failures.append("正式 AppRoot 必须预置作物适配 Agent 桥接，并在 WATER 正式演出协议落地前关闭权威世界表现。")
	if app_scene.contains("FirstLevelAgentBridge") or app_scene.contains("horizontal_watering_agent_bridge.gd"):
		failures.append("正式 AppRoot 不得保留横向浇水关卡的 Agent 桥接节点。")
	var app_root_scene := load("res://scenes/app/app_root.tscn") as PackedScene
	var app_root := app_root_scene.instantiate() if app_root_scene != null else null
	if (
		app_root == null
		or app_root.get_node_or_null("GameFlow/CropAdaptiveWateringDemo") == null
		or app_root.get_node_or_null("CropAgentBridge") == null
		or app_root.get_node_or_null("WorldEventPlayer") == null
	):
		failures.append("正式 AppRoot 场景必须能实例化完整的作物适配权威组合。")
	if app_root != null:
		root.add_child(app_root)
		await process_frame
		if app_root.get_node_or_null("TaskWorkspace") != null:
			failures.append("正式 AppRoot 实例不得包含旧 TaskWorkspace 节点。")
		if app_root.get("crop_agent_bridge") != app_root.get_node_or_null("CropAgentBridge"):
			failures.append("正式 AppRoot 必须把作物适配桥接作为唯一 Agent 适配器。")
		if bool(app_root.get("water_candidate_compatibility_enabled")):
			failures.append("WATER 前端候选兼容必须默认关闭，只能由明确配置启用。")
		app_root.queue_free()
		await process_frame
	start.enter_button.pressed.emit()
	await _wait_for_transition(flow)
	if start.visible or not level.visible:
		failures.append("点击进入农场后必须完成淡出并显示作物适配演示。")
	level.call("_enter_free_play")
	(level.get_node("CompletionCard/Margin/Content/Actions/NextButton") as Button).pressed.emit()
	await process_frame
	if not (level.get_node("CompletionCard/Margin/Content/CompletionTitle") as Label).text.contains("下一关"):
		failures.append("完成页的下一关按钮必须进入明确的预告状态。")
	level.replay_requested.emit()
	await _wait_for_transition(flow)
	if int(level.get("_phase")) != CropAdaptiveWateringDemo.Phase.INTRO:
		failures.append("重玩本关必须通过GameFlow重新开始关卡。")
	level.return_home_requested.emit()
	await _wait_for_transition(flow)
	if not start.visible or level.visible or start.enter_button.disabled:
		failures.append("返回农场必须恢复可再次进入的开始界面。")
	flow.queue_free()
	await process_frame
	if failures.is_empty():
		print("GAME_FLOW_START_SCREEN_TEST_PASS: 开始界面唯一接入作物适配演示")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)


func _wait_for_transition(flow: Control) -> void:
	for _frame in range(240):
		await process_frame
		if not bool(flow.get("_transitioning")):
			return
