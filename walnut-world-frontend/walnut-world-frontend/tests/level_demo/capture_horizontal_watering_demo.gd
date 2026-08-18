extends SceneTree

const LEVEL := preload("res://scenes/level_demo/horizontal_watering_demo.tscn")

func _initialize() -> void:
	root.size = Vector2i(1280, 720)
	var state_name := "manual"
	var output_path := "res://docs/design/verification/horizontal-watering-demo-manual.png"
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with("--state="):
			state_name = argument.trim_prefix("--state=")
		elif argument.begins_with("--output="):
			output_path = argument.trim_prefix("--output=")
	var level := LEVEL.instantiate()
	level.demo_timing_scale = 1.0 if state_name in ["magic", "watering"] else 0.0
	root.add_child(level)
	await process_frame
	if state_name == "magic":
		level.set_preview_state("code")
		await process_frame
		level.set_fill_values("0", "5", "i")
		level.request_submit_and_run()
	elif state_name == "watering":
		level.skip_story_dialogue()
		await process_frame
		level.manual_water_plot(0)
		await create_timer(0.30).timeout
	elif state_name == "dialogue_layout":
		(level.get_node("Hud/StoryDialogueOverlay") as StoryDialogueOverlay).advance()
	elif state_name != "manual":
		level.set_preview_state(state_name)
	for _frame in range(8):
		await process_frame
	var image := root.get_texture().get_image()
	var absolute_path := ProjectSettings.globalize_path(output_path)
	var result := image.save_png(absolute_path)
	if result != OK:
		push_error("无法保存 Demo 验证截图：%s" % absolute_path)
		quit(1)
		return
	print("HORIZONTAL_WATERING_CAPTURE_PASS: %s" % absolute_path)
	quit(0)
