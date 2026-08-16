extends SceneTree

const TaskWorkspace := preload("res://scenes/task/task_workspace.tscn")
const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")


class Product:
	extends RefCounted
	var save_count := 0
	func upsert_draft(_attempt: Dictionary, _session_id: String, _draft_id: String, _key: String, request: Dictionary) -> Dictionary:
		save_count += 1
		if save_count == 1:
			var store := Engine.get_main_loop().root.get_node("ClientStore") as WalnutClientStore
			store.mark_draft_dirty("int main() { return 8; }")
		var saved := request.duplicate(true)
		saved.merge({"revision": save_count + 1, "draft_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}, true)
		return {"ok": true, "status": 200, "headers": {}, "value": saved}


func _initialize() -> void:
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	if store == null:
		store = StoreScript.new()
		store.name = "ClientStore"
		root.add_child(store)
	store.persistence_enabled = false
	store.set_authoritative_session({"session_id": "session_demo_0001"})
	store.set_draft(_draft())
	var session := root.get_node_or_null("SessionController") as Node
	if session == null:
		session = ControllerScript.new()
		session.name = "SessionController"
		root.add_child(session)
	await process_frame
	var product := Product.new()
	session.configure(null, product)
	session.configure_draft_context({"attempt": {"request_id": "req_demo"}})
	var page := TaskWorkspace.instantiate()
	root.add_child(page)
	await process_frame
	var editor := page.get_node("DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeEditorPanel/Content/CodeEditor") as CodeEdit
	editor.text = "int main() { return 7; }"
	editor.text_changed.emit()
	await create_timer(2.0).timeout
	if product.save_count != 2 or store.draft_state != WalnutClientStore.DraftState.CLEAN or store.local_source != "int main() { return 8; }":
		push_error("TaskWorkspace must debounce changes, preserve edits made during save, and schedule the next canonical Draft save.")
		quit(1)
		return
	print("TASK_WORKSPACE_AUTOSAVE_TEST_PASS")
	quit(0)


func _draft() -> Dictionary:
	return {
		"session_id": "session_demo_0001",
		"draft_id": "draft_demo_0001",
		"skill_id": "skill_demo_0001",
		"revision": 1,
		"draft_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		"display_name": "Demo",
		"content_ref": {},
		"source_bundle": {"language": "CPP20", "entrypoint": "src/main.cpp", "files": [{"path": "src/main.cpp", "content": "int main() { return 0; }", "content_sha256": ""}]},
	}
