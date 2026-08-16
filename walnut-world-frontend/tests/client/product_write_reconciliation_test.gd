extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")


class Product:
	extends RefCounted
	var draft_write_calls := 0
	var draft_read_calls := 0
	var patch_write_calls := 0
	var interaction_read_calls := 0
	var last_draft_request: Dictionary = {}
	var last_patch_request: Dictionary = {}
	var accepted_patch_draft: Dictionary = {}

	func upsert_draft(_attempt: Dictionary, _session_id: String, _draft_id: String, _key: String, request: Dictionary) -> Dictionary:
		draft_write_calls += 1
		last_draft_request = request.duplicate(true)
		return _reconcile("SKILL_DRAFT", "session_demo_0001", "draft_demo_0001")

	func get_draft(_attempt: Dictionary, _session_id: String, _draft_id: String) -> Dictionary:
		draft_read_calls += 1
		var accepted_patch := patch_write_calls > 0
		if accepted_patch and not accepted_patch_draft.is_empty():
			return {"ok": true, "value": accepted_patch_draft.duplicate(true)}
		var source := "int main() { return %s; }" % ("2" if accepted_patch else "1")
		return {"ok": true, "value": _draft(source, 3 if accepted_patch else 2, "c".repeat(64) if accepted_patch else "b".repeat(64))}

	func record_patch_decision(_attempt: Dictionary, _session_id: String, _interaction_id: String, _patch_id: String, _key: String, request: Dictionary, _request_body: String) -> Dictionary:
		patch_write_calls += 1
		last_patch_request = request.duplicate(true)
		return _reconcile("AGENT_INTERACTION", str(request.session_id), str(request.interaction_id))

	func get_interaction(_attempt: Dictionary, _session_id: String, _interaction_id: String) -> Dictionary:
		interaction_read_calls += 1
		return {"ok": true, "value": {"patch_decision": _patch_receipt(last_patch_request)}}

	func _reconcile(resource_type: String, session_id: String, resource_id: String) -> Dictionary:
		var resource_url := "/product-experience/v1/sessions/%s/skill-drafts/%s" % [session_id, resource_id] if resource_type == "SKILL_DRAFT" else "/product-experience/v1/sessions/%s/agent-interactions/%s" % [session_id, resource_id]
		return {
			"ok": false,
			"status": 503,
			"headers": {"location": resource_url},
			"error": {
				"request_id": "req_reconcile_0001", "trace_id": "trace_reconcile_0001", "correlation_id": "corr_reconcile_0001",
				"status": "RECONCILE",
				"data": null,
				"error": {"code": "DEPENDENCY_UNAVAILABLE"},
				"reconciliation": {"resource_type": resource_type, "session_id": session_id, "resource_id": resource_id, "resource_url": resource_url, "original_trace_id": "trace_durable_0001"},
			},
		}

	func _draft(source: String, revision: int, sha: String) -> Dictionary:
		return {
			"session_id": "session_demo_0001", "draft_id": "draft_demo_0001", "skill_id": "skill_demo_0001",
			"revision": revision, "draft_sha256": sha, "content_ref": {"unit_id": "unit_demo_0001", "version": "1.0.0", "content_hash": "e".repeat(64)}, "display_name": "演示技能",
			"source_bundle": {"language": "CPP20", "entrypoint": "src/main.cpp", "files": [{"path": "src/main.cpp", "content": source, "content_sha256": source.sha256_text()}]},
		}

	func _patch_receipt(request: Dictionary) -> Dictionary:
		return {
			"decision_id": request.get("decision_id", ""), "session_id": request.get("session_id", ""), "turn_id": request.get("turn_id", ""),
			"interaction_id": request.get("interaction_id", ""), "interaction_revision_before": request.get("expected_interaction_revision", 0), "interaction_revision_after": int(request.get("expected_interaction_revision", 0)) + 1, "patch_id": request.get("patch_id", ""), "patch_sha256": request.get("patch_sha256", ""),
			"draft_id": request.get("draft_id", ""), "skill_id": request.get("skill_id", ""), "decision": request.get("decision", ""),
			"reason_code": request.get("reason_code"), "draft_updated": request.get("decision", "") == "ACCEPT",
			"draft_revision_before": request.get("base_draft_revision", 0), "draft_sha256_before": request.get("base_draft_sha256", ""),
			"draft_revision_after": int(request.get("base_draft_revision", 0)) + (1 if request.get("decision", "") == "ACCEPT" else 0),
			"draft_sha256_after": request.get("result_draft_sha256", "") if request.get("decision", "") == "ACCEPT" else request.get("base_draft_sha256", ""),
		}


