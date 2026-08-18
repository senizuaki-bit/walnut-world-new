extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")


class DelayedProduct:
	extends RefCounted
	func upsert_draft(_attempt: Dictionary, _session_id: String, _draft_id: String, _key: String, request: Dictionary) -> Dictionary:
		var store := Engine.get_main_loop().root.get_node("ClientStore") as WalnutClientStore
		store.mark_draft_dirty("int main() { return 2; }")
		await Engine.get_main_loop().process_frame
		var saved := request.duplicate(true)
		saved.merge({"revision": 2, "draft_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}, true)
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
	store.mark_draft_dirty("int main() { return 1; }")
	var controller := root.get_node_or_null("SessionController") as Node
	if controller == null:
		controller = ControllerScript.new()
		controller.name = "SessionController"
		root.add_child(controller)
	await process_frame
	controller.configure(null, DelayedProduct.new())
	controller.configure_draft_context({"attempt": {"request_id": "req_demo"}})
	var result: Dictionary = await controller.request_save()
	if not bool(result.get("ok", false)) or store.local_source != "int main() { return 2; }" or store.draft_state != WalnutClientStore.DraftState.DIRTY or int(store.draft.get("revision", 0)) != 2:
		push_error("A local edit made during save must survive the canonical save receipt and remain dirty for the next CAS save.")
		quit(1)
		return
	print("DRAFT_SAVE_CONCURRENCY_TEST_PASS")
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
