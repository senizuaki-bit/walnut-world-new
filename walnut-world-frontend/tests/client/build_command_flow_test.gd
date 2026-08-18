extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")

class FakeProduct:
	extends RefCounted
	func upsert_draft(_attempt: Dictionary, _session_id: String, _draft_id: String, _key: String, request: Dictionary) -> Dictionary:
		await Engine.get_main_loop().process_frame
		var saved := request.duplicate(true)
		saved.merge({
			"request_context": _attempt,
			"revision": 2,
			"draft_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"created_at": "2026-08-09T00:00:00Z",
			"updated_at": "2026-08-09T00:00:01Z",
			"last_applied_patch_id": null,
			"links": {"self": "/draft", "session_workspace": "/workspace", "builds": "/v1/skill-builds"},
		}, true)
		return {"ok": true, "status": 200, "headers": {}, "value": saved}


class FakeGame:
	extends RefCounted
	var submitted_request: Dictionary = {}

	func submit_skill_build(_attempt: Dictionary, _key: String, request: Dictionary) -> Dictionary:
		submitted_request = request.duplicate(true)
		await Engine.get_main_loop().process_frame
		return {"ok": true, "status": 202, "headers": {}, "value": {"command_id": "cmd_build_0001"}}

	func get_command(_attempt: Dictionary, command_id: String) -> Dictionary:
		await Engine.get_main_loop().process_frame
		return {
			"ok": true,
			"status": 200,
			"headers": {},
			"value": {
				"command_id": command_id,
				"terminal": true,
				"status": "APPLIED",
				"result": {"result_type": "RESOURCE_CREATED", "resource_type": "SKILL_BUILD", "resource_id": "build_demo_0001", "resource_url": "/v1/skill-builds/build_demo_0001"},
			},
		}

	func get_skill_build(_attempt: Dictionary, build_id: String) -> Dictionary:
		await Engine.get_main_loop().process_frame
		return {"ok": true, "status": 200, "headers": {}, "value": {"build_id": build_id, "skill_id": "skill_demo_0001", "status": "CERTIFIED", "terminal": true, "artifact": {"source_sha256": "3df92b6369b4ea28b2e2fc684fc14516cef6f2386b3afa60f3885491bd5cdd13"}}}


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
	var game := FakeGame.new()
	controller.configure(game, FakeProduct.new())
	controller.configure_draft_context({"attempt": _draft().request_context})
	controller.configure_build_context({"compiler_profile": "YAYA_CPP20_SAFE_V1", "test_suite_version": "farm-water-v3", "requested_capabilities": ["WORLD_READ", "WATER"]})
	await controller.request_build()
	var files: Array = game.submitted_request.get("source_bundle", {}).get("files", [])
	var compiled_source := str(files[0].get("content", "")) if not files.is_empty() else ""
	var helper_source := str(files[1].get("content", "")) if files.size() == 2 else ""
	if compiled_source != "int main() { return 0; }" or helper_source != "int helper() { return 1; }\n" or store.draft_state != store.DraftState.CLEAN or store.flow_state != store.FlowState.CERTIFIED:
		push_error("Build must save the current Draft first, reconcile its Command, then expose only the terminal canonical Build.")
		quit(1)
		return
	print("BUILD_COMMAND_FLOW_TEST_PASS")
	quit(0)


func _draft() -> Dictionary:
	return {
		"request_context": {
			"schema_version": "1.0.0", "request_id": "req_build_0001", "correlation_id": "corr_build_0001", "trace_id": "trace_build_0001", "requested_at": "2026-08-09T00:00:00Z",
			"actor": {"tenant_id": "tenant_demo", "actor_id": "student_demo", "actor_type": "student", "roles": ["game:player"]},
			"content_ref": {"unit_id": "YAYA_FARM_001", "version": "1.0.0", "content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
		},
		"session_id": "session_demo_0001", "draft_id": "draft_demo_0001", "skill_id": "skill_demo_0001", "revision": 1,
		"content_ref": {"unit_id": "YAYA_FARM_001", "version": "1.0.0", "content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
		"display_name": "Demo",
		"source_bundle": {"language": "CPP20", "entrypoint": "src/main.cpp", "files": [
			{"path": "src/main.cpp", "content": "", "content_sha256": ""},
			{"path": "src/helper.cpp", "content": "int helper() { return 1; }\n", "content_sha256": "51ac77291eaad6ec31cef5a64b37e52b3227d4bf325e3fbf4b83c964565db9eb"},
		]},
		"draft_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		"created_at": "2026-08-09T00:00:00Z", "updated_at": "2026-08-09T00:00:00Z", "last_applied_patch_id": null,
		"links": {"self": "/draft", "session_workspace": "/workspace", "builds": "/v1/skill-builds"},
	}
