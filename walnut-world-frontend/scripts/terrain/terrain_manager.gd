class_name TerrainManager
extends Node3D

const SAVE_VERSION: int = 1
const SAVE_DIRECTORY: String = "user://saves"

@export_group("Map")
@export_range(1, 100, 1) var map_width: int = 20
@export_range(1, 100, 1) var map_height: int = 20
@export_range(0.1, 10.0, 0.1) var cell_size: float = 1.0
@export_range(1, 50, 1) var chunk_size: int = 10
@export_range(0.05, 1.0, 0.01) var ground_thickness: float = 0.2
@export_range(0.1, 2.0, 0.05) var boundary_thickness: float = 0.3
@export_range(0.5, 5.0, 0.1) var boundary_height: float = 2.0
@export var terrain_materials: Array[Material] = []

var map_data: TerrainMapData = TerrainMapData.new()

var _cell_occupants: Dictionary[Vector2i, StringName] = {}
var _building_cells: Dictionary[StringName, Array] = {}
var _building_anchors: Dictionary[StringName, Vector2i] = {}
var _building_nodes: Dictionary[StringName, Node3D] = {}
var _dirty_chunks: Dictionary[Vector2i, bool] = {}
var _chunk_nodes: Dictionary[Vector2i, MeshInstance3D] = {}
var _rebuild_scheduled: bool = false

@onready var _base_ground: MeshInstance3D = $BaseGround
@onready var _ground_collision: CollisionShape3D = $GroundBody/CollisionShape3D
@onready var _chunks: Node3D = $Chunks
@onready var _buildings: Node3D = $Buildings
@onready var _left_wall: StaticBody3D = $WorldBounds/LeftWall
@onready var _right_wall: StaticBody3D = $WorldBounds/RightWall
@onready var _top_wall: StaticBody3D = $WorldBounds/TopWall
@onready var _bottom_wall: StaticBody3D = $WorldBounds/BottomWall

func _ready() -> void:
	configure_map(map_width, map_height, cell_size)

func configure_map(new_width: int, new_height: int, new_cell_size: float) -> bool:
	if not map_data.configure(new_width, new_height, new_cell_size):
		return false
	map_width = new_width
	map_height = new_height
	cell_size = new_cell_size
	clear_registered_buildings()
	if is_inside_tree():
		_clear_chunk_nodes()
		_configure_scene_geometry()
		_mark_all_chunks_dirty()
		_register_preplaced_buildings()
	return true

func set_terrain_cell(cell: Vector2i, cell_type: int) -> bool:
	if map_data.get_cell(cell) == cell_type:
		return false
	if not map_data.set_cell(cell, cell_type):
		return false
	_mark_chunks_affected_by_cell(cell)
	return true

func get_autotile_mask(cell: Vector2i) -> int:
	if not map_data.is_inside_map(cell):
		return 0
	var cell_type: int = map_data.get_cell(cell)
	if cell_type == TerrainMapData.CellType.GRASS:
		return 0
	var mask: int = 0
	if map_data.get_cell(cell + Vector2i.UP) == cell_type:
		mask |= 1
	if map_data.get_cell(cell + Vector2i.RIGHT) == cell_type:
		mask |= 2
	if map_data.get_cell(cell + Vector2i.DOWN) == cell_type:
		mask |= 4
	if map_data.get_cell(cell + Vector2i.LEFT) == cell_type:
		mask |= 8
	return mask

func get_dirty_chunk_coords() -> Array[Vector2i]:
	var chunk_coords: Array[Vector2i] = []
	for chunk_coord in _dirty_chunks:
		chunk_coords.append(chunk_coord)
	return chunk_coords

func flush_dirty_chunks() -> void:
	if _dirty_chunks.is_empty():
		return
	var chunk_coords: Array[Vector2i] = get_dirty_chunk_coords()
	_dirty_chunks.clear()
	_rebuild_scheduled = false
	for chunk_coord in chunk_coords:
		_rebuild_chunk(chunk_coord)

