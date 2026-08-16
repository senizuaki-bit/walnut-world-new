extends SceneTree

const ViewportScene := preload("res://scenes/task/world_viewport.tscn")


func _initialize() -> void:
	var viewport := ViewportScene.instantiate()
	root.add_child(viewport)
	await process_frame
	await process_frame
	var event := {
		"event_type": "world.action.harvested", "event_version": 1,
		"action_index": 0, "action_count": 1,
		"payload": {
			"actor_entity_id": "student_avatar", "plot_id": "farm_plot_0001",
			"position": {"x": 1, "y": 2}, "crop_type": "carrot",
			"growth_stage": 3, "ready_to_harvest": true,
		},
	}
	var begun: Dictionary = viewport.begin_presentation_event(event, 2.0)
	var marker: Node = viewport.farm_world.get_node_or_null("HarvestPresentationMarker")
	if not begun.get("ok", false) or marker == null or not viewport.presentation_label.visible:
		return _fail("Formal WorldViewport did not expose a visible HARVEST presentation at 2x speed.")
	if not viewport.finish_presentation_event(event, false):
		return _fail("Formal WorldViewport did not finish the HARVEST presentation.")
	await process_frame
	if viewport.farm_world.get_node_or_null("HarvestPresentationMarker") != null:
		return _fail("HARVEST presentation marker leaked after completion.")
	if not viewport.project_replay_snapshot(_snapshot(1)) or not viewport.project_replay_snapshot(_snapshot(2)):
		return _fail("Formal WorldViewport could not reset/restore replay visuals from verified Snapshots.")

	# Local input/prediction may move only the preauthored renderer. It cannot
	# write ClientStore, and the next authoritative Snapshot must correct it.
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	var authoritative := _snapshot(2)
	if store == null or not store.replace_world(authoritative):
		return _fail("Could not establish authoritative World for prediction correction test.")
	var avatar := viewport.farm_world.get_node_or_null("Player") as Node3D
	var terrain := viewport.farm_world.get_node_or_null("TerrainManager") as TerrainManager
	if avatar == null or terrain == null:
		return _fail("Formal renderer lacks preauthored avatar/terrain authority targets.")
	var authority_before_prediction: Dictionary = store.world_snapshot.duplicate(true)
	avatar.global_position = Vector3(99.0, 0.0, 99.0)
	await process_frame
	if store.world_snapshot != authority_before_prediction:
		return _fail("Local avatar prediction overwrote ClientStore World authority.")
	viewport._on_world_replaced(authoritative)
	var expected_position := terrain.map_data.cell_to_world(Vector2i(1, 2))
	if avatar.global_position != expected_position or store.world_snapshot != authority_before_prediction:
		return _fail("Authoritative Snapshot did not correct the locally moved renderer without changing ClientStore.")

	# The formal composition must not mount the level demo or derive World
	# authority from source-code regex/pattern matching.
	var formal_sources := {
		"AppRoot scene": FileAccess.get_file_as_string("res://scenes/app/app_root.tscn"),
		"AppRoot script": FileAccess.get_file_as_string("res://scenes/app/app_root.gd"),
		"TaskWorkspace scene": FileAccess.get_file_as_string("res://scenes/task/task_workspace.tscn"),
		"WorldViewport": FileAccess.get_file_as_string("res://scenes/task/world_viewport.gd"),
	}
	for label in formal_sources:
		var source := str(formal_sources[label])
		for forbidden in ["level_demo", "horizontal_watering_demo", "RegEx.new", ".compile("]:
			if source.contains(forbidden):
				return _fail("%s mounts demo/source-pattern authority seam: %s" % [label, forbidden])
	print("WORLD_VIEWPORT_PRESENTATION_TEST_PASS")
	quit(0)


func _snapshot(revision: int) -> Dictionary:
	return {
		"world_id": "world_demo", "revision": revision,
		"last_event_sequence": revision, "state_schema_version": "1.0.0",
		"state_hash": ("1" if revision == 1 else "2").repeat(64),
		"world_rules_version": "rules_demo",
		"state": {
			"avatar": {"position": {"x": 1, "y": 2}},
			"plots": [{
				"position": {"x": 1, "y": 2}, "soil_state": "TILLED",
				"hydration": 100 if revision == 1 else 0,
			}],
		},
	}


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
