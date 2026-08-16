extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")
const ProductGatewayScript := preload("res://scripts/client/product_interaction_gateway.gd")
const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")

const WORLD_HASH := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const ARTIFACT_HASH := "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


class Game:
	extends RefCounted
	var attempts := 0
	var request: Dictionary = {}
	var infrastructure_failure := false
	var run_reads := 0
	var evidence_reads := 0
	var event_reads := 0
	var snapshot_reads := 0

	func submit_agent_turn(_context: Dictionary, _session_id: String, _key: String, value: Dictionary) -> Dictionary:
		attempts += 1
		request = value.duplicate(true)
		return {"ok": true, "headers": {}, "value": {"command_id": _command_id()}}

	func get_command(_context: Dictionary, command_id: String) -> Dictionary:
		if infrastructure_failure:
			return {"ok": true, "headers": {}, "value": {
				"command_id": command_id,
				"terminal": true,
				"status": "FAILED",
				"result": null,
				"error": {"code": "PROVIDER_UNAVAILABLE"},
				"evidence_refs": [],
				"links": {"run": "/v1/runs/%s" % _run_id()},
			}}
		return {"ok": true, "headers": {}, "value": {
			"command_id": command_id,
			"terminal": true,
			"status": "REJECTED",
			"result": null,
			"error": {"code": "WORLD_RULE_REJECTED"},
			"evidence_refs": [_evidence_ref()],
			"links": {"run": "/v1/runs/%s" % _run_id()},
		}}

	func get_run(_context: Dictionary, _run_id_value: String) -> Dictionary:
		run_reads += 1
		return {"ok": true, "headers": {}, "value": {
			"run_id": _run_id(),
			"session_id": "session_demo_0001",
			"turn_id": request.turn_id,
			"command_id": _command_id(),
			"status": "FAILED",
			"terminal": true,
			"skill": _skill_binding(),
			"sandbox": {"status": "SUCCEEDED"},
			"world_application": {
				"status": "REJECTED",
				"receipt": null,
				"failure": {"code": "WORLD_RULE_REJECTED", "details": {"reason": "TASK_INCOMPLETE"}},
			},
			"agent_feedback": _feedback(),
			"evidence_refs": [_evidence_ref()],
		}}

	func get_evidence(_context: Dictionary, _evidence_id: String) -> Dictionary:
		evidence_reads += 1
		return {"ok": true, "headers": {}, "value": {
			"evidence_ref": _evidence_ref(),
			"source": {
				"source_type": "SKILL_RUN",
				"source_id": _run_id(),
				"command_id": _command_id(),
				"world_id": "world_demo_0001",
			},
			"payload": {
				"evidence_kind": "SKILL_RUN",
				"run_id": _run_id(),
				"sandbox_status": "SUCCEEDED",
				"world_status": "REJECTED",
				"intent_count": 1,
			},
		}}

	func get_world_events(_context: Dictionary, world_id: String, after_sequence: int, _limit: int) -> Dictionary:
		event_reads += 1
		return {"ok": true, "headers": {}, "value": {
			"world_id": world_id,
			"snapshot_revision": 4,
			"from_sequence": after_sequence,
			"to_sequence": after_sequence,
			"events": [],
			"next_after_sequence": after_sequence,
			"has_more": false,
		}}

	func get_world_snapshot(_context: Dictionary, _world_id: String) -> Dictionary:
		snapshot_reads += 1
		return {"ok": true, "headers": {}, "value": _snapshot()}

	func _command_id() -> String:
		return "cmd_objective_failure_%04d" % attempts

	func _run_id() -> String:
		return "run_objective_failure_%04d" % attempts

	func _evidence_ref() -> Dictionary:
		return {
			"evidence_id": "evidence_objective_failure_%04d" % attempts,
			"evidence_type": "SANDBOX_LOG",
			"created_at": "2026-08-12T00:00:00Z",
		}

	func _feedback() -> Dictionary:
		return {
			"session_id": "session_demo_0001",
			"turn_id": str(request.get("turn_id", "")),
			"command_id": _command_id(),
			"run_id": _run_id(),
			"message_key": "agent.objective_failed",
			"message": "Same objective failure feedback %s" % attempts,
			"source": "provider",
			"degraded": false,
			"fallback_reason": null,
			"evidence_refs": [_evidence_ref()],
			"completed_at": "2026-08-12T00:00:00Z",
		}

	func _skill_binding() -> Dictionary:
		return {
			"skill_id": "skill_demo_0001",
			"skill_version_id": "skillver_demo_0001",
			"artifact_sha256": ARTIFACT_HASH,
			"certification_id": "cert_demo_0001",
		}

	func _snapshot() -> Dictionary:
		return {
			"world_id": "world_demo_0001",
			"revision": 4,
			"last_event_sequence": 7,
			"state_schema_version": "1.0.0",
			"state_hash": WORLD_HASH,
			"world_rules_version": "rules",
			"state": {"plots": []},
		}


