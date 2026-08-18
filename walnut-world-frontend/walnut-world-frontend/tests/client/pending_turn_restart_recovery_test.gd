extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")

const OLD_HASH := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const NEW_HASH := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
const ARTIFACT_HASH := "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


class Game:
	extends RefCounted
	var calls: Array[Dictionary] = []
	var terminal_failure := false

	func submit_agent_turn(_context: Dictionary, session_id: String, key: String, request: Dictionary) -> Dictionary:
		calls.append({
			"session_id": session_id,
			"key": key,
			"request": request.duplicate(true),
			"request_json": JSON.stringify(request),
		})
		return {"ok": true, "headers": {}, "value": {"command_id": "cmd_restart_demo_0001"}}

	func get_command(_context: Dictionary, command_id: String) -> Dictionary:
		if terminal_failure:
			return {"ok": true, "headers": {}, "value": {
				"command_id": command_id,
				"terminal": true,
				"status": "FAILED",
				"result": null,
				"links": {},
			}}
		return {"ok": true, "headers": {}, "value": {
			"command_id": command_id,
			"terminal": true,
			"status": "APPLIED",
			"result": {
				"result_type": "WORLD_COMMIT",
				"world_id": "world_demo_0001",
				"previous_revision": 4,
				"world_revision": 5,
				"first_event_sequence": 8,
				"last_event_sequence": 9,
			},
			"links": {"run": "/v1/runs/run_restart_demo_0001"},
		}}

	func get_run(_context: Dictionary, _run_id: String) -> Dictionary:
		return {"ok": true, "headers": {}, "value": {
			"run_id": "run_restart_demo_0001",
			"session_id": "session_demo_0001",
			"turn_id": str(calls[0].request.turn_id),
			"command_id": "cmd_restart_demo_0001",
			"status": "SUCCEEDED",
			"terminal": true,
			"skill": _skill_binding(),
			"world_application": {"status": "COMMITTED", "receipt": _receipt(), "failure": null},
			"agent_feedback": {"turn_id": "turn_restart_demo_0001", "command_id": "cmd_restart_demo_0001", "run_id": "run_restart_demo_0001", "source": "provider", "degraded": false, "fallback_reason": null},
			"evidence_refs": [{"evidence_id": "evidence_restart_demo_0001"}],
		}}

	func get_evidence(_context: Dictionary, _evidence_id: String) -> Dictionary:
		return {"ok": true, "headers": {}, "value": {
			"evidence_ref": {"evidence_id": "evidence_restart_demo_0001"},
			"source": {"command_id": "cmd_restart_demo_0001"},
			"payload": {
				"evidence_kind": "WORLD_COMMIT",
				"world_id": "world_demo_0001",
				"previous_revision": 4,
				"world_revision": 5,
				"first_event_sequence": 8,
				"last_event_sequence": 9,
				"state_hash": NEW_HASH,
			},
		}}

	func get_world_events(_context: Dictionary, _world_id: String, after_sequence: int, _limit: int) -> Dictionary:
		return {"ok": true, "headers": {}, "value": {
			"world_id": "world_demo_0001",
			"snapshot_revision": 5,
			"events": [
				{"event_id": "evt_restart_0008", "sequence": 8, "command_id": "cmd_restart_demo_0001"},
				{"event_id": "evt_restart_0009", "sequence": 9, "command_id": "cmd_restart_demo_0001"},
			] if after_sequence == 7 else [],
			"next_after_sequence": 9,
			"has_more": false,
		}}

	func get_world_snapshot(_context: Dictionary, _world_id: String) -> Dictionary:
		return {"ok": true, "headers": {}, "value": _snapshot(5, 9, NEW_HASH)}

	func _receipt() -> Dictionary:
		return {
			"world_id": "world_demo_0001",
			"previous_revision": 4,
			"world_revision": 5,
			"first_event_sequence": 8,
			"last_event_sequence": 9,
			"state_hash": NEW_HASH,
			"committed_at": "2026-08-12T00:00:00Z",
		}

	func _skill_binding() -> Dictionary:
		return {
			"skill_id": "skill_demo_0001",
			"skill_version_id": "skillver_demo_0001",
			"artifact_sha256": ARTIFACT_HASH,
			"certification_id": "cert_demo_0001",
		}

	func _snapshot(revision: int, sequence: int, state_hash: String) -> Dictionary:
		return {
			"world_id": "world_demo_0001",
			"revision": revision,
			"last_event_sequence": sequence,
			"state_schema_version": "1.0.0",
			"state_hash": state_hash,
			"world_rules_version": "rules",
			"state": {},
		}


