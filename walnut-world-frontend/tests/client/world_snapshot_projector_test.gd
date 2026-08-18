extends SceneTree

const Projector := preload("res://scripts/client/world_snapshot_projector.gd")
const TerrainScene := preload("res://scenes/terrain/terrain_manager.tscn")

func _initialize() -> void:
	var terrain := TerrainScene.instantiate() as TerrainManager
	root.add_child(terrain)
	await process_frame
	terrain.configure_map(4, 4, 1.0)
	var avatar := Node3D.new()
	root.add_child(avatar)
	var projector := Projector.new()
	if not projector.apply(_snapshot(), terrain, avatar):
		push_error("Authority Snapshot should map only to existing terrain and avatar nodes.")
		quit(1)
		return
	if terrain.map_data.get_cell(Vector2i(1, 1)) != TerrainMapData.CellType.FARMLAND or terrain.map_data.get_cell(Vector2i(2, 1)) != TerrainMapData.CellType.DIRT or avatar.global_position != terrain.map_data.cell_to_world(Vector2i(1, 2)):
		push_error("Snapshot soil/hydration and avatar position were not projected to preauthored nodes.")
		quit(1)
		return
	print("WORLD_SNAPSHOT_PROJECTOR_TEST_PASS")
	quit(0)

func _snapshot() -> Dictionary:
	return {"world_id": "world_demo_0001", "revision": 2, "state": {"avatar": {"position": {"x": 1, "y": 2}}, "plots": [{"position": {"x": 1, "y": 1}, "soil_state": "TILLED", "hydration": 100}, {"position": {"x": 2, "y": 1}, "soil_state": "TILLED", "hydration": 0}]}}