func can_place_building(footprint: BuildingFootprint, anchor_cell: Vector2i) -> bool:
	if footprint == null or not footprint.is_valid() or not map_data.is_inside_map(anchor_cell):
		return false
	if not footprint.occupies_cells:
		return true
	for cell in footprint.get_occupied_cells(anchor_cell):
		if not map_data.is_inside_map(cell):
			return false
		if _cell_occupants.has(cell):
			return false
		if map_data.get_cell(cell) == TerrainMapData.CellType.FARMLAND and not footprint.allow_on_farmland:
			return false
	return true

func register_building(building_id: StringName, footprint: BuildingFootprint, anchor_cell: Vector2i) -> bool:
	if building_id.is_empty() or _building_cells.has(building_id):
		return false
	if not can_place_building(footprint, anchor_cell):
		return false
	var occupied_cells: Array[Vector2i] = footprint.get_occupied_cells(anchor_cell)
	for cell in occupied_cells:
		_cell_occupants[cell] = building_id
	_building_cells[building_id] = occupied_cells
	_building_anchors[building_id] = anchor_cell
	return true

func unregister_building(building_id: StringName) -> bool:
	if not _building_cells.has(building_id):
		return false
	var occupied_cells: Array = _building_cells[building_id]
	for cell_variant in occupied_cells:
		var cell: Vector2i = cell_variant as Vector2i
		_cell_occupants.erase(cell)
	_building_cells.erase(building_id)
	_building_anchors.erase(building_id)
	_building_nodes.erase(building_id)
	return true

func get_building_at_cell(cell: Vector2i) -> StringName:
	if not _cell_occupants.has(cell):
		return &""
	return _cell_occupants[cell]

func get_building_anchor(building_id: StringName) -> Vector2i:
	if not _building_anchors.has(building_id):
		return Vector2i(-1, -1)
	return _building_anchors[building_id]

func clear_registered_buildings() -> void:
	_cell_occupants.clear()
	_building_cells.clear()
	_building_anchors.clear()
	_building_nodes.clear()

func _register_preplaced_buildings() -> void:
	for building_node in _buildings.get_children():
		var footprint := _find_footprint(building_node)
		if footprint == null or not footprint.occupies_cells:
			continue
		var anchor_cell: Vector2i = map_data.world_to_cell(building_node.position)
		if not register_building(building_node.name, footprint, anchor_cell):
			push_warning("Unable to register preplaced building: %s" % building_node.name)
			continue
		_building_nodes[building_node.name] = building_node

func place_building(scene: PackedScene, anchor_cell: Vector2i, rotation_y: float = 0.0, building_id: StringName = &"") -> Node3D:
	if scene == null:
		return null
	var building_node: Node3D = scene.instantiate() as Node3D
	if building_node == null:
		return null
	var resolved_id: StringName = building_id
	if resolved_id.is_empty():
		resolved_id = StringName("building_%d" % _building_nodes.size())
	return _place_building_instance(building_node, anchor_cell, rotation_y, resolved_id)

func _place_building_instance(building_node: Node3D, anchor_cell: Vector2i, rotation_y: float, building_id: StringName) -> Node3D:
	var footprint := _find_footprint(building_node)
	if footprint == null or not can_place_building(footprint, anchor_cell):
		building_node.queue_free()
		return null
	building_node.name = building_id
	building_node.position = map_data.cell_to_world(anchor_cell) + footprint.placement_offset
	building_node.rotation.y = rotation_y
	_buildings.add_child(building_node)
	if not register_building(building_id, footprint, anchor_cell):
		_buildings.remove_child(building_node)
		building_node.queue_free()
		return null
	_building_nodes[building_id] = building_node
	return building_node

func get_save_path(slot: int) -> String:
	return "%s/terrain_slot_%d.json" % [SAVE_DIRECTORY, slot]

func has_save_slot(slot: int) -> bool:
	return slot >= 0 and FileAccess.file_exists(get_save_path(slot))

