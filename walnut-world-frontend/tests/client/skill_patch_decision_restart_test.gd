extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")
const AgentContractFixtureLocator := preload("res://scripts/testing/agent_contract_fixture_locator.gd")


class Product:
	extends RefCounted
	var proposal: Dictionary
	var accepted_draft: Dictionary
	var mode := "COMMIT_ACK_LOST"
	var writes: Array[Dictionary] = []
	var interaction_reads := 0
	var draft_reads := 0

	func _init(proposal_value: Dictionary, accepted_value: Dictionary) -> void:
		proposal = proposal_value.duplicate(true)
		accepted_draft = accepted_value.duplicate(true)

	func record_patch_decision(
		_attempt: Dictionary,
		_session_id: String,
		_interaction_id: String,
		_patch_id: String,
		key: String,
		request: Dictionary,
		request_body: String,
	) -> Dictionary:
		writes.append({
			"key": key,
			"request": request.duplicate(true),
			"request_body": request_body,
		})
		var receipt := _receipt(request)
		if mode == "COMMIT_ACK_LOST" and writes.size() == 1:
			proposal.patch_decision = receipt.duplicate(true)
			return _lost_response()
		if mode == "UNCOMMITTED_ACK_LOST" and writes.size() == 1:
			return _lost_response()
		if mode == "ALWAYS_UNCOMMITTED_LOSS":
			return _lost_response()
		proposal.patch_decision = receipt.duplicate(true)
		return {"ok": true, "status": 200, "headers": {}, "value": receipt}

	func get_interaction(_attempt: Dictionary, _session_id: String, _interaction_id: String) -> Dictionary:
		interaction_reads += 1
		return {"ok": true, "status": 200, "headers": {}, "value": proposal.duplicate(true)}

	func get_draft(_attempt: Dictionary, _session_id: String, _draft_id: String) -> Dictionary:
		draft_reads += 1
		return {"ok": true, "status": 200, "headers": {}, "value": accepted_draft.duplicate(true)}

	func _lost_response() -> Dictionary:
		return {
			"ok": false, "status": 0, "headers": {},
			"error": {"code": "RESPONSE_LOST", "retryable": true},
		}

	func _receipt(request: Dictionary) -> Dictionary:
		var accepted := str(request.decision) == "ACCEPT"
		return {
			"decision_id": request.decision_id,
			"session_id": request.session_id,
			"turn_id": request.turn_id,
			"interaction_id": request.interaction_id,
			"interaction_revision_before": request.expected_interaction_revision,
			"interaction_revision_after": request.expected_interaction_revision + 1,
			"patch_id": request.patch_id,
			"patch_sha256": request.patch_sha256,
			"draft_id": request.draft_id,
			"skill_id": request.skill_id,
			"decision": request.decision,
			"reason_code": request.reason_code,
			"draft_updated": accepted,
			"draft_revision_before": request.base_draft_revision,
			"draft_sha256_before": request.base_draft_sha256,
			"draft_revision_after": request.base_draft_revision + (1 if accepted else 0),
			"draft_sha256_after": request.result_draft_sha256 if accepted else request.base_draft_sha256,
		}