class Product:
	extends RefCounted
	var interaction_after_sequences: Array[int] = []

	func list_interactions(_context: Dictionary, _session_id: String, after_sequence: int, _limit: int) -> Dictionary:
		interaction_after_sequences.append(after_sequence)
		return {"ok": true, "headers": {}, "value": {
			"interactions": [{
				"interaction_id": "interaction_restart_demo_0001",
				"session_id": "session_demo_0001",
				"turn_id": "turn_restart_demo_0001",
				"sequence": 3,
				"feedback": {
					"turn_id": "turn_restart_demo_0001",
					"command_id": "cmd_restart_demo_0001",
					"run_id": "run_restart_demo_0001",
					"source": "provider",
					"degraded": false,
					"fallback_reason": null,
				},
			}],
			"next_after_sequence": 3,
			"high_watermark_sequence": 3,
			"has_more": false,
		}}


func _initialize() -> void:
	var persistence_path := "user://pending_turn_restart_recovery_test.json"
	var absolute_path := ProjectSettings.globalize_path(persistence_path)
	if FileAccess.file_exists(persistence_path):
		DirAccess.remove_absolute(absolute_path)

	var existing_store := root.get_node_or_null("ClientStore")
	if existing_store != null:
		root.remove_child(existing_store)
		existing_store.free()
	var first := StoreScript.new()
	first.name = "ClientStore"
	first.persistence_enabled = false
	root.add_child(first)
	await process_frame
	first.configure_persistence(persistence_path, true, false)
	first.bind_authority("https://api.yaya.example", _bootstrap())
	first.set_authoritative_bootstrap(_bootstrap())
	first.set_authoritative_session(_session())
	first.set_interaction_cursor(2)
	first.replace_world(_snapshot(4, 7, OLD_HASH))
	var original_request := _original_request()
	var original_identity := _turn_identity("session_demo_0001", original_request)
	var original_envelope := {
		"session_id": "session_demo_0001",
		"turn_id": "turn_restart_demo_0001",
		"idempotency_key": RequestContextFactory.idempotency_key_for("createAgentTurn", "session_demo_0001:turn_restart_demo_0001"),
		"request": original_request,
		"pre_world": _snapshot(4, 7, OLD_HASH),
		"interaction_cursor_before": 2,
	}
	first.ensure_pending_operation("agent_turn", original_identity, original_envelope)
	var original_request_json := JSON.stringify(original_request)
	root.remove_child(first)
	first.free()

	var restored := StoreScript.new()
	restored.name = "ClientStore"
	restored.persistence_enabled = false
	root.add_child(restored)
	await process_frame
	if not restored.configure_persistence(persistence_path, true, true):
		_abort("Second ClientStore could not load the persisted test envelope.", absolute_path)
		return
	if restored.get_pending_operation("agent_turn") != original_envelope:
		_abort("The second ClientStore did not restore the exact pending Turn envelope.", absolute_path)
		return

	# Simulate AppRoot's canonical Workspace/Snapshot recovery after the server
	# already committed the lost-response Turn. These high-water marks must not
	# be used to synthesize client_turn_sequence=5 or a new turn_id.
	restored.set_workspace({
		"session": {
			"session_id": "session_demo_0001",
			"status": "ACTIVE",
			"last_turn_sequence": 4,
		},
		"current_task": {"task_id": "task_demo_0001"},
		"last_interaction_sequence": 3,
	})
	restored.replace_world(_snapshot(5, 9, NEW_HASH))

	var existing_controller := root.get_node_or_null("SessionController")
	if existing_controller != null:
		root.remove_child(existing_controller)
		existing_controller.free()
	var controller := ControllerScript.new()
	controller.name = "SessionController"
	root.add_child(controller)
	await process_frame
	var game := Game.new()
	var product := Product.new()
	controller.configure(game, product)
	controller.configure_polling({
		"initial_delay_seconds": 0.0,
		"base_delay_seconds": 0.0,
		"max_delay_seconds": 0.0,
		"jitter_ratio": 0.0,
		"interaction_delay_seconds": 0.0,
		"interaction_deadline_seconds": 0.2,
	})
	controller.configure_authority(_bootstrap(), _session())

	# Calling the ordinary new-Turn entry point while an envelope is pending
	# must only reconcile the old operation and then return.
	await controller.request_turn()
	if (
		game.calls.size() != 1
		or str(game.calls[0].session_id) != "session_demo_0001"
		or str(game.calls[0].key) != str(original_envelope.idempotency_key)
		or game.calls[0].request != original_request
		or str(game.calls[0].request_json) != original_request_json
		or str(game.calls[0].request.turn_id) != "turn_restart_demo_0001"
		or int(game.calls[0].request.client_state.client_turn_sequence) != 4
		or not restored.get_pending_operation("agent_turn").is_empty()
		or restored.flow_state != WalnutClientStore.FlowState.COMPLETED
		or int(restored.world_snapshot.get("revision", -1)) != 5
		or product.interaction_after_sequences != [2]
	):
		_abort("Response-loss recovery did not reuse the exact persisted identity/body/cursors.", absolute_path)
		return

	# An authoritative terminal failure is also closed and cleared; it must not
	# strand an envelope that would be replayed forever.
	game.terminal_failure = true
	var failed_request := {
		"turn_id": "turn_restart_terminal_0001",
		"expected_world_revision": 5,
		"input": {"type": "MESSAGE", "text": "hint", "locale": "zh-CN"},
		"skill_bindings": [],
		"client_state": {"last_event_sequence": 9, "client_turn_sequence": 5},
	}
	restored.ensure_pending_operation("agent_hint", _turn_identity("session_demo_0001", failed_request), {
		"session_id": "session_demo_0001",
		"turn_id": "turn_restart_terminal_0001",
		"idempotency_key": RequestContextFactory.idempotency_key_for("createAgentTurn", "session_demo_0001:turn_restart_terminal_0001"),
		"request": failed_request,
		"pre_world": _snapshot(5, 9, NEW_HASH),
		"interaction_cursor_before": 3,
	})
	await controller.request_turn()
	if (
		game.calls.size() != 2
		or game.calls[1].request != failed_request
		or not restored.get_pending_operation("agent_hint").is_empty()
		or str(restored.last_error.get("code", "")) != "TURN_COMMAND_FAILED"
	):
		_abort("Terminal pending Turn reconciliation must clear the original envelope without a new identity.", absolute_path)
		return

	DirAccess.remove_absolute(absolute_path)
	print("PENDING_TURN_RESTART_RECOVERY_TEST_PASS")
	quit(0)


