extends SceneTree

const ControllerScript := preload("res://autoload/session_controller.gd")
const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const ProductGateway := preload("res://scripts/client/product_interaction_gateway.gd")
const AgentContractFixtureLocator := preload("res://scripts/testing/agent_contract_fixture_locator.gd")


class Game:
	extends RefCounted
	var submissions: Array[Dictionary] = []

	func submit_agent_turn(
		_context: Dictionary,
		session_id: String,
		key: String,
		request: Dictionary,
	) -> Dictionary:
		submissions.append({
			"session_id": session_id, "idempotency_key": key,
			"request": request.duplicate(true),
		})
		return _failure("STOP_AFTER_REQUEST")

	func get_run(_context: Dictionary, _run_id: String) -> Dictionary:
		return _failure("UNEXPECTED_RUN_GET")

	func get_command(_context: Dictionary, _command_id: String) -> Dictionary:
		return _failure("UNEXPECTED_COMMAND_GET")

	func _failure(code: String) -> Dictionary:
		return {
			"ok": false, "status": 0, "headers": {},
			"error": {
				"scope": "CLIENT_LOCAL", "code": code, "message": code,
				"retryable": false, "data": null,
			},
		}


class Product:
	extends RefCounted
	var selected_interaction: Dictionary

	func get_interaction(_context: Dictionary, _session_id: String, interaction_id: String) -> Dictionary:
		if str(selected_interaction.get("interaction_id", "")) != interaction_id:
			return {"ok": false, "status": 404, "headers": {}, "error": {"code": "NOT_FOUND"}}
		return {"ok": true, "status": 200, "headers": {}, "value": selected_interaction.duplicate(true)}

	func list_interactions(_context: Dictionary, _session_id: String, _after_sequence: int, _limit: int) -> Dictionary:
		return {"ok": false, "status": 0, "headers": {}, "error": {"code": "UNEXPECTED_INTERACTION_GET"}}


class PresentationPlayer:
	extends Node
	func get_cursor() -> int: return 0


func _initialize() -> void:
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	var controller := root.get_node_or_null("SessionController")
	if store == null or controller == null:
		return _fail("Required production autoloads are unavailable.")
	store.persistence_enabled = false
	await process_frame
	_setup_authority(store, controller)
	var game := Game.new()
	var product := Product.new()
	product.selected_interaction = _failed_interaction()
	controller.configure(game, product, true)
	if controller.patch_decisions_enabled:
		return _fail("A legacy dependency-injection boolean bypassed the v0.6 capability authority.")
	controller.configure_skill_patch_capability(_capability(true))
	if controller.patch_decisions_enabled:
		return _fail("Backend Skill Patch capability bypassed the locally disabled M1 presentation gate.")
	var presentation_player := PresentationPlayer.new()
	root.add_child(presentation_player)
	controller.configure_world_presentation(null, presentation_player, null, true)
	controller.configure_skill_patch_capability(_capability(true, false))
	if controller.patch_decisions_enabled:
		return _fail("Skill Patch enabled while Backend M1 presentation capability was false.")
	controller.configure_skill_patch_capability(_capability(true))

	var failure_interaction := _failed_interaction()
	product.selected_interaction = failure_interaction
	var boundary := ProductGateway.new(null)
	var pinned_validation: Dictionary = boundary._validate_interaction(
		failure_interaction, str(failure_interaction.session_id), str(failure_interaction.interaction_id),
	)
	if not pinned_validation.get("ok", false):
		return _fail("The selected objective failure is not valid under the pinned AgentInteraction contract: %s" % pinned_validation)
	if not controller.register_visible_patch_failure(failure_interaction):
		return _fail("Verified terminal-failure interaction was not eligible for explicit Patch request.")
	await controller.request_ai_patch()
	if game.submissions.size() != 1:
		return _fail("Explicit Patch request did not submit exactly one Turn identity: flow=%s error=%s pending=%s active=%s" % [
			store.flow_state, store.last_error, store.pending_operations,
			controller.active_skill_tuple,
		])
	var input: Dictionary = game.submissions[0].request.input
	if input != {
		"type": "UI_ACTION",
		"action_id": "request_ai_patch",
		"selection_id": "interaction_water_001",
	}:
		return _fail("Patch request did not bind the current visible failure interaction through exact UI_ACTION: %s" % input)

	game.submissions.clear()
	controller.configure_skill_patch_capability(_capability(false))
	await controller.request_ai_patch()
	if not game.submissions.is_empty():
		return _fail("Capability false still emitted a Patch request mutation.")

	controller.configure_skill_patch_capability(_capability(true))
	var teaching_hint := _failed_interaction("teaching_agent", "hint", 3)
	if not boundary._validate_interaction(teaching_hint, teaching_hint.session_id, teaching_hint.interaction_id).ok:
		return _fail("The ordinary level-3 teaching hint fixture is not contract-valid.")
	if not controller.register_visible_patch_failure(teaching_hint):
		return _fail("A visible contract-valid teaching failure was not selectable; threshold is Backend-owned.")
	var fabricated_hint_four := _failed_interaction("teaching_agent", "hint", 4)
	if boundary._validate_interaction(fabricated_hint_four, fabricated_hint_four.session_id, fabricated_hint_four.interaction_id).ok:
		return _fail("Pinned AgentInteraction validation unexpectedly accepted fabricated hint+4.")
	if controller.register_visible_patch_failure(fabricated_hint_four):
		return _fail("Fabricated ordinary hint+4 exposed the Patch request entry.")
	var unsupported_role := _failed_interaction("world_agent", "message", null)
	if controller.register_visible_patch_failure(unsupported_role):
		return _fail("An unsupported objective-failure role exposed the Patch request entry.")
	print("SKILL_PATCH_EXPLICIT_REQUEST_TEST_PASS")
	quit(0)


