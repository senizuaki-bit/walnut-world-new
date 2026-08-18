extends SceneTree

const TERRAIN_MAP_DATA_SCRIPT: Script = preload("res://scripts/terrain/terrain_map_data.gd")

var _failures: Array[String] = []

func _initialize() -> void:
	var map: Variant = TERRAIN_MAP_DATA_SCRIPT.new()
	_expect(map.configure(20, 20, 1.0), "20x20 map configures")
	_expect(map.width == 20 and map.height == 20, "configured dimensions are retained")
	_expect(map.get_cell(Vector2i(0, 0)) == map.CellType.GRASS, "new cells default to grass")
	_expect(map.set_cell(Vector2i(5, 7), map.CellType.DIRT), "inside write succeeds")
	_expect(map.get_cell(Vector2i(5, 7)) == map.CellType.DIRT, "inside write is readable")
	_expect(not map.set_cell(Vector2i(-1, 0), map.CellType.PATH), "negative write is rejected")
	_expect(not map.set_cell(Vector2i(20, 0), map.CellType.PATH), "right edge write is rejected")
	_expect(map.get_cell(Vector2i(20, 0)) == map.CellType.GRASS, "outside read is safe grass")
	_expect(map.cell_to_world(Vector2i(0, 0)).is_equal_approx(Vector3(-9.5, 0.0, -9.5)), "origin cell center")
	_expect(map.cell_to_world(Vector2i(19, 19)).is_equal_approx(Vector3(9.5, 0.0, 9.5)), "last cell center")
	_expect(map.world_to_cell(Vector3(-10.0, 0.0, -10.0)) == Vector2i(0, 0), "minimum map corner maps inside")
	_expect(map.world_to_cell(Vector3(9.99, 0.0, 9.99)) == Vector2i(19, 19), "maximum interior point maps to last cell")
	_expect(map.world_to_cell(Vector3(10.0, 0.0, 0.0)) == Vector2i(20, 10), "maximum boundary maps outside")
	_expect(not map.configure(0, 20, 1.0), "zero width is rejected")
	_finish()

func _expect(condition: bool, message: String) -> void:
	if not condition:
		_failures.append(message)

func _finish() -> void:
	if _failures.is_empty():
		print("TERRAIN_MAP_DATA_TEST_PASS")
		quit(0)
		return
	for failure in _failures:
		push_error(failure)
	quit(1)
