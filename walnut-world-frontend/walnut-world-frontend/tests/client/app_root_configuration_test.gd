extends SceneTree

const AppRootScript := preload("res://scenes/app/app_root.gd")


class FakeProjectionBridge:
	extends Node
	var activation_calls := 0
	var rejection_messages: Array[String] = []
	var activation_result := {"ok": true}

	func activate_initial_projection() -> Dictionary:
		activation_calls += 1
		return activation_result.duplicate(true)

	func reject_initial_projection(message: String) -> Dictionary:
		rejection_messages.append(message)
		return {"ok": false, "message": message}


func _initialize() -> void:
	var configured: Dictionary = AppRootScript.resolve_configuration(
		"https://API.YAYA.EXAMPLE:443///",
		{"YAYA_API_BASE_URL": "", "YAYA_AUTH_TOKEN": "development-token"},
	)
	if (
		not bool(configured.get("ok", false))
		or str(configured.get("base_url", "")) != "https://api.yaya.example"
		or configured.has("session_id")
	):
		push_error("AppRoot must resolve only endpoint/token configuration and never accept a manual Session id.")
		quit(1)
		return
	var missing_token: Dictionary = AppRootScript.resolve_configuration(
		"https://api.yaya.example", {},
	)
	if bool(missing_token.get("ok", true)) or str(missing_token.get("error_code", "")) != "AUTH_TOKEN_MISSING":
		push_error("AppRoot must fail closed when no runtime bearer token exists.")
		quit(1)
		return
	for missing_capability in ["skill_builds", "skill_activations", "agent_sessions", "http_world_recovery", "evidence_query"]:
		var capabilities := {
			"skill_builds": true,
			"skill_activations": true,
			"agent_sessions": true,
			"http_world_recovery": true,
			"evidence_query": true,
		}
		capabilities[missing_capability] = false
		var capability_guard := AppRootScript._validate_required_capabilities({"capabilities": capabilities})
		if bool(capability_guard.get("ok", true)) or str(capability_guard.get("error_code", "")) != "STUDENT_CAPABILITY_UNAVAILABLE":
			push_error("AppRoot must fail closed when required capability is unavailable: %s" % missing_capability)
			quit(1)
			return
	var identity_a := AppRootScript._session_create_identity({
		"world_id": "world_demo_0001",
		"learner_id": "learner_demo_0001",
		"agent_profile_id": "profile_demo_0001",
		"channel": "GAME",
		"locale": "zh-CN",
		"content": {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": "a".repeat(64)},
		"expected_world_revision": 0,
	})
	var identity_b := AppRootScript._session_create_identity({
		"expected_world_revision": 0,
		"content": {"content_hash": "a".repeat(64), "version": "1.0.0", "unit_id": "TASK_DEMO_001"},
		"locale": "zh-CN",
		"channel": "GAME",
		"agent_profile_id": "profile_demo_0001",
		"learner_id": "learner_demo_0001",
		"world_id": "world_demo_0001",
	})
	if identity_a.is_empty() or identity_a != identity_b:
		push_error("Session create logical identity must be stable across JSON object member ordering.")
		quit(1)
		return
	var source := FileAccess.get_file_as_string("res://scenes/app/app_root.gd")
	if source.contains("YAYA_SESSION_ID") or source.contains("@export var session_id"):
		push_error("AppRoot must contain no YAYA_SESSION_ID/manual Session configuration path.")
		quit(1)
		return
	var app_scene := FileAccess.get_file_as_string("res://scenes/app/app_root.tscn")
	if (
		not app_scene.contains("[node name=\"WorldEventPlayer\"")
		or not app_scene.contains("world_presentation_enabled = false")
		or not app_scene.contains("skill_patch_enabled = false")
		or not app_scene.contains("[node name=\"CropAgentBridge\"")
		or not app_scene.contains("crop_agent_bridge.gd")
		or app_scene.contains("task_workspace.tscn")
		or app_scene.contains("main.tscn")
		or app_scene.contains("FirstLevelAgentBridge")
		or app_scene.contains("horizontal_watering_agent_bridge.gd")
	):
		push_error("Formal AppRoot must assemble only the CropAdaptiveWateringDemo authority path while Skill Patch stays disabled.")
		quit(1)
		return
	if (
		not source.contains("WorldPresentationGateway.new(_transport)")
		or not source.contains("session_controller.configure_world_presentation")
		or not source.contains("ProductCapabilityGateway.new(_transport)")
		or not source.contains("await _configure_skill_patch_capability()")
		or not source.contains("crop_agent_bridge.configure(")
		or source.contains("horizontal_watering_agent_bridge")
		or source.contains("TaskWorkspace/WorldViewport")
		or source.contains("task_workspace")
	):
		push_error("Formal AppRoot did not compose additive v0.5 presentation and default-closed v0.6 Skill Patch capability Gateways.")
		quit(1)
		return
	var success_app := AppRootScript.new()
	var success_bridge := FakeProjectionBridge.new()
	success_app.crop_agent_bridge = success_bridge
	var success_completion: Array[Dictionary] = []
	success_app.startup_finished.connect(func(value: Dictionary) -> void: success_completion.append(value))
	success_app._finish({"ok": true, "session_id": "session_demo"})
	if success_bridge.activation_calls != 1 or success_completion.size() != 1 or not success_completion[0].get("ok", false):
		push_error("AppRoot success _finish must explicitly open the initial authority projection gate exactly once.")
		quit(1)
		return
	success_app.free()
	success_bridge.free()
	var failure_app := AppRootScript.new()
	var failure_bridge := FakeProjectionBridge.new()
	failure_app.crop_agent_bridge = failure_bridge
	var failure_completion: Array[Dictionary] = []
	failure_app.startup_finished.connect(func(value: Dictionary) -> void: failure_completion.append(value))
	failure_app._finish({"ok": false, "error_code": "STARTUP_FAILED", "message": "startup recovery failed"})
	if (
		failure_bridge.activation_calls != 0
		or failure_bridge.rejection_messages != ["startup recovery failed"]
		or failure_completion.size() != 1
		or failure_completion[0].get("ok", true)
	):
		push_error("AppRoot failed _finish must keep projection closed and enter the preconfigured recovery UI.")
		quit(1)
		return
	failure_app.free()
	failure_bridge.free()
	var controller := root.get_node_or_null("SessionController")
	if controller == null or controller.patch_decisions_enabled:
		push_error("Formal SessionController must default PatchDecision to fail closed.")
		quit(1)
		return
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	var quarantine_bootstrap := {
		"actor": {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]},
		"content": {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": "a".repeat(64)},
		"activation": {"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"}, "registry_revision": 0, "active": null},
	}
	if store == null or not store.bind_authority("https://api.yaya.example", quarantine_bootstrap).get("ok", false):
		push_error("Could not establish AppRoot authority quarantine test state.")
		quit(1)
		return
	store.persistence_enabled = false
	store.set_authoritative_bootstrap(quarantine_bootstrap)
	store.replace_world({"world_id": "world_demo_0001", "revision": 1, "last_event_sequence": 1, "state_schema_version": "1.0.0", "state_hash": "b".repeat(64), "world_rules_version": "rules", "state": {}})
	var quarantine_app := AppRootScript.new()
	root.add_child(quarantine_app)
	await process_frame
	if not store.world_snapshot.is_empty():
		push_error("AppRoot must quarantine a persisted World before its newly fetched Bootstrap is verified.")
		quit(1)
		return
	root.remove_child(quarantine_app)
	quarantine_app.free()
	var workspace_recovery_index := source.find("await session_controller.recover_workspace")
	var quarantine_index := source.find("client_store.begin_authority_revalidation()")
	var synchronous_bootstrap_flow_index := source.find("client_store.set_flow(WalnutClientStore.FlowState.BOOTSTRAPPING)")
	var binding_index := source.find("store.bind_authority(")
	var bootstrap_commit_index := source.find("store.set_authoritative_bootstrap(_bootstrap)")
	var session_commit_index := source.find("store.set_authoritative_session(session)")
	var revalidation_complete_index := source.find("store.complete_authority_revalidation(_bootstrap, session)")
	var pending_draft_recovery_index := source.find("await session_controller.recover_pending_draft_save_operations()")
	var pending_turn_recovery_index := source.find("await session_controller.recover_pending_turn_operations(true)")
	var ready_index := source.find("store.set_flow(WalnutClientStore.FlowState.READY)")
	if (
		quarantine_index < 0
		or synchronous_bootstrap_flow_index <= quarantine_index
		or binding_index < 0
		or bootstrap_commit_index <= binding_index
		or session_commit_index <= bootstrap_commit_index
		or revalidation_complete_index <= session_commit_index
		or workspace_recovery_index <= revalidation_complete_index
		or pending_draft_recovery_index <= workspace_recovery_index
		or pending_turn_recovery_index <= pending_draft_recovery_index
		or ready_index <= pending_turn_recovery_index
	):
		push_error("AppRoot must recover persisted Draft then Turn envelopes after Workspace and before READY.")
		quit(1)
		return
	var corrupt_path := "user://app_root_corrupt_persistence_test.json"
	var corrupt_absolute := ProjectSettings.globalize_path(corrupt_path)
	var corrupt_file := FileAccess.open(corrupt_path, FileAccess.WRITE)
	if store == null or corrupt_file == null:
		push_error("Could not establish the corrupt persistence fail-closed test seam.")
		quit(1)
		return
	corrupt_file.store_string("{}")
	corrupt_file.close()
	if store.configure_persistence(corrupt_path, true, true):
		DirAccess.remove_absolute(corrupt_absolute)
		push_error("Malformed persisted authority must not be treated as an empty cache.")
		quit(1)
		return
	var app := AppRootScript.new()
	app.runtime_environment_override = {
		"YAYA_API_BASE_URL": "http://127.0.0.1:1",
		"YAYA_AUTH_TOKEN": "not-used",
	}
	var completion := {"done": false, "value": {}}
	app.startup_finished.connect(func(value: Dictionary) -> void:
		completion.done = true
		completion.value = value.duplicate(true)
	)
	root.add_child(app)
	for _index in range(10):
		if completion.done:
			break
		await process_frame
	app.queue_free()
	await process_frame
	DirAccess.remove_absolute(corrupt_absolute)
	if (
		not completion.done
		or completion.value.get("ok", true)
		or str(completion.value.get("error_code", "")) != "CLIENT_PERSISTENCE_CORRUPT"
	):
		push_error("AppRoot must fail before networking when persisted authority is corrupt: %s" % str(completion.value))
		quit(1)
		return
	print("APP_ROOT_CONFIGURATION_TEST_PASS")
	quit(0)
