extends SceneTree

## Opt-in, fixture-free acceptance against an independently running Gateway.
## The test loads the real AppRoot scene and uses its production HTTP transport
## and Gateways. It never starts a local server and never substitutes a fake.

const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const RequestContexts := preload("res://autoload/request_context_factory.gd")
const REQUIRED_CAPABILITIES := [
	"skill_builds",
	"skill_activations",
	"agent_sessions",
	"http_world_recovery",
	"evidence_query",
]
const ACTIVE_TUPLE_FIELDS := [
	"activation_id",
	"skill_id",
	"skill_version_id",
	"artifact_sha256",
	"certification_id",
	"registry_revision",
	"activated_at",
]
const DEFAULT_TOTAL_DEADLINE_SECONDS := 600.0
const DEFAULT_RESOURCE_DEADLINE_SECONDS := 180.0
const DEFAULT_INTERACTION_DEADLINE_SECONDS := 90.0
const INTERACTION_RETRY_SECONDS := 0.25
const DRAFT_MUTATION_MARKER := "// INT1_REAL_GATEWAY_FAILURE_DRAFT_V1"
const CORRECTED_DRAFT_MARKER := "// INT1_REAL_GATEWAY_CORRECTED_DRAFT_V2"
const RUNTIME_SEED_ENV := "YAYA_DETERMINISTIC_SEED"
const INT1_FAILURE_ROLES := ["teaching_agent", "teaching_agent", "bug_agent"]
const M2_FAILURE_ROLES := ["teaching_agent", "teaching_agent", "bug_agent", "bug_agent"]
const EXPECTED_M2_POST_COUNT := 12
const EXPECTED_M2_PUT_COUNT := 1
const EXPECTED_M2_TURN_COUNT := 6
const EXPECTED_M2_RUN_COUNT := 5
const EXPECTED_M2_LEARNER_COUNT := 5


