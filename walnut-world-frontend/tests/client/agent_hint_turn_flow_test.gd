extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")


class Game:
	extends RefCounted
	var request: Dictionary = {}
	func submit_agent_turn(_attempt: Dictionary, _session: String, _key: String, value: Dictionary) -> Dictionary:
		request = value.duplicate(true)
		return {"ok": true, "value": {"command_id": "cmd_hint_demo_0001"}}
	func get_command(_attempt: Dictionary, command_id: String) -> Dictionary:
		return {"ok": true, "value": {"command_id": command_id, "terminal": true, "status": "APPLIED", "links": {}}}
	func get_run(_attempt: Dictionary, _run_id: String) -> Dictionary:
		return {"ok": false}


class Product:
	extends RefCounted
	var game: RefCounted
	var referenced_run_id: Variant = null
	func _init(source: RefCounted) -> void:
		game = source
	func list_interactions(_attempt: Dictionary, _session_id: String, after_sequence: int, _limit: int) -> Dictionary:
		return {"ok": true, "value": {"interactions": [{"interaction_id": "interaction_hint_0001", "session_id": "session_demo_0001", "turn_id": game.request.turn_id, "sequence": after_sequence + 1, "feedback": {"turn_id": game.request.turn_id, "command_id": "cmd_hint_demo_0001", "run_id": referenced_run_id, "source": "provider", "degraded": false, "fallback_reason": null}}], "next_after_sequence": after_sequence + 1, "high_watermark_sequence": after_sequence + 1, "has_more": false}}


func _initialize() -> void:
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	if store == null:
		store = StoreScript.new(); store.name = "ClientStore"; root.add_child(store)
	var controller := root.get_node_or_null("SessionController") as Node
	if controller == null:
		controller = ControllerScript.new(); controller.name = "SessionController"; root.add_child(controller)
	await process_frame
	store.persistence_enabled = false
	var bootstrap := {
		"actor": {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]},
		"content": {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": "d".repeat(64)},
		"session": {"current_session_id": "session_demo_0001"},
		"activation": {"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"}, "registry_revision": 0, "active": null},
	}
	var session := {"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": bootstrap.content}
	store.set_authoritative_bootstrap(bootstrap)
	store.set_authoritative_session(session)
	store.set_workspace({"session": {"session_id": "session_demo_0001", "status": "ACTIVE", "last_turn_sequence": 3}, "current_task": {"task_id": "task_demo_0001"}})
	store.replace_world({"world_id": "world_demo_0001", "revision": 4, "last_event_sequence": 7, "state_schema_version": "1.0.0", "state_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "world_rules_version": "rules", "state": {}})
	var game := Game.new()
	var product := Product.new(game)
	controller.configure(game, product)
	controller.configure_authority(bootstrap, session)
	controller.configure_polling({"initial_delay_seconds": 0.0, "base_delay_seconds": 0.0, "max_delay_seconds": 0.0, "jitter_ratio": 0.0, "interaction_delay_seconds": 0.0, "interaction_deadline_seconds": 0.2})
	controller.configure_draft_context({"attempt": {}})
	await controller.request_hint()
	if str(game.request.input.get("type", "")) != "MESSAGE" or str(game.request.input.get("locale", "")) != "zh-CN" or not game.request.skill_bindings is Array or not game.request.skill_bindings.is_empty() or store.flow_state != WalnutClientStore.FlowState.ACTIVE:
		push_error("Hint must use a message Agent Turn without inventing an active Skill binding or Run.")
		quit(1)
		return
	var failed_run_id := "run_failed_demo_0001"
	store.set_objective_result({"objective_succeeded": false, "run_id": failed_run_id})
	product.referenced_run_id = failed_run_id
	await controller.request_hint()
	if (
		store.flow_state != WalnutClientStore.FlowState.ACTIVE
		or not store.get_pending_operation("agent_hint").is_empty()
		or store.last_interaction_sequence != 2
	):
		push_error(
			"Hint after objective failure must close against the exact referenced failed Run and clear its restart envelope: flow=%s pending=%s cursor=%s errors=%s."
			% [
				store.flow_state,
				store.get_pending_operation("agent_hint"),
				store.last_interaction_sequence,
				store.last_error,
			],
		)
		quit(1)
		return
	print("AGENT_HINT_TURN_FLOW_TEST_PASS")
	quit(0)
