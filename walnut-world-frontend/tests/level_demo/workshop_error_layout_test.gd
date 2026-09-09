extends SceneTree

const LEVEL := preload("res://scenes/level_demo/crop_adaptive_watering_demo.tscn")
var failures: Array[String] = []


func _initialize() -> void:
	Engine.set_meta("art_reduced_motion", true)
	root.size = Vector2i(1280, 720)
	var level := LEVEL.instantiate() as CropAdaptiveWateringDemo
	root.add_child(level)
	await process_frame
	level.story_dialogue.skip_sequence()
	level.call("_begin_workshop_experiments")
	level.story_dialogue.skip_sequence()
	var action_y := level.workshop_action_button.position.y
	var card := level.workshop_overlay.get_node("Card") as Control
	var card_height := card.size.y
	for step in [0, 1]:
		level.set("_workshop_step", step)
		level.call("_show_workshop_step")
		level.call("_on_workshop_action_pressed")
		for _frame in range(4):
			await process_frame
		var error := level.get_node("%WorkshopError") as Label
		var board := level.workshop_overlay.get_node("Card/Margin/Content/WorkshopCodePanel") as Control
		if not error.visible or error.get_global_rect().position.y < board.get_global_rect().end.y + 8:
			failures.append("步骤 %d 的错误提示必须位于代码板下方独立区域。" % step)
		if error.get_global_rect().end.y + 8 > level.workshop_action_button.get_global_rect().position.y:
			failures.append("步骤 %d 的错误提示不能与检查按钮重叠。" % step)
		if error.size.y < error.get_minimum_size().y or error.text.is_empty():
			failures.append("所有错误文字必须完整展示。")
		if not card.get_global_rect().encloses(level.workshop_action_button.get_global_rect()) or card.get_global_rect().end.y > 704:
			failures.append("反馈与按钮必须保持在面板和屏幕内。")
		if step == 1 and (not error.text.contains("严重缺水") or not error.text.contains("轻度缺水")):
			failures.append("第二步必须保留多字段错误。")
		for field in level.call("_workshop_inputs"):
			field.clear_validation()
		level.call("_refresh_workshop_errors")
		await process_frame
		await process_frame
		if error.visible or not is_equal_approx(level.workshop_action_button.position.y, action_y) or not is_equal_approx(card.size.y, card_height):
			failures.append("错误清除后应恢复原有面板和按钮位置。")
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("WORKSHOP_ERROR_LAYOUT_PASS")
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)
