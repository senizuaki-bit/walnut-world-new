extends SceneTree

const TERRAIN_SCENE := preload("res://scenes/terrain/terrain_manager.tscn")

var _failures: Array[String] = []

func _initialize() -> void:
	var terrain := TERRAIN_SCENE.instantiate() as TerrainManager
	root.add_child(terrain)
	await process_frame
	var ground_mesh := terrain.get_node_or_null("BaseGround") as MeshInstance3D
	_expect(ground_mesh != null, "基础草地节点存在")
	if ground_mesh != null:
		var base_material := ground_mesh.get_active_material(0) as StandardMaterial3D
		_expect(base_material != null and base_material.albedo_texture != null and base_material.albedo_texture.resource_path.ends_with("grass_plot.png"), "基础草地使用 grass_plot 贴图")
	var expected_terrain_textures := ["tilled_soil.png", "watered_soil.png", "dirt_road.png"]
	for material_index in expected_terrain_textures.size():
		var terrain_material := terrain.terrain_materials[material_index] as StandardMaterial3D
		_expect(terrain_material != null and terrain_material.albedo_texture != null and terrain_material.albedo_texture.resource_path.ends_with(expected_terrain_textures[material_index]), "地皮覆盖材质使用 %s" % expected_terrain_textures[material_index])
	_finish()

func _expect(condition: bool, message: String) -> void:
	if not condition:
		_failures.append(message)

func _finish() -> void:
	if _failures.is_empty():
		print("TERRAIN_MATERIAL_TEST_PASS")
		quit(0)
		return
	for failure in _failures:
		push_error(failure)
	quit(1)
