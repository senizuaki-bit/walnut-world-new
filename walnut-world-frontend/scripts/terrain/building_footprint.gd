class_name BuildingFootprint
extends Node

@export_group("Grid Footprint")
@export var footprint_size: Vector2i = Vector2i.ONE
@export var anchor_offset: Vector2i = Vector2i.ZERO
@export var placement_offset: Vector3 = Vector3.ZERO

@export_group("Placement Rules")
@export var occupies_cells: bool = true
@export var allow_on_farmland: bool = false
@export var persistent: bool = true

func is_valid() -> bool:
	return footprint_size.x > 0 and footprint_size.y > 0

func get_occupied_cells(anchor_cell: Vector2i) -> Array[Vector2i]:
	var cells: Array[Vector2i] = []
	if not occupies_cells or not is_valid():
		return cells
	var first_cell: Vector2i = anchor_cell + anchor_offset
	for row in footprint_size.y:
		for column in footprint_size.x:
			cells.append(first_cell + Vector2i(column, row))
	return cells
