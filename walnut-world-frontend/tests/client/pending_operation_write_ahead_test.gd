extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")

const HASH_A := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const HASH_C := "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


class Product:
	extends RefCounted
	var writes := 0

	func get_draft(_context: Dictionary, _session_id: String, _draft_id: String) -> Dictionary:
		return {"ok": false, "status": 503, "error": {"retryable": true}}

	func upsert_draft(_context: Dictionary, _session_id: String, _draft_id: String, _key: String, _request: Dictionary) -> Dictionary:
		writes += 1
		return {"ok": false, "status": 503, "error": {"retryable": true}}


class Game:
	extends RefCounted
	var submissions := 0

	func submit_agent_turn(_context: Dictionary, _session_id: String, _key: String, _request: Dictionary) -> Dictionary:
		submissions += 1
		return {"ok": false, "status": 503, "error": {"retryable": true}}

	func get_run(_context: Dictionary, _run_id: String) -> Dictionary:
		return {"ok": false}


func _initialize() -> void:
	var existing := root.get_node_or_null("ClientStore")
	if existing != null:
		root.remove_child(existing)
		existing.free()
	var path := "user://pending_operation_write_ahead_test.json"
	var absolute_path := ProjectSettings.globalize_path(path)
	var absolute_temp := ProjectSettings.globalize_path("%s.tmp" % path)
	_remove_file(absolute_path)
	_remove_file(absolute_temp)

	var first := StoreScript.new()
	first.name = "ClientStore"
	first.persistence_enabled = false
	root.add_child(first)
	await process_frame
	if not first.configure_persistence(path, true, false):
		_abort("Could not configure the write-ahead persistence seam.", absolute_path, absolute_temp)
		return
	if not first.bind_authority("https://api.yaya.example", _bootstrap()).get("ok", false):
		_abort("Could not bind the write-ahead persistence authority.", absolute_path, absolute_temp)
		return
	first.set_authoritative_bootstrap(_bootstrap())
	first.set_authoritative_session(_session())
	first.replace_world(_snapshot())
	var original_request := {
		"turn_id": "turn_write_ahead_0001",
		"expected_world_revision": 4,
		"input": {"type": "ASSIGNED_TASK", "task_id": "task_demo_0001"},
		"skill_bindings": [_skill_binding()],
		"client_state": {"last_event_sequence": 7, "client_turn_sequence": 1},
	}
	var original_envelope := {
		"session_id": "session_demo_0001",
		"turn_id": "turn_write_ahead_0001",
		"idempotency_key": RequestContextFactory.idempotency_key_for("createAgentTurn", "session_demo_0001:turn_write_ahead_0001"),
		"request": original_request,
		"pre_world": _snapshot(),
		"interaction_cursor_before": 0,
	}
	var original_identity := _turn_identity("session_demo_0001", original_request)
	var persisted := first.ensure_pending_operation("agent_turn", original_identity, original_envelope)
	if not persisted.get("ok", false) or persisted.get("value") != original_envelope:
		_abort("ClientStore did not report successful durable envelope persistence.", absolute_path, absolute_temp)
		return
	var bytes_before_conflict := FileAccess.get_file_as_string(path)
	var conflict := first.ensure_pending_operation("agent_turn", "identity-two", {
		"idempotency_key": "idem_different_identity_0001",
		"request": {"turn_id": "turn_different_identity_0001"},
	})
	if (
		conflict.get("ok", true)
		or str(conflict.get("error", {}).get("code", "")) != "PENDING_OPERATION_IDENTITY_CONFLICT"
		or first.get_pending_operation("agent_turn") != original_envelope
		or FileAccess.get_file_as_string(path) != bytes_before_conflict
	):
		_abort("A different identity overwrote or mutated an occupied pending slot.", absolute_path, absolute_temp)
		return

	# Cross-instance replay must return the JSON-restored immutable envelope,
	# even when the caller proposes different bytes under the same identity.
	root.remove_child(first)
	first.free()
	var restored := StoreScript.new()
	restored.name = "ClientStore"
	restored.persistence_enabled = false
	root.add_child(restored)
	await process_frame
	if not restored.configure_persistence(path, true, true):
		_abort("Could not restore the durable write-ahead envelope.", absolute_path, absolute_temp)
		return
	var replay := restored.ensure_pending_operation("agent_turn", original_identity, {
		"idempotency_key": "idem_changed_but_must_not_replace",
		"request": {"turn_id": "turn_changed_but_must_not_replace"},
	})
	if (
		not replay.get("ok", false)
		or replay.get("value") != original_envelope
		or JSON.stringify(replay.get("value")) != JSON.stringify(original_envelope)
	):
		_abort("Same-identity replay did not preserve the byte-equivalent durable request/key.", absolute_path, absolute_temp)
		return
	if not restored.clear_pending_operation("agent_turn"):
		_abort("Could not durably clear the write-ahead test envelope.", absolute_path, absolute_temp)
		return
	var wrong_identity := restored.ensure_pending_operation("agent_turn", "identity-does-not-match-body", original_envelope)
	if wrong_identity.get("ok", true) or str(wrong_identity.get("error", {}).get("code", "")) != "PENDING_OPERATION_SEMANTIC_INVALID" or not restored.get_pending_operation("agent_turn").is_empty():
		_abort("A pending Turn with a body/key identity mismatch did not fail closed.", absolute_path, absolute_temp)
		return
	var wrong_active_request: Dictionary = original_request.duplicate(true)
	wrong_active_request.skill_bindings[0].artifact_sha256 = "d".repeat(64)
	var wrong_active_envelope: Dictionary = original_envelope.duplicate(true)
	wrong_active_envelope.request = wrong_active_request
	var wrong_active := restored.ensure_pending_operation(
		"agent_turn",
		_turn_identity("session_demo_0001", wrong_active_request),
		wrong_active_envelope,
	)
	if wrong_active.get("ok", true) or str(wrong_active.get("error", {}).get("code", "")) != "PENDING_OPERATION_SEMANTIC_INVALID" or not restored.get_pending_operation("agent_turn").is_empty():
		_abort("A pending Turn with a stale/non-active Skill tuple did not fail closed.", absolute_path, absolute_temp)
		return
	var wrong_world_envelope: Dictionary = original_envelope.duplicate(true)
	wrong_world_envelope.pre_world.world_id = "world_other_0001"
	var wrong_world := restored.ensure_pending_operation(
		"agent_turn",
		original_identity,
		wrong_world_envelope,
	)
	if wrong_world.get("ok", true) or str(wrong_world.get("error", {}).get("code", "")) != "PENDING_OPERATION_SEMANTIC_INVALID" or not restored.get_pending_operation("agent_turn").is_empty():
		_abort("A pending Turn bound to another World did not fail closed.", absolute_path, absolute_temp)
		return

	# Point at a non-existent parent directory to force FileAccess.open to fail.
	# Both Draft and Turn must stop before their first network write.
	var missing_path := "user://missing_write_ahead_%s/state.json" % Time.get_ticks_usec()
	restored.configure_persistence(missing_path, true, false)
	restored.set_draft(_draft())
	restored.mark_draft_dirty("int main() { return 7; }")
	restored.set_workspace({
		"session": {"session_id": "session_demo_0001", "status": "ACTIVE", "last_turn_sequence": 0},
		"current_task": {"task_id": "task_demo_0001"},
		"last_interaction_sequence": 0,
	})
	restored.replace_world(_snapshot())
	var controller := ControllerScript.new()
	controller.name = "SessionController"
	root.add_child(controller)
	await process_frame
	var game := Game.new()
	var product := Product.new()
	controller.configure(game, product)
	controller.configure_authority(_bootstrap(), _session())
	controller.configure_draft_context({"attempt": {}})
	var draft_result: Dictionary = await controller.request_save()
	if (
		draft_result.get("ok", true)
		or str(draft_result.get("error", {}).get("code", "")) != "PENDING_OPERATION_PERSISTENCE_FAILED"
		or product.writes != 0
		or not restored.get_pending_operation("draft_save").is_empty()
	):
		_abort("Draft issued a network write without atomically persisting its envelope.", absolute_path, absolute_temp)
		return
	await controller.request_turn()
	if (
		game.submissions != 0
		or str(restored.last_error.get("code", "")) != "PENDING_OPERATION_PERSISTENCE_FAILED"
		or not restored.get_pending_operation("agent_turn").is_empty()
	):
		_abort("Turn issued a network write without atomically persisting its envelope.", absolute_path, absolute_temp)
		return
	_remove_file(absolute_path)
	_remove_file(absolute_temp)
	print("PENDING_OPERATION_WRITE_AHEAD_TEST_PASS")
	quit(0)