func _initialize() -> void:
	if OS.get_environment("YAYA_REAL_GATEWAY_E2E") != "1":
		print("REAL_GATEWAY_CHAIN_E2E_SKIP: set YAYA_REAL_GATEWAY_E2E=1 to run against a real Gateway.")
		quit(0)
		return

	var base_url := OS.get_environment("YAYA_API_BASE_URL").strip_edges()
	var bearer_token := OS.get_environment("YAYA_AUTH_TOKEN").strip_edges()
	if base_url.is_empty() or bearer_token.is_empty():
		_abort("CONFIGURATION_MISSING", "YAYA_API_BASE_URL and YAYA_AUTH_TOKEN are required in opt-in mode.")
		return
	var settings := _read_settings()
	if not settings.ok:
		_abort(str(settings.code), str(settings.message))
		return
	var absolute_deadline := Time.get_ticks_msec() + ceili(float(settings.total_deadline_seconds) * 1000.0)

	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	var controller := root.get_node_or_null("SessionController")
	if store == null or controller == null:
		_abort("AUTOLOAD_UNAVAILABLE", "ClientStore and SessionController autoloads are required.")
		return
	var persistence_identity := WalnutClientStore.normalize_api_base_url(base_url).sha256_text().left(16)
	var persistence_path := "user://real_gateway_chain_%s.json" % persistence_identity
	var reset_performed := false
	var reset_residual_count: Variant = null
	if OS.get_environment("YAYA_REAL_GATEWAY_E2E_RESET_PERSISTENCE") == "1":
		var reset_result := _remove_exact_persistence_family(persistence_path, false)
		if not reset_result.ok:
			_abort(str(reset_result.code), str(reset_result.message))
			return
		_clear_client_cache_for_fresh_phase(store)
		reset_performed = true
		reset_residual_count = int(reset_result.residual_count)
	if not store.configure_persistence(persistence_path, true, true):
		_abort("PERSISTENCE_CONFIGURATION_FAILED", "The real-chain persistence seam could not be configured.")
		return

	var packed := load("res://scenes/app/app_root.tscn") as PackedScene
	if packed == null:
		_abort("APP_ROOT_SCENE_MISSING", "The production app_root.tscn scene could not be loaded.")
		return
	var app := packed.instantiate()
	var presentation_enabled := OS.get_environment("YAYA_REAL_GATEWAY_E2E_ENABLE_WORLD_PRESENTATION") == "1"
	var skill_patch_enabled := OS.get_environment("YAYA_REAL_GATEWAY_E2E_ENABLE_SKILL_PATCH") == "1"
	var failure_roles: Array = M2_FAILURE_ROLES if skill_patch_enabled else INT1_FAILURE_ROLES
	if skill_patch_enabled and not presentation_enabled:
		_abort("SKILL_PATCH_M1_GATE_REQUIRED", "Formal Skill Patch acceptance requires authoritative World presentation in the same run.")
		return
	app.world_presentation_enabled = presentation_enabled
	app.skill_patch_enabled = skill_patch_enabled
	app.poller_settings_override = {
		"deadline_seconds": float(settings.resource_deadline_seconds),
		"interaction_deadline_seconds": float(settings.interaction_deadline_seconds),
	}
	var startup := {"done": false, "result": {}}
	app.startup_finished.connect(func(result: Dictionary) -> void:
		startup.done = true
		startup.result = result.duplicate(true)
	, Object.CONNECT_ONE_SHOT)
	root.add_child(app)
	while not bool(startup.done) and Time.get_ticks_msec() < absolute_deadline:
		await process_frame
	if not bool(startup.done):
		_abort("APP_STARTUP_TIMEOUT", "AppRoot did not finish before the total E2E deadline.")
		return
	if not bool(startup.result.get("ok", false)):
		_abort_result("APP_STARTUP_FAILED", startup.result)
		return
	if not _production_clients_are_wired(app):
		_abort("PRODUCTION_CLIENT_REQUIRED", "AppRoot did not construct the production HTTP transport and Gateways.")
		return
	var task_workspace := app.get_node_or_null("TaskWorkspace")
	if (
		task_workspace == null
		or not task_workspace.has_signal("build_action_finished")
		or not task_workspace.has_signal("activation_action_finished")
		or not task_workspace.has_signal("submit_action_finished")
		or not task_workspace.has_signal("patch_request_action_finished")
		or not task_workspace.has_signal("patch_decision_action_finished")
		or not task_workspace.has_method("_on_build_requested")
		or not task_workspace.has_method("_on_activation_requested")
		or not task_workspace.has_method("_on_submit_requested")
	):
		_abort("FORMAL_ACTION_PATH_MISSING", "AppRoot TaskWorkspace does not expose separate formal Build, Activation, Submit/Run and Patch paths.")
		return

	var bootstrap: Dictionary = store.authoritative_bootstrap.duplicate(true)
	var bootstrap_guard := ContractValidator.validate_student_bootstrap_v2(bootstrap)
	if not bootstrap_guard.ok:
		_abort("BOOTSTRAP_INVALID", str(bootstrap_guard.error.get("message", "StudentBootstrapV2 validation failed.")))
		return
	var capability_guard := _require_all_capabilities(bootstrap.capabilities)
	if not capability_guard.ok:
		_abort(str(capability_guard.code), str(capability_guard.message))
		return
	var authority_guard := _verify_startup_authority(store, bootstrap)
	if not authority_guard.ok:
		_abort(str(authority_guard.code), str(authority_guard.message))
		return
	if skill_patch_enabled:
		var m2_controls := _verify_formal_patch_controls(
			task_workspace, absolute_deadline,
		)
		if not m2_controls.get("ok", false):
			_abort(str(m2_controls.get("code", "M2_FORMAL_CONTROLS_INVALID")), str(m2_controls.get("message", "Formal M2 controls are invalid.")))
			return

	var game_gateway: RefCounted = app.get("_game_gateway")
	var product_gateway: RefCounted = app.get("_product_gateway")
	if not store.active_skill_tuple.is_empty():
		_abort("FRESH_AUTHORITY_REQUIRED", "Real acceptance requires a fresh authority with no pre-existing active Skill tuple.")
		return
	var starter_draft: Dictionary = store.draft.duplicate(true)
	var starter_workspace: Dictionary = store.workspace.duplicate(true)
	if int(starter_draft.get("revision", -1)) != 1 or int(starter_workspace.get("workspace_revision", -1)) != 1:
		_abort("STARTER_DRAFT_REQUIRED", "Real acceptance must begin from the server-created revision-1 starter Draft and Workspace.")
		return
	var starter_source := store.local_source
	var failure_source := _deterministic_failure_draft(starter_source)
	if failure_source.is_empty():
		_abort("FAILURE_DRAFT_MUTATION_INVALID", "The canonical starter source cannot produce the deterministic runtime-failure Draft.")
		return
	store.mark_draft_dirty(failure_source)
	var failure_save_result: Dictionary = await controller.request_save()
	if Time.get_ticks_msec() >= absolute_deadline:
		_abort("FAILURE_DRAFT_SAVE_DEADLINE_EXCEEDED", "Failure Draft CAS completed after the total E2E deadline.")
		return
	if not failure_save_result.get("ok", false) or store.draft_state != WalnutClientStore.DraftState.CLEAN:
		_abort_store("FAILURE_DRAFT_CAS_FAILED", "The deterministic failure Draft PUT/CAS did not close.", store)
		return
	var failure_draft: Dictionary = store.draft.duplicate(true)
	if failure_save_result.get("value") != failure_draft:
		_abort("FAILURE_DRAFT_CAS_STORE_DRIFT", "ClientStore failure Draft does not equal the canonical PUT/CAS response.")
		return
	var failure_draft_guard := _verify_saved_draft(starter_draft, failure_draft, failure_source)
	if not failure_draft_guard.ok:
		_abort(str(failure_draft_guard.code), str(failure_draft_guard.message))
		return
	var failure_workspace_result: Dictionary = await product_gateway.get_workspace(
		_new_context(bootstrap), str(store.authoritative_session.session_id),
	)
	if not failure_workspace_result.get("ok", false):
		_abort_result("FAILURE_DRAFT_WORKSPACE_QUERY_FAILED", failure_workspace_result)
		return
	var failure_workspace: Dictionary = failure_workspace_result.value
	var failure_workspace_guard := _verify_saved_draft_workspace(
		starter_workspace, failure_workspace, failure_draft,
	)
	if not failure_workspace_guard.ok:
		_abort(str(failure_workspace_guard.code), str(failure_workspace_guard.message))
		return
	store.set_workspace(failure_workspace)
	store.set_authoritative_session(failure_workspace.session)
	controller.configure_authority(bootstrap, failure_workspace.session)

	var failure_build_capture := {"value": {}}
	controller.build_resolved.connect(func(value: Dictionary) -> void:
		failure_build_capture.value = value.duplicate(true)
	, Object.CONNECT_ONE_SHOT)
	var failure_build_action: Dictionary = await _press_task_workspace_action(
		task_workspace, "BUILD", absolute_deadline,
	)
	if not failure_build_action.get("ok", false) or store.flow_state != WalnutClientStore.FlowState.CERTIFIED:
		_abort_store("FAILURE_BUILD_UI_ACTION_FAILED", "The formal Build button did not close Build/Certification.", store)
		return
	var failure_build_result: Dictionary = _verify_captured_certified_build(
		failure_build_capture.value, store, absolute_deadline, "FAILURE",
	)
	if not failure_build_result.ok:
		_abort(str(failure_build_result.code), str(failure_build_result.message))
		return
	var failure_build: Dictionary = failure_build_result.value
	var failure_activation_action: Dictionary = await _press_task_workspace_action(
		task_workspace, "ACTIVATE", absolute_deadline,
	)
	if not failure_activation_action.get("ok", false) or store.flow_state != WalnutClientStore.FlowState.ACTIVE:
		_abort_store("FAILURE_ACTIVATION_UI_ACTION_FAILED", "The formal Activation button did not publish the certified tuple.", store)
		return
	var failure_activation_result: Dictionary = await _refresh_activated_authority(
		controller, game_gateway, store, bootstrap, absolute_deadline, "FAILURE",
	)
	if not failure_activation_result.ok:
		_abort(str(failure_activation_result.code), str(failure_activation_result.message))
		return
	bootstrap = failure_activation_result.bootstrap
	var failure_active: Dictionary = store.active_skill_tuple.duplicate(true)

	var initial_world: Dictionary = store.world_snapshot.duplicate(true)
	var failure_runs: Array[Dictionary] = []
	var failure_commands: Array[Dictionary] = []
	var failure_interactions: Array[Dictionary] = []
	var failure_evidence: Array[Dictionary] = []
	var common_failure_reason := ""
	for failure_index in range(failure_roles.size()):
		var failure_turn: Dictionary = await _execute_failed_objective_turn(
			controller,
			game_gateway,
			product_gateway,
			store,
			bootstrap,
			failure_active,
			str(failure_roles[failure_index]),
			absolute_deadline,
			float(settings.interaction_deadline_seconds),
			task_workspace,
		)
		if not failure_turn.ok:
			_abort(str(failure_turn.code), str(failure_turn.message))
			return
		failure_runs.append(failure_turn.run)
		failure_commands.append(failure_turn.command)
		failure_interactions.append(failure_turn.interaction)
		failure_evidence.append_array(failure_turn.evidence)
		if common_failure_reason.is_empty():
			common_failure_reason = str(failure_turn.failure_reason)
		elif str(failure_turn.failure_reason) != common_failure_reason:
			_abort("FAILURE_REASON_DRIFT", "The objective-failure Runs did not preserve one canonical failure reason.")
			return
		var synchronized: Dictionary = await _synchronize_workspace_session(
			product_gateway, store, controller, bootstrap,
		)
		if not synchronized.ok:
			_abort(str(synchronized.code), str(synchronized.message))
			return
		if store.world_snapshot != initial_world:
			_abort("FAILURE_WORLD_AUTHORITY_MOVED", "A rejected objective Turn changed the canonical World authority.")
			return

	var saved_draft: Dictionary
	var saved_workspace: Dictionary
	var corrected_source := ""
	var patch_chain: Dictionary = {}
	if skill_patch_enabled:
		var patch_result: Dictionary = await _exercise_formal_patch_ui_chain(
			task_workspace,
			product_gateway,
			controller,
			store,
			bootstrap,
			failure_draft,
			failure_build,
			failure_active,
			failure_runs.back(),
			failure_interactions.back(),
			initial_world,
			absolute_deadline,
		)
		if not patch_result.get("ok", false):
			_abort(str(patch_result.get("code", "M2_PUBLIC_UI_CHAIN_FAILED")), str(patch_result.get("message", "Formal M2 Patch UI chain did not close.")))
			return
		patch_chain = patch_result.value
		saved_draft = patch_chain.accepted_draft.duplicate(true)
		saved_workspace = patch_chain.accepted_workspace.duplicate(true)
		corrected_source = str(patch_chain.accepted_source)
	else:
		var pre_correction_workspace: Dictionary = store.workspace.duplicate(true)
		corrected_source = _deterministic_corrected_draft(starter_source)
		if corrected_source.is_empty():
			_abort("CORRECTED_DRAFT_MUTATION_INVALID", "The starter source cannot produce the deterministic corrected Draft.")
			return
		store.mark_draft_dirty(corrected_source)
		var corrected_save_result: Dictionary = await controller.request_save()
		if Time.get_ticks_msec() >= absolute_deadline:
			_abort("CORRECTED_DRAFT_SAVE_DEADLINE_EXCEEDED", "Corrected Draft CAS completed after the total E2E deadline.")
			return
		if not corrected_save_result.get("ok", false) or store.draft_state != WalnutClientStore.DraftState.CLEAN:
			_abort_store("CORRECTED_DRAFT_CAS_FAILED", "The corrected Draft PUT/CAS did not close.", store)
			return
		saved_draft = store.draft.duplicate(true)
		if corrected_save_result.get("value") != saved_draft:
			_abort("CORRECTED_DRAFT_CAS_STORE_DRIFT", "ClientStore corrected Draft does not equal the canonical PUT/CAS response.")
			return
		var corrected_draft_guard := _verify_saved_draft(failure_draft, saved_draft, corrected_source)
		if not corrected_draft_guard.ok:
			_abort(str(corrected_draft_guard.code), str(corrected_draft_guard.message))
			return
		var corrected_workspace_result: Dictionary = await product_gateway.get_workspace(
			_new_context(bootstrap), str(store.authoritative_session.session_id),
		)
		if not corrected_workspace_result.get("ok", false):
			_abort_result("CORRECTED_DRAFT_WORKSPACE_QUERY_FAILED", corrected_workspace_result)
			return
		saved_workspace = corrected_workspace_result.value
		var corrected_workspace_guard := _verify_saved_draft_workspace(
			pre_correction_workspace, saved_workspace, saved_draft,
		)
		if not corrected_workspace_guard.ok:
			_abort(str(corrected_workspace_guard.code), str(corrected_workspace_guard.message))
			return
		store.set_workspace(saved_workspace)
		store.set_authoritative_session(saved_workspace.session)
		controller.configure_authority(bootstrap, saved_workspace.session)

	var pre_world: Dictionary = store.world_snapshot.duplicate(true)
	var pre_interaction_cursor := store.last_interaction_sequence
	var successful_build_capture := {"value": {}}
	controller.build_resolved.connect(func(value: Dictionary) -> void:
		successful_build_capture.value = value.duplicate(true)
	, Object.CONNECT_ONE_SHOT)
	var corrected_build_action: Dictionary = await _press_task_workspace_action(
		task_workspace, "BUILD", absolute_deadline,
	)
	if not corrected_build_action.get("ok", false) or store.flow_state != WalnutClientStore.FlowState.CERTIFIED:
		_abort_store("CORRECTED_BUILD_UI_ACTION_FAILED", "The formal Build button did not certify the corrected Draft.", store)
		return
	var successful_build_result: Dictionary = _verify_captured_certified_build(
		successful_build_capture.value, store, absolute_deadline, "CORRECTED",
	)
	if not successful_build_result.ok:
		_abort(str(successful_build_result.code), str(successful_build_result.message))
		return
	var build: Dictionary = successful_build_result.value
	var corrected_activation_action: Dictionary = await _press_task_workspace_action(
		task_workspace, "ACTIVATE", absolute_deadline,
	)
	if not corrected_activation_action.get("ok", false) or store.flow_state != WalnutClientStore.FlowState.ACTIVE:
		_abort_store("CORRECTED_ACTIVATION_UI_ACTION_FAILED", "The formal Activation button did not publish the corrected tuple.", store)
		return
	var successful_activation_result: Dictionary = await _refresh_activated_authority(
		controller, game_gateway, store, bootstrap, absolute_deadline, "CORRECTED",
	)
	if not successful_activation_result.ok:
		_abort(str(successful_activation_result.code), str(successful_activation_result.message))
		return
	bootstrap = successful_activation_result.bootstrap
	var active_guard := _verify_active_authority(store, bootstrap)
	if not active_guard.ok:
		_abort(str(active_guard.code), str(active_guard.message))
		return
	if store.active_skill_tuple == failure_active or int(store.active_skill_tuple.registry_revision) != 2:
		_abort("CORRECTED_ACTIVATION_NOT_ADVANCED", "The corrected Skill activation did not replace the failure version at registry revision 2.")
		return

	var captured_run := {"value": {}}
	var playback_capture := {
		"started": 0,
		"finished": 0,
		"event_ids_started": [],
		"event_ids_finished": [],
		"playing_observed": false,
	}
	if presentation_enabled:
		var player := app.get_node_or_null("WorldEventPlayer")
		if player == null:
			_abort("WORLD_PRESENTATION_PLAYER_MISSING", "Formal AppRoot has no authoritative WorldEventPlayer.")
			return
		player.playback_started.connect(func() -> void: playback_capture.started += 1)
		player.event_started.connect(func(event: Dictionary) -> void:
			playback_capture.event_ids_started.append(str(event.event_id))
			playback_capture.playing_observed = playback_capture.playing_observed or store.flow_state == WalnutClientStore.FlowState.PLAYING
		)
		player.event_finished.connect(func(event: Dictionary) -> void:
			playback_capture.event_ids_finished.append(str(event.event_id))
		)
		player.playback_finished.connect(func() -> void: playback_capture.finished += 1)
	controller.run_resolved.connect(func(value: Dictionary) -> void:
		captured_run.value = value.duplicate(true)
	, Object.CONNECT_ONE_SHOT)
	var run_submission: Dictionary = await _press_task_workspace_action(
		task_workspace, "SUBMIT", absolute_deadline,
	)
	if Time.get_ticks_msec() >= absolute_deadline:
		_abort("RUN_DEADLINE_EXCEEDED", "Run closure completed after the total E2E deadline.")
		return
	if not run_submission.get("ok", false) or store.flow_state != WalnutClientStore.FlowState.COMPLETED:
		_abort_store("RUN_CLOSURE_FAILED", "The formal Submit/Run button did not reach COMPLETED.", store)
		return
	var run: Variant = captured_run.value
	if not run is Dictionary or run.is_empty():
		_abort("RUN_RESOURCE_MISSING", "The completed flow did not expose its canonical Run.")
		return
	if presentation_enabled and (
		int(playback_capture.started) != 1
		or int(playback_capture.finished) != 1
		or playback_capture.playing_observed != true
		or playback_capture.event_ids_started.size() != 8
		or playback_capture.event_ids_started != playback_capture.event_ids_finished
	):
		_abort("WORLD_PRESENTATION_FORMAL_PLAYBACK_INVALID", "Formal successful Run did not play exactly eight ordered HARVEST actions through PLAYING.")
		return
	var run_guard := _verify_run(run, store.active_skill_tuple, pre_world)
	if not run_guard.ok:
		_abort(str(run_guard.code), str(run_guard.message))
		return
	var receipt: Dictionary = run.world_application.receipt
	var client_world_guard := _snapshot_matches_receipt(store.world_snapshot, receipt)
	if not client_world_guard.ok:
		_abort("CLIENT_WORLD_NOT_REPLACED", "ClientStore world does not equal the verified Run receipt.")
		return

	var command_run_guard: Dictionary = await _verify_command_and_run(
		game_gateway, bootstrap, run, receipt, absolute_deadline,
	)
	if not command_run_guard.ok:
		_abort(str(command_run_guard.code), str(command_run_guard.message))
		return

	var evidence_guard: Dictionary = await _verify_evidence(
		game_gateway, bootstrap, run, receipt, absolute_deadline,
	)
	if not evidence_guard.ok:
		_abort(str(evidence_guard.code), str(evidence_guard.message))
		return
	var event_guard: Dictionary = await _verify_events(
		game_gateway,
		bootstrap,
		receipt,
		int(pre_world.last_event_sequence),
		str(run.command_id),
		absolute_deadline,
	)
	if not event_guard.ok:
		_abort(str(event_guard.code), str(event_guard.message))
		return
	var snapshot_guard: Dictionary = await _verify_snapshot(
		game_gateway, bootstrap, receipt, absolute_deadline,
	)
	if not snapshot_guard.ok:
		_abort(str(snapshot_guard.code), str(snapshot_guard.message))
		return
	var interaction_verify_deadline := mini(
		absolute_deadline,
		Time.get_ticks_msec() + ceili(float(settings.interaction_deadline_seconds) * 1000.0),
	)
	var interaction_guard: Dictionary = await _verify_interaction(
		product_gateway,
		bootstrap,
		str(run.session_id),
		str(run.turn_id),
		str(run.command_id),
		str(run.run_id),
		run.agent_feedback,
		pre_interaction_cursor,
		interaction_verify_deadline,
	)
	if not interaction_guard.ok:
		_abort(str(interaction_guard.code), str(interaction_guard.message))
		return
	if str(interaction_guard.value.get("role", "")) != "book_agent":
		_abort("SUCCESS_ROLE_NOT_BOOK", "The corrected success Interaction did not close through Book authority.")
		return
	if store.last_interaction_sequence < int(interaction_guard.value.sequence):
		_abort("INTERACTION_CURSOR_NOT_PERSISTED", "ClientStore did not persist the verified AgentInteraction cursor.")
		return
	var final_workspace_result: Dictionary = await product_gateway.get_workspace(
		_new_context(bootstrap), str(store.authoritative_session.session_id),
	)
	if not final_workspace_result.get("ok", false):
		_abort_result("FINAL_WORKSPACE_QUERY_FAILED", final_workspace_result)
		return
	var final_workspace: Dictionary = final_workspace_result.value
	var final_workspace_guard := _verify_final_workspace(
		final_workspace,
		saved_workspace,
		saved_draft,
		store.world_snapshot,
		int(interaction_guard.value.sequence),
	)
	if not final_workspace_guard.ok:
		_abort(str(final_workspace_guard.code), str(final_workspace_guard.message))
		return
	store.set_workspace(final_workspace)
	store.set_authoritative_session(final_workspace.session)
	await process_frame
	var ui_guard := _verify_formal_ui_projection(
		app,
		store,
		interaction_guard.value,
		snapshot_guard.value,
	)
	if not ui_guard.ok:
		_abort(str(ui_guard.code), str(ui_guard.message))
		return
	var skill_patch_fingerprint := {
		"enabled": false,
		"status": "DISABLED",
		"backend_authority_fingerprint_required": false,
	}
	if skill_patch_enabled:
		var m2_public_read_guard: Dictionary = await _verify_m2_public_read_closure(
			game_gateway,
			product_gateway,
			bootstrap,
			patch_chain,
			saved_draft,
			build,
			store.active_skill_tuple,
			run,
			interaction_guard.value,
			absolute_deadline,
		)
		if not m2_public_read_guard.ok:
			_abort(str(m2_public_read_guard.code), str(m2_public_read_guard.message))
			return
		skill_patch_fingerprint = m2_public_read_guard.value

	var evidence_ids: Array[String] = []
	for evidence: Dictionary in failure_evidence:
		evidence_ids.append(str(evidence.get("evidence_ref", {}).get("evidence_id", "")))
	for evidence: Dictionary in evidence_guard.value:
		evidence_ids.append(str(evidence.get("evidence_ref", {}).get("evidence_id", "")))
	var turn_ids: Array[String] = []
	var command_ids: Array[String] = []
	var run_ids: Array[String] = []
	var interaction_ids: Array[String] = []
	var interaction_roles: Array[String] = []
	var run_statuses: Array[String] = []
	var command_statuses: Array[String] = []
	for failure_index in range(failure_runs.size()):
		turn_ids.append(str(failure_runs[failure_index].turn_id))
		command_ids.append(str(failure_runs[failure_index].command_id))
		run_ids.append(str(failure_runs[failure_index].run_id))
		interaction_ids.append(str(failure_interactions[failure_index].interaction_id))
		interaction_roles.append(str(failure_interactions[failure_index].role))
		run_statuses.append(str(failure_runs[failure_index].status))
		command_statuses.append(str(failure_commands[failure_index].status))
	if skill_patch_enabled:
		var patch_turn_guard := _canonical_patch_turn_id(patch_chain)
		if not patch_turn_guard.ok:
			_abort(str(patch_turn_guard.code), str(patch_turn_guard.message))
			return
		turn_ids.append(str(patch_turn_guard.value))
		command_ids.append(str(patch_chain.command.command_id))
		interaction_ids.append(str(patch_chain.decided_interaction.interaction_id))
		interaction_roles.append(str(patch_chain.decided_interaction.role))
		command_statuses.append(str(patch_chain.command.status))
	turn_ids.append(str(run.turn_id))
	command_ids.append(str(run.command_id))
	run_ids.append(str(run.run_id))
	interaction_ids.append(str(interaction_guard.value.interaction_id))
	interaction_roles.append(str(interaction_guard.value.role))
	run_statuses.append(str(run.status))
	command_statuses.append(str(command_run_guard.value.command.status))
	var persistence_bytes := FileAccess.get_file_as_string(persistence_path)
	if persistence_bytes.is_empty():
		_abort("PERSISTENCE_FINGERPRINT_MISSING", "Phase 1 did not leave a readable canonical persistence file for the recovery process.")
		return
	var transport_audit_guard := _verify_phase1_transport_audit(
		app.get("_transport"), skill_patch_enabled,
	)
	if not transport_audit_guard.ok:
		_abort(str(transport_audit_guard.code), str(transport_audit_guard.message))
		return
	var transport_attempt_audit: Dictionary = transport_audit_guard.value
	var authority_fingerprint := {
		"persistence_identity": persistence_identity,
		"persistence_sha256": persistence_bytes.sha256_text(),
		"authority_binding": store.authority_binding.duplicate(true),
		"authority_binding_sha256": JSON.stringify(store.authority_binding).sha256_text(),
		"session": store.authoritative_session.duplicate(true),
		"session_sha256": JSON.stringify(store.authoritative_session).sha256_text(),
		"workspace_id": str(final_workspace.workspace_id),
		"workspace_revision": int(final_workspace.workspace_revision),
		"workspace_sha256": JSON.stringify(final_workspace).sha256_text(),
		"draft_id": str(saved_draft.draft_id),
		"draft_revision": int(saved_draft.revision),
		"draft_sha256": str(saved_draft.draft_sha256),
		"draft_resource_sha256": JSON.stringify(saved_draft).sha256_text(),
		"draft_source_sha256": corrected_source.sha256_text(),
		"active_skill_tuple": store.active_skill_tuple.duplicate(true),
		"active_skill_tuple_sha256": _active_tuple_sha256(store.active_skill_tuple),
		"world_id": str(store.world_snapshot.world_id),
		"world_revision": int(store.world_snapshot.revision),
		"last_event_sequence": int(store.world_snapshot.last_event_sequence),
		"world_state_hash": str(store.world_snapshot.state_hash),
		"presentation_high_watermark": int(app.get_node("WorldEventPlayer").get_cursor()) if presentation_enabled else 0,
		"world_snapshot_sha256": JSON.stringify(store.world_snapshot).sha256_text(),
		"interaction_id": str(interaction_guard.value.interaction_id),
		"turn_id": str(interaction_guard.value.turn_id),
		"interaction_sequence": int(interaction_guard.value.sequence),
		"interaction_revision": int(interaction_guard.value.interaction_revision),
		"interaction_role": str(interaction_guard.value.role),
		"interaction_sha256": JSON.stringify(interaction_guard.value).sha256_text(),
		"interaction_feedback": interaction_guard.value.feedback.duplicate(true),
		"interaction_feedback_sha256": JSON.stringify(interaction_guard.value.feedback).sha256_text(),
	}
	print("REAL_GATEWAY_CHAIN_E2E_PASS %s" % JSON.stringify({
		"phase1_fingerprint_schema": "1.0.0",
		"api_store_closure": {
			"draft_cas_performed": true,
			"failure_chain_closed": true,
			"correction_draft_cas_performed": not skill_patch_enabled,
			"patch_decision_performed": skill_patch_enabled,
			"second_build_performed": true,
			"build_performed": true,
			"run_closed": true,
		},
		"persistence_reset_performed": reset_performed,
		"persistence_reset_residual_count": reset_residual_count,
		"persistence_identity": persistence_identity,
		"persistence_sha256": persistence_bytes.sha256_text(),
		"starter_draft_revision": int(starter_draft.revision),
		"failure_draft_revision": int(failure_draft.revision),
		"saved_draft_revision": int(saved_draft.revision),
		"starter_workspace_revision": int(starter_workspace.workspace_revision),
		"failure_workspace_revision": int(failure_workspace.workspace_revision),
		"saved_workspace_revision": int(saved_workspace.workspace_revision),
		"failure_draft_source_sha256": failure_source.sha256_text(),
		"failure_draft_sha256": str(failure_draft.draft_sha256),
		"draft_source_sha256": corrected_source.sha256_text(),
		"draft_sha256": str(saved_draft.draft_sha256),
		"failure_build_source_sha256": str(failure_build.get("artifact", {}).get("source_sha256", "")),
		"build_source_sha256": str(build.get("artifact", {}).get("source_sha256", "")),
		"session_id": str(store.authoritative_session.session_id),
		"workspace_id": str(final_workspace.workspace_id),
		"final_workspace_revision": int(final_workspace.workspace_revision),
		"final_workspace_sha256": JSON.stringify(final_workspace).sha256_text(),
		"draft_id": str(saved_draft.draft_id),
		"build_id": str(build.get("build_id", "")),
		"build_ids": [str(failure_build.get("build_id", "")), str(build.get("build_id", ""))],
		"activation_id": str(store.active_skill_tuple.activation_id),
		"activation_ids": [str(failure_active.activation_id), str(store.active_skill_tuple.activation_id)],
		"active_skill_tuple": store.active_skill_tuple.duplicate(true),
		"active_skill_tuple_sha256": _active_tuple_sha256(store.active_skill_tuple),
		"turn_id": str(run.turn_id),
		"turn_ids": turn_ids,
		"command_id": str(run.command_id),
		"command_ids": command_ids,
		"command_statuses": command_statuses,
		"run_id": str(run.run_id),
		"run_ids": run_ids,
		"run_statuses": run_statuses,
		"failure_reason": common_failure_reason,
		"world_id": str(receipt.world_id),
		"world_revision": int(receipt.world_revision),
		"last_event_sequence": int(receipt.last_event_sequence),
		"world_state_hash": str(receipt.state_hash),
		"world_presentation": {
			"enabled": presentation_enabled,
			"playback_started": int(playback_capture.started),
			"playback_finished": int(playback_capture.finished),
			"playing_observed": playback_capture.playing_observed == true,
			"event_ids_started": playback_capture.event_ids_started.duplicate(),
			"event_ids_finished": playback_capture.event_ids_finished.duplicate(),
			"presentation_high_watermark": int(app.get_node("WorldEventPlayer").get_cursor()) if presentation_enabled else 0,
		},
		"skill_patch": skill_patch_fingerprint,
		"evidence_count": int(evidence_ids.size()),
		"evidence_ids": evidence_ids,
		"interaction_id": str(interaction_guard.value.interaction_id),
		"interaction_ids": interaction_ids,
		"interaction_roles": interaction_roles,
		"interaction_sequence": int(interaction_guard.value.sequence),
		"interaction_revision": int(interaction_guard.value.interaction_revision),
		"interaction_role": str(interaction_guard.value.role),
		"interaction_feedback_sha256": JSON.stringify(interaction_guard.value.feedback).sha256_text(),
		"transport_attempt_audit": transport_attempt_audit,
		"authority_fingerprint": authority_fingerprint,
		"live_pending_response_loss": {
			"status": "NOT_PROVEN",
			"reason": "The live Gateway contract exposes no acceptance-safe response-loss fault injection; focused cross-store recovery tests cover this path offline.",
		},
		"ui_display": ui_guard.value,
	}))
	quit(0)


