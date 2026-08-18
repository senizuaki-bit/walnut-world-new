extends SceneTree

const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")
const StoreScript := preload("res://autoload/client_store.gd")
const AgentContractFixtureLocator := preload("res://scripts/testing/agent_contract_fixture_locator.gd")


class Game:
	extends RefCounted
	var submissions: Array[Dictionary] = []
	var command_gets := 0
	var run_gets := 0
	var command_id := "cmd_patch_request_0001"
	var fail_next_command_get := false
	var fail_next_submission_before_accept := false

	func submit_agent_turn(_attempt: Dictionary, session_id: String, key: String, request: Dictionary) -> Dictionary:
		submissions.append({"session_id": session_id, "key": key, "request": request.duplicate(true)})
		if fail_next_submission_before_accept:
			fail_next_submission_before_accept = false
			return {"ok": false, "status": 503, "headers": {}, "error": {"code": "TEST_ACK_LOST_BEFORE_ACCEPT", "retryable": true}}
		return {"ok": true, "status": 202, "headers": {}, "value": {"command_id": command_id}}

	func get_command(_attempt: Dictionary, requested_id: String) -> Dictionary:
		command_gets += 1
		if fail_next_command_get:
			fail_next_command_get = false
			return {"ok": false, "status": 400, "headers": {}, "error": {"code": "TEST_PROCESS_LOSS"}}
		return {"ok": true, "status": 200, "headers": {}, "value": {
			"command_id": requested_id, "command_type": "EXECUTE_AGENT_TURN",
			"status": "APPLIED", "terminal": true,
			"result": {"result_type": "NO_EFFECT", "reason_code": "SKILL_PATCH_PROPOSED"},
			"links": {"self": "/v1/commands/%s" % requested_id},
		}}

	func get_run(_attempt: Dictionary, _run_id: String) -> Dictionary:
		run_gets += 1
		return {"ok": false, "status": 0, "headers": {}, "error": {"code": "RUN_MUST_NOT_BE_READ"}}


class Product:
	extends RefCounted
	var interaction: Dictionary
	var game: Game
	var list_calls := 0
	var fail_first_list := false
	var corrupt_proposal_run_id := false
	var selected_interaction: Dictionary
	var selected_gets := 0
	var selected_missing := false
	var selected_tampered := false

	func get_interaction(_attempt: Dictionary, _session_id: String, interaction_id: String) -> Dictionary:
		selected_gets += 1
		if selected_missing:
			return {"ok": false, "status": 404, "headers": {}, "error": {"code": "NOT_FOUND", "retryable": false}}
		var selected := selected_interaction.duplicate(true)
		if selected_tampered:
			selected.feedback.message = "%s tampered" % str(selected.feedback.message)
			var feedback_hash := ContractValidator.canonical_json_sha256_v1(selected.feedback)
			selected.feedback_event.feedback_sha256 = feedback_hash
			selected.projection_source.feedback_sha256 = feedback_hash
			var source_payload: Dictionary = selected.projection_source.duplicate(true)
			source_payload.erase("source_sha256")
			selected.projection_source.source_sha256 = ContractValidator.canonical_json_sha256_v1(source_payload)
		return {"ok": true, "status": 200, "headers": {}, "value": selected} if str(selected.get("interaction_id", "")) == interaction_id else {"ok": false, "status": 404, "headers": {}, "error": {"code": "NOT_FOUND"}}

	func list_interactions(_attempt: Dictionary, session_id: String, after_sequence: int, limit: int) -> Dictionary:
		list_calls += 1
		if fail_first_list and list_calls == 1:
			return {"ok": false, "status": 503, "headers": {}, "error": {"retryable": true}}
		var projected := _project_for_latest_turn()
		return {"ok": true, "status": 200, "headers": {}, "value": {
			"session_id": session_id, "requested_after_sequence": after_sequence,
			"requested_limit": limit, "high_watermark_sequence": int(projected.sequence),
			"from_sequence": int(projected.sequence), "to_sequence": int(projected.sequence),
			"has_more": false, "next_after_sequence": int(projected.sequence),
			"interactions": [projected],
		}}

	func _project_for_latest_turn() -> Dictionary:
		var projected := interaction.duplicate(true)
		var request: Dictionary = game.submissions.back().request
		projected.turn_id = request.turn_id
		projected.feedback.turn_id = request.turn_id
		projected.feedback.command_id = game.command_id
		projected.feedback.run_id = "run_forbidden_0001" if corrupt_proposal_run_id else null
		projected.feedback_event.command_id = game.command_id
		projected.skill_patch.turn_id = request.turn_id
		projected.projection_source.turn_id = request.turn_id
		projected.projection_source.command_id = game.command_id
		var feedback_hash := ContractValidator.canonical_json_sha256_v1(projected.feedback)
		projected.feedback_event.feedback_sha256 = feedback_hash
		projected.projection_source.feedback_sha256 = feedback_hash
		var patch_payload: Dictionary = projected.skill_patch.duplicate(true)
		patch_payload.erase("patch_sha256")
		projected.skill_patch.patch_sha256 = ContractValidator.canonical_json_sha256_v1(patch_payload)
		projected.projection_source.skill_patch_sha256 = projected.skill_patch.patch_sha256
		var source_payload: Dictionary = projected.projection_source.duplicate(true)
		source_payload.erase("source_sha256")
		projected.projection_source.source_sha256 = ContractValidator.canonical_json_sha256_v1(source_payload)
		return projected


