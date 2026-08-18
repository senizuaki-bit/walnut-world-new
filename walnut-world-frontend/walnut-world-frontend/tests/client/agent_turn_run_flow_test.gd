extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")

const OLD_HASH := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const NEW_HASH := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class Game:
	extends RefCounted
	var request: Dictionary = {}
	var contexts: Array[Dictionary] = []
	var evidence_reads := 0
	var event_reads := 0
	var emit_event_gap := false

	func submit_agent_turn(context: Dictionary, _session: String, _key: String, value: Dictionary) -> Dictionary:
		contexts.append(context.duplicate(true))
		request = value.duplicate(true)
		return {"ok": true, "headers": {}, "value": {"command_id": "cmd_turn_demo_0001"}}

	func get_command(context: Dictionary, command_id: String) -> Dictionary:
		contexts.append(context.duplicate(true))
		return {"ok": true, "headers": {}, "value": {
			"command_id": command_id,
			"terminal": true,
			"status": "APPLIED",
			"result": {"result_type": "WORLD_COMMIT", "world_id": "world_demo_0001", "previous_revision": 4, "world_revision": 5, "first_event_sequence": 8, "last_event_sequence": 9},
			"links": {"run": "/v1/runs/run_demo_0001"},
		}}

	func get_run(context: Dictionary, _run_id: String) -> Dictionary:
		contexts.append(context.duplicate(true))
		return {"ok": true, "headers": {}, "value": {
			"run_id": "run_demo_0001",
			"session_id": "session_demo_0001",
			"turn_id": request.turn_id,
			"command_id": "cmd_turn_demo_0001",
			"status": "SUCCEEDED",
			"terminal": true,
			"skill": {"skill_id": "skill_demo_0001", "skill_version_id": "skillver_demo_0001", "artifact_sha256": "c".repeat(64), "certification_id": "cert_demo_0001"},
			"world_application": {"status": "COMMITTED", "receipt": _receipt(), "failure": null},
			"agent_feedback": _feedback(),
			"evidence_refs": [{"evidence_id": "evidence_world_demo_0001"}],
		}}

	func get_evidence(context: Dictionary, _evidence_id: String) -> Dictionary:
		contexts.append(context.duplicate(true))
		evidence_reads += 1
		return {"ok": true, "headers": {}, "value": {
			"evidence_ref": {"evidence_id": "evidence_world_demo_0001"},
			"source": {"command_id": "cmd_turn_demo_0001"},
			"payload": _world_evidence(),
		}}

	func get_world_events(context: Dictionary, _world_id: String, after_sequence: int, _limit: int) -> Dictionary:
		contexts.append(context.duplicate(true))
		event_reads += 1
		return {"ok": true, "headers": {}, "value": {
			"world_id": "world_demo_0001",
			"snapshot_revision": 5,
			"events": [
				{"event_id": "evt_demo_0008", "sequence": 9 if emit_event_gap else 8, "command_id": "cmd_turn_demo_0001"},
				{"event_id": "evt_demo_0009", "sequence": 9, "command_id": "cmd_turn_demo_0001"},
			] if after_sequence == 7 else [],
			"next_after_sequence": 9,
			"has_more": false,
		}}

	func get_world_snapshot(context: Dictionary, _world_id: String) -> Dictionary:
		contexts.append(context.duplicate(true))
		return {"ok": true, "headers": {}, "value": _snapshot(5, 9, NEW_HASH)}

	func _receipt() -> Dictionary:
		return {"world_id": "world_demo_0001", "previous_revision": 4, "world_revision": 5, "first_event_sequence": 8, "last_event_sequence": 9, "state_hash": NEW_HASH, "committed_at": "2026-08-12T00:00:00Z"}

	func _world_evidence() -> Dictionary:
		return {"evidence_kind": "WORLD_COMMIT", "world_id": "world_demo_0001", "previous_revision": 4, "world_revision": 5, "first_event_sequence": 8, "last_event_sequence": 9, "state_hash": NEW_HASH}

	func _feedback() -> Dictionary:
		return {"turn_id": request.turn_id, "command_id": "cmd_turn_demo_0001", "run_id": "run_demo_0001", "source": "provider", "degraded": false, "fallback_reason": null}

	func _snapshot(revision: int, sequence: int, state_hash: String) -> Dictionary:
		return {"world_id": "world_demo_0001", "revision": revision, "last_event_sequence": sequence, "state_schema_version": "1.0.0", "state_hash": state_hash, "world_rules_version": "rules", "state": {}}