func delete_save(slot: int) -> bool:
	if slot < 0 or not has_save_slot(slot):
		return slot >= 0
	return DirAccess.remove_absolute(ProjectSettings.globalize_path(get_save_path(slot))) == OK

func save_to_slot(slot: int) -> bool:
	if slot < 0:
		return false
	var directory_error: Error = DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(SAVE_DIRECTORY))
	if directory_error != OK:
		push_error("Unable to create terrain save directory")
		return false
	var save_file := FileAccess.open(get_save_path(slot), FileAccess.WRITE)
	if save_file == null:
		push_error("Unable to open terrain save file")
		return false
	var cell_values: Array[int] = []
	for cell_value in map_data.get_cells_copy():
		cell_values.append(cell_value)
	var save_data := {
		"version": SAVE_VERSION,
		"terrain": {
			"width": map_data.width,
			"height": map_data.height,
			"cell_size": map_data.cell_size,
			"cells": cell_values,
		},
		"buildings": _serialize_persistent_buildings(),
	}
	save_file.store_string(JSON.stringify(save_data, "\t"))
	var write_error: Error = save_file.get_error()
	save_file.close()
	if write_error != OK:
		push_error("Unable to write terrain save file")
		return false
	return true

func load_from_slot(slot: int) -> bool:
	if slot < 0 or not has_save_slot(slot):
		return false
	var save_file := FileAccess.open(get_save_path(slot), FileAccess.READ)
	if save_file == null:
		return false
	var json := JSON.new()
	var parse_error: Error = json.parse(save_file.get_as_text())
	save_file.close()
	if parse_error != OK:
		return false
	var parsed_data: Variant = json.data
	if not parsed_data is Dictionary:
		return false
	var save_data: Dictionary = parsed_data
	if int(save_data.get("version", -1)) != SAVE_VERSION:
		return false
	var candidate_map := _parse_saved_map(save_data.get("terrain"))
	if candidate_map == null:
		return false
	var raw_buildings: Variant = save_data.get("buildings")
	if not raw_buildings is Array:
		return false
	var raw_building_array: Array = raw_buildings
	var building_entries: Array[Dictionary] = _parse_building_entries(raw_building_array)
	if building_entries.size() != raw_building_array.size():
		return false
	if not _validate_building_entries(building_entries, candidate_map):
		return false
	_apply_loaded_state(candidate_map, building_entries)
	return true

func _serialize_persistent_buildings() -> Array[Dictionary]:
	var saved_buildings: Array[Dictionary] = []
	for building_id in _building_nodes:
		var building_node: Node3D = _building_nodes[building_id]
		var footprint := _find_footprint(building_node)
		if footprint == null or not footprint.persistent or building_node.scene_file_path.is_empty():
			continue
		var anchor: Vector2i = get_building_anchor(building_id)
		saved_buildings.append({
			"building_id": String(building_id),
			"scene_path": building_node.scene_file_path,
			"anchor_cell": {"x": anchor.x, "y": anchor.y},
			"rotation_y": building_node.rotation.y,
			"scale": _serialize_vector3(building_node.scale),
			"footprint": _serialize_footprint(footprint),
		})
	return saved_buildings

func _serialize_footprint(footprint: BuildingFootprint) -> Dictionary:
	return {
		"size": {"x": footprint.footprint_size.x, "y": footprint.footprint_size.y},
		"offset": {"x": footprint.anchor_offset.x, "y": footprint.anchor_offset.y},
		"placement_offset": {"x": footprint.placement_offset.x, "y": footprint.placement_offset.y, "z": footprint.placement_offset.z},
		"allow_on_farmland": footprint.allow_on_farmland,
	}

func _serialize_vector3(value: Vector3) -> Dictionary:
	return {"x": value.x, "y": value.y, "z": value.z}

