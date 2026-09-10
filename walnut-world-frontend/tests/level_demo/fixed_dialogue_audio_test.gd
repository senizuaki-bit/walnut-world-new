extends SceneTree

var failures: Array[String] = []


func check(condition: bool, message: String) -> void:
	if not condition:
		failures.append(message)


func _initialize() -> void:
	call_deferred("run")


func run() -> void:
	create_timer(40).timeout.connect(func(): push_error("FIXED_DIALOGUE_TIMEOUT"); quit(1))
	root.size = Vector2i(1280, 720)
	var level := load("res://scenes/level_demo/crop_adaptive_watering_demo.tscn").instantiate() as CropAdaptiveWateringDemo
	root.add_child(level)
	await process_frame
	var overlay := level.story_dialogue
	overlay.skip_sequence()
	for clip: AudioStream in overlay.FIXED_AUDIO.CLIPS.values():
		check(clip.get_length() > 0.1, "Every packaged clip must contain complete audio")
	var seen: Array[int] = []
	var finished := {"count": 0}
	overlay.line_changed.connect(func(index, text):
		seen.append(index)
		check(text == level.INTRO_LINES[index], "Intro must use the user's exact text")
		check(overlay.line_voice.playing, "Audio must start with each line")
	)
	var on_finished := func(): finished.count += 1
	overlay.sequence_finished.connect(on_finished)
	level._play_intro()
	check(overlay.line_voice.playing, "Intro must play local audio immediately")
	check(overlay.body_label.visible_characters == -1 and not overlay.is_typing(), "The entire sentence must appear immediately")
	check(overlay.continue_hint.visible, "Click-to-continue must be available while speech is playing")
	await create_timer(overlay.line_voice.stream.get_length() + 0.5).timeout
	check(overlay.get_line_index() == 0 and overlay.visible and finished.count == 0, "Audio ending must wait on the current line")
	check(not overlay.line_voice.playing, "Finished audio must not loop")
	var screenshot := OS.get_environment("WALNUT_FIXED_DIALOGUE_SCREENSHOT")
	if not screenshot.is_empty():
		await RenderingServer.frame_post_draw
		root.get_texture().get_image().save_png(screenshot)
	click_dialogue(overlay)
	await process_frame
	print("MANUAL_CLICK_1 line=%d playing=%s" % [overlay.get_line_index(), overlay.line_voice.playing])
	check(overlay.get_line_index() == 1 and overlay.line_voice.playing, "One click after playback must start only the next line")
	var second_stream := overlay.line_voice.stream
	await create_timer(0.2).timeout
	click_dialogue(overlay)
	await process_frame
	print("MANUAL_CLICK_2 line=%d playing=%s" % [overlay.get_line_index(), overlay.line_voice.playing])
	check(overlay.get_line_index() == 2 and overlay.line_voice.playing and overlay.line_voice.stream != second_stream, "Click during speech must stop it and play the next sentence")
	check(overlay.body_label.visible_characters == -1, "The next sentence must also be complete immediately")
	if overlay.line_voice.stream == null:
		for failure in failures:
			push_error(failure)
		quit(1)
		return
	await create_timer(overlay.line_voice.stream.get_length() + 0.5).timeout
	check(overlay.get_line_index() == 2 and overlay.visible and finished.count == 0, "The last line must also wait for a click after playback")
	var enter := InputEventKey.new()
	enter.keycode = KEY_ENTER
	enter.pressed = true
	root.push_input(enter, true)
	await create_timer(0.3).timeout
	check(seen == [0, 1, 2] and finished.count == 1, "Manual input must visit each sentence once and complete once")
	check(not overlay.visible and not overlay.line_voice.playing, "End of sequence must close dialogue and stop audio")
	check(not level.primary_button.disabled, "Intro completion must unlock the next lesson action")
	# Disconnect the intro-only observer before exercising other speakers.
	for connection: Dictionary in overlay.line_changed.get_connections():
		overlay.line_changed.disconnect(connection.callable)
	overlay.play_sequence("芽芽", null, [level.INTRO_LINES[0]])
	overlay.skip_sequence()
	check(not overlay.line_voice.playing, "Skip must stop speech immediately")
	var count_after_skip: int = finished.count
	await create_timer(0.3).timeout
	check(finished.count == count_after_skip, "Skipped audio must not emit a stale completion")
	overlay.play_sequence("系统", null, [level.INTRO_LINES[0]])
	check(not overlay.line_voice.playing, "System lines must never reuse a character's audio")
	check(overlay.body_label.visible_characters == -1, "System text must also display as a complete sentence")
	overlay.advance()
	check(not overlay.is_typing(), "Unvoiced text keeps its manual controls")
	overlay.skip_sequence()
	overlay.play_sequence("芽芽", null, ["这是新的动态回复，没有预生成音频。"])
	check(not overlay.line_voice.playing, "Changed text must not play a mismatched old recording")
	overlay.skip_sequence()
	level._begin_workshop_experiments()
	check(overlay.line_voice.playing and overlay.line_voice.stream.resource_path.ends_with("dingdang_workshop.wav"), "Workshop must use the recorded Uncle Beard line")
	overlay.hide()
	check(not overlay.line_voice.playing, "Hiding dialogue must stop speech")
	overlay.play_sequence("芽芽", null, [level.INTRO_LINES[0]])
	check(overlay.line_voice.playing and overlay.get_line_index() == 0, "Replay must restart narration from the beginning")
	overlay.skip_sequence()
	# Allow the workshop's deferred layout to settle before releasing its scene.
	await process_frame
	await process_frame
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("FIXED_DIALOGUE_AUDIO_PASS: full sentences; no automatic advance; mouse interrupts one line; keyboard completes; skip/hide/replay; system silent")
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)


func click_dialogue(overlay: StoryDialogueOverlay) -> void:
	var event := InputEventMouseButton.new()
	event.button_index = MOUSE_BUTTON_LEFT
	event.position = overlay.dialogue_scroll.get_global_rect().get_center()
	event.pressed = true
	root.push_input(event, true)
	event = event.duplicate()
	event.pressed = false
	root.push_input(event, true)
