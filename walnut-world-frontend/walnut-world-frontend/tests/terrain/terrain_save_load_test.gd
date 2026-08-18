extends SceneTree

const TERRAIN_MANAGER_SCENE: PackedScene = preload("res://scenes/terrain/terrain_manager.tscn")
const GARDEN_HOUSE_SCENE: PackedScene = preload("res://scenes/environment/garden_house.tscn")
const DIRT: int = 1
const STONE: int = 4
const TEST_SLOT: int = 7931
const INVALID_SLOT: int = 7932
const MISSING_SLOT: int = 7933

var _failures: Array[String] = []

func _initialize() -> void:
	var source: Variant = TERRAIN_MANAGER_SCENE.instantiate()
	if not source.has_method("save_to_slot") or not source.has_method("load_from_slot") or not source.has_method("place_building") or not source.has_method("get_save_path"):
		source.free()
		push_error("TerrainManager save/load and placement APIs are missing")
		quit(1)
		return
	root.add_child(source)
	await process_frame
	source.delete_save(TEST_SLOT)
	source.delete_save(INVALID_SLOT)
	source.set_terrain_cell(Vector2i(2, 3), DIRT)
	var placed_building: Variant = source.place_building(GARDEN_HOUSE_SCENE, Vector2i(12, 8), 1.25, &"save_house")
	_expect(placed_building != null, "persistent building is placed")
	placed_building.scale = Vector3(1.4, 1.2, 1.4)
	_expect(source.save_to_slot(TEST_SLOT), "save succeeds")

	var target: Variant = TERRAIN_MANAGER_SCENE.instantiate()
	root.add_child(target)
	await process_frame
	target.set_terrain_cell(Vector2i(0, 0), STONE)
	_expect(target.load_from_slot(TEST_SLOT), "load succeeds")
	_expect(target.map_data.get_cell(Vector2i(2, 3)) == DIRT, "terrain cell round trips")
	_expect(target.get_building_at_cell(Vector2i(11, 7)) == &"save_house", "building footprint round trips")
	var loaded_building := target.get_node_or_null("Buildings/save_house") as Node3D
	_expect(loaded_building != null, "building scene is reconstructed")
	if loaded_building != null:
		_expect(is_equal_approx(loaded_building.rotation.y, 1.25), "building rotation round trips")
		_expect(loaded_building.scale.is_equal_approx(Vector3(1.4, 1.2, 1.4)), "building scale round trips")
	_expect(not target.load_from_slot(MISSING_SLOT), "missing save is safe")

	var invalid_file := FileAccess.open(target.get_save_path(INVALID_SLOT), FileAccess.WRITE)
	invalid_file.store_string("this is not JSON")
	invalid_file.close()
	target.set_terrain_cell(Vector2i(0, 0), STONE)
	_expect(not target.load_from_slot(INVALID_SLOT), "invalid save is rejected")
	_expect(target.map_data.get_cell(Vector2i(0, 0)) == STONE, "invalid load leaves current terrain unchanged")

	source.delete_save(TEST_SLOT)
	source.delete_save(INVALID_SLOT)
	source.queue_free()
	target.queue_free()
	await process_frame
	_finish()

func _expect(condition: bool, message: String) -> void:
	if not condition:
		_failures.append(message)

func _finish() -> void:
	if _failures.is_empty():
		print("TERRAIN_SAVE_LOAD_TEST_PASS")
		quit(0)
		return
	for failure in _failures:
		push_error(failure)
	quit(1)