func _parse_saved_map(raw_terrain: Variant) -> TerrainMapData:
	if not raw_terrain is Dictionary:
		return null
	var terrain_data: Dictionary = raw_terrain
	var raw_width: Variant = terrain_data.get("width")
	var raw_height: Variant = terrain_data.get("height")
	var raw_cell_size: Variant = terrain_data.get("cell_size")
	var raw_cells: Variant = terrain_data.get("cells")
	if not _is_integer_number(raw_width) or not _is_integer_number(raw_height) or not _is_number(raw_cell_size) or not raw_cells is Array:
		return null
	var width: int = int(raw_width)
	var height: int = int(raw_height)
	var candidate_map := TerrainMapData.new()
	if width <= 0 or width > 100 or height <= 0 or height > 100 or not candidate_map.configure(width, height, float(raw_cell_size)):
		return null
	var raw_cell_array: Array = raw_cells
	if raw_cell_array.size() != width * height:
		return null
	var cells := PackedByteArray()
	cells.resize(raw_cell_array.size())
	for index in raw_cell_array.size():
		var raw_cell_value: Variant = raw_cell_array[index]
		if not _is_integer_number(raw_cell_value):
			return null
		var cell_value: int = int(raw_cell_value)
		if cell_value < TerrainMapData.CellType.GRASS or cell_value > TerrainMapData.CellType.STONE:
			return null
		cells[index] = cell_value
	if not candidate_map.replace_cells(cells):
		return null
	return candidate_map

func _parse_building_entries(raw_entries: Array) -> Array[Dictionary]:
	var parsed_entries: Array[Dictionary] = []
	for raw_entry in raw_entries:
		if not raw_entry is Dictionary:
			return []
		var entry: Dictionary = raw_entry
		var raw_id: Variant = entry.get("building_id")
		var raw_scene_path: Variant = entry.get("scene_path")
		var raw_anchor: Variant = entry.get("anchor_cell")
		var raw_rotation: Variant = entry.get("rotation_y")
		var raw_scale: Variant = entry.get("scale")
		var raw_footprint: Variant = entry.get("footprint")
		if not raw_id is String or String(raw_id).is_empty() or not raw_scene_path is String or String(raw_scene_path).is_empty() or not raw_anchor is Dictionary or not _is_number(raw_rotation) or not _is_valid_vector3_data(raw_scale) or not raw_footprint is Dictionary:
			return []
		var anchor: Vector2i = _parse_vector2i(raw_anchor)
		if anchor.x == -1 and anchor.y == -1:
			return []
		var footprint: Dictionary = raw_footprint
		if not _is_valid_footprint_data(footprint):
			return []
		var scale: Vector3 = _parse_vector3(raw_scale)
		if is_zero_approx(scale.x) or is_zero_approx(scale.y) or is_zero_approx(scale.z):
			return []
		parsed_entries.append({
			"building_id": StringName(String(raw_id)),
			"scene_path": String(raw_scene_path),
			"anchor_cell": anchor,
			"rotation_y": float(raw_rotation),
			"scale": scale,
			"footprint": footprint,
		})
	return parsed_entries

func _validate_building_entries(entries: Array[Dictionary], candidate_map: TerrainMapData) -> bool:
	var occupied_cells: Dictionary[Vector2i, bool] = {}
	var building_ids: Dictionary[StringName, bool] = {}
	for entry in entries:
		var building_id: StringName = entry["building_id"]
		if building_ids.has(building_id):
			return false
		building_ids[building_id] = true
		var scene_path: String = entry["scene_path"]
		var scene: PackedScene = ResourceLoader.load(scene_path) as PackedScene
		if scene == null:
			return false
		var validation_node: Node3D = scene.instantiate() as Node3D
		if validation_node == null:
			return false
		var footprint := _find_footprint(validation_node)
		if footprint == null:
			validation_node.free()
			return false
		_apply_footprint_data(footprint, entry["footprint"])
		var anchor_cell: Vector2i = entry["anchor_cell"]
		var is_valid: bool = footprint.occupies_cells and footprint.persistent and candidate_map.is_inside_map(anchor_cell)
		for cell in footprint.get_occupied_cells(anchor_cell):
			if not candidate_map.is_inside_map(cell) or occupied_cells.has(cell) or (candidate_map.get_cell(cell) == TerrainMapData.CellType.FARMLAND and not footprint.allow_on_farmland):
				is_valid = false
				break
			occupied_cells[cell] = true
		validation_node.free()
		if not is_valid:
			return false
	return true