class PresentationPlayer:
	extends Node
	func get_cursor() -> int: return 0


func _initialize() -> void:
	var restart_path := "user://skill_patch_request_authority_restart_test.json"
	_remove_persistence_siblings(restart_path)
	var store: WalnutClientStore = root.get_node_or_null("ClientStore") as WalnutClientStore
	var controller: Node = root.get_node_or_null("SessionController")
	if store == null or controller == null:
		return _fail("Production autoloads unavailable.")
	store.persistence_enabled = false
	await process_frame
	var base: Dictionary = _example("product-skill-draft-base.json")
	var failure := _visible_failure()
	_setup(store, controller, base, failure)
	var game := Game.new()
	var product := Product.new()
	product.game = game
	product.selected_interaction = failure
	controller.configure(game, product)
	var presentation_player := PresentationPlayer.new()
	root.add_child(presentation_player)
	controller.configure_world_presentation(null, presentation_player, null, true)
	controller.configure_skill_patch_capability(_capability())
	if not controller.register_visible_patch_failure(failure):
		return _fail("Contract-valid visible objective failure was not selectable.")
	product.interaction = _proposal_for(failure)
	var world_before := store.world_snapshot.duplicate(true)
	var proposal_capture: Array[Dictionary] = []
	controller.interactions_recovered.connect(func(values: Array[Dictionary]) -> void:
		proposal_capture.assign(values)
	)
	var result: Dictionary = await controller.request_ai_patch()
	if (
		not result.get("ok", false)
		or game.submissions.size() != 1
		or game.run_gets != 0
		or store.world_snapshot != world_before
		or not store.get_pending_operation("agent_patch_request").is_empty()
		or proposal_capture.is_empty()
		or not controller.validate_minimal_skill_patch_interaction(proposal_capture.back()).get("ok", false)
		or proposal_capture.back().feedback.run_id != null
		or proposal_capture.back().feedback.evidence_refs != failure.feedback.evidence_refs
		or proposal_capture.back().skill_patch.evidence_refs != failure.feedback.evidence_refs
	):
		return _fail("Patch request did not close through NO_EFFECT + proposal without Run/World mutation: %s" % result)

	# Process-loss seam: terminal Command is durable, Interaction read fails, then
	# recovery must use GET command/list only and never replay POST or read Run.
	_setup(store, controller, base, failure)
	if not store.bind_authority("https://api.yaya.example", store.authoritative_bootstrap).get("ok", false):
		return _fail("Restart test could not bind the persisted public origin authority.")
	# First-time namespace binding intentionally clears any authority that was
	# present before its origin was known; repopulate only from the same fixture.
	_setup(store, controller, base, failure)
	if not store.configure_persistence(restart_path, true, false):
		return _fail("Restart test could not enable durable ClientStore persistence.")
	controller.configure(game, product)
	controller.configure_skill_patch_capability(_capability())
	controller.register_visible_patch_failure(failure)
	product.fail_first_list = true
	product.list_calls = 0
	var submit_count := game.submissions.size()
	var failed: Dictionary = await controller.request_ai_patch()
	if failed.get("ok", true) or store.get_pending_operation("agent_patch_request").is_empty():
		return _fail("Interaction response loss did not retain the durable patch-request envelope: result=%s integrity=%s pending=%s" % [failed, store.persistence_integrity_result(), store.pending_operations])
	# Process restart does not retain UI/controller memory. Recovery must use the
	# persisted selected-failure authority even when the proposal is now the
	# latest visible Interaction and the old selection memory is empty.
	root.remove_child(controller)
	controller.free()
	root.remove_child(store)
	store.free()
	var restored := StoreScript.new()
	restored.name = "ClientStore"
	restored.persistence_enabled = false
	if not restored.configure_persistence(restart_path, true, true):
		return _fail("A new process could not reload the durable selected-failure authority.")
	root.add_child(restored)
	await process_frame
	store = restored
	_restore_workspace_after_restart(store, base, failure)
	controller = ControllerScript.new()
	controller.name = "SessionController"
	root.add_child(controller)
	await process_frame
	controller.configure(game, product)
	controller.configure_authority(store.authoritative_bootstrap, store.authoritative_session)
	controller.configure_world_presentation(null, presentation_player, null, true)
	controller.configure_skill_patch_capability(_capability())
	var recovered: Dictionary = await controller.recover_pending_patch_request()
	if (
		not recovered.get("ok", false)
		or game.submissions.size() != submit_count + 1
		or game.run_gets != 0
		or store.world_snapshot != world_before
		or not store.get_pending_operation("agent_patch_request").is_empty()
	):
		return _fail("Patch-request restart recovery was not GET-only and World-inert: %s" % recovered)

	# If the first createAgentTurn response has no accepted Command identity, a
	# restart revalidates the exact selected failure by GET, then reuses the same
	# durable Turn/idempotency identity. It must not derive a request from latest UI.
	_setup(store, controller, base, failure)
	controller.configure(game, product)
	controller.configure_world_presentation(null, presentation_player, null, true)
	controller.configure_skill_patch_capability(_capability())
	controller.register_visible_patch_failure(failure)
	product.selected_interaction = failure
	product.selected_missing = false
	product.selected_tampered = false
	game.fail_next_submission_before_accept = true
	var before_unaccepted := game.submissions.size()
	var unaccepted: Dictionary = await controller.request_ai_patch()
	var unaccepted_pending: Dictionary = store.get_pending_operation("agent_patch_request")
	if unaccepted.get("ok", true) or unaccepted_pending.is_empty():
		return _fail("Unaccepted response loss did not retain one durable request authority.")
	var original_unaccepted_body := JSON.stringify(game.submissions.back().request)
	root.remove_child(controller)
	controller.free()
	root.remove_child(store)
	store.free()
	restored = StoreScript.new()
	restored.name = "ClientStore"
	restored.persistence_enabled = false
	if not restored.configure_persistence(restart_path, true, true):
		return _fail("Unaccepted request identity did not survive a ClientStore process restart.")
	root.add_child(restored)
	await process_frame
	store = restored
	_restore_workspace_after_restart(store, base, failure)
	controller = ControllerScript.new()
	controller.name = "SessionController"
	root.add_child(controller)
	await process_frame
	controller.configure(game, product)
	controller.configure_authority(store.authoritative_bootstrap, store.authoritative_session)
	controller.configure_world_presentation(null, presentation_player, null, true)
	controller.configure_skill_patch_capability(_capability())
	var unaccepted_recovery: Dictionary = await controller.recover_pending_patch_request()
	if (
		not unaccepted_recovery.get("ok", false)
		or game.submissions.size() != before_unaccepted + 2
		or game.submissions[-1].key != game.submissions[-2].key
		or game.submissions[-1].request != game.submissions[-2].request
		or JSON.stringify(game.submissions[-1].request) != original_unaccepted_body
		or game.run_gets != 0
	):
		return _fail("Unaccepted patch request was not replayed under the same durable identity after canonical GET: %s" % unaccepted_recovery)

	# A vanished or self-consistently rehashed selected failure is corruption,
	# even after the proposal Command is terminal. Recovery must fail before any
	# new mutation and preserve the pending envelope for operator-safe recovery.
	for corruption in ["MISSING", "TAMPERED"]:
		_setup(store, controller, base, failure)
		controller.configure(game, product)
		controller.configure_world_presentation(null, presentation_player, null, true)
		controller.configure_skill_patch_capability(_capability())
		controller.register_visible_patch_failure(failure)
		product.selected_interaction = failure
		product.selected_missing = false
		product.selected_tampered = false
		product.fail_first_list = true
		product.list_calls = 0
		var corrupted_start: Dictionary = await controller.request_ai_patch()
		if corrupted_start.get("ok", true) or store.get_pending_operation("agent_patch_request").is_empty():
			return _fail("Corruption precondition did not leave a terminal pending proposal request.")
		product.selected_missing = corruption == "MISSING"
		product.selected_tampered = corruption == "TAMPERED"
		controller.visible_patch_failure.clear()
		var mutation_count := game.submissions.size()
		var corrupted_recovery: Dictionary = await controller.recover_pending_patch_request()
		if (
			corrupted_recovery.get("ok", true)
			or game.submissions.size() != mutation_count
			or store.get_pending_operation("agent_patch_request").is_empty()
			or game.run_gets != 0
		):
			return _fail("Selected failure %s did not fail closed before mutation: %s" % [corruption, corrupted_recovery])
		if corruption == "TAMPERED" and str(corrupted_recovery.get("error", {}).get("code", "")) != "SKILL_PATCH_SELECTED_AUTHORITY_DRIFT":
			return _fail("Self-consistently rehashed selected failure was not detected as authority drift: %s" % corrupted_recovery)
		store.pending_operations.clear()
	product.selected_missing = false
	product.selected_tampered = false

	# Accepted Command identity must be persisted before terminal polling. If the
	# process loses the first Command GET, restart is GET-only and must not replay
	# createAgentTurn even though no terminal Command was observed yet.
	_setup(store, controller, base, failure)
	controller.configure(game, product)
	controller.configure_skill_patch_capability(_capability())
	controller.register_visible_patch_failure(failure)
	product.fail_first_list = false
	game.fail_next_command_get = true
	var accepted_submit_count := game.submissions.size()
	var interrupted: Dictionary = await controller.request_ai_patch()
	var accepted_pending: Dictionary = store.get_pending_operation("agent_patch_request")
	if (
		interrupted.get("ok", true)
		or accepted_pending.get("recovery", {}).get("phase") != "COMMAND_ACCEPTED"
	):
		return _fail("Accepted Command identity was not durable before the failed first GET: %s" % accepted_pending)
	var accepted_recovery: Dictionary = await controller.recover_pending_patch_request()
	if (
		not accepted_recovery.get("ok", false)
		or game.submissions.size() != accepted_submit_count + 1
		or game.run_gets != 0
		or store.world_snapshot != world_before
		or not store.get_pending_operation("agent_patch_request").is_empty()
	):
		return _fail("Accepted patch-request restart replayed POST or touched Run/World: %s" % accepted_recovery)

	# A no-Run proposal that nevertheless claims a Run identity is corrupted.
	# It must not be surfaced as a proposal or clear the durable request envelope.
	_setup(store, controller, base, failure)
	controller.configure(game, product)
	controller.configure_world_presentation(null, presentation_player, null, true)
	controller.configure_skill_patch_capability(_capability())
	controller.register_visible_patch_failure(failure)
	product.corrupt_proposal_run_id = true
	var corrupt_result: Dictionary = await controller.request_ai_patch()
	if (
		corrupt_result.get("ok", true)
		or str(corrupt_result.get("error", {}).get("code", "")) != "SKILL_PATCH_PROPOSAL_MISSING"
		or store.get_pending_operation("agent_patch_request").is_empty()
		or game.run_gets != 0
	):
		return _fail("Proposal with non-null Run identity did not fail closed: %s" % corrupt_result)

	_remove_persistence_siblings(restart_path)
	print("SKILL_PATCH_REQUEST_NO_RUN_TEST_PASS")
	quit(0)


