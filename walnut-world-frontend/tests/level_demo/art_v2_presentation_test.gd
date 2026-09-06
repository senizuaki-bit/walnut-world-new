extends SceneTree


func _initialize() -> void:
	root.size = Vector2i(1280, 720)
	call_deferred("_run")


func _run() -> void:
	var failures: Array[String] = []
	var level := load("res://scenes/level_demo/crop_adaptive_watering_demo.tscn").instantiate() as CropAdaptiveWateringDemo
	level.timing_scale = 0.05
	root.add_child(level)
	await process_frame
	level.story_dialogue.skip_sequence()
	await _verify_second_review(level, failures)
	await _verify_review_regressions(level, failures)
	var card := level.plot_grid.get_child(0) as CropPlotCard
	var moisture := card.current_moisture
	var target := card.target_moisture
	card.configure(0, "胡萝卜", 20, 60, null)
	if (card.crop_art as ArtMotionTexture).motion_id != "crop-carrot-severe-dry-sway":
		failures.append("严重缺水必须对应缺水作物动画。")
	card.set_attention(true)
	if not card.soil_glow.visible or card.attention_frame.visible:
		failures.append("土地注意力必须使用透明轮廓动画，旧矩形框必须停用。")
	card.show_candidate_outcome(7200, "OVERWATERED")
	if not (card.crop_art as ArtMotionTexture).motion_id.contains("waterlogged"):
		failures.append("过浇反馈必须展示过湿作物。")
	if card.current_moisture != moisture or card.target_moisture != target:
		failures.append("候选视觉反馈不得改写权威湿度。")
	card.reset_candidate_display()
	if not (card.crop_art as ArtMotionTexture).motion_id.contains("severe-dry"):
		failures.append("重播前必须恢复初始作物状态。")
	level.call("_show_skill_tree", false)
	for index in range(5):
		var icon := level.get_node("SkillTreeOverlay/Card/TreeStage/Node%d" % index) as TextureRect
		if icon.texture == null or icon.mouse_filter != Control.MOUSE_FILTER_IGNORE:
			failures.append("五个技能节点必须有图标且不能新增学习热区。")
	for concept: Node in level.get_node("SkillTreeOverlay/Card/Margin/Content/ConceptCards").get_children():
		if (concept.get_node("Content/Completed") as Control).visible:
			failures.append("剧情可学习时不得显示完成标记。")
	var dialogue := level.story_dialogue
	dialogue.play_sequence("小核桃", null, ["正在读取这一块土地的湿度。"])
	if (dialogue.portrait as ArtMotionTexture).motion_id != "char-walnut-talk":
		failures.append("当前说话者必须切换说话动画。")
	dialogue.advance()
	if (dialogue.portrait as ArtMotionTexture).motion_id != "char-walnut-idle":
		failures.append("台词完成后必须切回待机。")
	dialogue.skip_sequence()
	dialogue.play_sequence("书书", null, ["这是一段需要保留完整阅读空间的成长记录。".repeat(100)])
	dialogue.advance()
	await process_frame
	await process_frame
	var scroll := dialogue.get_node("DialogueCard/ContentRoot/ContentMargin") as ScrollContainer
	if scroll.get_v_scroll_bar().max_value <= scroll.get_v_scroll_bar().page:
		failures.append("长角色消息必须可以滚动阅读，不能裁掉正文。")
	dialogue.skip_sequence()
	dialogue.play_sequence("书书", null, ["点击正文区域仍然可以立即展开整句对话。"])
	await process_frame
	await process_frame
	var click := InputEventMouseButton.new()
	click.button_index = MOUSE_BUTTON_LEFT
	click.pressed = true
	click.position = scroll.get_global_rect().get_center()
	root.push_input(click, true)
	await process_frame
	if dialogue.is_typing():
		failures.append("滚动区域不能吞掉点击展开对话的操作。")
	dialogue.skip_sequence()
	var motion := level.get_node("PlayerCompanion") as ArtMotionTexture
	motion.play_clip("char-player-talk", true)
	await process_frame
	motion.hide()
	var paused_at := motion.elapsed_ms
	await create_timer(0.05).timeout
	if not is_equal_approx(motion.elapsed_ms, paused_at):
		failures.append("隐藏动画必须冻结进度。")
	motion.show()
	motion.reduced_motion = true
	await process_frame
	if motion.texture is AtlasTexture:
		failures.append("减少动态时必须显示静态回退图。")
	var frames: SpriteFrames = level.watering_can.sprite_frames
	var durations: Array[float] = []
	for clip in [&"pour", &"pour_two"]:
		var duration := 0.0
		for index in range(frames.get_frame_count(clip)):
			duration += frames.get_frame_duration(clip, index) / frames.get_animation_speed(clip)
		durations.append(duration)
		if frames.get_animation_loop_mode(clip) != SpriteFrames.LOOP_NONE:
			failures.append("浇水必须是单次动画。")
	if absf(durations[0] - 2.0) > 0.1 or absf(durations[1] - 4.0) > 0.1:
		failures.append("1份和2份浇水必须遵循素材包约2秒和4秒时序。")
	Engine.set_meta("art_reduced_motion", true)
	level.watering_can.visible = true
	level.watering_can.speed_scale = 100.0
	level.watering_can.play(&"pour")
	await process_frame
	if not (level.watering_can.get_node("Still") as Sprite2D).visible:
		failures.append("减少动态时浇水器必须显示静态回退。")
	await create_timer(0.08).timeout
	if level.watering_can.is_playing():
		failures.append("静态回退不能阻塞原有动作时钟。")
	Engine.remove_meta("art_reduced_motion")
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("ART_V2_PRESENTATION_PASS")
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)


