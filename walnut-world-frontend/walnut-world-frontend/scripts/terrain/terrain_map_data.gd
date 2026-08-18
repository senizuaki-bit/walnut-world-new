class_name TerrainMapData
extends RefCounted

enum CellType {
	GRASS,
	DIRT,
	FARMLAND,
	PATH,
	STONE,
}

var width: int = 0
var height: int = 0
var cell_size: float = 1.0

var _cells: PackedByteArray = PackedByteArray()

func configure(new_width: int, new_height: int, new_cell_size: float) -> bool:
	if new_width <= 0 or new_height <= 0 or new_cell_size <= 0.0:
		return false
	width = new_width
	height = new_height
	cell_size = new_cell_size
	_cells.resize(width * height)
	_cells.fill(CellType.GRASS)
	return true

func is_inside_map(cell: Vector2i) -> bool:
	return cell.x >= 0 and cell.x < width and cell.y >= 0 and cell.y < height

func get_cell(cell: Vector2i) -> int:
	if not is_inside_map(cell):
		return CellType.GRASS
	return _cells[_to_index(cell)]

func set_cell(cell: Vector2i, cell_type: int) -> bool:
	if not is_inside_map(cell) or not _is_valid_cell_type(cell_type):
		return false
	_cells[_to_index(cell)] = cell_type
	return true

func world_to_cell(world_position: Vector3) -> Vector2i:
	var half_width: float = float(width) * cell_size * 0.5
	var half_height: float = float(height) * cell_size * 0.5
	return Vector2i(
		floori((world_position.x + half_width) / cell_size),
		floori((world_position.z + half_height) / cell_size),
	)

func cell_to_world(cell: Vector2i) -> Vector3:
	if not is_inside_map(cell):
		return Vector3.ZERO
	var half_width: float = float(width) * cell_size * 0.5
	var half_height: float = float(height) * cell_size * 0.5
	return Vector3(
		(float(cell.x) + 0.5) * cell_size - half_width,
		0.0,
		(float(cell.y) + 0.5) * cell_size - half_height,
	)

func get_cells_copy() -> PackedByteArray:
	return _cells.duplicate()

func replace_cells(values: PackedByteArray) -> bool:
	if values.size() != width * height:
		return false
	for value in values:
		if not _is_valid_cell_type(value):
			return false
	_cells = values.duplicate()
	return true

func _to_index(cell: Vector2i) -> int:
	return cell.y * width + cell.x

func _is_valid_cell_type(cell_type: int) -> bool:
	return cell_type >= CellType.GRASS and cell_type <= CellType.STONE
