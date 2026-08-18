extends SceneTree

const BUILDING_FOOTPRINT_SCRIPT: Script = preload("res://scripts/terrain/building_footprint.gd")
const TERRAIN_MANAGER_SCRIPT: Script = preload("res://scripts/terrain/terrain_manager.gd")
const FARMLAND: int = 2

var _failures: Array[String] = []

func _initialize() -> void:
	var manager: Variant = TERRAIN_MANAGER_SCRIPT.new()
	_expect(manager.configure_map(20, 20, 1.0), "test map configures")
	var footprint: Variant = BUILDING_FOOTPRINT_SCRIPT.new()
	footprint.footprint_size = Vector2i(2, 2)
	_expect(manager.can_place_building(footprint, Vector2i(3, 3)), "empty 2x2 footprint can be placed")
	_expect(manager.register_building(&"barn", footprint, Vector2i(3, 3)), "registration succeeds")
	_expect(manager.get_building_at_cell(Vector2i(3, 3)) == &"barn", "anchor cell is occupied")
	_expect(manager.get_building_at_cell(Vector2i(4, 4)) == &"barn", "whole footprint is occupied")
	_expect(not manager.can_place_building(footprint, Vector2i(4, 4)), "overlap is rejected")
	_expect(not manager.can_place_building(footprint, Vector2i(19, 19)), "out of bounds is rejected")
	manager.map_data.set_cell(Vector2i(8, 8), FARMLAND)
	_expect(not manager.can_place_building(footprint, Vector2i(8, 8)), "farmland is blocked by default")
	footprint.allow_on_farmland = true
	_expect(manager.can_place_building(footprint, Vector2i(8, 8)), "explicit farmland permission is honored")
	_expect(manager.unregister_building(&"barn"), "unregistration succeeds")
	_expect(manager.get_building_at_cell(Vector2i(3, 3)) == &"", "unregistration releases all cells")
	manager.free()
	footprint.free()
	_finish()

func _expect(condition: bool, message: String) -> void:
	if not condition:
		_failures.append(message)

func _finish() -> void:
	if _failures.is_empty():
		print("BUILDING_OCCUPANCY_TEST_PASS")
		quit(0)
		return
	for failure in _failures:
		push_error(failure)
	quit(1)
