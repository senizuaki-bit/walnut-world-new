extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")

class Product:
	extends RefCounted
	func get_workspace(_a: Dictionary, _s: String) -> Dictionary:
		return {"ok": true, "value": {"session": {"session_id": "session_demo_0001", "status": "ACTIVE"}, "skill_draft_refs": [{"draft_id": "draft_demo_0001", "skill_id": "skill_demo_0001", "revision": 1, "draft_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}], "world_checkpoint": {"world_id": "world_demo_0001", "world_revision": 1, "last_event_sequence": 2}}}
	func get_draft(_a: Dictionary, _s: String, _d: String) -> Dictionary:
		return {"ok": true, "value": {"draft_id": "draft_demo_0001", "skill_id": "skill_demo_0001", "revision": 1, "draft_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "source_bundle": {"entrypoint": "src/main.cpp", "files": [{"path": "src/main.cpp", "content": "", "content_sha256": ""}]}}}
	func list_interactions(_a: Dictionary, _s: String, _after: int, _limit: int) -> Dictionary:
		return {"ok": true, "value": {"interactions": [], "next_after_sequence": 0, "has_more": false}}

class Game:
	extends RefCounted
	func get_world_snapshot(_a: Dictionary, _world: String) -> Dictionary:
		return {"ok": true, "value": {"world_id": "world_demo_0001", "revision": 1, "last_event_sequence": 2, "state_schema_version": "1.0.0", "state_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "world_rules_version": "rules", "state": {}}}

func _initialize() -> void:
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	if store == null:
		store = StoreScript.new(); store.name = "ClientStore"; root.add_child(store)
	var controller := root.get_node_or_null("SessionController") as Node
	if controller == null:
		controller = ControllerScript.new(); controller.name = "SessionController"; root.add_child(controller)
	await process_frame
	controller.configure(Game.new(), Product.new())
	var result: Dictionary = await controller.recover_workspace({}, "session_demo_0001")
	if not result.get("ok", false) or str(store.draft.get("draft_id", "")) != "draft_demo_0001" or str(store.world_snapshot.get("world_id", "")) != "world_demo_0001":
		push_error("Workspace recovery must atomically load canonical Draft and Snapshot: %s" % str(result))
		quit(1); return
	print("WORKSPACE_RECOVERY_TEST_PASS")
	quit(0)