static func _canonical_patch_turn_id(patch_chain: Dictionary) -> Dictionary:
	var command: Variant = patch_chain.get("command")
	var decided_interaction: Variant = patch_chain.get("decided_interaction")
	if (
		not command is Dictionary
		or command.has("turn_id")
		or not decided_interaction is Dictionary
		or str(decided_interaction.get("turn_id", "")).is_empty()
	):
		return {
			"ok": false,
			"code": "M2_PATCH_TURN_ID_INVALID",
			"message": "The fifth no-Run Patch Turn must be identified by its canonical decided Interaction, not CommandResult.",
		}
	return {"ok": true, "value": str(decided_interaction.turn_id)}


func _verify_m2_public_read_closure(
	game_gateway: RefCounted,
	product_gateway: RefCounted,
	bootstrap: Dictionary,
	patch_chain: Dictionary,
	accepted_draft: Dictionary,
	build: Dictionary,
	active: Dictionary,
	run: Dictionary,
	final_interaction: Dictionary,
	absolute_deadline: int,
) -> Dictionary:
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("M2_PUBLIC_READ_DEADLINE_EXCEEDED", "M2 public read closure began after the total deadline.")
	var session_id := str(accepted_draft.session_id)
	var proposal_command_result: Dictionary = await game_gateway.get_command(
		_new_context(bootstrap), str(patch_chain.command.command_id),
	)
	var proposal_interaction_result: Dictionary = await product_gateway.get_interaction(
		_new_context(bootstrap), session_id, str(patch_chain.decided_interaction.interaction_id),
	)
	var accepted_draft_result: Dictionary = await product_gateway.get_draft(
		_new_context(bootstrap), session_id, str(accepted_draft.draft_id),
	)
	var build_result: Dictionary = await game_gateway.get_skill_build(
		_new_context(bootstrap), str(build.build_id),
	)
	var activation_result: Dictionary = await game_gateway.get_skill_activation(
		_new_context(bootstrap), str(active.activation_id),
	)
	var run_result: Dictionary = await game_gateway.get_run(
		_new_context(bootstrap), str(run.run_id),
	)
	var final_interaction_result: Dictionary = await product_gateway.get_interaction(
		_new_context(bootstrap), session_id, str(final_interaction.interaction_id),
	)
	for named_result in [
		{"name": "proposal Command", "result": proposal_command_result},
		{"name": "proposal Interaction", "result": proposal_interaction_result},
		{"name": "accepted Draft", "result": accepted_draft_result},
		{"name": "corrected Build", "result": build_result},
		{"name": "corrected Activation", "result": activation_result},
		{"name": "successful Run", "result": run_result},
		{"name": "final Interaction", "result": final_interaction_result},
	]:
		if not named_result.result.get("ok", false):
			return _gateway_failure("M2_PUBLIC_READ_FAILED", named_result.result)
	if (
		proposal_command_result.value != patch_chain.command
		or proposal_interaction_result.value != patch_chain.decided_interaction
		or accepted_draft_result.value != accepted_draft
		or build_result.value != build
		or run_result.value != run
		or final_interaction_result.value != final_interaction
	):
		return _failure("M2_PUBLIC_RESOURCE_DRIFT", "A canonical M2 GET returned bytes different from the resources closed by the UI flow.")
	var activation: Dictionary = activation_result.value
	var activation_validation := ContractValidator.validate_skill_activation(activation)
	var build_validation := ContractValidator.validate_skill_build(build_result.value)
	var run_validation := ContractValidator.validate_run(run_result.value)
	if (
		not activation_validation.ok
		or not build_validation.ok
		or not run_validation.ok
		or str(build.skill_id) != str(active.skill_id)
		or str(build.skill_version_id) != str(active.skill_version_id)
		or str(build.artifact.artifact_sha256) != str(active.artifact_sha256)
		or str(build.certification.certification_id) != str(active.certification_id)
		or str(activation.activation_id) != str(active.activation_id)
		or str(activation.skill_id) != str(active.skill_id)
		or str(activation.skill_version_id) != str(active.skill_version_id)
		or str(activation.artifact_sha256) != str(active.artifact_sha256)
		or str(activation.certification_id) != str(active.certification_id)
		or int(activation.registry_revision) != int(active.registry_revision)
		or str(activation.activated_at) != str(active.activated_at)
		or run.skill != {
			"skill_id": active.skill_id,
			"skill_version_id": active.skill_version_id,
			"artifact_sha256": active.artifact_sha256,
			"certification_id": active.certification_id,
		}
		or int(patch_chain.decided_interaction.sequence) != EXPECTED_M2_TURN_COUNT - 1
		or int(final_interaction.sequence) != EXPECTED_M2_TURN_COUNT
		or str(final_interaction.feedback.run_id) != str(run.run_id)
	):
		return _failure("M2_PUBLIC_RESOURCE_CROSS_LINK_INVALID", "Public Draft/Build/Activation/Run/Interaction resources do not form one exact M2 authority chain.")
	var public_hashes := {
		"proposal_command_sha256": JSON.stringify(proposal_command_result.value).sha256_text(),
		"proposal_interaction_sha256": JSON.stringify(proposal_interaction_result.value).sha256_text(),
		"patch_sha256": str(patch_chain.patch.patch_sha256),
		"decision_sha256": JSON.stringify(patch_chain.decision).sha256_text(),
		"accepted_draft_resource_sha256": JSON.stringify(accepted_draft_result.value).sha256_text(),
		"build_resource_sha256": JSON.stringify(build_result.value).sha256_text(),
		"activation_resource_sha256": JSON.stringify(activation).sha256_text(),
		"run_resource_sha256": JSON.stringify(run_result.value).sha256_text(),
		"final_interaction_sha256": JSON.stringify(final_interaction_result.value).sha256_text(),
	}
	return {"ok": true, "value": {
		"enabled": true,
		"status": "PUBLIC_UI_CHAIN_CLOSED",
		"backend_authority_fingerprint_required": true,
		"expected_transport_counts": {
			"POST": EXPECTED_M2_POST_COUNT,
			"PUT": EXPECTED_M2_PUT_COUNT,
		},
		"expected_backend_counts": {
			"turns": EXPECTED_M2_TURN_COUNT,
			"runs": EXPECTED_M2_RUN_COUNT,
			"learner_jobs": EXPECTED_M2_LEARNER_COUNT,
		},
		"formal_actions": ["REQUEST_PATCH", "ACCEPT_PATCH", "BUILD", "ACTIVATE", "SUBMIT"],
		"proposal_interaction_id": str(patch_chain.decided_interaction.interaction_id),
		"patch_id": str(patch_chain.patch.patch_id),
		"decision_id": str(patch_chain.decision.decision_id),
		"accepted_draft_id": str(accepted_draft.draft_id),
		"build_id": str(build.build_id),
		"activation_id": str(activation.activation_id),
		"run_id": str(run.run_id),
		"public_terminal_run_get_validated_learner_projection": true,
		"public_hashes": public_hashes,
		"public_chain_sha256": JSON.stringify([
			public_hashes.proposal_command_sha256,
			public_hashes.proposal_interaction_sha256,
			public_hashes.patch_sha256,
			public_hashes.decision_sha256,
			public_hashes.accepted_draft_resource_sha256,
			public_hashes.build_resource_sha256,
			public_hashes.activation_resource_sha256,
			public_hashes.run_resource_sha256,
			public_hashes.final_interaction_sha256,
		]).sha256_text(),
	}}


func _verify_phase1_transport_audit(transport: Variant, skill_patch_enabled: bool = false) -> Dictionary:
	if not transport is Object or not transport.has_method("get_attempt_audit"):
		return _failure("TRANSPORT_ATTEMPT_AUDIT_UNAVAILABLE", "The production HTTP transport exposes no queryable attempt audit.")
	var audit: Dictionary = transport.get_attempt_audit()
	if (
		int(audit.get("total_started", -1)) <= 0
		or int(audit.get("total_started", -1)) != int(audit.get("total_completed", -2))
	):
		return _failure("TRANSPORT_ATTEMPT_AUDIT_INCOMPLETE", "The production HTTP attempt audit contains no traffic or an unfinished request.")
	var method_counts: Variant = audit.get("method_counts")
	var operation_counts: Variant = audit.get("operation_counts")
	if not method_counts is Dictionary or not operation_counts is Dictionary:
		return _failure("TRANSPORT_ATTEMPT_AUDIT_INVALID", "The production HTTP attempt audit has no method/operation aggregates.")
	var expected_mutations := (
		{
			"create_agent_session": 1,
			"upsert_product_skill_draft": 1,
			"submit_skill_build": 2,
			"activate_skill_version": 2,
			"submit_agent_turn": 6,
			"record_product_patch_decision": 1,
		}
		if skill_patch_enabled
		else {
			"create_agent_session": 1,
			"upsert_product_skill_draft": 2,
			"submit_skill_build": 2,
			"activate_skill_version": 2,
			"submit_agent_turn": 4,
		}
	)
	for operation in expected_mutations:
		if int(operation_counts.get(operation, 0)) != int(expected_mutations[operation]):
			return _failure("TRANSPORT_MUTATION_COUNT_MISMATCH", "Production HTTP operation %s was not attempted exactly %d time(s)." % [operation, expected_mutations[operation]])
	var expected_post_count := EXPECTED_M2_POST_COUNT if skill_patch_enabled else 9
	var expected_put_count := EXPECTED_M2_PUT_COUNT if skill_patch_enabled else 2
	if (
		int(method_counts.get("POST", 0)) != expected_post_count
		or int(method_counts.get("PUT", 0)) != expected_put_count
		or int(method_counts.get("PATCH", 0)) != 0
		or int(method_counts.get("DELETE", 0)) != 0
	):
		return _failure("TRANSPORT_MUTATION_METHOD_MISMATCH", "Production HTTP method audit does not equal the exact phase-1 write boundary (%d POST, %d PUT, 0 PATCH/DELETE)." % [expected_post_count, expected_put_count])
	return {"ok": true, "value": audit.duplicate(true)}


