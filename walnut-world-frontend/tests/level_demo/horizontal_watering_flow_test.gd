extends SceneTree

const LEVEL_PATH := "res://scenes/level_demo/horizontal_watering_demo.tscn"
const CORRECT_CODE := "for (int i = 0; i < 5; i++) {\n    cout << \"浇\" << i << \"号土地中……\\n\";\n}\ncout << \"浇灌完成！\";"


func _initialize() -> void:
	var failures: Array[String] = []
	var level := (load(LEVEL_PATH) as PackedScene).instantiate()
	level.demo_timing_scale = 0.0
	root.add_child(level)
	await process_frame
	await process_frame
	var story := level.get_node("Hud/StoryDialogueOverlay") as StoryDialogueOverlay
	var body := story.get_node("DialogueCard/ContentRoot/ContentMargin/Content/Body") as Label
	if not story.visible or body.text != "我刚刚种了一排幼苗":
		failures.append("开场必须先显示芽芽的第一句全屏对话。")
	if level.manual_water_plot(0):
		failures.append("芽芽对话结束前不得开始操作土地。")
	level.skip_story_dialogue()
	await process_frame
	var first_plot := level.get_node("ManualRow/Plot0") as WateringPlot
	if not first_plot.is_guided():
		failures.append("对话结束后必须把唯一视觉焦点交给 0 号土地。")
	if level.manual_water_plot(1):
		failures.append("手动阶段不得跳过当前土地。")
	for index in range(5):
		if not level.manual_water_plot(index):
			failures.append("手动阶段未接受连续土地：%d" % index)
		await create_timer(0.12).timeout
	if level.get_phase_name() != "DISCOVER_REPEAT" or not story.visible:
		failures.append("第一排完成后必须显示叮当师傅的线性对话。")
	if body.text != "孩子，你做得真不错":
		failures.append("叮当师傅第一句文案不正确。")
	level.skip_story_dialogue()
	await process_frame
	if level.get_phase_name() != "CODE_CHALLENGE" or not level.get_node("Hud/CodeDrawer").visible:
		failures.append("叮当对话结束后必须直接亮出固定代码区和运行结果区。")
	level.set_fill_values("0", "5", "i")
	level.flush_autosave()
	if level.get_fill_values() != ["0", "5", "i"] or level.get_saved_draft() != CORRECT_CODE:
		failures.append("三个方格必须生成并自动保存正确的 for 循环。")
	level.reset_code_to_starter()
	if level.get_fill_values() != ["", "", ""]:
		failures.append("重填按钮必须清空三个方格。")
	var starts_at_one: Dictionary = level.evaluate_code("for (int i = 1; i < 5; i++) { cout << \"浇\" << i; }")
	if starts_at_one.get("error_kind") != "missing_first":
		failures.append("边界错误必须准确报告遗漏 0 号土地。")
	var constant_zero: Dictionary = level.evaluate_code("for (int i = 0; i < 5; i++) { cout << \"浇\" << 0; }")
	if constant_zero.get("error_kind") != "constant_plot":
		failures.append("固定浇 0 号土地必须提示第三格应使用 i。")
	var success: Dictionary = level.submit_code_for_test(CORRECT_CODE)
	if not success.get("passed", false) or level.get_phase_name() != "COMPLETED":
		failures.append("正确循环必须完成关卡。")
	var trace := level.get_node("Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Trace") as RichTextLabel
	if not trace.text.contains("浇0号土地中") or not trace.text.contains("浇灌完成"):
		failures.append("运行结果必须使用儿童可读的浇水文案。")
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("HORIZONTAL_WATERING_FLOW_TEST_PASS: 全屏对话、手动浇水、方格循环与结算闭环通过")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
