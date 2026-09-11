extends SceneTree

const LEVEL := preload("res://scenes/level_demo/crop_adaptive_watering_demo.tscn")
var failures: Array[String] = []


func _initialize() -> void:
	root.size = Vector2i(1280, 720)
	call_deferred("_run")


func _run() -> void:
	var level := LEVEL.instantiate() as CropAdaptiveWateringDemo
	level.timing_scale = 0.15
	root.add_child(level)
	await process_frame
	level.story_dialogue.skip_sequence()
	level._begin_manual_compare()
	for index in [1, 6]:
		level._on_plot_pressed(index)
		await level._choose_manual_water(CropAdaptiveWateringDemo.EXPECTED_UNITS[index])
	level._on_plot_pressed(5)
	level._choose_manual_water(2)
	_check(level._manual_feedback_playing, "最后一次正确选择必须先播放土地浇水反馈")
	_check(not level.skill_tree_overlay.visible, "动画开始时技能树不得遮住最后一块土地")
	_check(not level.water_choices.visible, "浇水期间必须收起选择，避免重复操作")
	level._on_plot_pressed(5)
	level._choose_manual_water(2)
	_check(level._manual_cursor == 2 and level._selected_manual_plot == -1, "动画期间重复点击不得提前推进或重新选中土地")
	await _wait_for_feedback(level)
	_check(level.skill_tree_overlay.visible and level._phase == CropAdaptiveWateringDemo.Phase.SKILL_TREE, "动画结束后才能打开技能树")
	_check((level.plot_grid.get_child(5) as CropPlotCard).soil_art.motion_id == "soil-target-met-ambient", "技能树出现前最后一块土地必须已切到湿润状态")
	level._on_plot_pressed(5)
	_check(level._manual_cursor == 3, "完成后的土地点击不得读取越界的步骤索引")

	# Leaving the lesson while a clip is running must invalidate its continuation.
	level.restart_level()
	level.story_dialogue.skip_sequence()
	level._begin_manual_compare()
	level._on_plot_pressed(1)
	level._choose_manual_water(1)
	level.restart_level()
	level.story_dialogue.skip_sequence()
	await create_timer(0.5).timeout
	_check(level._phase == CropAdaptiveWateringDemo.Phase.INTRO and level._manual_cursor == 0, "重玩后旧动画不得推进新一轮的步骤")
	_check(not level.skill_tree_overlay.visible, "重玩后旧动画不得重新弹出技能树")
	level._begin_manual_compare()
	level._on_plot_pressed(1)
	level._choose_manual_water(1)
	level._begin_workshop_experiments()
	level.story_dialogue.skip_sequence()
	await create_timer(0.5).timeout
	_check(level._phase == CropAdaptiveWateringDemo.Phase.WORKSHOP and level._manual_cursor == 0, "切入工坊后旧动画不得回写手动步骤")
	for card: CropPlotCard in level.plot_grid.get_children():
		_check(not card.attention_hint.visible, "工坊页面不得残留土地指引")
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("CROP_MANUAL_TRANSITION_TEST_PASS: 动画完成后弹窗、重复点击、重玩与离开取消通过")
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)


func _wait_for_feedback(level: CropAdaptiveWateringDemo) -> void:
	var deadline := Time.get_ticks_msec() + 5000
	while level._manual_feedback_playing and Time.get_ticks_msec() < deadline:
		await process_frame
	_check(not level._manual_feedback_playing, "浇水反馈必须在有限时间内完成")


func _check(condition: bool, message: String) -> void:
	if not condition:
		failures.append(message)