func _remove_exact_persistence_family(path: String, require_target: bool) -> Dictionary:
	if not path.begins_with("user://real_gateway_chain_") or not path.ends_with(".json"):
		return _failure("PERSISTENCE_RESET_SCOPE_INVALID", "Refusing to remove a path outside the exact real-chain test identity.")
	if require_target and not FileAccess.file_exists(path):
		return _failure("PERSISTENCE_RESET_MISSING", "The exact real-chain persistence target is absent.")
	var candidates := [path, "%s.bak" % path, "%s.tmp" % path]
	for candidate in candidates:
		if not FileAccess.file_exists(candidate):
			continue
		var error := DirAccess.remove_absolute(ProjectSettings.globalize_path(candidate))
		if error != OK:
			return _failure("PERSISTENCE_RESET_FAILED", "An exact real-chain persistence target, backup, or temporary file could not be removed.")
	for candidate in candidates:
		if FileAccess.file_exists(candidate):
			return _failure("PERSISTENCE_RESET_RESIDUAL", "An exact real-chain persistence target, backup, or temporary file remains after reset.")
	return {"ok": true, "residual_count": 0}


func _clear_client_cache_for_fresh_phase(store: WalnutClientStore) -> void:
	# The autoload may have read the normal application persistence file before
	# this opt-in test selects its hash-scoped file.  Clear only in-memory client
	# cache; the fresh server authority still creates every business resource.
	store.workspace.clear()
	store.content.clear()
	store.draft.clear()
	store.local_source = ""
	store.draft_state = WalnutClientStore.DraftState.CLEAN
	store.flow_state = WalnutClientStore.FlowState.BOOTSTRAPPING
	store.world_snapshot.clear()
	store.last_applied_sequence = 0
	store.applied_event_ids.clear()
	store.objective_result.clear()
	store.last_error.clear()
	store.authoritative_bootstrap.clear()
	store.authoritative_session.clear()
	store.activation_authority.clear()
	store.active_skill_tuple.clear()
	store.authority_binding.clear()
	store.pending_operations.clear()
	store.last_interaction_sequence = 0


func _deterministic_failure_draft(source: String) -> String:
	var include_anchor := "#include <iostream>"
	var loop_anchor := "    for (int index = 1; index <= length; ++index) {"
	if (
		source.contains(DRAFT_MUTATION_MARKER)
		or source.contains(CORRECTED_DRAFT_MARKER)
		or source.contains(RUNTIME_SEED_ENV)
		or source.count(include_anchor) != 1
		or source.count(loop_anchor) != 1
	):
		return ""
	var mutated := source.replace(include_anchor, "#include <cstdlib>\n%s" % include_anchor)
	mutated = mutated.replace(loop_anchor, """    if (std::getenv(\"%s\") != nullptr && length > 0) {
        --length;
    }
%s""" % [RUNTIME_SEED_ENV, loop_anchor])
	return "%s%s%s\n" % [mutated, "" if mutated.ends_with("\n") else "\n", DRAFT_MUTATION_MARKER]


func _deterministic_corrected_draft(source: String) -> String:
	if source.contains(DRAFT_MUTATION_MARKER) or source.contains(CORRECTED_DRAFT_MARKER):
		return ""
	return "%s%s%s\n" % [source, "" if source.ends_with("\n") else "\n", CORRECTED_DRAFT_MARKER]


func _press_task_workspace_action(
	task_workspace: Node,
	action: String,
	absolute_deadline: int,
) -> Dictionary:
	var specs := {
		"BUILD": {"signal": "build_action_finished", "button": "BuildButton"},
		"ACTIVATE": {"signal": "activation_action_finished", "button": "ActivationButton"},
		"SUBMIT": {"signal": "submit_action_finished", "button": "SubmitButton"},
		"REQUEST_PATCH": {"signal": "patch_request_action_finished", "button": "RequestAiPatchButton"},
		"ACCEPT_PATCH": {"signal": "patch_decision_action_finished", "button": "CodePatchDialog"},
	}
	if not specs.has(action):
		return _failure("FORMAL_ACTION_INVALID", "Unknown TaskWorkspace action: %s" % action)
	var spec: Dictionary = specs[action]
	var completion := {"done": false, "result": {}}
	if action == "ACCEPT_PATCH":
		task_workspace.connect(str(spec.signal), func(_decision: String, result: Dictionary) -> void:
			completion.done = true
			completion.result = result.duplicate(true)
		, Object.CONNECT_ONE_SHOT)
	else:
		task_workspace.connect(str(spec.signal), func(result: Dictionary) -> void:
			completion.done = true
			completion.result = result.duplicate(true)
		, Object.CONNECT_ONE_SHOT)
	var button_path := (
		"Hud/SafeArea/EdgeLayer/ToolRail/RequestAiPatchButton"
		if action == "REQUEST_PATCH"
		else "DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/RunControlPanel/Actions/%s" % str(spec.button)
	)
	var button := task_workspace.get_node_or_null(button_path) as Button
	if action == "ACCEPT_PATCH":
		var dialog: Variant = task_workspace.get("patch_dialog")
		button = dialog.get_ok_button() if dialog is ConfirmationDialog else null
	if button == null:
		return _failure("FORMAL_ACTION_BUTTON_MISSING", "TaskWorkspace has no formal %s signal source." % str(spec.button))
	if button.disabled:
		return _failure("FORMAL_ACTION_BUTTON_DISABLED", "TaskWorkspace %s is disabled before its explicit stage." % str(spec.button))
	button.pressed.emit()
	while not bool(completion.done) and Time.get_ticks_msec() < absolute_deadline:
		await process_frame
	if not bool(completion.done):
		return _failure("FORMAL_ACTION_PATH_TIMEOUT", "TaskWorkspace %s signal did not close before the total E2E deadline." % action)
	var result: Variant = completion.result
	if not result is Dictionary:
		return _failure("FORMAL_ACTION_RESULT_INVALID", "TaskWorkspace %s signal returned no structured result." % action)
	return result


func _verify_formal_patch_controls(
	task_workspace: Node,
	absolute_deadline: int,
) -> Dictionary:
	var request_button := task_workspace.get_node_or_null(
		"Hud/SafeArea/EdgeLayer/ToolRail/RequestAiPatchButton",
	) as Button
	if request_button == null:
		return _failure("FORMAL_PATCH_REQUEST_BUTTON_MISSING", "TaskWorkspace has no RequestAiPatchButton.")
	var dialog: Variant = task_workspace.get("patch_dialog")
	if not dialog is ConfirmationDialog or str(dialog.name) != "CodePatchDialog":
		return _failure("FORMAL_PATCH_DIALOG_MISSING", "TaskWorkspace has no formal CodePatchDialog.")
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("FORMAL_PATCH_SEAM_DEADLINE_EXCEEDED", "M2 UI seam was inspected after the total deadline.")
	if request_button.visible or not request_button.disabled or dialog.visible:
		return _failure("FORMAL_PATCH_PREMATURELY_AVAILABLE", "Patch controls became actionable before a visible objective failure existed.")
	return {"ok": true}


func _exercise_formal_patch_ui_chain(
	task_workspace: Node,
	product_gateway: RefCounted,
	controller: Node,
	store: WalnutClientStore,
	bootstrap: Dictionary,
	failure_draft: Dictionary,
	failure_build: Dictionary,
	failure_active: Dictionary,
	failure_run: Dictionary,
	failure_interaction: Dictionary,
	initial_world: Dictionary,
	absolute_deadline: int,
) -> Dictionary:
	if not controller.has_method("recover_patch_failure_authority"):
		return _failure("FORMAL_PATCH_FAILURE_RECOVERY_SEAM_MISSING", "SessionController cannot revalidate the persisted failed-Run authority through public GETs.")
	var visible_failures: Array[Dictionary] = [failure_interaction.duplicate(true)]
	var failure_recovery: Dictionary = await controller.recover_patch_failure_authority(visible_failures)
	if not failure_recovery.get("ok", false):
		return _gateway_failure("FORMAL_PATCH_FAILURE_AUTHORITY_RECOVERY_FAILED", failure_recovery)
	if (
		not bool(failure_recovery.get("value", {}).get("recovered", false))
		or controller.get("certified_build") != failure_build
		or controller.get("active_skill_tuple") != failure_active
	):
		return _failure("FORMAL_PATCH_FAILURE_AUTHORITY_RECOVERY_DRIFT", "Public Build/Run/Evidence GET recovery did not restore the exact fourth failed-Run authority.")
	var request_button := task_workspace.get_node_or_null(
		"Hud/SafeArea/EdgeLayer/ToolRail/RequestAiPatchButton",
	) as Button
	if request_button == null or not request_button.visible or request_button.disabled:
		return _failure("FORMAL_PATCH_REQUEST_NOT_ACTIONABLE", "RequestAiPatchButton is not explicitly actionable after four visible objective failures.")
	var draft_before: Dictionary = store.draft.duplicate(true)
	var workspace_before: Dictionary = store.workspace.duplicate(true)
	var world_before: Dictionary = store.world_snapshot.duplicate(true)
	var active_before: Dictionary = store.active_skill_tuple.duplicate(true)
	var request: Dictionary = await _press_task_workspace_action(
		task_workspace, "REQUEST_PATCH", absolute_deadline,
	)
	if not request.get("ok", false):
		return _gateway_failure("FORMAL_PATCH_REQUEST_FAILED", request)
	if (
		store.draft != draft_before
		or store.workspace != workspace_before
		or store.world_snapshot != world_before
		or store.active_skill_tuple != active_before
	):
		return _failure("FORMAL_PATCH_REQUEST_SIDE_EFFECT", "Requesting a Patch changed Draft, Workspace, World, or active Skill before student confirmation.")
	var proposal_guard := _verify_patch_proposal(
		controller,
		request,
		failure_draft,
		failure_build,
		failure_active,
		failure_run,
		failure_interaction,
	)
	if not proposal_guard.ok:
		return proposal_guard
	var proposal_interaction: Dictionary = proposal_guard.value.interaction
	var proposal: Dictionary = proposal_guard.value.patch
	var dialog: Variant = task_workspace.get("patch_dialog")
	if (
		not dialog is ConfirmationDialog
		or not dialog.visible
	):
		return _failure("FORMAL_PATCH_PREVIEW_MISSING", "The public proposal did not open the formal Patch preview.")
	var preview_guard := _verify_visible_patch_preview(str(dialog.dialog_text), proposal, failure_draft)
	if not preview_guard.ok:
		return preview_guard
	var proposal_workspace_result: Dictionary = await _synchronize_workspace_session(
		product_gateway, store, controller, bootstrap,
	)
	if not proposal_workspace_result.ok:
		return proposal_workspace_result
	var proposal_workspace: Dictionary = proposal_workspace_result.value
	if (
		store.draft != failure_draft
		or store.world_snapshot != initial_world
		or store.active_skill_tuple != failure_active
	):
		return _failure("FORMAL_PATCH_PROPOSAL_AUTHORITY_MOVED", "Proposal synchronization changed Draft, World, or active Skill authority before ACCEPT.")
	var accept: Dictionary = await _press_task_workspace_action(
		task_workspace, "ACCEPT_PATCH", absolute_deadline,
	)
	if not accept.get("ok", false):
		return _gateway_failure("FORMAL_PATCH_ACCEPT_FAILED", accept)
	var canonical_interaction_result: Dictionary = await product_gateway.get_interaction(
		_new_context(bootstrap), str(proposal_interaction.session_id),
		str(proposal_interaction.interaction_id),
	)
	if not canonical_interaction_result.get("ok", false):
		return _gateway_failure("FORMAL_PATCH_DECIDED_INTERACTION_QUERY_FAILED", canonical_interaction_result)
	var canonical_draft_result: Dictionary = await product_gateway.get_draft(
		_new_context(bootstrap), str(failure_draft.session_id), str(failure_draft.draft_id),
	)
	if not canonical_draft_result.get("ok", false):
		return _gateway_failure("FORMAL_PATCH_ACCEPTED_DRAFT_QUERY_FAILED", canonical_draft_result)
	var decision_guard := _verify_accepted_patch_decision(
		proposal_interaction,
		proposal,
		accept.value,
		canonical_interaction_result.value,
		failure_draft,
		canonical_draft_result.value,
	)
	if not decision_guard.ok:
		return decision_guard
	if store.draft != canonical_draft_result.value:
		return _failure("FORMAL_PATCH_ACCEPTED_DRAFT_STORE_DRIFT", "ClientStore does not equal the accepted canonical Draft GET.")
	if (
		store.flow_state != WalnutClientStore.FlowState.READY
		or not store.active_skill_tuple.is_empty()
		or not controller.get("certified_build").is_empty()
		or not controller.get("active_skill_tuple").is_empty()
		or store.world_snapshot != initial_world
	):
		return _failure("FORMAL_PATCH_AUTO_EXECUTION_DETECTED", "ACCEPT did not stop at READY with prior Build/Activation invalidated and World unchanged.")
	var accepted_workspace_result: Dictionary = await product_gateway.get_workspace(
		_new_context(bootstrap), str(store.authoritative_session.session_id),
	)
	if not accepted_workspace_result.get("ok", false):
		return _gateway_failure("FORMAL_PATCH_ACCEPTED_WORKSPACE_QUERY_FAILED", accepted_workspace_result)
	var accepted_workspace: Dictionary = accepted_workspace_result.value
	var workspace_guard := _verify_saved_draft_workspace(
		proposal_workspace, accepted_workspace, canonical_draft_result.value,
	)
	if not workspace_guard.ok:
		return workspace_guard
	if int(accepted_workspace.get("last_interaction_sequence", -1)) != int(proposal_interaction.sequence):
		return _failure("FORMAL_PATCH_ACCEPTED_WORKSPACE_CURSOR_DRIFT", "PatchDecision changed the Turn/Interaction cursor instead of only revising the proposal and Draft.")
	store.set_workspace(accepted_workspace)
	store.set_authoritative_session(accepted_workspace.session)
	controller.configure_authority(bootstrap, accepted_workspace.session)
	return {"ok": true, "value": {
		"command": proposal_guard.value.command,
		"proposal_interaction": proposal_interaction,
		"decided_interaction": canonical_interaction_result.value.duplicate(true),
		"patch": proposal.duplicate(true),
		"decision": accept.value.duplicate(true),
		"accepted_draft": canonical_draft_result.value.duplicate(true),
		"accepted_source": str(proposal_guard.value.accepted_source),
		"proposal_workspace": proposal_workspace,
		"accepted_workspace": accepted_workspace,
	}}


