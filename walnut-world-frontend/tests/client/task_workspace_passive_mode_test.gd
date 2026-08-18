extends SceneTree

const WORKSPACE_PATH := "res://scenes/task/task_workspace.tscn"


func _initialize() -> void:
	var workspace := (load(WORKSPACE_PATH) as PackedScene).instantiate() as Control
	workspace.set("interactive_enabled", false)
	root.add_child(workspace)
	await process_frame
	var failures: Array[String] = []
	for path in ["AgentInteractionPresenter", "Hud", "DrawerLayer"]:
		var layer := workspace.get_node_or_null(path) as CanvasLayer
		if layer == null or layer.visible:
			failures.append("被动 TaskWorkspace 必须关闭旧 UI CanvasLayer：%s" % path)
	workspace.queue_free()
	await process_frame
	if failures.is_empty():
		print("TASK_WORKSPACE_PASSIVE_MODE_TEST_PASS: 旧 UI CanvasLayer 已完全隔离")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