func _initialize() -> void:
	var proposal: Dictionary = _example("product-agent-interaction-page.json").interactions[0]
	var base: Dictionary = _example("product-skill-draft-base.json")
	var accepted_draft: Dictionary = _example("product-skill-draft.json")

	# ACK lost after Backend COMMIT: an actual ClientStore reload must retain the
	# exact serialized body, GET the canonical receipt, and never replay POST.
	var committed_path := "user://skill_patch_decision_committed_restart.json"
	_cleanup(committed_path)
	var first := await _install_store(committed_path, false)
	if first == null:
		return _abort("First durable ClientStore could not be installed.", committed_path)
	_prepare_store(first, base, proposal)
	var committed_product := Product.new(proposal, accepted_draft)
	var first_controller := await _install_controller(committed_product, proposal)
	var first_result: Dictionary = await first_controller.decide_patch(proposal, "ACCEPT")
	var slot := "patch_decision:%s" % str(proposal.skill_patch.patch_id)
	var first_envelope: Dictionary = first.get_pending_operation(slot)
	if (
		first_result.get("ok", true)
		or committed_product.writes.size() != 1
		or first_envelope.is_empty()
		or str(first_envelope.get("request_body", "")) != str(committed_product.writes[0].request_body)
	):
		return _abort("Committed response loss did not retain the first exact request bytes.", committed_path)
	var first_raw_body := str(first_envelope.request_body)
	_remove_node("SessionController")
	_remove_node("ClientStore")
	var restored := await _install_store(committed_path, true)
	if restored == null or str(restored.get_pending_operation(slot).get("request_body", "")) != first_raw_body:
		return _abort("Persisted PatchDecision raw body changed across ClientStore reload.", committed_path)
	var restored_controller := await _install_controller(committed_product, proposal)
	var committed_recovery: Dictionary = await restored_controller.recover_pending_patch_decisions()
	if (
		not committed_recovery.get("ok", false)
		or committed_product.writes.size() != 1
		or committed_product.interaction_reads != 1
		or committed_product.draft_reads != 1
		or restored.draft != accepted_draft
		or not restored.get_pending_operation(slot).is_empty()
	):
		return _abort("Committed ACK-loss recovery replayed POST or failed canonical Draft closure: %s" % committed_recovery, committed_path)
	var invalidation: Variant = restored.get("patch_activation_invalidation")
	if (
		not invalidation is Dictionary
		or invalidation.is_empty()
		or not restored.active_skill_tuple.is_empty()
		or not restored_controller.active_skill_tuple.is_empty()
	):
		return _abort("ACCEPT did not durably invalidate the previously active Skill tuple.", committed_path)

	# A clean process restart must not recover Bootstrap's stale pre-Patch active
	# tuple. The invalidation remains authoritative through a second clean restart;
	# Run is blocked until the student performs a fresh Build and Activation for
	# the accepted Draft provenance.
	_remove_node("SessionController")
	_remove_node("ClientStore")
	var clean_restart_one := await _install_store(committed_path, true)
	if clean_restart_one == null:
		return _abort("Accepted Draft invalidation did not survive the first clean restart.", committed_path)
	var clean_controller_one := await _install_controller(committed_product, proposal)
	var blocked_one: Dictionary = await clean_controller_one.request_submit_and_run()
	if (
		not clean_restart_one.active_skill_tuple.is_empty()
		or not clean_controller_one.active_skill_tuple.is_empty()
		or str(blocked_one.get("stage", "")) != "ACTIVATE"
	):
		return _abort("First clean restart restored the stale Activation or allowed Run: %s" % blocked_one, committed_path)
	_remove_node("SessionController")
	_remove_node("ClientStore")
	var clean_restart_two := await _install_store(committed_path, true)
	if clean_restart_two == null:
		return _abort("Accepted Draft invalidation did not survive the second clean restart.", committed_path)
	var clean_controller_two := await _install_controller(committed_product, proposal)
	var blocked_two: Dictionary = await clean_controller_two.request_submit_and_run()
	if (
		not clean_restart_two.get("patch_activation_invalidation") is Dictionary
		or clean_restart_two.get("patch_activation_invalidation").is_empty()
		or not clean_restart_two.active_skill_tuple.is_empty()
		or not clean_controller_two.active_skill_tuple.is_empty()
		or str(blocked_two.get("stage", "")) != "ACTIVATE"
	):
		return _abort("Second clean restart restored the stale Activation or allowed Run: %s" % blocked_two, committed_path)
	clean_restart_two.set_draft(accepted_draft)
	var new_active := _new_active_tuple()
	var built_authority := _accepted_build_authority(accepted_draft)
	var drifted_authority := built_authority.duplicate(true)
	drifted_authority.draft_sha256 = "0".repeat(64)
	if (
		clean_restart_two.update_activation_authority(
			{"world_id": "world_patch_0001", "agent_profile_id": "profile_patch_0001"},
			6,
			new_active,
			drifted_authority,
		)
		or clean_restart_two.patch_activation_invalidation.is_empty()
	):
		return _abort("Wrong Build/Draft provenance cleared the accepted-Draft invalidation.", committed_path)
	if not clean_restart_two.update_activation_authority(
		{"world_id": "world_patch_0001", "agent_profile_id": "profile_patch_0001"},
		6,
		new_active,
		built_authority,
	):
		return _abort("Exact fresh Build+Activation provenance did not close the invalidation.", committed_path)
	_remove_node("SessionController")
	_remove_node("ClientStore")
	var activated_restart := await _install_store(committed_path, true)
	if (
		activated_restart == null
		or not activated_restart.patch_activation_invalidation.is_empty()
		or activated_restart.active_skill_tuple != new_active
	):
		return _abort("Fresh manual Activation authority did not survive a clean restart.", committed_path)
	_cleanup(committed_path)

	# ACK lost before COMMIT: restart first GETs the Interaction, then replays
	# the same key and the exact persisted raw body once. REJECT mutates no local
	# Draft/Build/Activation/Run readiness.
	var replay_path := "user://skill_patch_decision_uncommitted_restart.json"
	_cleanup(replay_path)
	_remove_node("SessionController")
	_remove_node("ClientStore")
	var replay_first := await _install_store(replay_path, false)
	if replay_first == null:
		return _abort("Replay ClientStore could not be installed.", replay_path)
	_prepare_store(replay_first, base, proposal)
	var replay_product := Product.new(proposal, accepted_draft)
	replay_product.mode = "UNCOMMITTED_ACK_LOST"
	var replay_controller := await _install_controller(replay_product, proposal)
	_seed_lifecycle(replay_controller)
	replay_first.set_flow(WalnutClientStore.FlowState.ACTIVE)
	var before_reject := _fingerprint(replay_first, replay_controller)
	var rejected_first: Dictionary = await replay_controller.decide_patch(proposal, "REJECT", "STUDENT_REJECTED")
	var replay_envelope: Dictionary = replay_first.get_pending_operation(slot)
	if rejected_first.get("ok", true) or replay_product.writes.size() != 1 or replay_envelope.is_empty():
		return _abort("Uncommitted response loss did not retain a pending REJECT.", replay_path)
	var replay_key := str(replay_product.writes[0].key)
	var replay_body := str(replay_product.writes[0].request_body)
	_remove_node("SessionController")
	_remove_node("ClientStore")
	var replay_restored := await _install_store(replay_path, true)
	if replay_restored == null:
		return _abort("Uncommitted pending REJECT did not reload.", replay_path)
	var replay_restored_controller := await _install_controller(replay_product, proposal)
	_seed_lifecycle(replay_restored_controller)
	replay_restored.set_draft(base)
	replay_restored.set_flow(WalnutClientStore.FlowState.ACTIVE)
	var replay_before := _fingerprint(replay_restored, replay_restored_controller)
	var replayed: Dictionary = await replay_restored_controller.recover_pending_patch_decisions()
	if (
		not replayed.get("ok", false)
		or replay_product.writes.size() != 2
		or str(replay_product.writes[1].key) != replay_key
		or str(replay_product.writes[1].request_body) != replay_body
		or replay_product.writes[1].request != replay_product.writes[0].request
		or not replay_restored.get_pending_operation(slot).is_empty()
		or _fingerprint(replay_restored, replay_restored_controller) != replay_before
		or before_reject.draft != replay_before.draft
	):
		return _abort("Uncommitted REJECT was not replayed with exact bytes and zero lifecycle mutation: %s" % replayed, replay_path)

	# Same stable decision identity with different immutable payload is blocked
	# locally. Corrupting the persisted body/hash is also fail-closed before any
	# Gateway read or write.
	_cleanup(replay_path)
	_remove_node("SessionController")
	_remove_node("ClientStore")
	var corrupt_store := await _install_store(replay_path, false)
	if corrupt_store == null:
		return _abort("Corruption test ClientStore could not be installed.", replay_path)
	_prepare_store(corrupt_store, base, proposal)
	var corrupt_product := Product.new(proposal, accepted_draft)
	corrupt_product.mode = "ALWAYS_UNCOMMITTED_LOSS"
	var corrupt_controller := await _install_controller(corrupt_product, proposal)
	var pending_result: Dictionary = await corrupt_controller.decide_patch(proposal, "REJECT", "STUDENT_REJECTED")
	var conflict: Dictionary = await corrupt_controller.decide_patch(proposal, "REJECT", "STUDENT_DECLINED")
	if (
		pending_result.get("ok", true)
		or conflict.get("ok", true)
		or str(conflict.get("error", {}).get("code", "")) != "PATCH_DECISION_PAYLOAD_CONFLICT"
		or corrupt_product.writes.size() != 1
	):
		return _abort("Same PatchDecision identity accepted different immutable payload.", replay_path)
	corrupt_store.pending_operations[slot].envelope.request_body += " "
	var reads_before := corrupt_product.interaction_reads
	var writes_before := corrupt_product.writes.size()
	var corrupt: Dictionary = await corrupt_controller.recover_pending_patch_decisions()
	if (
		corrupt.get("ok", true)
		or corrupt_product.interaction_reads != reads_before
		or corrupt_product.writes.size() != writes_before
	):
		return _abort("Corrupt raw PatchDecision body was not rejected before Gateway access.", replay_path)

	_cleanup(replay_path)
	print("SKILL_PATCH_DECISION_RESTART_TEST_PASS")
	quit(0)


