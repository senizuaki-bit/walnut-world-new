extends SceneTree

const CROP_DEMO := preload("res://scenes/level_demo/crop_adaptive_watering_demo.tscn")
const START_SCREEN := preload("res://scenes/ui/game_start_screen.tscn")


func _initialize() -> void:
	var capture_size := Vector2i(1280, 720)
	root.size = capture_size
	root.gui_embed_subwindows = true
	var state_name := "start"
	var output_path := "res://docs/design/verification/crop-adaptive-start.png"
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with("--state="):
			state_name = argument.trim_prefix("--state=")
		elif argument.begins_with("--width="):
			capture_size.x = int(argument.trim_prefix("--width="))
		elif argument.begins_with("--height="):
			capture_size.y = int(argument.trim_prefix("--height="))
		elif argument.begins_with("--output="):
			output_path = argument.trim_prefix("--output=")
	state_name = {"free_play": "free", "world_feedback": "feedback"}.get(state_name, state_name)
	if state_name not in ["start", "intro", "manual", "manual_choice", "old_tool", "skill_tree", "workshop", "workshop_dialogue", "workshop_branch", "workshop_summary", "bug", "growth", "patch", "free", "preview", "feedback", "hint", "validating", "unlocked", "code", "failed", "results", "complete", "question_idle", "question_listening", "question_answering", "question_complete", "question_farm"]:
		push_error("Unknown capture state: %s" % state_name)
		quit(1)
		return
	var capture_root: CanvasItem
	if state_name == "start":
		capture_root = START_SCREEN.instantiate()
	else:
		capture_root = CROP_DEMO.instantiate()
	root.add_child(capture_root)
	await process_frame
	root.size = capture_size
	await process_frame
	await process_frame
	if state_name != "start":
		var level := capture_root as CropAdaptiveWateringDemo
		level.timing_scale = 0.05
		if state_name != "intro":
			(level.get_node("StoryDialogueOverlay") as StoryDialogueOverlay).skip_sequence()
		if state_name.begins_with("question_"):
			if state_name == "question_farm":
				level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
			else:
				level.call("_begin_workshop_experiments")
				level.story_dialogue.skip_sequence()
			var question := level.mentor_question
			if state_name in ["question_listening", "question_answering", "question_complete"]:
				# Screenshot-only display state: no audio capture or model request.
				question.call("_on_voice_state", "READY")
				if state_name != "question_listening":
					question.call("_on_response_started")
					question.call("_on_text_received", "先比较同一下标的目标湿度与当前湿度，再计算它们的差。", state_name == "question_complete")
		elif state_name == "manual":
			level.call("_begin_manual_compare")
		elif state_name == "manual_choice":
			level.call("_begin_manual_compare")
			level.call("_on_plot_pressed", 1)
		elif state_name == "old_tool":
			await level.call("_play_old_tool_demo")
		elif state_name == "skill_tree":
			level.call("_show_skill_tree", false)
		elif state_name == "workshop":
			level.call("_begin_workshop_experiments")
			(level.get_node("StoryDialogueOverlay") as StoryDialogueOverlay).skip_sequence()
		elif state_name == "workshop_dialogue":
			level.call("_begin_workshop_experiments")
			(level.get_node("StoryDialogueOverlay") as StoryDialogueOverlay).advance()
		elif state_name == "workshop_branch":
			level.call("_begin_workshop_experiments")
			(level.get_node("StoryDialogueOverlay") as StoryDialogueOverlay).skip_sequence()
			(level.get_node("%GapTargetInput") as LineEdit).text = "target"
			(level.get_node("%GapMoistureInput") as LineEdit).text = "moisture"
			level.call("_on_workshop_action_pressed")
		elif state_name == "workshop_summary":
			level.call("_begin_workshop_experiments")
			(level.get_node("StoryDialogueOverlay") as StoryDialogueOverlay).skip_sequence()
			(level.get_node("%GapTargetInput") as LineEdit).text = "target"
			(level.get_node("%GapMoistureInput") as LineEdit).text = "moisture"
			level.call("_on_workshop_action_pressed")
			(level.get_node("%SevereBoundaryInput") as LineEdit).text = "30"
			(level.get_node("%SevereUnitsInput") as LineEdit).text = "2"
			(level.get_node("%LightBoundaryInput") as LineEdit).text = "0"
			(level.get_node("%LightUnitsInput") as LineEdit).text = "1"
			level.call("_on_workshop_action_pressed")
		elif state_name == "bug":
			level.call("_show_bug_challenge")
			(level.get_node("StoryDialogueOverlay") as StoryDialogueOverlay).skip_sequence()
		elif state_name == "growth":
			level.call("_show_growth_summary")
		elif state_name == "patch":
			# Screenshot-only fixture; never applies the proposal or mutates a world.
			level.code_editor.text = CropAdaptiveWateringDemo.STARTER_CODE
			level.set("_same_failure_count", 4)
			level.set("_same_failure_key", "FIXED_TARGET_VALUE")
			level.set("_hint_level", 3)
			level.call("_set_phase", CropAdaptiveWateringDemo.Phase.FAILED)
			level.call("_on_patch_requested")
		elif state_name == "free" or state_name == "preview":
			level.call("_enter_free_play")
			if state_name == "preview":
				level.show_next_level_preview()
		elif state_name == "feedback":
			level.call("_begin_growth_summary")
			level.story_dialogue.advance()
		elif state_name == "hint":
			level.call("_set_phase", CropAdaptiveWateringDemo.Phase.FAILED)
			level.call("_on_hint_pressed")
			level.story_dialogue.advance()
		elif state_name == "validating":
			level.begin_agent_submission("正在检查这次行动的结果，请稍候。")
		elif state_name == "unlocked":
			level.call("_show_skill_tree", true)
		elif state_name == "code":
			level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
			level.call("_show_code_drawer")
		elif state_name == "failed":
			level.set("_build_result", CropAdaptiveWateringDemo.evaluate_source(CropAdaptiveWateringDemo.STARTER_CODE))
			for action in level.get("_build_result").actions:
				var i := int(action.plot_index)
				(level.plot_grid.get_child(i) as CropPlotCard).set_result(int(action.units), false, int(action.units) != CropAdaptiveWateringDemo.EXPECTED_UNITS[i])
			level.call("_fail_run")
		elif state_name == "results":
			for index in range(8):
				var units := 0 if CropAdaptiveWateringDemo.MOISTURE[index] >= 60 else (2 if 60 - CropAdaptiveWateringDemo.MOISTURE[index] >= 30 else 1)
				var is_error := units != CropAdaptiveWateringDemo.EXPECTED_UNITS[index]
				(level.get_node("Hud/FarmLayout/PlotGrid").get_child(index) as CropPlotCard).set_result(units, false, is_error)
			level.call("_set_phase", CropAdaptiveWateringDemo.Phase.OLD_TOOL)
		elif state_name == "complete":
			for index in range(8):
				(level.get_node("Hud/FarmLayout/PlotGrid").get_child(index) as CropPlotCard).set_result(CropAdaptiveWateringDemo.EXPECTED_UNITS[index], false)
			level.call("_complete_level")
	var settle_frames := 72 if state_name == "start" else 12
	for _frame in range(settle_frames):
		await process_frame
	# Capture settled full dialogue rather than a machine-dependent typing fragment.
	await create_timer(0.45).timeout
	if state_name != "start":
		var dialogue := (capture_root as CropAdaptiveWateringDemo).story_dialogue
		if dialogue.visible and dialogue.is_typing():
			dialogue.advance()
	await process_frame
	await RenderingServer.frame_post_draw
	var image := root.get_texture().get_image()
	var absolute_path := ProjectSettings.globalize_path(output_path)
	var result := image.save_png(absolute_path)
	if result != OK:
		push_error("无法保存作物适配浇水器验证截图：%s" % absolute_path)
		quit(1)
		return
	print("CROP_ADAPTIVE_CAPTURE_PASS: %s" % absolute_path)
	quit(0)
