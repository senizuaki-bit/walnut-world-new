extends SceneTree

const LEVEL_PATH := "res://scenes/level_demo/horizontal_watering_demo.tscn"


func _initialize() -> void:
	var failures: Array[String] = []
	var level := (load(LEVEL_PATH) as PackedScene).instantiate()
	root.add_child(level)
	await process_frame
	for required_path in [
		"WorldEnvironment", "Camera3D", "ManualRow", "AutoRow",
		"Cast/Yaya", "Cast/LittleWalnut", "Cast/DingDang", "Cast/ShuShu",
		"ManualWateringCan", "MagicWateringCan", "Guidance/ManualPath", "Guidance/VariableBadge",
		"Hud/SafeArea/TaskCard", "Hud/SafeArea/PhaseStrip", "Hud/SafeArea/ToolRail/HintButton",
		"Hud/SafeArea/GrowthFeedback", "Hud/SafeArea/CompletionCard",
		"Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeFillPanel/CodeMargin/CodeLines/LoopLine/StartInput",
		"Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeFillPanel/CodeMargin/CodeLines/LoopLine/LimitInput",
		"Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeFillPanel/CodeMargin/CodeLines/OutputLine/IndexInput",
		"Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Trace",
		"Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Actions/ResetButton",
		"Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Actions/SubmitButton",
		"Hud/MagicSpellOverlay/SpellTitle", "Hud/MagicSpellOverlay/RippleOuter",
		"Hud/StoryDialogueOverlay/DialogueCard/ContentRoot/LeafFrame",
		"Hud/StoryDialogueOverlay/DialogueCard/ContentRoot/ContinueHint",
	]:
		if level.get_node_or_null(required_path) == null:
			failures.append("缺少预置节点：%s" % required_path)
	for row_name in ["ManualRow", "AutoRow"]:
		var row := level.get_node(row_name)
		if row.get_child_count() != 5:
			failures.append("%s 必须预置 5 块土地。" % row_name)
		for plot in row.get_children():
			for plot_path in ["TilledSoil", "WateredSoil", "Seedling", "GrowthSparkles", "GuideRing", "InteractionArea/CollisionShape3D"]:
				if plot.get_node_or_null(plot_path) == null:
					failures.append("%s/%s 缺少 %s" % [row_name, plot.name, plot_path])
	for removed_path in [
		"HorizontalWateringRig", "Guidance/MachineBadge", "Hud/SafeArea/DialogueCard",
		"Hud/SafeArea/TechUnlockCard", "Hud/SafeArea/ToolRail/CodeButton",
		"Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeEditor",
		"Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/VariableMap",
		"Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Stats",
		"Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Actions/DebugToggleButton",
	]:
		if level.get_node_or_null(removed_path) != null:
			failures.append("旧节点必须移除：%s" % removed_path)
	var hint := level.get_node("Hud/StoryDialogueOverlay/DialogueCard/ContentRoot/ContinueHint") as Label
	var body := level.get_node("Hud/StoryDialogueOverlay/DialogueCard/ContentRoot/ContentMargin/Content/Body") as Label
	if hint.text != "▼点击继续":
		failures.append("全屏对话继续提示文案不符合设计。")
	if body.vertical_alignment != VERTICAL_ALIGNMENT_TOP:
		failures.append("对话正文必须顶部对齐，避免落在卡片中部。")
	if hint.anchor_left != 1.0 or hint.anchor_top != 1.0 or hint.offset_right > -80.0 or hint.offset_bottom > -40.0:
		failures.append("点击继续提示必须独立锚定在对话框右下角。")
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("HORIZONTAL_WATERING_DEMO_TEST_PASS: 预置对话、方格代码台与魔法演出节点契约完整")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