func _verify_patch_proposal(
	controller: Node,
	request: Dictionary,
	base_draft: Dictionary,
	failure_build: Dictionary,
	failure_active: Dictionary,
	failure_run: Dictionary,
	failure_interaction: Dictionary,
) -> Dictionary:
	var value: Variant = request.get("value")
	var command: Variant = value.get("command") if value is Dictionary else null
	var interaction: Variant = value.get("interaction") if value is Dictionary else null
	if not command is Dictionary or not interaction is Dictionary:
		return _failure("FORMAL_PATCH_PROPOSAL_RESOURCES_MISSING", "Patch request exposed no canonical Command and AgentInteraction pair.")
	var command_validation := ContractValidator.validate_command_result(command)
	var proposal_validation: Dictionary = controller.call("validate_minimal_skill_patch_interaction", interaction)
	var feedback: Variant = interaction.get("feedback")
	var patch: Variant = interaction.get("skill_patch")
	var selected_feedback: Variant = failure_interaction.get("feedback")
	if (
		not command_validation.ok
		or not proposal_validation.get("ok", false)
		or not feedback is Dictionary
		or not patch is Dictionary
		or not selected_feedback is Dictionary
		or str(command.get("command_type", "")) != "EXECUTE_AGENT_TURN"
		or str(command.get("status", "")) != "APPLIED"
		or not bool(command.get("terminal", false))
		or command.get("result") != {"result_type": "NO_EFFECT", "reason_code": "SKILL_PATCH_PROPOSED"}
		or command.get("links", {}).has("run")
		or str(interaction.get("session_id", "")) != str(base_draft.session_id)
		or int(interaction.get("sequence", -1)) != int(failure_interaction.sequence) + 1
		or int(interaction.get("interaction_revision", -1)) != 1
		or str(interaction.get("role", "")) != "teaching_agent"
		or str(interaction.get("response_type", "")) != "skill_patch"
		or int(interaction.get("hint_level", -1)) != 4
		or interaction.get("patch_decision") != null
		or str(feedback.get("command_id", "")) != str(command.command_id)
		or feedback.get("run_id") != null
		or str(feedback.get("source", "")) != "provider"
		or bool(feedback.get("degraded", true))
		or feedback.get("fallback_reason") != null
		or feedback.get("evidence_refs") != selected_feedback.get("evidence_refs")
		or patch.get("evidence_refs") != selected_feedback.get("evidence_refs")
		or selected_feedback != failure_run.get("agent_feedback")
	):
		return _failure("FORMAL_PATCH_PROPOSAL_INVALID", "Patch request did not close as the exact fifth no-Run teaching proposal bound to the fourth failed Run.")
	var controller_build: Variant = controller.get("certified_build")
	var controller_active: Variant = controller.get("active_skill_tuple")
	if controller_build != failure_build or controller_active != failure_active:
		return _failure("FORMAL_PATCH_PROPOSAL_SKILL_AUTHORITY_DRIFT", "Patch request changed the selected failed Build or active Skill authority.")
	var operations: Array = patch.operations
	var operation: Dictionary = operations[0]
	var base_bundle: Dictionary = base_draft.source_bundle.duplicate(true)
	var accepted_source := str(operation.content)
	var entrypoint_count := 0
	for index in range(base_bundle.files.size()):
		var file: Variant = base_bundle.files[index]
		if file is Dictionary and str(file.get("path", "")) == str(base_bundle.entrypoint):
			entrypoint_count += 1
			file["content"] = accepted_source
			file["content_sha256"] = accepted_source.sha256_text()
			base_bundle.files[index] = file
	var expected_result_sha := ContractValidator.canonical_json_sha256_v1({
		"session_id": base_draft.session_id,
		"draft_id": base_draft.draft_id,
		"skill_id": base_draft.skill_id,
		"content_ref": base_draft.content_ref,
		"display_name": base_draft.display_name,
		"source_bundle": base_bundle,
	})
	if (
		entrypoint_count != 1
		or accepted_source.is_empty()
		or str(patch.get("result_draft_sha256", "")) != expected_result_sha
		or expected_result_sha == str(base_draft.draft_sha256)
	):
		return _failure("FORMAL_PATCH_RESULT_DRAFT_DIGEST_INVALID", "Patch result hash does not equal the exact one-operation accepted Draft bytes.")
	return {"ok": true, "value": {
		"command": command.duplicate(true),
		"interaction": interaction.duplicate(true),
		"patch": patch.duplicate(true),
		"accepted_source": accepted_source,
	}}


func _verify_visible_patch_preview(
	preview: String,
	patch: Dictionary,
	base_draft: Dictionary,
) -> Dictionary:
	var operation: Dictionary = patch.operations[0]
	var required_fragments: Array[String] = [
		"AI CODE PATCH (NOT APPLIED)",
		str(patch.rationale),
		str(patch.base_draft_revision),
		str(patch.base_draft_sha256),
		str(patch.result_draft_sha256),
		str(operation.operation),
		str(operation.path),
		str(operation.previous_content_sha256),
		str(operation.content_sha256),
		str(operation.content),
		"--- BEFORE",
		"+++ AFTER",
	]
	var entrypoint := str(base_draft.source_bundle.entrypoint)
	for file_value: Variant in base_draft.source_bundle.files:
		if file_value is Dictionary and str(file_value.get("path", "")) == entrypoint:
			required_fragments.append(str(file_value.get("content", "")))
	for reference_value: Variant in patch.evidence_refs:
		if not reference_value is Dictionary:
			return _failure("FORMAL_PATCH_PREVIEW_EVIDENCE_INVALID", "Patch preview authority contains a non-object Evidence reference.")
		required_fragments.append(str(reference_value.get("evidence_id", "")))
		required_fragments.append(str(reference_value.get("sha256", "")))
	for fragment in required_fragments:
		if fragment.is_empty() or not preview.contains(fragment):
			return _failure("FORMAL_PATCH_PREVIEW_INCOMPLETE", "Visible Patch preview omits exact proposal content: %s" % fragment.left(80))
	return {"ok": true}


func _verify_accepted_patch_decision(
	proposal_interaction: Dictionary,
	patch: Dictionary,
	receipt: Dictionary,
	decided_interaction: Dictionary,
	base_draft: Dictionary,
	accepted_draft: Dictionary,
) -> Dictionary:
	var expected_decided := proposal_interaction.duplicate(true)
	expected_decided["interaction_revision"] = 2
	expected_decided["patch_decision"] = receipt.duplicate(true)
	expected_decided["updated_at"] = receipt.get("decided_at")
	if (
		str(receipt.get("decision", "")) != "ACCEPT"
		or receipt.get("reason_code") != null
		or receipt.get("draft_updated") != true
		or str(receipt.get("session_id", "")) != str(proposal_interaction.session_id)
		or str(receipt.get("turn_id", "")) != str(proposal_interaction.turn_id)
		or str(receipt.get("interaction_id", "")) != str(proposal_interaction.interaction_id)
		or int(receipt.get("interaction_revision_before", -1)) != 1
		or int(receipt.get("interaction_revision_after", -1)) != 2
		or str(receipt.get("patch_id", "")) != str(patch.patch_id)
		or str(receipt.get("patch_sha256", "")) != str(patch.patch_sha256)
		or str(receipt.get("draft_id", "")) != str(base_draft.draft_id)
		or str(receipt.get("skill_id", "")) != str(base_draft.skill_id)
		or int(receipt.get("draft_revision_before", -1)) != int(base_draft.revision)
		or str(receipt.get("draft_sha256_before", "")) != str(base_draft.draft_sha256)
		or int(receipt.get("draft_revision_after", -1)) != int(base_draft.revision) + 1
		or str(receipt.get("draft_sha256_after", "")) != str(patch.result_draft_sha256)
		or decided_interaction != expected_decided
	):
		return _failure("FORMAL_PATCH_DECISION_INVALID", "Explicit ACCEPT did not produce the exact revision-2 Interaction and revision-3 Draft receipt.")
	var accepted_source := str(patch.operations[0].content)
	var saved_guard := _verify_saved_draft(base_draft, accepted_draft, accepted_source)
	if not saved_guard.ok:
		return saved_guard
	if (
		str(accepted_draft.get("last_applied_patch_id", "")) != str(patch.patch_id)
		or str(accepted_draft.get("draft_sha256", "")) != str(patch.result_draft_sha256)
	):
		return _failure("FORMAL_PATCH_ACCEPTED_DRAFT_INVALID", "Accepted Draft does not name the exact applied Patch/result hash.")
	return {"ok": true}


func _verify_captured_certified_build(
	build_value: Variant,
	store: WalnutClientStore,
	absolute_deadline: int,
	phase: String,
) -> Dictionary:
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("%s_BUILD_DEADLINE_EXCEEDED" % phase, "%s Build completed after the total E2E deadline." % phase)
	var build: Variant = build_value
	if not build is Dictionary or build.is_empty():
		return _failure("%s_BUILD_RESOURCE_MISSING" % phase, "%s Build did not expose its canonical SkillBuild." % phase)
	var validation := ContractValidator.validate_skill_build(build)
	if not validation.ok or str(build.get("status", "")) != "CERTIFIED" or not bool(build.get("terminal", false)):
		return _failure("%s_BUILD_RESOURCE_INVALID" % phase, "%s SkillBuild is not a terminal certified resource." % phase)
	return {"ok": true, "value": build.duplicate(true)}


func _refresh_activated_authority(
	controller: Node,
	game_gateway: RefCounted,
	store: WalnutClientStore,
	bootstrap: Dictionary,
	absolute_deadline: int,
	phase: String,
) -> Dictionary:
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("%s_ACTIVATION_DEADLINE_EXCEEDED" % phase, "%s Activation completed after the total E2E deadline." % phase)
	if store.active_skill_tuple.is_empty():
		return _failure("%s_ACTIVATION_NOT_ACTIVE" % phase, "%s SkillActivation did not become ACTIVE." % phase)
	var refreshed: Dictionary = await _refresh_public_authority(
		game_gateway, bootstrap, store, absolute_deadline,
	)
	if not refreshed.ok:
		return refreshed
	var current_bootstrap: Dictionary = refreshed.value
	var guard := _verify_active_authority(store, current_bootstrap)
	if not guard.ok:
		return guard
	controller.configure_authority(current_bootstrap, store.authoritative_session)
	return {
		"ok": true,
		"bootstrap": current_bootstrap,
		"active": store.active_skill_tuple.duplicate(true),
	}


func _execute_failed_objective_turn(
	controller: Node,
	game_gateway: RefCounted,
	product_gateway: RefCounted,
	store: WalnutClientStore,
	bootstrap: Dictionary,
	active: Dictionary,
	expected_role: String,
	absolute_deadline: int,
	interaction_deadline_seconds: float,
	task_workspace: Node,
) -> Dictionary:
	var pre_world: Dictionary = store.world_snapshot.duplicate(true)
	var pre_interaction_cursor := store.last_interaction_sequence
	var captured := {"value": {}}
	controller.run_resolved.connect(func(value: Dictionary) -> void:
		captured.value = value.duplicate(true)
	, Object.CONNECT_ONE_SHOT)
	var submission: Dictionary = await _press_task_workspace_action(
		task_workspace, "SUBMIT", absolute_deadline,
	)
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("FAILURE_RUN_DEADLINE_EXCEEDED", "Objective-failure Run closure exceeded the total deadline.")
	if not submission.get("ok", false) or store.flow_state != WalnutClientStore.FlowState.COMPLETED:
		return _failure("FAILURE_RUN_CLOSURE_FAILED", "Objective-failure Turn/Run did not reach a verified terminal closure.")
	var run: Variant = captured.value
	if not run is Dictionary or run.is_empty():
		return _failure("FAILURE_RUN_RESOURCE_MISSING", "Objective-failure flow exposed no canonical Run.")
	var expected_active := store.active_skill_tuple if active.is_empty() else active
	var run_guard := _verify_failed_run(run, expected_active, pre_world)
	if not run_guard.ok:
		return run_guard
	var command_guard: Dictionary = await _verify_failed_command_and_run(
		game_gateway, bootstrap, run, absolute_deadline,
	)
	if not command_guard.ok:
		return command_guard
	var evidence_guard: Dictionary = await _verify_failed_evidence(
		game_gateway, bootstrap, run, absolute_deadline,
	)
	if not evidence_guard.ok:
		return evidence_guard
	var world_guard: Dictionary = await _verify_world_unchanged(
		game_gateway, bootstrap, pre_world, absolute_deadline,
	)
	if not world_guard.ok:
		return world_guard
	var interaction_deadline := mini(
		absolute_deadline,
		Time.get_ticks_msec() + ceili(interaction_deadline_seconds * 1000.0),
	)
	var interaction_guard: Dictionary = await _verify_interaction(
		product_gateway,
		bootstrap,
		str(run.session_id),
		str(run.turn_id),
		str(run.command_id),
		str(run.run_id),
		run.agent_feedback,
		pre_interaction_cursor,
		interaction_deadline,
	)
	if not interaction_guard.ok:
		return interaction_guard
	var interaction: Dictionary = interaction_guard.value
	if str(interaction.get("role", "")) != expected_role:
		return _failure("FAILURE_ROLE_SEQUENCE_MISMATCH", "Objective-failure Interaction did not follow teaching/teaching/bug authority.")
	if int(interaction.get("sequence", -1)) != pre_interaction_cursor + 1:
		return _failure("FAILURE_INTERACTION_SEQUENCE_GAP", "Objective-failure Interaction sequence is not contiguous.")
	return {
		"ok": true,
		"run": run.duplicate(true),
		"command": command_guard.value.command,
		"interaction": interaction,
		"evidence": evidence_guard.value,
		"failure_reason": run.world_application.failure.details.reason,
	}