func _bootstrap() -> Dictionary:
	return {
		"actor": {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]},
		"content": {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": "d".repeat(64)},
		"activation": {
			"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"},
			"registry_revision": 3,
			"active": {
				"activation_id": "activation_demo_0001",
				"skill_id": "skill_demo_0001",
				"skill_version_id": "skillver_demo_0001",
				"artifact_sha256": HASH_C,
				"certification_id": "cert_demo_0001",
				"registry_revision": 3,
				"activated_at": "2026-08-12T00:00:00Z",
			},
		},
	}


func _session() -> Dictionary:
	return {
		"session_id": "session_demo_0001",
		"world_id": "world_demo_0001",
		"learner_id": "learner_demo_0001",
		"agent_profile_id": "profile_demo_0001",
		"channel": "GAME",
		"content": _bootstrap().content,
	}


func _skill_binding() -> Dictionary:
	return {
		"skill_id": "skill_demo_0001",
		"skill_version_id": "skillver_demo_0001",
		"artifact_sha256": HASH_C,
		"certification_id": "cert_demo_0001",
	}


func _turn_identity(session_id: String, request: Dictionary) -> String:
	return JSON.stringify({
		"session_id": session_id,
		"world_revision": request.expected_world_revision,
		"last_event_sequence": request.client_state.last_event_sequence,
		"client_turn_sequence": request.client_state.client_turn_sequence,
		"input": request.input,
		"skill_bindings": request.skill_bindings,
	}).sha256_text()


func _snapshot() -> Dictionary:
	return {
		"world_id": "world_demo_0001",
		"revision": 4,
		"last_event_sequence": 7,
		"state_schema_version": "1.0.0",
		"state_hash": HASH_A,
		"world_rules_version": "rules",
		"state": {},
	}


func _draft() -> Dictionary:
	var source := "int main() { return 0; }"
	return {
		"session_id": "session_demo_0001",
		"draft_id": "draft_demo_0001",
		"skill_id": "skill_demo_0001",
		"revision": 1,
		"draft_sha256": HASH_A,
		"display_name": "Demo",
		"content_ref": _bootstrap().content,
		"source_bundle": {
			"language": "CPP20",
			"entrypoint": "src/main.cpp",
			"files": [{"path": "src/main.cpp", "content": source, "content_sha256": source.sha256_text()}],
		},
	}


func _remove_file(path: String) -> void:
	if FileAccess.file_exists(path):
		DirAccess.remove_absolute(path)


func _abort(message: String, absolute_path: String, absolute_temp: String) -> void:
	_remove_file(absolute_path)
	_remove_file(absolute_temp)
	push_error(message)
	quit(1)
