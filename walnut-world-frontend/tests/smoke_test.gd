extends SceneTree

const MAIN_SCENE := preload("res://scenes/main/main.tscn")

func _initialize() -> void:
	var failures: Array[String] = []
	for action in [&"move_up", &"move_down", &"move_left", &"move_right", &"camera_rotate_left", &"camera_rotate_right"]:
		if not InputMap.has_action(action):
			failures.append("缺少 InputMap 动作：%s" % action)

	if not _has_key(&"move_up", KEY_W) or not _has_key(&"move_up", KEY_UP):
		failures.append("move_up 未同时绑定 W 和方向上")
	if not _has_key(&"move_down", KEY_S) or not _has_key(&"move_down", KEY_DOWN):
		failures.append("move_down 未同时绑定 S 和方向下")
	if not _has_key(&"move_left", KEY_A) or not _has_key(&"move_left", KEY_LEFT):
		failures.append("move_left 未同时绑定 A 和方向左")
	if not _has_key(&"move_right", KEY_D) or not _has_key(&"move_right", KEY_RIGHT):
		failures.append("move_right 未同时绑定 D 和方向右")
	if not _has_key(&"camera_rotate_left", KEY_Q):
		failures.append("camera_rotate_left 未绑定 Q")
	if not _has_key(&"camera_rotate_right", KEY_E):
		failures.append("camera_rotate_right 未绑定 E")

	var main := MAIN_SCENE.instantiate()
	root.add_child(main)
	await process_frame

	var player := main.get_node("Player") as CharacterBody3D
	var sprite := player.get_node("Visual/AnimatedSprite3D") as AnimatedSprite3D
	var camera := main.get_node_or_null("CameraRig/Camera3D") as Camera3D
	if camera == null:
		failures.append("主场景缺少 Camera3D")
	else:
		if camera.projection != Camera3D.PROJECTION_PERSPECTIVE:
			failures.append("相机不是透视投影")
		if not camera.current:
			failures.append("主相机未设为 current")
		if camera.fov < 20.0 or camera.fov > 40.0:
			failures.append("相机 FOV 未处于弱透视范围")
	var camera_rig := main.get_node_or_null("CameraRig") as Node3D
	if camera_rig == null:
		failures.append("主场景缺少 CameraRig")
	elif camera == null:
		failures.append("CameraRig 缺少 Camera3D")
	else:
		var camera_local_position := camera.position
		var camera_local_rotation := camera.rotation
		player.global_position = Vector3(3.0, 0.0, -4.0)
		await process_frame
		await process_frame
		var expected_rig_position := Vector3(3.0, 0.0, -4.0)
		if not camera_rig.global_position.is_equal_approx(expected_rig_position):
			failures.append("CameraRig 没有与玩家同步 XZ")
		if not camera.position.is_equal_approx(camera_local_position) or not camera.rotation.is_equal_approx(camera_local_rotation):
			failures.append("跟随过程改变了 Camera3D 的固定局部变换")
		player.global_position = Vector3.ZERO
		await process_frame
		await process_frame
		var initial_camera_rotation := camera_rig.rotation.y
		var initial_camera_distance := camera.position.length()
		camera_rig._unhandled_input(_make_action_event(&"camera_rotate_left"))
		if not is_equal_approx(camera_rig.rotation.y, initial_camera_rotation):
			failures.append("Q 触发后镜头发生瞬移，而非平滑转向")
		camera_rig._process(0.1)
		if camera_rig.rotation.y >= initial_camera_rotation or camera_rig.rotation.y <= initial_camera_rotation - deg_to_rad(45.0):
			failures.append("镜头转向首帧不处于平滑过渡区间")
		for frame in 30:
			camera_rig._process(0.1)
		if not is_equal_approx(camera_rig.rotation.y - initial_camera_rotation, deg_to_rad(-45.0)):
			failures.append("Q 未使镜头最终向左转 45 度")
		if not is_equal_approx(camera.position.length(), initial_camera_distance):
			failures.append("镜头转向改变了镜头距离")
		player.global_position = Vector3.ZERO
		player.velocity = Vector3.ZERO
		camera_rig._unhandled_input(_make_action_event(&"camera_rotate_left"))
		for frame in 30:
			camera_rig._process(0.1)
		var rotated_move_start := player.global_position
		var expected_screen_forward := camera_rig.global_transform.basis * Vector3.FORWARD
		Input.action_press(&"move_up")
		await _wait_physics_frames(24)
		var rotated_move := player.global_position - rotated_move_start
		if rotated_move.length_squared() == 0.0 or rotated_move.normalized().dot(expected_screen_forward.normalized()) < 0.98:
			failures.append("旋转镜头后 W 没有沿屏幕前方移动")
		if sprite.animation != &"walk_up":
			failures.append("镜头转 90 度后按 W 未播放 walk_up")
		Input.action_release(&"move_up")
		camera_rig._unhandled_input(_make_action_event(&"camera_rotate_right"))
		camera_rig._unhandled_input(_make_action_event(&"camera_rotate_right"))
		for frame in 30:
			camera_rig._process(0.1)
		if not is_equal_approx(camera_rig.rotation.y, initial_camera_rotation):
			failures.append("E 未使镜头最终向右转 45 度")
		for step in 20:
			camera_rig._unhandled_input(_make_wheel_event(MOUSE_BUTTON_WHEEL_UP))
		if not is_equal_approx(camera.position.length(), 6.0):
			failures.append("滚轮放大未限制在 6 米")
		for step in 20:
			camera_rig._unhandled_input(_make_wheel_event(MOUSE_BUTTON_WHEEL_DOWN))
		if not is_equal_approx(camera.position.length(), 16.0):
			failures.append("滚轮缩小未限制在 16 米")
	var terrain := main.get_node_or_null("TerrainManager") as Node3D
	if terrain == null:
		failures.append("主场景缺少 TerrainManager")
	else:
		var ground_mesh := terrain.get_node_or_null("BaseGround") as MeshInstance3D
		if ground_mesh == null or not ground_mesh.mesh is PlaneMesh:
			failures.append("TerrainManager 基础地面未使用 PlaneMesh")
		if terrain.get_node_or_null("GroundBody/CollisionShape3D") == null:
			failures.append("TerrainManager 地面缺少单一 StaticBody3D 碰撞")
		if terrain.get_node_or_null("Chunks") == null or terrain.get_node_or_null("Buildings") == null:
			failures.append("TerrainManager 缺少 Chunks 或 Buildings 预置节点")
		if terrain.get_node_or_null("WorldBounds/LeftWall/CollisionShape3D") == null or terrain.get_node_or_null("WorldBounds/RightWall/CollisionShape3D") == null or terrain.get_node_or_null("WorldBounds/TopWall/CollisionShape3D") == null or terrain.get_node_or_null("WorldBounds/BottomWall/CollisionShape3D") == null:
			failures.append("TerrainManager 缺少完整边界墙碰撞")
	if main.get_node_or_null("GroundTiles") != null:
		failures.append("主场景不应保留 GroundTiles")
	var grass_instances := main.get_node("GrassInstances")
	if grass_instances.get_child_count() < 2:
		failures.append("草丛实例少于两个")
	for grass_patch in grass_instances.get_children():
		var grass_sprite := grass_patch.get_node_or_null("Billboard") as Sprite3D
		if grass_sprite == null or grass_sprite.texture == null:
			failures.append("草丛实例缺少可显示贴图")
	var garden_house := main.get_node_or_null("TerrainManager/Buildings/GardenHouseInstance") as Node3D
	if garden_house == null:
		failures.append("Main scene is missing GardenHouseInstance")
	else:
		var garden_house_sprite := garden_house.get_node_or_null("Billboard") as Sprite3D
		if garden_house_sprite == null or garden_house_sprite.texture == null:
			failures.append("GardenHouseInstance is missing its Billboard texture")
		elif not is_equal_approx(garden_house_sprite.pixel_size, 0.0016):
			failures.append("GardenHouseInstance pixel size does not match grass_patch")
		var garden_footprint := garden_house.get_node_or_null("BuildingFootprint") as BuildingFootprint
		if garden_footprint == null:
			failures.append("GardenHouseInstance is missing BuildingFootprint")
		elif garden_footprint.footprint_size != Vector2i(3, 3) or garden_footprint.anchor_offset != Vector2i(-1, -1):
			failures.append("GardenHouseInstance footprint configuration is incorrect")
	var art_gallery := main.get_node_or_null("TerrainManager/Buildings/ArtGalleryInstance") as Node3D
	if art_gallery == null:
		failures.append("Main scene is missing ArtGalleryInstance")
	else:
		var art_footprint := art_gallery.get_node_or_null("BuildingFootprint") as BuildingFootprint
		if art_footprint == null or art_footprint.occupies_cells or art_footprint.persistent:
			failures.append("ArtGalleryInstance must expose non-persistent non-occupying footprint configuration")
		var art_prop_count := 0
		for art_prop in art_gallery.get_children():
			if art_prop is BuildingFootprint:
				continue
			art_prop_count += 1
			var art_sprite := art_prop.get_node_or_null("Billboard") as Sprite3D
			if art_sprite == null or art_sprite.texture == null:
				failures.append("ArtGalleryInstance contains a prop without a textured Sprite3D billboard")
			elif not is_equal_approx(art_sprite.pixel_size, 0.0016):
				failures.append("ArtGalleryInstance billboard pixel size is not standardized")
		if art_prop_count != 30:
			failures.append("ArtGalleryInstance must contain all 30 generated art props")
	if sprite.sprite_frames.get_frame_count(&"walk_up") != 6:
		failures.append("walk_up 不是 6 帧")
	if sprite.sprite_frames.get_frame_count(&"walk_down") != 4:
		failures.append("walk_down 不是 4 帧")
	if sprite.sprite_frames.get_frame_count(&"walk_left") != 6:
		failures.append("walk_left 不是 6 帧")
	if sprite.sprite_frames.get_frame_count(&"walk_right") != 6:
		failures.append("walk_right 不是 6 帧")
	if sprite.sprite_frames.get_frame_count(&"idle_down") < 2:
		failures.append("idle_down 少于 2 帧")
	if sprite.billboard != BaseMaterial3D.BILLBOARD_ENABLED:
		failures.append("角色精灵未启用广告牌模式")

	var move_cases: Array[Dictionary] = [
		{"action": &"move_up", "direction": Vector3(0, 0, -1), "walk": &"walk_up", "idle": &"idle_up"},
		{"action": &"move_down", "direction": Vector3(0, 0, 1), "walk": &"walk_down", "idle": &"idle_down"},
		{"action": &"move_left", "direction": Vector3(-1, 0, 0), "walk": &"walk_left", "idle": &"idle_left"},
		{"action": &"move_right", "direction": Vector3(1, 0, 0), "walk": &"walk_right", "idle": &"idle_right"}
	]
	for move_case in move_cases:
		player.global_position = Vector3.ZERO
		player.velocity = Vector3.ZERO
		var start := player.global_position
		Input.action_press(move_case.action)
		await _wait_physics_frames(24)
		Input.action_release(move_case.action)
		var moved := player.global_position - start
		if moved.dot(move_case.direction) <= 0.2:
			failures.append("%s 未驱动预期方向移动" % move_case.action)
		if sprite.animation != move_case.walk:
			failures.append("%s 未切换 %s" % [move_case.action, move_case.walk])
		await _wait_physics_frames(28)
		if sprite.animation != move_case.idle:
			failures.append("%s 停下后未切换 %s" % [move_case.action, move_case.idle])

	player.global_position = Vector3.ZERO
	player.velocity = Vector3.ZERO
	var diagonal_start := player.global_position
	Input.action_press(&"move_left")
	Input.action_press(&"move_down")
	await _wait_physics_frames(24)
	Input.action_release(&"move_left")
	Input.action_release(&"move_down")
	var diagonal_distance := player.global_position - diagonal_start
	if not is_equal_approx(absf(diagonal_distance.x), absf(diagonal_distance.z)):
		failures.append("对角输入没有进行归一化")
	await _wait_physics_frames(28)

	if failures.is_empty():
		print("SMOKE_TEST_PASS: 输入、移动、动画帧数、广告牌和归一化均已验证")
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)

func _has_key(action: StringName, keycode: Key) -> bool:
	for event in InputMap.action_get_events(action):
		if event is InputEventKey and event.keycode == keycode:
			return true
	return false

func _make_action_event(action: StringName) -> InputEventAction:
	var event := InputEventAction.new()
	event.action = action
	event.pressed = true
	return event

func _make_wheel_event(button_index: MouseButton) -> InputEventMouseButton:
	var event := InputEventMouseButton.new()
	event.button_index = button_index
	event.pressed = true
	return event

func _wait_physics_frames(frame_count: int) -> void:
	for frame_index in frame_count:
		await physics_frame