func _synchronize_workspace_session(
	product_gateway: RefCounted,
	store: WalnutClientStore,
	controller: Node,
	bootstrap: Dictionary,
) -> Dictionary:
	var previous_session: Dictionary = store.authoritative_session.duplicate(true)
	var result: Dictionary = await product_gateway.get_workspace(
		_new_context(bootstrap), str(store.authoritative_session.session_id),
	)
	if not result.get("ok", false):
		return _gateway_failure("WORKSPACE_SESSION_REFRESH_FAILED", result)
	var workspace: Dictionary = result.value
	var session: Variant = workspace.get("session")
	if not session is Dictionary:
		return _failure("WORKSPACE_SESSION_REFRESH_INVALID", "Canonical Workspace has no AgentSession authority.")
	var session_guard := ContractValidator.validate_agent_session(session)
	if not session_guard.ok or str(session.get("session_id", "")) != str(store.authoritative_session.session_id):
		return _failure("WORKSPACE_SESSION_REFRESH_INVALID", "Canonical Workspace returned a different or invalid AgentSession.")
	var advance_guard := _verify_session_advanced(previous_session, session, 1)
	if not advance_guard.ok:
		return advance_guard
	if int(workspace.get("last_interaction_sequence", -1)) != store.last_interaction_sequence:
		return _failure("WORKSPACE_INTERACTION_CURSOR_MISMATCH", "Canonical Workspace does not publish the exact verified Interaction cursor.")
	store.set_workspace(workspace)
	store.set_authoritative_session(session)
	controller.configure_authority(bootstrap, session)
	return {"ok": true, "value": workspace}


func _verify_saved_draft(starter: Dictionary, saved: Dictionary, mutated_source: String) -> Dictionary:
	if (
		str(saved.get("session_id", "")) != str(starter.get("session_id", ""))
		or str(saved.get("draft_id", "")) != str(starter.get("draft_id", ""))
		or str(saved.get("skill_id", "")) != str(starter.get("skill_id", ""))
		or saved.get("content_ref") != starter.get("content_ref")
		or str(saved.get("display_name", "")) != str(starter.get("display_name", ""))
		or int(saved.get("revision", -1)) != int(starter.get("revision", -1)) + 1
	):
		return _failure("DRAFT_CAS_IDENTITY_MISMATCH", "Saved Draft did not advance the exact starter Draft identity by one revision.")
	var starter_sha := str(starter.get("draft_sha256", ""))
	var saved_sha := str(saved.get("draft_sha256", ""))
	if not _valid_sha256(saved_sha) or saved_sha == starter_sha:
		return _failure("DRAFT_CAS_DIGEST_NOT_ADVANCED", "Saved Draft hash is invalid or did not advance from the starter CAS base.")
	var expected_bundle_value: Variant = starter.get("source_bundle")
	var saved_bundle_value: Variant = saved.get("source_bundle")
	if not expected_bundle_value is Dictionary or not saved_bundle_value is Dictionary:
		return _failure("DRAFT_CAS_SOURCE_BUNDLE_INVALID", "Starter or saved Draft has no canonical source bundle.")
	var expected_bundle: Dictionary = expected_bundle_value.duplicate(true)
	var entrypoint := str(expected_bundle.get("entrypoint", ""))
	var entrypoint_count := 0
	for index in range(expected_bundle.get("files", []).size()):
		var file: Variant = expected_bundle.files[index]
		if file is Dictionary and str(file.get("path", "")) == entrypoint:
			entrypoint_count += 1
			file["content"] = mutated_source
			file["content_sha256"] = mutated_source.sha256_text()
			expected_bundle.files[index] = file
	if entrypoint_count != 1 or saved_bundle_value != expected_bundle:
		return _failure("DRAFT_CAS_SOURCE_MISMATCH", "Saved Draft does not contain the deterministic entrypoint mutation and exact source hash.")
	for file_value in saved_bundle_value.files:
		if (
			not file_value is Dictionary
			or str(file_value.get("content", "")).sha256_text() != str(file_value.get("content_sha256", ""))
		):
			return _failure("DRAFT_CAS_FILE_DIGEST_MISMATCH", "Saved Draft contains a source file whose content hash is not exact.")
	return {"ok": true}


func _verify_saved_draft_workspace(
	starter: Dictionary,
	saved: Dictionary,
	draft: Dictionary,
) -> Dictionary:
	if int(saved.get("workspace_revision", -1)) != int(starter.get("workspace_revision", -1)) + 1:
		return _failure("DRAFT_WORKSPACE_REVISION_MISMATCH", "Draft CAS did not advance Workspace by one revision.")
	if saved.get("session") != starter.get("session"):
		return _failure("DRAFT_WORKSPACE_SESSION_DRIFT", "Draft CAS changed the canonical Workspace Session.")
	var refs: Variant = saved.get("skill_draft_refs")
	var expected_ref := {
		"draft_id": str(draft.draft_id),
		"skill_id": str(draft.skill_id),
		"revision": int(draft.revision),
		"draft_sha256": str(draft.draft_sha256),
		"url": "/product-experience/v1/sessions/%s/skill-drafts/%s" % [
			str(draft.session_id),
			str(draft.draft_id),
		],
	}
	if not refs is Array or refs.size() != 1 or refs[0] != expected_ref:
		return _failure("DRAFT_WORKSPACE_REF_MISMATCH", "Workspace does not expose exactly the newly saved canonical Draft reference.")
	return {"ok": true}


func _verify_final_workspace(
	final_workspace: Dictionary,
	saved_workspace: Dictionary,
	saved_draft: Dictionary,
	snapshot: Dictionary,
	interaction_sequence: int,
) -> Dictionary:
	var session_guard := _verify_session_advanced(
		saved_workspace.get("session", {}), final_workspace.get("session", {}), 1,
	)
	if not session_guard.ok:
		return session_guard
	if (
		str(final_workspace.get("workspace_id", "")) != str(saved_workspace.get("workspace_id", ""))
		or int(final_workspace.get("workspace_revision", -1)) < int(saved_workspace.get("workspace_revision", -1))
		or final_workspace.get("content_ref") != saved_workspace.get("content_ref")
	):
		return _failure("FINAL_WORKSPACE_IDENTITY_DRIFT", "Final Workspace changed immutable identity or regressed its revision.")
	var expected_ref := {
		"draft_id": saved_draft.get("draft_id"),
		"skill_id": saved_draft.get("skill_id"),
		"revision": saved_draft.get("revision"),
		"draft_sha256": saved_draft.get("draft_sha256"),
		"url": "/product-experience/v1/sessions/%s/skill-drafts/%s" % [
			str(saved_draft.get("session_id", "")), str(saved_draft.get("draft_id", "")),
		],
	}
	var checkpoint: Variant = final_workspace.get("world_checkpoint")
	if (
		final_workspace.get("skill_draft_refs") != [expected_ref]
		or not checkpoint is Dictionary
		or str(checkpoint.get("world_id", "")) != str(snapshot.get("world_id", ""))
		or int(checkpoint.get("world_revision", -1)) != int(snapshot.get("revision", -1))
		or int(checkpoint.get("last_event_sequence", -1)) != int(snapshot.get("last_event_sequence", -1))
		or str(checkpoint.get("state_hash", "")) != str(snapshot.get("state_hash", ""))
		or int(final_workspace.get("last_interaction_sequence", -1)) != interaction_sequence
	):
		return _failure("FINAL_WORKSPACE_AUTHORITY_DRIFT", "Final Workspace does not close over the exact Draft, World checkpoint, and Interaction cursor.")
	return {"ok": true}


func _verify_session_advanced(before: Variant, after: Variant, delta: int) -> Dictionary:
	if not before is Dictionary or not after is Dictionary:
		return _failure("SESSION_ADVANCE_INVALID", "Workspace Session authority is missing before or after a Turn.")
	var validation := ContractValidator.validate_agent_session(after)
	if not validation.ok:
		return _failure("SESSION_ADVANCE_INVALID", str(validation.error.get("message", "Advanced AgentSession is invalid.")))
	if int(after.get("last_turn_sequence", -1)) != int(before.get("last_turn_sequence", -1)) + delta:
		return _failure("SESSION_TURN_SEQUENCE_MISMATCH", "Canonical AgentSession did not advance by exactly one Turn.")
	var before_identity: Dictionary = before.duplicate(true)
	var after_identity: Dictionary = after.duplicate(true)
	for mutable_field in ["updated_at", "last_turn_sequence"]:
		before_identity.erase(mutable_field)
		after_identity.erase(mutable_field)
	if before_identity != after_identity:
		return _failure("SESSION_IDENTITY_DRIFT", "AgentSession changed fields other than updated_at and last_turn_sequence.")
	return {"ok": true}


func _verify_formal_ui_projection(
	app: Node,
	store: WalnutClientStore,
	interaction: Dictionary,
	snapshot: Dictionary,
) -> Dictionary:
	var task_workspace := app.get_node_or_null("TaskWorkspace") as Control
	if (
		task_workspace == null
		or task_workspace.get_script() == null
		or str(task_workspace.get_script().resource_path) != "res://scenes/task/task_workspace.gd"
	):
		return _failure("FORMAL_TASK_WORKSPACE_MISSING", "AppRoot does not display the formal TaskWorkspace scene.")
	var editor := task_workspace.get_node_or_null("DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeEditorPanel/Content/CodeEditor") as CodeEdit
	var task_title := task_workspace.get_node_or_null("Hud/SafeArea/EdgeLayer/TaskTag/TaskText/TaskTitle") as Label
	var content_task: Variant = store.content.get("task")
	if (
		editor == null
		or str(editor.text) != store.local_source
		or task_title == null
		or not content_task is Dictionary
		or str(task_title.text) != str(content_task.get("name", ""))
	):
		return _failure("TASK_WORKSPACE_PROJECTION_MISMATCH", "TaskWorkspace does not display the canonical Content/Draft projection.")

	var dialogue := task_workspace.get_node_or_null("DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/DialoguePanel") as PanelContainer
	if (
		dialogue == null
		or dialogue.get_script() == null
		or str(dialogue.get_script().resource_path) != "res://scenes/task/dialogue_panel.gd"
	):
		return _failure("FORMAL_DIALOGUE_PANEL_MISSING", "TaskWorkspace does not contain the formal DialoguePanel scene.")
	var speaker_label := dialogue.get_node_or_null("Margin/Content/Speaker") as Label
	var dialogue_text := dialogue.get_node_or_null("Margin/Content/DialogueText") as Label
	var question_label := dialogue.get_node_or_null("Margin/Content/Question") as Label
	var response_type_label := dialogue.get_node_or_null("Margin/Content/ResponseType") as Label
	var feedback: Variant = interaction.get("feedback")
	var role_names := {
		"world_agent": "芽芽 / 叮当",
		"xiaohutao": "小核桃",
		"teaching_agent": "教学角色",
		"bug_agent": "Bug 角色",
		"book_agent": "书书",
		"system": "系统",
	}
	var expected_speaker := str(role_names.get(str(interaction.get("role", "")), "系统"))
	var question_value: Variant = interaction.get("question")
	var expected_question := "" if question_value == null else str(question_value)
	var hint_value: Variant = interaction.get("hint_level")
	var hint_level := int(hint_value) if typeof(hint_value) == TYPE_INT else 0
	var expected_response_type := str(dialogue.call(
		"_response_type_label", str(interaction.get("response_type", "message")), hint_level,
	))
	if (
		not feedback is Dictionary
		or speaker_label == null
		or str(speaker_label.text) != expected_speaker
		or dialogue_text == null
		or str(dialogue_text.text) != str(feedback.get("message", ""))
		or question_label == null
		or question_label.visible != (not expected_question.is_empty())
		or (not expected_question.is_empty() and not str(question_label.text).ends_with(expected_question))
		or response_type_label == null
		or str(response_type_label.text) != expected_response_type
	):
		return _failure("DIALOGUE_PROJECTION_MISMATCH", "DialoguePanel does not display the matching canonical AgentInteraction fields.")

	var world_viewport := task_workspace.get_node_or_null("WorldViewport") as PanelContainer
	if (
		world_viewport == null
		or world_viewport.get_script() == null
		or str(world_viewport.get_script().resource_path) != "res://scenes/task/world_viewport.gd"
	):
		return _failure("FORMAL_WORLD_VIEWPORT_MISSING", "TaskWorkspace does not contain the formal WorldViewport scene.")
	var farm_world := world_viewport.get_node_or_null("ViewportShell/SubViewportContainer/SubViewport/FarmWorld")
	var terrain: TerrainManager = null
	var avatar: Node3D = null
	if farm_world != null:
		terrain = farm_world.get_node_or_null("TerrainManager") as TerrainManager
		avatar = farm_world.get_node_or_null("Player") as Node3D
	var state: Variant = snapshot.get("state")
	if terrain == null or avatar == null or not state is Dictionary:
		return _failure("WORLD_VIEWPORT_PROJECTION_UNAVAILABLE", "WorldViewport has no preauthored terrain/avatar projection target.")
	var avatar_value: Variant = state.get("avatar")
	var avatar_position: Variant = avatar_value.get("position") if avatar_value is Dictionary else null
	if not avatar_position is Dictionary:
		return _failure("WORLD_VIEWPORT_AVATAR_INVALID", "Canonical Snapshot has no displayable avatar position.")
	var avatar_cell := Vector2i(int(avatar_position.get("x", -1)), int(avatar_position.get("y", -1)))
	if not terrain.map_data.is_inside_map(avatar_cell) or terrain.map_data.world_to_cell(avatar.global_position) != avatar_cell:
		return _failure("WORLD_VIEWPORT_AVATAR_MISMATCH", "WorldViewport avatar does not equal the canonical Snapshot position.")
	for plot_value in state.get("plots", []):
		if not plot_value is Dictionary or not plot_value.get("position") is Dictionary:
			return _failure("WORLD_VIEWPORT_PLOT_INVALID", "Canonical Snapshot contains an invalid plot projection.")
		var cell := Vector2i(int(plot_value.position.x), int(plot_value.position.y))
		var expected_cell_type := TerrainMapData.CellType.GRASS
		if str(plot_value.get("soil_state", "")) == "TILLED":
			expected_cell_type = TerrainMapData.CellType.FARMLAND if int(plot_value.get("hydration", 0)) > 0 else TerrainMapData.CellType.DIRT
		if not terrain.map_data.is_inside_map(cell) or terrain.map_data.get_cell(cell) != expected_cell_type:
			return _failure("WORLD_VIEWPORT_TERRAIN_MISMATCH", "WorldViewport terrain does not equal a canonical Snapshot plot.")
	return {
		"ok": true,
		"value": {
			"task_workspace": true,
			"dialogue_panel": true,
			"world_viewport": true,
		},
	}


func _valid_sha256(value: String) -> bool:
	if value.length() != 64:
		return false
	for index in range(value.length()):
		if "0123456789abcdef".find(value.substr(index, 1)) < 0:
			return false
	return true


func _active_tuple_sha256(value: Dictionary) -> String:
	var ordered: Array = []
	for field in ACTIVE_TUPLE_FIELDS:
		ordered.append(value.get(field))
	return JSON.stringify(ordered).sha256_text()


