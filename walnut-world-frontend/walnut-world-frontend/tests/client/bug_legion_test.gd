extends SceneTree

const BUG_LEGION_SCENE := preload("res://scenes/characters/bug_legion/bug_legion.tscn")


func _initialize() -> void:
	var legion := BUG_LEGION_SCENE.instantiate()
	legion.reveal_duration_seconds = 0.01
	legion.dismiss_duration_seconds = 0.01
	root.add_child(legion)
	await process_frame
	legion.show_challenge()
	if not legion.visible or legion.get_node("Members").get_child_count() != 4:
		push_error("Bug 先生的世界表现必须展示由四个预置节点组成的 Bug 军团。")
		quit(1)
		return
	for member in legion.get_node("Members").get_children():
		if not member is Sprite3D or not member.texture.resource_path.ends_with("pest_bug.png"):
			push_error("Bug 军团成员必须复用前端害虫美术资源。")
			quit(1)
			return
	legion.dismiss()
	await create_timer(0.08).timeout
	if legion.visible:
		push_error("Bug 军团在人物展示结束后必须退出世界画面。")
		quit(1)
		return
	print("BUG_LEGION_TEST_PASS")
	quit(0)