func _setup(store: WalnutClientStore, controller: Node, draft: Dictionary, failure: Dictionary) -> void:
	var actor: Dictionary = failure.request_context.actor.duplicate(true)
	var content: Dictionary = failure.request_context.content_ref.duplicate(true)
	var active := {
		"activation_id": "activation_demo_0001", "skill_id": draft.skill_id,
		"skill_version_id": "skillver_demo_0001", "artifact_sha256": "d".repeat(64),
		"certification_id": "cert_demo_0001", "registry_revision": 1,
		"activated_at": "2026-08-14T01:02:03Z",
	}
	var bootstrap := {
		"actor": actor, "content": content, "world": {"world_id": "world_demo_0001"},
		"activation": {"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"}, "registry_revision": 1, "active": active},
	}
	var session := {"session_id": failure.session_id, "world_id": "world_demo_0001"}
	controller.configure_authority(bootstrap, session)
	var build: Dictionary = _example("game-skill-build.json")
	build.skill_id = active.skill_id
	build.skill_version_id = active.skill_version_id
	build.artifact.artifact_sha256 = active.artifact_sha256
	build.certification.certification_id = active.certification_id
	controller.certified_build = build
	store.set_authoritative_bootstrap(bootstrap)
	store.set_authoritative_session(session)
	store.pending_operations.clear()
	store.set_workspace({"session": {"session_id": failure.session_id, "status": "ACTIVE", "last_turn_sequence": 4}, "current_task": {"task_id": "task_demo_0001"}})
	store.replace_world({"world_id": "world_demo_0001", "revision": 3, "last_event_sequence": 7, "state_schema_version": "1.0.0", "state_hash": "f".repeat(64), "world_rules_version": "rules", "state": {}})
	store.set_draft(draft)
	store.last_interaction_sequence = int(failure.sequence)
	store.set_interaction_cursor(int(failure.sequence))
	store.set_objective_result({"objective_succeeded": false, "run_id": failure.feedback.run_id})
	store.set_flow(WalnutClientStore.FlowState.COMPLETED)