func _apply_loaded_state(candidate_map: TerrainMapData, entries: Array[Dictionary]) -> void:
	_remove_persistent_buildings()
	clear_registered_buildings()
	map_data = candidate_map
	map_width = candidate_map.width
	map_height = candidate_map.height
	cell_size = candidate_map.cell_size
	_clear_chunk_nodes()
	_configure_scene_geometry()
	_mark_all_chunks_dirty()
	_register_preplaced_buildings()
	for entry in entries:
		var scene: PackedScene = ResourceLoader.load(entry["scene_path"]) as PackedScene
		var building_node: Node3D = scene.instantiate() as Node3D
		var footprint := _find_footprint(building_node)
		_apply_footprint_data(footprint, entry["footprint"])
		building_node.scale = entry["scale"]
		_place_building_instance(building_node, entry["anchor_cell"], entry["rotation_y"], entry["building_id"])

func _remove_persistent_buildings() -> void:
	for building_node in _buildings.get_children():
		var footprint := _find_footprint(building_node)
		if footprint == null or not footprint.persistent:
			continue
		_buildings.remove_child(building_node)
		building_node.queue_free()

func _find_footprint(building_node: Node) -> BuildingFootprint:
	for child in building_node.get_children():
		if child is BuildingFootprint:
			return child
	return null

func _apply_footprint_data(footprint: BuildingFootprint, data: Dictionary) -> void:
	footprint.footprint_size = _parse_vector2i(data["size"])
	footprint.anchor_offset = _parse_vector2i(data["offset"])
	var placement_offset: Dictionary = data["placement_offset"]
	footprint.placement_offset = Vector3(float(placement_offset["x"]), float(placement_offset["y"]), float(placement_offset["z"]))
	footprint.allow_on_farmland = bool(data["allow_on_farmland"])

func _is_valid_footprint_data(data: Dictionary) -> bool:
	var raw_size: Variant = data.get("size")
	var raw_offset: Variant = data.get("offset")
	var raw_placement_offset: Variant = data.get("placement_offset")
	var raw_farmland: Variant = data.get("allow_on_farmland")
	if not raw_size is Dictionary or not raw_offset is Dictionary or not raw_placement_offset is Dictionary or not raw_farmland is bool or not _is_valid_vector2i_data(raw_size) or not _is_valid_vector2i_data(raw_offset):
		return false
	var size: Vector2i = _parse_vector2i(raw_size)
	if size.x <= 0 or size.y <= 0:
		return false
	var placement_offset: Dictionary = raw_placement_offset
	return _is_number(placement_offset.get("x")) and _is_number(placement_offset.get("y")) and _is_number(placement_offset.get("z"))

func _parse_vector2i(raw_vector: Variant) -> Vector2i:
	if not raw_vector is Dictionary:
		return Vector2i(-1, -1)
	var vector_data: Dictionary = raw_vector
	var raw_x: Variant = vector_data.get("x")
	var raw_y: Variant = vector_data.get("y")
	if not _is_integer_number(raw_x) or not _is_integer_number(raw_y):
		return Vector2i(-1, -1)
	return Vector2i(int(raw_x), int(raw_y))

func _is_valid_vector2i_data(raw_vector: Variant) -> bool:
	if not raw_vector is Dictionary:
		return false
	var vector_data: Dictionary = raw_vector
	return _is_integer_number(vector_data.get("x")) and _is_integer_number(vector_data.get("y"))

func _parse_vector3(raw_vector: Variant) -> Vector3:
	if not _is_valid_vector3_data(raw_vector):
		return Vector3.ZERO
	var vector_data: Dictionary = raw_vector
	return Vector3(float(vector_data["x"]), float(vector_data["y"]), float(vector_data["z"]))

