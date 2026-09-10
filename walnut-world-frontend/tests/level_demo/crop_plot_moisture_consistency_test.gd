extends SceneTree

const LEVEL := preload("res://scenes/level_demo/crop_adaptive_watering_demo.tscn")
const STATES := ["severe-dry", "light-thirst", "light-thirst", "waterlogged", "target-met", "severe-dry", "target-met", "light-thirst"]
const CROP_IDS := ["carrot", "tomato", "potato", "corn", "carrot", "tomato", "potato", "corn"]
var failures: Array[String] = []


func _initialize() -> void:
	Engine.set_meta("art_reduced_motion", true)
	root.size = Vector2i(1280, 720)
	var level := LEVEL.instantiate() as CropAdaptiveWateringDemo
	level.timing_scale = 0.01
	root.add_child(level)
	await process_frame
	(level.get_node("StoryDialogueOverlay") as StoryDialogueOverlay).skip_sequence()
	_check_cards(level, "初始状态")
	await level._play_old_tool_demo()
	_check_cards(level, "旧工具演示")
	var old_units := [2, 0, 1, 0, 0, 1, 1, 1]
	for index in range(8):
		var card := level.plot_grid.get_child(index) as CropPlotCard
		var expected_badge := "跳过" if old_units[index] == 0 else "%d份 · %d ml" % [old_units[index], old_units[index] * 250]
		if card.water_badge.text != expected_badge or card._error_active != (index in [1, 5, 6]):
			failures.append("旧工具%d号土地必须保留水量和错误反馈" % index)
	level._begin_manual_compare()
	_check_cards(level, "进入手动比较")
	for index in [1, 6, 5]:
		level._on_plot_pressed(index)
		level._choose_manual_water(2 if index == 6 else 0)
		_check_cards(level, "手动选择错误水量")
		level._choose_manual_water(CropAdaptiveWateringDemo.EXPECTED_UNITS[index])
		_check_cards(level, "手动选择正确水量")
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("CROP_PLOT_MOISTURE_CONSISTENCY_TEST_PASS: 八块土地的贴图、湿度与水量反馈在旧工具和手动比较阶段保持一致")
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)


func _check_cards(level: CropAdaptiveWateringDemo, stage: String) -> void:
	for index in range(8):
		var card := level.plot_grid.get_child(index) as CropPlotCard
		var state: String = STATES[index]
		if (card.crop_art as ArtMotionTexture).motion_id != "crop-%s-%s-sway" % [CROP_IDS[index], state]:
			failures.append("%s：%d号作物贴图与当前湿度不一致" % [stage, index])
		if state in ["target-met", "waterlogged"]:
			if card.soil_art.motion_id != "soil-%s-ambient" % state:
				failures.append("%s：%d号土壤动画与当前湿度不一致" % [stage, index])
		elif not card.soil_art.motion_id.is_empty() or card.soil_art.texture.resource_path != "res://assets/art/redesign/crop_adaptive/v2/components/soil-%s.png" % state:
			failures.append("%s：%d号土壤贴图与当前湿度不一致" % [stage, index])
		if card.current_moisture != CropAdaptiveWateringDemo.MOISTURE[index] or card.current_label.text != "当前湿度 %d" % CropAdaptiveWateringDemo.MOISTURE[index]:
			failures.append("%s：%d号水量判断不得修改当前湿度" % [stage, index])
		if card.soil_glow.visible and not card.soil_glow.motion_id.begins_with("soil-%s-" % state):
			failures.append("%s：%d号高亮土壤与当前湿度不一致" % [stage, index])
