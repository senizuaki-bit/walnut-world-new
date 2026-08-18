extends SceneTree

const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const AgentContractFixtureLocator := preload("res://scripts/testing/agent_contract_fixture_locator.gd")


class Product:
	extends RefCounted
	var decisions: Array[Dictionary] = []
	var canonical_reads := 0
	var interaction_reads := 0
	var accepted_draft: Dictionary
	var proposal: Dictionary

	func _init(canonical: Dictionary, pending: Dictionary) -> void:
		accepted_draft = canonical.duplicate(true)
		proposal = pending.duplicate(true)

	func record_patch_decision(
		_attempt: Dictionary,
		_session_id: String,
		_interaction_id: String,
		_patch_id: String,
		key: String,
		request: Dictionary,
		request_body: String = "",
	) -> Dictionary:
		decisions.append({
			"key": key, "request": request.duplicate(true),
			"request_body": request_body,
		})
		return {"ok": true, "status": 200, "headers": {}, "value": _receipt(request)}

	func get_interaction(_attempt: Dictionary, _session_id: String, _interaction_id: String) -> Dictionary:
		interaction_reads += 1
		return {"ok": true, "status": 200, "headers": {}, "value": proposal.duplicate(true)}

	func get_draft(_attempt: Dictionary, _session_id: String, _draft_id: String) -> Dictionary:
		canonical_reads += 1
		return {"ok": true, "status": 200, "headers": {}, "value": accepted_draft.duplicate(true)}

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
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	var controller := root.get_node_or_null("SessionController")
	if store == null or controller == null:
		return _fail("Production autoloads are unavailable.")
	store.persistence_enabled = false
	await process_frame
	var base: Dictionary = _example("product-skill-draft-base.json")
	var accepted_draft: Dictionary = _example("product-skill-draft.json")
	var proposal: Dictionary = _example("product-agent-interaction-page.json").interactions[0]
	var corrupt_accepted_drafts: Array[Dictionary] = []
	var changed_content := accepted_draft.duplicate(true)
	changed_content.source_bundle.files[0].content += "// corrupt accepted response\n"
	corrupt_accepted_drafts.append({"label": "content hash", "value": changed_content})
	var noncanonical_path := accepted_draft.duplicate(true)
	noncanonical_path.source_bundle.files[0].path = "src/../main.cpp"
	noncanonical_path.source_bundle.entrypoint = "src/../main.cpp"
	_rehash_draft(noncanonical_path)
	corrupt_accepted_drafts.append({"label": "self-consistent invalid path", "value": noncanonical_path})
	var wrong_projection_hash := accepted_draft.duplicate(true)
	wrong_projection_hash.draft_sha256 = str(proposal.skill_patch.result_draft_sha256)
	wrong_projection_hash.display_name += " tampered"
	corrupt_accepted_drafts.append({"label": "Draft projection hash", "value": wrong_projection_hash})
	for corruption: Dictionary in corrupt_accepted_drafts:
		_setup_authority(store, controller, base, proposal)
		var corrupt_product := Product.new(corruption.value, proposal)
		controller.configure(null, corrupt_product)
		controller.configure_world_presentation(null, null, null, true)
		controller.configure_skill_patch_capability(_capability())
		_seed_old_lifecycle(controller)
		store.set_flow(WalnutClientStore.FlowState.ACTIVE)
		var before_corruption := _client_lifecycle_fingerprint(store, controller)
		var corrupt_result: Dictionary = await controller.decide_patch(proposal, "ACCEPT")
		if (
			corrupt_result.get("ok", true)
			or str(corrupt_result.get("error", {}).get("code", "")) != "PATCH_DECISION_DRAFT_CORRUPT"
			or _client_lifecycle_fingerprint(store, controller) != before_corruption
		):
			return _fail("Corrupt accepted Draft changed editor/lifecycle authority (%s): %s" % [corruption.label, corrupt_result])
	_setup_authority(store, controller, base, proposal)
	var product := Product.new(accepted_draft, proposal)
	controller.configure(null, product)
	controller.configure_world_presentation(null, null, null, true)
	controller.configure_skill_patch_capability(_capability())
	_seed_old_lifecycle(controller)
	store.set_flow(WalnutClientStore.FlowState.ACTIVE)

	var accepted: Dictionary = await controller.decide_patch(proposal, "ACCEPT")
	if (
		not accepted.get("ok", false)
		or product.decisions.size() != 1
		or product.canonical_reads != 1
		or store.draft != accepted_draft
		or store.local_source != str(accepted_draft.source_bundle.files[0].content)
		or store.draft_state != WalnutClientStore.DraftState.CLEAN
		or not controller.certified_build.is_empty()
		or not controller.active_activation.is_empty()
		or not controller.active_skill_tuple.is_empty()
		or store.flow_state != WalnutClientStore.FlowState.READY
	):
		return _fail("ACCEPT must create/load exactly the next canonical Draft and invalidate only local old lifecycle readiness: %s" % accepted)
	if str(product.decisions[0].request_body).is_empty():
		return _fail("PatchDecision did not preserve the exact first-attempt HTTP request body bytes.")
	var parsed: Variant = _normalize_numbers(JSON.parse_string(product.decisions[0].request_body))
	if parsed != product.decisions[0].request:
		return _fail("PatchDecision raw body does not parse to the exact validated request.")

	var calls_after_accept := product.decisions.size()
	var duplicate_accept: Dictionary = await controller.decide_patch(proposal, "ACCEPT")
	if duplicate_accept.get("ok", false) or product.decisions.size() != calls_after_accept:
		return _fail("Repeated ACCEPT reapplied an already-consumed Patch proposal.")

	_setup_authority(store, controller, base, proposal)
	controller.configure(null, product)
	controller.configure_world_presentation(null, null, null, true)
	controller.configure_skill_patch_capability(_capability())
	_seed_old_lifecycle(controller)
	store.set_flow(WalnutClientStore.FlowState.ACTIVE)
	var before_reject := _client_lifecycle_fingerprint(store, controller)
	var rejected: Dictionary = await controller.decide_patch(proposal, "REJECT", "STUDENT_REJECTED")
	if (
		not rejected.get("ok", false)
		or product.decisions.size() != calls_after_accept + 1
		or product.canonical_reads != 1
		or _client_lifecycle_fingerprint(store, controller) != before_reject
	):
		return _fail("REJECT produced a Draft/Build/Activation/Run readiness side effect: %s" % rejected)

	_setup_authority(store, controller, base, proposal)
	controller.configure(null, product)
	controller.configure_world_presentation(null, null, null, true)
	controller.configure_skill_patch_capability(_capability())
	store.mark_draft_dirty("// unsaved student edit\n" + store.local_source)
	var dirty_source := store.local_source
	var calls_before_dirty := product.decisions.size()
	var dirty_accept: Dictionary = await controller.decide_patch(proposal, "ACCEPT")
	if (
		dirty_accept.get("ok", false)
		or str(dirty_accept.get("error", {}).get("code", "")) != "PATCH_LOCAL_EDIT_CONFLICT"
		or product.decisions.size() != calls_before_dirty
		or store.local_source != dirty_source
		or store.draft_state != WalnutClientStore.DraftState.DIRTY
	):
		return _fail("Dirty local edits were not protected before ACCEPT mutation: %s" % dirty_accept)

	print("PATCH_DECISION_TEST_PASS")
	quit(0)


