extends SceneTree

const RecoveryE2E := preload("res://tests/client/real_gateway_chain_recovery_e2e_test.gd")


func _initialize() -> void:
	var normalized_numbers: Variant = RecoveryE2E._normalize_json_integers(JSON.parse_string(
		'{"nested":{"registry_revision":2,"score":0.5,"unsafe":9007199254740992}}'
	))
	if (
		not normalized_numbers is Dictionary
		or typeof(normalized_numbers.nested.registry_revision) != TYPE_INT
		or int(normalized_numbers.nested.registry_revision) != 2
		or typeof(normalized_numbers.nested.score) != TYPE_FLOAT
		or float(normalized_numbers.nested.score) != 0.5
		or typeof(normalized_numbers.nested.unsafe) != TYPE_FLOAT
	):
		push_error("Recovery fingerprint JSON normalization must convert only finite safe integers.")
		quit(1)
		return
	var source := FileAccess.get_file_as_string("res://tests/client/real_gateway_chain_e2e_test.gd")
	if source.is_empty():
		push_error("Real Gateway E2E source is missing.")
		quit(1)
		return
	for forbidden in [
		"FakeGateway",
		"FixtureTransport",
		"LocalGatewayServer",
		"TCPServer",
		"127.0.0.1:8790",
		"world_demo",
		"session_demo",
		"HASH_",
	]:
		if source.contains(forbidden):
			push_error("Real Gateway E2E must remain fixture-free; forbidden token: %s" % forbidden)
			quit(1)
			return
	for required in [
		"YAYA_REAL_GATEWAY_E2E",
		"YAYA_API_BASE_URL",
		"YAYA_AUTH_TOKEN",
		"res://scenes/app/app_root.tscn",
		"validate_student_bootstrap_v2",
		"validate_agent_session",
		"validate_skill_build",
		"validate_run",
		"get_student_bootstrap",
		'initial_session_request.erase("expected_world_revision")',
		"BOOTSTRAP_SESSION_WORLD_NOT_CURRENT",
		"mark_draft_dirty",
		"request_save",
		"get_workspace",
		"DRAFT_MUTATION_MARKER",
		"CORRECTED_DRAFT_MARKER",
		"YAYA_DETERMINISTIC_SEED",
		"INT1_FAILURE_ROLES",
		'["teaching_agent", "teaching_agent", "bug_agent"]',
		"EXPECTED_WATERING_FAILURE_INTENT_COUNT := 5",
		"_deterministic_failure_draft",
		"_deterministic_corrected_draft",
		"_execute_failed_objective_turn",
		"_verify_failed_command_and_run",
		"_verify_failed_evidence",
		"_verify_world_unchanged",
		"failure_chain_closed",
		"correction_draft_cas_performed",
		"second_build_performed",
		"failure_draft_revision",
		"saved_draft_revision",
		"failure_workspace_revision",
		"saved_workspace_revision",
		"failure_reason",
		"TASK_INCOMPLETE",
		'"interaction_roles"',
		'"command_statuses"',
		'"run_statuses"',
		'"transport_attempt_audit"',
		'"authority_fingerprint"',
		'"live_pending_response_loss"',
		'"NOT_PROVEN"',
		'"persistence_sha256"',
		'"url": "/product-experience/v1/sessions/%s/skill-drafts/%s"',
		"build_action_finished",
		"activation_action_finished",
		"submit_action_finished",
		"CropAgentBridge",
		"CropAdaptiveWateringDemo",
		"GameFlow/CropAdaptiveWateringDemo",
		"CodeDrawer/Surface/Margin/Content/Actions/RunButton",
		"_open_crop_formal_run_ui",
		"_press_crop_run_action",
		"expect_build_activation",
		"var expected_stages: Array[String] = []",
		'expected_stages.assign(["BUILD", "ACTIVATION", "SUBMIT"])',
		"CropAgentBridge %s failed with reported_stage=%s code=%s: %s",
		'failure_index == 0',
		'CROP_SKILL_PATCH_PATH_NOT_ENABLED',
		"_verify_captured_certified_build",
		"_refresh_activated_authority",
		"normalize_api_base_url",
		"get_command",
		"get_run",
		"get_evidence",
		"get_world_events",
		"get_world_snapshot",
		"list_interactions",
		"formal_projection_state",
		"CropAdaptiveWateringDemo.Phase.COMPLETED",
		"content_draft_interaction_snapshot",
		"ui_display",
		"CAPABILITY_UNAVAILABLE",
	]:
		if not source.contains(required):
			push_error("Real Gateway E2E is missing required live-chain seam: %s" % required)
			quit(1)
			return
	for forbidden in ['"side_effect_counts"', '"no_mutating_flow_invoked"']:
		if source.contains(forbidden):
			push_error("Real Gateway E2E must use observed transport/persistence evidence, not a hard-coded self-attestation: %s" % forbidden)
			quit(1)
			return
	if source.contains('var expected_stages: Array[String] = ["BUILD", "ACTIVATION", "SUBMIT"] if'):
		push_error("Crop Run helper must not assign untyped ternary literals directly to Array[String].")
		quit(1)
		return
	for forbidden in [
		"controller.request_build", "controller.request_activation",
		"controller.request_ai_patch", "controller.decide_patch",
	]:
		if source.contains(forbidden):
			push_error("Real Gateway E2E must drive Build/Activation through the production Crop RunButton, not directly through Controller: %s" % forbidden)
			quit(1)
			return
	var retired_helper_offset := source.find("func _press_task_workspace_action")
	var int1_main := source.substr(0, retired_helper_offset) if retired_helper_offset >= 0 else source
	for forbidden in ["TaskWorkspace", "BuildButton", "ActivationButton", "SubmitButton"]:
		if int1_main.contains(forbidden):
			push_error("Crop INT1 main path still references a retired TaskWorkspace action: %s" % forbidden)
			quit(1)
			return
	var crop_bridge_source := FileAccess.get_file_as_string("res://scenes/app/crop_agent_bridge.gd")
	for required in [
		"signal build_action_finished(result: Dictionary)",
		"signal activation_action_finished(result: Dictionary)",
		"signal submit_action_finished(result: Dictionary)",
		'await _session.call("request_build")',
		'await _session.call("request_activation")',
		'await _session.call("request_submit_and_run")',
		"build_action_finished.emit",
		"activation_action_finished.emit",
		"submit_action_finished.emit",
		"_last_error_code",
		"diagnostic_message := _last_error_message",
		"diagnostic_code := _last_error_code",
	]:
		if not crop_bridge_source.contains(required):
			push_error("CropAgentBridge is missing one explicit Build/Activation/Submit completion seam: %s" % required)
			quit(1)
			return
	var crop_level_source := FileAccess.get_file_as_string("res://scenes/level_demo/crop_adaptive_watering_demo.gd")
	var crop_level_scene := FileAccess.get_file_as_string("res://scenes/level_demo/crop_adaptive_watering_demo.tscn")
	for required in [
		"run_button.pressed.connect(_request_run)",
		"agent_submit_requested.emit(code_editor.text)",
		"func open_formal_run_workspace()",
		"func load_authoritative_projection",
		"func formal_projection_state",
	]:
		if not crop_level_source.contains(required):
			push_error("CropAdaptiveWateringDemo is missing its formal Run/projection seam: %s" % required)
			quit(1)
			return
	if not crop_level_scene.contains('[node name="RunButton" type="Button"'):
		push_error("CropAdaptiveWateringDemo scene has no actual RunButton.")
		quit(1)
		return
	var controller_source := FileAccess.get_file_as_string("res://autoload/session_controller.gd")
	if controller_source.contains("request_build_activate_and_run"):
		push_error("SessionController must not expose a one-call automatic Build/Activation/Run composite.")
		quit(1)
		return
	for capability in [
		"skill_builds",
		"skill_activations",
		"agent_sessions",
		"http_world_recovery",
		"evidence_query",
	]:
		if source.count('"%s"' % capability) != 1:
			push_error("Real Gateway E2E must require capability exactly once: %s" % capability)
			quit(1)
			return
	var runner := FileAccess.get_file_as_string("res://scripts/run-real-gateway-e2e.ps1")
	if (
		runner.is_empty()
		or not runner.contains("YAYA_REAL_GATEWAY_E2E")
		or not runner.contains("YAYA_API_BASE_URL")
		or not runner.contains("YAYA_AUTH_TOKEN")
		or runner.contains("YAYA_AUTH_TOKEN =")
	):
		push_error("PowerShell runner must require environment authority without embedding a token.")
		quit(1)
		return
	var timeout_fixture := FileAccess.get_file_as_string("res://tests/client/run-real-gateway-e2e-timeout-test.ps1")
	if timeout_fixture.is_empty():
		push_error("PowerShell timeout/process-tree fixture is missing.")
		quit(1)
		return
	for field in [
		"crop_adaptive_watering_demo",
		"crop_agent_bridge",
		"run_button",
		"content_draft_interaction_snapshot",
	]:
		if runner.count("$fingerprint.ui_display.%s" % field) != 2:
			push_error("PowerShell runner must require the new UI fingerprint field in both phases: %s" % field)
			quit(1)
			return
		if timeout_fixture.count(field) != 1:
			push_error("PowerShell timeout fixture must emit the new UI fingerprint field exactly once: %s" % field)
			quit(1)
			return
	for retired_field in ["task_workspace", "dialogue_panel", "world_viewport"]:
		if runner.contains(retired_field) or timeout_fixture.contains(retired_field):
			push_error("PowerShell runner/fixture still accepts a retired UI fingerprint field: %s" % retired_field)
			quit(1)
			return
	var recovery := FileAccess.get_file_as_string("res://tests/client/real_gateway_chain_recovery_e2e_test.gd")
	if recovery.is_empty():
		push_error("Real Gateway recovery-only E2E source is missing.")
		quit(1)
		return
	for forbidden in [
		"request_save",
		"request_build",
		"request_activation",
		"request_submit_and_run",
		"create_agent_session",
		"FakeGateway",
		"FixtureTransport",
		"TCPServer",
	]:
		if recovery.contains(forbidden):
			push_error("Recovery-only E2E must not invoke or embed a mutating/fake seam: %s" % forbidden)
			quit(1)
			return
	for required in [
		"YAYA_REAL_GATEWAY_E2E_RECOVERY_ONLY",
		"real_gateway_chain_%s.json",
		"configure_persistence",
		"get_student_bootstrap",
		"RECOVERY_PREFLIGHT_HOST_UNAVAILABLE",
		"RECOVERY_PREFLIGHT_AUTHORITY_DRIFT",
		"res://scenes/app/app_root.tscn",
		"persisted_session",
		"persisted_active",
		"persisted_world",
		"authority_binding",
		"normalize_api_base_url",
		"PERSISTED_AUTHORITY_BINDING_INVALID",
		"pending_operations.is_empty",
		"validate_student_bootstrap_v2",
		"validate_agent_session",
		"skill_draft_refs",
		"world_checkpoint",
		"interactions_recovered",
		"GameFlow/CropAdaptiveWateringDemo",
		"CropAgentBridge",
		"formal_projection_state",
		"YAYA_REAL_GATEWAY_E2E_PHASE1_FINGERPRINT_PATH",
		"phase1_authority_exact_match",
		"PHASE1_AUTHORITY_FINGERPRINT_DRIFT",
		"_normalize_json_integers(parsed)",
		"MAX_SAFE_JSON_INTEGER",
		"transport_attempt_audit",
		"get_attempt_audit",
		"RECOVERY_TRANSPORT_MUTATION_ATTEMPTED",
		"live_pending_response_loss",
		"NOT_PROVEN",
		"REAL_GATEWAY_CHAIN_RECOVERY_PASS",
		"PERSISTENCE_CLEANUP_SCOPE_INVALID",
		"ProjectSettings.globalize_path(candidate)",
		"persistence_cleanup_performed",
		"persistence_cleanup_residual_count",
		'"%s.bak" % path',
		'"%s.tmp" % path',
	]:
		if not recovery.contains(required):
			push_error("Recovery-only E2E is missing required restart proof: %s" % required)
			quit(1)
			return
	for forbidden in ['"no_mutating_flow_invoked"', '"side_effect_counts"']:
		if recovery.contains(forbidden):
			push_error("Recovery-only E2E must not self-attest mutation absence: %s" % forbidden)
			quit(1)
			return
	var host_added := recovery.find("root.add_child(preflight_host)")
	var frame_barrier := recovery.find("await process_frame", host_added)
	var host_guard := recovery.find("RECOVERY_PREFLIGHT_HOST_UNAVAILABLE", frame_barrier)
	var transport_created := recovery.find("HttpTransport.new(preflight_host", host_guard)
	var bootstrap_requested := recovery.find("preflight_gateway.get_student_bootstrap", transport_created)
	if (
		host_added < 0
		or frame_barrier <= host_added
		or host_guard <= frame_barrier
		or transport_created <= host_guard
		or bootstrap_requested <= transport_created
	):
		push_error("Recovery-only preflight must wait for and validate its scene-tree host before creating the HTTP transport.")
		quit(1)
		return
	if recovery.find("RECOVERY_PREFLIGHT_AUTHORITY_DRIFT") > recovery.find("root.add_child(app)"):
		push_error("Recovery-only E2E must fail closed before AppRoot can select Session creation.")
		quit(1)
		return
	for required in [
		"[switch]$RecoveryOnly",
		"real_gateway_chain_recovery_e2e_test.gd",
		"REAL_GATEWAY_CHAIN_RECOVERY_PASS",
		"YAYA_REAL_GATEWAY_E2E_RECOVERY_ONLY",
		"[switch]$ResetPersistence",
		"[switch]$CleanupPersistence",
		"$Phase1FingerprintPath",
		"YAYA_REAL_GATEWAY_E2E_PHASE1_FINGERPRINT_PATH",
		"phase1_authority_exact_match",
		"transport_attempt_audit",
		"Live response-loss replay remains NOT_PROVEN",
		"Recovery-only mode must retain the exact phase-1 persistence file.",
		"Get-Content -LiteralPath $stdoutPath -Encoding UTF8",
		"Get-Content -LiteralPath $stderrPath -Encoding UTF8",
		"Get-Content -LiteralPath $phase1FingerprintFullPath -Raw -Encoding UTF8",
	]:
		if not runner.contains(required):
			push_error("PowerShell runner is missing the recovery-only process mode: %s" % required)
			quit(1)
			return
	for required in [
		"YAYA_REAL_GATEWAY_E2E_RESET_PERSISTENCE",
		"PERSISTENCE_RESET_SCOPE_INVALID",
		"_clear_client_cache_for_fresh_phase",
		"ProjectSettings.globalize_path(candidate)",
		"persistence_reset_performed",
		"persistence_reset_residual_count",
		'"%s.bak" % path',
		'"%s.tmp" % path',
	]:
		if not source.contains(required):
			push_error("Phase-1 E2E is missing exact test persistence reset: %s" % required)
			quit(1)
			return
	var app_root_source := FileAccess.get_file_as_string("res://scenes/app/app_root.gd")
	if not app_root_source.contains('preload("res://scripts/client/audited_http_agent_api_transport.gd")'):
		push_error("Production AppRoot must instantiate the Frontend-only audited HTTP transport wrapper.")
		quit(1)
		return
	var transport_source := FileAccess.get_file_as_string("res://scripts/client/audited_http_agent_api_transport.gd")
	for required in [
		"ATTEMPT_AUDIT_HISTORY_LIMIT",
		"get_attempt_audit",
		"reset_attempt_audit",
		"_record_attempt_started",
		"_record_attempt_completed",
		'"operation": operation',
		'"method": method_name',
		'"path": path',
		'["response_status"] = response_status',
	]:
		if not transport_source.contains(required):
			push_error("Production HTTP transport is missing bounded non-sensitive attempt evidence: %s" % required)
			quit(1)
			return
	var audit_test := FileAccess.get_file_as_string("res://tests/client/http_transport_host_lifecycle_test.gd")
	if (
		audit_test.is_empty()
		or not audit_test.contains("HTTP_TRANSPORT_HOST_LIFECYCLE_TEST_PASS")
		or not audit_test.contains("offline-lifecycle-token")
		or not audit_test.contains("history_truncated")
	):
		push_error("HTTP attempt audit focused regression is missing its bounds/redaction/reset proof.")
		quit(1)
		return
	print("REAL_GATEWAY_CHAIN_E2E_STATIC_TEST_PASS")
	quit(0)
