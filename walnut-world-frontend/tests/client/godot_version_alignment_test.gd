extends SceneTree


func _initialize() -> void:
	var failures: Array[String] = []
	var project := ConfigFile.new()
	if project.load("res://project.godot") != OK:
		failures.append("project.godot 必须可读取。")
	else:
		var features: PackedStringArray = project.get_value("application", "config/features", PackedStringArray())
		if "4.7" not in features:
			failures.append("project.godot 必须把 Godot 兼容特征锁定为 4.7。")
	var selected_files := {
		"根 README": ProjectSettings.globalize_path("res://../README.md"),
		"前端 README": ProjectSettings.globalize_path("res://README.md"),
		"离线门禁": ProjectSettings.globalize_path("res://scripts/run-offline-tests.ps1"),
		"真实网关门禁": ProjectSettings.globalize_path("res://scripts/run-real-gateway-e2e.ps1"),
	}
	for label: String in selected_files:
		var text := FileAccess.get_file_as_string(selected_files[label])
		if text.is_empty():
			failures.append("%s 必须可读取。" % label)
		elif "4.5.2" in text or "Godot 4.7.1" not in text:
			failures.append("%s 必须只声明当前 Godot 4.7.1 口径。" % label)
	var frontend_readme := FileAccess.get_file_as_string("res://README.md")
	for boundary in ["world_cue_requested", "WorldPresentationEvent", "world_presentation_enabled=false"]:
		if boundary not in frontend_readme:
			failures.append("前端 README 必须区分本地角色 2D cue 与权威世界演出：缺少 %s。" % boundary)
	var offline_runner := FileAccess.get_file_as_string("res://scripts/run-offline-tests.ps1")
	var real_runner := FileAccess.get_file_as_string("res://scripts/run-real-gateway-e2e.ps1")
	if "^4\\.7\\.1\\.stable" not in offline_runner or "^4\\.7\\.1\\.stable" not in real_runner:
		failures.append("前端离线与真实网关门禁必须实际校验 Godot 4.7.1 stable。")
	var formal_scene := FileAccess.get_file_as_string("res://scenes/level_demo/crop_adaptive_watering_demo.tscn")
	if (
		"[node name=\"BugLegion2D\"" not in formal_scene
		or "[node name=\"AgentInteractionPresenter\"" not in formal_scene
	):
		failures.append("正式作物关卡必须预置本地角色 2D cue 组件。")
	if failures.is_empty():
		print("GODOT_VERSION_ALIGNMENT_TEST_PASS")
		quit(0)
		return
	for failure: String in failures:
		push_error(failure)
	quit(1)