func _initialize() -> void:
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	if store == null:
		store = StoreScript.new(); store.name = "ClientStore"; root.add_child(store)
	store.persistence_enabled = false
	store.set_authoritative_session({"session_id": "session_demo_0001"})
	store.set_draft(_draft("int main() { return 0; }", 1, "a".repeat(64)))
	var controller := root.get_node_or_null("SessionController") as Node
	if controller == null:
		controller = ControllerScript.new(); controller.name = "SessionController"; root.add_child(controller)
	await process_frame
	var product := Product.new()
	controller.configure(null, product, true)
	controller.configure_draft_context({"attempt": {}})
	store.mark_draft_dirty("int main() { return 1; }")
	var saved: Dictionary = await controller.request_save()
	if not saved.get("ok", false) or product.draft_write_calls != 1 or product.draft_read_calls != 1 or store.local_source != "int main() { return 1; }" or store.draft_state != WalnutClientStore.DraftState.CLEAN:
		push_error("A durable Product Draft reconciliation must read and accept the matching canonical Draft without replaying the write.")
		quit(1); return
	var proposal: Dictionary = _example("product-agent-interaction-page.json").interactions[0]
	var patch_base: Dictionary = _example("product-skill-draft-base.json")
	product.accepted_patch_draft = _example("product-skill-draft.json")
	controller.configure_authority({"actor": proposal.request_context.actor, "content": proposal.request_context.content_ref}, {"session_id": proposal.session_id})
	controller.configure_world_presentation(null, null, null, true)
	controller.configure_skill_patch_capability(_capability())
	store.set_authoritative_session({"session_id": proposal.session_id})
	store.set_draft(patch_base)
	var accepted: Dictionary = await controller.decide_patch(proposal, "ACCEPT")
	if not accepted.get("ok", false) or product.patch_write_calls != 1 or product.interaction_read_calls != 1 or product.draft_read_calls != 2 or store.draft != product.accepted_patch_draft:
		push_error("A durable PatchDecision reconciliation must read the canonical interaction and, for ACCEPT, the canonical Draft without replaying.")
		quit(1); return
	print("PRODUCT_WRITE_RECONCILIATION_TEST_PASS")
	quit(0)


func _draft(source: String, revision: int, sha: String) -> Dictionary:
	var value := _draft_without_language(source, revision, sha)
	value.source_bundle["language"] = "CPP20"
	return value


func _draft_without_language(source: String, revision: int, sha: String) -> Dictionary:
	return {"session_id": "session_demo_0001", "draft_id": "draft_demo_0001", "skill_id": "skill_demo_0001", "revision": revision, "draft_sha256": sha, "content_ref": {"unit_id": "unit_demo_0001", "version": "1.0.0", "content_hash": "e".repeat(64)}, "display_name": "演示技能", "source_bundle": {"entrypoint": "src/main.cpp", "files": [{"path": "src/main.cpp", "content": source, "content_sha256": source.sha256_text()}]}}


func _capability() -> Dictionary:
	return {"world_presentation_enabled": true, "skill_patch_enabled": true, "skill_patch_constraints": {"request_mode": "EXPLICIT_UI_ACTION", "selection_target": "FAILED_INTERACTION", "agent_role": "teaching_agent", "scenario": "RECTIFICATION", "required_hint_level": 4, "operation": "UPSERT_FILE", "target": "CURRENT_ENTRYPOINT", "max_files": 1, "max_operations": 1, "requires_failed_evidence": true, "cas_required": true, "requires_student_confirmation": true, "auto_build": false, "auto_activate": false, "auto_run": false}}


func _example(file_name: String) -> Dictionary:
	var examples := ProjectSettings.globalize_path("res://../agent/contracts/examples").simplify_path()
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(examples.path_join(file_name)))
	return _normalize_numbers(parsed.value)


func _normalize_numbers(value: Variant) -> Variant:
	if typeof(value) == TYPE_FLOAT and value == floor(value): return int(value)
	if value is Array:
		var result: Array = []
		for item in value: result.append(_normalize_numbers(item))
		return result
	if value is Dictionary:
		var result := {}
		for key in value: result[key] = _normalize_numbers(value[key])
		return result
	return value