class Product:
	extends RefCounted
	var calls := 0
	var game: RefCounted
	var mismatch_feedback := false

	func _init(source: RefCounted) -> void:
		game = source

	func list_interactions(_context: Dictionary, _session_id: String, after_sequence: int, _limit: int) -> Dictionary:
		calls += 1
		var feedback: Dictionary = game._feedback()
		if mismatch_feedback:
			feedback["message"] = "This projection does not exactly match Run.agent_feedback."
		return {"ok": true, "value": {
			"interactions": [{
				"interaction_id": "interaction_demo_0001",
				"session_id": "session_demo_0001",
				"turn_id": game.request.turn_id,
				"sequence": after_sequence + 1,
				"feedback": feedback,
			}],
			"next_after_sequence": after_sequence + 1,
			"high_watermark_sequence": after_sequence + 1,
			"has_more": false,
		}}


func _initialize() -> void:
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	if store == null:
		store = StoreScript.new(); store.name = "ClientStore"; root.add_child(store)
	var controller := root.get_node_or_null("SessionController") as Node
	if controller == null:
		controller = ControllerScript.new(); controller.name = "SessionController"; root.add_child(controller)
	await process_frame
	store.persistence_enabled = false
	store.set_authoritative_bootstrap(_bootstrap())
	store.set_authoritative_session({"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": _bootstrap().content})
	store.set_workspace({"session": {"session_id": "session_demo_0001", "status": "ACTIVE", "last_turn_sequence": 3}, "current_task": {"task_id": "task_demo_0001"}, "last_interaction_sequence": 0})
	store.replace_world(_snapshot(4, 7, OLD_HASH))
	var game := Game.new()
	var product := Product.new(game)
	controller.configure(game, product)
	controller.configure_polling({"initial_delay_seconds": 0.0, "base_delay_seconds": 0.0, "max_delay_seconds": 0.0, "jitter_ratio": 0.0, "interaction_delay_seconds": 0.0, "interaction_deadline_seconds": 0.02})
	controller.configure_authority(_bootstrap(), {"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": _bootstrap().content})
	product.mismatch_feedback = true
	await controller.request_turn()
	if (
		store.flow_state == WalnutClientStore.FlowState.COMPLETED
		or str(store.last_error.get("code", "")) != "INTERACTION_RECONCILIATION_TIMEOUT"
		or store.get_pending_operation("agent_turn").is_empty()
	):
		push_error("A successful Run must not close against a merely related Interaction.feedback payload.")
		quit(1)
		return
	product.mismatch_feedback = false
	controller.configure_polling({"initial_delay_seconds": 0.0, "base_delay_seconds": 0.0, "max_delay_seconds": 0.0, "jitter_ratio": 0.0, "interaction_delay_seconds": 0.0, "interaction_deadline_seconds": 0.2})
	await controller.request_turn()
	# Fill-in is done by the fake after submission; verify exact closed state.
	if (
		store.flow_state != WalnutClientStore.FlowState.COMPLETED
		or int(store.world_snapshot.get("revision", -1)) != 5
		or int(store.world_snapshot.get("last_event_sequence", -1)) != 9
		or game.evidence_reads != 2
		or game.event_reads != 2
		or product.calls < 1
		or str(game.request.input.get("task_id", "")) != "task_demo_0001"
		or int(game.request.client_state.client_turn_sequence) != 4
	):
		push_error("Agent Turn must close Run/Evidence/Events/Snapshot/current Interaction: %s" % str(store.last_error))
		quit(1)
		return
	var ids := {}
	for context in game.contexts:
		ids[str(context.get("request_id", ""))] = true
	if ids.size() != game.contexts.size():
		push_error("Every Game HTTP attempt must receive a fresh RequestContext.")
		quit(1)
		return
	game.emit_event_gap = true
	var gap_result: Dictionary = await controller._recover_receipt_events(
		game._receipt(), 7, "cmd_turn_demo_0001",
	)
	if gap_result.get("ok", true) or str(gap_result.get("error", {}).get("code", "")) != "WORLD_EVENTS_GAP":
		push_error("HTTP world recovery must reject a sequence gap before Snapshot replacement.")
		quit(1)
		return
	print("AGENT_TURN_RUN_FLOW_TEST_PASS")
	quit(0)


func _bootstrap() -> Dictionary:
	return {
		"actor": {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]},
		"content": {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": "d".repeat(64)},
		"activation": {"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"}, "registry_revision": 3, "active": {"activation_id": "activation_demo_0001", "skill_id": "skill_demo_0001", "skill_version_id": "skillver_demo_0001", "artifact_sha256": "c".repeat(64), "certification_id": "cert_demo_0001", "registry_revision": 3, "activated_at": "2026-08-12T00:00:00Z"}},
	}


func _snapshot(revision: int, sequence: int, state_hash: String) -> Dictionary:
	return {"world_id": "world_demo_0001", "revision": revision, "last_event_sequence": sequence, "state_schema_version": "1.0.0", "state_hash": state_hash, "world_rules_version": "rules", "state": {}}
