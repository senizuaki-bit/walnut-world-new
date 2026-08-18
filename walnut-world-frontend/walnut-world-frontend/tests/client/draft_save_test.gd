extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")

class FakeProduct:
	extends RefCounted
	var request: Dictionary = {}
	func upsert_draft(_attempt: Dictionary, _session_id: String, _draft_id: String, _key: String, value: Dictionary) -> Dictionary:
		request = value.duplicate(true)
		await Engine.get_main_loop().process_frame
		var saved := value.duplicate(true)
		saved.merge({"request_context": _attempt, "revision": 2, "draft_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "created_at": "2026-08-09T00:00:00Z", "updated_at": "2026-08-09T00:00:01Z", "last_applied_patch_id": null, "links": {"self": "/draft", "session_workspace": "/workspace", "builds": "/v1/skill-builds"}}, true)
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
	store.mark_draft_dirty("int main() { return 0; }")
	var controller := root.get_node_or_null("SessionController") as Node
	if controller == null:
		controller = ControllerScript.new()
		controller.name = "SessionController"
		root.add_child(controller)
	await process_frame
	var product := FakeProduct.new()
	controller.configure(null, product)
	controller.configure_draft_context({"attempt": _draft().request_context})
	controller.capability_unavailable.connect(func(capability: String, message: String) -> void: print("UNAVAILABLE ", capability, " ", message))
	await controller.request_save()
	var files: Array = product.request.get("source_bundle", {}).get("files", [])
	if files.is_empty() or str(files[0].get("content", "")) != "int main() { return 0; }" or store.draft_state != store.DraftState.CLEAN:
		push_error("保存必须提交当前编辑器文本，并用 canonical Draft 清除 dirty 状态：%s" % str(product.request))
		quit(1)
		return
	print("DRAFT_SAVE_TEST_PASS")
	quit(0)

func _draft() -> Dictionary:
	return {"request_context": {"schema_version": "1.0.0", "request_id": "req_draft_0001", "correlation_id": "corr_draft_0001", "trace_id": "trace_draft_0001", "requested_at": "2026-08-09T00:00:00Z", "actor": {"tenant_id": "tenant_demo", "actor_id": "student_demo", "actor_type": "student", "roles": ["game:player"]}, "content_ref": {"unit_id": "YAYA_FARM_001", "version": "1.0.0", "content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}, "session_id": "session_demo_0001", "draft_id": "draft_demo_0001", "skill_id": "skill_demo_0001", "revision": 1, "content_ref": {"unit_id": "YAYA_FARM_001", "version": "1.0.0", "content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}, "display_name": "Demo", "source_bundle": {"language": "CPP20", "entrypoint": "src/main.cpp", "files": [{"path": "src/main.cpp", "content": "", "content_sha256": ""}]}, "draft_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "created_at": "2026-08-09T00:00:00Z", "updated_at": "2026-08-09T00:00:00Z", "last_applied_patch_id": null, "links": {"self": "/draft", "session_workspace": "/workspace", "builds": "/v1/skill-builds"}}
