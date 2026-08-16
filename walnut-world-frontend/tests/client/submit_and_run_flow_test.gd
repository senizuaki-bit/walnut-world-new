extends SceneTree

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")

const HASH_A := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const HASH_B := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
const HASH_C := "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


class Product:
	extends RefCounted
	var saved := false
	var game: RefCounted
	func _init(source: RefCounted) -> void: game = source
	func upsert_draft(_attempt: Dictionary, _session_id: String, _draft_id: String, _key: String, request: Dictionary) -> Dictionary:
		saved = true
		var canonical := request.duplicate(true)
		canonical["revision"] = 2
		canonical["draft_sha256"] = HASH_A
		return {"ok": true, "value": canonical}
	func list_interactions(_attempt: Dictionary, _session_id: String, after_sequence: int, _limit: int) -> Dictionary:
		return {"ok": true, "value": {"interactions": [{"interaction_id": "interaction_demo_0001", "session_id": "session_demo_0001", "turn_id": game.turn_request.turn_id, "sequence": after_sequence + 1, "feedback": game._feedback()}], "next_after_sequence": after_sequence + 1, "high_watermark_sequence": after_sequence + 1, "has_more": false}}


class Game:
	extends RefCounted
	var stages: Array[String] = []
	var build_request: Dictionary = {}
	var activation_request: Dictionary = {}
	var turn_request: Dictionary = {}
	func submit_skill_build(_a: Dictionary, _k: String, _r: Dictionary) -> Dictionary:
		stages.append("build")
		build_request = _r.duplicate(true)
		return {"ok": true, "headers": {}, "value": {"command_id": "cmd_build_demo_0001"}}
	func activate_skill_version(_a: Dictionary, _v: String, _k: String, _r: Dictionary) -> Dictionary:
		stages.append("activate")
		activation_request = _r.duplicate(true)
		return {"ok": true, "headers": {}, "value": {"command_id": "cmd_activation_demo_0001"}}
	func submit_agent_turn(_attempt: Dictionary, _session: String, _key: String, request: Dictionary) -> Dictionary:
		stages.append("turn")
		turn_request = request.duplicate(true)
		return {"ok": true, "headers": {}, "value": {"command_id": "cmd_turn_demo_0001"}}
	func get_command(_attempt: Dictionary, command_id: String) -> Dictionary:
		if command_id == "cmd_build_demo_0001":
			return {"ok": true, "headers": {}, "value": {"command_id": command_id, "terminal": true, "status": "APPLIED", "result": {"resource_type": "SKILL_BUILD", "resource_id": "build_demo_0001"}}}
		if command_id == "cmd_activation_demo_0001":
			return {"ok": true, "headers": {}, "value": {"command_id": command_id, "terminal": true, "status": "APPLIED", "result": {"resource_type": "SKILL_ACTIVATION", "resource_id": "activation_demo_0001"}}}
		return {"ok": true, "headers": {}, "value": {"command_id": command_id, "terminal": true, "status": "APPLIED", "result": {"result_type": "WORLD_COMMIT", "world_id": "world_demo_0001", "previous_revision": 1, "world_revision": 2, "first_event_sequence": 1, "last_event_sequence": 1}, "links": {"run": "/v1/runs/run_demo_0001"}}}
	func get_skill_build(_attempt: Dictionary, _build_id: String) -> Dictionary:
		var projection: Array = []
		for file: Dictionary in build_request.source_bundle.files:
			projection.append([file.path, file.content_sha256])
		return {"ok": true, "headers": {}, "value": {"build_id": "build_demo_0001", "skill_id": "skill_demo_0001", "skill_version_id": "skillver_demo_0001", "status": "CERTIFIED", "terminal": true, "artifact": {"artifact_sha256": HASH_C, "source_sha256": JSON.stringify(projection).sha256_text()}, "certification": {"certification_id": "cert_demo_0001"}}}
	func get_skill_activation(_attempt: Dictionary, _activation_id: String) -> Dictionary:
		var previous_revision := int(activation_request.expected_registry_revision)
		return {"ok": true, "headers": {}, "value": {"activation_id": "activation_demo_0001", "skill_id": "skill_demo_0001", "skill_version_id": "skillver_demo_0001", "certification_id": "cert_demo_0001", "artifact_sha256": HASH_C, "activation_scope": activation_request.activation_scope.duplicate(true), "previous_registry_revision": previous_revision, "registry_revision": previous_revision + 1, "activated_at": "2026-08-12T00:00:00Z"}}
	func get_run(_attempt: Dictionary, _run_id: String) -> Dictionary:
		return {"ok": true, "headers": {}, "value": {"run_id": "run_demo_0001", "session_id": "session_demo_0001", "turn_id": turn_request.turn_id, "command_id": "cmd_turn_demo_0001", "status": "SUCCEEDED", "terminal": true, "skill": {"skill_id": "skill_demo_0001", "skill_version_id": "skillver_demo_0001", "artifact_sha256": HASH_C, "certification_id": "cert_demo_0001"}, "world_application": {"status": "COMMITTED", "receipt": _receipt(), "failure": null}, "agent_feedback": _feedback(), "evidence_refs": [{"evidence_id": "evidence_world_demo_0001"}]}}
	func get_evidence(_attempt: Dictionary, _evidence_id: String) -> Dictionary:
		return {"ok": true, "value": {"evidence_ref": {"evidence_id": "evidence_world_demo_0001"}, "source": {"command_id": "cmd_turn_demo_0001"}, "payload": {"evidence_kind": "WORLD_COMMIT", "world_id": "world_demo_0001", "previous_revision": 1, "world_revision": 2, "first_event_sequence": 1, "last_event_sequence": 1, "state_hash": HASH_B}}}
	func get_world_events(_attempt: Dictionary, _world_id: String, _after: int, _limit: int) -> Dictionary:
		return {"ok": true, "value": {"world_id": "world_demo_0001", "snapshot_revision": 2, "events": [{"event_id": "evt_demo_0001", "sequence": 1, "command_id": "cmd_turn_demo_0001"}], "next_after_sequence": 1, "has_more": false}}
	func get_world_snapshot(_attempt: Dictionary, _world_id: String) -> Dictionary:
		return {"ok": true, "value": {"world_id": "world_demo_0001", "revision": 2, "last_event_sequence": 1, "state_schema_version": "1.0.0", "state_hash": HASH_B, "world_rules_version": "rules", "state": {}}}
	func _receipt() -> Dictionary:
		return {"world_id": "world_demo_0001", "previous_revision": 1, "world_revision": 2, "first_event_sequence": 1, "last_event_sequence": 1, "state_hash": HASH_B, "committed_at": "2026-08-12T00:00:00Z"}
	func _feedback() -> Dictionary:
		return {"turn_id": turn_request.turn_id, "command_id": "cmd_turn_demo_0001", "run_id": "run_demo_0001", "source": "provider", "degraded": false, "fallback_reason": null}


