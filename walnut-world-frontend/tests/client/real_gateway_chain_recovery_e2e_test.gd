extends SceneTree

## Recovery-only half of the opt-in cross-process acceptance.  A first Godot
## process must already have completed the real Gateway chain.  This second OS
## process opens the same ClientStore persistence identity and lets the formal
## AppRoot rebuild every display projection from the one public Gateway.  It
## never calls a mutating SessionController flow.

const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const HttpTransport := preload("res://addons/yaya_contract_client/http_agent_api_transport.gd")
const GameGateway := preload("res://addons/yaya_contract_client/agent_api_gateway.gd")
const RequestContexts := preload("res://autoload/request_context_factory.gd")
const ACTIVE_TUPLE_FIELDS := [
	"activation_id",
	"skill_id",
	"skill_version_id",
	"artifact_sha256",
	"certification_id",
	"registry_revision",
	"activated_at",
]
const MAX_SAFE_JSON_INTEGER := 9007199254740991.0
const DEFAULT_TOTAL_DEADLINE_SECONDS := 180.0


func _initialize() -> void:
	if (
		OS.get_environment("YAYA_REAL_GATEWAY_E2E") != "1"
		or OS.get_environment("YAYA_REAL_GATEWAY_E2E_RECOVERY_ONLY") != "1"
	):
		print("REAL_GATEWAY_CHAIN_RECOVERY_SKIP: use the recovery-only real Gateway runner.")
		quit(0)
		return

	var base_url := OS.get_environment("YAYA_API_BASE_URL").strip_edges()
	var bearer_token := OS.get_environment("YAYA_AUTH_TOKEN").strip_edges()
	if base_url.is_empty() or bearer_token.is_empty():
		_abort("CONFIGURATION_MISSING", "YAYA_API_BASE_URL and YAYA_AUTH_TOKEN are required.")
		return
	var total_seconds := _positive_environment_seconds(
		"YAYA_REAL_GATEWAY_E2E_TOTAL_DEADLINE_SECONDS", DEFAULT_TOTAL_DEADLINE_SECONDS,
	)
	if total_seconds <= 0.0:
		_abort("E2E_SETTINGS_INVALID", "The recovery deadline must be positive.")
		return
	var absolute_deadline := Time.get_ticks_msec() + ceili(total_seconds * 1000.0)

	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	var controller := root.get_node_or_null("SessionController")
	if store == null or controller == null:
		_abort("AUTOLOAD_UNAVAILABLE", "ClientStore and SessionController autoloads are required.")
		return
	var persistence_identity := WalnutClientStore.normalize_api_base_url(base_url).sha256_text().left(16)
	var persistence_path := "user://real_gateway_chain_%s.json" % persistence_identity
	var phase1_fingerprint_path := OS.get_environment("YAYA_REAL_GATEWAY_E2E_PHASE1_FINGERPRINT_PATH").strip_edges()
	if phase1_fingerprint_path.is_empty():
		_abort("PHASE1_FINGERPRINT_PATH_MISSING", "Recovery-only acceptance requires the persisted fingerprint emitted by this run's phase 1.")
		return
	var expected_result := _load_phase1_fingerprint(phase1_fingerprint_path)
	if not expected_result.ok:
		_abort(str(expected_result.code), str(expected_result.message))
		return
	var expected_phase1: Dictionary = expected_result.value
	var expected_authority: Variant = expected_phase1.get("authority_fingerprint")
	var expected_skill_patch: Variant = expected_phase1.get("skill_patch")
	var skill_patch_enabled := OS.get_environment("YAYA_REAL_GATEWAY_E2E_ENABLE_SKILL_PATCH") == "1"
	if (
		str(expected_phase1.get("phase1_fingerprint_schema", "")) != "1.0.0"
		or not expected_authority is Dictionary
		or str(expected_authority.get("persistence_identity", "")) != persistence_identity
	):
		_abort("PHASE1_FINGERPRINT_IDENTITY_MISMATCH", "The supplied phase-1 fingerprint does not name this exact API-origin persistence identity.")
		return
	if skill_patch_enabled and (
		not expected_skill_patch is Dictionary
		or expected_skill_patch.get("enabled") != true
		or str(expected_skill_patch.get("status", "")) != "PUBLIC_UI_CHAIN_CLOSED"
		or expected_skill_patch.get("backend_authority_fingerprint_required") != true
		or expected_skill_patch.get("expected_transport_counts") != {"POST": 12, "PUT": 1}
		or expected_skill_patch.get("expected_backend_counts") != {
			"turns": 6, "runs": 5, "learner_jobs": 5,
		}
	):
		_abort("PHASE1_SKILL_PATCH_FINGERPRINT_INVALID", "The supplied phase-1 fingerprint has no exact public M2 Patch closure or external authority requirement.")
		return
	if not store.configure_persistence(persistence_path, true, true):
		_abort("PERSISTENCE_CONFIGURATION_FAILED", "The recovery process could not open the canonical ClientStore path.")
		return
	var persisted_guard := _verify_persisted_authority(
		store, WalnutClientStore.normalize_api_base_url(base_url),
	)
	if not persisted_guard.ok:
		_abort(str(persisted_guard.code), str(persisted_guard.message))
		return
	var persisted_session: Dictionary = store.authoritative_session.duplicate(true)
	var persisted_active: Dictionary = store.active_skill_tuple.duplicate(true)
	var persisted_world: Dictionary = store.world_snapshot.duplicate(true)
	var persisted_interaction_sequence := store.last_interaction_sequence
	var persistence_bytes := FileAccess.get_file_as_string(persistence_path)
	if persistence_bytes.is_empty():
		_abort("PERSISTED_STORE_EMPTY", "The canonical ClientStore file is absent or empty.")
		return
	if str(expected_authority.get("persistence_sha256", "")) != persistence_bytes.sha256_text():
		_abort("PHASE1_PERSISTENCE_BYTES_DRIFT", "Recovery did not open the exact persistence bytes fingerprinted by this run's phase 1.")
		return
	# Fail before AppRoot can choose its recover-or-create branch unless public
	# Bootstrap already names the exact persisted Session and active tuple.
	var preflight_host := Node.new()
	preflight_host.name = "RecoveryPreflightHost"
	root.add_child(preflight_host)
	await process_frame
	if not is_instance_valid(preflight_host) or not preflight_host.is_inside_tree():
		_abort("RECOVERY_PREFLIGHT_HOST_UNAVAILABLE", "The recovery preflight HTTP host did not enter the scene tree.")
		return
	var preflight_transport := HttpTransport.new(preflight_host, base_url, bearer_token)
	var preflight_gateway := GameGateway.new(preflight_transport)
	var preflight_result: Dictionary = await preflight_gateway.get_student_bootstrap(
		RequestContexts.new_wire_attempt(),
	)
	preflight_transport.shutdown()
	preflight_host.queue_free()
	if not preflight_result.get("ok", false):
		_abort("RECOVERY_PREFLIGHT_FAILED", str(preflight_result))
		return
	var preflight_bootstrap: Dictionary = preflight_result.value
	var preflight_validation := ContractValidator.validate_student_bootstrap_v2(preflight_bootstrap)
	if not preflight_validation.ok:
		_abort("RECOVERY_PREFLIGHT_INVALID", str(preflight_validation.error))
		return
	if (
		str(preflight_bootstrap.get("session", {}).get("current_session_id", ""))
		!= str(persisted_session.session_id)
		or preflight_bootstrap.get("activation", {}).get("active") != persisted_active
	):
		_abort("RECOVERY_PREFLIGHT_AUTHORITY_DRIFT", "Public Bootstrap would not take AppRoot's read-only Session recovery branch.")
		return

	var recovered_interactions: Array[Dictionary] = []
	controller.interactions_recovered.connect(func(values: Array[Dictionary]) -> void:
		recovered_interactions.assign(values.map(func(value: Dictionary) -> Dictionary:
			return value.duplicate(true)
		))
	)
	var packed := load("res://scenes/app/app_root.tscn") as PackedScene
	if packed == null:
		_abort("APP_ROOT_SCENE_MISSING", "The formal AppRoot scene could not be loaded.")
		return
	var app := packed.instantiate()
	var presentation_enabled := OS.get_environment("YAYA_REAL_GATEWAY_E2E_ENABLE_WORLD_PRESENTATION") == "1"
	if skill_patch_enabled and not presentation_enabled:
		_abort("SKILL_PATCH_M1_GATE_REQUIRED", "Skill Patch recovery requires authoritative World presentation recovery.")
		return
	app.world_presentation_enabled = presentation_enabled
	app.skill_patch_enabled = skill_patch_enabled
	app.poller_settings_override = {
		"deadline_seconds": total_seconds,
		"interaction_deadline_seconds": total_seconds,
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
		_abort("APP_STARTUP_TIMEOUT", "Recovery-only AppRoot startup exceeded its deadline.")
		return
	if not bool(startup.result.get("ok", false)):
		_abort("APP_STARTUP_FAILED", str(startup.result))
		return
	if not _production_clients_are_wired(app):
		_abort("PRODUCTION_CLIENT_REQUIRED", "Recovery did not use the production HTTP transport and Gateways.")
		return
	var presentation_high_watermark := 0
	if presentation_enabled:
		var presentation_player := app.get_node_or_null("WorldEventPlayer")
		if presentation_player == null or not presentation_player.has_method("get_cursor"):
			_abort("PRESENTATION_PLAYER_UNAVAILABLE", "Recovery did not install the formal WorldEventPlayer cursor authority.")
			return
		presentation_high_watermark = int(presentation_player.call("get_cursor"))
		if presentation_high_watermark < 0:
			_abort("PRESENTATION_CURSOR_INVALID", "Recovery produced a negative presentation high watermark.")
			return

	var bootstrap: Dictionary = store.authoritative_bootstrap.duplicate(true)
	var bootstrap_guard := ContractValidator.validate_student_bootstrap_v2(bootstrap)
	if not bootstrap_guard.ok:
		_abort("BOOTSTRAP_INVALID", str(bootstrap_guard.error.get("message", "StudentBootstrapV2 validation failed.")))
		return
	var recovery_guard := _verify_recovered_authority(
		store,
		bootstrap,
		persisted_session,
		persisted_active,
		persisted_world,
		persisted_interaction_sequence,
		recovered_interactions,
		presentation_enabled,
		presentation_high_watermark,
		expected_authority,
		persistence_identity,
		persistence_bytes.sha256_text(),
	)
	if not recovery_guard.ok:
		_abort(str(recovery_guard.code), str(recovery_guard.message))
		return
	var interaction: Dictionary = recovery_guard.interaction
	var recovered_skill_patch := {
		"enabled": false,
		"status": "DISABLED",
		"backend_authority_fingerprint_required": false,
	}
	if skill_patch_enabled:
		var recovered_m2_guard: Dictionary = await _verify_recovered_m2_public_read_closure(
			app.get("_game_gateway"),
			app.get("_product_gateway"),
			bootstrap,
			expected_skill_patch,
			recovered_interactions,
			store.draft,
			interaction,
			absolute_deadline,
		)
		if not recovered_m2_guard.ok:
			_abort(str(recovered_m2_guard.code), str(recovered_m2_guard.message))
			return
		recovered_skill_patch = recovered_m2_guard.value
		if recovered_skill_patch != expected_skill_patch:
			_abort("PHASE1_SKILL_PATCH_FINGERPRINT_DRIFT", "GET-only recovery reconstructed a different public Patch/Decision/Draft/Build/Activation/Run fingerprint.")
			return
	await process_frame
	await process_frame
	var ui_guard := _verify_formal_ui_projection(app, store, interaction)
	if not ui_guard.ok:
		_abort(str(ui_guard.code), str(ui_guard.message))
		return
	var transport_audit_guard := _verify_read_only_transport_audit(app.get("_transport"))
	if not transport_audit_guard.ok:
		_abort(str(transport_audit_guard.code), str(transport_audit_guard.message))
		return

	var workspace: Dictionary = store.workspace
	var draft: Dictionary = store.draft
	var snapshot: Dictionary = store.world_snapshot
	var authority_fingerprint := _bind_presentation_authority_fingerprint(_authority_fingerprint(
		store, workspace, draft, snapshot, interaction, persistence_identity, persistence_bytes.sha256_text(),
	), presentation_enabled, presentation_high_watermark)
	var fingerprint := {
		"recovery_only": true,
		"persisted_store_loaded": true,
		"phase1_fingerprint_schema": str(expected_phase1.phase1_fingerprint_schema),
		"expected_phase1_fingerprint_sha256": str(expected_result.sha256),
		"phase1_authority_exact_match": true,
		"persistence_identity": persistence_identity,
		"persistence_sha256": persistence_bytes.sha256_text(),
		"session_id": str(store.authoritative_session.session_id),
		"workspace_id": str(workspace.workspace_id),
		"workspace_revision": int(workspace.workspace_revision),
		"workspace_sha256": JSON.stringify(workspace).sha256_text(),
		"draft_id": str(draft.draft_id),
		"draft_revision": int(draft.revision),
		"draft_sha256": str(draft.draft_sha256),
		"draft_source_sha256": store.local_source.sha256_text(),
		"activation_id": str(store.active_skill_tuple.activation_id),
		"active_skill_tuple": store.active_skill_tuple.duplicate(true),
		"active_skill_tuple_sha256": _active_tuple_sha256(store.active_skill_tuple),
		"world_id": str(snapshot.world_id),
		"world_revision": int(snapshot.revision),
		"last_event_sequence": int(snapshot.last_event_sequence),
		"world_state_hash": str(snapshot.state_hash),
		"world_presentation": {
			"enabled": presentation_enabled,
			"recovered_by_snapshot": presentation_enabled,
			"presentation_high_watermark": int(app.get_node("WorldEventPlayer").get_cursor()) if presentation_enabled else 0,
		},
		"skill_patch": recovered_skill_patch,
		"phase1_skill_patch_exact_match": (
			not skill_patch_enabled
			or recovered_skill_patch == expected_skill_patch
		),
		"interaction_id": str(interaction.interaction_id),
		"turn_id": str(interaction.turn_id),
		"command_id": str(interaction.feedback.command_id),
		"run_id": str(interaction.feedback.run_id),
		"interaction_sequence": int(interaction.sequence),
		"interaction_revision": int(interaction.interaction_revision),
		"interaction_role": str(interaction.role),
		"interaction_feedback_sha256": JSON.stringify(interaction.feedback).sha256_text(),
		"authority_fingerprint": authority_fingerprint,
		"transport_attempt_audit": transport_audit_guard.value,
		"live_pending_response_loss": {
			"status": "NOT_PROVEN",
			"reason": "The live Gateway contract exposes no acceptance-safe response-loss fault injection; focused cross-store recovery tests cover this path offline.",
		},
		"ui_display": ui_guard.value,
		"persistence_cleanup_performed": false,
		"persistence_cleanup_residual_count": null,
	}
	if OS.get_environment("YAYA_REAL_GATEWAY_E2E_CLEANUP_PERSISTENCE") == "1":
		var cleanup_result := _remove_exact_persistence_family(persistence_path, true)
		if not cleanup_result.ok:
			_abort(str(cleanup_result.code), str(cleanup_result.message))
			return
		fingerprint.persistence_cleanup_performed = true
		fingerprint.persistence_cleanup_residual_count = int(cleanup_result.residual_count)
	print("REAL_GATEWAY_CHAIN_RECOVERY_PASS %s" % JSON.stringify(fingerprint))
	quit(0)


func _verify_recovered_m2_public_read_closure(
	game_gateway: RefCounted,
	product_gateway: RefCounted,
	bootstrap: Dictionary,
	expected: Dictionary,
	interactions: Array[Dictionary],
	accepted_draft: Dictionary,
	final_interaction: Dictionary,
	absolute_deadline: int,
) -> Dictionary:
	if Time.get_ticks_msec() >= absolute_deadline:
		return _failure("M2_RECOVERY_PUBLIC_READ_DEADLINE_EXCEEDED", "M2 GET-only public read closure began after the recovery deadline.")
	var proposal: Dictionary = {}
	for interaction_value: Dictionary in interactions:
		if str(interaction_value.get("interaction_id", "")) == str(expected.proposal_interaction_id):
			if not proposal.is_empty():
				return _failure("M2_RECOVERY_PROPOSAL_AMBIGUOUS", "Recovered Interaction list contains the Patch proposal identity more than once.")
			proposal = interaction_value.duplicate(true)
	if proposal.is_empty():
		return _failure("M2_RECOVERY_PROPOSAL_MISSING", "Recovered Interaction list contains no exact decided Patch proposal.")
	var patch: Variant = proposal.get("skill_patch")
	var decision: Variant = proposal.get("patch_decision")
	var feedback: Variant = proposal.get("feedback")
	if (
		not patch is Dictionary
		or not decision is Dictionary
		or not feedback is Dictionary
		or int(proposal.get("sequence", -1)) != 5
		or int(proposal.get("interaction_revision", -1)) != 2
		or str(proposal.get("role", "")) != "teaching_agent"
		or str(proposal.get("response_type", "")) != "skill_patch"
		or feedback.get("run_id") != null
		or str(patch.get("patch_id", "")) != str(expected.patch_id)
		or str(decision.get("decision_id", "")) != str(expected.decision_id)
		or str(decision.get("decision", "")) != "ACCEPT"
	):
		return _failure("M2_RECOVERY_PROPOSAL_INVALID", "Recovered Patch proposal/ACCEPT projection does not equal the public M2 wire authority.")
	var session_id := str(accepted_draft.session_id)
	var command_result: Dictionary = await game_gateway.get_command(
		_new_context(bootstrap), str(feedback.command_id),
	)
	var proposal_result: Dictionary = await product_gateway.get_interaction(
		_new_context(bootstrap), session_id, str(proposal.interaction_id),
	)
	var draft_result: Dictionary = await product_gateway.get_draft(
		_new_context(bootstrap), session_id, str(expected.accepted_draft_id),
	)
	var build_result: Dictionary = await game_gateway.get_skill_build(
		_new_context(bootstrap), str(expected.build_id),
	)
	var activation_result: Dictionary = await game_gateway.get_skill_activation(
		_new_context(bootstrap), str(expected.activation_id),
	)
	var run_result: Dictionary = await game_gateway.get_run(
		_new_context(bootstrap), str(expected.run_id),
	)
	var final_interaction_result: Dictionary = await product_gateway.get_interaction(
		_new_context(bootstrap), session_id, str(final_interaction.interaction_id),
	)
	for named_result in [
		{"name": "proposal Command", "result": command_result},
		{"name": "proposal Interaction", "result": proposal_result},
		{"name": "accepted Draft", "result": draft_result},
		{"name": "corrected Build", "result": build_result},
		{"name": "corrected Activation", "result": activation_result},
		{"name": "successful Run", "result": run_result},
		{"name": "final Interaction", "result": final_interaction_result},
	]:
		if not named_result.result.get("ok", false):
			return _gateway_failure("M2_RECOVERY_PUBLIC_READ_FAILED", named_result.result)
	if (
		proposal_result.value != proposal
		or draft_result.value != accepted_draft
		or final_interaction_result.value != final_interaction
	):
		return _failure("M2_RECOVERY_PUBLIC_RESOURCE_DRIFT", "GET-only recovery returned different Interaction or accepted Draft bytes.")
	var command: Dictionary = command_result.value
	var build: Dictionary = build_result.value
	var activation: Dictionary = activation_result.value
	var run: Dictionary = run_result.value
	var command_validation := ContractValidator.validate_command_result(command)
	var build_validation := ContractValidator.validate_skill_build(build)
	var activation_validation := ContractValidator.validate_skill_activation(activation)
	var run_validation := ContractValidator.validate_run(run)
	if (
		not command_validation.ok
		or not build_validation.ok
		or not activation_validation.ok
		or not run_validation.ok
		or str(command.get("status", "")) != "APPLIED"
		or command.get("result") != {"result_type": "NO_EFFECT", "reason_code": "SKILL_PATCH_PROPOSED"}
		or command.get("links", {}).has("run")
		or str(command.get("command_id", "")) != str(feedback.command_id)
		or str(build.get("status", "")) != "CERTIFIED"
		or not bool(build.get("terminal", false))
		or str(build.skill_id) != str(activation.skill_id)
		or str(build.skill_version_id) != str(activation.skill_version_id)
		or str(build.artifact.artifact_sha256) != str(activation.artifact_sha256)
		or str(build.certification.certification_id) != str(activation.certification_id)
		or str(activation.activation_id) != str(expected.activation_id)
		or int(activation.registry_revision) != 2
		or run.skill != {
			"skill_id": activation.skill_id,
			"skill_version_id": activation.skill_version_id,
			"artifact_sha256": activation.artifact_sha256,
			"certification_id": activation.certification_id,
		}
		or str(run.get("status", "")) != "SUCCEEDED"
		or not bool(run.get("terminal", false))
		or int(final_interaction.get("sequence", -1)) != 6
		or str(final_interaction.get("role", "")) != "book_agent"
		or str(final_interaction.get("feedback", {}).get("run_id", "")) != str(run.run_id)
	):
		return _failure("M2_RECOVERY_PUBLIC_RESOURCE_CROSS_LINK_INVALID", "Recovered Command/Draft/Build/Activation/Run/Interaction resources do not form the exact M2 public chain.")
	var public_hashes := {
		"proposal_command_sha256": JSON.stringify(command).sha256_text(),
		"proposal_interaction_sha256": JSON.stringify(proposal).sha256_text(),
		"patch_sha256": str(patch.patch_sha256),
		"decision_sha256": JSON.stringify(decision).sha256_text(),
		"accepted_draft_resource_sha256": JSON.stringify(accepted_draft).sha256_text(),
		"build_resource_sha256": JSON.stringify(build).sha256_text(),
		"activation_resource_sha256": JSON.stringify(activation).sha256_text(),
		"run_resource_sha256": JSON.stringify(run).sha256_text(),
		"final_interaction_sha256": JSON.stringify(final_interaction).sha256_text(),
	}
	return {"ok": true, "value": {
		"enabled": true,
		"status": "PUBLIC_UI_CHAIN_CLOSED",
		"backend_authority_fingerprint_required": true,
		"expected_transport_counts": {"POST": 12, "PUT": 1},
		"expected_backend_counts": {"turns": 6, "runs": 5, "learner_jobs": 5},
		"formal_actions": ["REQUEST_PATCH", "ACCEPT_PATCH", "BUILD", "ACTIVATE", "SUBMIT"],
		"proposal_interaction_id": str(proposal.interaction_id),
		"patch_id": str(patch.patch_id),
		"decision_id": str(decision.decision_id),
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


func _verify_persisted_authority(store: WalnutClientStore, normalized_base_url: String) -> Dictionary:
	if (
		str(store.authority_binding.get("api_base_url", "")) != normalized_base_url
		or store.authority_binding.get("actor") != store.authoritative_bootstrap.get("actor")
		or store.authority_binding.get("content") != store.authoritative_bootstrap.get("content")
	):
		return _failure("PERSISTED_AUTHORITY_BINDING_INVALID", "Persisted authority is not bound to the normalized API origin and exact actor/content identity.")
	var session_guard := ContractValidator.validate_agent_session(store.authoritative_session)
	if not session_guard.ok:
		return _failure("PERSISTED_SESSION_INVALID", "The second process did not load a valid persisted AgentSession.")
	if not _valid_active_tuple(store.active_skill_tuple):
		return _failure("PERSISTED_ACTIVE_TUPLE_INVALID", "The second process did not load the exact active Skill tuple.")
	var snapshot: Dictionary = store.world_snapshot
	if (
		str(snapshot.get("world_id", "")).is_empty()
		or int(snapshot.get("revision", -1)) < 1
		or int(snapshot.get("last_event_sequence", -1)) < 1
		or not _valid_sha256(str(snapshot.get("state_hash", "")))
	):
		return _failure("PERSISTED_WORLD_INVALID", "The second process did not load a terminal World cursor and hash.")
	if store.last_interaction_sequence < 1:
		return _failure("PERSISTED_INTERACTION_CURSOR_INVALID", "The completed Interaction cursor was not persisted.")
	if not store.pending_operations.is_empty():
		return _failure("PERSISTED_OPERATION_STILL_PENDING", "Recovery-only acceptance requires every prior write envelope to be terminal.")
	return {"ok": true}


func _verify_recovered_authority(
	store: WalnutClientStore,
	bootstrap: Dictionary,
	persisted_session: Dictionary,
	persisted_active: Dictionary,
	persisted_world: Dictionary,
	persisted_interaction_sequence: int,
	interactions: Array[Dictionary],
	presentation_enabled: bool,
	presentation_high_watermark: int,
	expected_authority: Dictionary,
	persistence_identity: String,
	persistence_sha256: String,
) -> Dictionary:
	if (
		str(bootstrap.get("session", {}).get("current_session_id", "")) != str(persisted_session.session_id)
		or store.authoritative_session != persisted_session
	):
		return _failure("SESSION_RECOVERY_DRIFT", "Public Bootstrap/Session recovery changed the persisted Session identity or bytes.")
	if (
		bootstrap.get("activation", {}).get("active") != persisted_active
		or store.active_skill_tuple != persisted_active
	):
		return _failure("ACTIVE_TUPLE_RECOVERY_DRIFT", "Public activation authority changed the exact persisted Skill tuple.")
	if store.world_snapshot != persisted_world:
		return _failure("WORLD_RECOVERY_DRIFT", "Public Snapshot recovery changed the persisted World revision, sequence, state, or hash.")
	var workspace: Dictionary = store.workspace
	var draft: Dictionary = store.draft
	if workspace.is_empty() or draft.is_empty():
		return _failure("WORKSPACE_RECOVERY_INCOMPLETE", "Workspace or Draft was not rebuilt from public authority.")
	if workspace.get("session") != store.authoritative_session:
		return _failure("WORKSPACE_SESSION_DRIFT", "Workspace does not embed the exact recovered Session.")
	var refs: Variant = workspace.get("skill_draft_refs")
	var expected_ref := {
		"draft_id": draft.get("draft_id"),
		"skill_id": draft.get("skill_id"),
		"revision": draft.get("revision"),
		"draft_sha256": draft.get("draft_sha256"),
		"url": "/product-experience/v1/sessions/%s/skill-drafts/%s" % [
			str(draft.get("session_id", "")), str(draft.get("draft_id", "")),
		],
	}
	if (
		str(draft.get("session_id", "")) != str(persisted_session.session_id)
		or not refs is Array
		or refs.size() != 1
		or refs[0] != expected_ref
		or not _valid_sha256(str(draft.get("draft_sha256", "")))
		or store.local_source.sha256_text() != _entrypoint_sha256(draft)
	):
		return _failure("DRAFT_RECOVERY_DRIFT", "Workspace and Draft do not close over one exact canonical source bundle.")
	var checkpoint: Variant = workspace.get("world_checkpoint")
	if (
		not checkpoint is Dictionary
		or str(checkpoint.get("world_id", "")) != str(store.world_snapshot.world_id)
		or int(checkpoint.get("world_revision", -1)) > int(store.world_snapshot.revision)
		or int(checkpoint.get("last_event_sequence", -1)) > int(store.world_snapshot.last_event_sequence)
	):
		return _failure("WORKSPACE_WORLD_DRIFT", "Workspace checkpoint is not a valid prefix of the recovered World authority.")
	if interactions.is_empty():
		return _failure("INTERACTION_RECOVERY_EMPTY", "AppRoot did not rebuild Product AgentInteraction state.")
	var interaction: Dictionary = interactions.back()
	var feedback: Variant = interaction.get("feedback")
	if (
		str(interaction.get("session_id", "")) != str(persisted_session.session_id)
		or int(interaction.get("sequence", -1)) != persisted_interaction_sequence
		or store.last_interaction_sequence != persisted_interaction_sequence
		or not feedback is Dictionary
		or str(feedback.get("source", "")) != "provider"
		or bool(feedback.get("degraded", true))
		or feedback.get("fallback_reason") != null
	):
		return _failure("INTERACTION_RECOVERY_DRIFT", "Recovered Interaction is not the exact non-degraded terminal projection.")
	var recovered_fingerprint := _bind_presentation_authority_fingerprint(_authority_fingerprint(
		store, workspace, draft, store.world_snapshot, interaction, persistence_identity, persistence_sha256,
	), presentation_enabled, presentation_high_watermark)
	for field in expected_authority:
		if not recovered_fingerprint.has(field) or recovered_fingerprint[field] != expected_authority[field]:
			return _failure("PHASE1_AUTHORITY_FINGERPRINT_DRIFT", "Recovery field %s does not exactly match this run's persisted phase-1 authority fingerprint." % str(field))
	for field in recovered_fingerprint:
		if not expected_authority.has(field):
			return _failure("PHASE1_AUTHORITY_FINGERPRINT_SHAPE_DRIFT", "Recovery produced an authority field absent from this run's phase-1 fingerprint: %s." % str(field))
	return {"ok": true, "interaction": interaction.duplicate(true)}


func _bind_presentation_authority_fingerprint(
	fingerprint: Dictionary,
	presentation_enabled: bool,
	presentation_high_watermark: int,
) -> Dictionary:
	var bound := fingerprint.duplicate(true)
	bound["presentation_high_watermark"] = presentation_high_watermark if presentation_enabled else 0
	return bound


func _verify_formal_ui_projection(app: Node, store: WalnutClientStore, interaction: Dictionary) -> Dictionary:
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
		return _failure("TASK_WORKSPACE_PROJECTION_MISMATCH", "TaskWorkspace does not display the recovered Content and Draft.")

	var dialogue := task_workspace.get_node_or_null("DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/DialoguePanel") as PanelContainer
	var dialogue_text: Label = null
	if dialogue != null:
		dialogue_text = dialogue.get_node_or_null("Margin/Content/DialogueText") as Label
	if (
		dialogue == null
		or dialogue.get_script() == null
		or str(dialogue.get_script().resource_path) != "res://scenes/task/dialogue_panel.gd"
		or dialogue_text == null
		or str(dialogue_text.text) != str(interaction.get("feedback", {}).get("message", ""))
	):
		return _failure("DIALOGUE_PROJECTION_MISMATCH", "DialoguePanel does not display the recovered AgentInteraction feedback.")

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
	var state: Variant = store.world_snapshot.get("state")
	var avatar_value: Variant = state.get("avatar") if state is Dictionary else null
	var avatar_position: Variant = avatar_value.get("position") if avatar_value is Dictionary else null
	if terrain == null or avatar == null or not state is Dictionary or not avatar_position is Dictionary:
		return _failure("WORLD_VIEWPORT_PROJECTION_UNAVAILABLE", "WorldViewport has no recovered terrain/avatar projection target.")
	var avatar_cell := Vector2i(int(avatar_position.get("x", -1)), int(avatar_position.get("y", -1)))
	if not terrain.map_data.is_inside_map(avatar_cell) or terrain.map_data.world_to_cell(avatar.global_position) != avatar_cell:
		return _failure("WORLD_VIEWPORT_AVATAR_MISMATCH", "WorldViewport avatar does not equal the recovered Snapshot.")
	for plot_value in state.get("plots", []):
		if not plot_value is Dictionary or not plot_value.get("position") is Dictionary:
			return _failure("WORLD_VIEWPORT_PLOT_INVALID", "Recovered Snapshot contains an invalid plot.")
		var cell := Vector2i(int(plot_value.position.x), int(plot_value.position.y))
		var expected_cell_type := TerrainMapData.CellType.GRASS
		if str(plot_value.get("soil_state", "")) == "TILLED":
			expected_cell_type = TerrainMapData.CellType.FARMLAND if int(plot_value.get("hydration", 0)) > 0 else TerrainMapData.CellType.DIRT
		if not terrain.map_data.is_inside_map(cell) or terrain.map_data.get_cell(cell) != expected_cell_type:
			return _failure("WORLD_VIEWPORT_TERRAIN_MISMATCH", "WorldViewport terrain does not equal the recovered Snapshot.")
	return {
		"ok": true,
		"value": {
			"task_workspace": true,
			"dialogue_panel": true,
			"world_viewport": true,
		},
	}


func _load_phase1_fingerprint(path: String) -> Dictionary:
	if not FileAccess.file_exists(path):
		return _failure("PHASE1_FINGERPRINT_MISSING", "The supplied phase-1 fingerprint file does not exist.")
	var source := FileAccess.get_file_as_string(path)
	if source.is_empty():
		return _failure("PHASE1_FINGERPRINT_EMPTY", "The supplied phase-1 fingerprint file is empty.")
	var parsed: Variant = JSON.parse_string(source)
	if not parsed is Dictionary:
		return _failure("PHASE1_FINGERPRINT_INVALID", "The supplied phase-1 fingerprint is not one JSON object.")
	return {"ok": true, "value": _normalize_json_integers(parsed), "sha256": source.sha256_text()}


static func _normalize_json_integers(value: Variant) -> Variant:
	match typeof(value):
		TYPE_FLOAT:
			if (
				is_finite(value)
				and value == floor(value)
				and value >= -MAX_SAFE_JSON_INTEGER
				and value <= MAX_SAFE_JSON_INTEGER
			):
				return int(value)
		TYPE_ARRAY:
			var normalized_array: Array = []
			for item in value:
				normalized_array.append(_normalize_json_integers(item))
			return normalized_array
		TYPE_DICTIONARY:
			var normalized_dictionary := {}
			for key in value:
				normalized_dictionary[key] = _normalize_json_integers(value[key])
			return normalized_dictionary
	return value


func _authority_fingerprint(
	store: WalnutClientStore,
	workspace: Dictionary,
	draft: Dictionary,
	snapshot: Dictionary,
	interaction: Dictionary,
	persistence_identity: String,
	persistence_sha256: String,
) -> Dictionary:
	return {
		"persistence_identity": persistence_identity,
		"persistence_sha256": persistence_sha256,
		"authority_binding": store.authority_binding.duplicate(true),
		"authority_binding_sha256": JSON.stringify(store.authority_binding).sha256_text(),
		"session": store.authoritative_session.duplicate(true),
		"session_sha256": JSON.stringify(store.authoritative_session).sha256_text(),
		"workspace_id": str(workspace.workspace_id),
		"workspace_revision": int(workspace.workspace_revision),
		"workspace_sha256": JSON.stringify(workspace).sha256_text(),
		"draft_id": str(draft.draft_id),
		"draft_revision": int(draft.revision),
		"draft_sha256": str(draft.draft_sha256),
		"draft_resource_sha256": JSON.stringify(draft).sha256_text(),
		"draft_source_sha256": store.local_source.sha256_text(),
		"active_skill_tuple": store.active_skill_tuple.duplicate(true),
		"active_skill_tuple_sha256": _active_tuple_sha256(store.active_skill_tuple),
		"world_id": str(snapshot.world_id),
		"world_revision": int(snapshot.revision),
		"last_event_sequence": int(snapshot.last_event_sequence),
		"world_state_hash": str(snapshot.state_hash),
		"world_snapshot_sha256": JSON.stringify(snapshot).sha256_text(),
		"interaction_id": str(interaction.interaction_id),
		"turn_id": str(interaction.turn_id),
		"interaction_sequence": int(interaction.sequence),
		"interaction_revision": int(interaction.interaction_revision),
		"interaction_role": str(interaction.role),
		"interaction_sha256": JSON.stringify(interaction).sha256_text(),
		"interaction_feedback": interaction.feedback.duplicate(true),
		"interaction_feedback_sha256": JSON.stringify(interaction.feedback).sha256_text(),
	}


func _verify_read_only_transport_audit(transport: Variant) -> Dictionary:
	if not transport is Object or not transport.has_method("get_attempt_audit"):
		return _failure("TRANSPORT_ATTEMPT_AUDIT_UNAVAILABLE", "The production recovery transport exposes no queryable attempt audit.")
	var audit: Dictionary = transport.get_attempt_audit()
	var started := int(audit.get("total_started", -1))
	if started <= 0 or started != int(audit.get("total_completed", -2)):
		return _failure("TRANSPORT_ATTEMPT_AUDIT_INCOMPLETE", "Recovery HTTP audit contains no traffic or an unfinished attempt.")
	var method_counts: Variant = audit.get("method_counts")
	if not method_counts is Dictionary or int(method_counts.get("GET", 0)) != started:
		return _failure("RECOVERY_TRANSPORT_NOT_READ_ONLY", "Formal AppRoot recovery did not consist exclusively of audited GET attempts.")
	for method in method_counts:
		if str(method) != "GET" and int(method_counts[method]) != 0:
			return _failure("RECOVERY_TRANSPORT_MUTATION_ATTEMPTED", "Formal AppRoot recovery attempted audited mutating method %s." % str(method))
	var recent_attempts: Variant = audit.get("recent_attempts")
	if not recent_attempts is Array:
		return _failure("TRANSPORT_ATTEMPT_HISTORY_INVALID", "Recovery HTTP audit has no bounded attempt history.")
	for attempt in recent_attempts:
		if (
			not attempt is Dictionary
			or str(attempt.get("method", "")) != "GET"
			or not bool(attempt.get("completed", false))
		):
			return _failure("RECOVERY_TRANSPORT_ATTEMPT_INVALID", "A bounded recovery HTTP attempt is mutating or incomplete.")
	return {"ok": true, "value": audit.duplicate(true)}


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


func _valid_active_tuple(value: Dictionary) -> bool:
	if value.size() != ACTIVE_TUPLE_FIELDS.size():
		return false
	for field in ACTIVE_TUPLE_FIELDS:
		if not value.has(field):
			return false
	return (
		not str(value.activation_id).is_empty()
		and not str(value.skill_id).is_empty()
		and not str(value.skill_version_id).is_empty()
		and _valid_sha256(str(value.artifact_sha256))
		and not str(value.certification_id).is_empty()
		and int(value.registry_revision) >= 1
	)


func _active_tuple_sha256(value: Dictionary) -> String:
	var ordered: Array = []
	for field in ACTIVE_TUPLE_FIELDS:
		ordered.append(value.get(field))
	return JSON.stringify(ordered).sha256_text()


func _new_context(bootstrap: Dictionary) -> Dictionary:
	return RequestContexts.new_attempt(bootstrap.actor, bootstrap.content)


func _gateway_failure(code: String, result: Dictionary) -> Dictionary:
	var error: Variant = result.get("error")
	var message := "Gateway request failed with HTTP status %d." % int(result.get("status", 0))
	if error is Dictionary:
		if not str(error.get("code", "")).is_empty():
			code = str(error.code)
		var nested: Variant = error.get("error")
		if nested is Dictionary and not str(nested.get("message", "")).is_empty():
			message = str(nested.message)
		elif not str(error.get("message", "")).is_empty():
			message = str(error.message)
	return _failure(code, message)


func _entrypoint_sha256(draft: Dictionary) -> String:
	var bundle: Variant = draft.get("source_bundle")
	if not bundle is Dictionary or not bundle.get("files") is Array:
		return ""
	var entrypoint := str(bundle.get("entrypoint", ""))
	var found := ""
	for file_value in bundle.files:
		if file_value is Dictionary and str(file_value.get("path", "")) == entrypoint:
			if not found.is_empty():
				return ""
			found = str(file_value.get("content", "")).sha256_text()
			if found != str(file_value.get("content_sha256", "")):
				return ""
	return found


func _valid_sha256(value: String) -> bool:
	if value.length() != 64:
		return false
	for index in range(value.length()):
		if "0123456789abcdef".find(value.substr(index, 1)) < 0:
			return false
	return true


func _remove_exact_persistence_family(path: String, require_target: bool) -> Dictionary:
	if not path.begins_with("user://real_gateway_chain_") or not path.ends_with(".json"):
		return _failure("PERSISTENCE_CLEANUP_SCOPE_INVALID", "Refusing to remove a path outside the exact real-chain test identity.")
	if require_target and not FileAccess.file_exists(path):
		return _failure("PERSISTENCE_CLEANUP_MISSING", "The recovery test state disappeared before exact cleanup.")
	var candidates := [path, "%s.bak" % path, "%s.tmp" % path]
	for candidate in candidates:
		if not FileAccess.file_exists(candidate):
			continue
		var error := DirAccess.remove_absolute(ProjectSettings.globalize_path(candidate))
		if error != OK:
			return _failure("PERSISTENCE_CLEANUP_FAILED", "An exact real-chain persistence target, backup, or temporary file could not be removed.")
	for candidate in candidates:
		if FileAccess.file_exists(candidate):
			return _failure("PERSISTENCE_CLEANUP_RESIDUAL", "An exact real-chain persistence target, backup, or temporary file remains after cleanup.")
	return {"ok": true, "residual_count": 0}


func _positive_environment_seconds(name: String, fallback: float) -> float:
	var raw := OS.get_environment(name).strip_edges()
	if raw.is_empty():
		return fallback
	if not raw.is_valid_float():
		return -1.0
	return float(raw)


func _failure(code: String, message: String) -> Dictionary:
	return {"ok": false, "code": code, "message": message}


func _abort(code: String, message: String) -> void:
	push_error("REAL_GATEWAY_CHAIN_RECOVERY_FAIL [%s] %s" % [code, message])
	quit(1)