func _verify_review_regressions(level: CropAdaptiveWateringDemo, failures: Array[String]) -> void:
	var corn := level.plot_grid.get_child(3) as CropPlotCard
	if not (corn.crop_art as ArtMotionTexture).motion_id.contains("waterlogged"):
		failures.append("初始湿度90/目标65的玉米必须显示积水。")
	corn.configure(3, "玉米", 73, 65, null)
	if (corn.crop_art as ArtMotionTexture).motion_id.contains("waterlogged"):
		failures.append("超出目标恰好8不应显示积水。")
	corn.configure(3, "玉米", 74, 65, null)
	if not (corn.crop_art as ArtMotionTexture).motion_id.contains("waterlogged"):
		failures.append("超出目标9必须显示积水。")
	corn.configure(3, "玉米", 90, 65, null)
	level.call("_begin_workshop_experiments")
	await process_frame
	var dialogue := level.story_dialogue
	var field := level.gap_target_input
	for keycode in [KEY_A, KEY_TAB]:
		var key := InputEventKey.new()
		key.keycode = keycode
		key.unicode = 97 if keycode == KEY_A else 0
		key.pressed = true
		root.push_input(key, true)
		await process_frame
	if not field.text.is_empty() or field.has_focus():
		failures.append("对话期间键盘和Tab不能穿透到底层输入。")
	# Real keyboard input must complete the line, not type a newline underneath.
	var enter := InputEventKey.new()
	enter.keycode = KEY_ENTER
	enter.pressed = true
	root.push_input(enter, true)
	if dialogue.is_typing():
		failures.append("对话必须支持键盘继续。")
	dialogue.skip_sequence()
	if not field.has_focus():
		failures.append("关闭对话后应恢复原输入焦点。")
	field.text = "wrong"
	level.gap_moisture_input.text = "moisture"
	level.call("_on_workshop_action_pressed")
	if not bool(field.get("has_error")) or bool(level.gap_moisture_input.get("has_error")):
		failures.append("错误外观必须只标记不正确字段。")
	if field.text != "wrong" or not (level.get_node("%WorkshopError") as Label).visible:
		failures.append("校验失败必须保留输入并显示独立说明。")
	field.text = "target"
	field.text_changed.emit(field.text)
	if bool(field.get("has_error")) or (level.get_node("%WorkshopError") as Label).visible:
		failures.append("重新编辑后必须清除旧字段错误与提示。")
	level.call("_on_workshop_action_pressed")
	level.call("_on_workshop_action_pressed")
	var messages := (level.get_node("%WorkshopError") as Label).text
	if not bool(level.severe_boundary_input.get("has_error")) or not bool(level.light_units_input.get("has_error")) or messages.is_empty():
		failures.append("四个数值字段也必须独立校验并提供原因。")
	level.call("_hide_lesson_overlays")
	level.call("_begin_manual_compare")
	var card := level.plot_grid.get_child(1) as CropPlotCard
	for reduced in [false, true]:
		Engine.set_meta("art_reduced_motion", reduced)
		level.call("_on_plot_pressed", 0)
		if card.scale != Vector2.ONE or not card.soil_glow.visible or int(level.get("_manual_cursor")) != 0:
			failures.append("误点只能增强目标土地光晕，不缩放卡片或推进序号。")
	Engine.remove_meta("art_reduced_motion")
	level.set("_same_failure_count", 4)
	level.set("_same_failure_key", "FIXED_TARGET_VALUE")
	level.set("_hint_level", 3)
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.FAILED)
	await process_frame
	await process_frame
	var tools := level.get_node("Hud/ToolRail") as Control
	var playback := level.get_node("Hud/PlaybackRail") as Control
	if tools.get_global_rect().intersects(playback.get_global_rect()):
		failures.append("提案按钮可见时，工具栏与播放控件仍不能重叠。")
	level.call("_on_patch_requested")
	await process_frame
	await process_frame
	if level.patch_dialog.size.y > 680:
		failures.append("提案弹窗必须完整放入720高的视口，实际大小 %s。" % level.patch_dialog.size)
	if level.patch_dialog.get_node_or_null("Content/Rows/Columns/Before/Rows/Code") == null or level.patch_dialog.get_node_or_null("Content/Rows/Columns/After/Rows/Code") == null:
		failures.append("提案必须提供独立的前后对照栏。")
	level.patch_dialog.hide()
	var pump := level.get_node("Pump") as ArtMotionTexture
	pump.playback_speed = 30.0
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.RUNNING)
	if pump.motion_id != "prop-pump-start":
		failures.append("本地执行开始应触发水泵启动。")
	# Headless rendering also loads atlases asynchronously; wait with a bounded deadline.
	var deadline := Time.get_ticks_msec() + 5000
	while pump.motion_id == "prop-pump-start" and Time.get_ticks_msec() < deadline:
		await process_frame
	if pump.motion_id != "prop-pump-working":
		failures.append("水泵启动完成后必须进入工作循环。")
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.FAILED)
	if pump.motion_id != "prop-pump-stop":
		failures.append("执行结束应触发水泵停机。")
	deadline = Time.get_ticks_msec() + 5000
	while pump.motion_id == "prop-pump-stop" and Time.get_ticks_msec() < deadline:
		await process_frame
	if pump.motion_id != "prop-pump-standby":
		failures.append("水泵停机结束后必须回到待机。")
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.RUNNING)
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.FAILED)
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CHAIN_ERROR)
	await create_timer(0.15).timeout
	if pump.motion_id != "prop-pump-fault":
		failures.append("故障必须中断停机过渡，不能被旧动画切回待机。")
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	if pump.motion_id != "prop-pump-standby":
		failures.append("故障恢复后应回到待机。")
	Engine.set_meta("art_reduced_motion", true)
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.RUNNING)
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	if pump.motion_id != "prop-pump-standby":
		failures.append("减少动态不能阻塞水泵状态回收。")
	Engine.remove_meta("art_reduced_motion")
	pump.playback_speed = 1.0


