extends SceneTree

const MAIN_SCENE := preload("res://scenes/main/main.tscn")

func _initialize() -> void:
	await process_frame
	var failures: Array[String] = []
	var main := MAIN_SCENE.instantiate() as Node3D
	root.add_child(main)
	await process_frame
	var gallery := main.get_node_or_null("TerrainManager/Buildings/ArtGalleryInstance") as Node3D
	if gallery == null:
		failures.append("Main must instance ArtGalleryInstance")
	else:
		var footprint := gallery.get_node_or_null("BuildingFootprint") as BuildingFootprint
		if footprint == null or footprint.occupies_cells or footprint.persistent:
			failures.append("ArtGallery must have a non-persistent non-occupying footprint configuration")
		var art_prop_count := 0
		for art_prop in gallery.get_children():
			if art_prop is BuildingFootprint:
				continue
			art_prop_count += 1
			var billboard := art_prop.get_node_or_null("Billboard") as Sprite3D
			if billboard == null or billboard.texture == null:
				failures.append("%s is missing a textured Sprite3D billboard" % art_prop.name)
				continue
			if not is_equal_approx(billboard.pixel_size, 0.0016):
				failures.append("%s has a non-standard billboard pixel size" % art_prop.name)
			if billboard.billboard != BaseMaterial3D.BILLBOARD_ENABLED:
				failures.append("%s has billboard mode disabled" % art_prop.name)
		if art_prop_count != 30:
			failures.append("ArtGallery must expose all 30 generated art props")
	main.queue_free()
	await process_frame
	if failures.is_empty():
		print("ART_GALLERY_TEST_PASS: 30 个透明美术资源均已接入 Sprite3D 预置场景")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
