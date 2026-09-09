extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")

const WORLD_HASH := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const EVIDENCE_HASH := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
var _presented: Array[Dictionary] = []


class Game:
	extends RefCounted
	var fail_submission := true
	var calls: Array[Dictionary] = []

	func submit_agent_turn(
		_attempt: Dictionary,
		session_id: String,
		key: String,
		request: Dictionary,
	) -> Dictionary:
		calls.append({
			"session_id": session_id,
			"key": key,
			"request": request.duplicate(true),
		})
		if fail_submission:
			return {
				"ok": false,
				"status": 0,
				"headers": {},
				"error": {
					"scope": "CLIENT_LOCAL",
					"code": "LOCAL_TRANSPORT_NETWORK_ERROR",
					"message": "synthetic response loss",
					"retryable": true,
				},
			}
		return {
			"ok": true,
			"status": 202,
			"headers": {},
			"value": {"command_id": "cmd_build_feedback_0001"},
		}

	func get_command(_attempt: Dictionary, command_id: String) -> Dictionary:
		return {
			"ok": true,
			"status": 200,
			"headers": {},
			"value": {
				"command_id": command_id,
				"terminal": true,
				"status": "APPLIED",
				"links": {},
			},
		}


class Product:
	extends RefCounted
	var game: Game

	func _init(source: Game) -> void:
		game = source

	func list_interactions(
		_attempt: Dictionary,
		_session_id: String,
		after_sequence: int,
		_limit: int,
	) -> Dictionary:
		var interactions: Array[Dictionary] = []
		if not game.fail_submission and not game.calls.is_empty():
			interactions.append({
				"interaction_id": "interaction_build_feedback_0001",
				"session_id": "session_demo_0001",
				"turn_id": str(game.calls.back().request.turn_id),
				"sequence": 1,
				"role": "teaching_agent",
				"response_type": "hint",
				"hint_level": 1,
				"question": null,
				"feedback": {
					"turn_id": str(game.calls.back().request.turn_id),
					"command_id": "cmd_build_feedback_0001",
					"run_id": null,
					"source": "provider",
					"degraded": false,
					"fallback_reason": null,
					"evidence_refs": [_evidence_ref()],
				},
			})
		var visible: Array[Dictionary] = []
		for interaction: Dictionary in interactions:
			if int(interaction.sequence) > after_sequence:
				visible.append(interaction)
		return {
			"ok": true,
			"status": 200,
			"headers": {},
			"value": {
				"interactions": visible,
				"next_after_sequence": 1 if not interactions.is_empty() else after_sequence,
				"high_watermark_sequence": 1 if not interactions.is_empty() else after_sequence,
				"has_more": false,
			},
		}

	func _evidence_ref() -> Dictionary:
		return {
			"evidence_id": "evidence_build_feedback_0001",
			"evidence_type": "TEST_REPORT",
			"created_at": "2026-09-02T00:00:00Z",
			"sha256": EVIDENCE_HASH,
			"uri": "/v1/evidence/evidence_build_feedback_0001",
		}


func _initialize() -> void:
	var persistence_path := "user://build_feedback_turn_recovery_test.json"
	var absolute_path := ProjectSettings.globalize_path(persistence_path)
	if FileAccess.file_exists(persistence_path):
		DirAccess.remove_absolute(absolute_path)
	var game := Game.new()
	var first_store := await _make_store(persistence_path, false)
	var first_controller := _make_controller(game)
	var first_result: Dictionary = await first_controller.request_build_feedback(_build())
	var pending := first_store.get_pending_operation("agent_build_feedback")
	if (
		first_result.get("ok", true)
		or pending.is_empty()
		or first_store.flow_state != WalnutClientStore.FlowState.BUILD_FAILED
		or game.calls.size() != 1
	):
		_abort("Response loss must retain one persisted Build-feedback Turn envelope.", absolute_path)
		return
	var original_request: Dictionary = game.calls[0].request.duplicate(true)
	var original_key := str(game.calls[0].key)
	root.remove_child(first_controller)
	first_controller.free()
	root.remove_child(first_store)
	first_store.free()

	var restored := await _make_store(persistence_path, true)
	var controller := _make_controller(game)
	_presented.clear()
	controller.interactions_recovered.connect(_on_interactions_recovered)
	game.fail_submission = false
	var recovered: Dictionary = await controller.recover_pending_turn_operations(
		true,
		["agent_build_feedback"],
	)
	if (
		not recovered.get("ok", false)
		or game.calls.size() != 2
		or game.calls[1].request != original_request
		or str(game.calls[1].key) != original_key
		or not restored.get_pending_operation("agent_build_feedback").is_empty()
		or restored.flow_state != WalnutClientStore.FlowState.BUILD_FAILED
		or restored.world_snapshot != _snapshot()
		or _presented.size() != 1
		or _presented[0].feedback.run_id != null
		or _presented[0].feedback.evidence_refs != _build().evidence_refs
		or _presented[0].get("skill_patch") != null
	):
		_abort("Restart recovery must reuse one Build-derived Turn and leave World/Run absent.", absolute_path)
		return

	var repeated: Dictionary = await controller.request_build_feedback(_build())
	if (
		not repeated.get("ok", false)
		or str(repeated.get("value", {}).get("outcome", "")) != "BUILD_FEEDBACK_ALREADY_COMPLETED"
		or game.calls.size() != 2
	):
		_abort("Repeated Build callbacks must recover the existing Interaction without a second Turn.", absolute_path)
		return
	DirAccess.remove_absolute(absolute_path)
	print("BUILD_FEEDBACK_TURN_RECOVERY_TEST_PASS")
	quit(0)


