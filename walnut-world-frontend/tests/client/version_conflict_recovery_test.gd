extends SceneTree

const Gateway := preload("res://addons/yaya_contract_client/agent_api_gateway.gd")
var failures: Array[String] = []

class StaleRegistry:
	extends RefCounted
	var bootstrap: Dictionary
	var requests: Array[Dictionary] = []
	var keys: Array[String] = []
	var reads := 0
	var status := 409
	func activate_skill_version(_attempt: Dictionary, _version: String, key: String, request: Dictionary) -> Dictionary:
		requests.append(request.duplicate(true))
		keys.append(key)
		if int(request.expected_registry_revision) == 7:
			return {"ok": false, "status": status, "error": {"code": "CONTENT_VERSION_MISMATCH"}}
		return {"ok": true, "value": {"command_id": "cmd_activation_0001"}}
	func get_student_bootstrap(attempt: Dictionary) -> Dictionary:
		reads += 1
		# Use the production wire guard: a permissive mock missed this defect.
		var guard: Dictionary = Gateway.new()._validate_wire_attempt_context(attempt)
		return {"ok": true, "value": bootstrap} if guard.ok else guard
	func get_command(_attempt: Dictionary, command_id: String) -> Dictionary:
		return {"ok": true, "value": {"command_id": command_id, "terminal": true, "status": "APPLIED", "result": {"resource_type": "SKILL_ACTIVATION", "resource_id": "activation_demo_0001"}}}
	func get_skill_activation(_attempt: Dictionary, _id: String) -> Dictionary:
		return {"ok": true, "value": {"activation_id": "activation_demo_0001", "skill_id": "skill_demo_0001", "skill_version_id": "skillver_demo_0001", "certification_id": "cert_demo_0001", "artifact_sha256": "a".repeat(64), "activation_scope": bootstrap.activation.scope, "previous_registry_revision": 8, "registry_revision": 9, "activated_at": "2026-09-11T00:00:00Z"}}

class StaleDraft:
	extends RefCounted
	var latest: Dictionary
	var requests: Array[Dictionary] = []
	var fail_read := false
	var edit_on_read := false
	func upsert_draft(_attempt: Dictionary, _session: String, _draft: String, _key: String, request: Dictionary) -> Dictionary:
		requests.append(request.duplicate(true))
		if int(request.base_revision) == 1:
			return {"ok": false, "status": 409, "error": {"code": "CONTENT_VERSION_MISMATCH", "category": "CONCURRENCY"}}
		var saved := latest.duplicate(true)
		saved.source_bundle = request.source_bundle.duplicate(true)
		saved.revision = 3
		return {"ok": true, "value": saved}
	func get_draft(_attempt: Dictionary, _session: String, _draft: String) -> Dictionary:
		if edit_on_read:
			Engine.get_main_loop().root.get_node("ClientStore").mark_draft_dirty("int main() { return 9; }")
		await Engine.get_main_loop().process_frame
		return {"ok": false} if fail_read else {"ok": true, "value": latest}

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var store := root.get_node("ClientStore") as WalnutClientStore
	var controller := root.get_node("SessionController")
	store.persistence_enabled = false
	controller.configure_polling({"initial_delay_seconds": 0.0, "base_delay_seconds": 0.0, "max_delay_seconds": 0.0, "jitter_ratio": 0.0})
	for scenario in ["recover", "wrong_scope", "auth_failure"]:
		var authority := _bootstrap()
		var game := StaleRegistry.new()
		game.bootstrap = authority.duplicate(true)
		game.bootstrap.activation.registry_revision = 8
		if scenario == "wrong_scope": game.bootstrap.activation.scope.world_id = "world_other_0001"
		if scenario == "auth_failure": game.status = 401
		controller.configure(game)
		store.set_authoritative_bootstrap(authority)
		controller.configure_authority(authority)
		controller.certified_build = {"skill_id": "skill_demo_0001", "skill_version_id": "skillver_demo_0001", "artifact": {"artifact_sha256": "a".repeat(64)}, "certification": {"certification_id": "cert_demo_0001"}}
		await controller.request_activation()
		if scenario == "recover":
			check(game.requests.size() == 2 and game.reads == 1, "Stale registry must refresh over the production wire contract and retry once.")
			check(store.flow_state == WalnutClientStore.FlowState.ACTIVE and int(store.activation_authority.registry_revision) == 9, "Fresh activation must reconcile and become ACTIVE.")
			check(game.keys.size() == 2 and game.keys[0] != game.keys[1], "Changed CAS revision requires a new idempotency key.")
		else:
			check(game.requests.size() == 1, "Changed scope and authentication failures must not retry activation.")
			if scenario == "auth_failure": check(game.reads == 0, "Authentication failure must not trigger registry refresh.")
	for scenario in ["recover", "read_failure", "wrong_draft"]:
		store._clear_authority_payload()
		store.authority_binding.clear()
		controller.authority_context.clear()
		var product := StaleDraft.new()
		product.latest = _draft()
		product.latest.revision = 2
		product.latest.draft_sha256 = "c".repeat(64)
		product.edit_on_read = true
		product.fail_read = scenario == "read_failure"
		if scenario == "wrong_draft": product.latest.draft_id = "draft_other_0001"
		controller.configure(null, product)
		controller.configure_draft_context({"attempt": {"request_id": "req_test"}})
		store.set_authoritative_session({"session_id": "session_demo_0001"})
		store.set_draft(_draft())
		store.mark_draft_dirty("int main() { return 1; }")
		var result: Dictionary = await controller.request_save()
		check(not result.get("ok", false) and product.requests.size() == 1, "Conflict must not automatically overwrite a concurrent server edit.")
		check(store.local_source == "int main() { return 9; }", "Edits typed during refresh must survive.")
		if scenario == "recover":
			check(int(store.draft.revision) == 2 and store.last_error.code == "DRAFT_VERSION_CONFLICT", "Latest CAS base and actionable error must reach the UI.")
			result = await controller.request_save()
			check(result.get("ok", false) and store.draft_state == WalnutClientStore.DraftState.CLEAN, "Next explicit save must succeed with refreshed revision.")
			check(product.requests.size() == 2 and int(product.requests[-1].base_revision) == 2 and product.requests[-1].source_bundle.files[0].content == "int main() { return 9; }", "Retry must use latest base and preserved editor bytes.")
		else:
			check(int(store.draft.revision) == 1, "Failed or mismatched reads must not rebase the editor.")
	if failures.is_empty():
		print("VERSION_CONFLICT_RECOVERY_PASS")
	else:
		for message in failures: push_error(message)
	quit(0 if failures.is_empty() else 1)

func check(condition: bool, message: String) -> void:
	if not condition: failures.append(message)

func _bootstrap() -> Dictionary:
	return {"actor": {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]}, "content": {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": "b".repeat(64)}, "activation": {"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"}, "registry_revision": 7, "active": null}}

func _draft() -> Dictionary:
	return {"session_id": "session_demo_0001", "draft_id": "draft_demo_0001", "skill_id": "skill_demo_0001", "revision": 1, "draft_sha256": "b".repeat(64), "display_name": "Demo", "content_ref": {}, "source_bundle": {"language": "CPP20", "entrypoint": "src/main.cpp", "files": [{"path": "src/main.cpp", "content": "int main() { return 0; }", "content_sha256": ""}]}}
