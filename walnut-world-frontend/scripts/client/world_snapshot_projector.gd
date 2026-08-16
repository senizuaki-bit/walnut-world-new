class_name WorldSnapshotProjector
extends RefCounted

## Presentation-only mapping from a validated authority Snapshot to the
## preauthored farm nodes. It never creates world objects or derives outcomes.

signal projection_rejected(reason: String)
signal projection_applied(world_id: String, revision: int)

var _projected_plot_cells: Array[Vector2i] = []

func apply(snapshot: Dictionary, terrain: TerrainManager, avatar: Node3D) -> bool:
	var prepared := validate(snapshot, terrain, avatar)
	if not prepared.ok:
		projection_rejected.emit("World Snapshot cannot be mapped to the preauthored farm scene.")
		return false
	var state: Dictionary = snapshot.state
	for cell in _projected_plot_cells:
		if terrain.map_data.is_inside_map(cell):
			terrain.set_terrain_cell(cell, TerrainMapData.CellType.GRASS)
	var next_projected_cells: Array[Vector2i] = []
	for plot in state.plots:
		var cell := Vector2i(int(plot.position.x), int(plot.position.y))
		var terrain_type := TerrainMapData.CellType.GRASS
		if str(plot.soil_state) == "TILLED":
			terrain_type = TerrainMapData.CellType.FARMLAND if int(plot.hydration) > 0 else TerrainMapData.CellType.DIRT
		terrain.set_terrain_cell(cell, terrain_type)
		next_projected_cells.append(cell)
	var avatar_cell := Vector2i(int(state.avatar.position.x), int(state.avatar.position.y))
	avatar.global_position = terrain.map_data.cell_to_world(avatar_cell)
	terrain.flush_dirty_chunks()
	_projected_plot_cells = next_projected_cells
	projection_applied.emit(str(snapshot.world_id), int(snapshot.revision))
	return true


func validate(snapshot: Dictionary, terrain: TerrainManager, avatar: Node3D) -> Dictionary:
	if terrain == null or avatar == null or not _valid_state(snapshot.get("state")):
		return {"ok": false, "message": "World Snapshot cannot be mapped to the preauthored farm scene."}
	var state: Dictionary = snapshot.state
	for plot in state.plots:
		var cell := Vector2i(int(plot.position.x), int(plot.position.y))
		if not terrain.map_data.is_inside_map(cell):
			return {"ok": false, "message": "Snapshot plot position is outside the preauthored terrain."}
	var avatar_cell := Vector2i(int(state.avatar.position.x), int(state.avatar.position.y))
	if not terrain.map_data.is_inside_map(avatar_cell):
		return {"ok": false, "message": "Snapshot avatar position is outside the preauthored terrain."}
	return {"ok": true}


func _valid_state(value: Variant) -> bool:
	if not value is Dictionary or not value.get("plots") is Array or not value.get("avatar") is Dictionary:
		return false
	for plot in value.plots:
		if not plot is Dictionary or not plot.get("position") is Dictionary or not plot.position.has("x") or not plot.position.has("y") or not plot.has("soil_state") or not plot.has("hydration"):
			return false
		if str(plot.soil_state) not in ["UNTILLED", "TILLED"] or typeof(plot.hydration) not in [TYPE_INT, TYPE_FLOAT]:
			return false
	var position: Variant = value.avatar.get("position")
	return position is Dictionary and position.has("x") and position.has("y")