func _make_store(path: String, load_existing: bool) -> WalnutClientStore:
	var existing := root.get_node_or_null("ClientStore")
	if existing != null:
		root.remove_child(existing)
		existing.free()
	var store := StoreScript.new() as WalnutClientStore
	store.name = "ClientStore"
	store.persistence_enabled = false
	root.add_child(store)
	await process_frame
	if not store.configure_persistence(path, true, load_existing):
		return store
	if not load_existing:
		store.bind_authority("https://api.yaya.example", _bootstrap())
		store.set_authoritative_bootstrap(_bootstrap())
		store.set_authoritative_session(_session())
	store.set_workspace({
		"session": {
			"session_id": "session_demo_0001",
			"status": "ACTIVE",
			"last_turn_sequence": 0,
		},
		"current_task": {"task_id": "task_demo_0001"},
		"last_interaction_sequence": 0,
	})
	if store.world_snapshot.is_empty():
		store.replace_world(_snapshot())
	store.set_flow(WalnutClientStore.FlowState.BUILD_FAILED)
	return store


func _make_controller(game: Game) -> Node:
	var existing := root.get_node_or_null("SessionController")
	if existing != null:
		root.remove_child(existing)
		existing.free()
	var controller := ControllerScript.new()
	controller.name = "SessionController"
	root.add_child(controller)
	controller.configure(game, Product.new(game))
	controller.configure_authority(_bootstrap(), _session())
	controller.configure_polling({
		"initial_delay_seconds": 0.0,
		"base_delay_seconds": 0.0,
		"max_delay_seconds": 0.0,
		"jitter_ratio": 0.0,
		"interaction_delay_seconds": 0.0,
		"interaction_deadline_seconds": 0.2,
	})
	return controller


func _build() -> Dictionary:
	return {
		"build_id": "build_feedback_demo_0001",
		"skill_id": "skill_demo_0001",
		"status": "REJECTED",
		"terminal": true,
		"evidence_refs": [_evidence_ref()],
	}


func _evidence_ref() -> Dictionary:
	return {
		"evidence_id": "evidence_build_feedback_0001",
		"evidence_type": "TEST_REPORT",
		"created_at": "2026-09-02T00:00:00Z",
		"sha256": EVIDENCE_HASH,
		"uri": "/v1/evidence/evidence_build_feedback_0001",
	}


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
		"session": {"current_session_id": "session_demo_0001"},
		"world": _snapshot(),
		"activation": {
			"scope": {
				"world_id": "world_demo_0001",
				"agent_profile_id": "profile_demo_0001",
			},
			"registry_revision": 0,
			"active": null,
		},
	}


func _session() -> Dictionary:
	return {
		"session_id": "session_demo_0001",
		"world_id": "world_demo_0001",
		"content": _bootstrap().content,
	}


func _snapshot() -> Dictionary:
	return {
		"world_id": "world_demo_0001",
		"revision": 4,
		"last_event_sequence": 7,
		"state_schema_version": "1.0.0",
		"state_hash": WORLD_HASH,
		"world_rules_version": "rules",
		"state": {},
	}


func _abort(message: String, absolute_path: String) -> void:
	push_error(message)
	if FileAccess.file_exists(absolute_path):
		DirAccess.remove_absolute(absolute_path)
	quit(1)


func _on_interactions_recovered(items: Array[Dictionary]) -> void:
	_presented.append_array(items)