func _restore_workspace_after_restart(
	store: WalnutClientStore,
	draft: Dictionary,
	failure: Dictionary,
) -> void:
	store.set_workspace({
		"session": {
			"session_id": failure.session_id, "status": "ACTIVE",
			"last_turn_sequence": 4,
		},
		"current_task": {"task_id": "task_demo_0001"},
	})
	store.set_draft(draft)
	store.set_objective_result({
		"objective_succeeded": false, "run_id": failure.feedback.run_id,
	})
	store.set_flow(WalnutClientStore.FlowState.COMPLETED)


func _visible_failure() -> Dictionary:
	var interaction: Dictionary = _example("product-agent-interaction-page.json").interactions[0]
	interaction.role = "bug_agent"; interaction.response_type = "message"
	interaction.question = null; interaction.hint_level = null
	interaction.skill_patch = null; interaction.patch_decision = null
	interaction.links.skill_draft = null
	interaction.projection_source.role = "bug_agent"
	interaction.projection_source.response_type = "message"
	interaction.projection_source.question = null
	interaction.projection_source.hint_level = null
	interaction.projection_source.skill_patch_sha256 = null
	_rehash_projection(interaction)
	return interaction


func _proposal_for(failure: Dictionary) -> Dictionary:
	var interaction: Dictionary = _example("product-agent-interaction-page.json").interactions[0]
	interaction.interaction_id = "interaction_patch_0002"
	interaction.sequence = int(failure.sequence) + 1
	interaction.skill_patch.interaction_id = interaction.interaction_id
	interaction.projection_source.interaction_id = interaction.interaction_id
	interaction.projection_source.sequence = interaction.sequence
	interaction.links.self = "/product-experience/v1/sessions/%s/agent-interactions/%s" % [interaction.session_id, interaction.interaction_id]
	return interaction


