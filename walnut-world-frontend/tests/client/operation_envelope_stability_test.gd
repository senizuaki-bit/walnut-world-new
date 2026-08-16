extends SceneTree

const ControllerScript := preload("res://autoload/session_controller.gd")

const HASH_A := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const HASH_B := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
const HASH_C := "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


class Product:
	extends RefCounted
	var draft_calls: Array[Dictionary] = []
	var patch_calls: Array[Dictionary] = []
	var reconcile_existing_draft := false
	func upsert_draft(_a: Dictionary, _s: String, _d: String, key: String, request: Dictionary) -> Dictionary:
		draft_calls.append({"key": key, "request": request.duplicate(true)})
		return {"ok": false, "status": 503, "headers": {}, "error": {"retryable": true}}
	func record_patch_decision(_a: Dictionary, _s: String, _i: String, _p: String, key: String, request: Dictionary, request_body: String) -> Dictionary:
		patch_calls.append({"key": key, "request": request.duplicate(true), "request_body": request_body})
		return {"ok": false, "status": 503, "headers": {}, "error": {"retryable": true}}
	func get_draft(_a: Dictionary, _s: String, _d: String) -> Dictionary:
		var canonical: Dictionary = draft_calls[0].request.duplicate(true)
		if reconcile_existing_draft:
			canonical["revision"] = 2
			canonical["draft_sha256"] = HASH_B
		else:
			canonical.source_bundle.files[0].content = "int main(){return -1;}"
			canonical.source_bundle.files[0].content_sha256 = str(canonical.source_bundle.files[0].content).sha256_text()
		return {"ok": true, "value": canonical}


class Game:
	extends RefCounted
	var turn_calls: Array[Dictionary] = []
	func submit_agent_turn(_a: Dictionary, _s: String, key: String, request: Dictionary) -> Dictionary:
		turn_calls.append({"key": key, "request": request.duplicate(true)})
		return {"ok": false, "status": 0, "headers": {}, "error": {"code": "LOCAL_NETWORK", "retryable": true}}
	func get_command(_a: Dictionary, _id: String) -> Dictionary: return {"ok": false}
	func get_run(_a: Dictionary, _id: String) -> Dictionary: return {"ok": false}


func _initialize() -> void:
	var store := root.get_node("ClientStore") as WalnutClientStore
	var controller := root.get_node("SessionController") as Node
	await process_frame
	store.persistence_enabled = false
	var game := Game.new()
	var product := Product.new()
	controller.configure(game, product, true)
	controller.configure_authority(_bootstrap(), {"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": _bootstrap().content})
	store.set_authoritative_bootstrap(_bootstrap())
	store.set_authoritative_session({"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": _bootstrap().content})
	store.set_draft(_draft())
	store.mark_draft_dirty("int main(){return 0;}")
	await controller.request_save()
	await controller.request_save()
	if product.draft_calls.size() != 2 or product.draft_calls[0] != product.draft_calls[1]:
		push_error("Draft retry must reuse identical key/body/client_saved_at.")
		quit(1)
		return
	product.reconcile_existing_draft = true
	store.mark_draft_dirty("int main(){return 9;}")
	await controller.request_save()
	if (
		product.draft_calls.size() != 2
		or store.local_source != "int main(){return 9;}"
		or store.draft_state != WalnutClientStore.DraftState.DIRTY
		or not store.get_pending_operation("draft_save").is_empty()
	):
		push_error("A different Draft identity must first reconcile the old envelope while preserving the newer local edit.")
		quit(1)
		return
	await controller.request_save()
	if (
		product.draft_calls.size() != 3
		or product.draft_calls[2] == product.draft_calls[1]
		or str(product.draft_calls[2].request.source_bundle.files[0].content) != "int main(){return 9;}"
	):
		push_error("Only a later save may persist and write the new Draft identity after old-envelope reconciliation.")
		quit(1)
		return

	store.set_workspace({"session": {"session_id": "session_demo_0001", "status": "ACTIVE", "last_turn_sequence": 0}, "current_task": {"task_id": "task_demo_0001"}, "last_interaction_sequence": 0})
	store.replace_world({"world_id": "world_demo_0001", "revision": 1, "last_event_sequence": 0, "state_schema_version": "1.0.0", "state_hash": HASH_A, "world_rules_version": "rules", "state": {}})
	await controller.request_turn()
	await controller.request_turn()
	if game.turn_calls.size() != 2 or game.turn_calls[0] != game.turn_calls[1]:
		push_error("Turn retry must reuse identical turn_id/idempotency key/body.")
		quit(1)
		return

	var interaction: Dictionary = _example("product-agent-interaction-page.json").interactions[0]
	var patch_base: Dictionary = _example("product-skill-draft-base.json")
	controller.configure_authority({"actor": interaction.request_context.actor, "content": interaction.request_context.content_ref}, {"session_id": interaction.session_id})
	store.set_authoritative_session({"session_id": interaction.session_id})
	store.set_draft(patch_base)
	controller.configure_world_presentation(null, null, null, true)
	controller.configure_skill_patch_capability(_capability())
	await controller.decide_patch(interaction, "REJECT", "STUDENT_REJECTED")
	await controller.decide_patch(interaction, "REJECT", "STUDENT_REJECTED")
	if product.patch_calls.size() != 2 or product.patch_calls[0] != product.patch_calls[1]:
		push_error("Patch retry must reuse identical decision_id/key/decided_at/body.")
		quit(1)
		return
	print("OPERATION_ENVELOPE_STABILITY_TEST_PASS")
	quit(0)


func _bootstrap() -> Dictionary:
	return {"actor": {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]}, "content": {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": HASH_A}, "activation": {"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"}, "registry_revision": 3, "active": {"activation_id": "activation_demo_0001", "skill_id": "skill_demo_0001", "skill_version_id": "skillver_demo_0001", "artifact_sha256": HASH_C, "certification_id": "cert_demo_0001", "registry_revision": 3, "activated_at": "2026-08-12T00:00:00Z"}}}


func _draft() -> Dictionary:
	return {"session_id": "session_demo_0001", "draft_id": "draft_demo_0001", "skill_id": "skill_demo_0001", "revision": 1, "draft_sha256": HASH_A, "display_name": "Demo", "content_ref": _bootstrap().content, "source_bundle": {"language": "CPP20", "entrypoint": "src/main.cpp", "files": [{"path": "src/main.cpp", "content": "", "content_sha256": HASH_B}]}}


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