func _verify_second_review(level: CropAdaptiveWateringDemo, failures: Array[String]) -> void:
	if level.get_node_or_null("Hud/ReduceMotion") != null:
		failures.append("不再提供用户不需要的减少动态开关。")
	var dialogue := level.story_dialogue
	var counts := [0]
	var finished := func() -> void: counts[0] += 1
	dialogue.sequence_finished.connect(finished)
	dialogue.play_sequence("书书", null, ["结束这一段对话。"])
	dialogue.advance()
	dialogue.advance()
	await create_timer(0.05).timeout
	dialogue.skip_sequence()
	dialogue.skip_sequence()
	await create_timer(0.3).timeout
	if counts[0] != 1:
		failures.append("退场途中中断及重复中断，只能通知结束一次。")
	dialogue.play_sequence("书书", null, ["新对话必须不受旧回调影响。"])
	await create_timer(0.25).timeout
	if not dialogue.visible or counts[0] != 1:
		failures.append("中断旧对话后，新对话不能被旧回调提前关闭。")
	dialogue.skip_sequence()
	dialogue.sequence_finished.disconnect(finished)
	level.call("_begin_workshop_experiments")
	dialogue.skip_sequence()
	level.gap_target_input.text = "target"
	level.gap_target_input.text_changed.emit("target")
	level.call("_show_workshop_step")
	if not level.gap_target_input.text.is_empty() or level.gap_target_input.get_theme_stylebox("normal").resource_path.contains("filled"):
		failures.append("重新显示第一步必须同时清空内容和已填写皮肤。")
	var button := level.workshop_action_button
	await process_frame
	await process_frame
	var before := button.get_global_rect()
	level.call("_on_workshop_action_pressed")
	var skin := button.get_theme_stylebox("normal") as StyleBoxTexture
	var motion := button.get_node("Feedback") as ArtMotionTexture
	if skin.texture != motion.texture or motion.motion_id != "button-error-motion":
		failures.append("错误动画必须成为原按钮的皮肤。")
	if level.get_node_or_null("WorkshopOverlay/WorkshopFeedback") != null:
		failures.append("不能保留远离按钮的装饰反馈色条。")
	button.call("show_feedback", true)
	if motion.motion_id != "button-success-motion":
		failures.append("成功反馈必须复用同一按钮。")
	# Wait for actual atlas completion, not just the first poster frame.
	var deadline := Time.get_ticks_msec() + 5000
	while motion.visible and Time.get_ticks_msec() < deadline:
		await process_frame
	await process_frame
	if button.has_theme_stylebox_override("normal"):
		failures.append("反馈结束后必须恢复普通/悬停皮肤。")
	if not button.get_global_rect().is_equal_approx(before):
		failures.append("按钮反馈不能改变布局或点击范围：%s → %s。" % [before, button.get_global_rect()])
	button.call("show_feedback", false)
	level.call("_hide_lesson_overlays")
	if motion.visible or button.has_theme_stylebox_override("normal"):
		failures.append("离开工坊必须清除旧按钮反馈。")
