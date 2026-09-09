extends SceneTree

const LEVEL := preload("res://scenes/level_demo/crop_adaptive_watering_demo.tscn")
var failures: Array[String] = []


func _initialize() -> void:
	Engine.set_meta("art_reduced_motion", true)
	root.size = Vector2i(1280, 720)
	root.gui_embed_subwindows = true
	var level := LEVEL.instantiate() as CropAdaptiveWateringDemo
	root.add_child(level)
	await process_frame
	level.story_dialogue.skip_sequence()
	level.call("_begin_workshop_experiments")
	var question := level.mentor_question
	check(not question.visible, "固定台词期间不能出现提问入口")
	level.story_dialogue.skip_sequence()
	await process_frame
	check(question.visible, "实验页固定台词结束后出现提问入口")
	check(not level.hint_button.visible, "旧工具栏不能显示第二个提问按钮")
	check(question.ask_button.get_global_rect().position.x >= (level.workshop_overlay.get_node("Card") as Control).get_global_rect().end.x, "提问按钮不能遮挡实验题板")
	check(question.ask_button.get_global_rect().position.y >= (level.workshop_overlay.get_node("Mentor") as Control).get_global_rect().end.y, "按钮应在师傅脚下")
	question.ask_button.button_down.emit()
	question.ask_button.button_up.emit()
	check(question.state == MentorQuestion.State.IDLE and not question.reply_panel.visible, "短按不能提交问题")
	var mouse := InputEventMouseButton.new()
	mouse.button_index = MOUSE_BUTTON_LEFT
	mouse.button_mask = MOUSE_BUTTON_MASK_LEFT
	mouse.pressed = true
	mouse.position = question.ask_button.get_global_rect().get_center()
	root.push_input(mouse, true)
	await create_timer(0.4).timeout
	check(question.state == MentorQuestion.State.RECORDING, "鼠标长按后进入聆听状态")
	question.get_window().focus_exited.emit()
	mouse = mouse.duplicate()
	mouse.pressed = false
	mouse.button_mask = 0
	root.push_input(mouse, true)
	check(question.state == MentorQuestion.State.IDLE and not question.reply_panel.visible, "取消录音不能显示回答")
	question.ask_button.button_down.emit()
	await create_timer(0.4).timeout
	question.ask_button.button_up.emit()
	check(question.reply_panel.visible and question.ask_button.disabled, "松开后等待回复并禁止重复提问")
	check(not question.understood_button.visible, "回复生成前不能显示我懂了")
	await create_timer(0.8).timeout
	check(question.state == MentorQuestion.State.ANSWERING and not question.reply_text.text.is_empty() and not question.understood_button.visible, "回复必须逐字展示")
	check(not question.understood_button.visible, "回复未完成不能提前关闭")
	question.typing_timer.wait_time = 0.001
	await wait_for_answer(question)
	for _frame in range(4):
		await process_frame
	check(question.state == MentorQuestion.State.COMPLETE and question.understood_button.visible, "全文生成完成后显示我懂了")
	check(question.reply_scroll.get_v_scroll_bar().max_value > question.reply_scroll.size.y, "长文本必须可以滚动")
	var bar := question.reply_scroll.get_v_scroll_bar()
	check(bar.value + bar.page >= bar.max_value - 1.0, "全文完成后必须能看到最后一句")
	check(question.reply_panel.get_global_rect().end.y <= question.ask_button.get_global_rect().position.y, "完成态回复框不能挤压提问按钮")
	question.ask_button.button_down.emit()
	question.ask_button.button_up.emit()
	check(question.understood_button.visible and question.reply_panel.visible, "短按不能丢失上一轮完整回答")
	question.understood_button.pressed.emit()
	check(not question.reply_panel.visible and question.ask_button.visible and not question.ask_button.disabled, "我懂了仅关闭回复框，保留可用提问按钮")

	# Native touch dispatch: release must be caught outside the GUI focus path.
	var touch := InputEventScreenTouch.new()
	touch.index = 3
	touch.pressed = true
	touch.position = question.ask_button.get_global_rect().get_center()
	root.push_input(touch, true)
	await create_timer(0.4).timeout
	check(question.state == MentorQuestion.State.RECORDING, "触摸长按需要进入聆听状态")
	touch = touch.duplicate()
	touch.pressed = false
	root.push_input(touch, true)
	await create_timer(0.8).timeout
	check(question.state == MentorQuestion.State.ANSWERING, "触摸松开后必须触发一次回复")
	level.story_dialogue.play_sequence("叮当师傅", null, ["固定台词优先。"])
	check(not question.visible and question.state == MentorQuestion.State.IDLE, "新固定台词必须取消未完成回复")
	level.story_dialogue.skip_sequence()
	check(question.visible and not question.reply_panel.visible, "固定台词结束后不能恢复旧回复")

	level.workshop_overlay.hide()
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	await process_frame
	await process_frame
	check(level.farm_mentor.visible and question.visible, "农田编写阶段显示师傅及提问按钮")
	var code_position := level.code_button.global_position
	check(absf(code_position.x - ((291.0 + 290.0) * 720.0 / 941.0 + 12.0)) < 1.0, "技能卷轴保持原工具栏位置")
	level.call("_show_code_drawer")
	check(not question.visible, "卷轴打开时隐藏被遮挡的提问入口")
	level.code_drawer.hide()
	await process_frame
	check(question.visible and code_position.is_equal_approx(level.code_button.global_position), "关闭卷轴后恢复入口且不移动卷轴按钮")
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.FAILED)
	level.set("_same_failure_count", 4)
	level.set("_same_failure_key", "FIXED_TARGET_VALUE")
	level.set("_hint_level", 3)
	level.call("_on_patch_requested")
	await process_frame
	check(level.patch_dialog.visible and question.visible, "修改预览保留师傅下方提问入口")
	check(not level.farm_mentor.visible, "修改预览不得叠加第二个师傅")
	mouse = InputEventMouseButton.new()
	mouse.button_index = MOUSE_BUTTON_LEFT
	mouse.button_mask = MOUSE_BUTTON_MASK_LEFT
	mouse.pressed = true
	mouse.position = question.ask_button.get_global_rect().get_center()
	root.push_input(mouse, true)
	await create_timer(0.4).timeout
	check(question.state == MentorQuestion.State.RECORDING, "修改预览弹窗外的按钮必须接收鼠标长按")
	mouse = mouse.duplicate()
	mouse.pressed = false
	mouse.button_mask = 0
	root.push_input(mouse, true)
	await create_timer(0.8).timeout
	check(question.state == MentorQuestion.State.ANSWERING and level.patch_dialog.visible, "修改预览提问不关闭修改比较")
	level.patch_dialog.hide()
	await process_frame
	check(question.state == MentorQuestion.State.IDLE and not question.reply_panel.visible, "退出修改预览清理专属回复")
	question.begin_hold()
	await create_timer(0.4).timeout
	question.end_hold()
	level.restart_level()
	await create_timer(0.8).timeout
	check(question.state == MentorQuestion.State.IDLE and not question.reply_panel.visible, "重开关卡后不得残留回复计时器")
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("MENTOR_QUESTION_PASS")
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)


func wait_for_answer(question: MentorQuestion) -> void:
	var deadline := Time.get_ticks_msec() + 20000
	while question.state != MentorQuestion.State.COMPLETE and Time.get_ticks_msec() < deadline:
		await process_frame
	await process_frame


func check(condition: bool, message: String) -> void:
	if not condition:
		failures.append(message)