func _is_valid_vector3_data(raw_vector: Variant) -> bool:
	if not raw_vector is Dictionary:
		return false
	var vector_data: Dictionary = raw_vector
	return _is_number(vector_data.get("x")) and _is_number(vector_data.get("y")) and _is_number(vector_data.get("z"))

func _is_number(value: Variant) -> bool:
	return typeof(value) == TYPE_INT or typeof(value) == TYPE_FLOAT

func _is_integer_number(value: Variant) -> bool:
	return typeof(value) == TYPE_INT or (typeof(value) == TYPE_FLOAT and is_equal_approx(float(value), roundf(float(value))))

func _configure_scene_geometry() -> void:
	var map_world_width: float = float(map_width) * cell_size
	var map_world_height: float = float(map_height) * cell_size
	var ground_mesh: PlaneMesh = _base_ground.mesh as PlaneMesh
	ground_mesh.size = Vector2(map_world_width, map_world_height)
	var ground_shape: BoxShape3D = _ground_collision.shape as BoxShape3D
	ground_shape.size = Vector3(map_world_width, ground_thickness, map_world_height)
	_ground_collision.position = Vector3(0.0, -ground_thickness * 0.5, 0.0)
	_configure_wall(_left_wall, Vector3(-map_world_width * 0.5 - boundary_thickness * 0.5, boundary_height * 0.5, 0.0), Vector3(boundary_thickness, boundary_height, map_world_height))
	_configure_wall(_right_wall, Vector3(map_world_width * 0.5 + boundary_thickness * 0.5, boundary_height * 0.5, 0.0), Vector3(boundary_thickness, boundary_height, map_world_height))
	_configure_wall(_top_wall, Vector3(0.0, boundary_height * 0.5, -map_world_height * 0.5 - boundary_thickness * 0.5), Vector3(map_world_width, boundary_height, boundary_thickness))
	_configure_wall(_bottom_wall, Vector3(0.0, boundary_height * 0.5, map_world_height * 0.5 + boundary_thickness * 0.5), Vector3(map_world_width, boundary_height, boundary_thickness))

func _configure_wall(wall: StaticBody3D, wall_position: Vector3, shape_size: Vector3) -> void:
	wall.position = wall_position
	var collision_shape: CollisionShape3D = wall.get_child(0) as CollisionShape3D
	var box_shape: BoxShape3D = collision_shape.shape as BoxShape3D
	box_shape.size = shape_size

func _mark_all_chunks_dirty() -> void:
	for chunk_y in ceili(float(map_height) / float(chunk_size)):
		for chunk_x in ceili(float(map_width) / float(chunk_size)):
			_mark_chunk_dirty(Vector2i(chunk_x, chunk_y))

func _mark_chunks_affected_by_cell(cell: Vector2i) -> void:
	_mark_chunk_dirty(_cell_to_chunk(cell))
	for neighbor in [cell + Vector2i.UP, cell + Vector2i.RIGHT, cell + Vector2i.DOWN, cell + Vector2i.LEFT]:
		if map_data.is_inside_map(neighbor):
			_mark_chunk_dirty(_cell_to_chunk(neighbor))

func _mark_chunk_dirty(chunk_coord: Vector2i) -> void:
	_dirty_chunks[chunk_coord] = true
	if not is_inside_tree() or _rebuild_scheduled:
		return
	_rebuild_scheduled = true
	call_deferred("_flush_dirty_chunks_deferred")

func _flush_dirty_chunks_deferred() -> void:
	flush_dirty_chunks()

func _cell_to_chunk(cell: Vector2i) -> Vector2i:
	return Vector2i(cell.x / chunk_size, cell.y / chunk_size)

