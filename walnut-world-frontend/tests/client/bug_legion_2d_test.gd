extends SceneTree

const BUG_LEGION_SCENE := preload("res://scenes/characters/bug_legion/bug_legion_2d.tscn")


func _initialize() -> void:
	var legion := BUG_LEGION_SCENE.instantiate()
	root.add_child(legion)
	await process_frame
	var members := legion.get_node("Root/Members") as Control
	if legion.visible or members.get_child_count() != 4:
		_abort("2D Bug 军团必须初始隐藏，并由四个预置成员组成。")
		return
	if legion.mouse_filter != Control.MOUSE_FILTER_IGNORE or members.mouse_filter != Control.MOUSE_FILTER_IGNORE:
		_abort("2D Bug 军团表现层不得阻挡玩家输入。")
		return
	for member in members.get_children():
		if (
			not member is TextureRect
			or member.mouse_filter != Control.MOUSE_FILTER_IGNORE
			or member.texture == null
			or not member.texture.resource_path.ends_with("char-bug-idle.png")
		):
			_abort("四个 2D 成员必须复用 char-bug-idle.png，且全部忽略鼠标。")
			return
	legion.show_legion()
	if not legion.visible:
		_abort("Bug 角色 world cue 开始时必须立即显示 2D 军团。")
		return
	await create_timer(0.28).timeout
	legion.hide_legion()
	await create_timer(0.22).timeout
	if legion.visible:
		_abort("Bug 角色 world cue 结束后必须关闭 2D 军团。")
		return
	print("BUG_LEGION_2D_TEST_PASS")
	quit(0)


func _abort(message: String) -> void:
	push_error(message)
	quit(1)
