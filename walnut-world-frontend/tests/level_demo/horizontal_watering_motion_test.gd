extends SceneTree

const LEVEL_PATH := "res://scenes/level_demo/horizontal_watering_demo.tscn"


func _initialize() -> void:
	var failures: Array[String] = []
	var level := (load(LEVEL_PATH) as PackedScene).instantiate()
	level.demo_timing_scale = 0.2
	root.add_child(level)
	await process_frame
	await process_frame
	level.skip_story_dialogue()
	await process_frame
	var first_plot := level.get_node("ManualRow/Plot0") as WateringPlot
	var manual_can := level.get_node("ManualWateringCan") as AnimatedSprite3D
	if not level.manual_water_plot(0):
		failures.append("首块土地应能启动手动水壶动画。")
	var saw_manual_can := false
	for _step in range(30):
		if manual_can.visible:
			saw_manual_can = true
			break
		await create_timer(0.01).timeout
	if not saw_manual_can:
		failures.append("点击土地后必须出现手持水壶序列帧。")
	for _step in range(20):
		if first_plot.is_watered:
			break
		await create_timer(0.05).timeout
	if not first_plot.is_watered or not first_plot.get_node("Seedling").visible:
		failures.append("浇水结束后必须让幼苗从土地中出现。")
	level.set_preview_state("code")
	await process_frame
	level.set_fill_values("0", "5", "i")
	level.request_submit_and_run()
	await create_timer(0.10).timeout
	var spell_overlay := level.get_node("Hud/MagicSpellOverlay") as Control
	var shushu := level.get_node("Cast/ShuShu") as AnimatedSprite3D
	if not spell_overlay.visible or not shushu.visible or not shushu.is_playing():
		failures.append("自动阶段必须先由书书施放循环浇水魔法。")
	var magic_can := level.get_node("MagicWateringCan") as AnimatedSprite3D
	var saw_magic_can := false
	for _step in range(30):
		if magic_can.visible:
			saw_magic_can = true
			break
		await create_timer(0.05).timeout
	if not saw_magic_can:
		failures.append("魔法完成后必须由大水壶逐块执行，而非旧横向装置。")
	for _step in range(50):
		if level.get_phase_name() == "COMPLETED":
			break
		await create_timer(0.05).timeout
	if level.get_phase_name() != "COMPLETED":
		failures.append("大水壶遍历 0—4 号土地后必须完成关卡。")
	for plot in level.get_node("AutoRow").get_children():
		if not (plot as WateringPlot).is_watered:
			failures.append("自动行存在未浇土地：%s" % plot.name)
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("HORIZONTAL_WATERING_MOTION_TEST_PASS: 手动水壶、幼苗弹出、书书施法与大水壶遍历通过")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