class Product:
	extends RefCounted
	var game: RefCounted
	var calls := 0
	var drift_event_timestamp := false
	var last_result: Dictionary = {}
	var requested_after_sequences: Array[int] = []
	var gateway: RefCounted

	func _init(source: RefCounted, drift_timestamp: bool = false) -> void:
		game = source
		drift_event_timestamp = drift_timestamp
		gateway = ProductGatewayScript.new(self)

	func list_interactions(context: Dictionary, session_id: String, after_sequence: int, limit: int) -> Dictionary:
		calls += 1
		requested_after_sequences.append(after_sequence)
		last_result = await gateway.list_interactions(context, session_id, after_sequence, limit)
		return last_result.duplicate(true)

	func execute(operation: String, arguments: Dictionary) -> Dictionary:
		await Engine.get_main_loop().process_frame
		if operation != "list_product_agent_interactions":
			return {"ok": false, "status": 500, "headers": {}, "error": {"code": "UNEXPECTED_OPERATION"}}
		var context: Dictionary = arguments.attempt_context.duplicate(true)
		var session_id := str(arguments.session_id)
		var after_sequence := int(arguments.after_sequence)
		var limit := int(arguments.limit)
		var role := "bug_agent" if game.attempts >= 3 else "teaching_agent"
		var response_type := "message" if role == "bug_agent" else "hint"
		var interaction_id := "interaction_objective_failure_%04d" % game.attempts
		var feedback: Dictionary = game._feedback()
		var feedback_sha256 := ContractValidator.canonical_json_sha256_v1(feedback)
		var occurred_at := str(feedback.completed_at)
		if drift_event_timestamp:
			occurred_at = occurred_at.trim_suffix("Z") + "+00:00"
		var feedback_event := {
			"event_id": "evt_objective_failure_%04d" % game.attempts,
			"event_type": "agent.turn.feedback_ready",
			"event_version": 1,
			"schema_version": "1.0.0",
			"stream_id": "agent-session:%s" % session_id,
			"sequence": after_sequence + 1,
			"occurred_at": occurred_at,
			"producer": "walnut_agent_runtime",
			"trace_id": context.trace_id,
			"command_id": game._command_id(),
			"correlation_id": context.correlation_id,
			"causation_id": game._command_id(),
			"content_ref": context.content_ref.duplicate(true),
			"feedback_sha256": feedback_sha256,
		}
		var projection_source := {
			"receipt_id": "receipt_objective_failure_%04d" % game.attempts,
			"source_type": "AGENT_TURN_PRODUCT_PROJECTION",
			"source_revision": 1,
			"actor": context.actor.duplicate(true),
			"content_ref": context.content_ref.duplicate(true),
			"interaction_id": interaction_id,
			"session_id": session_id,
			"turn_id": game.request.turn_id,
			"sequence": after_sequence + 1,
			"command_id": game._command_id(),
			"feedback_event_id": feedback_event.event_id,
			"feedback_sha256": feedback_sha256,
			"role": role,
			"response_type": response_type,
			"question": null,
			"hint_level": null if role == "bug_agent" else 2,
			"skill_patch_sha256": null,
			"committed_at": "2026-08-12T00:00:01Z",
		}
		projection_source["source_sha256"] = ContractValidator.canonical_json_sha256_v1(projection_source)
		var interaction := {
			"request_context": context.duplicate(true),
			"interaction_id": interaction_id,
			"session_id": session_id,
			"turn_id": game.request.turn_id,
			"sequence": after_sequence + 1,
			"interaction_revision": 1,
			"projection_source": projection_source,
			"role": role,
			"response_type": response_type,
			"question": null,
			"hint_level": null if role == "bug_agent" else 2,
			"feedback": feedback,
			"feedback_event": feedback_event,
			"skill_patch": null,
			"patch_decision": null,
			"created_at": "2026-08-12T00:00:01Z",
			"updated_at": "2026-08-12T00:00:01Z",
			"links": {
				"self": "/product-experience/v1/sessions/%s/agent-interactions/%s" % [session_id, interaction_id],
				"session_workspace": "/product-experience/v1/sessions/%s/workspace" % session_id,
				"skill_draft": null,
			},
		}
		return {"ok": true, "status": 200, "headers": {"x-interaction-high-watermark": str(after_sequence + 1)}, "value": {
			"request_context": context,
			"session_id": session_id,
			"requested_after_sequence": after_sequence,
			"requested_limit": limit,
			"high_watermark_sequence": after_sequence + 1,
			"from_sequence": after_sequence + 1,
			"to_sequence": after_sequence + 1,
			"has_more": false,
			"next_after_sequence": after_sequence + 1,
			"interactions": [interaction],
		}}