func _setup_authority(store: WalnutClientStore, controller: Node) -> void:
	var visible := _failed_interaction()
	var actor: Dictionary = visible.request_context.actor.duplicate(true)
	var content: Dictionary = visible.request_context.content_ref.duplicate(true)
	var session := {
		"session_id": visible.session_id,
		"world_id": "world_demo_0001",
		"request_context": {
			"schema_version": "1.0.0", "request_id": "req_session_demo_0001",
			"trace_id": "trace_session_demo_0001", "correlation_id": "corr_session_demo_0001",
			"requested_at": "2026-08-14T01:02:03Z", "actor": actor,
			"content_ref": content,
		},
	}
	var active := {
		"activation_id": "activation_demo_0001", "skill_id": "skill_demo_0001",
		"skill_version_id": "skillver_demo_0001", "artifact_sha256": "d".repeat(64),
		"certification_id": "cert_demo_0001", "registry_revision": 1,
		"activated_at": "2026-08-14T01:02:03Z",
	}
	var bootstrap := {
		"actor": actor, "content": content,
		"world": {"world_id": "world_demo_0001"},
		"activation": {
			"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"},
			"registry_revision": 1, "active": active,
		},
	}
	controller.configure_authority(bootstrap, session)
	controller.active_skill_tuple = active
	controller.certified_build = {
		"build_id": "build_demo_0001", "skill_id": active.skill_id,
		"skill_version_id": active.skill_version_id, "status": "CERTIFIED",
		"terminal": true,
		"artifact": {"artifact_sha256": active.artifact_sha256},
		"certification": {"certification_id": active.certification_id},
	}
	store.set_authoritative_bootstrap(bootstrap)
	store.set_authoritative_session(session)
	store.set_workspace({
		"session": {"session_id": visible.session_id, "status": "ACTIVE", "last_turn_sequence": 4},
		"current_task": {"task_id": "task_demo_0001"},
	})
	store.replace_world({
		"world_id": "world_demo_0001", "revision": 1, "last_event_sequence": 1,
		"state_schema_version": "1.0.0", "state_hash": "b".repeat(64),
		"world_rules_version": "rules_demo", "state": {},
	})
	store.set_draft({
		"session_id": "session_demo_0001", "draft_id": "draft_demo_0001",
		"skill_id": "skill_demo_0001", "revision": 1,
		"draft_sha256": "c".repeat(64), "source_bundle": {
			"entrypoint": "src/main.cpp", "files": [{
				"path": "src/main.cpp", "content": "int main(){}",
				"content_sha256": "int main(){}".sha256_text(),
			}],
		},
	})
	store.set_flow(WalnutClientStore.FlowState.COMPLETED)
	store.set_objective_result({
		"objective_succeeded": false, "summary": "verified failure",
		"run_id": str(visible.feedback.run_id),
	})
	store.set_interaction_cursor(int(visible.sequence))


func _failed_interaction(
	role: String = "bug_agent",
	response_type: String = "message",
	hint_level: Variant = null,
) -> Dictionary:
	var examples := AgentContractFixtureLocator.examples_root()
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(
		examples.path_join("product-agent-interaction-page.json"),
	))
	var page: Dictionary = _normalize_numbers(parsed.value)
	var interaction: Dictionary = page.interactions[0].duplicate(true)
	interaction.role = role
	interaction.response_type = response_type
	interaction.question = "What failed?" if response_type == "question" else null
	interaction.hint_level = hint_level if response_type == "hint" else null
	interaction.skill_patch = null
	interaction.patch_decision = null
	interaction.links.skill_draft = null
	interaction.projection_source.role = role
	interaction.projection_source.response_type = response_type
	interaction.projection_source.question = interaction.question
	interaction.projection_source.hint_level = interaction.hint_level
	interaction.projection_source.skill_patch_sha256 = null
	var source_payload: Dictionary = interaction.projection_source.duplicate(true)
	source_payload.erase("source_sha256")
	interaction.projection_source.source_sha256 = ContractValidator.canonical_json_sha256_v1(source_payload)
	return interaction


func _normalize_numbers(value: Variant) -> Variant:
	if typeof(value) == TYPE_FLOAT and value == floor(value):
		return int(value)
	if value is Array:
		var normalized: Array = []
		for item in value:
			normalized.append(_normalize_numbers(item))
		return normalized
	if value is Dictionary:
		var normalized := {}
		for key in value:
			normalized[key] = _normalize_numbers(value[key])
		return normalized
	return value


func _capability(enabled: bool, presentation_enabled: bool = true) -> Dictionary:
	return {
		"world_presentation_enabled": presentation_enabled,
		"skill_patch_enabled": enabled,
		"skill_patch_constraints": {
			"request_mode": "EXPLICIT_UI_ACTION", "agent_role": "teaching_agent",
			"selection_target": "FAILED_INTERACTION",
			"scenario": "RECTIFICATION", "required_hint_level": 4,
			"operation": "UPSERT_FILE", "target": "CURRENT_ENTRYPOINT",
			"max_files": 1, "max_operations": 1,
			"requires_failed_evidence": true, "cas_required": true,
			"requires_student_confirmation": true,
			"auto_build": false, "auto_activate": false, "auto_run": false,
		},
	}


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