func _initialize() -> void:
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	if store == null: store = StoreScript.new(); store.name = "ClientStore"; root.add_child(store)
	var controller := root.get_node_or_null("SessionController") as Node
	if controller == null: controller = ControllerScript.new(); controller.name = "SessionController"; root.add_child(controller)
	await process_frame
	store.persistence_enabled = false
	var active_bootstrap := _bootstrap(true)
	_prepare_store(store, active_bootstrap)
	var game := Game.new()
	var product := Product.new(game)
	controller.configure(game, product)
	controller.configure_polling({"initial_delay_seconds": 0.0, "base_delay_seconds": 0.0, "max_delay_seconds": 0.0, "jitter_ratio": 0.0, "interaction_delay_seconds": 0.0, "interaction_deadline_seconds": 0.2})
	controller.configure_authority(active_bootstrap, {"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": active_bootstrap.content})
	controller.begin_startup_authority_revalidation()
	await controller.request_build()
	await controller.request_activation()
	var blocked_result: Dictionary = await controller.request_submit_and_run()
	if blocked_result.get("ok", true) or str(blocked_result.get("stage", "")) != "AUTHORITY" or product.saved or not game.stages.is_empty():
		push_error("Controller must reject every student mutation while AppRoot authority revalidation is pending.")
		quit(1)
		return
	store.set_flow(WalnutClientStore.FlowState.ACTIVE)
	controller.set_startup_authority_ready(true)
	var result: Dictionary = await controller.request_submit_and_run()
	if not result.get("ok", false) or not product.saved or game.stages != ["turn"] or store.flow_state != WalnutClientStore.FlowState.COMPLETED:
		push_error("An existing public activation must preserve the direct Save -> Run action: %s %s" % [str(game.stages), str(store.last_error)])
		quit(1)
		return
	store.set_flow(WalnutClientStore.FlowState.ERROR)
	var stages_before_error := game.stages.duplicate()
	await controller.request_build()
	await controller.request_activation()
	var error_blocked: Dictionary = await controller.request_submit_and_run()
	if error_blocked.get("ok", true) or str(error_blocked.get("stage", "")) != "AUTHORITY" or game.stages != stages_before_error:
		push_error("Controller must reject student mutations while the formal AppRoot is in ERROR.")
		quit(1)
		return
	var fresh_bootstrap := _bootstrap(false)
	_prepare_store(store, fresh_bootstrap)
	store.set_flow(WalnutClientStore.FlowState.READY)
	var fresh_game := Game.new()
	var fresh_product := Product.new(fresh_game)
	controller.configure(fresh_game, fresh_product)
	controller.configure_authority(fresh_bootstrap, {"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": fresh_bootstrap.content})
	var premature_run: Dictionary = await controller.request_submit_and_run()
	if premature_run.get("ok", true) or str(premature_run.get("stage", "")) != "ACTIVATE" or not fresh_game.stages.is_empty():
		push_error("Submit/Run must fail closed without an active tuple and must never auto-trigger Build or Activation.")
		quit(1)
		return
	await controller.request_build()
	if fresh_game.stages != ["build"] or store.flow_state != WalnutClientStore.FlowState.CERTIFIED:
		push_error("The first explicit action must close only Build/Certification.")
		quit(1)
		return
	await controller.request_activation()
	if fresh_game.stages != ["build", "activate"] or store.flow_state != WalnutClientStore.FlowState.ACTIVE:
		push_error("The second explicit action must close only Activation.")
		quit(1)
		return
	result = await controller.request_submit_and_run()
	if (
		not result.get("ok", false)
		or not fresh_product.saved
		or fresh_game.stages != ["build", "activate", "turn"]
		or store.flow_state != WalnutClientStore.FlowState.COMPLETED
		or fresh_game.build_request.get("compiler_profile") != fresh_bootstrap.build.compiler_profile
		or fresh_game.build_request.get("test_suite_version") != fresh_bootstrap.build.test_suite_version
		or fresh_game.build_request.get("requested_capabilities") != fresh_bootstrap.build.allowed_capabilities
		or fresh_game.activation_request.get("activation_scope") != fresh_bootstrap.activation.scope
		or int(fresh_game.activation_request.get("expected_registry_revision", -1)) != int(fresh_bootstrap.activation.registry_revision)
	):
		push_error("Three explicit actions must close Build -> Activation -> Run with Build/scope/revision copied from StudentBootstrap: %s %s" % [str(fresh_game.stages), str(store.last_error)])
		quit(1)
		return
	print("SUBMIT_AND_RUN_FLOW_TEST_PASS")
	quit(0)


