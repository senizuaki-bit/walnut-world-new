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