func _install_store(path: String, load_existing: bool) -> WalnutClientStore:
	_remove_node("ClientStore")
	var store := StoreScript.new()
	store.name = "ClientStore"
	store.persistence_enabled = false
	root.add_child(store)
	await process_frame
	if not store.configure_persistence(path, true, load_existing):
		return null
	return store


func _install_controller(product: Product, proposal: Dictionary) -> Node:
	_remove_node("SessionController")
	var controller := ControllerScript.new()
	controller.name = "SessionController"
	root.add_child(controller)
	await process_frame
	controller.configure(null, product)
	controller.configure_authority(_bootstrap(proposal), _session(proposal))
	controller.configure_world_presentation(null, null, null, true)
	controller.configure_skill_patch_capability(_capability())
	return controller


func _prepare_store(store: WalnutClientStore, draft: Dictionary, proposal: Dictionary) -> void:
	var bootstrap := _bootstrap(proposal)
	var session := _session(proposal)
	store.bind_authority("https://api.yaya.example", bootstrap)
	store.set_authoritative_bootstrap(bootstrap)
	store.set_authoritative_session(session)
	store.set_draft(draft)


func _bootstrap(proposal: Dictionary) -> Dictionary:
	return {
		"actor": proposal.request_context.actor.duplicate(true),
		"content": proposal.request_context.content_ref.duplicate(true),
		"activation": {
			"scope": {"world_id": "world_patch_0001", "agent_profile_id": "profile_patch_0001"},
			"registry_revision": 5,
			"active": _old_active_tuple(),
		},
	}