func _prepare_store(store: WalnutClientStore, bootstrap: Dictionary) -> void:
	store.set_authoritative_bootstrap(bootstrap)
	store.set_authoritative_session({"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": bootstrap.content})
	store.set_draft(_draft())
	store.mark_draft_dirty("int main() { return 0; }")
	store.set_workspace({"session": {"session_id": "session_demo_0001", "status": "ACTIVE", "last_turn_sequence": 0}, "current_task": {"task_id": "task_demo_0001"}, "last_interaction_sequence": store.last_interaction_sequence})
	store.replace_world({"world_id": "world_demo_0001", "revision": 1, "last_event_sequence": 0, "state_schema_version": "1.0.0", "state_hash": HASH_A, "world_rules_version": "rules", "state": {}})


func _bootstrap(has_active := true) -> Dictionary:
	var active: Variant = {
		"activation_id": "activation_demo_0001",
		"skill_id": "skill_demo_0001",
		"skill_version_id": "skillver_demo_0001",
		"artifact_sha256": HASH_C,
		"certification_id": "cert_demo_0001",
		"registry_revision": 3,
		"activated_at": "2026-08-12T00:00:00Z",
	} if has_active else null
	return {"actor": {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]}, "content": {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": HASH_A}, "build": {"build_policy_id": "policy_demo_0001", "compiler_profile": "YAYA_CPP20_SAFE_V1", "compiler_version": "clang-20.1.0", "sandbox_image_digest": "sha256:" + HASH_B, "test_suite_version": "farm-water-v3", "allowed_capabilities": ["WORLD_READ", "WATER"], "max_source_files": 32, "max_source_bytes": 1048576}, "activation": {"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"}, "registry_revision": 3, "active": active}}


func _draft() -> Dictionary:
	var source := "int main() { return 0; }"
	return {"session_id": "session_demo_0001", "draft_id": "draft_demo_0001", "skill_id": "skill_demo_0001", "revision": 1, "draft_sha256": HASH_A, "display_name": "Demo", "content_ref": _bootstrap().content, "source_bundle": {"language": "CPP20", "entrypoint": "src/main.cpp", "files": [{"path": "src/main.cpp", "content": source, "content_sha256": source.sha256_text()}]}}