func _rehash_projection(interaction: Dictionary) -> void:
	var payload: Dictionary = interaction.projection_source.duplicate(true)
	payload.erase("source_sha256")
	interaction.projection_source.source_sha256 = ContractValidator.canonical_json_sha256_v1(payload)


func _capability() -> Dictionary:
	return {"world_presentation_enabled": true, "skill_patch_enabled": true, "skill_patch_constraints": {"request_mode": "EXPLICIT_UI_ACTION", "selection_target": "FAILED_INTERACTION", "agent_role": "teaching_agent", "scenario": "RECTIFICATION", "required_hint_level": 4, "operation": "UPSERT_FILE", "target": "CURRENT_ENTRYPOINT", "max_files": 1, "max_operations": 1, "requires_failed_evidence": true, "cas_required": true, "requires_student_confirmation": true, "auto_build": false, "auto_activate": false, "auto_run": false}}


func _example(file_name: String) -> Dictionary:
	var examples := AgentContractFixtureLocator.examples_root()
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(examples.path_join(file_name)))
	return _normalize(parsed.value)


func _normalize(value: Variant) -> Variant:
	if typeof(value) == TYPE_FLOAT and value == floor(value): return int(value)
	if value is Array:
		var result: Array = []
		for item in value: result.append(_normalize(item))
		return result
	if value is Dictionary:
		var result := {}
		for key in value: result[key] = _normalize(value[key])
		return result
	return value


func _fail(message: String) -> void:
	push_error(message)
	quit(1)


func _remove_persistence_siblings(path: String) -> void:
	for candidate in [path, "%s.tmp" % path, "%s.bak" % path]:
		var absolute := ProjectSettings.globalize_path(candidate)
		if FileAccess.file_exists(candidate):
			DirAccess.remove_absolute(absolute)