func _old_active_tuple() -> Dictionary:
	return {
		"activation_id": "activation_old_0001",
		"skill_id": "skill_water_001",
		"skill_version_id": "skill_version_old_0001",
		"artifact_sha256": "a".repeat(64),
		"certification_id": "certification_old_0001",
		"registry_revision": 5,
		"activated_at": "2026-08-07T00:00:00Z",
	}


func _new_active_tuple() -> Dictionary:
	return {
		"activation_id": "activation_new_0001",
		"skill_id": "skill_water_001",
		"skill_version_id": "skill_version_new_0001",
		"artifact_sha256": "b".repeat(64),
		"certification_id": "certification_new_0001",
		"registry_revision": 6,
		"activated_at": "2026-08-14T00:00:00Z",
	}


func _accepted_build_authority(draft: Dictionary) -> Dictionary:
	var projection: Array = []
	for file: Dictionary in draft.source_bundle.files:
		projection.append([file.path, file.content_sha256])
	return {
		"build_id": "build_new_0001",
		"session_id": draft.session_id,
		"draft_id": draft.draft_id,
		"skill_id": draft.skill_id,
		"draft_revision": draft.revision,
		"draft_sha256": draft.draft_sha256,
		"source_bundle_sha256": JSON.stringify(projection).sha256_text(),
	}


func _session(proposal: Dictionary) -> Dictionary:
	return {"session_id": proposal.session_id}


func _seed_lifecycle(controller: Node) -> void:
	controller.certified_build = {"build_id": "build_old_0001"}
	controller.active_activation = {"activation_id": "activation_old_0001"}
	controller.active_skill_tuple = {"activation_id": "activation_old_0001"}


func _fingerprint(store: WalnutClientStore, controller: Node) -> Dictionary:
	return {
		"draft": store.draft.duplicate(true),
		"source": store.local_source,
		"draft_state": store.draft_state,
		"flow": store.flow_state,
		"certified_build": controller.certified_build.duplicate(true),
		"active_activation": controller.active_activation.duplicate(true),
		"active_skill_tuple": controller.active_skill_tuple.duplicate(true),
	}


func _capability() -> Dictionary:
	return {
		"world_presentation_enabled": true,
		"skill_patch_enabled": true,
		"skill_patch_constraints": {
			"request_mode": "EXPLICIT_UI_ACTION", "selection_target": "FAILED_INTERACTION",
			"agent_role": "teaching_agent", "scenario": "RECTIFICATION",
			"required_hint_level": 4, "operation": "UPSERT_FILE",
			"target": "CURRENT_ENTRYPOINT", "max_files": 1, "max_operations": 1,
			"requires_failed_evidence": true, "cas_required": true,
			"requires_student_confirmation": true,
			"auto_build": false, "auto_activate": false, "auto_run": false,
		},
	}


func _example(file_name: String) -> Dictionary:
	var examples := AgentContractFixtureLocator.examples_root()
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(examples.path_join(file_name)))
	return _normalize_numbers(parsed.value)


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


func _remove_node(node_name: String) -> void:
	var existing := root.get_node_or_null(node_name)
	if existing != null:
		root.remove_child(existing)
		existing.free()


func _cleanup(path: String) -> void:
	for suffix in ["", ".tmp", ".bak"]:
		var absolute := ProjectSettings.globalize_path(path + suffix)
		if FileAccess.file_exists(absolute):
			DirAccess.remove_absolute(absolute)


func _abort(message: String, path: String) -> void:
	_cleanup(path)
	push_error(message)
	quit(1)
