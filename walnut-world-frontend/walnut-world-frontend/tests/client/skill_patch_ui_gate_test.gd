extends SceneTree

const WorkspaceScene := preload("res://scenes/task/task_workspace.tscn")
const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const AgentContractFixtureLocator := preload("res://scripts/testing/agent_contract_fixture_locator.gd")

var _stage := "INITIALIZING"


class Product:
	extends RefCounted
	var decisions: Array[Dictionary] = []
	var fail_next_decision := false

	func record_patch_decision(
		_attempt: Dictionary,
		_session_id: String,
		_interaction_id: String,
		_patch_id: String,
		key: String,
		request: Dictionary,
		request_body: String,
	) -> Dictionary:
		if fail_next_decision:
			fail_next_decision = false
			return {
				"ok": false, "status": 409, "headers": {},
				"error": {
					"scope": "PRODUCT_GATEWAY", "code": "PATCH_DECISION_TEST_FAILURE",
					"message": "The explicit decision remains pending after a simulated conflict.",
				},
			}
		decisions.append({"key": key, "request": request.duplicate(true), "request_body": request_body})
		return {"ok": true, "status": 200, "headers": {}, "value": _receipt(request)}

	func get_draft(_attempt: Dictionary, _session_id: String, _draft_id: String) -> Dictionary:
		return {"ok": false, "status": 0, "headers": {}, "error": {"code": "REJECT_MUST_NOT_READ_DRAFT"}}

	func _receipt(request: Dictionary) -> Dictionary:
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
			"draft_updated": false,
			"draft_revision_before": request.base_draft_revision,
			"draft_sha256_before": request.base_draft_sha256,
			"draft_revision_after": request.base_draft_revision,
			"draft_sha256_after": request.base_draft_sha256,
		}


func _initialize() -> void:
	create_timer(5.0).timeout.connect(func() -> void:
		_fail("Timed out while exercising the formal Skill Patch UI at stage %s." % _stage)
	)
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	var controller := root.get_node_or_null("SessionController")
	if store == null or controller == null:
		return _fail("Required production autoloads are unavailable.")
	store.persistence_enabled = false
	_stage = "FIRST_FRAME"
	await process_frame
	_stage = "INSTANTIATING_WORKSPACE"
	var page := WorkspaceScene.instantiate()
	root.add_child(page)
	_stage = "WORKSPACE_READY"
	await process_frame
	var request_button := page.get_node_or_null("Hud/SafeArea/EdgeLayer/ToolRail/RequestAiPatchButton") as Button
	if request_button == null:
		return _fail("Formal TaskWorkspace has no explicit AI Patch request control.")
	if request_button.visible or page.patch_dialog != null:
		return _fail("Skill Patch controls are installed while the feature/capability is false.")

	_setup_authority(store, controller)
	var product := Product.new()
	controller.configure(null, product)
	controller.configure_world_presentation(null, null, null, true)
	_stage = "CAPABILITY_ENABLED"
	controller.configure_skill_patch_capability(_capability(true))
	page.configure_skill_patch_enabled(true)
	var failures: Array[Dictionary] = [_failure_interaction()]
	page._on_interactions_recovered(failures)
	if not request_button.visible or request_button.disabled:
		return _fail("Verified visible terminal failure at hint level 4 did not expose the explicit request control.")
	# Request errors are student-visible and do not consume the visible failure.
	# This isolated UI fixture intentionally lacks the public Build provenance
	# needed to submit a real proposal request.
	_stage = "REQUEST_ERROR"
	await page._on_ai_patch_requested()
	await process_frame
	if (
		not str(page.result_text.text).contains("SKILL_PATCH_REQUEST_GATEWAY_UNAVAILABLE")
		or not request_button.visible
		or request_button.disabled
	):
		return _fail("Skill Patch request failure was hidden or consumed recoverable UI state: toast=%s visible=%s disabled=%s can_request=%s" % [
			str(page.result_text.text), request_button.visible, request_button.disabled,
			controller.can_request_ai_patch(),
		])

	var proposal := _proposal_interaction(store.draft)
	_stage = "PROPOSAL_PREVIEW"
	var proposals: Array[Dictionary] = [proposal]
	page._on_patch_interactions_recovered(proposals)
	if page.patch_dialog == null or not page.patch_dialog.visible:
		return _fail("Validated proposal did not open the explicit confirmation preview.")
	var preview: String = str(page.patch_dialog.dialog_text)
	for exact in [
		"UPSERT_FILE", "src/main.cpp", "yaya::water(\"plot_1\", 500)",
		"evidence_world_00000001", "Use the bounded watering action",
		"--- BEFORE src/main.cpp", "int main() { return 0; }",
		"+++ AFTER src/main.cpp", "AI CODE PATCH (NOT APPLIED)",
	]:
		if not preview.contains(exact):
			return _fail("Patch preview omitted exact review material: %s" % exact)
	if page.reject_patch_button == null or page.patch_dialog.get_cancel_button().text != "关闭预览":
		return _fail("Patch preview does not separate explicit REJECT from close/Escape.")

	# Escape/window close is not a PatchDecision. The exact proposal remains in
	# UI state and the formal entry button reopens it without a network mutation.
	var pending_before_close: Dictionary = page.pending_patch_interaction.duplicate(true)
	page.patch_dialog.hide()
	page.patch_dialog.canceled.emit()
	await process_frame
	if product.decisions.size() != 0 or page.pending_patch_interaction != pending_before_close:
		return _fail("Closing Patch preview silently submitted REJECT or lost the proposal.")
	request_button.pressed.emit()
	await process_frame
	if not page.patch_dialog.visible or product.decisions.size() != 0:
		return _fail("Pending proposal could not be reopened without a new mutation.")

	# Decision errors display the exact authority failure and retain the same
	# reviewed proposal for retry/recovery.
	var draft_before_reject: Dictionary = store.draft.duplicate(true)
	product.fail_next_decision = true
	page.patch_dialog.custom_action.emit(&"reject_patch")
	await process_frame
	await process_frame
	if (
		product.decisions.size() != 0
		or page.pending_patch_interaction.is_empty()
		or not page.patch_dialog.visible
		or not str(page.result_text.text).contains("PATCH_DECISION_TEST_FAILURE")
	):
		return _fail("PatchDecision failure was hidden or discarded the recoverable proposal.")

	# A decision reconciled after response loss notifies the formal page, which
	# clears/hides the now-decided proposal instead of leaving a false pending UI.
	if not controller.has_signal("patch_decision_resolved"):
		return _fail("SessionController does not expose resolved PatchDecision recovery to TaskWorkspace.")
	controller.emit_signal(
		"patch_decision_resolved",
		str(proposal.interaction_id),
		str(proposal.skill_patch.patch_id),
		"REJECT",
	)
	await process_frame
	if not page.pending_patch_interaction.is_empty() or page.patch_dialog.visible:
		return _fail("Reconciled PatchDecision left a false pending proposal in the formal UI.")

	# Only the separate explicit reject action records REJECT, and it leaves all
	# canonical client Draft bytes unchanged.
	page._on_patch_interactions_recovered(proposals)
	page.patch_dialog.custom_action.emit(&"reject_patch")
	await process_frame
	await process_frame
	if (
		product.decisions.size() != 1
		or str(product.decisions[0].request.get("decision", "")) != "REJECT"
		or not page.pending_patch_interaction.is_empty()
		or store.draft != draft_before_reject
	):
		return _fail("Explicit REJECT did not produce exactly one zero-Draft-side-effect decision.")

	# A clean UI/controller restart can recover the canonical failure Interaction,
	# but the public Workspace/Interaction wire does not expose Build provenance.
	# The surface must report that NOT_PROVEN state instead of silently hiding why
	# a new request cannot be submitted.
	controller.configure(null, product)
	controller.configure_authority({
		"actor": failures[0].request_context.actor,
		"content": failures[0].request_context.content_ref,
	}, {"session_id": failures[0].session_id})
	controller.configure_world_presentation(null, null, null, true)
	controller.configure_skill_patch_capability(_capability(true))
	store.set_objective_result({"summary": "Clean restart has no in-memory failed Run/Build authority."})
	page._on_interactions_recovered(failures)
	await process_frame
	if not str(page.result_text.text).contains("SKILL_PATCH_BUILD_PROVENANCE_NOT_PROVEN_AFTER_RESTART"):
		return _fail("Clean restart silently lost the latest eligible failure instead of reporting NOT_PROVEN provenance.")

	page.configure_skill_patch_enabled(false)
	_stage = "CAPABILITY_DISABLED"
	if request_button.visible or page.patch_dialog != null:
		return _fail("Disabling Skill Patch did not remove every formal entry/decision surface.")
	print("SKILL_PATCH_UI_GATE_TEST_PASS")
	quit(0)


