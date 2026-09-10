extends SceneTree

func _initialize() -> void:
	var level = load("res://scenes/level_demo/crop_adaptive_watering_demo.tscn").instantiate()
	level.visible = false
	level.timing_scale = 0.01
	root.add_child(level)
	await process_frame
	level.configure_agent_mode(true)
	level.load_agent_draft("// previous draft")
	var draft := {"count": 0, "source": ""}
	level.agent_draft_changed.connect(func(source): draft.count += 1; draft.source = source)
	level.reset_button.pressed.emit()
	if draft.count != 1 or draft.source != level.INITIAL_PRACTICE_CODE:
		push_error("Reset must publish exactly one change through the formal draft signal")
		quit(1)
		return
	level.restart_level()
	await process_frame
	level.story_dialogue.skip_sequence()
	if level.code_editor.text != level.INITIAL_PRACTICE_CODE:
		push_error("Reset draft must survive re-entering the level")
		quit(1)
		return
	level.complete_agent_submission("verified run")
	var expected_title: String = level.completion_title.text
	var expected_summary: String = level.completion_summary.text
	level.show_next_level_preview()
	level.restart_level()
	await process_frame
	level.story_dialogue.skip_sequence()
	level.complete_agent_submission("verified second run")
	if level.next_button.disabled or not level.replay_button.visible or not level.return_button.visible or level.completion_title.text != expected_title or level.completion_summary.text != expected_summary:
		push_error("Second completion must reset the preview title, actions and summary")
		quit(1)
		return
	print("CROP_REPEAT_STATE_PASS")
	level.queue_free()
	await process_frame
	quit(0)
