extends SceneTree

const LEVEL_PATH := "res://scenes/level_demo/crop_adaptive_watering_demo.tscn"


func _initialize() -> void:
	var failures: Array[String] = []
	var level := (load(LEVEL_PATH) as PackedScene).instantiate() as CropAdaptiveWateringDemo
	level.timing_scale = 0.05
	root.add_child(level)
	await process_frame
	var story_overlay := level.get_node("StoryDialogueOverlay") as StoryDialogueOverlay
	story_overlay.skip_sequence()
	story_overlay.play_agent_presentation("叮当师傅", null, "请先完成两步代码实验。", "两个数组为什么要使用同一个 i？", "教学实验")
	story_overlay.advance()
	await process_frame
	var dialogue_card := story_overlay.get_node("DialogueCard") as Control
	var dialogue_question := story_overlay.get_node("DialogueCard/ContentRoot/ContentMargin/Content/Question") as Label
	if dialogue_card.get_global_rect().end.y - dialogue_question.get_global_rect().end.y < 70.0:
		failures.append("叮当对话框的“想一想”文字必须上移，避开底部叶片装饰。")
	story_overlay.skip_sequence()
	var grid := level.get_node_or_null("Hud/FarmLayout/PlotGrid") as GridContainer
	if grid == null or grid.columns != 4 or grid.get_child_count() != 8:
		failures.append("第一关必须预置2×4共8块作物土地。")
	for required_path in [
		"Hud/TaskCard", "Hud/PhasePill", "Hud/EvidencePanel", "Hud/WaterChoices",
		"CodeDrawer/Surface/Margin/Content/CodeEditor",
		"CodeDrawer/DismissCodeButton",
		"CodeDrawer/Surface/Margin/Content/Header/CloseCodeButton",
		"CodeDrawer/Surface/Margin/Content/Actions/RunButton",
		"CompletionCard/Margin/Content/Actions/ReplayButton",
		"CompletionCard/Margin/Content/Actions/NextButton",
		"Hud/ToolRail/RequestPatchButton",
		"Hud/PlaybackRail/PlaybackSpeedButton",
		"Hud/PlaybackRail/SkipPlaybackButton",
		"Hud/PlaybackRail/ReplayPlaybackButton",
		"StoryDialogueOverlay", "PatchDialog",
		"SkillTreeOverlay", "WorkshopOverlay", "WorkshopOverlay/Card/Margin/Content/WorkshopCodePanel",
		"WorkshopOverlay/Card/Margin/Content/WorkshopCodePanel/Margin/Stack/WorkshopGapCode/GapLine/GapTargetInput",
		"WorkshopOverlay/Card/Margin/Content/WorkshopCodePanel/Margin/Stack/WorkshopBranchCode/SevereCondition/SevereBoundaryInput",
		"BugChallengeOverlay", "GrowthSummaryOverlay",
	]:
		if level.get_node_or_null(required_path) == null:
			failures.append("缺少第一关预置节点：%s" % required_path)
	for removed_path in [
		"CodeDrawer/Surface/Margin/Content/Actions/BuildButton",
		"CodeDrawer/Surface/Margin/Content/Actions/ActivationButton",
	]:
		if level.get_node_or_null(removed_path) != null:
			failures.append("代码界面不应再显示构建或激活按钮：%s" % removed_path)
	for pending_button_path in [
		"Hud/PlaybackRail/PlaybackSpeedButton",
		"Hud/PlaybackRail/SkipPlaybackButton",
		"Hud/PlaybackRail/ReplayPlaybackButton",
	]:
		var pending_button := level.get_node_or_null(pending_button_path) as Button
		if pending_button == null or not pending_button.disabled:
			failures.append("等待逻辑接入的按钮必须保持禁用：%s" % pending_button_path)
	var reject_patch_button := level.find_child("RejectPatchButton", true, false) as Button
	if reject_patch_button == null or reject_patch_button.disabled:
		failures.append("Patch 对话框必须提供可用的明确拒绝按钮。")
	var patch_dialog := level.get_node("PatchDialog") as ConfirmationDialog
	if patch_dialog.get_cancel_button().text != "关闭预览":
		failures.append("关闭 Patch 预览必须与明确拒绝操作区分。")
	var grass := level.get_node("Grass") as TextureRect
	if grass.anchor_right != 1.0 or grass.anchor_bottom != 1.0 or grass.stretch_mode != TextureRect.STRETCH_KEEP_ASPECT_COVERED:
		failures.append("土地背景必须完整覆盖关卡摄像机范围。")
	var evidence_panel := level.get_node("Hud/EvidencePanel") as Control
	var evidence_body := level.get_node("Hud/EvidencePanel/Margin/Content/EvidenceBody") as RichTextLabel
	var water_choices := level.get_node("Hud/WaterChoices") as HBoxContainer
	var initial_editor := level.get_node("CodeDrawer/Surface/Margin/Content/CodeEditor") as CodeEdit
	if initial_editor.text != CropAdaptiveWateringDemo.INITIAL_PRACTICE_CODE or "/*目标*/" not in initial_editor.text:
		failures.append("首次代码草稿必须是含填空的初步代码，不得一上来给完整答案。")
	var first_card := grid.get_child(0) as CropPlotCard
	if not (first_card.get_node("Margin/Content/Values/CurrentLabel") as Label).text.begins_with("当前湿度"):
		failures.append("土地卡片必须明确标注“当前湿度”。")
	if not (first_card.get_node("Margin/Content/Values/TargetLabel") as Label).text.begins_with("目标湿度"):
		failures.append("土地卡片必须明确标注“目标湿度”。")
	evidence_body.text = "[b]同下标读取当前值与目标值 → 计算 gap → 选择 0 / 1 / 2 份水[/b]\n叮当师傅已经在清泉工坊准备好代码卷轴。"
	await process_frame
	if evidence_body.fit_content or evidence_panel.size.y > 110.0:
		failures.append("底部说明面板必须保持紧凑高度，不得自动撑高遮挡土地。")
	level.call("_begin_manual_compare")
	if not evidence_body.text.contains("缺口 ≥ 30") or not evidence_body.text.contains("缺口 ≤ 0"):
		failures.append("让孩子选择水量前，必须先告知缺口与 0 / 1 / 2 份水的完整映射。")
	var expected_manual_order := [1, 6, 5]
	for expected_index in expected_manual_order:
		for card_index in range(grid.get_child_count()):
			var attention := grid.get_child(card_index).get_node("AttentionFrame") as Panel
			if attention.visible != (card_index == expected_index):
				failures.append("手动验证阶段必须持续高亮当前土地%d。" % expected_index)
		level.call("_on_plot_pressed", expected_index)
		await process_frame
		if expected_index == 1:
			if not evidence_body.text.contains("当前湿度") or not evidence_body.text.contains("目标湿度"):
				failures.append("手动判断提示必须使用完整的湿度字段名。")
			if evidence_panel.get_global_rect().end.y > water_choices.get_global_rect().position.y:
				failures.append("浇水份数选项必须放在提示框下方，不得与提示文字重叠。")
			var lowest_card_edge := 0.0
			for card in grid.get_children():
				lowest_card_edge = maxf(lowest_card_edge, (card as Control).get_global_rect().end.y)
			if lowest_card_edge > evidence_panel.get_global_rect().position.y:
				failures.append("提示框必须放在农作物网格下方。")
		level.call("_choose_manual_water", CropAdaptiveWateringDemo.EXPECTED_UNITS[expected_index])
	if not (level.get_node("SkillTreeOverlay") as Control).visible or int(level.get("_phase")) != CropAdaptiveWateringDemo.Phase.SKILL_TREE:
		failures.append("手动比较完成后必须进入4★技能树页面。")
	level.call("_on_skill_tree_continue_pressed")
	if not (level.get_node("WorkshopOverlay") as Control).visible or int(level.get("_phase")) != CropAdaptiveWateringDemo.Phase.WORKSHOP:
		failures.append("技能树委托后必须进入叮当三步实验页面。")
	var gap_code := level.get_node("WorkshopOverlay/Card/Margin/Content/WorkshopCodePanel/Margin/Stack/WorkshopGapCode") as Control
	var branch_code := level.get_node("WorkshopOverlay/Card/Margin/Content/WorkshopCodePanel/Margin/Stack/WorkshopBranchCode") as Control
	var summary_code := level.get_node("WorkshopOverlay/Card/Margin/Content/WorkshopCodePanel/Margin/Stack/WorkshopSummaryCode") as Control
	var gap_target := level.get_node("WorkshopOverlay/Card/Margin/Content/WorkshopCodePanel/Margin/Stack/WorkshopGapCode/GapLine/GapTargetInput") as LineEdit
	var gap_moisture := level.get_node("WorkshopOverlay/Card/Margin/Content/WorkshopCodePanel/Margin/Stack/WorkshopGapCode/GapLine/GapMoistureInput") as LineEdit
	if not gap_code.visible or not gap_target.text.is_empty() or not gap_moisture.text.is_empty():
		failures.append("叮当教学第一步必须显示两个真实的数组名输入框。")
	level.call("_on_workshop_action_pressed")
	if int(level.get("_workshop_step")) != 0:
		failures.append("第一步代码未填完时不得直接跳到下一步。")
	gap_target.text = "target"
	gap_moisture.text = "moisture"
	level.call("_on_workshop_action_pressed")
	if int(level.get("_workshop_step")) != 1 or not branch_code.visible or gap_code.visible:
		failures.append("数据配对通过后必须进入分级边界与输出份数填空。")
	(level.get_node("%SevereBoundaryInput") as LineEdit).text = "30"
	(level.get_node("%SevereUnitsInput") as LineEdit).text = "2"
	(level.get_node("%LightBoundaryInput") as LineEdit).text = "0"
	(level.get_node("%LightUnitsInput") as LineEdit).text = "1"
	level.call("_on_workshop_action_pressed")
	if int(level.get("_workshop_step")) != 2 or not summary_code.visible or branch_code.visible:
		failures.append("两步填空通过后必须先把数据配对与分级动作的理解结合起来。")
	level.call("_on_workshop_action_pressed")
	await process_frame
	if not (level.get_node("CodeDrawer") as Control).visible:
		failures.append("代码界面必须可以打开。")
	if initial_editor.text != CropAdaptiveWateringDemo.INITIAL_PRACTICE_CODE or not initial_editor.text.contains("第一步") or not initial_editor.text.contains("第二步"):
		failures.append("最终卷轴必须严格保留文档的两步练习结构和待补代码注释，不得自动带入答案。")
	var run_button := level.get_node("CodeDrawer/Surface/Margin/Content/Actions/RunButton") as Button
	if run_button.disabled or not run_button.text.contains("直接运行"):
		failures.append("进入代码阶段后必须立即提供唯一的“直接运行”操作。")
	(level.get_node("CodeDrawer/Surface/Margin/Content/Header/CloseCodeButton") as Button).pressed.emit()
	await create_timer(0.05).timeout
	if (level.get_node("CodeDrawer") as Control).visible:
		failures.append("代码界面的关闭按钮必须收起抽屉。")
	var fixed_target := CropAdaptiveWateringDemo.evaluate_source(CropAdaptiveWateringDemo.STARTER_CODE)
	if not fixed_target.get("build_ok", false) or fixed_target.get("failure_key") != "FIXED_TARGET_VALUE":
		failures.append("固定60代码必须构建成功并稳定分类为FIXED_TARGET_VALUE。")
	var correct := CropAdaptiveWateringDemo.evaluate_source(CropAdaptiveWateringDemo.CORRECT_CODE)
	if not correct.get("objective_succeeded", false):
		failures.append("参考规则必须客观通过。")
	var actions: Array = correct.get("actions", [])
	if actions.size() != 8:
		failures.append("正确规则必须为8次迭代生成事实记录。")
	else:
		var units: Array[int] = []
		for action: Dictionary in actions:
			units.append(int(action.get("units", -1)))
		if units != [2, 1, 1, 0, 0, 2, 0, 1]:
			failures.append("0/1/2份水量映射不符合关卡文档。")
	var reversed := CropAdaptiveWateringDemo.evaluate_source(CropAdaptiveWateringDemo.CORRECT_CODE.replace("target[i] - moisture[i]", "moisture[i] - target[i]"))
	if reversed.get("failure_key") != "REVERSED_GAP":
		failures.append("差值方向写反必须产生独立失败分类。")
	var boundary := CropAdaptiveWateringDemo.evaluate_source(CropAdaptiveWateringDemo.CORRECT_CODE.replace("i < 8", "i < 7"))
	if boundary.get("failure_key") != "LOOP_BOUNDARY":
		failures.append("漏掉7号土地必须产生循环边界失败分类。")
	level.set("_same_failure_key", "FIXED_TARGET_VALUE")
	level.set("_same_failure_count", 1)
	level.set("_hint_level", -1)
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.FAILED)
	level.call("_on_hint_pressed")
	if not (level.get_node("Hud/EvidencePanel/Margin/Content/EvidenceTitle") as Label).text.ends_with("L0"):
		failures.append("失败后的第一次求助必须从L0事实提示开始。")
	level.set("_same_failure_count", 4)
	level.set("_hint_level", 2)
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.FAILED)
	var patch_button := level.get_node("Hud/ToolRail/RequestPatchButton") as Button
	if patch_button.visible:
		failures.append("未看完L0-L3提示时不能请求AI修改提案。")
	level.call("_on_hint_pressed")
	if not patch_button.visible:
		failures.append("同类失败4次且看完L3后应开放主动AI修改提案。")
	level.set("_build_result", fixed_target)
	level.set("_same_failure_count", 2)
	level.set("_same_failure_key", "FIXED_TARGET_VALUE")
	level.set("_bug_challenge_seen", false)
	level.call("_fail_run")
	await process_frame
	if not (level.get_node("BugChallengeOverlay") as Control).visible:
		failures.append("第三次同类失败必须展示Bug先生的双作物真实反例。")
	level.call("_on_bug_continue_pressed")
	if (level.get_node("BugChallengeOverlay") as Control).visible:
		failures.append("Bug真实反例必须可以确认后关闭。")
	level.set("_same_failure_count", 4)
	level.set("_hint_level", 3)
	initial_editor.text = CropAdaptiveWateringDemo.STARTER_CODE
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.FAILED)
	level.call("_on_patch_requested")
	if not (level.get_node("PatchDialog") as ConfirmationDialog).visible:
		failures.append("满足阈值后必须能打开AI Patch前后对比。")
	reject_patch_button.pressed.emit()
	await process_frame
	if str(level.get_node("CodeDrawer/Surface/Margin/Content/CodeEditor").get("text")) != CropAdaptiveWateringDemo.STARTER_CODE:
		failures.append("拒绝Patch必须保留学生原草稿。")
	level.set("_same_failure_count", 5)
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.FAILED)
	level.call("_on_patch_requested")
	(level.get_node("PatchDialog") as ConfirmationDialog).hide()
	if not bool(level.get("_patch_pending")):
		failures.append("关闭Patch预览不得被当成拒绝，未决提案必须保留。")
	level.call("_on_patch_requested")
	level.call("_accept_patch")
	if not (level.get_node("CodeDrawer/Surface/Margin/Content/CodeEditor") as CodeEdit).text.contains("target[i] - moisture[i]"):
		failures.append("接受Patch必须只生成包含局部修改的新草稿。")
	if int(level.get("_phase")) != CropAdaptiveWateringDemo.Phase.CODE or run_button.disabled:
		failures.append("接受Patch后必须回到可由学生再次直接运行的草稿状态。")
	level.call("_complete_level")
	if int(level.get("_phase")) != CropAdaptiveWateringDemo.Phase.OBJECTIVE_COMPLETE:
		failures.append("成功后必须先进入客观世界完成页。")
	level.call("_show_growth_summary")
	if not (level.get_node("GrowthSummaryOverlay") as Control).visible:
		failures.append("书书成长总结页面必须可以根据本轮记录展示。")
	level.call("_on_archive_pressed")
	if not (level.get_node("SkillTreeOverlay") as Control).visible or int(level.get("_phase")) != CropAdaptiveWateringDemo.Phase.SKILL_UNLOCKED:
		failures.append("完成归档后必须回到已解锁的4★技能树页面。")
	var title_before_error := (level.get_node("Hud/EvidencePanel/Margin/Content/EvidenceTitle") as Label).text
	var body_before_error := evidence_body.text
	level.present_agent_error("AGENT_RAW_ERROR_SHOULD_NOT_BE_VISIBLE")
	if (level.get_node("Hud/EvidencePanel/Margin/Content/EvidenceTitle") as Label).text != title_before_error or evidence_body.text != body_before_error:
		failures.append("Agent 异常不得覆盖当前关卡说明。")
	level.fail_agent_submission("验证", "AGENT_RAW_ERROR_SHOULD_NOT_BE_VISIBLE")
	if "AGENT_RAW_ERROR_SHOULD_NOT_BE_VISIBLE" in evidence_body.text or "Agent" in (level.get_node("Hud/EvidencePanel/Margin/Content/EvidenceTitle") as Label).text:
		failures.append("Agent 提交失败时不得显示原始报错或 Agent 字样。")
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("CROP_ADAPTIVE_WATERING_DEMO_TEST_PASS: 2×4农田、分层错误与0/1/2份规则通过")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
