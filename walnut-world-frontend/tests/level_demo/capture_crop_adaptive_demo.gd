extends SceneTree

const CROP_DEMO := preload("res://scenes/level_demo/crop_adaptive_watering_demo.tscn")
const START_SCREEN := preload("res://scenes/ui/game_start_screen.tscn")


func _initialize() -> void:
	root.size = Vector2i(1280, 720)
	var state_name := "start"
	var output_path := "res://docs/design/verification/crop-adaptive-start.png"
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with("--state="):
			state_name = argument.trim_prefix("--state=")
		elif argument.begins_with("--output="):
			output_path = argument.trim_prefix("--output=")
	var capture_root: CanvasItem
	if state_name == "start":
		capture_root = START_SCREEN.instantiate()
	else:
		capture_root = CROP_DEMO.instantiate()
	root.add_child(capture_root)
	await process_frame
	if state_name != "start":
		var level := capture_root as CropAdaptiveWateringDemo
		level.timing_scale = 0.05
		(level.get_node("StoryDialogueOverlay") as StoryDialogueOverlay).skip_sequence()
		if state_name == "manual":
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
		elif state_name == "unlocked":
			level.call("_show_skill_tree", true)
		elif state_name == "code":
			level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
			level.call("_show_code_drawer")
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
	var image := root.get_texture().get_image()
	var absolute_path := ProjectSettings.globalize_path(output_path)
	var result := image.save_png(absolute_path)
	if result != OK:
		push_error("无法保存作物适配浇水器验证截图：%s" % absolute_path)
		quit(1)
		return
	print("CROP_ADAPTIVE_CAPTURE_PASS: %s" % absolute_path)
	quit(0)