func _setup_authority(
	store: WalnutClientStore,
	controller: Node,
	draft: Dictionary,
	proposal: Dictionary,
) -> void:
	var bootstrap := {
		"actor": proposal.request_context.actor.duplicate(true),
		"content": proposal.request_context.content_ref.duplicate(true),
	}
	var session := {"session_id": proposal.session_id}
	controller.configure_authority(bootstrap, session)
	store.set_authoritative_bootstrap(bootstrap)
	store.set_authoritative_session(session)
	store.pending_operations.clear()
	store.set_draft(draft)


func _seed_old_lifecycle(controller: Node) -> void:
	controller.certified_build = {"build_id": "build_old_0001"}
	controller.active_activation = {"activation_id": "activation_old_0001"}
	controller.active_skill_tuple = {"activation_id": "activation_old_0001"}


func _client_lifecycle_fingerprint(store: WalnutClientStore, controller: Node) -> Dictionary:
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


func _rehash_draft(draft: Dictionary) -> void:
	draft.draft_sha256 = ContractValidator.canonical_json_sha256_v1({
		"session_id": draft.session_id,
		"draft_id": draft.draft_id,
		"skill_id": draft.skill_id,
		"content_ref": draft.content_ref,
		"display_name": draft.display_name,
		"source_bundle": draft.source_bundle,
	})


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