func _read_settings() -> Dictionary:
	var total := _positive_environment_seconds(
		"YAYA_REAL_GATEWAY_E2E_TOTAL_DEADLINE_SECONDS", DEFAULT_TOTAL_DEADLINE_SECONDS,
	)
	var resource := _positive_environment_seconds(
		"YAYA_REAL_GATEWAY_E2E_RESOURCE_DEADLINE_SECONDS", DEFAULT_RESOURCE_DEADLINE_SECONDS,
	)
	var interaction := _positive_environment_seconds(
		"YAYA_REAL_GATEWAY_E2E_INTERACTION_DEADLINE_SECONDS", DEFAULT_INTERACTION_DEADLINE_SECONDS,
	)
	if total <= 0.0 or resource <= 0.0 or interaction <= 0.0:
		return _failure("E2E_SETTINGS_INVALID", "All real Gateway E2E deadlines must be positive numbers.")
	if resource >= total or interaction >= total:
		return _failure("E2E_SETTINGS_INVALID", "Resource and Interaction deadlines must be smaller than the total deadline.")
	return {
		"ok": true,
		"total_deadline_seconds": total,
		"resource_deadline_seconds": resource,
		"interaction_deadline_seconds": interaction,
	}


func _positive_environment_seconds(name: String, fallback: float) -> float:
	var raw := OS.get_environment(name).strip_edges()
	if raw.is_empty():
		return fallback
	if not raw.is_valid_float():
		return -1.0
	return float(raw)


func _production_clients_are_wired(app: Node) -> bool:
	var expected := {
		"_transport": "res://scripts/client/audited_http_agent_api_transport.gd",
		"_game_gateway": "res://addons/yaya_contract_client/agent_api_gateway.gd",
		"_product_gateway": "res://scripts/client/product_interaction_gateway.gd",
	}
	for property in expected:
		var client: Variant = app.get(property)
		if not client is Object or client.get_script() == null:
			return false
		if str(client.get_script().resource_path) != str(expected[property]):
			return false
	return true


func _require_all_capabilities(capabilities: Dictionary) -> Dictionary:
	var missing: Array[String] = []
	for capability in REQUIRED_CAPABILITIES:
		if capabilities.get(capability) != true:
			missing.append(capability)
	if not missing.is_empty():
		return _failure(
			"CAPABILITY_UNAVAILABLE",
			"Real Gateway E2E requires every public capability; unavailable: %s" % ", ".join(missing),
		)
	return {"ok": true}


func _verify_startup_authority(store: WalnutClientStore, bootstrap: Dictionary) -> Dictionary:
	var session: Dictionary = store.authoritative_session
	var session_guard := ContractValidator.validate_agent_session(session)
	if not session_guard.ok:
		return _failure("SESSION_INVALID", "AppRoot did not restore one valid canonical AgentSession.")
	var workspace: Dictionary = store.workspace
	var draft: Dictionary = store.draft
	var snapshot: Dictionary = store.world_snapshot
	if workspace.is_empty() or draft.is_empty() or snapshot.is_empty():
		return _failure("WORKSPACE_INCOMPLETE", "Workspace, Draft and Snapshot must all be recovered before the chain continues.")
	if str(workspace.get("session", {}).get("session_id", "")) != str(session.session_id):
		return _failure("WORKSPACE_SESSION_MISMATCH", "Workspace does not embed the exact canonical AgentSession.")
	if str(draft.get("session_id", "")) != str(session.session_id):
		return _failure("DRAFT_SESSION_MISMATCH", "Canonical Draft belongs to another AgentSession.")
	if (
		str(snapshot.get("world_id", "")) != str(bootstrap.world.world_id)
		or int(snapshot.get("revision", -1)) != int(bootstrap.world.revision)
		or int(snapshot.get("last_event_sequence", -1)) != int(bootstrap.world.last_event_sequence)
		or str(snapshot.get("state_hash", "")) != str(bootstrap.world.state_hash)
	):
		return _failure("STARTUP_WORLD_MISMATCH", "Recovered Snapshot does not equal StudentBootstrap world authority.")
	return {"ok": true}


func _verify_active_authority(store: WalnutClientStore, bootstrap: Dictionary) -> Dictionary:
	var active: Dictionary = store.active_skill_tuple
	if active.size() != ACTIVE_TUPLE_FIELDS.size():
		return _failure("ACTIVE_TUPLE_MISSING", "No exact seven-field active Skill tuple is available.")
	for field in ACTIVE_TUPLE_FIELDS:
		if not active.has(field):
			return _failure("ACTIVE_TUPLE_INVALID", "The active Skill tuple is missing %s." % field)
	var activation: Variant = bootstrap.get("activation")
	if not activation is Dictionary or activation.get("active") != active:
		return _failure("ACTIVE_AUTHORITY_MISMATCH", "ClientStore active tuple does not equal public activation authority.")
	if int(active.registry_revision) != int(activation.registry_revision):
		return _failure("ACTIVE_REGISTRY_MISMATCH", "Active tuple registry revision is stale.")
	return {"ok": true}


func _refresh_public_authority(
	game_gateway: RefCounted,
	initial_bootstrap: Dictionary,
	store: WalnutClientStore,
	absolute_deadline: int,
) -> Dictionary:
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("BOOTSTRAP_REFRESH_DEADLINE_EXCEEDED", "Public authority refresh exceeded the total deadline.")
	var result: Dictionary = await game_gateway.get_student_bootstrap(
		RequestContexts.new_wire_attempt(),
	)
	if not result.get("ok", false):
		return _gateway_failure("BOOTSTRAP_REFRESH_FAILED", result)
	var refreshed: Dictionary = result.value
	var validation := ContractValidator.validate_student_bootstrap_v2(refreshed)
	if not validation.ok:
		return _failure("BOOTSTRAP_REFRESH_INVALID", str(validation.error.get("message", "Refreshed StudentBootstrapV2 is invalid.")))
	if (
		refreshed.actor != initial_bootstrap.actor
		or refreshed.content != initial_bootstrap.content
		or str(refreshed.world.world_id) != str(initial_bootstrap.world.world_id)
		or refreshed.session.create_request != initial_bootstrap.session.create_request
	):
		return _failure("BOOTSTRAP_REFRESH_IDENTITY_DRIFT", "Refreshed StudentBootstrap changed immutable actor/content/world/session authority.")
	var capability_guard := _require_all_capabilities(refreshed.capabilities)
	if not capability_guard.ok:
		return capability_guard
	if str(refreshed.session.current_session_id) != str(store.authoritative_session.session_id):
		return _failure("BOOTSTRAP_SESSION_NOT_PUBLISHED", "Refreshed StudentBootstrap does not publish the exact canonical Session for the next process.")
	var snapshot: Dictionary = store.world_snapshot
	if (
		int(refreshed.world.revision) != int(snapshot.revision)
		or int(refreshed.world.last_event_sequence) != int(snapshot.last_event_sequence)
		or str(refreshed.world.state_hash) != str(snapshot.state_hash)
	):
		return _failure("BOOTSTRAP_WORLD_NOT_CURRENT", "Refreshed StudentBootstrap world authority does not equal the recovered Snapshot.")
	return {"ok": true, "value": refreshed.duplicate(true)}


func _verify_failed_run(run: Dictionary, active: Dictionary, _pre_world: Dictionary) -> Dictionary:
	var validation := ContractValidator.validate_run(run)
	if not validation.ok:
		return _failure("FAILURE_RUN_INVALID", str(validation.error.get("message", "Rejected Run validation failed.")))
	if str(run.get("status", "")) != "REJECTED" or not bool(run.get("terminal", false)):
		return _failure("FAILURE_RUN_NOT_REJECTED", "Objective-failure Run must be terminal REJECTED.")
	var expected_skill := {
		"skill_id": active.skill_id,
		"skill_version_id": active.skill_version_id,
		"artifact_sha256": active.artifact_sha256,
		"certification_id": active.certification_id,
	}
	if run.get("skill") != expected_skill:
		return _failure("FAILURE_RUN_SKILL_MISMATCH", "Rejected Run does not use the exact failure-version Skill binding.")
	var sandbox: Variant = run.get("sandbox")
	if (
		not sandbox is Dictionary
		or str(sandbox.get("status", "")) != "SUCCEEDED"
		or not sandbox.get("action_intents") is Array
		or sandbox.action_intents.size() != 7
		or sandbox.get("failure") != null
	):
		return _failure("FAILURE_SANDBOX_RESULT_INVALID", "Failure-version Sandbox must succeed with exactly seven staged actions.")
	var world_application: Variant = run.get("world_application")
	if (
		not world_application is Dictionary
		or str(world_application.get("status", "")) != "REJECTED"
		or world_application.get("receipt") != null
		or not world_application.get("failure") is Dictionary
		or str(world_application.failure.get("code", "")) != "WORLD_RULE_REJECTED"
		or str(world_application.failure.get("stage", "")) != "WORLD_VALIDATE"
		or str(world_application.failure.get("details", {}).get("reason", "")) != "TASK_INCOMPLETE"
	):
		return _failure("FAILURE_WORLD_RESULT_INVALID", "Failure-version Run must expose canonical TASK_INCOMPLETE World rejection without a receipt.")
	var feedback: Variant = run.get("agent_feedback")
	if (
		not feedback is Dictionary
		or str(feedback.get("source", "")) != "provider"
		or bool(feedback.get("degraded", true))
		or feedback.get("fallback_reason") != null
	):
		return _failure("FAILURE_FEEDBACK_DEGRADED", "Objective-failure feedback must come from the real Provider without degradation or fallback.")
	if run.get("evidence_refs") != feedback.get("evidence_refs") or run.evidence_refs.size() != 1:
		return _failure("FAILURE_EVIDENCE_REFS_INVALID", "Rejected Run and Provider feedback must share one exact SKILL_RUN Evidence reference.")
	return {"ok": true}


func _verify_failed_command_and_run(
	game_gateway: RefCounted,
	bootstrap: Dictionary,
	run: Dictionary,
	absolute_deadline: int,
) -> Dictionary:
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("FAILURE_COMMAND_DEADLINE_EXCEEDED", "Rejected Command verification exceeded the total deadline.")
	var command_result: Dictionary = await game_gateway.get_command(
		_new_context(bootstrap), str(run.command_id),
	)
	if not command_result.get("ok", false):
		return _gateway_failure("FAILURE_COMMAND_QUERY_FAILED", command_result)
	var command: Dictionary = command_result.value
	if (
		str(command.get("status", "")) != "REJECTED"
		or not bool(command.get("terminal", false))
		or command.get("result") != null
		or not command.get("error") is Dictionary
		or str(command.error.get("code", "")) != "WORLD_RULE_REJECTED"
		or str(command.error.get("stage", "")) != "WORLD_VALIDATE"
		or str(command.error.get("details", {}).get("reason_code", "")) != "TASK_INCOMPLETE"
		or command.get("evidence_refs") != run.get("evidence_refs")
		or str(command.get("links", {}).get("run", "")) != "/v1/runs/%s" % str(run.run_id)
	):
		return _failure("FAILURE_COMMAND_INVALID", "Canonical Command did not close as the exact TASK_INCOMPLETE rejection derived from its Run.")
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("FAILURE_RUN_QUERY_DEADLINE_EXCEEDED", "Rejected Run verification exceeded the total deadline.")
	var run_result: Dictionary = await game_gateway.get_run(
		_new_context(bootstrap), str(run.run_id),
	)
	if not run_result.get("ok", false):
		return _gateway_failure("FAILURE_RUN_QUERY_FAILED", run_result)
	if run_result.value != run:
		return _failure("FAILURE_RUN_RESOURCE_DRIFT", "A second canonical GET returned a different rejected Run.")
	return {"ok": true, "value": {"command": command, "run": run_result.value}}


func _verify_failed_evidence(
	game_gateway: RefCounted,
	bootstrap: Dictionary,
	run: Dictionary,
	absolute_deadline: int,
) -> Dictionary:
	var references: Variant = run.get("evidence_refs")
	if not references is Array or references.size() != 1:
		return _failure("FAILURE_EVIDENCE_MISSING", "Rejected Run must expose one SKILL_RUN Evidence reference.")
	var recovered: Array[Dictionary] = []
	for reference in references:
		if Time.get_ticks_msec() >= absolute_deadline:
			return _failure("FAILURE_EVIDENCE_DEADLINE_EXCEEDED", "Rejected Evidence verification exceeded the total deadline.")
		var result: Dictionary = await game_gateway.get_evidence(
			_new_context(bootstrap), str(reference.get("evidence_id", "")),
		)
		if not result.get("ok", false):
			return _gateway_failure("FAILURE_EVIDENCE_QUERY_FAILED", result)
		var evidence: Dictionary = result.value
		var validation := ContractValidator.validate_evidence(evidence)
		if not validation.ok:
			return _failure("FAILURE_EVIDENCE_INVALID", str(validation.error.get("message", "Rejected Evidence validation failed.")))
		var payload: Variant = evidence.get("payload")
		if (
			evidence.get("evidence_ref") != reference
			or str(evidence.get("source", {}).get("source_type", "")) != "SKILL_RUN"
			or str(evidence.get("source", {}).get("source_id", "")) != str(run.run_id)
			or str(evidence.get("source", {}).get("command_id", "")) != str(run.command_id)
			or not payload is Dictionary
			or str(payload.get("evidence_kind", "")) != "SKILL_RUN"
			or str(payload.get("run_id", "")) != str(run.run_id)
			or str(payload.get("sandbox_status", "")) != "SUCCEEDED"
			or str(payload.get("world_status", "")) != "REJECTED"
			or int(payload.get("intent_count", -1)) != 7
		):
			return _failure("FAILURE_EVIDENCE_MISMATCH", "Rejected SKILL_RUN Evidence does not exactly describe the seven-intent World rejection.")
		recovered.append(evidence.duplicate(true))
	return {"ok": true, "value": recovered}


