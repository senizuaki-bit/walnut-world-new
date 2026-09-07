extends SceneTree

const K := 720.0 / 941.0
var failures: Array[String] = []

func _initialize() -> void:
	var manifest: Dictionary = JSON.parse_string(FileAccess.get_file_as_string("res://assets/art/redesign/crop_adaptive/v2/guide-layout.json"))
	var home := (load("res://scenes/ui/game_start_screen.tscn") as PackedScene).instantiate()
	root.add_child(home)
	await create_timer(0.8).timeout
	_check_rect(home.get_node("HeroCard"), _rect(manifest, "S01", "ui-hero-frame"), "S01 hero")
	_check_rect(home.get_node("%EnterButton"), _rect(manifest, "S01", "button-normal"), "S01 enter")
	var home_surface := home.get_node("HeroCard/OriginalSurface") as NinePatchRect
	if not home_surface.texture.resource_path.ends_with("ui-hero-frame.png"):
		failures.append("Homepage must use the original, separately composited frame")
	home.queue_free()
	await process_frame
	var level := (load("res://scenes/level_demo/crop_adaptive_watering_demo.tscn") as PackedScene).instantiate() as CropAdaptiveWateringDemo
	root.add_child(level)
	await process_frame
	level.story_dialogue.skip_sequence()
	for item in [["SkillTreeOverlay", "E03"], ["WorkshopOverlay", "S04"], ["BugChallengeOverlay", "E04"], ["GrowthSummaryOverlay", "E06"]]:
		_check_rect(level.get_node(item[0] + "/Card"), _rect(manifest, item[1], "ui-panel"), item[1])
	_check_rect(level.get_node("CodeDrawer/Surface"), _rect(manifest, "S07", "ui-panel"), "S07 drawer")
	var first := level.plot_grid.get_child(0) as CropPlotCard
	first.set_attention(true)
	if not first.attention_frame.material is ShaderMaterial or first.attention_frame.texture != first.soil_art.texture:
		failures.append("Selection must follow the actual soil alpha, not a rectangular overlay")
	first.show_candidate_action(250, 250, false)
	if not is_equal_approx(first.moisture_bar.value, 2.5) or first.current_moisture != 20:
		failures.append("Candidate hydration uses the existing 0–10000 scale and must not modify world values")
	first.show_candidate_outcome(250, "CORRECT")
	if not first.crop_art.texture.resource_path.ends_with("crop-carrot-target-met.png"):
		failures.append("Candidate crop appearance must follow evaluator status")
	level.call("_begin_workshop_experiments")
	level.story_dialogue.skip_sequence()
	level.gap_target_input.text = "wrong"
	level.call("_on_workshop_action_pressed")
	if not bool(level.gap_target_input.get_meta("invalid", false)) or level.gap_target_input.text != "wrong":
		failures.append("Invalid inputs must keep the student's value and show an error skin")
	level.gap_target_input.text = "target"
	level.gap_target_input.text_changed.emit("target")
	if bool(level.gap_target_input.get_meta("invalid", false)):
		failures.append("Editing must clear the field's error state")
	for button in [level.get_node("%ClosePatchButton"), level.get_node("%RejectPatchButton"), level.get_node("%AcceptPatchButton")]:
		if button.owner != level:
			failures.append("Patch actions must be authored scene nodes")
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("V2_PROTOTYPE_CONTRACT_TEST_PASS: source geometry, original surfaces, soil silhouette, native inputs and candidate units")
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)

func _rect(manifest: Dictionary, view: String, asset: String) -> Rect2:
	for page in manifest.pages:
		if page.view == view:
			for item in page.assets:
				if item.id == asset:
					return Rect2(Vector2(item.rect[0], item.rect[1]) * K, Vector2(item.rect[2], item.rect[3]) * K)
	push_error("Missing source rectangle: %s/%s" % [view, asset])
	return Rect2()

func _check_rect(control: Control, expected: Rect2, label: String) -> void:
	var actual := control.get_global_rect()
	if actual.position.distance_to(expected.position) > 2.0 or actual.size.distance_to(expected.size) > 2.0:
		failures.append("%s drifted from guide-layout.json: %s / %s" % [label, actual, expected])