func _initialize() -> void:
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	if store == null:
		store = StoreScript.new()
		store.name = "ClientStore"
		root.add_child(store)
	var controller := root.get_node_or_null("SessionController") as Node
	if controller == null:
		controller = ControllerScript.new()
		controller.name = "SessionController"
		root.add_child(controller)
	await process_frame
	store.persistence_enabled = false
	store.set_authoritative_bootstrap(_bootstrap())
	store.set_authoritative_session({
		"session_id": "session_demo_0001",
		"world_id": "world_demo_0001",
		"content": _bootstrap().content,
	})
	store.replace_world(_snapshot())
	var game := Game.new()
	var product := Product.new(game)
	controller.configure(game, product)
	controller.configure_polling({
		"initial_delay_seconds": 0.0,
		"base_delay_seconds": 0.0,
		"max_delay_seconds": 0.0,
		"jitter_ratio": 0.0,
		"interaction_delay_seconds": 0.0,
		"interaction_deadline_seconds": 0.2,
	})
	controller.configure_authority(_bootstrap(), {
		"session_id": "session_demo_0001",
		"world_id": "world_demo_0001",
		"content": _bootstrap().content,
	})
	var displayed: Array[Dictionary] = []
	controller.interactions_recovered.connect(func(values: Array[Dictionary]) -> void:
		if not values.is_empty():
			displayed.append(values.back().duplicate(true))
	)

	for index in range(3):
		store.set_workspace({
			"session": {
				"session_id": "session_demo_0001",
				"status": "ACTIVE",
				"last_turn_sequence": index,
			},
			"current_task": {"task_id": "task_demo_0001"},
			"last_interaction_sequence": index,
		})
		await controller.request_turn()
		if (
			store.flow_state != WalnutClientStore.FlowState.COMPLETED
			or not store.get_pending_operation("agent_turn").is_empty()
			or store.world_snapshot != _snapshot()
			or bool(store.objective_result.get("objective_succeeded", true))
		):
			_abort("Objective failure did not close as verified feedback without a World commit: %s" % str(store.last_error))
			return

	var displayed_roles: Array[String] = []
	for interaction in displayed:
		displayed_roles.append(str(interaction.get("role", "")))
	var bug_interaction: Dictionary = displayed.back() if not displayed.is_empty() else {}
	if (
		displayed_roles != ["teaching_agent", "teaching_agent", "bug_agent"]
		or str(bug_interaction.get("response_type", "")) != "message"
		or bug_interaction.get("question") != null
		or bug_interaction.get("hint_level") != null
		or game.run_reads != 3
		or game.evidence_reads != 3
		or game.event_reads != 3
		or game.snapshot_reads != 3
		or product.calls != 3
		or store.last_interaction_sequence != 3
		or str(bug_interaction.get("feedback", {}).get("completed_at", "")) != "2026-08-12T00:00:00Z"
		or str(bug_interaction.get("feedback_event", {}).get("occurred_at", "")) != "2026-08-12T00:00:00Z"
	):
		_abort("REJECTED Commands with FAILED Runs must consume equal UTC-Z timestamps and advance the exact Product cursor: %s" % str(displayed_roles))
		return

	# Provider/worker failure is not an objective failure even if a Run link is
	# present. It must fail loud without reading/displaying product projection.
	game.infrastructure_failure = true
	store.set_workspace({
		"session": {"session_id": "session_demo_0001", "status": "ACTIVE", "last_turn_sequence": 3},
		"current_task": {"task_id": "task_demo_0001"},
		"last_interaction_sequence": 3,
	})
	await controller.request_turn()
	if (
		str(store.last_error.get("code", "")) != "TURN_COMMAND_FAILED"
		or store.flow_state != WalnutClientStore.FlowState.ERROR
		or game.run_reads != 3
		or game.evidence_reads != 3
		or game.event_reads != 3
		or game.snapshot_reads != 3
		or product.calls != 3
		or displayed.size() != 3
		or not store.get_pending_operation("agent_turn").is_empty()
	):
		_abort("Infrastructure/provider terminal failure was confused with verified objective failure feedback.")
		return

	# A semantically equal but byte-drifted offset is rejected by the real Product
	# gateway. SessionController must surface that contract failure without
	# emitting the interaction or advancing ClientStore's verified cursor.
	game.infrastructure_failure = false
	var drift_product := Product.new(game, true)
	controller.configure(game, drift_product)
	store.set_workspace({
		"session": {"session_id": "session_demo_0001", "status": "ACTIVE", "last_turn_sequence": 4},
		"current_task": {"task_id": "task_demo_0001"},
		"last_interaction_sequence": 3,
	})
	await controller.request_turn()
	var drift_retried_from_verified_cursor := not drift_product.requested_after_sequences.is_empty()
	for requested_after in drift_product.requested_after_sequences:
		drift_retried_from_verified_cursor = drift_retried_from_verified_cursor and requested_after == 3
	if (
		drift_product.calls < 1
		or drift_product.last_result.get("ok", true)
		or int(drift_product.last_result.get("status", -1)) != 0
		or str(drift_product.last_result.get("error", {}).get("code", "")) != "PRODUCT_RESPONSE_INVALID"
		or not drift_retried_from_verified_cursor
		or store.last_interaction_sequence != 3
		or displayed.size() != 3
		or store.flow_state != WalnutClientStore.FlowState.ERROR
		or str(store.last_error.get("code", "")) != "PRODUCT_RESPONSE_INVALID"
	):
		_abort("Offset-drifted objective-failure projection was not rejected without cursor/display advancement: %s" % str(drift_product.last_result))
		return
	product.gateway = null
	drift_product.gateway = null
	print("OBJECTIVE_FAILURE_FEEDBACK_FLOW_TEST_PASS")
	quit(0)


func _bootstrap() -> Dictionary:
	return {
		"actor": {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]},
		"content": {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": "d".repeat(64)},
		"session": {"current_session_id": "session_demo_0001"},
		"activation": {
			"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"},
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


func _snapshot() -> Dictionary:
	return {
		"world_id": "world_demo_0001",
		"revision": 4,
		"last_event_sequence": 7,
		"state_schema_version": "1.0.0",
		"state_hash": WORLD_HASH,
		"world_rules_version": "rules",
		"state": {"plots": []},
	}


func _abort(message: String) -> void:
	push_error(message)
	quit(1)