func _verify_world_unchanged(
	game_gateway: RefCounted,
	bootstrap: Dictionary,
	pre_world: Dictionary,
	absolute_deadline: int,
) -> Dictionary:
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("FAILURE_WORLD_EVENTS_DEADLINE_EXCEEDED", "Zero-commit Events verification exceeded the total deadline.")
	var events_result: Dictionary = await game_gateway.get_world_events(
		_new_context(bootstrap), str(pre_world.world_id), int(pre_world.last_event_sequence), 1,
	)
	if not events_result.get("ok", false):
		return _gateway_failure("FAILURE_WORLD_EVENTS_QUERY_FAILED", events_result)
	var page: Dictionary = events_result.value
	if (
		str(page.get("world_id", "")) != str(pre_world.world_id)
		or int(page.get("snapshot_revision", -1)) != int(pre_world.revision)
		or not page.get("events") is Array
		or not page.events.is_empty()
		or int(page.get("next_after_sequence", -1)) != int(pre_world.last_event_sequence)
		or bool(page.get("has_more", true))
	):
		return _failure("FAILURE_WORLD_EVENTS_ADVANCED", "Rejected Run exposed a World Event or advanced canonical World cursors.")
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("FAILURE_WORLD_SNAPSHOT_DEADLINE_EXCEEDED", "Zero-commit Snapshot verification exceeded the total deadline.")
	var snapshot_result: Dictionary = await game_gateway.get_world_snapshot(
		_new_context(bootstrap), str(pre_world.world_id),
	)
	if not snapshot_result.get("ok", false):
		return _gateway_failure("FAILURE_WORLD_SNAPSHOT_QUERY_FAILED", snapshot_result)
	if snapshot_result.value != pre_world:
		return _failure("FAILURE_WORLD_SNAPSHOT_ADVANCED", "Rejected Run changed the canonical World Snapshot.")
	return {"ok": true, "value": pre_world.duplicate(true)}


func _verify_run(run: Dictionary, active: Dictionary, pre_world: Dictionary) -> Dictionary:
	var validation := ContractValidator.validate_run(run)
	if not validation.ok:
		return _failure("RUN_INVALID", str(validation.error.get("message", "Run validation failed.")))
	if str(run.status) != "SUCCEEDED" or not bool(run.terminal):
		return _failure("RUN_NOT_SUCCEEDED", "The real Run must be terminal SUCCEEDED.")
	var expected_skill := {
		"skill_id": active.skill_id,
		"skill_version_id": active.skill_version_id,
		"artifact_sha256": active.artifact_sha256,
		"certification_id": active.certification_id,
	}
	if run.skill != expected_skill:
		return _failure("RUN_SKILL_MISMATCH", "Run does not use the exact publicly active Skill binding.")
	if (
		str(run.agent_feedback.source) != "provider"
		or bool(run.agent_feedback.degraded)
		or run.agent_feedback.fallback_reason != null
	):
		return _failure("RUN_FEEDBACK_DEGRADED", "Real E2E rejects degraded or fallback Agent feedback.")
	var receipt: Variant = run.get("world_application", {}).get("receipt")
	if not receipt is Dictionary:
		return _failure("WORLD_RECEIPT_MISSING", "SUCCEEDED Run has no World commit receipt.")
	if (
		str(receipt.get("world_id", "")) != str(pre_world.get("world_id", ""))
		or int(receipt.get("previous_revision", -1)) != int(pre_world.get("revision", -2))
		or int(receipt.get("world_revision", -1)) != int(pre_world.get("revision", -2)) + 1
		or int(receipt.get("first_event_sequence", -1)) != int(pre_world.get("last_event_sequence", -2)) + 1
		or int(receipt.get("last_event_sequence", -1)) < int(receipt.get("first_event_sequence", 0))
	):
		return _failure("WORLD_RECEIPT_MISMATCH", "Run receipt does not advance the exact pre-Turn world cursor.")
	return {"ok": true}


func _verify_command_and_run(
	game_gateway: RefCounted,
	bootstrap: Dictionary,
	run: Dictionary,
	receipt: Dictionary,
	absolute_deadline: int,
) -> Dictionary:
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("COMMAND_DEADLINE_EXCEEDED", "Command verification exceeded the total deadline.")
	var command_result: Dictionary = await game_gateway.get_command(
		_new_context(bootstrap), str(run.command_id),
	)
	if not command_result.get("ok", false):
		return _gateway_failure("COMMAND_QUERY_FAILED", command_result)
	var command: Dictionary = command_result.value
	if str(command.status) != "APPLIED" or not bool(command.terminal):
		return _failure("COMMAND_NOT_APPLIED", "Canonical Turn Command is not terminal APPLIED.")
	var result: Variant = command.get("result")
	if not result is Dictionary or str(result.get("result_type", "")) != "WORLD_COMMIT":
		return _failure("COMMAND_WORLD_COMMIT_MISSING", "Canonical Command has no WORLD_COMMIT result.")
	for field in ["world_id", "previous_revision", "world_revision", "first_event_sequence", "last_event_sequence"]:
		if result.get(field) != receipt.get(field):
			return _failure("COMMAND_RECEIPT_MISMATCH", "Command WORLD_COMMIT disagrees with the Run receipt.")
	if str(command.get("links", {}).get("run", "")) != "/v1/runs/%s" % str(run.run_id):
		return _failure("COMMAND_RUN_LINK_MISMATCH", "Command does not link the exact canonical Run.")

	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("RUN_QUERY_DEADLINE_EXCEEDED", "Canonical Run verification exceeded the total deadline.")
	var run_result: Dictionary = await game_gateway.get_run(
		_new_context(bootstrap), str(run.run_id),
	)
	if not run_result.get("ok", false):
		return _gateway_failure("RUN_QUERY_FAILED", run_result)
	if run_result.value != run:
		return _failure("RUN_RESOURCE_DRIFT", "A second canonical GET returned a different terminal Run.")
	return {"ok": true, "value": {"command": command, "run": run_result.value}}


func _verify_evidence(
	game_gateway: RefCounted,
	bootstrap: Dictionary,
	run: Dictionary,
	receipt: Dictionary,
	absolute_deadline: int,
) -> Dictionary:
	var references: Variant = run.get("evidence_refs")
	if not references is Array or references.is_empty():
		return _failure("EVIDENCE_MISSING", "Run must expose at least one Evidence reference.")
	var recovered: Array[Dictionary] = []
	var world_commit_found := false
	for reference in references:
		if Time.get_ticks_msec() >= absolute_deadline:
			return _failure("EVIDENCE_DEADLINE_EXCEEDED", "Evidence verification exceeded the total deadline.")
		var result: Dictionary = await game_gateway.get_evidence(
			_new_context(bootstrap), str(reference.get("evidence_id", "")),
		)
		if not result.get("ok", false):
			return _gateway_failure("EVIDENCE_QUERY_FAILED", result)
		var evidence: Dictionary = result.value
		if evidence.get("evidence_ref") != reference:
			return _failure("EVIDENCE_REFERENCE_MISMATCH", "Evidence resource does not equal its Run reference.")
		var payload: Variant = evidence.get("payload")
		if payload is Dictionary and str(payload.get("evidence_kind", "")) == "WORLD_COMMIT":
			for field in ["world_id", "previous_revision", "world_revision", "first_event_sequence", "last_event_sequence", "state_hash"]:
				if payload.get(field) != receipt.get(field):
					return _failure("WORLD_EVIDENCE_MISMATCH", "WORLD_COMMIT Evidence disagrees with the Run receipt.")
			if str(evidence.get("source", {}).get("command_id", "")) != str(run.command_id):
				return _failure("WORLD_EVIDENCE_COMMAND_MISMATCH", "WORLD_COMMIT Evidence names another Command.")
			world_commit_found = true
		recovered.append(evidence.duplicate(true))
	if not world_commit_found:
		return _failure("WORLD_EVIDENCE_MISSING", "No exact WORLD_COMMIT Evidence was recovered.")
	return {"ok": true, "value": recovered}


func _verify_events(
	game_gateway: RefCounted,
	bootstrap: Dictionary,
	receipt: Dictionary,
	initial_cursor: int,
	command_id: String,
	absolute_deadline: int,
) -> Dictionary:
	var cursor := initial_cursor
	var first_sequence := -1
	var recovered: Array[Dictionary] = []
	while cursor < int(receipt.last_event_sequence):
		if Time.get_ticks_msec() >= absolute_deadline:
			return _failure("EVENTS_DEADLINE_EXCEEDED", "HTTP Events verification exceeded the total deadline.")
		var limit := mini(500, int(receipt.last_event_sequence) - cursor)
		var result: Dictionary = await game_gateway.get_world_events(
			_new_context(bootstrap), str(receipt.world_id), cursor, limit,
		)
		if not result.get("ok", false):
			return _gateway_failure("WORLD_EVENTS_QUERY_FAILED", result)
		var page: Dictionary = result.value
		if str(page.world_id) != str(receipt.world_id) or int(page.snapshot_revision) != int(receipt.world_revision):
			return _failure("WORLD_EVENTS_AUTHORITY_MISMATCH", "HTTP Events page disagrees with the receipt world/revision.")
		if page.events.is_empty():
			return _failure("WORLD_EVENTS_INCOMPLETE", "HTTP Events ended before the receipt cursor.")
		var expected := cursor + 1
		for event in page.events:
			if int(event.sequence) != expected:
				return _failure("WORLD_EVENTS_GAP", "HTTP Events contain a sequence gap.")
			if int(event.sequence) > int(receipt.last_event_sequence):
				return _failure("WORLD_EVENTS_OVERRUN", "HTTP Events crossed the receipt boundary.")
			if str(event.command_id) != command_id:
				return _failure("WORLD_EVENTS_COMMAND_MISMATCH", "Receipt Event belongs to another Command.")
			if first_sequence < 0:
				first_sequence = int(event.sequence)
			recovered.append(event.duplicate(true))
			expected += 1
		var next_cursor := int(page.next_after_sequence)
		if next_cursor != expected - 1 or next_cursor <= cursor:
			return _failure("WORLD_EVENTS_CURSOR_INVALID", "HTTP Events cursor did not advance exactly over its records.")
		cursor = next_cursor
		if not bool(page.has_more) and cursor < int(receipt.last_event_sequence):
			return _failure("WORLD_EVENTS_INCOMPLETE", "HTTP Events pagination closed before the receipt cursor.")
	if first_sequence != int(receipt.first_event_sequence) or cursor != int(receipt.last_event_sequence):
		return _failure("WORLD_EVENTS_RECEIPT_MISMATCH", "HTTP Events do not exactly cover the receipt sequence range.")
	return {"ok": true, "value": recovered}


func _verify_snapshot(
	game_gateway: RefCounted,
	bootstrap: Dictionary,
	receipt: Dictionary,
	absolute_deadline: int,
) -> Dictionary:
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("SNAPSHOT_DEADLINE_EXCEEDED", "Snapshot verification exceeded the total deadline.")
	var result: Dictionary = await game_gateway.get_world_snapshot(
		_new_context(bootstrap), str(receipt.world_id),
	)
	if not result.get("ok", false):
		return _gateway_failure("WORLD_SNAPSHOT_QUERY_FAILED", result)
	var snapshot: Dictionary = result.value
	if not _snapshot_matches_receipt(snapshot, receipt).ok:
		return _failure("WORLD_SNAPSHOT_RECEIPT_MISMATCH", "Canonical Snapshot does not equal the Run receipt.")
	return {"ok": true, "value": snapshot}


func _snapshot_matches_receipt(snapshot: Dictionary, receipt: Dictionary) -> Dictionary:
	return {"ok": (
		str(snapshot.get("world_id", "")) == str(receipt.get("world_id", ""))
		and int(snapshot.get("revision", -1)) == int(receipt.get("world_revision", -2))
		and int(snapshot.get("last_event_sequence", -1)) == int(receipt.get("last_event_sequence", -2))
		and str(snapshot.get("state_hash", "")) == str(receipt.get("state_hash", ""))
	)}


func _verify_interaction(
	product_gateway: RefCounted,
	bootstrap: Dictionary,
	session_id: String,
	turn_id: String,
	command_id: String,
	run_id: String,
	expected_feedback: Dictionary,
	after_sequence: int,
	absolute_deadline: int,
) -> Dictionary:
	var cursor := after_sequence
	while Time.get_ticks_msec() < absolute_deadline:
		var result: Dictionary = await product_gateway.list_interactions(
			_new_context(bootstrap), session_id, cursor, 50,
		)
		if not result.get("ok", false):
			return _gateway_failure("INTERACTION_QUERY_FAILED", result)
		var page: Dictionary = result.value
		for interaction in page.interactions:
			var feedback: Variant = interaction.get("feedback")
			if (
				str(interaction.get("session_id", "")) == session_id
				and str(interaction.get("turn_id", "")) == turn_id
				and feedback is Dictionary
				and str(feedback.get("command_id", "")) == command_id
				and str(feedback.get("run_id", "")) == run_id
				and feedback == expected_feedback
			):
				return {"ok": true, "value": interaction.duplicate(true)}
		var next_cursor := int(page.next_after_sequence)
		if bool(page.has_more):
			if next_cursor <= cursor:
				return _failure("INTERACTION_CURSOR_STALLED", "AgentInteraction pagination made no progress.")
			cursor = next_cursor
			continue
		if next_cursor < cursor:
			return _failure("INTERACTION_CURSOR_REGRESSED", "AgentInteraction cursor regressed.")
		cursor = next_cursor
		var remaining := float(absolute_deadline - Time.get_ticks_msec()) / 1000.0
		if INTERACTION_RETRY_SECONDS >= remaining:
			break
		await create_timer(INTERACTION_RETRY_SECONDS).timeout
	return _failure("INTERACTION_NOT_FOUND", "Matching canonical AgentInteraction did not appear before the total deadline.")


func _new_context(bootstrap: Dictionary) -> Dictionary:
	return RequestContexts.new_attempt(bootstrap.actor, bootstrap.content)


func _gateway_failure(code: String, result: Dictionary) -> Dictionary:
	var error: Variant = result.get("error")
	var message := "Gateway request failed with HTTP status %d." % int(result.get("status", 0))
	if not str(result.get("error_code", "")).is_empty():
		code = str(result.error_code)
	if not str(result.get("message", "")).is_empty():
		message = str(result.message)
	if error is Dictionary:
		if not str(error.get("code", "")).is_empty():
			code = str(error.code)
		var nested: Variant = error.get("error")
		if nested is Dictionary and not str(nested.get("message", "")).is_empty():
			message = str(nested.message)
		elif not str(error.get("message", "")).is_empty():
			message = str(error.message)
	return _failure(code, message)


func _failure(code: String, message: String) -> Dictionary:
	return {"ok": false, "code": code, "message": message}


func _abort_result(default_code: String, result: Dictionary) -> void:
	var failure := _gateway_failure(default_code, result)
	_abort(str(failure.code), str(failure.message))


func _abort_store(code: String, message: String, store: WalnutClientStore) -> void:
	var detail := message
	if not store.last_error.is_empty():
		var reported: Dictionary = store.last_error
		if reported.get("error") is Dictionary:
			reported = reported.error
		detail += " %s" % str(reported.get("code", "CLIENT_ERROR"))
		if not str(reported.get("message", "")).is_empty():
			detail += ": %s" % str(reported.message)
	_abort(code, detail)


func _abort(code: String, message: String) -> void:
	push_error("REAL_GATEWAY_CHAIN_E2E_FAIL [%s] %s" % [code, message])
	quit(1)