func _bootstrap() -> Dictionary:
	return {
		"actor": {
			"tenant_id": "tenant_demo",
			"actor_id": "learner_demo_0001",
			"actor_type": "student",
			"roles": ["student"],
		},
		"content": {
			"unit_id": "TASK_DEMO_001",
			"version": "1.0.0",
			"content_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
		},
		"world": {
			"world_id": "world_demo_0001",
			"revision": 5,
			"last_event_sequence": 9,
			"state_hash": NEW_HASH,
		},
		"activation": {
			"scope": {
				"world_id": "world_demo_0001",
				"agent_profile_id": "profile_demo_0001",
			},
			"registry_revision": 3,
			"active": {
				"activation_id": "activation_demo_0001",
				"skill_id": "skill_demo_0001",
				"skill_version_id": "skillver_demo_0001",
				"artifact_sha256": ARTIFACT_HASH,
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


func _original_request() -> Dictionary:
	return {
		"turn_id": "turn_restart_demo_0001",
		"expected_world_revision": 4,
		"input": {"type": "ASSIGNED_TASK", "task_id": "task_demo_0001"},
		"skill_bindings": [{
			"skill_id": "skill_demo_0001",
			"skill_version_id": "skillver_demo_0001",
			"artifact_sha256": ARTIFACT_HASH,
			"certification_id": "cert_demo_0001",
		}],
		"client_state": {"last_event_sequence": 7, "client_turn_sequence": 4},
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


func _snapshot(revision: int, sequence: int, state_hash: String) -> Dictionary:
	return {
		"world_id": "world_demo_0001",
		"revision": revision,
		"last_event_sequence": sequence,
		"state_schema_version": "1.0.0",
		"state_hash": state_hash,
		"world_rules_version": "rules",
		"state": {},
	}


func _abort(message: String, absolute_path: String) -> void:
	if FileAccess.file_exists(absolute_path):
		DirAccess.remove_absolute(absolute_path)
	push_error(message)
	quit(1)
