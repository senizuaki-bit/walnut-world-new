extends SceneTree

const TERRAIN_MANAGER_SCRIPT: Script = preload("res://scripts/terrain/terrain_manager.gd")
const DIRT: int = 1

var _failures: Array[String] = []

func _initialize() -> void:
	var manager: Variant = TERRAIN_MANAGER_SCRIPT.new()
	manager.chunk_size = 10
	_expect(manager.configure_map(20, 20, 1.0), "test map configures")
	_expect(manager.set_terrain_cell(Vector2i(9, 4), DIRT), "left border cell changes")
	_expect(manager.set_terrain_cell(Vector2i(10, 4), DIRT), "right border cell changes")
	_expect(manager.get_autotile_mask(Vector2i(9, 4)) == 2, "east neighbor sets bit 2")
	_expect(manager.get_autotile_mask(Vector2i(10, 4)) == 8, "west neighbor sets bit 8")
	var dirty_chunks: Array[Vector2i] = manager.get_dirty_chunk_coords()
	_expect(dirty_chunks.has(Vector2i(0, 0)), "left chunk is dirty")
	_expect(dirty_chunks.has(Vector2i(1, 0)), "right chunk is dirty across chunk boundary")
	manager.free()
	_finish()

func _expect(condition: bool, message: String) -> void:
	if not condition:
		_failures.append(message)

func _finish() -> void:
	if _failures.is_empty():
		print("TERRAIN_MANAGER_TEST_PASS")
		quit(0)
		return
	for failure in _failures:
		push_error(failure)
	quit(1)