func _setup_authority(store: WalnutClientStore, controller: Node) -> void:
	var failure := _failure_interaction()
	var actor: Dictionary = failure.request_context.actor.duplicate(true)
	var content: Dictionary = failure.request_context.content_ref.duplicate(true)
	var session := {"session_id": failure.session_id}
	controller.configure_authority({"actor": actor, "content": content}, session)
	store.set_authoritative_session(session)
	store.set_draft(_draft())
	store.set_objective_result({
		"objective_succeeded": false,
		"summary": "verified failure",
		"run_id": str(failure.feedback.run_id),
	})
	store.set_flow(WalnutClientStore.FlowState.COMPLETED)
	store.set_interaction_cursor(int(failure.sequence))


func _failure_interaction() -> Dictionary:
	var interaction: Dictionary = _read_example("product-agent-interaction-page.json").interactions[0]
	interaction.role = "bug_agent"
	interaction.response_type = "message"
	interaction.question = null
	interaction.hint_level = null
	interaction.skill_patch = null
	interaction.patch_decision = null
	interaction.links.skill_draft = null
	interaction.projection_source.role = interaction.role
	interaction.projection_source.response_type = interaction.response_type
	interaction.projection_source.question = null
	interaction.projection_source.hint_level = null
	interaction.projection_source.skill_patch_sha256 = null
	var projection: Dictionary = interaction.projection_source.duplicate(true)
	projection.erase("source_sha256")
	interaction.projection_source.source_sha256 = ContractValidator.canonical_json_sha256_v1(projection)
	return interaction


func _proposal_interaction(draft: Dictionary) -> Dictionary:
	var proposal: Dictionary = _read_example("product-agent-interaction-page.json").interactions[0]
	if proposal.skill_patch.draft_id != draft.draft_id:
		return {}
	return proposal


func _draft() -> Dictionary:
	return _read_example("product-skill-draft-base.json")


func _read_example(file_name: String) -> Dictionary:
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


func _capability(enabled: bool) -> Dictionary:
	return {
		"world_presentation_enabled": true,
		"skill_patch_enabled": enabled,
		"skill_patch_constraints": {
			"request_mode": "EXPLICIT_UI_ACTION", "agent_role": "teaching_agent",
			"selection_target": "FAILED_INTERACTION",
			"scenario": "RECTIFICATION", "required_hint_level": 4,
			"operation": "UPSERT_FILE", "target": "CURRENT_ENTRYPOINT",
			"max_files": 1, "max_operations": 1, "requires_failed_evidence": true,
			"cas_required": true, "requires_student_confirmation": true,
			"auto_build": false, "auto_activate": false, "auto_run": false,
		},
	}


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