func _rebuild_chunk(chunk_coord: Vector2i) -> void:
	var mesh_instance: MeshInstance3D = _get_or_create_chunk_node(chunk_coord)
	var terrain_mesh := ArrayMesh.new()
	var surface_index: int = 0
	for cell_type in range(TerrainMapData.CellType.DIRT, TerrainMapData.CellType.STONE + 1):
		var arrays: Array = _build_surface_arrays(chunk_coord, cell_type)
		if arrays.is_empty():
			continue
		terrain_mesh.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, arrays)
		if cell_type - 1 < terrain_materials.size():
			terrain_mesh.surface_set_material(surface_index, terrain_materials[cell_type - 1])
		surface_index += 1
	mesh_instance.mesh = terrain_mesh if surface_index > 0 else null

func _get_or_create_chunk_node(chunk_coord: Vector2i) -> MeshInstance3D:
	if _chunk_nodes.has(chunk_coord):
		return _chunk_nodes[chunk_coord]
	var mesh_instance := MeshInstance3D.new()
	mesh_instance.name = "Chunk_%d_%d" % [chunk_coord.x, chunk_coord.y]
	_chunks.add_child(mesh_instance)
	_chunk_nodes[chunk_coord] = mesh_instance
	return mesh_instance

func _clear_chunk_nodes() -> void:
	for mesh_instance in _chunk_nodes.values():
		if mesh_instance.get_parent() != null:
			mesh_instance.get_parent().remove_child(mesh_instance)
		mesh_instance.queue_free()
	_chunk_nodes.clear()

func _build_surface_arrays(chunk_coord: Vector2i, cell_type: int) -> Array:
	var vertices: PackedVector3Array = PackedVector3Array()
	var normals: PackedVector3Array = PackedVector3Array()
	var uvs: PackedVector2Array = PackedVector2Array()
	var indices: PackedInt32Array = PackedInt32Array()
	var first_cell: Vector2i = chunk_coord * chunk_size
	var last_cell: Vector2i = Vector2i(mini(first_cell.x + chunk_size, map_width), mini(first_cell.y + chunk_size, map_height))
	for cell_y in range(first_cell.y, last_cell.y):
		for cell_x in range(first_cell.x, last_cell.x):
			var cell := Vector2i(cell_x, cell_y)
			if map_data.get_cell(cell) != cell_type:
				continue
			_append_cell_quad(vertices, normals, uvs, indices, cell, get_autotile_mask(cell))
	if vertices.is_empty():
		return []
	var arrays: Array = []
	arrays.resize(Mesh.ARRAY_MAX)
	arrays[Mesh.ARRAY_VERTEX] = vertices
	arrays[Mesh.ARRAY_NORMAL] = normals
	arrays[Mesh.ARRAY_TEX_UV] = uvs
	arrays[Mesh.ARRAY_INDEX] = indices
	return arrays

func _append_cell_quad(vertices: PackedVector3Array, normals: PackedVector3Array, uvs: PackedVector2Array, indices: PackedInt32Array, cell: Vector2i, mask: int) -> void:
	var center: Vector3 = map_data.cell_to_world(cell)
	var half_size: float = cell_size * 0.5
	var vertex_start: int = vertices.size()
	vertices.append_array([
		Vector3(center.x - half_size, 0.003, center.z - half_size),
		Vector3(center.x + half_size, 0.003, center.z - half_size),
		Vector3(center.x + half_size, 0.003, center.z + half_size),
		Vector3(center.x - half_size, 0.003, center.z + half_size),
	])
	normals.append_array([Vector3.UP, Vector3.UP, Vector3.UP, Vector3.UP])
	uvs.append_array(_get_mask_uvs(mask))
	indices.append_array([vertex_start, vertex_start + 1, vertex_start + 2, vertex_start, vertex_start + 2, vertex_start + 3])

func _get_mask_uvs(_mask: int) -> PackedVector2Array:
	var minimum := Vector2.ZERO
	var maximum := Vector2.ONE
	return PackedVector2Array([
		Vector2(minimum.x, minimum.y),
		Vector2(maximum.x, minimum.y),
		Vector2(maximum.x, maximum.y),
		Vector2(minimum.x, maximum.y),
	])
